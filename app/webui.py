# -*- coding: utf-8 -*-
"""
Web 控制台

一个很小的 HTTP 服务，只做两件事：
  1. 用一个登录口令把入口关起来（ADMIN_USERNAME / ADMIN_PASSWORD，
     写在 compose 的 environment 里）
  2. 提供查看与修改设置的接口，改完热生效，不用重建容器

安全上的取舍（都是有意的，不是漏的）：
  · 默认**不启动**：没配 ADMIN_PASSWORD 就不开端口，日志里写明原因。
    一个默认开放、能改路由器密码的后台比"没这个功能"危险得多。
  · 口令用 hmac.compare_digest 比较（常量时间），用户名和密码都走同一条比较路径，
    不让人从响应快慢里猜出"用户名对不对"。
  · 登录防爆破，三层：单 IP 5 分钟窗口内错 8 次即锁定 → 反复触发锁定时间逐次翻倍
    （5 分钟起步，上限 6 小时）；所有来源合计错 60 次则全局熔断一个窗口（防换 IP 分摊）；
    每次登录校验前恒定延时 0.25 秒（拉平响应时间、拖慢脚本）。锁定期间密码对也不放行，
    登录成功即清零。
  · 会话令牌只放内存（重启即失效），Cookie 带 HttpOnly + SameSite=Lax；
    勾选「保持登录(7天)」时令牌与 Cookie 均按 7 天有效期签发。
  · 改状态的请求要求 Content-Type 为 application/json 且 Origin（若有）与 Host 一致
    —— 跨站的 form 表单发不出 application/json，配合 SameSite 足够挡掉 CSRF。
  · 不监听 0.0.0.0 之外的地址也做不到（容器里必须），所以文档里写明：
    这是**局域网内**的管理后台，别做端口映射到公网。
  · 只走 HTTP 不上 TLS：局域网自签证书更麻烦，且真正敏感的是"谁能访问到这个端口"。
"""
from __future__ import annotations

import hmac
import http.server
import json
import logging
import os
import secrets
import socketserver
import threading
import time
import urllib.parse

from . import settings as st
from .webpage import PAGE

log = logging.getLogger("ipv6sync.webui")

# 登录防爆破：窗口内允许的失败次数
FAIL_WINDOW = 300.0
FAIL_LIMIT = 8
GLOBAL_FAIL_LIMIT = 60   # 同窗口内全部来源的失败总上限（防换 IP 分摊的分布式猜解）
LOCK_MAX = 6 * 3600      # 单 IP 递进锁定的时长上限（5min → 10min → …）

CSP = ("default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; form-action 'none'; "
       "frame-ancestors 'none'; base-uri 'none'")


class WebUI:
    def __init__(self, node, host: str, port: int, user: str, password: str,
                 session_ttl: int = 12 * 3600):
        self.node = node
        self.host = host
        self.port = port
        self.user = user or "admin"
        self._password = password or ""
        self.session_ttl = max(300, int(session_ttl))
        self.REMEMBER_TTL = max(self.session_ttl, 7 * 24 * 3600)  # 保持登录：7 天

        self._tokens: dict[str, tuple[str, float]] = {}   # token -> (user, 过期时间)
        self._fails: dict[str, list[float]] = {}          # ip -> [失败时刻]
        self._global_fails: list[float] = []              # 所有来源的失败时刻（全局熔断用）
        self._locked_until: dict[str, float] = {}         # ip -> 锁定截止时刻
        self._strikes: dict[str, int] = {}                # ip -> 被锁次数（决定下次锁多久）
        self._lock = threading.Lock()
        self._server: http.server.ThreadingHTTPServer | None = None

    # ---------------- 会话 ----------------

    def enabled(self) -> bool:
        return bool(self._password) and self.port > 0

    def _check_credentials(self, user: str, password: str) -> bool:
        u_ok = hmac.compare_digest((user or "").encode("utf-8"),
                                   self.user.encode("utf-8"))
        p_ok = hmac.compare_digest((password or "").encode("utf-8"),
                                   self._password.encode("utf-8"))
        return bool(u_ok and p_ok)

    def _new_token(self, user: str, ttl: int | None = None) -> str:
        tok = secrets.token_urlsafe(32)
        with self._lock:
            self._tokens[tok] = (user, time.time() + (ttl or self.session_ttl))
        return tok

    def _valid(self, tok: str | None) -> str | None:
        if not tok:
            return None
        with self._lock:
            hit = self._tokens.get(tok)
            if not hit:
                return None
            user, exp = hit
            if exp < time.time():
                self._tokens.pop(tok, None)
                return None
            return user

    def _drop(self, tok: str | None):
        if tok:
            with self._lock:
                self._tokens.pop(tok, None)

    def _throttled(self, ip: str) -> float:
        """返回还需等待的秒数（0 = 可以尝试登录）。

        三层防线：
        · 单 IP 窗口：FAIL_WINDOW 秒内失败 FAIL_LIMIT 次 → 锁定该 IP；
        · 递进锁定：被锁的次数越多锁得越久（5 分钟 → 10 分钟 → …，上限 LOCK_MAX），
          让在线爆破在时间上不划算；
        · 全局熔断：所有来源加起来的失败总数达到 GLOBAL_FAIL_LIMIT →
          整体拒登一个窗口，堵"换一堆 IP 分摊失败次数"的路子。
        锁定期间**密码对了也不放行**；登录成功即全部清零。
        """
        now = time.time()
        with self._lock:
            until = self._locked_until.get(ip, 0.0)
            if until > now:
                return until - now
            if until:
                # 刚出狱：窗口清零重新计数（strikes 保留，下次锁更久）
                self._locked_until.pop(ip, None)
                self._fails.pop(ip, None)

            hist = [t for t in self._fails.get(ip, []) if now - t < FAIL_WINDOW]
            self._fails[ip] = hist
            if len(hist) >= FAIL_LIMIT:
                strikes = self._strikes.get(ip, 0) + 1
                self._strikes[ip] = strikes
                lock = min(FAIL_WINDOW * (2 ** (strikes - 1)), LOCK_MAX)
                self._locked_until[ip] = now + lock
                log.warning("登录防爆破：%s 在 %.0f 秒内失败 %d 次，"
                            "锁定 %.0f 秒（第 %d 次触发）",
                            ip, FAIL_WINDOW, len(hist), lock, strikes)
                return lock

            g = [t for t in self._global_fails if now - t < FAIL_WINDOW]
            self._global_fails = g
            if len(g) >= GLOBAL_FAIL_LIMIT:
                log.warning("登录防爆破：全部来源在 %.0f 秒内累计失败 %d 次，"
                            "全局熔断一个窗口", FAIL_WINDOW, len(g))
                return FAIL_WINDOW    # 不透露具体解锁时刻，也不区分来源
        return 0.0

    def _note_fail(self, ip: str):
        now = time.time()
        with self._lock:
            self._fails.setdefault(ip, []).append(now)
            self._global_fails.append(now)

    def _clear_fails(self, ip: str):
        with self._lock:
            self._fails.pop(ip, None)
            self._locked_until.pop(ip, None)
            self._strikes.pop(ip, None)

    # ---------------- 路由 ----------------

    def handle(self, handler: "Handler", method: str, path: str, body: bytes):
        q = urllib.parse.urlparse(path)
        route = q.path.rstrip("/") or "/"
        ip = handler.client_address[0]

        # 静态页面不需要登录（登录表单本身就在里面）
        if method == "GET" and route in ("/", "/index.html"):
            return handler.send_bytes(200, PAGE.replace(
                "__APP_VERSION__", _version()).encode("utf-8"),
                "text/html; charset=utf-8", csp=True)

        if method == "GET" and route == "/api/version":
            return handler.send_json({"name": "ipv6sync", "version": _version()})

        if method == "POST" and route == "/api/login":
            return self._login(handler, body, ip)

        token = handler.cookie("ipv6sync_web")
        user = self._valid(token)

        if method == "POST" and route == "/api/logout":
            self._drop(token)
            handler.send_json({"ok": True}, extra_headers=[
                ("Set-Cookie", "ipv6sync_web=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax")])
            return

        if method == "GET" and route == "/api/session":
            return handler.send_json({"authed": bool(user), "user": user or ""})

        if not user:
            # 未登录：状态类接口温和地回 authed=false，动作类接口直接 401
            if route in ("/api/state",):
                return handler.send_json({"authed": False})
            return handler.send_json({"ok": False, "error": "未登录"}, 401)

        if method == "GET" and route == "/api/state":
            return handler.send_json({
                "authed": True, "user": user,
                "overview": self.node.overview(),
                "settings": self.node.settings_view(),
                "status": self.node.snapshot(),
            })

        # ---- 以下都是改状态的动作，要求同源 ----
        if method == "POST" and not handler.same_origin():
            return handler.send_json(
                {"ok": False, "error": "跨站请求被拒绝（Origin 与 Host 不一致）"}, 403)

        ctype = (handler.headers.get("Content-Type") or "").split(";")[0].strip()
        if method == "POST" and ctype not in ("application/json", "text/plain", ""):
            return handler.send_json(
                {"ok": False, "error": "只接受 application/json"}, 415)

        try:
            if method == "POST" and route == "/api/settings":
                patch = _json_body(body)
                res = self.node.save_settings(patch)
                res["ok"] = True
                return handler.send_json(res)
            if method == "POST" and route == "/api/sync":
                snap = self.node.sync_now()
                return handler.send_json({"ok": snap.get("ok", True), "state": snap})
            if method == "POST" and route == "/api/router/test":
                return handler.send_json(self.node.test_connection())
            if method == "GET" and route == "/api/router/whitelist":
                return handler.send_json(self.node.whitelist())
            if method == "GET" and route == "/api/router/hosts":
                return handler.send_json(self.node.hosts())
            if method == "POST" and route == "/api/router/firewall":
                data = _json_body(body)
                want = data.get("enable")
                if not isinstance(want, bool):
                    return handler.send_json(
                        {"ok": False, "error": "enable 必须是 true/false"}, 400)
                return handler.send_json(self.node.set_firewall(want))
            if method == "POST" and route == "/api/notify/test":
                return handler.send_json(self.node.test_notify())
        except st.SettingsError as e:
            return handler.send_json({"ok": False, "error": str(e)}, 400)
        except Exception as e:  # noqa: BLE001
            log.exception("控制台接口 %s 出错", route)
            return handler.send_json(
                {"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

        return handler.send_json({"ok": False, "error": "not found"}, 404)

    def _login(self, handler: "Handler", body: bytes, ip: str):
        wait = self._throttled(ip)
        if wait > 0:
            return handler.send_json(
                {"ok": False,
                 "error": f"登录失败次数过多，请 {int(wait) + 1} 秒后再试"
                          f"（反复触发会逐次延长锁定）"}, 429)
        try:
            data = _json_body(body)
        except ValueError as e:
            return handler.send_json({"ok": False, "error": str(e)}, 400)

        time.sleep(0.25)   # 恒定延时：拉平正确/错误口令的响应时间，也拖慢脚本化尝试
        if self._check_credentials(str(data.get("user", "")),
                                   str(data.get("password", ""))):
            self._clear_fails(ip)
            ttl = self.REMEMBER_TTL if data.get("remember") else self.session_ttl
            tok = self._new_token(self.user, ttl)
            log.info("控制台登录成功，来源 %s（会话 %s）",
                     ip, "7天" if data.get("remember") else f"{self.session_ttl // 3600}小时")
            return handler.send_json(
                {"ok": True, "user": self.user},
                extra_headers=[("Set-Cookie",
                                f"ipv6sync_web={tok}; Path=/; HttpOnly; SameSite=Lax; "
                                f"Max-Age={ttl}")])

        self._note_fail(ip)
        log.warning("控制台登录失败，来源 %s", ip)
        return handler.send_json({"ok": False, "error": "用户名或密码不对"}, 401)

    # ---------------- 启动 ----------------

    def start(self) -> http.server.ThreadingHTTPServer | None:
        if self.port <= 0:
            log.info("Web 控制台已关闭（PORT=0）")
            return None
        if not self._password:
            log.warning("未设置 ADMIN_PASSWORD —— Web 控制台不启动。"
                        "需要它就在 compose 的 environment 里设 "
                        "ADMIN_USERNAME 与 ADMIN_PASSWORD")
            return None

        ui = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ipv6sync-console/" + _version()

            def log_message(self, fmt, *args):
                log.debug("%s - %s", self.client_address[0], fmt % args)

            # ---- 小工具 ----
            def cookie(self, name: str) -> str | None:
                raw = self.headers.get("Cookie") or ""
                for part in raw.split(";"):
                    k, _, v = part.strip().partition("=")
                    if k == name:
                        return v
                return None

            def same_origin(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True          # 非浏览器客户端不带 Origin
                host = self.headers.get("Host") or ""
                try:
                    o = urllib.parse.urlparse(origin)
                except ValueError:
                    return False
                return (o.netloc or "").lower() == host.lower()

            def _headers_common(self, code, ctype, length, csp=False,
                                extra=()):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                if csp:
                    self.send_header("Content-Security-Policy", CSP)
                for k, v in extra:
                    self.send_header(k, v)
                self.end_headers()

            def send_bytes(self, code, payload: bytes, ctype: str,
                           csp: bool = False, extra=()):
                try:
                    self._headers_common(code, ctype, len(payload), csp, extra)
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError,
                        ConnectionAbortedError):
                    pass

            def send_json(self, obj, code: int = 200, extra_headers=()):
                body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
                self.send_bytes(code, body, "application/json; charset=utf-8",
                                extra=extra_headers)

            def _read_body(self) -> bytes:
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    n = 0
                if n <= 0:
                    return b""
                if n > 1 << 20:
                    return b""
                return self.rfile.read(n)

            def do_GET(self):
                ui.handle(self, "GET", self.path, b"")

            def do_POST(self):
                ui.handle(self, "POST", self.path, self._read_body())

            def do_HEAD(self):
                self.send_error(405)

        class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        try:
            srv = Server((self.host, self.port), Handler)
        except OSError as e:
            log.error("Web 控制台端口 %d 绑定失败：%s", self.port, e)
            return None
        self._server = srv
        t = threading.Thread(target=srv.serve_forever, name="webui", daemon=True)
        t.start()
        log.info("Web 控制台已监听 %s:%d —— 浏览器打开 http://<本机IP>:%d/",
                 self.host, self.port, self.port)
        return srv

    def stop(self):
        if self._server:
            try:
                self._server.shutdown()
            except Exception:  # noqa: BLE001
                pass


def _json_body(body: bytes) -> dict:
    if not body:
        return {}
    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError(f"请求体不是合法 JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError("请求体必须是 JSON 对象")
    return data


def _version() -> str:
    from . import __version__
    return __version__


def webui_from_env(node, **overrides) -> WebUI:
    """按环境变量装配控制台。"""
    def env(name, default=""):
        v = os.environ.get(name)
        return default if v in (None, "") else v

    def env_int(name, default):
        try:
            return int(env(name, str(default)))
        except ValueError:
            log.warning("%s 不是整数，回退到 %s", name, default)
            return default

    from .config import _env_secret  # 复用同一套「值或 _FILE」逻辑
    host = overrides.get("host") or env("WEB_HOST", "0.0.0.0")
    port = overrides.get("port") or env_int("PORT", 6600)
    user = overrides.get("user") or env("ADMIN_USERNAME", "admin")
    password = overrides.get("password")
    if password is None:
        password = _env_secret("ADMIN_PASSWORD")
    ttl = env_int("WEB_SESSION_TTL", 12 * 3600)
    return WebUI(node, host, port, user, password, ttl)
