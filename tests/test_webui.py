# -*- coding: utf-8 -*-
"""
离线单元测试：设置覆盖层 + Web 控制台

不联网、不碰真路由器、不需要 Docker：
    python -m unittest discover -s tests -v
"""
import json
import os
import socket
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import settings as st          # noqa: E402
from app.config import Config, Rule, load_config   # noqa: E402
from app.node import Node               # noqa: E402
from app.router import AuthFailed       # noqa: E402
from app.sync import Syncer             # noqa: E402
from app.webui import WebUI, webui_from_env   # noqa: E402

ENV_KEYS = ("ROUTER_HOST", "ROUTER_USER", "ROUTER_PASSWORD", "ROUTER_PASSWORD_FILE",
            "SETTINGS_FILE", "STATE_FILE", "SESSION_FILE", "TARGET_MAC",
            "POLL_INTERVAL",
            "ENTRY_NAME", "PORT", "DRY_RUN", "VERIFY_AFTER_WRITE",
            "ADMIN_USERNAME", "ADMIN_PASSWORD", "ADMIN_PASSWORD_FILE")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class EnvSandbox(unittest.TestCase):
    """把相关环境变量清干净，避免外面 `.env` 之类的东西影响用例。"""

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in ENV_KEYS}
        for k in ENV_KEYS:
            os.environ.pop(k, None)
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["ROUTER_PASSWORD"] = "router-pw"
        os.environ["ROUTER_HOST"] = "192.168.3.1"
        os.environ["SETTINGS_FILE"] = os.path.join(self.tmp.name, "settings.json")
        os.environ["SESSION_FILE"] = os.path.join(self.tmp.name, "session.json")

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.tmp.cleanup()

    @property
    def settings_file(self) -> str:
        return os.environ["SETTINGS_FILE"]


# ==========================================================================
# 1. 设置覆盖层的校验与优先级
# ==========================================================================

class SettingsValidationTest(EnvSandbox):

    def test_unknown_key_rejected(self):
        with self.assertRaises(st.SettingsError):
            st.validate_overlay({"HACK_ME": "1"})

    def test_int_range_and_type(self):
        with self.assertRaises(st.SettingsError):
            st.validate_overlay({"POLL_INTERVAL": "abc"})
        with self.assertRaises(st.SettingsError):
            st.validate_overlay({"POLL_INTERVAL": "5"})        # 小于下限 10
        self.assertEqual(st.validate_overlay({"POLL_INTERVAL": "30"})["POLL_INTERVAL"], 30)

    def test_select_choices(self):
        with self.assertRaises(st.SettingsError):
            st.validate_overlay({"LOG_LEVEL": "chatty"})
        self.assertEqual(st.validate_overlay({"LOG_LEVEL": "DEBUG"})["LOG_LEVEL"],
                         "DEBUG")
        with self.assertRaises(st.SettingsError):
            st.validate_overlay({"ROUTER_SCHEME": "ftp"})
        self.assertEqual(
            st.validate_overlay({"ROUTER_SCHEME": "https"})["ROUTER_SCHEME"],
            "https")

    def test_bool_coercion(self):
        self.assertTrue(
            st.validate_overlay({"VERIFY_AFTER_WRITE": "true"})["VERIFY_AFTER_WRITE"])
        self.assertFalse(
            st.validate_overlay({"VERIFY_AFTER_WRITE": "0"})["VERIFY_AFTER_WRITE"])
        with self.assertRaises(st.SettingsError):
            st.validate_overlay({"VERIFY_AFTER_WRITE": "maybe"})

    def test_empty_password_means_keep(self):
        # 网页上密码留空表示"不修改"，不能把已配置的密码清成空串
        self.assertNotIn("ROUTER_PASSWORD", st.validate_overlay({"ROUTER_PASSWORD": ""}))
        self.assertIn("ROUTER_PASSWORD",
                      st.validate_overlay({"ROUTER_PASSWORD": "new"}))

    def test_empty_host_rejected(self):
        with self.assertRaises(st.SettingsError):
            st.validate_overlay({"ROUTER_HOST": "   "})

    def test_corrupt_file_is_ignored_not_fatal(self):
        with open(self.settings_file, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.assertEqual(st.load_overlay(self.settings_file), {})

    def test_file_with_bad_value_ignored(self):
        with open(self.settings_file, "w", encoding="utf-8") as f:
            json.dump({"POLL_INTERVAL": "nope"}, f)
        self.assertEqual(st.load_overlay(self.settings_file), {})

    def test_save_is_atomic_and_0600(self):
        st.save_overlay({"POLL_INTERVAL": 30}, self.settings_file)
        with open(self.settings_file, encoding="utf-8") as f:
            on_disk = json.load(f)
        self.assertEqual(st.validate_overlay(on_disk), {"POLL_INTERVAL": 30})
        if os.name != "nt":
            mode = os.stat(self.settings_file).st_mode & 0o777
            self.assertEqual(mode, 0o600, f"权限应为 600，实际 {oct(mode)}")


class SettingsPrecedenceTest(EnvSandbox):

    def test_env_used_when_no_overlay(self):
        cfg = load_config([])
        self.assertEqual(cfg.host, "192.168.3.1")
        self.assertEqual(cfg.poll_interval, 60)          # 默认值

    def test_overlay_beats_env(self):
        st.save_overlay({"ROUTER_HOST": "10.0.0.9", "POLL_INTERVAL": 45},
                        self.settings_file)
        cfg = load_config([])
        self.assertEqual(cfg.host, "10.0.0.9")
        self.assertEqual(cfg.poll_interval, 45)

    def test_overlay_targets_first_rule(self):
        st.save_overlay({"ENTRY_NAME": "MYNAS", "PORT": "8443",
                         "REMOTE_IP": "2001:db8::/32"}, self.settings_file)
        cfg = load_config([])
        r = cfg.rules[0]
        self.assertEqual(r.name, "MYNAS")
        self.assertEqual(r.port, 8443)
        self.assertEqual(r.remote_ip, "2001:db8::/32")

    def test_port_minus_one_means_all(self):
        st.save_overlay({"PORT": "-1"}, self.settings_file)
        self.assertIsNone(load_config([]).rules[0].port)

    def test_password_needed_unless_session_exists(self):
        # 常驻模式：没密码也允许启动（纯网页首次配置的前提），不再硬性报错
        os.environ.pop("ROUTER_PASSWORD")
        cfg = load_config([])
        self.assertEqual(cfg.password, "")
        # --once 单次模式没有凭据必然一事无成，必须干净报错
        with self.assertRaises(SystemExit):
            load_config(["--once"])
        # 有会话文件就能 --once 起来（会话过期后才需要密码）
        with open(os.environ["SESSION_FILE"], "w", encoding="utf-8") as f:
            json.dump({"base": "http://192.168.3.1", "logged_in": True}, f)
        cfg = load_config(["--once"])
        self.assertEqual(cfg.password, "")

    def test_password_from_file(self):
        os.environ.pop("ROUTER_PASSWORD")
        secret = os.path.join(self.tmp.name, "router_pw.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("pw-from-secret\n")     # 结尾换行必须被去掉
        os.environ["ROUTER_PASSWORD_FILE"] = secret
        cfg = load_config([])
        self.assertEqual(cfg.password, "pw-from-secret")
        self.assertIn("ROUTER_PASSWORD_FILE", cfg.password_source)

    def test_view_hides_password(self):
        out = {f["key"]: f for g in st.view(load_config([]), {})
               for f in g["fields"]}
        self.assertEqual(out["ROUTER_PASSWORD"]["value"], "")
        self.assertTrue(out["ROUTER_PASSWORD"]["is_set"])
        self.assertEqual(out["ROUTER_HOST"]["source"], "env")
        self.assertEqual(out["POLL_INTERVAL"]["source"], "default")


# ==========================================================================
# 2. Node 的热重配
# ==========================================================================

class NodeReconfigureTest(EnvSandbox):

    def test_save_applies_and_persists(self):
        node = Node(load_config([]))
        res = node.save_settings({"ROUTER_HOST": "10.0.0.9", "POLL_INTERVAL": 30,
                                  "DRY_RUN": True, "ENTRY_NAME": "MYNAS"})
        # 内存里立刻生效
        self.assertEqual(node.cfg.host, "10.0.0.9")
        self.assertEqual(node.cfg.poll_interval, 30)
        self.assertTrue(node.cfg.dry_run)
        self.assertEqual(node.cfg.rules[0].name, "MYNAS")
        # 同步器也换到了新配置上（不是只改了 Node 的副本）
        self.assertIs(node.syncer.cfg, node.cfg)
        self.assertIs(node.syncer.router, node.router)
        # 落盘了，重启后仍然生效
        with open(self.settings_file, encoding="utf-8") as f:
            saved = json.load(f)
        self.assertEqual(saved["ROUTER_HOST"], "10.0.0.9")
        self.assertEqual(load_config([]).host, "10.0.0.9")
        self.assertIn("settings", res)

    def test_invalid_patch_does_not_touch_disk(self):
        node = Node(load_config([]))
        with self.assertRaises(st.SettingsError):
            node.save_settings({"POLL_INTERVAL": "3"})
        self.assertEqual(node.cfg.poll_interval, 60)
        self.assertFalse(os.path.exists(self.settings_file))

    def test_credential_change_drops_old_session(self):
        session = os.environ["SESSION_FILE"]
        with open(session, "w", encoding="utf-8") as f:
            json.dump({"base": "http://192.168.3.1", "logged_in": True}, f)
        node = Node(load_config([]))
        self.assertTrue(node.router.logged_in)
        res = node.save_settings({"ROUTER_PASSWORD": "another-pw"})
        self.assertFalse(os.path.exists(session))          # 旧会话被丢掉
        self.assertTrue(any("丢弃旧会话" in n for n in res["notes"]))
        self.assertEqual(node.cfg.password, "another-pw")

    def test_router_host_change_drops_session_too(self):
        session = os.environ["SESSION_FILE"]
        with open(session, "w", encoding="utf-8") as f:
            json.dump({"base": "http://192.168.3.1", "logged_in": True}, f)
        node = Node(load_config([]))
        node.save_settings({"ROUTER_HOST": "10.1.1.1"})
        self.assertFalse(os.path.exists(session))

    def test_overview_reports_password_source(self):
        node = Node(load_config([]))
        node.save_settings({"ROUTER_PASSWORD": "from-web"})
        self.assertIn("网页设置", node.overview()["password_from"])
        self.assertTrue(node.overview()["password_set"])


# ==========================================================================
# 3. Web 控制台
# ==========================================================================

class FakeRouter:
    def __init__(self):
        self.logged_in = True
        self.username = "admin"
        self.level = 2

    def check_session(self):
        return True

    def login(self, password=None):
        return {"username": "admin", "level": 2}

    def get(self, name, params=None):
        if name == "deviceinfo":
            return {"ProductName": "华为凌霄子母路由 Q7 网线版",
                    "DeviceModel": "MEDUSA2-BR80", "SerialNumber": "d8e37e1f"}
        if name == "ip6firewall_enable":
            return {"Enable": True}
        if name == "ip6firewall_trustlist":
            return [{"Name": "NAS", "LocalIp": "240e:3a4:48ff:6d10::1",
                     "RemoteIp": "::/0", "Port": 16669, "ID": "…Trustlist.1."}]
        if name == "HostInfo":
            return [{"MACAddress": "00:11:22:AA:BB:CC", "HostName": "<b>名字</b>",
                     "IPAddress": "192.168.3.5", "Active": 1,
                     "Ipv6Addrs": [{"Ipv6Addr": "240e:3a4:48ff:6d10::1"}]}]
        if name == "ntwk/wlanfilterenhance":
            return []
        raise AssertionError(name)

    def post(self, name, data=None, wrapped=True, action=None):
        if name == "ip6firewall_enable":
            return {}
        raise AssertionError(name)


class FakeNode:
    def __init__(self):
        self.saved = []
        self.router = FakeRouter()
        from app.sync import Syncer
        from app.config import Config
        self.cfg = Config(rules=[])
        self.syncer = Syncer(self.cfg, self.router)

    def overview(self):
        return {"router_base": "http://192.168.3.1", "router_user": "admin",
                "password_from": "环境变量", "password_set": True,
                "settings_file": "/data/settings.json", "rules": []}

    def settings_view(self):
        return [{"group": "路由器连接", "fields": [
            {"key": "ROUTER_HOST", "label": "路由器地址", "kind": "str",
             "value": "192.168.3.1", "source": "env", "help": "", "placeholder": ""},
            {"key": "ROUTER_PASSWORD", "label": "路由器管理员密码", "kind": "secret",
             "value": "", "is_set": True, "source": "env", "help": "",
             "placeholder": "留空表示不修改"},
        ]}]

    def snapshot(self):
        return {"ok": True, "last_tick_at": "2026-09-21 17:40:00",
                "consecutive_failures": 0, "total_writes": 3, "logged_in": True,
                "firewall_ipv6_enabled": True, "dry_run": False,
                "last_error": None, "rules": {}}

    def save_settings(self, patch):
        if "ROUTER_HOST" in patch and patch["ROUTER_HOST"] == "bad":
            raise st.SettingsError("路由器地址不合法")
        self.saved.append(patch)
        return {"overview": self.overview(), "settings": self.settings_view(),
                "notes": ["测试备注"]}

    def sync_now(self):
        return self.snapshot()

    def test_connection(self):
        return {"ok": True, "device": {"product": "Q7", "model": "MEDUSA2-BR80",
                                       "sn": "d8e37e1f", "software": "1.0"}}

    def whitelist(self):
        return {"ok": True, "enabled": True, "max": 32,
                "entries": self.router.get("ip6firewall_trustlist")}

    def hosts(self):
        return {"ok": True, "hosts": [{"mac": "00:11:22:AA:BB:CC",
                                       "name": "<b>名字</b>",
                                       "ipv4": "192.168.3.5",
                                       "ipv6": ["240e:3a4:48ff:6d10::1"],
                                       "active": True, "offline_at": "",
                                       "kids": False}],
                "blacklist": [], "blacklist_error": "",
                "lan_prefixes": ["240e:3a4:48ff:6d10::/64"]}

    def set_firewall(self, on):
        return {"ok": True, "enabled": on}


def call(base, path, method="GET", body=None, cookie=None, origin=None,
         ctype=None, raw=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {}
    if data is not None:
        headers["Content-Type"] = ctype or "application/json"
    if cookie:
        headers["Cookie"] = cookie
    if origin:
        headers["Origin"] = origin
    req = urllib.request.Request(base + path, data=data, headers=headers,
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            payload = r.read().decode("utf-8")
            return r.status, (payload if raw else json.loads(payload)), r.headers
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode("utf-8")
        finally:
            e.close()          # 不关会在 GC 时报 unclosed socket
        if raw:
            return e.code, payload, e.headers
        try:
            return e.code, json.loads(payload), e.headers
        except json.JSONDecodeError:
            return e.code, {"raw": payload}, e.headers


class LoginBackoffTest(EnvSandbox):
    """网页上点按钮不能绕过退避 —— 设备只允许连错 3 次密码。"""

    class DeadRouter:
        def __init__(self):
            self.logged_in = False
            self.username = "admin"
            self.level = 0
            self.attempts = 0

        def check_session(self):
            return False

        def login(self, password=None):
            self.attempts += 1
            raise AuthFailed("密码错误（测试）")

    def make(self):
        router = self.DeadRouter()
        cfg = Config(host="192.168.3.1", username="admin", password="x",
                     session_file=None, rules=[Rule(name="NAS")])
        return Syncer(cfg, router), router

    def test_backoff_grows_and_blocks_repeat_attempts(self):
        syncer, router = self.make()
        self.assertFalse(syncer.ensure_login())
        self.assertEqual(router.attempts, 1)
        self.assertGreater(syncer.login_backoff_remaining(), 0)
        # 退避期内再调多少次都不会真的去打设备
        for _ in range(5):
            syncer.ensure_login()
        self.assertEqual(router.attempts, 1, "退避期内不应重试登录")
        # 第一次的退避时长就是配置的基数
        self.assertLessEqual(syncer.login_backoff_remaining(),
                             syncer.cfg.login_backoff_base)

    def test_backoff_doubles(self):
        syncer, router = self.make()
        syncer.ensure_login()
        first = syncer.login_backoff_remaining()
        syncer.next_login_at = 0.0          # 假装等到点了
        syncer.ensure_login()
        second = syncer.login_backoff_remaining()
        self.assertGreater(second, first * 1.8, "第二次退避应约为第一次的两倍")
        self.assertEqual(router.attempts, 2)

    def test_node_explains_backoff_instead_of_vague_error(self):
        router = self.DeadRouter()
        cfg = Config(host="192.168.3.1", username="admin", password="x",
                     session_file=None, rules=[Rule(name="NAS")])
        node = Node.__new__(Node)           # 只测 _logged_in，不装配真实对象
        node.syncer = Syncer(cfg, router)
        err = node._logged_in()
        self.assertIsNotNone(err)
        self.assertIn("退避", err)
        self.assertIn("秒", err)

    def test_whitelist_reports_backoff_without_touching_router(self):
        router = self.DeadRouter()
        cfg = Config(host="192.168.3.1", username="admin", password="x",
                     session_file=None, rules=[Rule(name="NAS")])
        node = Node.__new__(Node)
        node.syncer = Syncer(cfg, router)
        for _ in range(3):
            res = node.whitelist()
            self.assertFalse(res["ok"])
            self.assertIn("退避", res["error"])
        self.assertEqual(router.attempts, 1, "点多次也只应该试一次登录")


class ConsoleCredentialsTest(EnvSandbox):
    """控制台登录账号来自 compose 的 environment（ADMIN_USERNAME / ADMIN_PASSWORD）。"""

    def test_login_credentials_from_admin_env(self):
        os.environ["ADMIN_USERNAME"] = "boss"
        os.environ["ADMIN_PASSWORD"] = "s3cret-pw"
        ui = webui_from_env(FakeNode(), host="127.0.0.1", port=free_port())
        self.assertEqual(ui.user, "boss")
        self.assertTrue(ui.enabled())
        self.assertTrue(ui._check_credentials("boss", "s3cret-pw"))
        self.assertFalse(ui._check_credentials("boss", "wrong"))
        self.assertFalse(ui._check_credentials("admin", "s3cret-pw"))

    def test_password_file(self):
        secret = os.path.join(self.tmp.name, "web_pw.txt")
        with open(secret, "w", encoding="utf-8") as f:
            f.write("pw-from-file\n")       # 结尾换行必须被去掉
        os.environ["ADMIN_PASSWORD_FILE"] = secret
        ui = webui_from_env(FakeNode(), host="127.0.0.1", port=free_port())
        self.assertTrue(ui._check_credentials("admin", "pw-from-file"))

    def test_no_admin_password_disables_console(self):
        ui = webui_from_env(FakeNode(), host="127.0.0.1", port=free_port())
        self.assertFalse(ui.enabled())


class UnconfiguredStandbyTest(unittest.TestCase):
    """零凭据常驻待机：不碰路由器、不累计失败、状态里标明 unconfigured。"""

    class SilentRouter:
        def __init__(self):
            self.logged_in = False

        def check_session(self):
            return False

        def login(self, password=None):
            raise AssertionError("没有密码时绝不应尝试登录（会锁账号）")

    def make(self):
        cfg = Config(host="192.168.3.1", username="admin", password="",
                     session_file=None, rules=[Rule(name="NAS")])
        return Syncer(cfg, self.SilentRouter())

    def test_ensure_login_fast_fails_with_guidance(self):
        s = self.make()
        self.assertFalse(s.ensure_login())
        self.assertIn("未配置路由器密码", s.last_error)
        self.assertIn("控制台", s.last_error)

    def test_tick_unconfigured_is_not_a_failure(self):
        s = self.make()
        for _ in range(3):
            snap = s.tick()
            self.assertFalse(snap["ok"])
            self.assertTrue(snap["unconfigured"])
            self.assertEqual(snap["consecutive_failures"], 0,
                             "未配置不该累计失败（健康检查会永远 degraded）")


class WebUITest(unittest.TestCase):
    """真起一个 HTTP 服务，用真 socket 打一遍 —— 只把 Node 换成假的。"""

    def setUp(self):
        self.node = FakeNode()
        self.port = free_port()
        self.ui = WebUI(self.node, "127.0.0.1", self.port, "admin", "web-secret",
                        session_ttl=3600)
        self.srv = self.ui.start()
        self.assertIsNotNone(self.srv, "控制台没起来")
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.ui.stop()

    # ---- 页面与登录 ----
    def test_page_served_and_version_injected(self):
        code, body, hdr = call(self.base, "/", raw=True)
        self.assertEqual(code, 200)
        self.assertIn("IPv6 白名单同步", body)
        self.assertNotIn("__APP_VERSION__", body)
        self.assertIn("no-store", hdr.get("Cache-Control", ""))
        self.assertIn("Content-Security-Policy", hdr)

    def test_session_starts_unauthenticated(self):
        code, j, _ = call(self.base, "/api/session")
        self.assertEqual((code, j["authed"]), (200, False))

    def test_state_without_login_is_quiet(self):
        code, j, _ = call(self.base, "/api/state")
        self.assertEqual(code, 200)
        self.assertFalse(j["authed"])
        self.assertNotIn("settings", j)          # 未登录别漏设置

    def test_actions_require_login(self):
        for path, method in (("/api/settings", "POST"), ("/api/sync", "POST"),
                             ("/api/router/whitelist", "GET")):
            code, j, _ = call(self.base, path, method, body={} if method == "POST" else None)
            self.assertEqual(code, 401, path)
            self.assertFalse(j["ok"])

    def test_wrong_password_rejected(self):
        code, j, _ = call(self.base, "/api/login", "POST",
                          {"user": "admin", "password": "nope"})
        self.assertEqual(code, 401)
        self.assertFalse(j["ok"])

    def test_wrong_username_rejected(self):
        code, _, _ = call(self.base, "/api/login", "POST",
                          {"user": "root", "password": "web-secret"})
        self.assertEqual(code, 401)

    # ---- 登录后的完整流程 ----
    def login(self) -> str:
        code, j, hdr = call(self.base, "/api/login", "POST",
                            {"user": "admin", "password": "web-secret"})
        self.assertEqual(code, 200, j)
        cookie = hdr.get("Set-Cookie", "")
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=Lax", cookie)
        return cookie.split(";")[0]

    def test_full_flow(self):
        cookie = self.login()

        code, j, _ = call(self.base, "/api/state", cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(j["authed"])
        self.assertEqual(j["user"], "admin")
        self.assertTrue(j["settings"])
        # 密码绝不能出现在返回里
        self.assertNotIn("web-secret", json.dumps(j, ensure_ascii=False))
        blob = json.dumps(j, ensure_ascii=False)
        self.assertIn('"is_set": true', blob)

        code, j, _ = call(self.base, "/api/router/whitelist", cookie=cookie)
        self.assertEqual((code, j["ok"]), (200, True))
        self.assertEqual(j["entries"][0]["Name"], "NAS")

        code, j, _ = call(self.base, "/api/router/hosts", cookie=cookie)
        self.assertEqual((code, j["ok"]), (200, True))
        self.assertEqual(j["hosts"][0]["mac"], "00:11:22:AA:BB:CC")

        code, j, _ = call(self.base, "/api/router/test", "POST", {}, cookie=cookie)
        self.assertEqual((code, j["ok"]), (200, True))

        code, j, _ = call(self.base, "/api/router/firewall", "POST",
                          {"enable": True}, cookie=cookie)
        self.assertEqual((code, j["ok"]), (200, True))

        code, j, _ = call(self.base, "/api/sync", "POST", {}, cookie=cookie)
        self.assertEqual((code, j["ok"]), (200, True))

    def test_save_settings_round_trip(self):
        cookie = self.login()
        patch = {"ROUTER_HOST": "10.0.0.9", "POLL_INTERVAL": "30"}
        code, j, _ = call(self.base, "/api/settings", "POST", patch, cookie=cookie)
        self.assertEqual(code, 200)
        self.assertTrue(j["ok"])
        self.assertEqual(self.node.saved[-1], patch)
        self.assertEqual(j["notes"], ["测试备注"])

    def test_save_settings_validation_error_is_400(self):
        cookie = self.login()
        code, j, _ = call(self.base, "/api/settings", "POST",
                          {"ROUTER_HOST": "bad"}, cookie=cookie)
        self.assertEqual(code, 400)
        self.assertFalse(j["ok"])
        self.assertIn("不合法", j["error"])

    def test_cross_origin_post_rejected(self):
        cookie = self.login()
        code, j, _ = call(self.base, "/api/settings", "POST",
                          {"POLL_INTERVAL": "30"}, cookie=cookie,
                          origin="http://evil.example")
        self.assertEqual(code, 403)
        self.assertFalse(self.node.saved)

    def test_same_origin_post_allowed(self):
        cookie = self.login()
        code, _, _ = call(self.base, "/api/settings", "POST", {"POLL_INTERVAL": "30"},
                          cookie=cookie, origin=self.base)
        self.assertEqual(code, 200)

    def test_form_content_type_rejected(self):
        cookie = self.login()
        code, j, _ = call(self.base, "/api/settings", "POST", {"POLL_INTERVAL": "30"},
                          cookie=cookie, ctype="application/x-www-form-urlencoded")
        self.assertEqual(code, 415)

    def test_bad_cookie_is_anonymous(self):
        code, j, _ = call(self.base, "/api/state", cookie="ipv6sync_web=forged")
        self.assertFalse(j["authed"])

    def test_logout_invalidates_token(self):
        cookie = self.login()
        code, _, hdr = call(self.base, "/api/logout", "POST", {}, cookie=cookie)
        self.assertEqual(code, 200)
        self.assertIn("Max-Age=0", hdr.get("Set-Cookie", ""))
        code, j, _ = call(self.base, "/api/state", cookie=cookie)
        self.assertFalse(j["authed"])

    def test_unknown_route_404(self):
        cookie = self.login()
        code, j, _ = call(self.base, "/api/nope", cookie=cookie)
        self.assertEqual((code, j["ok"]), (404, False))


class WebUILoginThrottleTest(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        self.ui = WebUI(FakeNode(), "127.0.0.1", self.port, "admin", "web-secret")
        self.ui.start()
        self.base = f"http://127.0.0.1:{self.port}"

    def tearDown(self):
        self.ui.stop()

    def test_throttle_after_repeated_failures(self):
        codes = []
        for _ in range(10):
            code, _, _ = call(self.base, "/api/login", "POST",
                              {"user": "admin", "password": "wrong"})
            codes.append(code)
        self.assertIn(429, codes, f"连续失败应触发限速，实际 {codes}")
        # 被限速后即使密码对也不行，防止"边爆破边撞对"
        code, j, _ = call(self.base, "/api/login", "POST",
                          {"user": "admin", "password": "web-secret"})
        self.assertEqual(code, 429)
        self.assertIn("秒后再试", j["error"])

    def test_success_clears_failure_counter(self):
        for _ in range(3):
            call(self.base, "/api/login", "POST",
                 {"user": "admin", "password": "wrong"})
        code, _, _ = call(self.base, "/api/login", "POST",
                          {"user": "admin", "password": "web-secret"})
        self.assertEqual(code, 200)
        self.assertEqual(self.ui._throttled("127.0.0.1"), 0.0)

    # ---- 防爆破升级：递进锁定 / 全局熔断（直接测内部状态，不拖 0.25s 延时）----

    def test_lock_escalates_and_resets_on_release(self):
        ip = "10.9.9.9"
        for _ in range(8):
            self.ui._note_fail(ip)
        w1 = self.ui._throttled(ip)
        self.assertTrue(299 <= w1 <= 300, f"首次锁定应约 300 秒，实际 {w1}")

        # 锁定期间一直返回剩余等待
        self.assertGreater(self.ui._throttled(ip), 0)

        # 时间过去，出狱后窗口清零、可以再来，但下次锁更久
        self.ui._locked_until[ip] = time.time() - 1
        self.assertEqual(self.ui._throttled(ip), 0.0)
        for _ in range(8):
            self.ui._note_fail(ip)
        w2 = self.ui._throttled(ip)
        self.assertTrue(599 <= w2 <= 600, f"第二次锁定应约 600 秒，实际 {w2}")

        # 顶到上限 LOCK_MAX
        self.ui._locked_until[ip] = time.time() - 1
        for _ in range(9):          # 跳过中间几档，直接堆 strikes
            self.ui._locked_until[ip] = time.time() - 1
            self.ui._strikes[ip] = 20
            for _ in range(8):
                self.ui._note_fail(ip)
            w = self.ui._throttled(ip)
        self.assertLessEqual(w, 6 * 3600 + 1, "锁定时长不应超过 LOCK_MAX")

    def test_global_circuit_breaker_blocks_new_ip(self):
        # 60 次失败分摊到 60 个"不同 IP"，单个 IP 都没超限
        for i in range(60):
            self.ui._note_fail(f"10.1.0.{i}")
        w = self.ui._throttled("10.2.3.4")     # 全新的、零失败的 IP
        self.assertTrue(299 <= w <= 300,
                        f"全局熔断应拦住新 IP 约 300 秒，实际 {w}")

    def test_successful_login_resets_strikes(self):
        ip = "10.9.9.9"
        self.ui._strikes[ip] = 3
        self.ui._locked_until[ip] = time.time() - 1
        self.ui._fails[ip] = [time.time()] * 8
        self.ui._clear_fails(ip)
        self.assertEqual(self.ui._throttled(ip), 0.0)
        self.assertEqual(self.ui._strikes.get(ip, 0), 0,
                         "登录成功后应清空递进锁定计数")


class WebUIDisabledTest(unittest.TestCase):
    def test_no_password_means_no_server(self):
        ui = WebUI(FakeNode(), "127.0.0.1", free_port(), "admin", "")
        self.assertFalse(ui.enabled())
        self.assertIsNone(ui.start())        # 没口令就不开端口，而不是开个裸的

    def test_port_zero_means_disabled(self):
        ui = WebUI(FakeNode(), "127.0.0.1", 0, "admin", "x")
        self.assertFalse(ui.enabled())
        self.assertIsNone(ui.start())

    def test_expired_token_rejected(self):
        ui = WebUI(FakeNode(), "127.0.0.1", free_port(), "admin", "x", session_ttl=300)
        tok = ui._new_token("admin")
        self.assertEqual(ui._valid(tok), "admin")
        ui._tokens[tok] = ("admin", 1.0)     # 手动过期
        self.assertIsNone(ui._valid(tok))
        self.assertNotIn(tok, ui._tokens)    # 顺便清掉


# ==========================================================================
# 4. Node.hosts：在线/离线拆分、儿童上网标记、黑名单解析
# ==========================================================================

class NodeHostsTest(EnvSandbox):
    """Node.hosts() 的数据整形逻辑（用假路由器，不联网）。"""

    class FakeR:
        def __init__(self, wlan=None):
            self.logged_in = True
            self.wlan = wlan if wlan is not None else [
                {"WifiModulename": "WIFI-2.4G",
                 "BMACAddresses": [{"MACAddress": "04:F1:28:1A:01:73",
                                    "HostName": "诺基亚6"}]},
                {"WifiModulename": "WIFI-5G",
                 "BMACAddresses": [{"MACAddress": "04:F1:28:1A:01:73",
                                    "HostName": "诺基亚6"}]},
            ]

        def check_session(self):
            return True

        def get(self, name, params=None):
            if name == "HostInfo":
                return [
                    {"MACAddress": "AA:00:00:00:00:01", "HostName": "小爱音响",
                     "IPAddress": "192.168.3.31", "Active": 1,
                     "ParentControlEnable": 1, "MacFilterID": "3",
                     "Ipv6Addrs": [{"Ipv6Addr": "240e:3a4::a1"}]},
                    {"MACAddress": "AA:00:00:00:00:02", "HostName": "旧电脑",
                     "IPAddress": "192.168.3.9", "Active": "0",
                     "OfflineRecord": "2026-09-26 15:42:10#0#1"},
                    {"MACAddress": "AA:00:00:00:00:03", "HostName": "普通机",
                     "IPAddress": "192.168.3.11", "Active": True,
                     "MacFilterID": ""},
                ]
            if name == "ntwk/wlanfilterenhance":
                return self.wlan
            raise AssertionError(name)

    def make(self, **kw) -> Node:
        node = Node.__new__(Node)
        cfg = Config(host="192.168.3.1", username="admin", password="x",
                     session_file=None, rules=[])
        router = self.FakeR(**kw)
        node.syncer = Syncer(cfg, router)
        node.router = router
        return node

    def test_active_offline_kids_fields(self):
        res = self.make().hosts()
        self.assertTrue(res["ok"])
        by = {h["mac"]: h for h in res["hosts"]}
        self.assertTrue(by["AA:00:00:00:00:01"]["active"])
        self.assertTrue(by["AA:00:00:00:00:01"]["kids"], "ParentControlEnable=1 应判定为儿童上网")
        off = by["AA:00:00:00:00:02"]
        self.assertFalse(off["active"], "Active='0'（字符串）不能误判为在线")
        self.assertEqual(off["offline_at"], "2026-09-26 15:42:10")
        self.assertFalse(by["AA:00:00:00:00:03"]["kids"], "MacFilterID 为空不算儿童上网")

    def test_blacklist_merged_across_bands(self):
        res = self.make().hosts()
        self.assertEqual(len(res["blacklist"]), 1, "同一 MAC 跨 2.4G/5G 应合并去重")
        b = res["blacklist"][0]
        self.assertEqual(b["mac"], "04:F1:28:1A:01:73")
        self.assertEqual(b["name"], "诺基亚6")
        self.assertIn("+", b["band"], "两个频段都拦时应拼在一起")

    def test_blacklist_failure_does_not_break_hosts(self):
        node = self.make()
        router = node.syncer.router
        orig_get = router.get

        def boom(name, params=None):
            if name == "ntwk/wlanfilterenhance":
                from app.router import RouterError
                raise RouterError("模拟黑名单接口挂了")
            return orig_get(name, params)

        router.get = boom
        res = node.hosts()
        self.assertTrue(res["ok"])
        self.assertEqual(res["blacklist"], [])
        self.assertIn("模拟", res["blacklist_error"])

    def test_blacklist_tolerates_odd_shapes(self):
        from app.node import _parse_blacklist
        # 纯 MAC 字符串条目
        self.assertEqual(
            [r["mac"] for r in _parse_blacklist(
                {"BMACAddresses": [" aa:bb:cc:dd:ee:ff "]})],
            ["AA:BB:CC:DD:EE:FF"])
        # 再包一层字典 + 单数键名
        self.assertEqual(
            [r["mac"] for r in _parse_blacklist(
                [{"BMACAddresses": {"BMACAddress":
                    [{"MACAddress": "11:22:33:44:55:66"}]}}])],
            ["11:22:33:44:55:66"])
        # 空响应
        self.assertEqual(_parse_blacklist(None), [])
        self.assertEqual(_parse_blacklist({}), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
