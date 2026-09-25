# -*- coding: utf-8 -*-
"""
本机预览控制台（不是容器的一部分，纯粹为了"没 Docker 也能看一眼界面"）。

要点：
  · 路由器地址指向 TEST-NET-1（192.0.2.1，RFC 5737 保留段，永远不可达），
    所以**绝不会误碰真实路由器、不会消耗登录失败次数**。
  · 会话与设置写到临时目录，不污染 /data 语义的路径。
  · 只起 Web 服务，不起同步轮询循环（否则会一直打日志）。
"""
from __future__ import annotations

import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PORT = int(os.environ.get("PREVIEW_PORT", "8090"))
TMP = os.path.join(tempfile.gettempdir(), "ipv6sync-preview")
os.makedirs(TMP, exist_ok=True)

# 必须在 import config 之前设好
os.environ.update({
    "ROUTER_HOST": "192.0.2.1",          # TEST-NET-1，不可达
    "ROUTER_USER": "admin",
    "ROUTER_PASSWORD": "demo-password",  # 假密码：主机不可达，不可能发出去
    "TARGET_MAC": "AA:BB:CC:DD:EE:FF",
    "ENTRY_NAME": "NAS",
    "ENTRY_PORT": "16669",
    "POLL_INTERVAL": "60",
    "SESSION_FILE": os.path.join(TMP, "session.json"),
    "SETTINGS_FILE": os.path.join(TMP, "settings.json"),
    "ADMIN_USERNAME": os.environ.get("PREVIEW_USER", "admin"),
    "ADMIN_PASSWORD": os.environ.get("PREVIEW_PASSWORD", "preview"),
    "PORT": str(PORT),
    "WEB_HOST": "127.0.0.1",
    "HEALTH_PORT": "0",
    "LOG_LEVEL": "INFO",
})

from app.config import load_config          # noqa: E402
from app.main import setup_logging          # noqa: E402
from app.node import Node                   # noqa: E402
from app.webui import webui_from_env        # noqa: E402

setup_logging("INFO")
cfg = load_config([])
node = Node(cfg)
ui = webui_from_env(node)
srv = ui.start()

if not srv:
    raise SystemExit("控制台没起来")

print(f"预览控制台: http://127.0.0.1:{PORT}/   账号 {ui.user} / 口令 {os.environ['ADMIN_PASSWORD']}")
print("（路由器指向 192.0.2.1，不可达；所以设备类操作会失败，这是预期的）")

import time  # noqa: E402
try:
    while True:
        time.sleep(3600)
except KeyboardInterrupt:
    ui.stop()
