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

from data import feedback as pn_fb
from data import push_image as pi
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
        self.assertTrue(cfg["图片推送"])          # 2026-09-18：默认走图片
        self.assertEqual(cfg["每日上限"], 0)       # 0 = 不限制
        self.assertGreater(cfg["推送间隔秒"], 0)

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
        # 2026-09-18：取消每日上限 → 0 合法（= 不限制），不再钳成 1
        cfg = pn.sanitize({"每日上限": 100001})
        self.assertEqual(cfg["每日上限"], 100000)
        cfg2 = pn.sanitize({"每日上限": 0})
        self.assertEqual(cfg2["每日上限"], 0)
        cfg3 = pn.sanitize({"每日上限": -5})
        self.assertEqual(cfg3["每日上限"], 0)

    def test_pace_interval_clamped(self):
        self.assertEqual(pn.sanitize({"推送间隔秒": -1})["推送间隔秒"], 0.0)
        self.assertEqual(pn.sanitize({"推送间隔秒": 999})["推送间隔秒"], 60.0)
        self.assertEqual(pn.sanitize({"推送间隔秒": "x"})["推送间隔秒"], pn.DEFAULTS["推送间隔秒"])

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
    def test_unlimited_by_default(self):
        """每日上限默认 0 = 不限制（2026-09-18 用户确认取消）。"""
        cfg = pn.sanitize({"provider": "pushplus", "token": "t", "enabled": True})
        from datetime import date
        cfg["stats"] = {"日期": date.today().isoformat(), "今日已推": 99999}
        self.assertTrue(pn._quota_ok(cfg))

    def test_quota_blocks_when_exhausted(self):
        with _TmpStore():
            cfg = pn.save_config({"provider": "pushplus", "token": "t", "enabled": True,
                                  "每日上限": 10})
            from datetime import date
            cfg["stats"] = {"日期": date.today().isoformat(), "今日已推": 10}
            pn.save_config(cfg)
            with patch.object(pn.urllib.request, "urlopen") as m:
                r = pn.push_pipeline_result("双色球", predictions=[{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}}],
                                            fresh_prediction=True)
                self.assertIsNone(r)
                m.assert_not_called()

    def test_quota_resets_next_day(self):
        cfg = pn.sanitize({"provider": "pushplus", "token": "t", "enabled": True,
                           "每日上限": 10})
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

    def setUp(self):
        pn._reset_pace()          # 关掉节流，避免测试互相拖慢

    def _ready_cfg(self, token="abc-key-123"):
        return pn.sanitize({"provider": "wecom", "token": token, "enabled": True,
                            "推送间隔秒": 0})

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
    def setUp(self):
        pn._reset_pace()

    def test_end_to_end_wecom(self):
        """端到端（图片模式）：短文字 markdown + 号码图，全部走通。"""
        with _TmpStore():
            pn.save_config({"provider": "wecom", "token": "k1", "enabled": True,
                            "推送间隔秒": 0,
                            "看板地址": "http://100.64.0.5:5000"})
            bodies = []

            def _capture(req, timeout=None):
                bodies.append(json.loads(req.data.decode("utf-8")))
                return _fake_urlopen({"errcode": 0, "errmsg": "ok"})(req, timeout)

            with patch.object(pn.urllib.request, "urlopen", _capture):
                r = pn.push_pipeline_result(
                    "双色球",
                    predictions=[{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}],
                    eval_result={"new_feedback_count": 1},
                    hit_summary="命中 六等奖×1",
                    hit_records=[{"issue": "2026104", "prize": "六等奖", "valid": True,
                                  "num": "01 02 03 04 05 06 + 07", "match": "红中2 蓝中1"}],
                    draw_numbers={"期号": "2026104", "红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]},
                    pred_info="目标期号 2026105",
                    fresh_prediction=True)
        self.assertIsNotNone(r)
        self.assertTrue(r["ok"])
        # ① 出号文字 ② 号码图 ③ 结算文字 ④ 中奖图
        texts = [b["markdown"]["content"] for b in bodies if b.get("msgtype") == "markdown"]
        images = [b for b in bodies if b.get("msgtype") == "image"]
        self.assertEqual(len(images), 2)
        joined = "\n".join(texts)
        self.assertIn("彩票助手｜双色球", joined)     # 标题并入正文
        self.assertIn("本期出号", joined)             # ① 号码
        self.assertIn("共 1 注", joined)              # ② 数量
        self.assertIn("六等奖", joined)               # ③ 中奖记录
        self.assertIn("100.64.0.5", joined)           # 看板深链

    def test_image_payload_has_base64_and_md5(self):
        with _TmpStore():
            cfg = pn.save_config({"provider": "wecom", "token": "k1", "enabled": True,
                                  "推送间隔秒": 0})
            png = b"\x89PNG\r\n\x1a\n" + b"payload"
            seen = {}

            def _capture(req, timeout=None):
                seen["body"] = json.loads(req.data.decode("utf-8"))
                return _fake_urlopen({"errcode": 0, "errmsg": "ok"})(req, timeout)

            with patch.object(pn.urllib.request, "urlopen", _capture):
                r = pn.send_image(png, pn.sanitize(cfg))
        self.assertTrue(r["ok"])
        self.assertEqual(seen["body"]["msgtype"], "image")
        import base64 as _b64
        import hashlib as _hl
        self.assertEqual(_b64.b64decode(seen["body"]["image"]["base64"]), png)
        self.assertEqual(seen["body"]["image"]["md5"], _hl.md5(png).hexdigest())

    def test_no_new_content_skips(self):
        """既无号码又无结算 → 不发空消息（唯一保留的门槛）。"""
        with _TmpStore():
            pn.save_config({"provider": "wecom", "token": "k", "enabled": True,
                            "推送间隔秒": 0})
            with patch.object(pn.urllib.request, "urlopen") as m:
                r = pn.push_pipeline_result("双色球", eval_result={"new_feedback_count": 0})
                self.assertIsNone(r)
                m.assert_not_called()

    def test_strict_mode_pushes_reused_numbers(self):
        """严格模式：复用 pending（fresh_prediction=False）也要推（2026-09-18）。"""
        with _TmpStore():
            pn.save_config({"provider": "wecom", "token": "k", "enabled": True,
                            "推送间隔秒": 0})
            with patch.object(pn.urllib.request, "urlopen",
                              _fake_urlopen({"errcode": 0, "errmsg": "ok"})):
                r = pn.push_pipeline_result(
                    "双色球", eval_result={"new_feedback_count": 0},
                    predictions=[{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}],
                    fresh_prediction=False)
        self.assertIsNotNone(r)
        self.assertTrue(r["ok"])


class ImageRenderTests(unittest.TestCase):
    """图片渲染（data/push_image.py）：合法 PNG、乐透/数字型通吃、空输入报错。"""

    def test_numbers_png_for_lotto(self):
        lines = [f"{i:02d} {i+1:02d} {i+2:02d} {i+3:02d} {i+4:02d} {i+5:02d} + {i % 16 + 1:02d}"
                 for i in range(1, 51)]
        png = pi.render_numbers_image(lines, title="双色球 本期出号", subtitle="共 50 注")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertGreater(len(png), 2000)

    def test_numbers_png_for_digital(self):
        png = pi.render_numbers_image(["1 2 3", "4 5 6"], title="排列三 本期出号")
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_numbers_many_rows_still_valid(self):
        png = pi.render_numbers_image([f"{i:03d}" for i in range(120)], title="t")
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_wins_png(self):
        recs = [{"issue": "26104", "num": "01 02 03 04 05 06 + 07",
                 "match": "红中5 蓝中0", "prize": "四等奖"}]
        png = pi.render_wins_image(recs, title="双色球 开奖结算", subtitle="命中 四等×1")
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_wins_row_max_note(self):
        recs = [{"issue": f"26{i:03d}", "num": "1 2 3", "match": "", "prize": "九等奖"}
                for i in range(10)]
        png = pi.render_wins_image(recs, title="大乐透 开奖结算", row_max=3)
        self.assertTrue(png.startswith(b"\x89PNG"))

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            pi.render_numbers_image([], title="t")
        with self.assertRaises(ValueError):
            pi.render_wins_image([], title="t")

    def test_disclaimer_constant_present(self):
        self.assertIn("期望为负", pi.DISCLAIMER)

    def test_emoji_stripped(self):
        """中文字体没有 emoji 字形，画进去会变豆腐块 → 渲染前剔除。"""
        self.assertEqual(pi._clean("🎉 命中 六等×14"), "命中 六等×14")
        self.assertEqual(pi._clean("✅ A  ✅ B"), "A B")
        self.assertEqual(pi._clean("纯中文标题"), "纯中文标题")

    def test_emoji_subtitle_renders_without_missing_glyph(self):
        import warnings
        recs = [{"issue": "26104", "num": "01 02 03", "match": "红中1", "prize": "六等奖"}]
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            png = pi.render_wins_image(recs, title="双色球 开奖结算",
                                       subtitle="🎉 命中 六等×1")
        self.assertTrue(png.startswith(b"\x89PNG"))
        self.assertFalse([x for x in caught if "Glyph" in str(x.message)])


class TicketImageTests(unittest.TestCase):
    """票面图（render_tickets_image，2026-09-18）：看板弹窗同款排版。

    核心：球珠+分组渲染合法 PNG；分组/排序逻辑与看板 _bundleIntoGroups 同口径
    （每组5注、组内置信度降序、组间按最高置信度降序、Top1 组 recommended）；
    票面缺 `号码` 结构 → ValueError（push_notify 回退旧行式渲染）。
    """

    @staticmethod
    def _ticket(zone: str, nums, strat="高频策略", conf=0.35):
        return {"号码": {zone: list(nums)}, "策略": strat, "置信度": conf,
                "类型": "single"}

    def test_lotto_and_digital_render(self):
        lotto = [self._ticket("红球", [1, 2, 3, 4, 5, 6], conf=0.3 + i * 0.001)
                 for i in range(12)]
        for t in lotto:
            t["号码"]["蓝球"] = [7]
        png = pi.render_tickets_image(lotto, title="双色球 · 本期出号",
                                      subtitle="共 12 注",
                                      meta_lines=["第 26109 期 · 目标开奖 2026-09-22"])
        self.assertTrue(png.startswith(b"\x89PNG"))
        digital = [self._ticket("第1位", [d % 10], conf=0.3) for d in range(7)]
        png2 = pi.render_tickets_image(digital, title="七星彩 · 本期出号")
        self.assertTrue(png2.startswith(b"\x89PNG"))

    def test_bundle_semantics_match_board(self):
        ts = [self._ticket("第1位", [1], conf=0.30 + i * 0.01) for i in range(7)]
        ts += [self._ticket("第1位", [2], conf=0.99)]      # 最高置信度票在第3组
        groups = pi._bundle_tickets(ts, 5)
        self.assertEqual(len(groups), 2)
        self.assertTrue(groups[0]["recommended"])
        self.assertFalse(groups[1]["recommended"])
        # Top1 组 = 含 0.99 置信度票的组（第2组），其组内按置信度降序
        self.assertEqual(float(groups[0]["tickets"][0]["置信度"]), 0.99)
        self.assertEqual(len(groups[0]["tickets"]), 3)     # 8 注 → 5+3，Top组 3 张
        self.assertEqual(len(groups[1]["tickets"]), 5)

    def test_missing_zone_raises(self):
        with self.assertRaises(ValueError):
            pi.render_tickets_image([{"策略": "x"}], title="t")
        with self.assertRaises(ValueError):
            pi.render_tickets_image([], title="t")

    def test_star_survives_clean(self):
        """★/◎ 是排版必需符号，白名单保留；emoji 仍剔除。"""
        self.assertEqual(pi._clean("★ 推荐使用"), "★ 推荐使用")
        self.assertEqual(pi._clean("🎉 命中"), "命中")


class PaceTests(unittest.TestCase):
    """企微「每机器人 20 条/分钟」节流（2026-09-18）。"""

    def setUp(self):
        pn._reset_pace()

    def tearDown(self):
        pn._reset_pace()

    def test_pace_sleeps_remaining_interval(self):
        cfg = pn.sanitize({"provider": "wecom", "token": "k", "enabled": True,
                           "推送间隔秒": 3.2})
        clock = {"t": 1000.0}
        slept = []

        def _mono():
            return clock["t"]

        def _sleep(sec):
            slept.append(sec)
            clock["t"] += sec

        with patch.object(pn.time, "monotonic", _mono), \
                patch.object(pn.time, "sleep", _sleep):
            pn._pace(cfg)                      # 首次：无历史 → 不睡
            self.assertEqual(slept, [])
            pn._pace(cfg)                      # 紧接一次 → 补足 3.2s
        self.assertEqual(len(slept), 1)
        self.assertAlmostEqual(slept[0], 3.2, places=4)

    def test_pace_disabled_when_zero(self):
        cfg = pn.sanitize({"推送间隔秒": 0})
        with patch.object(pn.time, "sleep") as m:
            pn._pace(cfg)
            pn._pace(cfg)
            m.assert_not_called()

    def test_send_applies_pace_for_wecom_only(self):
        cfg = pn.sanitize({"provider": "wecom", "token": "k", "enabled": True})
        with patch.object(pn, "_pace") as mp, patch.object(
                pn.urllib.request, "urlopen", _fake_urlopen({"errcode": 0, "errmsg": "ok"})):
            pn.send("t", "c", cfg)
        mp.assert_called_once()

        cfg2 = pn.sanitize({"provider": "pushplus", "token": "k", "enabled": True})
        with patch.object(pn, "_pace") as mp2, patch.object(
                pn.urllib.request, "urlopen", _fake_urlopen({"code": 200, "msg": "ok"})):
            pn.send("t", "c", cfg2)
        mp2.assert_not_called()

    def test_send_image_applies_pace(self):
        cfg = pn.sanitize({"provider": "wecom", "token": "k", "enabled": True})
        with patch.object(pn, "_pace") as mp, patch.object(
                pn.urllib.request, "urlopen", _fake_urlopen({"errcode": 0, "errmsg": "ok"})):
            r = pn.send_image(b"\x89PNG-fake", cfg)
        self.assertTrue(r["ok"])
        mp.assert_called_once()

    def test_image_too_large_rejected(self):
        cfg = pn.sanitize({"provider": "wecom", "token": "k", "enabled": True})
        with patch.object(pn.urllib.request, "urlopen") as m:
            r = pn.send_image(b"x" * (pn.WECOM_MAX_IMAGE_BYTES + 1), cfg)
        self.assertFalse(r["ok"])
        m.assert_not_called()


class DegradeTests(unittest.TestCase):
    """图片失败 → 回退文本；推送异常绝不抛出（2026-09-18）。"""

    _ONE = {"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}

    def setUp(self):
        pn._reset_pace()

    def _wecom_cfg(self):
        return pn.sanitize({"provider": "wecom", "token": "k", "enabled": True,
                            "推送间隔秒": 0})

    def test_render_failure_falls_back_to_text(self):
        sent = []

        def _boom(*a, **kw):
            raise RuntimeError("font missing")

        def _capture(req, timeout=None):
            sent.append(json.loads(req.data.decode("utf-8")))
            return _fake_urlopen({"errcode": 0, "errmsg": "ok"})(req, timeout)

        with patch.object(pi, "render_tickets_image", _boom), \
                patch.object(pi, "render_numbers_image", _boom), \
                patch.object(pn.urllib.request, "urlopen", _capture):
            res = pn.push_lottery_images("双色球", predictions=[dict(self._ONE)],
                                        cfg=self._wecom_cfg())
        self.assertTrue(any(r.get("ok") for r in res))
        kinds = [b.get("msgtype") for b in sent]
        self.assertIn("markdown", kinds)
        self.assertNotIn("image", kinds)
        text = "\n".join(b["markdown"]["content"] for b in sent
                         if b.get("msgtype") == "markdown")
        self.assertIn("本期出号", text)

    def test_unsupported_provider_falls_back_to_text(self):
        """pushplus / serverchan 不支持图片消息 → 自动回退 markdown。"""
        cfg = pn.sanitize({"provider": "pushplus", "token": "k", "enabled": True})
        with patch.object(pn.urllib.request, "urlopen",
                          _fake_urlopen({"code": 200, "msg": "ok"})):
            res = pn.push_lottery_images("双色球", predictions=[dict(self._ONE)], cfg=cfg)
        self.assertTrue(any(r.get("ok") for r in res))

    def test_images_disabled_by_config(self):
        cfg = pn.sanitize({"provider": "wecom", "token": "k", "enabled": True,
                           "推送间隔秒": 0, "图片推送": False})
        seen = []

        def _capture(req, timeout=None):
            seen.append(json.loads(req.data.decode("utf-8")))
            return _fake_urlopen({"errcode": 0, "errmsg": "ok"})(req, timeout)

        with patch.object(pn.urllib.request, "urlopen", _capture):
            res = pn.push_lottery_images("双色球", predictions=[dict(self._ONE)], cfg=cfg)
        self.assertTrue(any(r.get("ok") for r in res))
        self.assertTrue(all(b.get("msgtype") == "markdown" for b in seen))

    def test_push_exception_never_raises(self):
        with _TmpStore():
            pn.save_config({"provider": "wecom", "token": "k", "enabled": True})
            with patch.object(pn, "push_lottery_images", side_effect=RuntimeError("boom")):
                r = pn.push_pipeline_result("双色球", predictions=[dict(self._ONE)])
        self.assertFalse(r["ok"])
        self.assertIn("boom", r["detail"])


class SelfCheckPushTests(unittest.TestCase):
    """项目体检 / 启动自检推送（2026-09-18 新增）。

    ⚠️ 这些入口推送成功后会在真身路径 `save_config()`（记 stats），
       所以**必须**在 `_TmpStore()` 里跑，否则会覆盖 config/push_notify.json。
       （2026-09-18 真的把用户的 webhook token 覆盖成 "k" 过一次。）
    """

    def setUp(self):
        pn._reset_pace()

    def _cfg(self):
        return pn.sanitize({"provider": "wecom", "token": "k", "enabled": True,
                            "推送间隔秒": 0})

    def _capture(self, seen):
        def _fn(req, timeout=None):
            seen.append(json.loads(req.data.decode("utf-8")))
            return _fake_urlopen({"errcode": 0, "errmsg": "ok"})(req, timeout)
        return _fn

    def test_health_summary_lists_problems(self):
        seen = []
        with _TmpStore():
            with patch.object(pn.urllib.request, "urlopen", self._capture(seen)):
                r = pn.push_health_summary(
                    [("双色球", "真缺期 26100"), ("系统", "写保护不可用")],
                    report_path="E:/707/logs/项目体检_20260918.md", cfg=self._cfg())
        self.assertTrue(r["ok"])
        text = seen[0]["markdown"]["content"]
        self.assertIn("真实待处理问题", text)
        self.assertIn("2 项", text)
        self.assertIn("真缺期 26100", text)
        self.assertIn("项目体检_20260918", text)

    def test_health_summary_all_clear(self):
        seen = []
        with _TmpStore():
            with patch.object(pn.urllib.request, "urlopen", self._capture(seen)):
                r = pn.push_health_summary([], cfg=self._cfg())
        self.assertTrue(r["ok"])
        self.assertIn("0 项", seen[0]["markdown"]["content"])

    def test_recovery_summary_reports_fetched_and_errors(self):
        seen = []
        results = {"双色球": {"fetched": 2, "latest_before": "26106", "latest_after": "26108"},
                   "大乐透": {"fetched": 0},
                   "七星彩": {"error": "timeout"}}
        with _TmpStore():
            with patch.object(pn.urllib.request, "urlopen", self._capture(seen)):
                r = pn.push_recovery_summary(results, cfg=self._cfg())
        self.assertTrue(r["ok"])
        text = seen[0]["markdown"]["content"]
        self.assertIn("双色球", text)
        self.assertIn("26108", text)
        self.assertIn("失败 1 个", text)

    def test_recovery_summary_no_change_still_pushes(self):
        """严格模式：无变化也推（用户要求「每个自检跑完都推一次」）。"""
        seen = []
        with _TmpStore():
            with patch.object(pn.urllib.request, "urlopen", self._capture(seen)):
                r = pn.push_recovery_summary({"双色球": {"fetched": 0}}, cfg=self._cfg())
        self.assertTrue(r["ok"])
        self.assertIn("无彩种滞后", seen[0]["markdown"]["content"])

    def test_not_ready_returns_none(self):
        with _TmpStore():
            self.assertIsNone(pn.push_health_summary([], cfg=pn.sanitize(None)))
            self.assertIsNone(pn.push_recovery_summary({}, cfg=pn.sanitize(None)))

    def test_stats_written_to_store(self):
        """统计确实落盘（用的是临时 store，不是真身）。"""
        with _TmpStore():
            with patch.object(pn.urllib.request, "urlopen",
                              self._capture([])):
                pn.push_health_summary([], cfg=self._cfg())
            self.assertEqual(pn.load_config()["stats"]["最近推送"]["结果"], "成功")


def _src_of(path_rel: str, marker: str) -> str:
    """取项目内某文件 marker 之后的源码片段（用于接线自检，避免导入重模块）。"""
    p = Path(__file__).resolve().parent.parent / path_rel
    text = p.read_text(encoding="utf-8-sig")
    self_assert = text.find(marker)
    assert self_assert >= 0, f"{path_rel} 里找不到 {marker}"
    return text[self_assert:]


class WiringTests(unittest.TestCase):
    """接线自检：所有自动化/自检入口都必须调用推送。

    2026-09-18 的缺口：Windows 计划任务走 `cli.py auto` 那条路**完全没有推送**
    （只有 Flask 内部流水线有），以及 `health_check.py` 从无自动调用者。
    """

    def test_cli_auto_pushes(self):
        src = _src_of("cli.py", "def _cmd_auto(")
        self.assertIn("push_pipeline_result", src)
        self.assertIn("fresh_prediction", src)

    def test_cli_auto_hit_records_carry_num_and_match(self):
        src = _src_of("cli.py", "def _cmd_auto(")
        self.assertIn("_fmt_feedback_ticket", src)
        self.assertIn("_fmt_match", src)

    def test_health_check_pushes(self):
        src = _src_of("scripts/health_check.py", "def main(")
        self.assertIn("push_health_summary", src)
        self.assertIn("real_problems", src)

    def test_startup_recovery_pushes(self):
        src = _src_of("web/startup_recovery.py", "def run_startup_recovery(")
        self.assertIn("push_recovery_summary", src)
        self.assertIn("recover_all_lotteries", src)

    def test_ps1_runs_health_check_in_nightly(self):
        src = _src_of("scripts/auto_scheduled.ps1", "if ($Daytime) {")
        full = (Path(__file__).resolve().parent.parent / "scripts" / "auto_scheduled.ps1"
                ).read_text(encoding="utf-8-sig")
        self.assertIn("health_check.py", full)
        self.assertIn("-not $Daytime", full)
        self.assertIn("PYTHONUTF8", full)
        self.assertTrue(src)


class LotteryNameTests(unittest.TestCase):
    """彩种规范名（2026-09-18 修）。

    曾误把 ALL_LOTTERIES 写成「排列三 / 排列五 / 3D」，而全项目规范名是
    「排列3 / 排列5 / 福彩3D」。名字对不上 → load_pending/load_feedback_history
    按名取文件全部落空（返回 0），这 3 个彩种在全彩种简报里被**静默跳过**且不报错。
    """

    def test_all_lotteries_match_config_canonical_names(self):
        import config
        for name in pn.ALL_LOTTERIES:
            self.assertIn(name, config.LOTTERY_CONFIG, f"{name!r} 不是 config 的规范彩种名")
        self.assertEqual(len(pn.ALL_LOTTERIES), len(set(pn.ALL_LOTTERIES)))

    def test_canonical_lottery_aliases(self):
        cases = {"排列三": "排列3", "排列五": "排列5", "3D": "福彩3D",
                 "福彩3d": "福彩3D", "双色球": "双色球", "大乐透": "大乐透"}
        for alias, canon in cases.items():
            self.assertEqual(pn.canonical_lottery(alias), canon)
        self.assertEqual(pn.canonical_lottery("  排列三  "), "排列3")
        self.assertEqual(pn.canonical_lottery(""), "")
        self.assertEqual(pn.canonical_lottery(None), "")

    def test_fmt_match_lotto_prefers_red_blue(self):
        self.assertEqual(pn._fmt_match({"红球命中": 5, "蓝球命中": 1}), "红中5 蓝中1")
        # 有红/蓝时不应退回「总命中」（否则会变成"命中6位"）
        self.assertEqual(pn._fmt_match({"红球命中": 5, "总命中": 6}), "红中5")

    def test_fmt_match_numeric_falls_back_to_total_hit(self):
        self.assertEqual(pn._fmt_match({"总命中": 3}), "命中3位")
        self.assertEqual(pn._fmt_match({}), "")

    def test_grade_of_dual_keys(self):
        self.assertEqual(pn._grade_of({"中奖等级": "六等"}), "六等")
        self.assertEqual(pn._grade_of({"中奖玩法": "直选"}), "直选")
        self.assertEqual(pn._grade_of({"中奖等级": "六等", "中奖玩法": "直选"}), "六等")
        self.assertEqual(pn._grade_of({}), "")

    def test_digest_payload_normalizes_alias_before_reading(self):
        seen = {}

        def _pending(name):
            seen["pending"] = name
            return []

        def _hist(name, lookback=None):
            seen["hist"] = name
            return []

        with patch.object(pn_fb, "load_pending", _pending), \
                patch.object(pn_fb, "load_feedback_history", _hist):
            payload = pn.digest_payload("排列三")
        self.assertIsNone(payload)                     # 无 pending 也无中奖 → None
        self.assertEqual(seen["pending"], "排列3")      # 别名已归一，能落到正确文件
        self.assertEqual(seen["hist"], "排列3")

    def test_digest_payload_counts_numeric_wins(self):
        rec = {"期号": 26236, "预测号码": {"第1位": [1], "第2位": [7], "第3位": [3]},
               "总命中": 3, "中奖等级": "直选", "中奖玩法": "直选", "valid_prediction": True}
        with patch.object(pn_fb, "load_pending", lambda n: []), \
                patch.object(pn_fb, "load_feedback_history", lambda n, lookback=None: [rec]):
            payload = pn.digest_payload("排列3")
        self.assertIsNotNone(payload)
        self.assertEqual(payload["eval_result"]["new_feedback_count"], 1)
        self.assertIn("直选", payload["hit_summary"])
        self.assertEqual(payload["hit_records"][0]["prize"], "直选")
        self.assertEqual(payload["hit_records"][0]["match"], "命中3位")


if __name__ == "__main__":
    unittest.main()
