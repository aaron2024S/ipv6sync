# -*- coding: utf-8 -*-
"""
通知推送：防火墙白名单发生变更后，把变化发到 ntfy / gotify / 企业微信群机器人。

三个渠道互相独立：各自的「启用」开关 + 自己的地址/Token/ID，
开了几个就同时推几个，互不影响。

设计约束：
  · 只依赖标准库 urllib —— 容器里没有第三方依赖
  · send_all() **永不抛异常**：通知只是锦上添花，任何失败都只返回错误说明，
    绝不能影响同步主流程
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request

log = logging.getLogger("ipv6sync.notify")

CHANNELS = ("ntfy", "gotify", "wecom")
TIMEOUT = 10


def extract_wecom_key(raw: str) -> str:
    """企业微信机器人标识：用户可能粘贴整个 webhook 地址，也可能只给 key 的 ID。

    支持两种输入，返回纯 key：
      https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=85d3153d-xxxx
      85d3153d-xxxx
    """
    s = (raw or "").strip()
    if "key=" in s:
        s = s.split("key=", 1)[1]
    return s.strip().strip("/").strip()


def enabled_channels(cfg) -> list[str]:
    """当前启用了哪些通知渠道（按 CHANNELS 顺序）。"""
    out = []
    for ch in CHANNELS:
        try:
            if getattr(cfg, f"notify_{ch}_enabled", False):
                out.append(ch)
        except Exception:  # noqa: BLE001
            pass
    return out


def send_all(cfg, title: str, message: str) -> list[tuple[str, bool, str]]:
    """向所有启用的渠道各推一条通知。

    返回 [(渠道名, 是否成功, 错误说明), ...]，一个渠道失败不影响其它渠道。
    没启用任何渠道时返回空列表。
    """
    results: list[tuple[str, bool, str]] = []
    for ch in enabled_channels(cfg):
        fn = {"ntfy": _ntfy, "gotify": _gotify, "wecom": _wecom}[ch]
        try:
            ok, err = fn(cfg, title, message)
        except Exception as e:  # noqa: BLE001
            log.warning("通知发送失败（%s）：%s", ch, e)
            ok, err = False, f"{type(e).__name__}: {e}"
        results.append((ch, ok, err))
    return results


# --------------------------------------------------------------------------
# 三个渠道的实现
# --------------------------------------------------------------------------

def _ntfy(cfg, title: str, message: str) -> tuple[bool, str]:
    """ntfy：POST 正文到 {服务器地址}/{主题}；地址里没写主题就用 ipv6sync。

    Token 可选（ntfy 的访问令牌，tk_ 开头），给了就带 Bearer 认证。
    标题不塞 HTTP 头（ntfy 对非 ASCII 头兼容性差），直接拼进正文。
    """
    base = (getattr(cfg, "notify_ntfy_url", "") or "").strip().rstrip("/")
    if not base:
        return False, "ntfy：未填服务器地址"
    if urllib.parse.urlparse(base).path in ("", "/"):
        base += "/ipv6sync"
    headers = {}
    if getattr(cfg, "notify_ntfy_token", ""):
        headers["Authorization"] = "Bearer " + cfg.notify_ntfy_token
    body = (title + "\n" + message).strip()
    return _post_raw(base, body.encode("utf-8"), headers, tag="ntfy")


def _gotify(cfg, title: str, message: str) -> tuple[bool, str]:
    """gotify：POST {服务器地址}/message?token=应用Token，JSON 带 title/message。"""
    base = (getattr(cfg, "notify_gotify_url", "") or "").strip().rstrip("/")
    if not base:
        return False, "gotify：未填服务器地址"
    token = getattr(cfg, "notify_gotify_token", "")
    if not token:
        return False, "gotify：未填 Token"
    url = base + "/message?token=" + urllib.parse.quote(token)
    ok, body = _post_json(url, {"title": title, "message": message})
    if ok:
        return True, ""
    # gotify 出错时返回 JSON {"error":"..."}
    try:
        return False, "gotify：" + json.loads(body).get("error", body[:160])
    except Exception:  # noqa: BLE001
        return False, f"gotify：{body[:160] or 'HTTP 错误'}"


def _wecom(cfg, title: str, message: str) -> tuple[bool, str]:
    """企业微信群机器人：只需要 webhook 里 key= 后面的那段 ID。"""
    key = extract_wecom_key(getattr(cfg, "notify_wecom_id", ""))
    if not key:
        return False, "企业微信：未填机器人 ID"
    content = (title + "\n" + message).strip()
    # 群机器人 text 消息上限 2048 字节（UTF-8），留余量截断，别让请求被整个拒掉
    content = content.encode("utf-8")[:2000].decode("utf-8", "ignore")
    url = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
           + urllib.parse.quote(key))
    ok, body = _post_json(url, {"msgtype": "text", "text": {"content": content}})
    if not ok:
        return False, f"企业微信：{body[:160] or 'HTTP 错误'}"
    # 企业微信 HTTP 恒 200，业务错误看 errcode
    try:
        j = json.loads(body)
        if j.get("errcode") not in (0, None):
            return False, f"企业微信错误 {j.get('errcode')}: {j.get('errmsg', '')}"
    except Exception:  # noqa: BLE001
        pass
    return True, ""


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------

def _post_raw(url: str, data: bytes, headers: dict | None = None,
              tag: str = "") -> tuple[bool, str]:
    """POST 原始字节，返回 (HTTP 是否成功, 响应文本)。"""
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return (200 <= r.status < 300), ""
    except Exception as e:  # noqa: BLE001
        return False, f"{tag}：{e}" if tag else str(e)


def _post_json(url: str, payload: dict) -> tuple[bool, str]:
    """POST JSON，返回 (HTTP 是否成功, 响应文本)。"""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type":
                                          "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return (200 <= r.status < 300), r.read().decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        body = ""
        resp = getattr(e, "read", None)
        if callable(resp):
            try:
                body = resp().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                body = ""
        return False, body or str(e)
