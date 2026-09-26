# -*- coding: utf-8 -*-
"""
运行时装配

把「配置 + 路由器客户端 + 同步器」装成一个 Node，并提供一个能力：
**网页改完设置能热生效**（重建 Config 与路由器对象，同步器原地换掉）。

配置优先级：网页设置(settings.json) > 环境变量 > 代码默认值

这里所有会碰路由器的动作都走 `syncer.ensure_login()` 而不是 `router.login()` ——
因为 ensure_login 带**指数退避**。设备连续 3 次密码错就锁 admin 账号，
网页这种"刷新一下就试一次"的入口如果绕过退避，很容易把账号锁死。
"""
from __future__ import annotations

import logging
import os
import threading

from . import discover, notify, settings as st, trustlist
from .config import load_config
from .router import HuaweiRouter, RouterError
from .sync import Syncer

log = logging.getLogger("ipv6sync.node")


def make_router(cfg) -> HuaweiRouter:
    return HuaweiRouter(base=cfg.base_url, username=cfg.username,
                        password=cfg.password, session_file=cfg.session_file,
                        timeout=cfg.timeout)


def _truthy(v) -> bool:
    """固件的开关字段可能是 bool / 1 / "0" / "false" 等各种形态，统一判定。

    注意 bool("0") 是 True，直接 bool() 会把固件用字符串表示的「关」误判成开。
    """
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() not in ("", "0", "false", "off", "no", "none")


def _is_kid(h: dict) -> bool:
    """该设备是否被路由器「儿童上网保护」管控。

    HostInfo 里带管控标记的字段有两个（固件版本不同留哪个不定）：
    ParentControlEnable（开关）与 MacFilterID（管控档案 ID，空/"0" 表示未加入）。
    """
    if _truthy(h.get("ParentControlEnable")):
        return True
    mfid = str(h.get("MacFilterID") or "").strip()
    return mfid not in ("", "0")


def _parse_blacklist(res) -> list:
    """从 wlanfilterenhance 响应里抽 WiFi MAC 黑名单（2.4G/5G 各一份，合并去重）。

    固件返回结构随版本变化（整体是列表还是字典、条目是对象还是纯 MAC 字符串
    都可能），这里做宽容解析：递归找键名含 BMAC 的字段，条目尽量带出主机名
    和频段。同名 MAC 跨频段重复出现时合并为一行，频段用 "+" 拼接。
    """
    rows: dict[str, dict] = {}

    def band_label(node: dict, fallback: str) -> str:
        for k in ("WifiModulename", "wifiModulename", "FrequencyBand",
                  "WifiBand", "Band", "band"):
            v = node.get(k)
            if v:
                return str(v)
        return fallback

    def add(mac, name, band: str):
        mac = str(mac or "").strip().upper()
        if not mac:
            return
        cur = rows.setdefault(mac, {"mac": mac, "name": "", "band": ""})
        if name and not cur["name"]:
            cur["name"] = str(name)
        if band and band not in cur["band"]:
            cur["band"] = (cur["band"] + "+" + band) if cur["band"] else band

    def walk(node, band: str):
        if isinstance(node, list):
            for x in node:
                walk(x, band)
            return
        if not isinstance(node, dict):
            return
        b = band_label(node, band)
        for key, val in node.items():
            if "bmac" in str(key).lower():
                entries = val
                if isinstance(entries, dict):      # 个别固件再包一层字典
                    entries = [v for v in entries.values() if isinstance(v, list)]
                    entries = [e for lst in entries for e in lst]
                if isinstance(entries, str):
                    entries = [entries]
                for e in entries if isinstance(entries, list) else []:
                    if isinstance(e, dict):
                        mac = (e.get("MACAddress") or e.get("MacAddress")
                               or e.get("BMACAddress") or e.get("mac"))
                        name = (e.get("HostName") or e.get("hostname")
                                or e.get("devName") or e.get("DeviceName"))
                        add(mac, name or "", b)
                    else:
                        add(e, "", b)
            else:
                walk(val, b)

    walk(res, "")
    return sorted(rows.values(), key=lambda r: r["mac"])


class Node:
    def __init__(self, cfg):
        self.settings_file = cfg.settings_file or st.settings_path()
        self.cfg = cfg
        self.overlay = st.load_overlay(self.settings_file)
        self.router = make_router(cfg)
        if self.router.session_file and self.router.load():
            log.info("已加载既有会话（%s）", self.router.session_file)
        self.syncer = Syncer(cfg, self.router)
        # 只保护"替换 self.cfg / self.router"这几行本身；
        # 真正跑一轮同步的互斥由 syncer.lock 负责。
        self._lock = threading.RLock()

    # ---------------- 只读视图 ----------------

    def snapshot(self) -> dict:
        return self.syncer.snapshot()

    def settings_view(self) -> list:
        with self._lock:
            return st.view(self.cfg, self.overlay)

    def overview(self) -> dict:
        with self._lock:
            cfg, ov = self.cfg, self.overlay
            return {
                "router_base": cfg.base_url,
                "router_user": cfg.username,
                "router_host": cfg.host,
                "password_from": cfg.password_source or (
                    "（未配置，只复用会话）" if not cfg.password else "环境变量"),
                "password_set": bool(cfg.password),
                "settings_file": self.settings_file,
                "settings_keys": sorted(ov),
                "session_file": cfg.session_file,
                "rules": [{
                    "name": r.name,
                    "port": "全部" if r.port in (None, -1) else r.port,
                    "remote_ip": r.remote_ip,
                    "mac": r.mac or "",
                } for r in cfg.rules],
            }

    # ---------------- 改设置 ----------------

    def save_settings(self, patch: dict) -> dict:
        """合并 → 校验 → 落盘 → 热重配。返回 {"overview":..,"settings":..,"notes":[..]}"""
        if not isinstance(patch, dict) or not patch:
            raise st.SettingsError("没有要保存的内容")

        with self._lock:
            merged = dict(self.overlay)
            merged.update(patch)
            clean = st.validate_overlay(merged)          # 坏值在这里被拒

            old = self.cfg
            st.save_overlay(clean, self.settings_file)

            # 重新装配：环境变量打底，覆盖层盖上去 —— 与启动时的顺序完全一致
            new_cfg = load_config([])
            notes = []

            # 换了地址 / 用户名 / 密码 → 旧会话对新身份没意义，直接丢掉
            cred_changed = (new_cfg.host != old.host
                            or new_cfg.username != old.username
                            or new_cfg.password != old.password)
            if cred_changed:
                self._drop_session(old.session_file)
                notes.append("路由器地址/账号/密码有变动，已丢弃旧会话，稍后按新账号重新登录")

            new_router = make_router(new_cfg)
            if not cred_changed and new_router.load():
                notes.append("已复用既有会话")

            self.cfg = new_cfg
            self.overlay = clean
            self.router = new_router
            self.syncer.reconfigure(new_cfg, new_router)

            log.info("网页设置已应用（%d 项变更）", len(patch))
            return {"overview": self.overview(),
                    "settings": st.view(new_cfg, clean),
                    "notes": notes}

    @staticmethod
    def _drop_session(path: str | None):
        if path and os.path.exists(path):
            try:
                os.remove(path)
                log.info("已删除旧会话 %s", path)
            except OSError as e:
                log.warning("删不掉旧会话 %s：%s", path, e)

    # ---------------- 动路由器 ----------------

    def _logged_in(self) -> str | None:
        """保证处于登录态；不行就返回错误说明（不抛异常，方便直接回给网页）。"""
        if self.syncer.ensure_login():
            return None
        left = self.syncer.login_backoff_remaining()
        base = self.syncer.last_error or "未知原因"
        if left > 0:
            # 网页上点按钮最容易踩这个：退避期间什么都不会发生，得说清为什么，
            # 否则用户会以为坏了，然后一直点。
            return (f"登录退避中，还需 {left} 秒才会再试（上次失败：{base}）。"
                    f"这是为了不让密码连错 3 次锁死管理员账号，先确认密码对不对。")
        return base

    def sync_now(self) -> dict:
        """立即跑一轮同步（与主循环互斥）。"""
        log.info("网页触发：立即同步一次")
        return self.syncer.tick()

    def test_connection(self) -> dict:
        """测试路由器连通性与账号是否可用：登录 + 读设备信息。"""
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            try:
                info = self.router.get("deviceinfo") or {}
            except RouterError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "device": self._device_summary(info)}

    @staticmethod
    def _device_summary(info: dict) -> dict:
        return {
            "product": info.get("ProductName") or info.get("DeviceName") or "",
            "model": info.get("DeviceModel") or info.get("Model") or "",
            "sn": info.get("SerialNumber") or info.get("Sn") or "",
            "software": info.get("SoftwareVersion") or "",
        }

    def whitelist(self) -> dict:
        """实读 IPv6 防火墙白名单。"""
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            try:
                return {"ok": True,
                        "enabled": trustlist.is_enabled(self.router),
                        "max": trustlist.MAX_ENTRIES,
                        "entries": trustlist.list_entries(self.router)}
            except RouterError as e:
                return {"ok": False, "error": str(e)}

    def set_firewall(self, on: bool) -> dict:
        """开 / 关 IPv6 防火墙总开关（网页上的显式动作，不自动做）。"""
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            try:
                res = trustlist.set_enabled(self.router, on)
            except RouterError as e:
                return {"ok": False, "error": str(e)}
            if not trustlist.ok(res):
                return {"ok": False, "error": f"设备拒绝：{res}"}
            return {"ok": True, "enabled": on}

    def test_notify(self) -> dict:
        """按当前通知配置向所有启用渠道各发一条测试消息（网页「发送测试通知」按钮）。"""
        cfg = self.cfg
        results = notify.send_all(cfg,
                                  (cfg.notify_title or "IPv6 白名单已更新")
                                  + "（测试）",
                                  "如果你收到这条消息，说明通知配置是通的。")
        if not results:
            return {"ok": False,
                    "error": "没有启用任何通知渠道，先在「通知设置」里打开开关并保存"}
        items = [{"channel": ch, "ok": ok, "error": err} for ch, ok, err in results]
        failed = [f"{ch}: {err}" for ch, ok, err in results if not ok]
        return {"ok": not failed, "results": items,
                "error": "; ".join(failed)}

    def hosts(self) -> dict:
        """设备列表（在线/离线/儿童上网 + WiFi 黑名单）。"""
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            try:
                raw = self.router.get("HostInfo") or []
            except RouterError as e:
                return {"ok": False, "error": str(e)}
            rows = []
            for h in raw:
                addrs = discover.host_candidates(h)
                offline_at = (h.get("OfflineRecord") or "").split("#")[0].strip()
                active = _truthy(h.get("Active"))
                rows.append({
                    "mac": h.get("MACAddress", "") or "",
                    "name": (h.get("HostName") or h.get("ActualName") or "")[:48],
                    "ipv4": h.get("IPAddress", "") or "",
                    "ipv6": addrs,
                    "active": active,
                    "offline_at": offline_at if (offline_at and not active) else "",
                    "kids": _is_kid(h),
                })
            rows.sort(key=lambda r: r["mac"])
            # 黑名单是另一个接口（wlanfilterenhance），失败不拖累设备列表本身
            try:
                blacklist = _parse_blacklist(
                    self.router.get("ntwk/wlanfilterenhance"))
                bl_error = ""
            except RouterError as e:
                blacklist, bl_error = [], str(e)
            return {"ok": True, "hosts": rows,
                    "blacklist": blacklist, "blacklist_error": bl_error,
                    "lan_prefixes": discover.lan_prefixes(self.router)}
