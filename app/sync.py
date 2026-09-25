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

from . import discover, notify, state, trustlist
from .config import Config, Rule
from .router import (AuthFailed, NetworkError, NotLoggedIn, RouterError,
                     SessionExpired)

log = logging.getLogger("ipv6sync.sync")


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

    # ---------------- 目标地址发现（固定流程，无分支） ----------------

    def resolve_addrs(self, rule: Rule) -> tuple[list[str], str]:
        """读本机网卡上的全局 IPv6，返回 (地址列表, 来源说明)。

        这是本程序唯一一条取址路径。容器用 host 网络时「本机接口」就是 NAS
        自己的网卡 —— 那上面的地址才是外部报文真正能落地的地方，也是唯一的
        地面真值。路由器设备表（HostInfo）**不参与**：它本质是邻居表缓存，
        更新有延迟，还会留着设备已经不用了的旧地址。

        返回的列表可能为空（网卡上没有可用的全局地址）。
        """
        addrs = discover.local_candidates()
        if not addrs:
            return [], "本机网卡上未发现可用的全局 IPv6"

        origin = f"本机网卡 {len(addrs)} 个地址"

        # 配了 TARGET_MAC 就核对一下。MAC 不在本机任何网卡上，说明这条规则
        # 想管的是**别的设备**，而本程序只会写本机地址 —— 写上去就是错误的
        # 放行目标。只告警不中断：部署约定就是"只同步本机"，改不改 MAC 是
        # 用户的事，但必须让他看见。
        if rule.mac:
            nm = discover.norm_mac(rule.mac)
            macs = discover.local_macs()
            if macs and nm and nm not in macs:
                origin += "（注意：TARGET_MAC 不是本机网卡）"
                log.warning(
                    "%s：配置的 TARGET_MAC=%s 不在本机网卡 %s 上，仍按本机地址"
                    "写入。若这条规则本意是给另一台设备放行，请把 TARGET_MAC"
                    "改成这台机器的", rule.name, rule.mac, sorted(macs))

        # 隐私临时地址排到后面：它过期就会换，稳定地址（SLAAC / DHCPv6）更耐用。
        # 只影响写入顺序（决定谁占基础条目名 NAS），**不做过滤** —— 临时地址
        # 同样要写，报文完全可能落在它上面。
        try:
            temp = discover.temporary_set()
        except OSError as e:  # noqa: BLE001
            log.debug("读本机 flags 失败，跳过临时地址排序：%s", e)
            temp = set()
        return discover.rank_candidates(addrs, temp_suspect=temp), origin

    # ---------------- 单条规则同步 ----------------

    @staticmethod
    def _detail(origin: str, notes: list, results: list) -> str:
        parts = [origin]
        if notes:
            parts.append("；".join(notes))
        parts.append("; ".join(results))
        return " | ".join(p for p in parts if p)

    def sync_rule(self, rule: Rule) -> RuleState:
        st = self.states[rule.name]
        addrs, origin = self.resolve_addrs(rule)
        if not addrs:
            st.update(addr=None, addrs=[], action="skip",
                      detail=origin, error="no-address")
            log.warning("%s：%s —— 容器是 host 网络吗？", rule.name, origin)
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
        try:
            gone = trustlist.remove_stale(self.router, rule.name, keep)
            if gone:
                results.append("已删除过期条目: " + ", ".join(gone))
                wrote += len(gone)
                # 删条目也是真的改动了设备配置，累计里要算上
                self.counters.bump(removed=len(gone))
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
            if not enabled:
                if self.cfg.ensure_firewall_on:
                    log.warning("IPv6 防火墙当前是关闭的，白名单不会生效 —— 正在打开")
                    trustlist.set_enabled(self.router, True)
                    self.firewall_on = True
                    self.tick_changes.append("IPv6 防火墙总开关已自动打开")
                else:
                    log.warning("IPv6 防火墙当前是关闭的，白名单不会生效"
                                "（如需自动打开请设 ENSURE_FIREWALL_ON=true）")

            for rule in self.cfg.rules:
                try:
                    self.sync_rule(rule)
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
