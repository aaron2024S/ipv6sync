#!/usr/bin/env python3
"""行为校验：把镜像里的应用层解出来，真跑一遍入口链路。

静态校验只能证明"字节长对了"，证明不了"容器起来能跑"。这一步在临时目录里
复现镜像内的目录结构与环境变量，用**宿主 python 当桩**替换镜像里的 Linux 解释器，
验证：

  T0  层内所有 .py 都能编译（防截断/防编码坏）
  T1  config.Env 含全部应用变量，值正确，且无重复键
  T2  WorkingDir=/app、User、Entrypoint、Cmd、Volumes 与 Dockerfile 语义一致
  T3  **工作目录真的被用上**：cwd=/app 时 `-u -m app.main` 能跑；换个 cwd 就 import 失败
       （这条是这类手工镜像最典型的静默故障：cwd 不对 -> 相对入口解析失败 -> 无限重启）
  T4  真被 import 的 app 包来自镜像层，而不是仓库里的源码副本
  T5  镜像里的那份代码跑通仓库的离线单测（打包进去的确实是能用的代码）
  T6  缺 ROUTER_PASSWORD 时是干净的配置错误（退出码 2、无 traceback，不是崩栈）
  T7  HEALTHCHECK 的命令行真的可用：对 200 的健康端点返回 0，对死端口返回非 0；
        并确认 -c 参数的引号能安全穿过 shell
  T8  声明的卷 /data 就是 SESSION_FILE 与 SETTINGS_FILE 的父目录（否则无处可写）
  T9  真起一次 Web 控制台：未登录拿不到设置、动作接口 401、登录后能读状态、
        保存设置热生效并落盘、非法值 400、退出后令牌失效
  T10 纯网页首次部署：不给 ADMIN_PASSWORD / 路由器密码时容器常驻待机
        （提示去网页填密码），而不是退出——这是"设置全进网页"的前提

用法：
  python validate_boot.py --arch amd64
"""
import argparse
import hashlib
import http.server
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
DIST_DIR = os.path.join(HERE, "dist")
IMAGE = "ipv6sync"


def _read_version(fallback: str = "1.0.6") -> str:
    """版本号唯一来源是 app/__init__.py，避免发版时多处硬编码漏改。"""
    try:
        with open(os.path.join(PROJECT, "app", "__init__.py"),
                  encoding="utf-8") as f:
            txt = f.read()
    except OSError:
        return fallback
    m = re.search(r'__version__\s*=\s*"([^"]+)"', txt)
    return m.group(1) if m else fallback


VERSION = _read_version()

EXPECT_ENV = {
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "PYTHONFAULTHANDLER": "1",
    "PYTHONIOENCODING": "utf-8",
    "LANG": "C.UTF-8",
    "TZ": "Asia/Shanghai",
    "SESSION_FILE": "/data/session.json",
    "SETTINGS_FILE": "/data/settings.json",
    "HEALTH_PORT": "8099",
    "PORT": "6600",
}

fails, warns = [], []


def fail(m):
    fails.append(m)
    print(f"  [FAIL] {m}")


def warn(m):
    warns.append(m)
    print(f"  [WARN] {m}")


def ok(m):
    print(f"  [ok]   {m}")


def run(cmd, cwd, env=None, timeout=180):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run(cmd, cwd=cwd, env=e, capture_output=True,
                       text=True, encoding="utf-8", errors="replace",
                       timeout=timeout)
    return p.returncode, p.stdout or "", p.stderr or ""


def norm(n):
    n = n[2:] if n.startswith("./") else n
    n = n.lstrip("/")
    while n.endswith("/"):
        n = n[:-1]
    return n


def extract_app_layer(tar_path, dest):
    """只解出最后那个应用层（基础层里有符号链接，Windows 上解不了，也不需要）。"""
    with tarfile.open(tar_path, "r:") as t:
        manifest = json.load(t.extractfile("manifest.json"))
        m0 = manifest[0]
        cfg = json.loads(t.extractfile(m0["Config"]).read())
        n_base = len(cfg["rootfs"]["diff_ids"]) - 1
        layer_names = m0["Layers"]
        if len(layer_names) != len(cfg["rootfs"]["diff_ids"]):
            raise SystemExit("manifest 层数与 diff_ids 不一致")
        blob = t.extractfile(layer_names[-1]).read()
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:") as lt:
        members = lt.getmembers()
        links = [m.name for m in members if m.issym() or m.islnk()]
        if links:
            raise SystemExit(f"应用层里有意料之外的链接，无法安全解出: {links}")
        try:
            # filter="data" 是 Python 3.12+ 的默认行为，显式写出来免得 3.14 告警
            lt.extractall(dest, filter="data")
        except TypeError:      # 老 python 没有 filter 参数
            lt.extractall(dest)
    return cfg, n_base, [norm(m.name) for m in members]


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class StubHealth(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"status":"ok"}'
        code = 200 if self.path.startswith("/healthz") else 404
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def t9_web_console(root, wd, stub, want_port: int):
    """真起一次控制台（用镜像里的代码），验证登录门禁与设置热生效。

    ROUTER_HOST 故意指向 127.0.0.1（本机没开 80 端口）—— 绝不会去碰真路由器，
    更不会试密码（设备只允许连错 3 次）。
    """
    import urllib.error
    import urllib.request

    web_port = free_port()
    health_port = free_port()
    pw = "console-pw-" + str(web_port)
    env = dict(EXPECT_ENV)
    env.update({
        "ROUTER_HOST": "127.0.0.1", "ROUTER_USER": "admin",
        "ROUTER_PASSWORD": "not-used", "ROUTER_TIMEOUT": "1",
        "SESSION_FILE": os.path.join(root, "data", "session.json"),
        "SETTINGS_FILE": os.path.join(root, "data", "settings.json"),
        "ADMIN_USERNAME": "admin", "ADMIN_PASSWORD": pw,
        "WEB_HOST": "127.0.0.1", "PORT": str(web_port),
        "HEALTH_PORT": str(health_port), "POLL_INTERVAL": "600",
    })
    for k in ("RULES", "RULES_JSON", "RULES_FILE", "DRY_RUN", "ONCE"):
        env.pop(k, None)

    if want_port != 6600:
        fail(f"T9 config.Env 的 PORT={want_port}，期望 6600")
        return

    log = os.path.join(root, "webui.log")
    base = f"http://127.0.0.1:{web_port}"
    proc = None
    try:
        with open(log, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(stub, cwd=wd, env={**os.environ, **env},
                                    stdout=lf, stderr=subprocess.STDOUT)
        deadline = time.time() + 25
        up = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            try:
                with urllib.request.urlopen(base + "/api/session", timeout=3) as r:
                    if r.status == 200:
                        up = True
                        break
            except Exception:  # noqa: BLE001
                time.sleep(0.5)
        if not up:
            fail(f"T9 控制台没起来（见 {log}）")
            return

        def call(path, method="GET", body=None, cookie=None):
            data = json.dumps(body).encode() if body is not None else None
            hdr = {"Content-Type": "application/json"} if data else {}
            if cookie:
                hdr["Cookie"] = cookie
            rq = urllib.request.Request(base + path, data=data, headers=hdr,
                                        method=method)
            try:
                with urllib.request.urlopen(rq, timeout=15) as r:
                    return r.status, r.read().decode("utf-8"), r.headers
            except urllib.error.HTTPError as e:
                try:
                    return e.code, e.read().decode("utf-8"), e.headers
                finally:
                    e.close()

        # 页面
        with urllib.request.urlopen(base + "/", timeout=10) as r:
            page = r.read().decode("utf-8", "replace")
        if 'type="password"' not in page or "__APP_VERSION__" in page:
            fail("T9 控制台页面异常（缺登录表单或版本未注入）")
        else:
            ok("T9 控制台页面正常（含登录表单、版本已注入）")

        st, body, _ = call("/api/state")
        if st != 200 or json.loads(body).get("authed") is not False:
            fail(f"T9 未登录时 /api/state 应回 authed=false，实际 {st} {body[:120]}")
        else:
            ok("T9 未登录时拿不到设置（authed=false）")

        st, body, _ = call("/api/sync", "POST", {})
        if st != 401:
            fail(f"T9 未登录时动作接口应 401，实际 {st}")
        else:
            ok("T9 未登录时动作接口 401")

        st, body, hdr = call("/api/login", "POST",
                             {"user": "admin", "password": pw})
        cookie = (hdr.get("Set-Cookie") or "").split(";")[0]
        if st != 200 or not cookie or "HttpOnly" not in (hdr.get("Set-Cookie") or ""):
            fail(f"T9 登录失败 st={st} cookie={cookie!r}")
            return
        ok("T9 控制台登录成功，Cookie 带 HttpOnly")

        st, body, _ = call("/api/state", cookie=cookie)
        j = json.loads(body)
        nf = sum(len(g["fields"]) for g in j["settings"])
        if not j.get("authed") or nf < 15:
            fail(f"T9 /api/state 异常：authed={j.get('authed')} 字段数={nf}")
        else:
            ok(f"T9 /api/state 返回 {len(j['settings'])} 组 / {nf} 个字段")
        if pw in body:
            fail("T9 响应里出现了控制台口令")
        else:
            ok("T9 响应里没有明文口令")

        st, body, _ = call("/api/settings", "POST",
                           {"POLL_INTERVAL": "300", "ENTRY_NAME": "VAL9"},
                           cookie=cookie)
        if st != 200 or not json.loads(body).get("ok"):
            fail(f"T9 保存设置失败 {st} {body[:160]}")
        else:
            rules = json.loads(body)["overview"]["rules"]
            if not rules or rules[0]["name"] != "VAL9":
                fail(f"T9 设置没热生效：{rules}")
            else:
                ok("T9 保存设置并热生效（条目名已变为 VAL9）")
        sf = os.path.join(root, "data", "settings.json")
        if not os.path.exists(sf):
            fail("T9 设置没落盘到 SETTINGS_FILE")
        else:
            ok("T9 设置已落盘 settings.json")

        st, body, _ = call("/api/settings", "POST", {"POLL_INTERVAL": "1"},
                           cookie=cookie)
        if st != 400:
            fail(f"T9 非法值应 400，实际 {st}")
        else:
            ok("T9 非法值被拒（400）")

        st, body, _ = call("/api/logout", "POST", {}, cookie=cookie)
        st2, body2, _ = call("/api/state", cookie=cookie)
        if json.loads(body2).get("authed"):
            fail("T9 退出登录后令牌仍然有效")
        else:
            ok("T9 退出登录后令牌失效")
    finally:
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def validate(arch):
    print(f"\n{'='*66}\n==== 行为校验 {IMAGE}:latest  linux/{arch}\n{'='*66}")
    tar_path = os.path.join(DIST_DIR, f"{IMAGE}-{VERSION}-{arch}-image.tar")
    if not os.path.exists(tar_path):
        fail(f"产物不存在: {tar_path}")
        return

    root = os.path.join(HERE, f"boot-run-{arch}-{time.strftime('%H%M%S')}")
    if os.path.exists(root):
        raise SystemExit("临时目录已存在，换个名字重跑")
    os.makedirs(root)
    print(f"复现目录: {root}")

    cfg, n_base, names = extract_app_layer(tar_path, root)
    print(f"  应用层 {len(names)} 个条目（基础 {n_base} 层不解出）")
    ok(f"应用层无符号链接/硬链接，可安全解出: {', '.join(sorted(names))}")

    wd_rel = cfg["config"]["WorkingDir"].strip("/")     # "app" = /app
    wd = os.path.join(root, wd_rel)
    if not os.path.isdir(wd):
        fail(f"复现出的工作目录 {wd} 不存在")
        return

    # ---------- T0 编译
    rc, out, err = run([sys.executable, "-m", "compileall", "-q", "."], wd)
    if rc != 0:
        fail(f"T0 层内 .py 编译失败: {err.strip()[:300]}")
    else:
        ok("T0 层内 .py 全部编译通过")

    # ---------- T1 env
    env_list = cfg["config"].get("Env") or []
    kv = {}
    dup = [e.split("=", 1)[0] for e in env_list
           if env_list.count(e) > 1 or e.split("=", 1)[0] in kv]
    for e in env_list:
        k, _, v = e.partition("=")
        kv[k] = v
    bad = {k: (kv.get(k), v) for k, v in EXPECT_ENV.items() if kv.get(k) != v}
    if bad:
        fail(f"T1 config.Env 不符: {bad}")
    else:
        ok(f"T1 config.Env 含全部 {len(EXPECT_ENV)} 个应用变量，值正确")
    if dup:
        fail(f"T1 config.Env 有重复键: {sorted(set(dup))}")
    else:
        ok(f"T1 config.Env 无重复键（共 {len(env_list)} 项，"
           f"PATH 等基础变量保留）")
    if "PATH" not in kv:
        fail("T1 config.Env 丢了基础镜像的 PATH")
    else:
        ok("T1 PATH 已保留")

    # ---------- T2 入口/工作目录/卷
    c = cfg["config"]
    checks = [
        ("WorkingDir", c.get("WorkingDir"), "/app"),
        ("User", c.get("User"), "syncapp"),
        ("Entrypoint", c.get("Entrypoint"), ["python", "-u", "-m", "app.main"]),
        ("Cmd", c.get("Cmd"), None),
    ]
    for name, got, want in checks:
        if got != want:
            fail(f"T2 config.{name} = {got!r}，期望 {want!r}")
        else:
            ok(f"T2 config.{name} = {got!r}")
    if "/data" not in (c.get("Volumes") or {}):
        fail("T2 缺 /data 卷")
    else:
        ok("T2 config.Volumes = {'/data'}")

    # ---------- T3 工作目录真的被用上
    app_entry = c["Entrypoint"][1:]          # ["-u","-m","app.main"]
    # 宿主 python 当桩，替换镜像里的 Linux 解释器
    stub = [sys.executable] + app_entry
    rc, out, err = run(stub + ["--help"], wd)
    if rc != 0:
        fail(f"T3 在 WorkingDir 下 `-m app.main --help` 失败 rc={rc}: "
             f"{(err or out).strip()[:300]}")
    elif "华为路由器" not in out:
        fail(f"T3 --help 输出不含预期文案: {out[:200]}")
    else:
        ok("T3 cwd=WorkingDir 时 `-u -m app.main --help` 正常（rc=0）")

    rc2, out2, err2 = run(stub + ["--help"], root)
    if rc2 == 0:
        fail("T3 换个 cwd 也成功了 —— 说明入口不依赖 WorkingDir，"
             "那 config.WorkingDir 就是摆设，必须查清")
    elif "No module named" not in (err2 + out2):
        warn(f"T3 换 cwd 后以 rc={rc2} 失败，但原因不是模块解析: "
             f"{(err2 or out2).strip()[:200]}")
    else:
        ok(f"T3 换 cwd 后按预期失败（rc={rc2}, No module named app）"
           f" —— WorkingDir=/app 是承重的")

    # ---------- T4 被 import 的 app 来自镜像层
    probe = ("import app,os,sys;"
             "print(os.path.abspath(app.__file__));"
             "print(os.getcwd())")
    rc, out, err = run([sys.executable, "-c", probe], wd)
    lines = [x.strip() for x in out.splitlines() if x.strip()]
    if rc != 0 or len(lines) < 2:
        fail(f"T4 import 探针失败 rc={rc}: {(err or out).strip()[:300]}")
    else:
        pkg_path, cwd_seen = lines[0], lines[1]
        if not pkg_path.startswith(root):
            fail(f"T4 被 import 的 app 不是镜像层里的那份: {pkg_path}")
        else:
            ok(f"T4 导入源 = 镜像层内 {os.path.relpath(pkg_path, root)}")
        if os.path.normcase(os.path.abspath(cwd_seen)) != os.path.normcase(wd):
            fail(f"T4 子进程 cwd 不是 WorkingDir: {cwd_seen} != {wd}")
        else:
            ok("T4 子进程 cwd 确实等于 WorkingDir")

    # ---------- T5 用镜像里的代码跑仓库单测
    tests_src = os.path.join(PROJECT, "tests")
    if not os.path.isdir(tests_src):
        warn("T5 找不到仓库 tests/，跳过")
    else:
        shutil.copytree(tests_src, os.path.join(wd, "tests"), dirs_exist_ok=True)
        env = {k: v for k, v in EXPECT_ENV.items()}
        env["SESSION_FILE"] = os.path.join(root, "data", "session.json")
        env["PYTHONPATH"] = ""
        rc, out, err = run([sys.executable, "-m", "unittest", "discover",
                            "-s", "tests", "-t", ".", "-v"], wd, env=env)
        m = re.search(r"Ran (\d+) tests?", err + out)
        n = int(m.group(1)) if m else 0
        if rc != 0:
            fail(f"T5 镜像内代码跑单测失败 rc={rc}: {(err or out).strip()[-500:]}")
        elif n < 20:
            fail(f"T5 只跑了 {n} 个测试，疑似没收集全")
        else:
            ok(f"T5 镜像内的 app 包跑通仓库单测：{n} 个用例全部通过")

    # ---------- T6 缺密码时的行为
    env6 = {k: v for k, v in EXPECT_ENV.items()}
    env6["SESSION_FILE"] = os.path.join(root, "data", "session.json")
    env6["ROUTER_PASSWORD"] = ""
    rc, out, err = run(stub + ["--once"], wd, env=env6, timeout=60)
    joined = out + err
    if "Traceback" in joined:
        fail(f"T6 缺密码时抛了 traceback（应干净报错）: {joined.strip()[:300]}")
    elif rc != 2:
        fail(f"T6 缺密码时 rc={rc}，期望 2")
    elif "ROUTER_PASSWORD" not in joined:
        fail(f"T6 报错信息没提到 ROUTER_PASSWORD: {joined.strip()[:200]}")
    else:
        ok(f"T6 缺密码 -> 干净的配置错误（rc={rc}，无 traceback）")

    # ---------- T7 healthcheck 命令行
    hc = (c.get("Healthcheck") or {}).get("Test")
    if not hc or hc[0] != "CMD-SHELL":
        fail(f"T7 Healthcheck.Test 形状不对: {hc}")
    else:
        cmd = hc[1]
        m = re.match(r'^python -c "(.*)"$', cmd, re.S)
        if not m:
            fail(f"T7 healthcheck 命令不是预期的 `python -c \"...\"` 形式: {cmd[:120]}")
        else:
            body = m.group(1)
            if '"' in body:
                fail("T7 healthcheck 的 -c 参数里含双引号，shell 下会被截断")
            else:
                ok("T7 healthcheck 的 -c 参数无双引号，可安全穿过 shell")
            # 对 200 端点应成功
            srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), StubHealth)
            port = srv.server_address[1]
            th = threading.Thread(target=srv.serve_forever, daemon=True)
            th.start()
            try:
                rc, out, err = run([sys.executable, "-c", body], wd,
                                   env={"HEALTH_PORT": str(port)}, timeout=30)
                if rc != 0:
                    fail(f"T7 健康检查对 200 端点返回 rc={rc}: {(err or out)[:200]}")
                else:
                    ok(f"T7 健康检查对 200 的 :{port}/healthz 返回 0")
            finally:
                srv.shutdown()
                srv.server_close()
            dead = free_port()
            rc, out, err = run([sys.executable, "-c", body], wd,
                               env={"HEALTH_PORT": str(dead)}, timeout=30)
            if rc == 0:
                fail("T7 健康检查对死端口也返回 0 —— 检查等于没做")
            else:
                ok(f"T7 健康检查对死端口 :{dead} 返回非 0（rc={rc}）")
    if hc:
        hh = c["Healthcheck"]
        vals = (hh.get("Interval"), hh.get("Timeout"),
                hh.get("StartPeriod"), hh.get("Retries"))
        if vals != (60000000000, 6000000000, 25000000000, 3):
            fail(f"T7 Healthcheck 时间参数不符: {vals}")
        else:
            ok("T7 Healthcheck 参数 interval=60s timeout=6s "
               "start-period=25s retries=3")

    # ---------- T8 卷与会话/设置文件一致
    for key in ("SESSION_FILE", "SETTINGS_FILE"):
        p = kv.get(key, "")
        if os.path.dirname(p) != "/data" or "/data" not in (c.get("Volumes") or {}):
            fail(f"T8 {key}={p} 与声明的卷 {list((c.get('Volumes') or {}))} 不匹配")
        else:
            ok(f"T8 {key}={p} 落在声明的卷 /data 内")

    # ---------- T9 控制台：真起一次服务，验证登录与改设置
    wp = kv.get("PORT", "")
    if not wp.isdigit():
        fail(f"T9 PORT={wp!r} 不是数字")
    else:
        t9_web_console(root, wd, stub, int(wp))

    # ---------- T10 纯网页首次部署：零凭据也要能常驻待机 ----------
    env10 = {k: v for k, v in EXPECT_ENV.items()}
    env10["SESSION_FILE"] = os.path.join(root, "data", "session.json")
    env10["ROUTER_PASSWORD"] = ""
    env10.pop("ADMIN_PASSWORD", None)
    env10.pop("ADMIN_USERNAME", None)
    log10 = os.path.join(root, "standby.log")
    proc10 = None
    lf10 = open(log10, "w", encoding="utf-8")
    try:
        proc10 = subprocess.Popen(stub, cwd=wd, env={**os.environ, **env10},
                                  stdout=lf10, stderr=subprocess.STDOUT)
        time.sleep(8)
        if proc10.poll() is not None:
            fail(f"T10 零凭据常驻模式退出了（rc={proc10.returncode}）—— "
                 f"纯网页首次配置走不通，见 {log10}")
        else:
            lf10.flush()
            with open(log10, encoding="utf-8") as f:
                txt = f.read()
            if "还没配置路由器密码" in txt:
                ok("T10 零凭据时容器常驻待机，日志提示去网页填密码")
            else:
                warn(f"T10 进程活着但日志里没看到待机提示（见 {log10}）: "
                     f"{txt.strip()[-200:]}")
    finally:
        lf10.close()
        if proc10 and proc10.poll() is None:
            proc10.terminate()
            try:
                proc10.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc10.kill()

    print(f"\n复现目录保留在 {root}（便于人工复核）")
    return root


def main():
    ap = argparse.ArgumentParser(description="行为校验离线镜像")
    ap.add_argument("--arch", default="amd64")
    args = ap.parse_args()
    validate(args.arch)
    print(f"\n{'='*66}")
    if fails:
        print(f"==== 行为校验失败：{len(fails)} 项 FAIL / {len(warns)} 项 WARN ====")
        for f in fails:
            print(f"  - {f}")
        sys.exit(1)
    print(f"==== 行为校验全部通过（{len(warns)} 项 WARN）====")
    for w in warns:
        print(f"  ! {w}")


if __name__ == "__main__":
    main()
