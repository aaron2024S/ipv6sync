# -*- coding: utf-8 -*-
"""通知模块的离线测试：三渠道独立开关与同时推送、URL/报文构造、
企业微信 key 提取、以及「白名单真有写入才发通知」的同步联动。全程不联网。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import discover, notify  # noqa: E402
from app.config import Config, Rule  # noqa: E402
from app.sync import Syncer  # noqa: E402

# 复用 test_logic 的假路由器。两种导入布局都要兼容：
#   本地 `python -m unittest discover -s tests`  → tests/ 在 sys.path，顶层导入
#   镜像内 `discover -s tests -t .`              → 只有项目根在 sys.path，走包路径
try:
    from test_logic import FakeRouter
except ImportError:  # noqa: E203
    from tests.test_logic import FakeRouter  # type: ignore

NAS_MAC = "00:11:22:AA:BB:CC"
OLD_ADDR = "240e:3a4:48ff:6d10:7173:1ffc:64ed:82c0"


def make_cfg(**kw) -> Config:
    # mac=None = 本机网卡取址路径（_LocalNetStub 已装好假网卡）；
    # 绑 MAC 的设备规则另有专属测试。
    cfg = Config(
        host="192.168.3.1", username="admin", password="x",
        session_file=None, poll_interval=1,
        rules=[Rule(name="NAS", port=None, remote_ip="::/0", mac=None)],
    )
    for k, v in kw.items():
        setattr(cfg, k, v)
    return cfg


def ntfy_on(**kw) -> Config:
    kw.setdefault("notify_ntfy_url", "https://ntfy.sh/mytopic")
    return make_cfg(notify_ntfy_enabled=True, **kw)


def gotify_on(**kw) -> Config:
    kw.setdefault("notify_gotify_url", "https://push.example.com")
    kw.setdefault("notify_gotify_token", "AppTok123")
    return make_cfg(notify_gotify_enabled=True, **kw)


def wecom_on(**kw) -> Config:
    kw.setdefault("notify_wecom_id", "85d3153d-1a2b")
    return make_cfg(notify_wecom_enabled=True, **kw)


class WecomKeyTest(unittest.TestCase):
    def test_bare_id(self):
        self.assertEqual(
            notify.extract_wecom_key("85d3153d-1a2b-3c4d-5e6f-778899aabbcc"),
            "85d3153d-1a2b-3c4d-5e6f-778899aabbcc")

    def test_full_webhook_url(self):
        self.assertEqual(
            notify.extract_wecom_key(
                "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key="
                "85d3153d-1a2b-3c4d-5e6f-778899aabbcc"),
            "85d3153d-1a2b-3c4d-5e6f-778899aabbcc")

    def test_empty(self):
        self.assertEqual(notify.extract_wecom_key(""), "")
        self.assertEqual(notify.extract_wecom_key(None), "")


class EnableTest(unittest.TestCase):
    def test_no_channel_enabled_is_noop(self):
        self.assertEqual(notify.enabled_channels(make_cfg()), [])
        self.assertEqual(notify.send_all(make_cfg(), "t", "m"), [])

    def test_enabled_channels_order(self):
        cfg = wecom_on()
        cfg.notify_ntfy_enabled = True
        cfg.notify_gotify_enabled = True
        self.assertEqual(notify.enabled_channels(cfg),
                         ["ntfy", "gotify", "wecom"])


class NtfyTest(unittest.TestCase):
    def test_topic_in_url_and_bearer(self):
        cfg = ntfy_on(notify_ntfy_token="tk_xxx")
        with mock.patch.object(notify, "_post_raw",
                               return_value=(True, "")) as p:
            results = notify.send_all(cfg, "标题", "消息")
        self.assertEqual(results, [("ntfy", True, "")])
        url, data, headers = p.call_args[0][:3]
        self.assertEqual(url, "https://ntfy.sh/mytopic")
        self.assertIn("标题".encode(), data)
        self.assertIn("消息".encode(), data)
        self.assertEqual(headers.get("Authorization"), "Bearer tk_xxx")

    def test_default_topic_appended(self):
        cfg = ntfy_on(notify_ntfy_url="https://ntfy.sh")
        with mock.patch.object(notify, "_post_raw",
                               return_value=(True, "")) as p:
            notify.send_all(cfg, "t", "m")
        self.assertEqual(p.call_args[0][0], "https://ntfy.sh/ipv6sync")

    def test_missing_url(self):
        cfg = ntfy_on(notify_ntfy_url="")
        results = notify.send_all(cfg, "t", "m")
        self.assertEqual(results[0][0], "ntfy")
        self.assertFalse(results[0][1])
        self.assertIn("服务器地址", results[0][2])


class GotifyTest(unittest.TestCase):
    def test_url_and_payload(self):
        cfg = gotify_on()
        with mock.patch.object(notify, "_post_json",
                               return_value=(True, "")) as p:
            results = notify.send_all(cfg, "标题", "消息")
        self.assertEqual(results, [("gotify", True, "")])
        url, payload = p.call_args[0]
        self.assertTrue(url.startswith("https://push.example.com/message?token=AppTok123"))
        self.assertEqual(payload["title"], "标题")
        self.assertEqual(payload["message"], "消息")

    def test_missing_token(self):
        cfg = gotify_on(notify_gotify_token="")
        results = notify.send_all(cfg, "t", "m")
        self.assertFalse(results[0][1])
        self.assertIn("Token", results[0][2])


class WecomTest(unittest.TestCase):
    def test_payload_and_errcode(self):
        cfg = wecom_on()
        with mock.patch.object(notify, "_post_json",
                               return_value=(True, '{"errcode":0}')) as p:
            results = notify.send_all(cfg, "标题", "消息")
        self.assertEqual(results, [("wecom", True, "")])
        url, payload = p.call_args[0]
        self.assertIn("key=85d3153d-1a2b", url)
        self.assertEqual(payload["msgtype"], "text")
        self.assertIn("标题", payload["text"]["content"])

    def test_business_error_surfacing(self):
        cfg = wecom_on()
        with mock.patch.object(
                notify, "_post_json",
                return_value=(True,
                              '{"errcode":93000,"errmsg":"invalid webhook"}')):
            results = notify.send_all(cfg, "t", "m")
        self.assertFalse(results[0][1])
        self.assertIn("93000", results[0][2])

    def test_missing_id(self):
        cfg = wecom_on(notify_wecom_id="")
        results = notify.send_all(cfg, "t", "m")
        self.assertFalse(results[0][1])
        self.assertIn("机器人 ID", results[0][2])


class MultiChannelTest(unittest.TestCase):
    def test_all_three_push_together(self):
        cfg = ntfy_on(notify_ntfy_token="tk_x")
        cfg.notify_gotify_enabled = True
        cfg.notify_gotify_url = "https://push.example.com"
        cfg.notify_gotify_token = "AppTok123"
        cfg.notify_wecom_enabled = True
        cfg.notify_wecom_id = "k-1"
        with mock.patch.object(notify, "_post_raw", return_value=(True, "")), \
             mock.patch.object(notify, "_post_json",
                               return_value=(True, '{"errcode":0}')):
            results = notify.send_all(cfg, "t", "m")
        self.assertEqual([r[0] for r in results], ["ntfy", "gotify", "wecom"])
        self.assertTrue(all(r[1] for r in results))

    def test_one_failure_does_not_block_others(self):
        cfg = ntfy_on()
        cfg.notify_wecom_enabled = True
        cfg.notify_wecom_id = "k-1"
        with mock.patch.object(notify, "_post_raw", return_value=(True, "")), \
             mock.patch.object(notify, "_post_json",
                               return_value=(True, '{"errcode":93000}')):
            results = notify.send_all(cfg, "t", "m")
        by_ch = dict((r[0], r[1:]) for r in results)
        self.assertTrue(by_ch["ntfy"][0])
        self.assertFalse(by_ch["wecom"][0])

    def test_channel_exception_caught(self):
        cfg = wecom_on()
        with mock.patch.object(notify, "_post_json",
                               side_effect=RuntimeError("boom")):
            results = notify.send_all(cfg, "t", "m")
        self.assertEqual(results[0][0], "wecom")
        self.assertFalse(results[0][1])
        self.assertIn("boom", results[0][2])


class _LocalNetStub:
    """给「本机网卡」装上假数据。

    同步现在固定读本机网卡（/proc/net/if_inet6），测试机上没有那个文件 ——
    不装桩的话每一轮都停在「没发现地址」，通知自然永远不会触发，用例会
    以一种很容易误读的方式失败。
    """

    def setUp(self):
        self._net_saved = (discover.local_candidates, discover.temporary_set)
        discover.local_candidates = lambda: [discover.canon(OLD_ADDR)]
        discover.temporary_set = lambda: set()

    def tearDown(self):
        discover.local_candidates, discover.temporary_set = self._net_saved


class SyncNotifyIntegrationTest(_LocalNetStub, unittest.TestCase):
    """白名单真有写入才发通知；没写入/演练模式/没启用渠道不发。"""

    def _tick(self, cfg):
        r = FakeRouter(entries=[])
        s = Syncer(cfg, r)
        with mock.patch.object(notify, "send_all",
                               return_value=[("wecom", True, "")]) as p:
            s.tick()
        return p

    def test_notify_sent_on_writes(self):
        cfg = wecom_on()
        p = self._tick(cfg)
        self.assertEqual(p.call_count, 1)
        title, msg = p.call_args[0][1], p.call_args[0][2]
        self.assertEqual(title, "IPv6 白名单已更新")
        self.assertIn("NAS", msg)
        self.assertIn(OLD_ADDR, msg)

    def test_custom_title_used(self):
        cfg = wecom_on()
        cfg.notify_title = "家庭 NAS 上线提醒"
        p = self._tick(cfg)
        title, msg = p.call_args[0][1], p.call_args[0][2]
        self.assertEqual(title, "家庭 NAS 上线提醒")
        self.assertIn("NAS", msg)

    def test_no_notify_when_unchanged(self):
        cfg = wecom_on()
        entries = [{"Name": "NAS", "LocalIp": OLD_ADDR,
                    "RemoteIp": "::/0", "Port": -1, "ID": "id1"}]
        s = Syncer(cfg, FakeRouter(entries=entries))
        with mock.patch.object(notify, "send_all",
                               return_value=[("wecom", True, "")]) as p:
            s.tick()
        p.assert_not_called()

    def test_no_notify_when_no_channel(self):
        # 没启用任何渠道：send_all 返回空列表，什么都不推，tick 正常完成
        s = Syncer(make_cfg(), FakeRouter(entries=[]))
        with mock.patch.object(notify, "send_all", return_value=[]) as p:
            snap = s.tick()
        self.assertEqual(p.call_count, 1)
        self.assertTrue(snap["ok"])

    def test_no_notify_in_dry_run(self):
        cfg = wecom_on(dry_run=True)
        p = self._tick(cfg)
        p.assert_not_called()

    def test_notify_failure_never_breaks_tick(self):
        cfg = wecom_on()
        r = FakeRouter(entries=[])
        s = Syncer(cfg, r)
        with mock.patch.object(notify, "send_all",
                               return_value=[("wecom", False, "网络炸了")]):
            snap = s.tick()
        self.assertTrue(snap["ok"])
        self.assertEqual(snap["rules"]["NAS"]["action"], "created")


if __name__ == "__main__":
    unittest.main(verbosity=2)
