# -*- coding: utf-8 -*-
"""跨容器重启的累计计数

为什么需要这个：`total_writes` 以前只活在内存里。容器一重启（改 compose 的
环境变量、NAS 重启、被 watchdog 拉起、crash 后自动重启），计数就归零，网页上
「累计写入」永远停在个位数，看起来像程序从来没干过活 —— 用户没法拿它判断
「到底同步过没有」。

写入本身是低频事件（有变化才写，一轮可能一次都不写），所以每轮同步结束时
落一次盘，代价可以忽略。这里**只记「确实发生过的动作」**，不记任何派生状态：
文件读不出来就当从来没发生过、从 0 开始 —— 统计坏了绝不能让同步起不来。

⚠ 文件里只有几个整数和时间戳，不含任何凭据，权限 0600 足够。
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time

log = logging.getLogger("ipv6sync.state")

DEFAULT_STATE_FILE = "/data/state.json"


def state_path() -> str:
    return os.environ.get("STATE_FILE") or DEFAULT_STATE_FILE


class Counters:
    """累计计数（跨重启）+ 本次启动计数。

    `bump()` 只改内存并标记脏，真正落盘由 `flush()` 做 —— 这样一轮同步里写了
    N 个地址也只写一次文件，不会写 N 次。
    """

    def __init__(self, path: str | None = None):
        self.path = path or state_path()
        self.created = 0
        self.updated = 0
        self.removed = 0
        self.first_write_at = ""
        self.last_write_at = ""
        self.file_error: str | None = None    # 最近一次读写失败原因（网页上要能看见）
        self._dirty = False
        # 本次启动以来的增量 —— 不落盘，重启即清零，用来和累计值对照
        self.session_created = 0
        self.session_updated = 0
        self.session_removed = 0
        self.load()

    # ---------------- 读 ----------------

    def load(self) -> bool:
        """读历史计数。任何问题都只是「从 0 开始」，绝不抛异常。"""
        if not self.path or not os.path.exists(self.path):
            return True
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            self.file_error = f"读取失败：{e}"
            log.warning("累计计数 %s 读不出来（%s），本次从 0 开始", self.path, e)
            return False
        if not isinstance(data, dict):
            self.file_error = "文件顶层不是对象"
            log.warning("累计计数 %s 顶层不是对象，已忽略", self.path)
            return False

        for k in ("created", "updated", "removed"):
            v = data.get(k)
            # bool 是 int 的子类，显式排掉，免得 true 被当成 1
            if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
                setattr(self, k, v)
        for k in ("first_write_at", "last_write_at"):
            v = data.get(k)
            if isinstance(v, str):
                setattr(self, k, v)
        self.file_error = None
        return True

    # ---------------- 写 ----------------

    def reset(self) -> None:
        """全部清零（网页「重置累计写入」用）。落盘交给调用方 flush()。"""
        self.created = self.updated = self.removed = 0
        self.session_created = self.session_updated = self.session_removed = 0
        self.first_write_at = self.last_write_at = ""
        self._dirty = True

    def bump(self, created: int = 0, updated: int = 0, removed: int = 0) -> None:
        """记一次真实发生过的写入 / 删除。只动内存，落盘交给 flush()。"""
        created = max(0, int(created or 0))
        updated = max(0, int(updated or 0))
        removed = max(0, int(removed or 0))
        if not (created or updated or removed):
            return
        self.created += created
        self.updated += updated
        self.removed += removed
        self.session_created += created
        self.session_updated += updated
        self.session_removed += removed
        if created or updated:
            now = time.strftime("%Y-%m-%d %H:%M:%S")
            self.last_write_at = now
            if not self.first_write_at:
                self.first_write_at = now
        self._dirty = True

    def flush(self) -> bool:
        """落盘。失败只记日志、留着下次再试 —— 统计绝不能影响同步本身。"""
        if not self._dirty or not self.path:
            return True
        tmp = None
        try:
            d = os.path.dirname(os.path.abspath(self.path))
            os.makedirs(d, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=d, prefix=".state-", suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.payload(), f, ensure_ascii=False, indent=1,
                          sort_keys=True)
                f.write("\n")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as e:
            self.file_error = f"写入失败：{e}"
            log.warning("累计计数写不进 %s：%s（下轮再试）", self.path, e)
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
            return False
        self._dirty = False
        self.file_error = None
        return True

    # ---------------- 视图 ----------------

    @property
    def total_writes(self) -> int:
        """累计写入的条目数（新增 + 更新）。删除单独算在 `removed`。"""
        return self.created + self.updated

    @property
    def session_writes(self) -> int:
        """本次启动以来写入的条目数。"""
        return self.session_created + self.session_updated

    def payload(self) -> dict:
        return {
            "created": self.created,
            "updated": self.updated,
            "removed": self.removed,
            "first_write_at": self.first_write_at,
            "last_write_at": self.last_write_at,
        }

    def snapshot(self) -> dict:
        return {
            "total_writes": self.total_writes,
            "created": self.created,
            "updated": self.updated,
            "removed": self.removed,
            "first_write_at": self.first_write_at,
            "last_write_at": self.last_write_at,
            "session_writes": self.session_writes,
            "session_created": self.session_created,
            "session_updated": self.session_updated,
            "session_removed": self.session_removed,
            "state_file": self.path,
            "state_file_error": self.file_error,
        }
