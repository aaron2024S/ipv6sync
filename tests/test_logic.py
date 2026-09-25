# -*- coding: utf-8 -*-
"""
离线单元测试：用假路由器验证「发现 → 比对 → 写入」的核心逻辑。
不联网、不需要真实设备，可在任何装了 Python 3.9+ 的机器上跑：

    python -m unittest discover -s tests -v
"""
import itertools
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import discover, trustlist  # noqa: E402
from app.config import Config, Rule  # noqa: E402
from app.sync import Syncer  # noqa: E402

NAS_MAC = "00:11:22:AA:BB:CC"
OLD_ADDR = "240e:3a4:48ff:6d10:7173:1ffc:64ed:82c0"
NEW_ADDR = "240e:3a5:483b:2f00:7173:1ffc:64ed:82c0"
# OLD_ADDR 的「固件风格」等价写法：全大写 + 补齐前导零 + 不压缩。
# 用来模拟路由器回读白名单时返回的格式与 HostInfo 不一致的情形。
OLD_ADDR_UGLY = "240E:03A4:48FF:6D10:7173:1FFC:64ED:82C0"

# 同一台设备上的第二个/第三个全局 IPv6：前缀相同，只有后 64 位不同。
# 现场就是这种情形 —— HostInfo 给的顺序里可能有不生效的那一个。
SEC_ADDR = "240e:3a5:483b:2f00:4006:3465:8422:000f"
THIRD_ADDR = "240e:3a5:483b:2f00:dead:beef:cafe:0001"


def C(a):
    """地址的规范形式。canon() 会把 `:000f` 压缩成 `:f`、大写转小写，
    所以断言里一律用它 —— 直接比原始常量会因为写法差异假失败。"""
    return discover.canon(a)


class FakeRouter:
    """最小可用的假路由器：只实现本程序用到的几个接口。"""

    def __init__(self, enabled=False, entries=None, hosts=None):
        self.logged_in = True
        self.username = "admin"
        self.level = 2
        self.writes = []
        self.enabled = enabled
        self.entries = list(entries or [])
        self.hosts = hosts or [{
            "MACAddress": NAS_MAC,
            "HostName": "DiskStation",
            "IPAddress": "192.168.3.5",
            "IPv6Address": "fe80::211:22ff:feaa:bbcc",
            "Ipv6Addrs": [{"Ipv6Addr": OLD_ADDR}],
        }]

    # --- 客户端接口 ---
    def check_session(self):
        return True

    def login(self, password=None):
        self.logged_in = True
        return {"username": "admin", "level": 2, "server_signature_verified": True}

    def get(self, name, params=None):
        if name == "ip6firewall_enable":
            return {"Enable": self.enabled}
        if name == "ip6firewall_trustlist":
            return [dict(e) for e in self.entries]
        if name == "HostInfo":
            return self.hosts
        raise AssertionError(f"假路由器没实现 GET {name}")

    def post(self, name, data=None, wrapped=True, action=None):
        self.writes.append({"name": name, "action": action, "data": data})
        if name == "ip6firewall_enable":
            self.enabled = bool((data or {}).get("Enable"))
            return {}
        if name != "ip6firewall_trustlist":
            raise AssertionError(f"假路由器没实现 POST {name}")

        if action == "create":
            item = dict(data)
            item["ID"] = f"InternetGatewayDevice.X_FireWall.Ipv6.Trustlist.{len(self.entries) + 1}."
            self.entries.append(item)
            return {}
        if action == "update":
            for i, e in enumerate(self.entries):
                if e.get("ID") == data.get("ID"):
                    self.entries[i] = dict(data)
                    return {}
            return {"errcode": 1, "errmsg": "not found"}
        if action == "delete":
            before = len(self.entries)
            # 固件 delete 用小写 ip 键：按 ID 删即可
            self.entries = [e for e in self.entries if e.get("ID") != data.get("ID")]
            return {} if len(self.entries) < before else {"errcode": 1}
        raise AssertionError(f"未知 action {action}")


# 累计计数文件的落盘位置。必须隔离：Config 的默认值是 /data/state.json，
# 而在 Windows 上它会被解析成<当前盘>:\data\state.json —— 测试真跑出写入就会
# 往盘根建目录写文件。这里给每个用例发一个独立文件，顺带避免用例之间串味。
_STATE_DIR = tempfile.mkdtemp(prefix="ipv6sync-test-state-")
_state_seq = itertools.count(1)


def make_cfg(**kw) -> Config:
    cfg = Config(
        host="192.168.3.1", username="admin", password="x",
        session_file=None, poll_interval=1,
        rules=[Rule(name="NAS", port=None, remote_ip="::/0", mac=NAS_MAC)],
        state_file=os.path.join(_STATE_DIR, f"state-{next(_state_seq)}.json"),
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


class LocalNetStub:
    """给「本机网卡」装上假数据。

    取址流程现在**固定读本机网卡**（/proc/net/if_inet6、/sys/class/net），
    而测试机（Windows、多数 CI）上这些路径不存在 —— 不装这个桩，同步路径
    根本走不到，用例会集体静默变成 "skip"。
    """

    LOCAL_ADDRS = [OLD_ADDR]           # 默认：网卡上就一个全局地址

    def setUp(self):
        self._net_saved = (discover.local_candidates, discover.local_macs,
                           discover.temporary_set)
        self.patch_local(self.LOCAL_ADDRS, {discover.norm_mac(NAS_MAC)})

    def tearDown(self):
        (discover.local_candidates, discover.local_macs,
         discover.temporary_set) = self._net_saved

    def patch_local(self, addrs, macs=None, temp=None):
        """改写「网卡上有什么」。不需要手动还原 —— tearDown 统一恢复。"""
        discover.local_candidates = (
            lambda: [discover.canon(a) or a for a in addrs])
        discover.temporary_set = lambda: set(temp or ())
        if macs is not None:
            discover.local_macs = lambda: set(macs)


class TestAddressHelpers(unittest.TestCase):
    def test_canon(self):
        self.assertEqual(discover.canon("240E:3A4:48FF:6D10::82C0"),
                         "240e:3a4:48ff:6d10::82c0")
        self.assertEqual(discover.canon("240e:3a4::1/64"), "240e:3a4::1")
        self.assertEqual(discover.canon("fe80::1%eth0"), "fe80::1")
        self.assertIsNone(discover.canon("not-an-ip"))
        self.assertIsNone(discover.canon(""))

    def test_is_usable_filters(self):
        self.assertTrue(discover.is_usable(OLD_ADDR))
        self.assertFalse(discover.is_usable("fe80::1"))         # 链路本地
        self.assertFalse(discover.is_usable("::1"))             # 回环
        self.assertFalse(discover.is_usable("ff02::1"))         # 组播
        self.assertFalse(discover.is_usable("::ffff:192.168.1.1"))  # v4 映射
        self.assertFalse(discover.is_usable("fc00::1"))         # ULA

    def test_norm_mac(self):
        self.assertEqual(discover.norm_mac("00:11:22:AA:bb:CC"), "001122aabbcc")
        self.assertEqual(discover.norm_mac("00-11-22-aa-bb-cc"), "001122aabbcc")

    def test_rank_puts_temporary_last_without_dropping(self):
        """只有一条排序规则：隐私临时地址靠后；顺序变了但一个都不丢。"""
        stable = "240e:3a5:483b:2f00:aaaa:bbbb:cccc:dddd"
        ranked = discover.rank_candidates([NEW_ADDR, stable],
                                          temp_suspect={NEW_ADDR})
        self.assertEqual(ranked, [stable, NEW_ADDR])
        self.assertEqual(sorted(ranked), sorted([NEW_ADDR, stable]))
        # 没标出临时地址时保持原本的来源顺序（稳定排序）
        self.assertEqual(discover.rank_candidates([NEW_ADDR, stable]),
                         [NEW_ADDR, stable])
        self.assertEqual(discover.rank_candidates([], temp_suspect=set()), [])

    def test_same_addr_ignores_formatting(self):
        """写法不同但地址相同要判等 —— 否则每轮都会白写一次闪存。"""
        self.assertTrue(discover.same_addr(OLD_ADDR, OLD_ADDR_UGLY))
        self.assertTrue(discover.same_addr(OLD_ADDR_UGLY, OLD_ADDR))
        self.assertTrue(discover.same_addr(OLD_ADDR, OLD_ADDR + "/64"))
        self.assertTrue(discover.same_addr(OLD_ADDR, OLD_ADDR))
        self.assertFalse(discover.same_addr(OLD_ADDR, NEW_ADDR))

    def test_same_addr_never_conflates_empty(self):
        """空值/非法值不能被判成与某个真实地址相同（否则该写的就不写了）。"""
        self.assertFalse(discover.same_addr("", OLD_ADDR))
        self.assertFalse(discover.same_addr(None, OLD_ADDR))
        self.assertFalse(discover.same_addr("not-an-ip", OLD_ADDR))


class TestRouterDeviceList(unittest.TestCase):
    """HostInfo 只用来给控制台的「在线设备」页列 MAC / IPv6，不参与取址。"""

    def test_host_candidates_reads_ipv6addrs(self):
        host = {"IPv6Address": "fe80::1",
                "Ipv6Addrs": [{"Ipv6Addr": OLD_ADDR}, {"Ipv6Addr": "fe80::9"}]}
        self.assertEqual(discover.host_candidates(host), [OLD_ADDR])


class TestTrustlistCrud(unittest.TestCase):
    def test_add_then_upsert_unchanged(self):
        r = FakeRouter()
        self.assertEqual(trustlist.upsert(r, "NAS", OLD_ADDR, None, "::/0")[0], "created")
        self.assertEqual(len(r.entries), 1)
        self.assertEqual(trustlist.upsert(r, "NAS", OLD_ADDR, None, "::/0")[0], "unchanged")
        self.assertEqual(len(r.entries), 1)

    def test_upsert_updates_in_place(self):
        r = FakeRouter()
        trustlist.upsert(r, "NAS", OLD_ADDR, None, "::/0")
        action, _ = trustlist.upsert(r, "NAS", NEW_ADDR, None, "::/0")
        self.assertEqual(action, "updated")
        self.assertEqual(len(r.entries), 1)
        self.assertEqual(r.entries[0]["LocalIp"], NEW_ADDR)

    def test_upsert_detects_port_change(self):
        r = FakeRouter()
        trustlist.upsert(r, "NAS", OLD_ADDR, 16669, "::/0")
        self.assertEqual(trustlist.upsert(r, "NAS", OLD_ADDR, 16669, "::/0")[0], "unchanged")
        self.assertEqual(trustlist.upsert(r, "NAS", OLD_ADDR, 80, "::/0")[0], "updated")

    def test_upsert_unchanged_across_formatting(self):
        """固件存的写法与本次传入不同、但地址相同 → unchanged，且一次都不写。"""
        r = FakeRouter(entries=[{"Name": "NAS", "LocalIp": OLD_ADDR_UGLY,
                                 "RemoteIp": "::/0", "Port": -1, "ID": "id1"}])
        action, _ = trustlist.upsert(r, "NAS", OLD_ADDR, None, "::/0")
        self.assertEqual(action, "unchanged")
        self.assertEqual(r.writes, [])

    def test_port_none_becomes_all_ports(self):
        r = FakeRouter()
        trustlist.add(r, "NAS", OLD_ADDR, None, "::/0")
        self.assertEqual(r.entries[0]["Port"], -1)
        trustlist.add(r, "NAS2", OLD_ADDR, "all", "::/0")
        self.assertEqual(r.entries[1]["Port"], -1)

    def test_max_entries_guard(self):
        r = FakeRouter(entries=[{"Name": f"e{i}", "ID": f"id{i}"}
                                for i in range(trustlist.MAX_ENTRIES)])
        with self.assertRaises(Exception):
            trustlist.upsert(r, "NAS", OLD_ADDR, None, "::/0")

    def test_delete_by_id(self):
        r = FakeRouter()
        trustlist.upsert(r, "NAS", OLD_ADDR, None, "::/0")
        entry = trustlist.find_by_name(r, "NAS")
        trustlist.delete(r, entry)
        self.assertEqual(r.entries, [])

    def test_ok_helper(self):
        self.assertTrue(trustlist.ok({}))
        self.assertTrue(trustlist.ok({"errcode": 0}))
        self.assertFalse(trustlist.ok({"errcode": 9003}))
        self.assertTrue(trustlist.ok(None))


class TestSyncer(LocalNetStub, unittest.TestCase):
    def test_creates_entry_when_missing(self):
        r = FakeRouter(entries=[])
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["rules"]["NAS"]["action"], "created")
        self.assertEqual(r.entries[0]["LocalIp"], OLD_ADDR)
        self.assertEqual(r.entries[0]["Name"], "NAS")

    def test_unchanged_when_address_same(self):
        r = FakeRouter(entries=[{"Name": "NAS", "LocalIp": OLD_ADDR,
                                 "RemoteIp": "::/0", "Port": -1, "ID": "id1"}])
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertEqual(snap["rules"]["NAS"]["action"], "unchanged")
        self.assertEqual(r.writes, [])          # 没有任何写操作

    def test_unchanged_when_router_formats_address_differently(self):
        """回归：路由器回读的地址写法与 HostInfo 不同（大写/未压缩），
        但地址其实没变 —— 必须判为无变化。

        以前的实现直接比字符串，这种固件会让每一轮轮询都误判成「变了」并
        重写一次白名单；白名单写入是 flash 操作，长期反复写毫无意义还伤寿命。
        """
        r = FakeRouter(entries=[{"Name": "NAS", "LocalIp": OLD_ADDR_UGLY,
                                 "RemoteIp": "::/0", "Port": -1, "ID": "id1"}])
        s = Syncer(make_cfg(), r)
        for _ in range(3):                      # 连跑三轮，一轮都不能写
            snap = s.tick()
            self.assertEqual(snap["rules"]["NAS"]["action"], "unchanged")
        self.assertEqual(r.writes, [])
        self.assertEqual(s.total_writes, 0)
        self.assertEqual(r.entries[0]["LocalIp"], OLD_ADDR_UGLY)   # 原文没被动过

    def test_follows_ipv6_change(self):
        """核心场景：本机网卡的 IPv6 变了 → 白名单原地更新。"""
        r = FakeRouter(entries=[{"Name": "NAS", "LocalIp": OLD_ADDR,
                                 "RemoteIp": "::/0", "Port": -1, "ID": "id1"}])
        self.patch_local([NEW_ADDR])            # 网卡上现在只剩新地址了
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertEqual(snap["rules"]["NAS"]["action"], "updated")
        self.assertEqual(len(r.entries), 1)                 # 没有新增条目
        self.assertEqual(r.entries[0]["LocalIp"], C(NEW_ADDR))  # 原地改成新地址
        self.assertEqual(r.entries[0]["ID"], "id1")          # ID 保持不变

    def test_dry_run_never_writes(self):
        r = FakeRouter(entries=[])
        s = Syncer(make_cfg(dry_run=True), r)
        snap = s.tick()
        self.assertEqual(snap["rules"]["NAS"]["action"], "dry-run")
        self.assertEqual(r.writes, [])

    def test_no_address_found_is_skipped_not_crashed(self):
        """网卡上没有可用的全局 IPv6（例如容器没走 host 网络）→ 跳过并说清楚。
        绝不能猜一个地址写上去。"""
        r = FakeRouter(entries=[])
        self.patch_local([])
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertEqual(snap["rules"]["NAS"]["action"], "skip")
        self.assertEqual(snap["rules"]["NAS"]["error"], "no-address")
        self.assertEqual(r.writes, [])

    def test_verify_failure_is_reported(self):
        """设备 ACK 了但没真保存（写后回读不一致）必须报错，不能静默成功。"""
        class AmnesiacRouter(FakeRouter):
            def post(self, name, data=None, wrapped=True, action=None):
                if name == "ip6firewall_trustlist":
                    self.writes.append({"action": action})
                    return {}          # 假装成功，但不落库
                return super().post(name, data, wrapped, action)

        r = AmnesiacRouter(entries=[])
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertEqual(snap["rules"]["NAS"]["error"], "verify-failed")

    def test_verify_accepts_equivalent_formatting(self):
        """固件把地址改写成另一种写法（大写）不等于「没保存」，
        回读校验要按规范化地址判等，不能误报 verify-failed。"""
        class NormalizingRouter(FakeRouter):
            def post(self, name, data=None, wrapped=True, action=None):
                res = super().post(name, data, wrapped, action)
                if name == "ip6firewall_trustlist" and action in ("create", "update"):
                    for e in self.entries:
                        if e.get("Name") == (data or {}).get("Name"):
                            e["LocalIp"] = str(e.get("LocalIp") or "").upper()
                return res

        r = NormalizingRouter(entries=[])
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertIsNone(snap["rules"]["NAS"]["error"])
        self.assertEqual(snap["rules"]["NAS"]["action"], "created")
        self.assertEqual(r.entries[0]["LocalIp"], OLD_ADDR.upper())   # 固件改写成了大写

    def test_mac_off_this_host_is_warned(self):
        """TARGET_MAC 不在本机网卡上 → 仍然写本机地址，但状态里必须看得见。

        部署约定就是"只同步本机"，所以这里不中断；但这几乎总是配错了对象，
        不提示的话用户会以为给 NVR 放行了、其实放行的是 NAS。
        """
        r = FakeRouter(entries=[])
        cfg = make_cfg()
        cfg.rules[0].mac = "AA:BB:CC:DD:EE:FF"           # 别台设备的 MAC
        self.patch_local([OLD_ADDR], {"001122aabbcc"})    # 本机只有 NAS 那块网卡
        st = Syncer(cfg, r).sync_rule(cfg.rules[0])
        self.assertIn("TARGET_MAC 不是本机网卡", st.detail)
        self.assertEqual(r.entries[0]["LocalIp"], C(OLD_ADDR))

    def test_ensure_firewall_on(self):
        r = FakeRouter(enabled=False)
        s = Syncer(make_cfg(ensure_firewall_on=True), r)
        s.tick()
        self.assertTrue(r.enabled)

    def test_firewall_not_touched_by_default(self):
        r = FakeRouter(enabled=False)
        s = Syncer(make_cfg(ensure_firewall_on=False), r)
        s.tick()
        self.assertFalse(r.enabled)


class TestMultiPort(LocalNetStub, unittest.TestCase):
    """多端口支持：PORT 规格解析、多条目展开、缩容清理。"""

    def test_parse_ports(self):
        self.assertEqual(trustlist.parse_ports(None), [-1])
        self.assertEqual(trustlist.parse_ports(""), [-1])
        self.assertEqual(trustlist.parse_ports("-1"), [-1])
        self.assertEqual(trustlist.parse_ports(16669), [16669])
        self.assertEqual(trustlist.parse_ports("16667"), [16667])
        self.assertEqual(trustlist.parse_ports("16667,5005,22"), [16667, 5005, 22])
        self.assertEqual(trustlist.parse_ports(" 16667 ， 5005 "), [16667, 5005])  # 中文逗号+空格
        self.assertEqual(trustlist.parse_ports("16667,16667"), [16667])           # 去重

    def test_parse_ports_rejects_bad_values(self):
        for bad in ("abc", "0", "65536", "80;443"):
            with self.assertRaises(ValueError):
                trustlist.parse_ports(bad)
        # 尾随逗号是合法的（空 token 被忽略）
        self.assertEqual(trustlist.parse_ports("16667,"), [16667])

    def test_rule_accepts_multi_port_spec(self):
        from app.config import Rule
        self.assertEqual(Rule(name="NAS", port="16667,5005").port, "16667,5005")
        self.assertEqual(Rule(name="NAS", port="16667,5005，22").port, "16667,5005,22")
        self.assertIsNone(Rule(name="NAS", port="-1").port)
        self.assertIsNone(Rule(name="NAS", port="").port)
        self.assertEqual(Rule(name="NAS", port="16667").port, 16667)

    def test_entry_names(self):
        self.assertEqual(trustlist.entry_names("NAS", [-1]), [("NAS", -1)])
        self.assertEqual(trustlist.entry_names("NAS", [16667]), [("NAS", 16667)])
        self.assertEqual(trustlist.entry_names("NAS", [16667, 5005]),
                         [("NAS", 16667), ("NAS-5005", 5005)])
        self.assertEqual(trustlist.entry_names("NAS", [16667, 5005, 22]),
                         [("NAS", 16667), ("NAS-5005", 5005), ("NAS-22", 22)])

    def test_sync_creates_entry_per_port(self):
        r = FakeRouter(entries=[])
        cfg = make_cfg()
        cfg.rules[0].port = "16667,5005"
        s = Syncer(cfg, r)
        snap = s.tick()
        self.assertTrue(snap["ok"])
        self.assertEqual({e["Name"] for e in r.entries}, {"NAS", "NAS-5005"})
        by_name = {e["Name"]: e for e in r.entries}
        self.assertEqual(by_name["NAS"]["Port"], 16667)
        self.assertEqual(by_name["NAS-5005"]["Port"], 5005)
        self.assertEqual(by_name["NAS"]["LocalIp"], OLD_ADDR)
        self.assertEqual(by_name["NAS-5005"]["LocalIp"], OLD_ADDR)

    def test_sync_removes_stale_port_entries(self):
        """端口列表缩容后，多出来的条目要被删掉，不会越积越多。"""
        r = FakeRouter(entries=[
            {"Name": "NAS", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": 16667, "ID": "id1"},
            {"Name": "NAS-5005", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": 5005, "ID": "id2"},
            {"Name": "NAS-22", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": 22, "ID": "id3"},
            {"Name": "别的设备-22", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": 22, "ID": "id4"},          # 不是本程序的名字，不能动
        ])
        cfg = make_cfg()
        cfg.rules[0].port = "16667"             # 收缩到只剩 16667
        s = Syncer(cfg, r)
        snap = s.tick()
        self.assertTrue(snap["ok"])
        names = {e["Name"] for e in r.entries}
        self.assertEqual(names, {"NAS", "别的设备-22"})   # NAS-5005 / NAS-22 被清理

    def test_multi_port_shrink_updates_and_cleans(self):
        """从多端口改到另一个多端口：改留下的、删多余的。"""
        r = FakeRouter(entries=[
            {"Name": "NAS", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": 16667, "ID": "id1"},
            {"Name": "NAS-5005", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": 5005, "ID": "id2"},
        ])
        cfg = make_cfg()
        cfg.rules[0].port = "16667,8080"
        s = Syncer(cfg, r)
        snap = s.tick()
        self.assertTrue(snap["ok"])
        by_name = {e["Name"]: e for e in r.entries}
        self.assertEqual(set(by_name), {"NAS", "NAS-8080"})
        self.assertEqual(by_name["NAS"]["Port"], 16667)     # 留下的原地更新
        self.assertEqual(by_name["NAS-8080"]["Port"], 8080)  # 新增

    def test_multi_port_unchanged_writes_nothing(self):
        r = FakeRouter(entries=[
            {"Name": "NAS", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": 16667, "ID": "id1"},
            {"Name": "NAS-5005", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": 5005, "ID": "id2"},
        ])
        cfg = make_cfg()
        cfg.rules[0].port = "16667,5005"
        s = Syncer(cfg, r)
        snap = s.tick()
        self.assertEqual(snap["rules"]["NAS"]["action"], "unchanged")
        self.assertEqual(r.writes, [])


    def test_settings_rejects_bad_port_spec(self):
        """网页保存时就要拦住坏端口规格，不能等同步引擎才炸。"""
        from app import settings as st_mod
        with self.assertRaises(st_mod.SettingsError):
            st_mod.validate_overlay({"PORT": "abc"})
        with self.assertRaises(st_mod.SettingsError):
            st_mod.validate_overlay({"PORT": "80;443"})
        with self.assertRaises(st_mod.SettingsError):
            st_mod.validate_overlay({"PORT": "0"})
        self.assertEqual(st_mod.validate_overlay({"PORT": "16667,5005"}),
                         {"PORT": "16667,5005"})
        self.assertEqual(st_mod.validate_overlay({"PORT": "-1"}), {"PORT": "-1"})


class TestMultiAddress(LocalNetStub, unittest.TestCase):
    """一台设备多个全局 IPv6 时「每个地址各写一条」的行为。

    现场背景：白名单的 LocalIp 是**目的地址匹配规则**，所以必须写"外部真会
    打到、且确实挂在该设备网卡上"的那个地址。一块网卡常同时有 SLAAC 稳定
    地址、RFC 4941 隐私临时地址、DHCPv6 地址，前缀一样、只有后 64 位不同；
    固件给的顺序不代表优先级，只写一条就是赌运气 —— 所以全写，一条都不赌。
    """

    def _router(self, addrs, entries=None, temp=None):
        """装好「网卡上有哪些地址」，再给一个空白的假路由器。"""
        self.patch_local(addrs, {discover.norm_mac(NAS_MAC)}, temp=temp)
        return FakeRouter(entries=entries or [])

    def test_entry_names_multi_address(self):
        self.assertEqual(trustlist.entry_names("NAS", [-1], 0), [("NAS", -1)])
        self.assertEqual(trustlist.entry_names("NAS", [-1], 1), [("NAS@2", -1)])
        self.assertEqual(trustlist.entry_names("NAS", [16667, 5005], 1),
                         [("NAS@2", 16667), ("NAS@2-5005", 5005)])
        # 第 0 个地址必须沿用基础名，否则升级后会把老条目白占一个新名额
        self.assertEqual(trustlist.entry_names("NAS", [16667], 0), [("NAS", 16667)])

    def test_managed_names_of(self):
        entries = [{"Name": "NAS"}, {"Name": "NAS@2"}, {"Name": "NAS@2-5005"},
                   {"Name": "NAS-5005"}, {"Name": "NAS2"}, {"Name": "NAS-备用"},
                   {"Name": "别动我"}, {"Name": ""}]
        self.assertEqual(trustlist.managed_names_of(entries, "NAS"),
                         ["NAS", "NAS@2", "NAS@2-5005", "NAS-5005"])

    def test_creates_entry_per_address(self):
        r = self._router([OLD_ADDR, SEC_ADDR])
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertTrue(snap["ok"])
        by_name = {e["Name"]: e for e in r.entries}
        self.assertEqual(set(by_name), {"NAS", "NAS@2"})
        self.assertTrue(discover.same_addr(by_name["NAS"]["LocalIp"], OLD_ADDR))
        self.assertTrue(discover.same_addr(by_name["NAS@2"]["LocalIp"], SEC_ADDR))
        st = snap["rules"]["NAS"]
        self.assertEqual(st["addrs"], [C(OLD_ADDR), C(SEC_ADDR)])
        self.assertEqual(st["addr"], C(OLD_ADDR))       # 主地址 = 列表第一个

    def test_multi_address_unchanged_second_round(self):
        """两个地址都已写好 → 第二轮一个字节都不写（写入是 flash 操作）。"""
        r = self._router([OLD_ADDR, SEC_ADDR])
        s = Syncer(make_cfg(), r)
        s.tick()
        r.writes.clear()
        snap = s.tick()
        self.assertEqual(snap["rules"]["NAS"]["action"], "unchanged")
        self.assertEqual(r.writes, [])

    def test_multi_port_expands_grid(self):
        """地址 × 端口 的笛卡尔积：2 个地址 × 2 个端口 = 4 条。"""
        r = self._router([OLD_ADDR, SEC_ADDR])
        cfg = make_cfg()
        cfg.rules[0].port = "16667,5005"
        s = Syncer(cfg, r)
        s.tick()
        self.assertEqual({e["Name"] for e in r.entries},
                         {"NAS", "NAS-5005", "NAS@2", "NAS@2-5005"})
        by_name = {e["Name"]: e for e in r.entries}
        self.assertTrue(discover.same_addr(by_name["NAS@2-5005"]["LocalIp"], SEC_ADDR))
        self.assertEqual(by_name["NAS@2-5005"]["Port"], 5005)

    def test_address_reduction_removes_stale_entry(self):
        """设备地址减少（旧地址过期）→ 对应条目要删掉，不能留着占名额。"""
        r = self._router([OLD_ADDR], entries=[
            {"Name": "NAS", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": -1, "ID": "id1"},
            {"Name": "NAS@2", "LocalIp": SEC_ADDR, "RemoteIp": "::/0",
             "Port": -1, "ID": "id2"},
        ])
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertTrue(snap["ok"])
        self.assertEqual({e["Name"] for e in r.entries}, {"NAS"})

    def test_cleanup_ignores_user_named_entries(self):
        """只删本程序命名的条目，用户手工加的（哪怕名字很像）一个都不能动。"""
        r = self._router([OLD_ADDR], entries=[
            {"Name": "NAS", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
             "Port": -1, "ID": "id1"},
            {"Name": "NAS@2", "LocalIp": SEC_ADDR, "RemoteIp": "::/0",
             "Port": -1, "ID": "id2"},          # 本程序命名，该删
            {"Name": "NAS2", "LocalIp": SEC_ADDR, "RemoteIp": "::/0",
             "Port": -1, "ID": "id3"},          # 用户命名，不能删
            {"Name": "NAS-备用", "LocalIp": SEC_ADDR, "RemoteIp": "::/0",
             "Port": -1, "ID": "id4"},          # 用户命名，不能删
            {"Name": "NAS@备用", "LocalIp": SEC_ADDR, "RemoteIp": "::/0",
             "Port": -1, "ID": "id5"},          # @ 后不是数字 → 用户命名
        ])
        s = Syncer(make_cfg(), r)
        s.tick()
        self.assertEqual({e["Name"] for e in r.entries},
                         {"NAS", "NAS2", "NAS-备用", "NAS@备用"})

    def test_temporary_addr_gets_base_name_last(self):
        """隐私临时地址排到后面 —— 只影响谁占基础条目名 NAS，不影响写不写。

        临时地址过期就会换，让它当"主地址"会让白名单里的 NAS 条目频繁改名；
        但**它照样要写**，报文完全可能落在它上面。
        """
        r = self._router([SEC_ADDR, OLD_ADDR], temp={C(SEC_ADDR)})
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        by_name = {e["Name"]: e for e in r.entries}
        self.assertEqual(set(by_name), {"NAS", "NAS@2"})
        self.assertTrue(discover.same_addr(by_name["NAS"]["LocalIp"], OLD_ADDR))
        self.assertTrue(discover.same_addr(by_name["NAS@2"]["LocalIp"], SEC_ADDR))
        self.assertEqual(snap["rules"]["NAS"]["addr"], C(OLD_ADDR))

    def test_writes_every_nic_address(self):
        """核心场景：网卡上有几个全局地址就写几条，一条都不漏。

        路由器设备表记的是 THIRD_ADDR（网卡上早就没了）、也不知道有 SEC_ADDR；
        网卡上现在是 OLD_ADDR + SEC_ADDR。白名单按目的地址匹配，只有网卡上
        真有的地址才收得到包 —— 所以按网卡写，路由器记了什么完全不影响。
        """
        r = self._router([OLD_ADDR, SEC_ADDR])
        cfg = make_cfg()
        st = Syncer(cfg, r).sync_rule(cfg.rules[0])
        self.assertEqual(st.addrs, [C(OLD_ADDR), C(SEC_ADDR)])
        self.assertIn("本机网卡", st.detail)
        self.assertEqual({e["Name"] for e in r.entries}, {"NAS", "NAS@2"})
        addrs = sorted(e["LocalIp"] for e in r.entries)
        self.assertEqual(addrs, sorted([C(OLD_ADDR), C(SEC_ADDR)]))

    def test_room_budget_limits_addresses(self):
        """白名单一共 32 条：只剩 1 个位置时只能写 1 个地址，并给出告警。
        用户手工加的 31 条绝不能被挤掉。"""
        others = [{"Name": f"用户{i}", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
                   "Port": -1, "ID": f"o{i}"} for i in range(31)]
        r = self._router([OLD_ADDR, SEC_ADDR], entries=others)
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        self.assertTrue(snap["ok"])
        names = {e["Name"] for e in r.entries}
        self.assertEqual(len(r.entries), 32)
        self.assertEqual(names - {f"用户{i}" for i in range(31)}, {"NAS"})
        self.assertIn("名额", snap["rules"]["NAS"]["detail"])

    def test_room_budget_counts_own_entries_as_reusable(self):
        """名额预算里，本程序已有的条目算"可复用"，不该把自己挤掉。"""
        mine = [{"Name": "NAS", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
                 "Port": -1, "ID": "id1"},
                {"Name": "NAS@2", "LocalIp": SEC_ADDR, "RemoteIp": "::/0",
                 "Port": -1, "ID": "id2"}]
        others = [{"Name": f"用户{i}", "LocalIp": OLD_ADDR, "RemoteIp": "::/0",
                   "Port": -1, "ID": f"o{i}"} for i in range(30)]
        r = self._router([OLD_ADDR, SEC_ADDR], entries=mine + others)
        s = Syncer(make_cfg(), r)
        snap = s.tick()
        # 30 + 2 = 32，已是满的，但两条都是自己的 → 原地更新即可，不该被截断
        self.assertEqual(snap["rules"]["NAS"]["action"], "unchanged")
        self.assertEqual(len(r.entries), 32)

    def test_dry_run_lists_every_address(self):
        r = self._router([OLD_ADDR, SEC_ADDR])
        s = Syncer(make_cfg(dry_run=True), r)
        snap = s.tick()
        st = snap["rules"]["NAS"]
        self.assertEqual(st["action"], "dry-run")
        self.assertEqual(len(st["addrs"]), 2)
        self.assertIn("将写", st["detail"])
        self.assertEqual(r.writes, [])


class TestConfig(unittest.TestCase):
    def test_env_parsing(self):
        env = {
            "ROUTER_HOST": "10.0.0.1", "ROUTER_PASSWORD": "secret",
            "ENTRY_NAME": "MyNAS", "ENTRY_PORT": "16669", "TARGET_MAC": NAS_MAC,
            "POLL_INTERVAL": "30",
        }
        saved = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        try:
            from app.config import load_config
            cfg = load_config([])
            self.assertEqual(cfg.host, "10.0.0.1")
            self.assertEqual(cfg.rules[0].name, "MyNAS")
            self.assertEqual(cfg.rules[0].port, 16669)
            self.assertEqual(cfg.rules[0].mac, NAS_MAC)
            self.assertEqual(cfg.poll_interval, 30)
            self.assertEqual(cfg.base_url, "http://10.0.0.1")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_removed_keys_are_ignored_but_rest_still_apply(self):
        """旧设置文件里留着本版本已删的项（SOURCE / WRITE_ALL …）。

        读文件必须**忽略那些项、其余照常生效** —— 整份丢弃会让用户此前填的
        路由器密码、条目名一起失效，而人还以为配置没丢，这是最糟的失败方式。
        """
        from app import settings as st_mod
        path = os.path.join(_STATE_DIR, "legacy-settings.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"ROUTER_HOST": "10.0.0.9", "ENTRY_NAME": "MyNAS",
                       "SOURCE": "router", "WRITE_ALL": False,
                       "MAX_ADDRS": 2, "IFACE": "eth0"}, f)
        ov = st_mod.load_overlay(path)
        self.assertEqual(ov, {"ROUTER_HOST": "10.0.0.9", "ENTRY_NAME": "MyNAS"})
        cfg = Config(rules=[Rule(name="NAS")])
        st_mod.apply_overlay(cfg, ov)
        self.assertEqual(cfg.host, "10.0.0.9")
        self.assertEqual(cfg.rules[0].name, "MyNAS")

    def test_web_submit_still_rejects_unknown_key(self):
        """但网页提交这条路径必须继续严格报错：
        把用户提交的设置静默丢掉，是最坏的行为。"""
        from app import settings as st_mod
        with self.assertRaises(st_mod.SettingsError) as cm:
            st_mod.validate_overlay({"SOURCE": "router"})
        self.assertIn("SOURCE", str(cm.exception))

    def test_page_exposes_only_the_fixed_flow(self):
        """页面上不该再出现取址开关 —— 流程已固定，留着只会让人迷糊。"""
        from app import settings as st_mod
        keys = {f["key"] for g in st_mod.view(Config(), {})
                for f in g["fields"]}
        for gone in ("SOURCE", "MISMATCH_POLICY", "WRITE_ALL", "MAX_ADDRS",
                     "ADDR_SUFFIX", "IFACE", "TARGET_HOSTNAME", "ALLOW_ULA"):
            self.assertNotIn(gone, keys)
        for kept in ("ROUTER_HOST", "ROUTER_PASSWORD", "TARGET_MAC",
                     "POLL_INTERVAL", "ENTRY_NAME", "PORT", "REMOTE_IP",
                     "DRY_RUN", "LOG_LEVEL"):
            self.assertIn(kept, keys)

    def test_rules_json(self):
        from app.config import load_rules_from_env
        raw = ('[{"name":"NAS","port":16669,"mac":"00:11:22:33:44:55"},'
               '{"name":"NVR","port":8000,"mac":"AA:BB:CC:DD:EE:FF"}]')
        saved = os.environ.get("RULES")
        os.environ["RULES"] = raw
        try:
            rules = load_rules_from_env()
            self.assertEqual([r.name for r in rules], ["NAS", "NVR"])
            self.assertEqual(rules[1].port, 8000)
        finally:
            if saved is None:
                os.environ.pop("RULES", None)
            else:
                os.environ["RULES"] = saved

    def test_missing_password_exits_only_for_once_mode(self):
        from app.config import load_config
        saved = os.environ.get("ROUTER_PASSWORD")
        os.environ.pop("ROUTER_PASSWORD", None)
        try:
            # 常驻模式：没密码也允许启动（纯网页首次配置的前提）
            load_config([])
            # --once 单次模式：没有凭据必然一事无成，必须干净报错
            with self.assertRaises(SystemExit):
                load_config(["--once"])
        finally:
            if saved is not None:
                os.environ["ROUTER_PASSWORD"] = saved


class FakeRouterLoggedOut(FakeRouter):
    """未登录、也没有可用会话的假路由器：测「等用户填密码」那条路径。"""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.logged_in = False

    def check_session(self):
        return False


class TestCounters(unittest.TestCase):
    """累计写入计数：跨容器重启保留、坏文件不致命。

    现场问题：容器重启一次计数就归零，网页上「累计写入」永远停在个位数，
    用户没法判断程序到底同步过没有。
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ipv6sync-counters-")
        self.path = os.path.join(self.dir, "state.json")

    def test_accumulates_and_survives_restart(self):
        from app import state as st
        c = st.Counters(self.path)
        self.assertEqual(c.total_writes, 0)
        c.bump(created=1)
        c.bump(updated=2)
        self.assertEqual(c.total_writes, 3)
        self.assertEqual(c.session_writes, 3)
        self.assertTrue(c.first_write_at and c.last_write_at)
        self.assertTrue(c.flush())

        # 「重启」= 新进程读同一个文件
        c2 = st.Counters(self.path)
        self.assertEqual((c2.created, c2.updated), (1, 2))
        self.assertEqual(c2.total_writes, 3)
        self.assertEqual(c2.session_writes, 0)     # 本次启动从 0 起算

    def test_removed_counted_separately(self):
        """删条目也是真的改了设备配置，要计数，但不算进「写入」。"""
        from app import state as st
        c = st.Counters(self.path)
        c.bump(removed=2)
        c.flush()
        self.assertEqual(c.removed, 2)
        self.assertEqual(c.total_writes, 0)
        self.assertEqual(st.Counters(self.path).removed, 2)

    def test_bump_nothing_is_noop(self):
        from app import state as st
        c = st.Counters(self.path)
        c.bump()
        self.assertFalse(c._dirty)
        self.assertFalse(os.path.exists(self.path))

    def test_corrupt_file_falls_back_to_zero(self):
        """状态文件坏了只是「从 0 开始」，绝不能让同步起不来。"""
        from app import state as st
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ 这不是 json")
        c = st.Counters(self.path)
        self.assertEqual(c.total_writes, 0)
        self.assertIsNotNone(c.file_error)
        c.bump(created=1)
        self.assertTrue(c.flush())                 # 写回去就把坏文件修好了
        self.assertIsNone(c.file_error)
        self.assertEqual(st.Counters(self.path).total_writes, 1)

    def test_bad_field_values_ignored(self):
        import json
        from app import state as st
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump([1, 2, 3], f)                # 顶层不是对象
        self.assertEqual(st.Counters(self.path).total_writes, 0)

        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"created": -5, "updated": True, "removed": 2}, f)
        c = st.Counters(self.path)
        self.assertEqual((c.created, c.updated, c.removed), (0, 0, 2))

    def test_unwritable_target_only_warns(self):
        """落盘失败不能抛异常，也不能清 dirty —— 下轮还要再试。"""
        from app import state as st
        blocker = os.path.join(self.dir, "blocker")
        with open(blocker, "w", encoding="utf-8") as f:
            f.write("x")                            # 路径中间挡着一个文件
        c = st.Counters(os.path.join(blocker, "state.json"))
        c.bump(created=1)
        self.assertFalse(c.flush())
        self.assertTrue(c._dirty)
        self.assertIsNotNone(c.file_error)


class TestStatusSnapshot(LocalNetStub, unittest.TestCase):
    """网页状态里的 ok 必须能反映「最近一轮」。

    现场问题：网页是**轮询**取状态的，永远赶不上某一轮；snapshot() 不传 ok
    时以前返回 None，前端 `!!st.ok` 恒为 false —— 于是「同步」一直显示
    「等待首轮」，哪怕它每分钟都在正常写入。
    """

    def test_never_ran_is_none(self):
        s = Syncer(make_cfg(), FakeRouter(entries=[]))
        snap = s.snapshot()
        self.assertIsNone(snap["ok"])
        self.assertEqual(snap["last_tick_at"], "")
        self.assertEqual(snap["last_success_at"], "")

    def test_polling_after_success_keeps_reporting_ok(self):
        s = Syncer(make_cfg(), FakeRouter(entries=[]))
        s.tick()
        for _ in range(3):                          # 轮询多次结果都要稳定
            snap = s.snapshot()
            self.assertTrue(snap["ok"])
            self.assertTrue(snap["last_success_at"])
            self.assertEqual(snap["consecutive_failures"], 0)

    def test_unconfigured_password_is_false_not_none(self):
        """没填密码 = 「等配置」，不是「还没跑过」—— 这两者前端文案不同。"""
        s = Syncer(make_cfg(password=""), FakeRouterLoggedOut(entries=[]))
        self.assertFalse(s.tick()["ok"])
        snap = s.snapshot()
        self.assertIs(snap["ok"], False)            # 轮询不能又变回 None
        self.assertTrue(snap["unconfigured"])
        self.assertEqual(snap["consecutive_failures"], 0)   # 等配置不算失败

    def test_tick_flushes_counters(self):
        """一轮结束就落盘：容器随时可能被重启，计数不能只留在内存里。"""
        cfg = make_cfg()
        s = Syncer(cfg, FakeRouter(entries=[]))
        s.tick()
        self.assertEqual(s.total_writes, 1)
        self.assertTrue(os.path.exists(cfg.state_file), "tick 结束应已落盘")

        s2 = Syncer(cfg, FakeRouter(entries=[]))    # 模拟重启
        self.assertEqual(s2.total_writes, 1)
        self.assertEqual(s2.counters.session_writes, 0)

    def test_stale_removal_counted_without_inflating_writes(self):
        """地址变少要删旧条目：删要计数（改了设备），但不能算成「写入」。"""
        cfg = make_cfg()
        stale = {"Name": "NAS@2", "LocalIp": THIRD_ADDR, "RemoteIp": "::/0",
                 "Port": -1, "ID": "9"}
        s = Syncer(cfg, FakeRouter(entries=[stale]))
        s.tick()
        self.assertEqual(s.counters.removed, 1)
        self.assertEqual(s.total_writes, 1)         # 只算了新建的那 1 条


if __name__ == "__main__":
    unittest.main(verbosity=2)
