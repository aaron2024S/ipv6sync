# -*- coding: utf-8 -*-
"""
同步引擎：发现目标设备当前 IPv6 → 与白名单条目比对 → 有变化才写回

「有变化才写」很关键：路由器配置写入是 flash 操作，没必要每次轮询都写一遍；
而且不写就不会碰到设备的写入频率限制。
"""
from __future__ import annotations

import dataclasses
import logging
import threading
import time

from . import discover, events, notify, state, trustlist
from .config import Config, Rule
from .router import (AuthFailed, NetworkError, NotLoggedIn, RouterError,
                     SessionExpired)

log = logging.getLogger("ipv6sync.sync")


def _truthy(v) -> bool:
    """固件的开关字段可能是 bool / 1 / "0" / "false" 等各种形态，统一判定。

    注意 bool("0") 是 True，直接 bool() 会把固件用字符串表示的「关」误判成开。
    """
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() not in ("", "0", "false", "off", "no", "none")


@dataclasses.dataclass
class RuleState:
    name: str
    addr: str | None = None            # 主地址（= addrs[0]），网页列表显示用
    addrs: list = dataclasses.field(default_factory=list)   # 本轮采用的全部地址
    action: str = "-"
    detail: str = ""
    error: str | None = None
    at: str = ""

    def update(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)
        self.at = time.strftime("%Y-%m-%d %H:%M:%S")


class Syncer:
    def __init__(self, cfg: Config, router):
        self.cfg = cfg
        self.router = router
        # 主循环在一个线程里 tick，网页控制台在另一个线程里改设置 / 触发同步，
        # 所以状态与"换配置"这两个动作必须互斥。
        self.lock = threading.RLock()
        self.states: dict[str, RuleState] = {r.name: RuleState(r.name)
                                             for r in cfg.rules}
        self.last_tick_at = ""
        self.last_error: str | None = None
        self.consecutive_failures = 0
        # 最近一轮的结果。注意它和 `snapshot(ok=...)` 的那个 ok 不是一回事：
        # 那个是「刚刚这一轮怎么样」，只在 tick 的返回值里有意义；网页是**轮询**
        # 拿状态的，它永远赶不上某一轮，所以必须把结果留在对象上。
        # 以前网页直接拿到的 ok 是 None，于是「同步」永远显示「等待首轮」。
        self.last_ok: bool | None = None
        self.last_success_at = ""
        # 累计计数跨容器重启（见 state.py）—— 存 /data 卷里，重启不归零
        self.counters = state.Counters(cfg.state_file)
        self.login_fail_count = 0
        self.next_login_at = 0.0
        self.firewall_on: bool | None = None
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        # 本轮实际改动了防火墙配置的描述（变更通知的素材），每轮 tick 开头清空
        self.tick_changes: list[str] = []
        # 本轮写入的触发来源（修改日志里的「触发」列）：主循环轮询是默认值，
        # 网页动作（立即同步 / 弹窗添加）在调用 tick 前先改写它。
        self.trigger = "轮询"
        # 上一轮观察到的防火墙总开关状态（None = 还没观察过）：用于把「开关
        # 在路由器端被人为改动」也记进修改日志。网页开关自己记，不靠这里。
        self._fw_seen = None

    def _log_event(self, action: str, trigger: str | None = None, **ev) -> None:
        """往修改日志追加一条（文件即数据，见 events.py）。失败不影响同步。"""
        events.record(self.cfg.state_file,
                      dict(ev, action=action,
                           trigger=trigger or self.trigger),
                      self.cfg.events_max)

    # ---------------- 热重配（网页改完设置后调用） ----------------

    def reconfigure(self, cfg: Config, router) -> None:
        """换成新的配置与路由器对象。

        会清掉登录退避计数 —— 换密码/换地址之后应该立刻允许试一次，
        沿用旧的退避时间会让"改完没反应"看起来像个 bug。
        """
        with self.lock:
            self.cfg = cfg
            self.router = router
            self.states = {r.name: RuleState(r.name) for r in cfg.rules}
            self.login_fail_count = 0
            self.next_login_at = 0.0
            self.firewall_on = None
            self.last_error = None
            log.info("已应用新配置：路由器=%s 用户=%s 周期=%ds 条目=%s",
                     cfg.base_url, cfg.username, cfg.poll_interval,
                     ", ".join(r.name for r in cfg.rules) or "-")

    # ---------------- 登录（带强退避） ----------------

    def login_backoff_remaining(self) -> int:
        """距离下次允许尝试登录还有多少秒（0 = 现在就可以试）。"""
        return max(0, int(self.next_login_at - time.time()))

    def ensure_login(self) -> bool:
        """复用会话失败才登录。

        密码错误时**指数退避**：设备 maxfailtimes=3，连续错 3 次会锁账号，
        自动重试必须足够慢，否则会把管理员锁死在门外。
        """
        if self.router.logged_in and self.router.check_session():
            return True

        # 没配密码（纯网页首次部署的初始状态）：绝不能拿空密码去撞登录，
        # 停在「未配置」状态等用户在网页里填，错误信息要能直接指路。
        if not self.cfg.password:
            self.last_error = ("未配置路由器密码 —— 打开浏览器控制台，"
                               "在「路由器连接」里填好密码即可开始同步")
            return False

        now = time.time()
        if now < self.next_login_at:
            left = int(self.next_login_at - now)
            log.debug("登录退避中，还需 %ds", left)
            return False

        try:
            info = self.router.login()
            self.login_fail_count = 0
            self.next_login_at = 0.0
            log.info("登录成功：user=%s level=%s（服务端签名已校验）",
                     info["username"], info["level"])
            return True
        except AuthFailed as e:
            self.login_fail_count += 1
            delay = min(self.cfg.login_backoff_base * (2 ** (self.login_fail_count - 1)),
                        self.cfg.login_backoff_max)
            self.next_login_at = time.time() + delay
            self.last_error = f"登录失败：{e}"
            log.error("登录失败（第 %d 次）：%s", self.login_fail_count, e)
            log.error("已退避 %d 秒后再试 —— 请勿快速重试，设备允许的失败次数只有 3 次",
                      delay)
            return False
        except RouterError as e:
            self.last_error = f"登录异常：{e}"
            self.next_login_at = time.time() + self.cfg.net_retry_delay
            log.error("登录异常：%s", e)
            return False

    # ---------------- 目标地址发现（按规则绑定方式分流，无开关） ----------------

    def _host_map(self) -> dict:
        """把 HostInfo 按 MAC（规范化小写）建索引；一次请求供所有设备规则共用。"""
        by_mac: dict[str, dict] = {}
        for h in self.router.get("HostInfo") or []:
            mac = discover.norm_mac(h.get("MACAddress") or "")
            if mac and mac not in by_mac:
                by_mac[mac] = h
        return by_mac

    def resolve_addrs(self, rule: Rule,
                      hosts_by_mac: dict | None = None) -> tuple[list[str], str]:
        """决定这条规则这一轮该写哪些地址，返回 (地址列表, 来源说明)。

        两条取址路径，按规则有没有绑定设备 MAC 自动分流：

        · 绑了 MAC（网页白名单页添加的规则都绑）：以路由器设备表里该设备的
          **第一条地址**为准 —— 华为的排序是「最新出现优先」，第一条就是该
          设备现役的地址，也是路由器自己选设备时会填的那条。设备离线、或
          表里没有它的记录时本轮跳过，白名单保持不动。
          该 MAC 恰好是本机（NAS 自己的规则）时再加一道幽灵校验：路由器的
          设备表是持久档案，可能残留已经废弃的旧地址，而本机就趴在跟前，
          /proc 的 flags 结果（deprecated/tentative 已过滤）是免费的真值。

        · 没绑 MAC（环境变量来的老规则）：读本机网卡 —— 容器用 host 网络时
          「本机接口」就是 NAS 自己，那上面的地址是报文真正能落地的地方。
          temporary 只降排序不过滤，deprecated / tentative 已被丢弃。
        """
        if rule.mac:
            return self._resolve_from_router(rule, hosts_by_mac or {})

        addrs = discover.local_candidates()
        if not addrs:
            return [], "本机网卡上未发现可用的全局 IPv6"

        origin = f"本机网卡 {len(addrs)} 个地址"

        # 隐私临时地址排到后面：它过期就会换，稳定地址（SLAAC / DHCPv6）更耐用。
        # 只影响写入顺序（决定谁占基础条目名 NAS），**不做过滤** —— 临时地址
        # 同样要写，报文完全可能落在它上面。
        try:
            temp = discover.temporary_set()
        except OSError as e:  # noqa: BLE001
            log.debug("读本机 flags 失败，跳过临时地址排序：%s", e)
            temp = set()
        return discover.rank_candidates(addrs, temp_suspect=temp), origin

    def _resolve_from_router(self, rule: Rule,
                             hosts_by_mac: dict) -> tuple[list[str], str]:
        """设备规则：路由器设备表里该 MAC 的第一条地址。"""
        nm = discover.norm_mac(rule.mac)
        host = hosts_by_mac.get(nm)
        if host is None:
            return [], ("路由器设备表里没有该 MAC 的记录"
                        "（路由器刚重启、表还没重建时会这样，本轮跳过）")
        # 只有固件明确报「离线」才跳过；字段缺失（个别固件不带）不能当成离线，
        # 否则一台正常设备会因为假路由器/异形响应被永远晾着。
        if "Active" in host and not _truthy(host.get("Active")):
            return [], "设备当前离线 —— 本轮跳过，白名单保持不动"
        cands = discover.host_candidates(host)
        if not cands:
            return [], "路由器还没观察到该设备的全局 IPv6（本轮跳过）"
        # 幽灵校验：只对「规则指向的设备就是本机」的规则做 —— /proc 就在手边，
        # 路由器残留的旧地址（设备换过地址后表里还挂着的）绝不能写进白名单。
        try:
            if nm in discover.local_macs():
                local = set(discover.local_candidates())
                if local and cands[0] not in local:
                    log.warning("%s：路由器报的第一条地址 %s 不在本机网卡上"
                                "（残留旧地址），本轮跳过", rule.name, cands[0])
                    return [], "路由器第一条地址与本机网卡不符（残留旧地址），本轮跳过"
        except OSError as e:  # noqa: BLE001
            log.debug("读本机网卡信息失败，跳过幽灵校验：%s", e)
        dev_name = host.get("HostName") or host.get("ActualName") or nm
        return cands[:1], f"路由器设备表「{dev_name}」第 1 条地址"

    # ---------------- 单条规则同步 ----------------

    @staticmethod
    def _detail(origin: str, notes: list, results: list) -> str:
        parts = [origin]
        if notes:
            parts.append("；".join(notes))
        parts.append("; ".join(results))
        return " | ".join(p for p in parts if p)

    def sync_rule(self, rule: Rule, hosts_by_mac: dict | None = None) -> RuleState:
        st = self.states[rule.name]
        addrs, origin = self.resolve_addrs(rule, hosts_by_mac)
        if not addrs:
            # 取不到地址（离线/空表/幽灵地址）= 本轮跳过，属正常运行节奏
            st.update(addr=None, addrs=[], action="skip",
                      detail=origin, error="no-address")
            log.info("%s：%s", rule.name, origin)
            return st

        try:
            ports = trustlist.parse_ports(rule.port)
        except ValueError as e:
            st.update(addr=addrs[0], addrs=list(addrs), action="error", error=str(e),
                      detail="放行端口格式错误（网页保存时本应已拦截）")
            log.error("%s：%s", rule.name, e)
            return st

        notes: list[str] = []

        # 名额预算：白名单总共 32 条。先把"不属于本规则"的条目扣掉 —— 那些是
        # 用户手工加的或其它规则的，绝不能因为本规则要加地址就把它们挤掉。
        existing = trustlist.list_entries(self.router)
        ours = trustlist.managed_names_of(existing, rule.name)
        room = max(0, trustlist.MAX_ENTRIES - (len(existing) - len(ours)))
        per_addr = len(trustlist.entry_names(rule.name, ports, 0))
        fit = max(1, room // max(1, per_addr))
        if len(addrs) > fit:
            notes.append(f"白名单名额只剩 {room} 条、每个地址需 {per_addr} 条，"
                         f"只写前 {fit} 个地址（精简端口可腾出名额）")
            log.warning("%s：%s", rule.name, notes[-1])
            addrs = addrs[:fit]

        # 期望条目集合 = 地址 × 端口
        desired: list[tuple[str, str, int]] = []
        for i, a in enumerate(addrs):
            for nm, p in trustlist.entry_names(rule.name, ports, i):
                desired.append((nm, a, p))

        results: list[str] = []
        wrote = 0
        failed = 0
        n_created = 0
        n_updated = 0
        n_rejected = 0
        n_verify = 0

        def unchanged(entry, addr: str, port: int) -> bool:
            return bool(entry and discover.same_addr(entry.get("LocalIp"), addr)
                        and trustlist.same_port(entry.get("Port"), port)
                        and (entry.get("RemoteIp") or "::/0")
                        == (rule.remote_ip or "::/0"))

        if self.cfg.dry_run:
            for nm, a, p in desired:
                cur = trustlist.find_by_name(self.router, nm)
                results.append(f"{nm}: 无变化" if unchanged(cur, a, p) else
                               f"{nm}: 将写 {a}"
                               + (f" 端口 {p}" if p != trustlist.ALL_PORTS else ""))
            st.update(addr=addrs[0], addrs=list(addrs), action="dry-run",
                      detail=self._detail(origin, notes, results), error=None)
            return st

        # 先删多余的（端口缩容 / 地址减少），顺带把名额腾出来给下面要新增的条目。
        # 只删"名字看起来是本程序生成的"那些，用户手工加的条目一个都不动。
        keep = {nm for nm, _a, _p in desired}
        # 删除前先记下旧条目的地址/端口：修改日志里要能看出"删掉的是什么"
        old_by_name = {e.get("Name"): (e.get("LocalIp") or "", e.get("Port"))
                       for e in existing}
        try:
            gone = trustlist.remove_stale(self.router, rule.name, keep)
            if gone:
                results.append("已删除过期条目: " + ", ".join(gone))
                wrote += len(gone)
                # 删条目也是真的改动了设备配置，累计里要算上
                self.counters.bump(removed=len(gone))
                for nm in gone:
                    oa, op = old_by_name.get(nm, ("", ""))
                    self._log_event("removed", device=rule.name,
                                    mac=rule.mac or "", entry=nm,
                                    addr=oa, port=op)
        except RouterError as e:
            log.warning("%s：清理过期条目失败：%s", rule.name, e)

        for nm, a, p in desired:
            cur = trustlist.find_by_name(self.router, nm)
            # 地址比「规范化后的值」：路由器回读的写法（大写/前导零/未压缩）
            # 可能和 HostInfo 给的不同，但那不是「地址变了」。判成变了就会
            # 每轮重写一次白名单，而写入是 flash 操作，必须避免。
            if unchanged(cur, a, p):
                results.append(f"{nm}: 无变化")
                continue

            action, res = trustlist.upsert(self.router, nm, a, p, rule.remote_ip)
            if not trustlist.ok(res):
                failed += 1
                n_rejected += 1
                results.append(f"{nm}: 设备拒绝 {res}")
                log.error("%s：写入被设备拒绝：%s", nm, res)
                continue

            self.counters.bump(created=1 if action == "created" else 0,
                               updated=1 if action == "updated" else 0)
            wrote += 1
            n_created += action == "created"
            n_updated += action == "updated"
            # 修改日志：地址更新要带上旧地址（更新列显示 旧 → 新）
            self._log_event(action, device=rule.name, mac=rule.mac or "",
                            entry=nm, addr=a,
                            old_addr=(cur.get("LocalIp") or "")
                            if action == "updated" else "",
                            port=p, remote_ip=rule.remote_ip or "::/0")
            back = trustlist.find_by_name(self.router, nm)
            # 回读校验同样按规范化地址比：固件把地址存成另一种写法不代表没保存
            if self.cfg.verify_after_write and not (
                    back and discover.same_addr(back.get("LocalIp"), a)):
                failed += 1
                n_verify += 1
                results.append(f"{nm}: 写后回读不一致")
                log.error("%s：写后回读不一致，设备可能没真的保存：%s", nm, back)
                continue
            results.append(f"{nm}: 已{'更新' if action == 'updated' else '新增'}")

        detail = self._detail(origin, notes, results)
        if failed:
            st.update(addr=addrs[0], addrs=list(addrs), action="error", detail=detail,
                      error=("write-rejected" if n_rejected and not n_verify
                             else "verify-failed" if n_verify and not n_rejected
                             else "partial-failure"))
        elif wrote:
            st.update(addr=addrs[0], addrs=list(addrs),
                      action="created" if n_created and not n_updated else "updated",
                      detail=detail, error=None)
            # 真实写入过（含清理删除）→ 记入本轮变更，供通知推送
            lines = [r for r in results
                     if ("已更新" in r or "已新增" in r or "已删除" in r
                         or "拒绝" in r or "不一致" in r)]
            if lines:
                head = "; ".join(lines[:6]) + (" …" if len(lines) > 6 else "")
                self.tick_changes.append(
                    f"{rule.name}（{', '.join(addrs)}）：{head}")
        else:
            st.update(addr=addrs[0], addrs=list(addrs), action="unchanged",
                      detail=detail, error=None)
        log.info("%s：%s", rule.name, "; ".join(results))
        return st

    # ---------------- 一轮 ----------------

    def tick(self) -> dict:
        """跑一轮。与网页控制台的「改设置 / 立即同步」互斥。"""
        with self.lock:
            try:
                return self._tick()
            finally:
                # 一轮只落一次盘：counters.bump 只改内存，
                # 否则一轮写 N 个地址就要写 N 次状态文件。
                self.counters.flush()

    def _tick(self) -> dict:
        self.last_tick_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.tick_changes = []
        try:
            if not self.ensure_login():
                self.last_ok = False
                if not self.cfg.password:
                    # 未配置密码是「等用户填」，不是故障 —— 不累计失败，
                    # 否则健康检查永远 degraded，网页上也一直显示失败 N 次。
                    self.consecutive_failures = 0
                else:
                    self.consecutive_failures += 1
                return self.snapshot(ok=False)

            # IPv6 防火墙总开关（默认不动用户的配置）
            enabled = trustlist.is_enabled(self.router)
            self.firewall_on = enabled
            # 开关在路由器端被人为改动（不是网页、也不是自动打开）→ 记一条。
            # 首轮只记基准不记事件，避免每次重启都多一条无意义记录。
            if self._fw_seen is not None and enabled != self._fw_seen:
                self._log_event("fw", trigger="路由器端",
                                device="（全局）", entry="—",
                                addr="总开关已开启" if enabled
                                else "总开关已关闭（进入待机）", port="")
            self._fw_seen = enabled
            if not enabled:
                if self.cfg.ensure_firewall_on:
                    log.warning("IPv6 防火墙当前是关闭的，白名单不会生效 —— 正在打开")
                    trustlist.set_enabled(self.router, True)
                    self.firewall_on = True
                    self.tick_changes.append("IPv6 防火墙总开关已自动打开")
                    self._log_event("fw", trigger="自动打开",
                                    device="（全局）", entry="—",
                                    addr="总开关已自动打开（ENSURE_FIREWALL_ON）",
                                    port="")
                    self._fw_seen = True
                else:
                    # 总开关关闭 = 白名单整体不生效。本轮只观察不写入：
                    # 地址照旧在变，写上去的条目却不起作用，纯属浪费 flash。
                    # 开关重新打开后，下一轮自动恢复同步并把地址追平。
                    log.warning("IPv6 防火墙当前是关闭的 —— 本轮跳过同步"
                                "（如需自动打开请设 ENSURE_FIREWALL_ON=true）")
                    self.consecutive_failures = 0
                    self.last_error = None
                    self.last_ok = True
                    self.last_success_at = self.last_tick_at
                    return self.snapshot(ok=True)

            # 设备表一次拉取、所有设备规则共用（每规则各拉一次太浪费）
            hosts_by_mac = self._host_map()
            for rule in self.cfg.rules:
                try:
                    self.sync_rule(rule, hosts_by_mac)
                except (NotLoggedIn, SessionExpired) as e:
                    log.warning("会话在同步 %s 时失效，稍后重登：%s", rule.name, e)
                    raise
                except RouterError as e:
                    self.states[rule.name].update(action="error", error=str(e),
                                                  detail="路由器交互失败")
                    log.error("%s 同步失败：%s", rule.name, e)

            self._notify_changes()
            self.consecutive_failures = 0
            self.last_error = None
            self.last_ok = True
            self.last_success_at = self.last_tick_at
            return self.snapshot(ok=True)

        except (NotLoggedIn, SessionExpired) as e:
            self.consecutive_failures += 1
            self.last_error = str(e)
            log.error("会话失效：%s", e)
            return self.snapshot(ok=False)
        except (NetworkError, AuthFailed) as e:
            self.consecutive_failures += 1
            self.last_error = str(e)
            log.error("本轮同步失败：%s", e)
            return self.snapshot(ok=False)
        except RouterError as e:
            self.consecutive_failures += 1
            self.last_error = str(e)
            log.error("路由器错误：%s", e)
            return self.snapshot(ok=False)
        except Exception as e:  # noqa: BLE001
            self.consecutive_failures += 1
            self.last_error = f"{type(e).__name__}: {e}"
            log.exception("本轮同步出现未预期错误")
            return self.snapshot(ok=False)

    def _notify_changes(self) -> None:
        """本轮真的改动了防火墙配置（写条目 / 删条目 / 开总开关）就推送通知。

        通知失败只记日志，绝不影响同步结果与下一轮轮询。
        """
        changes = list(self.tick_changes)
        if not changes or self.cfg.dry_run:
            return
        title = (self.cfg.notify_title or "IPv6 白名单已更新")
        results = notify.send_all(self.cfg, title, "\n".join(changes))
        for ch, ok, err in results:
            if ok:
                log.info("变更通知已发送（%s）：%d 项变更", ch, len(changes))
            else:
                log.warning("变更通知发送失败（%s）：%s", ch, err)

    @property
    def total_writes(self) -> int:
        """累计写入条目数（新增 + 更新，跨容器重启）。日志与兼容旧调用用。"""
        return self.counters.total_writes

    def snapshot(self, ok: bool | None = None) -> dict:
        """给健康检查 / 网页用的状态快照（加锁，避免读到改配置的中间态）。

        `ok` 不传 = 「最近一轮的结果」；只在还没跑过任何一轮时才留 None。
        网页是轮询取状态的，永远赶不上某一轮，所以这里必须回退到留在对象上的
        结果 —— 否则前端会把正常运行的实例一直显示成「等待首轮」。
        """
        with self.lock:
            return self._snapshot(ok)

    def _snapshot(self, ok: bool | None = None) -> dict:
        if ok is None:
            ok = self.last_ok
        data = {
            "ok": ok,
            "started_at": self.started_at,
            "last_tick_at": self.last_tick_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
            "consecutive_failures": self.consecutive_failures,
            "total_writes": self.total_writes,
            "counters": self.counters.snapshot(),
            "logged_in": bool(getattr(self.router, "logged_in", False)),
            "unconfigured": not self.cfg.password and not getattr(
                self.router, "logged_in", False),
            "firewall_ipv6_enabled": self.firewall_on,
            "dry_run": self.cfg.dry_run,
            "rules": {k: dataclasses.asdict(v) for k, v in self.states.items()},
        }
        return data
