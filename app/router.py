# -*- coding: utf-8 -*-
"""
华为路由器客户端（自包含，仅标准库）

协议要点（全部逆向自设备固件前端，详见项目 README）：
  * 登录 = SCRAM-SHA256 三步挑战/应答，密码只在本地参与运算
  * 请求 body = {"data": {...}, "csrf": {"csrf_param":.., "csrf_token":..}}
  * 增删改    = {"action": "create|update|delete", "data": {...}, "csrf": {...}}
  * **CSRF 令牌每次响应都轮换**，且令牌失效时设备会直接注销整个会话
    （表现为所有鉴权接口返回 404 空响应）—— 所以令牌一变必须立刻落盘。

依赖：仅 Python 标准库。
"""
from __future__ import annotations

import binascii
import hashlib
import hmac
import http.cookiejar
import json
import logging
import os
import re
import ssl
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("ipv6sync.router")

DEFAULT_BASE = "http://192.168.3.1"
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

# API 路由表子集（提取自 /js/main.js）
API = {
    "deviceinfo": "system/deviceinfo",
    "routerstatus": "system/routerstatus",
    "useraccount": "system/useraccount",
    "user_login_nonce": "system/user_login_nonce",
    "user_login_proof": "system/user_login_proof",
    "HostInfo": "system/HostInfo",
    "firewall": "ntwk/firewall",
    "ip6firewall_enable": "ntwk/ip6firewall_enable",
    "ip6firewall_trustlist": "ntwk/ip6firewall_trustlist",
    "ipv6_enable": "ntwk/ipv6_enable",
    "ipv6_lan": "ntwk/ipv6_lan",
    "ipv6_wan": "ntwk/ipv6_wan",
    "lan_host": "ntwk/lan_host",
}


# --------------------------------------------------------------------------
# SCRAMJS v1.0.1（Huawei）的 Python 复刻
# --------------------------------------------------------------------------

def forge_bytes(s: str) -> bytes:
    """forge.util.createBuffer(jsString)：每个 char 取低 8 位。"""
    return bytes((ord(c) & 0xFF) for c in s)


def hmac_sha2(key_hex: str, message: str) -> str:
    """SCRAMJS.HmacSHA2 —— 注意固件里 key / message 的位置与 RFC 5802 相反。"""
    return hmac.new(message.encode("utf-8"),
                    binascii.unhexlify(key_hex),
                    hashlib.sha256).hexdigest()


def salted_password(password: str, salt: bytes, iterations: int) -> str:
    return hashlib.pbkdf2_hmac("sha256", forge_bytes(password), salt,
                               int(iterations), 32).hex()


def client_proof(password: str, salt: bytes, iterations: int,
                 auth_message: str) -> str:
    sp = salted_password(password, salt, iterations)
    ck = hmac_sha2(sp, "Client Key")
    stored = hashlib.sha256(binascii.unhexlify(ck)).hexdigest()
    sig = hmac_sha2(stored, auth_message)
    a, b = binascii.unhexlify(ck), binascii.unhexlify(sig)
    return bytes(x ^ y for x, y in zip(a, b)).hex()


def server_proof(password: str, salt: bytes, iterations: int,
                 auth_message: str) -> str:
    sp = salted_password(password, salt, iterations)
    return hmac_sha2(hmac_sha2(sp, "Server Key"), auth_message)


def make_nonce(nbytes: int = 32) -> str:
    return os.urandom(nbytes).hex()


# --------------------------------------------------------------------------
# 异常
# --------------------------------------------------------------------------

class RouterError(Exception):
    pass


class NotLoggedIn(RouterError):
    pass


class SessionExpired(RouterError):
    pass


class AuthFailed(RouterError):
    """密码错误 / 账号锁定。注意设备 maxfailtimes=3，绝不可快速重试。"""


class NetworkError(RouterError):
    pass


# --------------------------------------------------------------------------
# 客户端
# --------------------------------------------------------------------------

class HuaweiRouter:
    def __init__(self, base: str = DEFAULT_BASE, username: str = "admin",
                 password: str | None = None, session_file: str | None = None,
                 timeout: float = 10.0):
        self.base = base.rstrip("/")
        self.username = username
        self.password = password
        self.timeout = timeout
        self.session_file = session_file

        self.cj = http.cookiejar.CookieJar()
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj),
            urllib.request.HTTPSHandler(context=ctx),
        )

        self.csrf_param = ""
        self.csrf_token = ""
        self.level = 0
        self.rsa_n = ""
        self.rsa_e = ""
        self.logged_in = False
        self.last_login_attempt = 0.0

    # ---------------- HTTP 底层 ----------------

    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "User-Agent": DEFAULT_UA,
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.base}/html/index.html",
        }
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, body: bytes | None = None,
                 headers: dict | None = None) -> tuple[int, bytes]:
        url = path if path.startswith("http") else f"{self.base}{path}"
        req = urllib.request.Request(url, data=body, method=method,
                                     headers=self._headers(headers))
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()
        except Exception as e:  # noqa: BLE001
            raise NetworkError(f"请求 {url} 失败: {type(e).__name__}: {e}") from e

    def _resolve(self, name_or_path: str) -> str:
        p = API.get(name_or_path, name_or_path)
        if not re.match(r"^(api/|/)", p):
            p = "api/" + p
        if not p.startswith("/"):
            p = "/" + p
        return p

    def get(self, name_or_path: str, params: dict | None = None):
        p = self._resolve(name_or_path)
        if params:
            p += ("&" if "?" in p else "?") + urllib.parse.urlencode(params)
        st, raw = self._request("GET", p)
        return self._decode(st, raw, p)

    def post(self, name_or_path: str, data: dict | None = None,
             wrapped: bool = True, action: str | None = None):
        p = self._resolve(name_or_path)
        csrf = {"csrf_param": self.csrf_param, "csrf_token": self.csrf_token}
        if action is not None:
            payload = {"action": action, "data": data or {}, "csrf": csrf}
        elif wrapped:
            payload = {"data": data or {}, "csrf": csrf}
        else:
            payload = dict(data or {})
            payload["csrf"] = csrf
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        st, raw = self._request("POST", p, body,
                                {"Content-Type": "application/json; charset=utf-8"})
        return self._decode(st, raw, p)

    def _decode(self, status: int, raw: bytes, path: str):
        txt = raw.decode("utf-8", "replace")
        if status == 404 and not txt:
            self._forget_session()
            raise NotLoggedIn(
                f"{path} 返回 404 空响应 —— 需要登录会话（设备用 404 隐藏受限路径）")
        try:
            obj = json.loads(txt)
        except Exception:  # noqa: BLE001
            raise RouterError(f"{path} 返回非 JSON ({status}): {txt[:300]}")

        if isinstance(obj, dict):
            marker = str(obj.get("csrf") or obj.get("errorCategory") or "")
            if "csrf" in marker.lower() and "err" in marker.lower():
                self._forget_session()
                raise SessionExpired(
                    f"CSRF 校验失败（{marker}），设备已注销该会话，需要重新登录")
            rotated = False
            if obj.get("csrf_param") and obj["csrf_param"] != self.csrf_param:
                self.csrf_param = obj["csrf_param"]
                rotated = True
            if obj.get("csrf_token") and obj["csrf_token"] != self.csrf_token:
                self.csrf_token = obj["csrf_token"]
                rotated = True
            if rotated:
                self._persist()
        return obj

    # ---------------- 会话持久化 ----------------

    def _persist(self):
        """CSRF 轮换后立刻落盘。漏掉这步会导致下次启动拿旧令牌 → 会话被注销。"""
        if not self.session_file or not self.logged_in:
            return
        try:
            data = {}
            if os.path.exists(self.session_file):
                try:
                    with open(self.session_file, encoding="utf-8") as f:
                        data = json.load(f)
                except Exception:  # noqa: BLE001
                    data = {}
            data.update({
                "base": self.base, "username": self.username,
                "csrf_param": self.csrf_param, "csrf_token": self.csrf_token,
                "level": self.level, "rsa_n": self.rsa_n, "rsa_e": self.rsa_e,
                "cookies": [{"name": c.name, "value": c.value,
                             "domain": c.domain, "path": c.path}
                            for c in self.cj],
                "logged_in": True,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
            # 原子写，避免掉电/被杀时留下半个文件
            d = os.path.dirname(os.path.abspath(self.session_file))
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".session-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
                os.chmod(tmp, 0o600)
                os.replace(tmp, self.session_file)
            except Exception:  # noqa: BLE001
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except Exception as e:  # noqa: BLE001
            log.warning("会话落盘失败（不影响本次运行）：%s", e)

    def load(self) -> bool:
        if not self.session_file or not os.path.exists(self.session_file):
            return False
        try:
            with open(self.session_file, encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:  # noqa: BLE001
            log.warning("会话文件损坏，忽略：%s", e)
            return False
        if data.get("base") and data["base"] != self.base:
            return False
        for c in data.get("cookies", []):
            try:
                self.cj.set_cookie(http.cookiejar.Cookie(
                    version=0, name=c["name"], value=c["value"],
                    port=None, port_specified=False,
                    domain=c.get("domain") or urllib.parse.urlparse(self.base).hostname,
                    domain_specified=True, domain_initial_dot=False,
                    path=c.get("path", "/"), path_specified=True,
                    secure=False, expires=None, discard=False,
                    comment=None, comment_url=None, rest={}, rfc2109=False))
            except Exception:  # noqa: BLE001
                pass
        self.csrf_param = data.get("csrf_param", "")
        self.csrf_token = data.get("csrf_token", "")
        self.username = data.get("username", self.username)
        self.level = data.get("level", 0)
        self.rsa_n = data.get("rsa_n", "")
        self.rsa_e = data.get("rsa_e", "")
        self.logged_in = bool(data.get("logged_in"))
        return self.logged_in

    def _forget_session(self):
        self.logged_in = False
        if self.session_file and os.path.exists(self.session_file):
            try:
                os.remove(self.session_file)
            except OSError:
                pass

    # ---------------- 登录 ----------------

    def fetch_index(self) -> dict:
        st, raw = self._request("GET", "/html/index.html")
        html = raw.decode("utf-8", "replace")
        meta = dict(re.findall(r'<meta\s+name="([^"]+)"\s+content="([^"]*)"', html))
        self.csrf_param = meta.get("csrf_param", self.csrf_param)
        self.csrf_token = meta.get("csrf_token", self.csrf_token)
        self.rsa_n = meta.get("n", self.rsa_n)
        self.rsa_e = meta.get("e", self.rsa_e)
        if not self.csrf_token:
            raise RouterError(
                "无法从 /html/index.html 取到 csrf_token —— 路由器固件版本可能不同")
        return {"csrf_param": self.csrf_param, "csrf_token": self.csrf_token}

    def login(self, password: str | None = None) -> dict:
        pwd = password if password is not None else self.password
        if not pwd:
            raise AuthFailed("未配置路由器密码")

        self.fetch_index()
        first_nonce = make_nonce()
        self.last_login_attempt = time.time()

        r1 = self.post("user_login_nonce",
                       {"username": self.username, "firstnonce": first_nonce})
        if r1.get("err") != 0:
            code = r1.get("err")
            # 2640003 = 用户不存在；4784230 = 密码错误
            raise AuthFailed(f"登录第 1 步被拒（err={code}）: "
                             f"{json.dumps(r1, ensure_ascii=False)[:200]}")

        salt = binascii.unhexlify(r1["salt"])
        iterations = int(r1["iterations"])
        server_nonce = r1["servernonce"]
        auth_msg = f"{first_nonce},{server_nonce},{server_nonce}"

        proof = client_proof(pwd, salt, iterations, auth_msg)
        expected_sig = server_proof(pwd, salt, iterations, auth_msg)

        r2 = self.post("user_login_proof",
                       {"clientproof": proof, "finalnonce": server_nonce})
        if r2.get("err") != 0:
            left = r2.get("maxfailtimes")
            raise AuthFailed(
                f"登录失败（err={r2.get('err')}, 类别={r2.get('errorCategory')}, "
                f"本次失败计数={r2.get('count')}/{left}）—— 密码错误，"
                f"再错会锁定账号，本程序不会自动重试")

        # 校验服务端签名（防中间人 / 防连到别的设备）
        sig = r2.get("serversignature") or r2.get("serverproof")
        if not sig:
            raise RouterError("响应里没有服务端签名，无法验证对端身份，已中止")
        if sig != expected_sig:
            raise RouterError("服务端签名不匹配 —— 可能不是同一台设备或存在中间人，已中止")

        self.level = int(r2.get("level", 0) or 0)
        self.rsa_n = r2.get("rsan", self.rsa_n)
        self.rsa_e = r2.get("rsae", self.rsa_e)
        self.logged_in = True
        self._persist()
        return {"username": self.username, "level": self.level,
                "server_signature_verified": True}

    def check_session(self) -> bool:
        try:
            self.get("ip6firewall_enable")
            return True
        except (NotLoggedIn, SessionExpired):
            return False
        except RouterError:
            return False

    def ensure_logged_in(self) -> dict:
        """优先复用会话；失效才重新登录。"""
        if self.logged_in and self.check_session():
            return {"reused": True, "username": self.username, "level": self.level}
        self.login()
        return {"reused": False, "username": self.username, "level": self.level}
