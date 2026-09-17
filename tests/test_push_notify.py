"""微信推送模块单元测试。

覆盖：配置归一 / 就绪判定 / 票面一行化 / 消息组装 / 渠道发送（mock 网络，绝不真发）/
每日额度 / 流水线守卫。全部把 STORE_PATH 指向 tmp，不碰真身、不发任何网络请求。
"""

import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data import push_notify as pn


class _TmpStore:
    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = pn.STORE_PATH
        pn.STORE_PATH = Path(self.tmp.name) / "push_notify.json"
        return pn.STORE_PATH

    def __exit__(self, *exc):
        pn.STORE_PATH = self._orig
        self.tmp.cleanup()


def _fake_urlopen(payload):
    """构造 mock urlopen 上下文管理器工厂。"""
    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.close()
            return False
    return lambda req, timeout=None: _Resp(json.dumps(payload).encode("utf-8"))


class ConfigTests(unittest.TestCase):
    def test_defaults(self):
        cfg = pn.sanitize(None)
        self.assertEqual(cfg["provider"], "off")
        self.assertFalse(cfg["enabled"])
        self.assertEqual(cfg["token"], "")
        self.assertTrue(cfg["推号码"])
        self.assertTrue(cfg["推结算"])
        self.assertEqual(cfg["每日上限"], 10)

    def test_invalid_provider_falls_to_off(self):
        cfg = pn.sanitize({"provider": "weixin", "token": "abc"})
        self.assertEqual(cfg["provider"], "off")
        self.assertFalse(cfg["enabled"])

    def test_enabled_requires_valid_provider(self):
        cfg = pn.sanitize({"provider": "pushplus", "token": "abc123", "enabled": True})
        self.assertTrue(cfg["enabled"])
        cfg2 = pn.sanitize({"provider": "off", "token": "abc123", "enabled": True})
        self.assertFalse(cfg2["enabled"])

    def test_daily_limit_clamped(self):
        cfg = pn.sanitize({"每日上限": 999})
        self.assertEqual(cfg["每日上限"], 50)
        cfg2 = pn.sanitize({"每日上限": 0})
        self.assertEqual(cfg2["每日上限"], 1)

    def test_ready_check(self):
        self.assertFalse(pn.effective_ready(pn.sanitize({"provider": "pushplus", "token": "", "enabled": True})))
        self.assertFalse(pn.effective_ready(pn.sanitize({"provider": "pushplus", "token": "t", "enabled": False})))
        self.assertTrue(pn.effective_ready(pn.sanitize({"provider": "pushplus", "token": "t", "enabled": True})))

    def test_roundtrip_preserves_stats(self):
        with _TmpStore():
            pn.save_config({"provider": "pushplus", "token": "t1", "enabled": True,
                            "stats": {"最近推送": {"时间": "x"}}})
            loaded = pn.load_config()
            self.assertEqual(loaded["stats"]["最近推送"]["时间"], "x")
            self.assertEqual(loaded["token"], "t1")


class FormatTests(unittest.TestCase):
    def test_lotto_line(self):
        t = {"号码": {"红球": [3, 7, 12, 21, 24, 28], "蓝球": [5]}}
        self.assertEqual(pn.format_ticket_line(t), "03 07 12 21 24 28 + 05")

    def test_digital_line_concatenates(self):
        t = {"号码": {"第1位": [2], "第2位": [9], "第3位": [2]}}
        self.assertEqual(pn.format_ticket_line(t), "2 9 2")

    def test_seven_star_line(self):
        t = {"号码": {f"第{i}位": [i] for i in range(1, 8)}}
        self.assertEqual(pn.format_ticket_line(t), "1 2 3 4 5 6 7")

    def test_legacy_flat_fallback(self):
        t = {"红球": [1, 2, 3], "蓝球": [4]}
        self.assertEqual(pn.format_ticket_line(t), "01 02 03 + 04")

    def test_empty_ticket(self):
        self.assertEqual(pn.format_ticket_line({}), "?")

    def test_zone_order_for_seven_star(self):
        t = {"号码": {"第2位": [2], "第1位": [1], "第7位": [7], "第10位": [3]}}
        # 第10位 解析为 10 但键名只有 第1位/第2位/第7位/第10位 → 按数值排序 [1,2,7,10→key3]
        self.assertEqual(pn.format_ticket_line(t), "1 2 7 3")


class MessageTests(unittest.TestCase):
    def test_no_content_returns_none(self):
        title, body = pn.build_pipeline_message("双色球", predictions=None, eval_result={"new_feedback_count": 0})
        self.assertIsNone(title)
        self.assertIsNone(body)

    def test_full_message(self):
        preds = [{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}},
                 {"号码": {"红球": [8, 9, 10, 11, 12, 13], "蓝球": [14]}}]
        evalr = {"new_feedback_count": 2}
        hits = [{"issue": "2026106", "prize": "二等奖", "valid": True}]
        title, body = pn.build_pipeline_message(
            "双色球", predictions=preds, eval_result=evalr, hit_summary="🎉 命中 二等奖×1",
            hit_records=hits, draw_numbers={"期号": "2026106", "红球": [1, 2, 3, 4, 5, 6],
                                            "蓝球": [7], "日期": "2026-09-15"},
            pred_info="预测: 2026-09-16 2组", fresh_prediction=True)
        self.assertIn("双色球", title)
        self.assertIn("2026106", body)
        self.assertIn("01 02 03 04 05 06 + 07", body)
        self.assertIn("二等奖", body)
        self.assertIn("仅供研究记录", body)

    def test_truncates_long_ticket_list(self):
        preds = [{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}} for _ in range(15)]
        _, body = pn.build_pipeline_message("双色球", predictions=preds, eval_result={"new_feedback_count": 0})
        self.assertIn("其余 5 注", body)

    def test_honest_footer_always_present(self):
        _, body = pn.build_pipeline_message("排列3", predictions=[{"号码": {"第1位": [1], "第2位": [2], "第3位": [3]}}],
                                            eval_result={"new_feedback_count": 0})
        self.assertIn("期望为负", body)


class SendTests(unittest.TestCase):
    def _ready_cfg(self):
        return pn.sanitize({"provider": "pushplus", "token": "tok123", "enabled": True})

    def test_pushplus_success(self):
        with patch.object(pn.urllib.request, "urlopen", _fake_urlopen({"code": 200, "msg": "ok"})):
            r = pn.send("t", "c", self._ready_cfg())
        self.assertTrue(r["ok"])
        self.assertEqual(r["provider"], "pushplus")

    def test_pushplus_rejected(self):
        with patch.object(pn.urllib.request, "urlopen", _fake_urlopen({"code": 903, "msg": "invalid token"})):
            r = pn.send("t", "c", self._ready_cfg())
        self.assertFalse(r["ok"])
        self.assertIn("invalid", r["detail"])

    def test_serverchan_success(self):
        cfg = pn.sanitize({"provider": "serverchan", "token": "SCT123", "enabled": True})
        seen = {}

        def _capture(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = req.data.decode("utf-8")
            return _fake_urlopen({"code": 0, "message": ""})(req, timeout)

        with patch.object(pn.urllib.request, "urlopen", _capture):
            r = pn.send("t", "c", cfg)
        self.assertTrue(r["ok"])
        self.assertIn("sctapi.ftqq.com/SCT123.send", seen["url"])
        self.assertIn("desp=", seen["body"])

    def test_network_error_never_raises(self):
        def _boom(req, timeout=None):
            raise OSError("network down")
        with patch.object(pn.urllib.request, "urlopen", _boom):
            r = pn.send("t", "c", self._ready_cfg())
        self.assertFalse(r["ok"])
        self.assertIn("network down", r["detail"])

    def test_not_ready_skips(self):
        with patch.object(pn.urllib.request, "urlopen") as m:
            r = pn.send("t", "c", pn.sanitize(None))
        self.assertFalse(r["ok"])
        m.assert_not_called()


class QuotaTests(unittest.TestCase):
    def test_quota_blocks_when_exhausted(self):
        with _TmpStore():
            cfg = pn.save_config({"provider": "pushplus", "token": "t", "enabled": True})
            from datetime import date
            cfg["stats"] = {"日期": date.today().isoformat(), "今日已推": 10}
            pn.save_config(cfg)
            with patch.object(pn.urllib.request, "urlopen") as m:
                r = pn.push_pipeline_result("双色球", predictions=[{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}}],
                                            fresh_prediction=True)
                self.assertIsNone(r)
                m.assert_not_called()

    def test_quota_resets_next_day(self):
        cfg = pn.sanitize({"provider": "pushplus", "token": "t", "enabled": True})
        cfg["stats"] = {"日期": "2000-01-01", "今日已推": 99}
        self.assertTrue(pn._quota_ok(cfg))


class PipelineHookTests(unittest.TestCase):
    def test_disabled_returns_none(self):
        with _TmpStore():
            pn.save_config({"enabled": False})
            with patch.object(pn.urllib.request, "urlopen") as m:
                self.assertIsNone(pn.push_pipeline_result("双色球", fresh_prediction=True))
                m.assert_not_called()

    def test_no_new_content_skips(self):
        with _TmpStore():
            pn.save_config({"provider": "pushplus", "token": "t", "enabled": True})
            with patch.object(pn.urllib.request, "urlopen") as m:
                # 无新结算 + 复用已有出号（fresh_prediction=False）→ 不打扰
                self.assertIsNone(pn.push_pipeline_result(
                    "双色球", eval_result={"new_feedback_count": 0}, fresh_prediction=False))
                m.assert_not_called()

    def test_reused_pending_with_new_eval_still_pushes(self):
        with _TmpStore():
            pn.save_config({"provider": "pushplus", "token": "t", "enabled": True})
            with patch.object(pn.urllib.request, "urlopen",
                              _fake_urlopen({"code": 200, "msg": "ok"})):
                r = pn.push_pipeline_result(
                    "双色球", eval_result={"new_feedback_count": 3}, fresh_prediction=False,
                    hit_summary="本期新增3条预测反馈，均未中奖")
        self.assertIsNotNone(r)
        self.assertTrue(r["ok"])

    def test_exception_never_raises(self):
        with _TmpStore():
            # STORE_PATH 指向不可写位置模拟异常
            pn.STORE_PATH = Path("Z:/nonexistent-dir/x.json")
            r = pn.push_pipeline_result("双色球", predictions=[], fresh_prediction=True)
            # 返回 None 或 ok=False 均可，绝不能抛异常
            self.assertTrue(r is None or r.get("ok") is False)


class WecomTests(unittest.TestCase):
    """企业微信群机器人渠道（2026-09-17 新增）。"""

    def _ready_cfg(self, token="abc-key-123"):
        return pn.sanitize({"provider": "wecom", "token": token, "enabled": True})

    def test_webhook_url_from_bare_key(self):
        self.assertEqual(pn._wecom_webhook_url("my-key"),
                         pn.WECOM_WEBHOOK_BASE + "my-key")

    def test_webhook_url_from_full_url(self):
        url = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xyz"
        self.assertEqual(pn._wecom_webhook_url(url), url)

    def test_ready_check_wecom(self):
        self.assertTrue(pn.effective_ready(self._ready_cfg()))
        self.assertFalse(pn.effective_ready(self._ready_cfg(token="")))

    def test_send_markdown_success(self):
        seen = {}

        def _capture(req, timeout=None):
            seen["url"] = req.full_url
            seen["body"] = json.loads(req.data.decode("utf-8"))
            return _fake_urlopen({"errcode": 0, "errmsg": "ok"})(req, timeout)

        with patch.object(pn.urllib.request, "urlopen", _capture):
            r = pn.send("彩票助手｜双色球", "**本期出号** 共 2 注\n1. 01 02", self._ready_cfg())
        self.assertTrue(r["ok"])
        self.assertEqual(r["provider"], "wecom")
        self.assertEqual(seen["body"]["msgtype"], "markdown")
        self.assertIn("本期出号", seen["body"]["markdown"]["content"])
        self.assertIn("key=abc-key-123", seen["url"])

    def test_send_errcode_failure(self):
        with patch.object(pn.urllib.request, "urlopen",
                          _fake_urlopen({"errcode": 93000, "errmsg": "invalid webhook url"})):
            r = pn.send("t", "c", self._ready_cfg())
        self.assertFalse(r["ok"])
        self.assertIn("invalid webhook url", r["detail"])

    def test_network_error_never_raises(self):
        def _boom(req, timeout=None):
            raise OSError("dns fail")
        with patch.object(pn.urllib.request, "urlopen", _boom):
            r = pn.send("t", "c", self._ready_cfg())
        self.assertFalse(r["ok"])
        self.assertIn("dns fail", r["detail"])

    def test_render_converts_bullets_and_adds_title(self):
        out = pn._wecom_render("标题", "**结算**\n- 命中 2 注\n  - 2026104 期 六等奖")
        self.assertTrue(out.startswith("# 标题\n"))
        self.assertIn("· 命中 2 注", out)
        self.assertIn("　· 2026104 期 六等奖", out)   # 子项缩进转全角，保持层级
        self.assertIn("**结算**", out)

    def test_render_keeps_quote_footer(self):
        out = pn._wecom_render("t", "> 仅供研究记录；期望为负")
        self.assertIn("> 仅供研究记录", out)

    def test_truncate_bytes_safe_for_chinese(self):
        out = pn._truncate_bytes("中" * 5000, pn.WECOM_MAX_BYTES)   # 15000 字节
        self.assertLessEqual(len(out.encode("utf-8")), pn.WECOM_MAX_BYTES)
        self.assertIn("已截断", out)

    def test_long_message_truncated_in_render(self):
        out = pn._wecom_render("t", "x" * 6000)
        self.assertLessEqual(len(out.encode("utf-8")), pn.WECOM_MAX_BYTES)
        self.assertTrue(out.startswith("# t\n"))


class MessageContentTests(unittest.TestCase):
    """推送正文必须覆盖三块：出号号码 / 出号数量 / 中奖结算（2026-09-17）。"""

    _ONE = {"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}

    def test_ticket_count_in_head(self):
        preds = [dict(self._ONE) for _ in range(3)]
        _, body = pn.build_pipeline_message("双色球", predictions=preds,
                                            eval_result={"new_feedback_count": 0})
        self.assertIn("共 3 注", body)

    def test_size_note_rendered(self):
        _, body = pn.build_pipeline_message(
            "双色球", predictions=[dict(self._ONE)], eval_result={"new_feedback_count": 0},
            size_note="出号数量：动态滑轨，当前生效 38 注")
        self.assertIn("动态滑轨", body)
        self.assertIn("38 注", body)

    def test_board_url_link(self):
        _, body = pn.build_pipeline_message(
            "双色球", predictions=[dict(self._ONE)], eval_result={"new_feedback_count": 0},
            board_url="http://100.64.0.5:5000")
        self.assertIn("[打开看板", body)
        self.assertIn("http://100.64.0.5:5000", body)

    def test_no_board_url_when_empty(self):
        _, body = pn.build_pipeline_message("双色球", predictions=[dict(self._ONE)],
                                            eval_result={"new_feedback_count": 0})
        self.assertNotIn("打开看板", body)

    def test_size_note_never_raises(self):
        self.assertIsInstance(pn._size_note("双色球", 5), str)
        self.assertEqual(pn._size_note("双色球", 0), "")

    def test_board_address_roundtrip(self):
        with _TmpStore():
            pn.save_config({"provider": "wecom", "token": "k", "enabled": True,
                            "看板地址": "http://100.64.0.5:5000"})
            self.assertEqual(pn.load_config()["看板地址"], "http://100.64.0.5:5000")


class WecomPipelineTests(unittest.TestCase):
    def test_end_to_end_wecom(self):
        with _TmpStore():
            pn.save_config({"provider": "wecom", "token": "k1", "enabled": True,
                            "看板地址": "http://100.64.0.5:5000"})
            seen = {}

            def _capture(req, timeout=None):
                seen["body"] = json.loads(req.data.decode("utf-8"))
                return _fake_urlopen({"errcode": 0, "errmsg": "ok"})(req, timeout)

            with patch.object(pn.urllib.request, "urlopen", _capture):
                r = pn.push_pipeline_result(
                    "双色球",
                    predictions=[{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}],
                    eval_result={"new_feedback_count": 1},
                    hit_summary="命中 六等奖×1",
                    hit_records=[{"issue": "2026104", "prize": "六等奖", "valid": True}],
                    draw_numbers={"期号": "2026104", "红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]},
                    fresh_prediction=True)
        self.assertIsNotNone(r)
        self.assertTrue(r["ok"])
        content = seen["body"]["markdown"]["content"]
        self.assertIn("彩票助手｜双色球", content)   # 标题并入正文
        self.assertIn("本期出号", content)           # ① 号码
        self.assertIn("共 1 注", content)            # ② 数量
        self.assertIn("六等奖", content)             # ③ 中奖记录
        self.assertIn("100.64.0.5", content)         # 看板深链


if __name__ == "__main__":
    unittest.main()
