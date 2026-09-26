# -*- coding: utf-8 -*-
"""白名单修改日志：/data/events.jsonl，一行一条 JSON，**文件即数据**。

设计原则（和 state.json 的内存+落盘双份不同，这里刻意不做内存副本）：
  · 记录 = 直接往文件追加一行，完事；
  · 查询 = 现读文件逐行解析（最多几千行，毫秒级），进程里不留状态；
  · 保留上限在写入时执行：追加后发现超过上限，把文件重写为最新 N 条；
  · 清空 = 文件截断为空；重启不需要任何恢复逻辑。

为什么允许整文件重写：记录是低频动作（地址变化 / 用户操作才发生），
几千行的重写代价可忽略；换来的是"永不会有内存与文件不一致"的简单性。
掉电最坏留下半行 JSON —— 读取时按行解析，坏行跳过，不影响其余历史。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time

log = logging.getLogger("ipv6sync.events")

# 追加/裁剪/清空共用一把锁：网页的清空与后台线程的记录可能同时发生
_lock = threading.Lock()


def events_path(state_file: str) -> str:
    """日志文件路径：与 state.json 同目录（默认 /data/events.jsonl）。"""
    d = os.path.dirname(os.path.abspath(state_file or "/data/state.json"))
    return os.path.join(d, "events.jsonl")


def record(state_file: str, event: dict, maxn: int) -> None:
    """追加一条事件并执行保留上限。任何文件系统故障都只记日志、不抛出——
    修改日志是锦上添花的功能，绝不能拖累同步主流程。"""
    event = dict(event)
    event["ts"] = time.strftime("%Y-%m-%d %H:%M:%S")
    path = events_path(state_file)
    try:
        with _lock:
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
            _trim(path, maxn)
    except OSError as e:
        log.warning("修改日志写不进去（不影响同步）：%s", e)


def read(state_file: str) -> list[dict]:
    """现读全部事件（文件顺序 = 时间正序；前端自己倒序展示）。坏行跳过。"""
    path = events_path(state_file)
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out: list[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            v = json.loads(ln)
        except ValueError:
            continue                       # 掉电残留的半行，跳过
        if isinstance(v, dict):
            out.append(v)
    return out


def clear(state_file: str) -> None:
    path = events_path(state_file)
    with _lock:
        with open(path, "w", encoding="utf-8") as f:
            pass                            # 截断为空 = 一键清空
    log.info("修改日志已清空：%s", path)


def _trim(path: str, maxn: int) -> None:
    """超过保留上限时把文件重写为最新 maxn 条（调用方需持锁）。"""
    if maxn <= 0:
        return
    with open(path, encoding="utf-8") as f:
        lines = f.readlines()
    if len(lines) <= maxn:
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.writelines(lines[-maxn:])
    os.replace(tmp, path)                   # 原子替换，不掉电留半截
    log.info("同步记录超过保留上限 %d 条，已丢弃最旧 %d 条",
             maxn, len(lines) - maxn)
