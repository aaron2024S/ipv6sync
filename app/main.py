# -*- coding: utf-8 -*-
"""入口：常驻轮询 / 单次执行 / 设备列表（用于查 MAC） / 健康检查服务"""
from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import __version__
from .config import HELP, load_config
from .node import Node
from .router import RouterError
from .webui import webui_from_env

log = logging.getLogger("ipv6sync")

_STOP = threading.Event()


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------

class _JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "msg": record.getMessage(),
        }, ensure_ascii=False)


def setup_logging(level: str, as_json: bool = False):
    root = logging.getLogger()
    root.handlers.clear()
    h = logging.StreamHandler(sys.stdout)
    if as_json:
        h.setFormatter(_JsonFormatter())
    else:
        h.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(h)
    root.setLevel(getattr(logging, level, logging.INFO))
    logging.getLogger("urllib3").setLevel(logging.WARNING)


# --------------------------------------------------------------------------
# 健康检查 / 状态接口
# --------------------------------------------------------------------------

def start_health_server(node, port: int) -> ThreadingHTTPServer | None:
    if port <= 0:
        return None

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # 静音
            pass

        def _send(self, code: int, obj):
            body = json.dumps(obj, ensure_ascii=False, indent=1).encode("utf-8")
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            path = self.path.split("?")[0]
            snap = node.snapshot()
            if path in ("/healthz", "/health", "/"):
                fresh = bool(snap["last_tick_at"])
                # 未配置密码 = 首次部署待机状态，不是故障（配置好就转 ok）
                unconfigured = snap.get("unconfigured")
                healthy = fresh and (snap["consecutive_failures"] == 0
                                     or unconfigured)
                self._send(200 if healthy else 503, {
                    "status": ("unconfigured" if unconfigured
                               else "ok" if healthy else "degraded"),
                    "last_tick_at": snap["last_tick_at"],
                    "consecutive_failures": snap["consecutive_failures"],
                    "last_error": snap["last_error"],
                })
            elif path == "/state":
                self._send(200, snap)
            else:
                self._send(404, {"error": "not found"})

    try:
        srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        log.error("健康检查端口 %d 绑定失败：%s", port, e)
        return None
    t = threading.Thread(target=srv.serve_forever, name="health", daemon=True)
    t.start()
    log.info("健康检查已监听 :%d  ——  /healthz  /state", port)
    return srv


# --------------------------------------------------------------------------
# 设备列表（帮用户找 TARGET_MAC）
# --------------------------------------------------------------------------

def list_hosts(node: Node):
    res = node.hosts()
    if not res.get("ok"):
        print("\n读取设备列表失败：", res.get("error"))
        return
    rows = [(h["mac"], h["name"][:34], h["ipv4"],
             ", ".join(h["ipv6"]) or "（无全局 IPv6）") for h in res["hosts"]]
    print("\n%-19s %-36s %-15s %s" % ("MAC（配 TARGET_MAC 用）", "主机名", "IPv4",
                                      "当前 IPv6"))
    print("-" * 130)
    for mac, name, ip, v6 in rows:
        print("%-19s %-36s %-15s %s" % (mac, name, ip, v6))
    print(f"\n共 {len(rows)} 台在线设备")
    print("当前路由器下发的 LAN 前缀：",
          ", ".join(res.get("lan_prefixes") or []) or "(无)")


def show_whitelist(node: Node):
    res = node.whitelist()
    if not res.get("ok"):
        print("\n读取白名单失败：", res.get("error"))
        return
    print("\nIPv6 防火墙开关:", "开启" if res["enabled"] else "关闭")
    entries = res["entries"]
    print(f"白名单条目 {len(entries)}/{res['max']}")
    print("\n%-22s %-44s %-10s %-7s %s" % ("名称", "本地 IPv6", "来源", "端口", "ID"))
    print("-" * 120)
    for e in entries:
        print("%-22s %-44s %-10s %-7s %s" % (
            str(e.get("Name"))[:21], str(e.get("LocalIp")),
            str(e.get("RemoteIp")), str(e.get("Port")), e.get("ID")))


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------

def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)

    if "-h" in argv or "--help" in argv:
        print(HELP)
        return 0

    if "--list-hosts" in argv or "--show" in argv or "--whitelist" in argv:
        cfg = load_config([a for a in argv if a not in
                           ("--list-hosts", "--show", "--whitelist")])
        setup_logging(cfg.log_level)
        node = Node(cfg)
        if not node.syncer.ensure_login():
            print(f"\n连接路由器失败：{node.syncer.last_error}")
            return 3
        if "--list-hosts" in argv:
            list_hosts(node)
        if "--whitelist" in argv:
            show_whitelist(node)
        return 0

    cfg = load_config(argv)
    setup_logging(cfg.log_level, cfg.log_json)

    log.info("=" * 66)
    log.info("华为路由器 IPv6 白名单同步 v%s", __version__)
    log.info("路由器 %s  用户 %s  密码 %s", cfg.base_url, cfg.username,
             f"来自 {cfg.password_source}" if cfg.password_source
             else "（未配置，只复用会话）")
    if not cfg.password:
        log.warning("还没配置路由器密码 —— 本程序会先待机。"
                    "浏览器打开控制台，在「路由器连接」里填好密码即自动开始同步"
                    "（写入 %s，无需重建容器）", cfg.settings_file)
    log.info("发现方式 读本机网卡（host 网络）  TARGET_MAC=%s", cfg.mac or "-")
    for i, r in enumerate(cfg.rules, 1):
        log.info("规则 %d: 名称=%s 端口=%s 来源=%s MAC=%s", i, r.name,
                 r.port if r.port not in (None, -1) else "全部",
                 r.remote_ip, r.mac or "-")
    log.info("轮询间隔 %ds  自动打开防火墙 %s  演练模式 %s  单次 %s",
             cfg.poll_interval, cfg.ensure_firewall_on, cfg.dry_run, cfg.once)
    log.info("设置文件 %s（网页里改的会盖过环境变量）", cfg.settings_file)
    log.info("=" * 66)

    node = Node(cfg)
    syncer = node.syncer

    srv = None
    webui = None
    if not cfg.once:
        srv = start_health_server(node, cfg.health_port)
        webui = webui_from_env(node)
        webui.start()
        if not webui.enabled():
            log.info("提示：在 compose 的 environment 里设 ADMIN_USERNAME / "
                     "ADMIN_PASSWORD 就能打开浏览器控制台"
                     "（在网页里改设置，不必重建容器）")

    def _stop(signum, _frame):
        log.info("收到信号 %s，准备退出", signum)
        _STOP.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _stop)
        except (ValueError, OSError):
            pass

    exit_code = 0
    try:
        while not _STOP.is_set():
            started = time.time()
            snap = syncer.tick()
            if cfg.once:
                print(json.dumps(snap, ensure_ascii=False, indent=1))
                summary = {k: v["action"] for k, v in snap["rules"].items()}
                failed = snap["consecutive_failures"] > 0 or any(
                    v["error"] for v in snap["rules"].values())
                exit_code = 1 if failed else 0
                log.info("单次执行完成：%s", summary)
                break

            # 睡眠期间也要能被打断；周期以**当前**配置为准（网页可能刚改过）
            interval = syncer.cfg.poll_interval
            elapsed = time.time() - started
            wait = max(1.0, interval - elapsed)
            while wait > 0 and not _STOP.is_set():
                step = min(1.0, wait)
                time.sleep(step)
                wait -= step
    finally:
        if webui:
            webui.stop()
        if srv:
            srv.shutdown()
        log.info("退出。累计写入 %d 次", syncer.total_writes)
    return exit_code


def cli():
    try:
        code = main()
    except SystemExit as e:
        if isinstance(e.code, str):
            print(e.code, file=sys.stderr)
            code = 2
        else:
            code = int(e.code or 0)
    except RouterError as e:
        log.error("路由器错误：%s", e)
        code = 3
    except KeyboardInterrupt:
        code = 0
    sys.exit(code or 0)


if __name__ == "__main__":
    cli()
