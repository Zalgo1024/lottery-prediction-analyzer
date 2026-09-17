"""出号数量配置（固定/动态滑轨）单元测试。

覆盖：
  - sanitize 归一化与钳制（数字型/乐透型上限不同）
  - effective_count 固定/动态两模式
  - resolve_groups 三级优先级（显式 requested > 配置 > 彩种默认）
  - 动态策略 decide_next_count（高冗余下调 / 低冗余微降 / 命中率偏离回补 / 硬下限）
  - 持久化原子写读回
  - API GET/POST/apply/reset（含非法输入）

所有测试把 store 指向临时文件，绝不触碰 config/ticket_size.json 真身。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from data import ticket_size as ts


class _TmpStore:
    """把 STORE_PATH 临时指向 tmp 目录的上下文管理器。"""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = ts.STORE_PATH
        ts.STORE_PATH = Path(self.tmp.name) / "ticket_size.json"
        return ts.STORE_PATH

    def __exit__(self, *exc):
        ts.STORE_PATH = self._orig
        self.tmp.cleanup()


class SanitizeTests(unittest.TestCase):
    def test_empty_input_gives_defaults(self):
        cfg = ts.sanitize(None)
        self.assertEqual(cfg["mode"], "fixed")
        for lot in ("双色球", "大乐透", "七星彩"):
            self.assertEqual(cfg["fixed"][lot], 100)
        for lot in ("福彩3D", "排列3", "排列5"):
            self.assertEqual(cfg["fixed"][lot], 5)

    def test_clamp_digital_and_lotto_upper_bounds(self):
        cfg = ts.sanitize({"mode": "fixed", "fixed": {"双色球": 999, "福彩3D": 999}})
        self.assertEqual(cfg["fixed"]["双色球"], ts.MAX_TICKETS_LOTTO)
        self.assertEqual(cfg["fixed"]["福彩3D"], ts.MAX_TICKETS_DIGITAL)

    def test_lower_bound_is_one(self):
        cfg = ts.sanitize({"mode": "fixed", "fixed": {"双色球": 0, "福彩3D": -5}})
        self.assertEqual(cfg["fixed"]["双色球"], 1)
        self.assertEqual(cfg["fixed"]["福彩3D"], 1)

    def test_invalid_mode_falls_back_to_fixed(self):
        cfg = ts.sanitize({"mode": "whatever"})
        self.assertEqual(cfg["mode"], "fixed")

    def test_unknown_lottery_ignored(self):
        cfg = ts.sanitize({"mode": "fixed", "fixed": {"不存在的彩种": 50}})
        self.assertNotIn("不存在的彩种", cfg["fixed"])


class EffectiveCountTests(unittest.TestCase):
    def test_fixed_mode_uses_fixed(self):
        cfg = ts.sanitize({"mode": "fixed", "fixed": {"双色球": 30}})
        self.assertEqual(ts.effective_count("双色球", cfg), 30)

    def test_dynamic_mode_uses_dynamic(self):
        cfg = ts.sanitize({"mode": "dynamic", "fixed": {"双色球": 30},
                           "dynamic": {"双色球": 12}})
        self.assertEqual(ts.effective_count("双色球", cfg), 12)

    def test_dynamic_missing_falls_back_to_fixed(self):
        cfg = ts.sanitize({"mode": "dynamic", "fixed": {"双色球": 30}})
        # dynamic 未显式给 → sanitize 时以 fixed 填充
        self.assertEqual(ts.effective_count("双色球", cfg), 30)


class ResolveGroupsTests(unittest.TestCase):
    def test_requested_overrides_everything(self):
        with _TmpStore():
            ts.save_config({"mode": "fixed", "fixed": {"双色球": 7}})
            from config import resolve_groups
            self.assertEqual(resolve_groups("双色球", 123), 123)

    def test_config_beats_lottery_default(self):
        with _TmpStore():
            ts.save_config({"mode": "fixed", "fixed": {"双色球": 42}})
            from config import resolve_groups
            self.assertEqual(resolve_groups("双色球"), 42)

    def test_disabled_control_falls_back_to_default(self):
        with _TmpStore():
            ts.save_config({"mode": "fixed", "fixed": {"双色球": 42}})
            import config
            with patch.object(config, "TICKET_SIZE_CONTROL_ENABLED", False):
                self.assertEqual(config.resolve_groups("双色球"), config.AUTO_GROUPS)
                self.assertEqual(config.resolve_groups("福彩3D"), config.DIGITAL_GROUPS)


class DecideNextCountTests(unittest.TestCase):
    def test_high_redundancy_steps_down(self):
        d = ts.decide_next_count("双色球", current=40, last_kpi={
            "启用": True, "压缩前注数": 100, "质量淘汰": 20, "重复淘汰": 10, "重叠淘汰": 25})
        self.assertEqual(d["next"], 38)
        self.assertIn("下调", d["reason"])

    def test_low_redundancy_slight_down(self):
        d = ts.decide_next_count("双色球", current=40, last_kpi={
            "启用": True, "压缩前注数": 100, "质量淘汰": 2, "重复淘汰": 1, "重叠淘汰": 3})
        self.assertEqual(d["next"], 39)

    def test_hit_rate_deviation_steps_up(self):
        d = ts.decide_next_count("双色球", current=40,
                                 last_kpi={"启用": True, "压缩前注数": 100,
                                           "质量淘汰": 0, "重复淘汰": 0, "重叠淘汰": 0},
                                 hit_rate=0.20, expected_rate=0.0671, hit_n=3000)
        self.assertGreater(d["next"], 40)
        self.assertIn("回补", d["reason"])
        self.assertIsNotNone(d["z"])

    def test_no_kpi_keeps_current(self):
        d = ts.decide_next_count("双色球", current=40, last_kpi=None)
        self.assertEqual(d["next"], 40)
        self.assertIn("维持", d["reason"])

    def test_floor_is_respected(self):
        d = ts.decide_next_count("双色球", current=2,
                                 last_kpi={"启用": True, "压缩前注数": 100,
                                           "质量淘汰": 60, "重复淘汰": 30, "重叠淘汰": 10})
        self.assertGreaterEqual(d["next"], ts.DYNAMIC_FLOOR)

    def test_deviation_uses_two_standard_errors(self):
        # 小样本下同一偏离比例不应触发回补（SE 大 → z 未超阈值）
        # 冗余度设为中位区间，避免落进「低冗余微降」分支，从而单测「回补是否被抑噪」
        mid_kpi = {"启用": True, "压缩前注数": 100, "质量淘汰": 20,
                   "重复淘汰": 0, "重叠淘汰": 15}  # 冗余 35% → 维持区间
        d = ts.decide_next_count("双色球", current=40, last_kpi=mid_kpi,
                                 hit_rate=0.10, expected_rate=0.0671, hit_n=20)
        self.assertEqual(d["next"], 40)  # z 未超阈值 → 维持
        self.assertIn("维持", d["reason"])

    def test_large_sample_deviation_triggers_step_up(self):
        # 同样偏离比例，大样本下 SE 小 → z 超阈值 → 回补
        mid_kpi = {"启用": True, "压缩前注数": 100, "质量淘汰": 20,
                   "重复淘汰": 0, "重叠淘汰": 15}
        d = ts.decide_next_count("双色球", current=40, last_kpi=mid_kpi,
                                 hit_rate=0.10, expected_rate=0.0671, hit_n=3000)
        self.assertGreater(d["next"], 40)
        self.assertIn("回补", d["reason"])

    def test_upper_cap_for_digital(self):
        d = ts.decide_next_count("福彩3D", current=ts.MAX_TICKETS_DIGITAL,
                                 last_kpi={"启用": True, "压缩前注数": 10,
                                           "质量淘汰": 0, "重复淘汰": 0, "重叠淘汰": 0},
                                 hit_rate=0.5, expected_rate=0.001, hit_n=1000)
        self.assertLessEqual(d["next"], ts.MAX_TICKETS_DIGITAL)


class PersistenceTests(unittest.TestCase):
    def test_save_load_roundtrip(self):
        with _TmpStore():
            saved = ts.save_config({"mode": "dynamic", "fixed": {"双色球": 33}})
            self.assertEqual(saved["mode"], "dynamic")
            loaded = ts.load_config()
            self.assertEqual(loaded["fixed"]["双色球"], 33)

    def test_load_missing_file_returns_empty(self):
        with _TmpStore():
            self.assertEqual(ts.load_config(), {})

    def test_load_corrupt_file_returns_empty(self):
        with _TmpStore() as p:
            p.write_text("{not json", encoding="utf-8")
            self.assertEqual(ts.load_config(), {})

    def test_apply_dynamic_result_writes_reason(self):
        with _TmpStore():
            cfg = ts.sanitize({"mode": "dynamic", "dynamic": {"双色球": 40}})
            ts.save_config(cfg)
            ts.apply_dynamic_result("双色球", 38, "测试理由")
            loaded = ts.load_config()
            self.assertEqual(loaded["dynamic"]["双色球"], 38)
            self.assertEqual(loaded["current"]["双色球"], 38)
            self.assertEqual(loaded["policy"]["理由"], "测试理由")


class SnapshotTests(unittest.TestCase):
    def test_snapshot_lists_all_lotteries(self):
        with _TmpStore():
            from config import LOTTERY_CONFIG
            snap = ts.snapshot()
            self.assertEqual(len(snap["lotteries"]), len(LOTTERY_CONFIG))
            for it in snap["lotteries"]:
                self.assertIn("上限", it)
                self.assertIn("下限", it)
                self.assertEqual(it["下限"], 1)

    def test_snapshot_dynamic_suggestion_present(self):
        with _TmpStore():
            ts.save_config({"mode": "dynamic", "dynamic": {"双色球": 40}})
            snap = ts.snapshot(expected_rates={"双色球": 0.0671},
                               hit_stats={"双色球": {"hit_n": 3000, "hit_rate": 0.067,
                                                     "last_kpi": {"启用": True, "压缩前注数": 100,
                                                                  "质量淘汰": 50, "重复淘汰": 0,
                                                                  "重叠淘汰": 5}}})
            item = [x for x in snap["lotteries"] if x["lottery"] == "双色球"][0]
            self.assertIn("建议", item)
            self.assertLess(item["建议"]["next"], 40)


class ApiTests(unittest.TestCase):
    def setUp(self):
        os.environ.setdefault("WORKBUDDY_ALLOW_PROD_FB_WRITE", "0")
        from web.app import app
        self.client = app.test_client()

    def test_get_returns_mode(self):
        with _TmpStore():
            r = self.client.get("/api/settings/ticket-size")
            self.assertEqual(r.status_code, 200)
            self.assertIn(r.get_json().get("mode"), ("fixed", "dynamic"))

    def test_post_set_fixed(self):
        with _TmpStore():
            r = self.client.post("/api/settings/ticket-size",
                                 json={"mode": "fixed", "fixed": {"双色球": 25}})
            self.assertEqual(r.status_code, 200)
            d = r.get_json()
            self.assertTrue(d.get("ok"))
            item = [x for x in d["lotteries"] if x["lottery"] == "双色球"][0]
            self.assertEqual(item["当前生效"], 25)

    def test_post_invalid_mode_400(self):
        with _TmpStore():
            r = self.client.post("/api/settings/ticket-size", json={"mode": "bogus"})
            self.assertEqual(r.status_code, 400)

    def test_post_out_of_range_clamped(self):
        with _TmpStore():
            r = self.client.post("/api/settings/ticket-size",
                                 json={"mode": "fixed", "fixed": {"福彩3D": 9999}})
            item = [x for x in r.get_json()["lotteries"] if x["lottery"] == "福彩3D"][0]
            self.assertEqual(item["当前生效"], ts.MAX_TICKETS_DIGITAL)

    def test_apply_requires_dynamic(self):
        with _TmpStore():
            self.client.post("/api/settings/ticket-size", json={"mode": "fixed"})
            r = self.client.post("/api/settings/ticket-size/apply", json={})
            self.assertEqual(r.status_code, 400)

    def test_apply_works_in_dynamic(self):
        with _TmpStore():
            self.client.post("/api/settings/ticket-size", json={"mode": "dynamic"})
            r = self.client.post("/api/settings/ticket-size/apply", json={})
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json().get("ok"))
            self.assertIn("调整", r.get_json())

    def test_reset_restores_defaults(self):
        with _TmpStore():
            self.client.post("/api/settings/ticket-size",
                             json={"mode": "fixed", "fixed": {"双色球": 17}})
            r = self.client.post("/api/settings/ticket-size/reset", json={})
            self.assertEqual(r.status_code, 200)
            item = [x for x in r.get_json()["lotteries"] if x["lottery"] == "双色球"][0]
            self.assertEqual(item["当前生效"], 100)


if __name__ == "__main__":
    unittest.main()
