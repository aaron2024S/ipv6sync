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

import ipaddress
import logging
import os
import threading

from . import discover, events, notify, settings as st, trustlist
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


def _port_label(p) -> str:
    """端口值 → 展示文本：-1/空 = 全部（修改日志的端口列用）。"""
    s = str(p or "").strip()
    return "全部" if s in ("", "-1") else s


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
        self.syncer.trigger = "手动同步"
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
        """实读 IPv6 防火墙白名单，附带规则列表与设备数据（弹窗选址用）。"""
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            try:
                return {"ok": True,
                        "enabled": trustlist.is_enabled(self.router),
                        "max": trustlist.MAX_ENTRIES,
                        "entries": trustlist.list_entries(self.router),
                        "rules": [{"name": r.name, "mac": r.mac or "",
                                   "port": "全部" if r.port in (None, -1)
                                           else r.port,
                                   "remote_ip": r.remote_ip}
                                  for r in self.cfg.rules],
                        "hosts": self._host_rows()}
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
            self.syncer.firewall_on = on
            self.syncer._fw_seen = on      # 网页动作自己记日志，别让下轮重复记
            self.syncer._log_event("fw", trigger="网页", device="（全局）",
                                   entry="—",
                                   addr="总开关已开启" if on
                                   else "总开关已关闭（进入待机）", port="")
            return {"ok": True, "enabled": on}

    def reset_counters(self) -> dict:
        """清零累计写入计数（网页「重置累计写入」按钮，需在网页上显式确认）。"""
        with self.syncer.lock:
            self.syncer.counters.reset()
            self.syncer.counters.flush()
            log.info("累计写入计数已从网页重置清零")
            return {"ok": True, "counters": self.syncer.counters.payload()}

    # ---------------- 修改日志（文件即数据，见 events.py） ----------------

    def events_list(self) -> dict:
        """现读 /data/events.jsonl（时间正序返回，前端倒序展示 + 过滤分页）。"""
        return {"ok": True, "events": events.read(self.cfg.state_file),
                "max": self.cfg.events_max}

    def events_clear(self) -> dict:
        """一键清空修改日志（网页上已二次确认；不影响白名单和同步）。"""
        events.clear(self.cfg.state_file)
        return {"ok": True}

    def set_events_max(self, value) -> dict:
        """改保留条数：立即裁剪并落盘（值存 settings.json 的 LOG_MAX，表单里隐藏）。"""
        try:
            n = int(value)
        except (TypeError, ValueError):
            raise st.SettingsError("保留条数必须是数字")
        if not 10 <= n <= 5000:
            raise st.SettingsError("保留条数必须在 10 ~ 5000 之间")
        with self._lock:
            merged = dict(self.overlay)
            merged["LOG_MAX"] = n
            clean = st.validate_overlay(merged)
            st.save_overlay(clean, self.settings_file)
            self.overlay = clean
            self.cfg.events_max = n
        # 立即按新上限裁剪一次（调小时马上生效，不等下次写入）
        path = events.events_path(self.cfg.state_file)
        try:
            with events._lock:
                events._trim(path, n)
        except OSError as e:
            log.warning("按新上限裁剪同步记录失败：%s", e)
        log.info("同步记录保留上限改为 %d 条", n)
        return {"ok": True, "max": n}

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

    def _host_rows(self) -> list[dict]:
        """HostInfo → 设备行（设备列表页与白名单弹窗共用）。调用方需已登录。"""
        rows = []
        for h in self.router.get("HostInfo") or []:
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
        return rows

    def hosts(self) -> dict:
        """设备列表（在线/离线/儿童上网 + WiFi 黑名单）。"""
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            try:
                rows = self._host_rows()
            except RouterError as e:
                return {"ok": False, "error": str(e)}
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

    # ---------------- 白名单规则管理（网页白名单页的弹窗） ----------------

    @staticmethod
    def _rule_from_cfg(r) -> dict:
        """Config 里的 Rule → overlay 存储形态（规则列表增删改的公共底座）。"""
        port = r.port
        return {"name": r.name, "mac": r.mac or "",
                "port": "-1" if port in (None, -1) else str(port),
                "remote_ip": r.remote_ip or "::/0"}

    @staticmethod
    def _rule_payload(data: dict) -> dict:
        """校验网页提交的一条规则，返回规范化后的存储形态。"""
        name = str(data.get("name") or "").strip()
        if not name:
            raise st.SettingsError("服务名称不能为空")
        if len(name) > 48:
            raise st.SettingsError("服务名称最长 48 个字符")
        mac = discover.norm_mac(data.get("mac") or "")
        if not mac:
            raise st.SettingsError(
                "必须选择一台设备 —— 自动维护要跟随设备的地址变化")
        port = str(data.get("port") or "-1").strip() or "-1"
        if port != "-1":
            for tok in port.replace("，", ",").split(","):
                tok = tok.strip()
                if not tok.isdigit() or not 1 <= int(tok) <= 65535:
                    raise st.SettingsError(
                        "放行端口格式不对：多个端口用英文逗号分隔，"
                        "每个 1-65535，或 -1 表示全部端口")
        remote = str(data.get("remote_ip") or "::/0").strip() or "::/0"
        try:
            ipaddress.ip_network(remote, strict=False)
        except ValueError:
            raise st.SettingsError(f"允许来源 {remote!r} 不是合法的网段或地址")
        return {"name": name, "mac": mac, "port": port, "remote_ip": remote}

    def _persist_rules(self, rules: list[dict]) -> None:
        """把规则列表写进 overlay 并热重配（调用方需持 syncer.lock）。"""
        merged = dict(self.overlay)
        merged[st.RULES_KEY] = rules
        clean = st.validate_overlay(merged)          # 坏值在这里被拒
        st.save_overlay(clean, self.settings_file)
        new_cfg = load_config([])
        self.cfg = new_cfg
        self.overlay = clean
        self.syncer.reconfigure(new_cfg, self.router)

    def rules_api(self, data: dict) -> dict:
        """白名单页的规则管理：add / update / remove，改完立即收敛一轮。

        remove 与「改名」都会把旧名字下本程序生成的条目从路由器上删掉 ——
        不然规则没了名字就没人认领，那些条目永远躺在白名单里。
        """
        action = str(data.get("action") or "")
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            try:
                rules = [self._rule_from_cfg(r) for r in self.cfg.rules]
                if action == "add":
                    item = self._rule_payload(data)
                    if any(x["name"] == item["name"] for x in rules):
                        raise st.SettingsError(f"已存在同名条目：{item['name']}")
                    rules.append(item)
                elif action == "update":
                    old = str(data.get("old_name") or "").strip()
                    idx = next((i for i, x in enumerate(rules)
                                if x["name"] == old), None)
                    if idx is None:
                        raise st.SettingsError(f"找不到要修改的条目：{old}")
                    item = self._rule_payload(data)
                    if item["name"] != old and any(
                            x["name"] == item["name"] for x in rules):
                        raise st.SettingsError(f"已存在同名条目：{item['name']}")
                    if item["name"] != old:
                        self._remove_entries_of(old)
                    rules[idx] = item
                elif action == "remove":
                    name = str(data.get("name") or "").strip()
                    idx = next((i for i, x in enumerate(rules)
                                if x["name"] == name), None)
                    if idx is None:
                        raise st.SettingsError(f"找不到要删除的条目：{name}")
                    self._remove_entries_of(name)
                    rules.pop(idx)
                else:
                    raise st.SettingsError("action 必须是 add / update / remove")

                self._persist_rules(rules)
            except st.SettingsError as e:
                return {"ok": False, "error": str(e)}

            if action in ("add", "update"):
                self.syncer.trigger = ("弹窗添加" if action == "add"
                                       else "弹窗修改")
                snap = self.syncer.tick()    # 立即按新规则收敛一轮（含变更通知）
                return {"ok": bool(snap.get("ok", True)),
                        "notes": ["已保存并立即同步"]}
            return {"ok": True, "notes": ["已删除条目及其自动维护规则"]}

    def _remove_entries_of(self, base: str) -> None:
        """把某规则名下的本程序条目从路由器上删掉（需持 syncer.lock）。"""
        old = {e.get("Name"): (e.get("LocalIp") or "", e.get("Port"))
               for e in trustlist.list_entries(self.router)}
        gone = trustlist.remove_stale(self.router, base, set())
        if gone:
            self.syncer.counters.bump(removed=len(gone))
            self.syncer.counters.flush()
            for nm in gone:
                oa, op = old.get(nm, ("", ""))
                self.syncer._log_event("removed", trigger="网页操作",
                                       device=base, entry=nm,
                                       addr=oa, port=_port_label(op))
            log.info("已删除规则 %s 名下的条目：%s", base, ", ".join(gone))

    def delete_whitelist_entry(self, data: dict) -> dict:
        """删除路由器白名单上的一条条目（手工条目的删除按钮）。"""
        entry_id = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            target = None
            for e in trustlist.list_entries(self.router):
                if ((entry_id and e.get("ID") == entry_id)
                        or (not entry_id and name and e.get("Name") == name)):
                    target = e
                    break
            if target is None:
                return {"ok": False,
                        "error": "白名单里找不到这条条目（可能已被删除）"}
            try:
                res = trustlist.delete(self.router, target)
            except RouterError as e:
                return {"ok": False, "error": str(e)}
            if not trustlist.ok(res):
                return {"ok": False, "error": f"设备拒绝：{res}"}
            self.syncer.counters.bump(removed=1)
            self.syncer.counters.flush()
            self.syncer._log_event("removed", trigger="网页操作",
                                   device=target.get("Name") or "（手工条目）",
                                   entry=target.get("Name") or "",
                                   addr=target.get("LocalIp") or "",
                                   port=_port_label(target.get("Port")))
            return {"ok": True}

    def update_whitelist_entry(self, data: dict) -> dict:
        """原地修改路由器白名单上的一条手工条目（不建自动维护规则）。"""
        entry_id = str(data.get("id") or "").strip()
        name = str(data.get("name") or "").strip()
        if not name:
            raise st.SettingsError("服务名称不能为空")
        local_ip = str(data.get("local_ip") or "").strip()
        if not discover.canon(local_ip):
            raise st.SettingsError(f"本地 IP {local_ip!r} 不是合法的 IPv6 地址")
        remote = str(data.get("remote_ip") or "::/0").strip() or "::/0"
        try:
            ipaddress.ip_network(remote, strict=False)
        except ValueError:
            raise st.SettingsError(f"允许来源 {remote!r} 不是合法的网段或地址")
        port = str(data.get("port") or "-1").strip() or "-1"
        if port != "-1":
            for tok in port.replace("，", ",").split(","):
                tok = tok.strip()
                if not tok.isdigit() or not 1 <= int(tok) <= 65535:
                    raise st.SettingsError(
                        "放行端口格式不对：多个端口用英文逗号分隔，"
                        "每个 1-65535，或 -1 表示全部端口")
        with self.syncer.lock:
            err = self._logged_in()
            if err:
                return {"ok": False, "error": err}
            target = None
            for e in trustlist.list_entries(self.router):
                if entry_id and e.get("ID") == entry_id:
                    target = e
                    break
            if target is None:
                return {"ok": False,
                        "error": "白名单里找不到这条条目（可能已被删除）"}
            try:
                res = trustlist.update(self.router, target["ID"], name,
                                       local_ip,
                                       None if port == "-1" else port, remote)
            except RouterError as e:
                return {"ok": False, "error": str(e)}
            if not trustlist.ok(res):
                return {"ok": False, "error": f"设备拒绝：{res}"}
            self.syncer.counters.bump(updated=1)
            self.syncer.counters.flush()
            self.syncer._log_event("updated", trigger="网页操作",
                                   device=name, entry=name, addr=local_ip,
                                   old_addr=target.get("LocalIp") or "",
                                   port=_port_label(port))
            return {"ok": True}
