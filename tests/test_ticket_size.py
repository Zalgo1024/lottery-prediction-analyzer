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
        cfg = ts.sanitize({"mode": "fixed", "fixed": {"双色球": 9999, "福彩3D": 9999}})
        self.assertEqual(cfg["fixed"]["双色球"], ts.max_tickets("双色球"))
        self.assertEqual(cfg["fixed"]["福彩3D"], ts.max_tickets("福彩3D"))

    def test_upper_bound_is_configurable(self):
        """★ 上限必须可配（原实现硬编码 数字50/乐透200，而生产 fixed 恰好等于上限
        ⇒ 滑轨拖到底就拖不动了，表现为「注数永远固定在最大值」）。"""
        cfg = ts.sanitize({"mode": "fixed",
                           "limits": {"双色球": 320, "福彩3D": 77},
                           "fixed": {"双色球": 9999, "福彩3D": 9999}})
        self.assertEqual(cfg["fixed"]["双色球"], 320)
        self.assertEqual(cfg["fixed"]["福彩3D"], 77)
        self.assertEqual(cfg["limits"]["双色球"], 320)

    def test_upper_bound_cannot_exceed_hard_cap(self):
        cfg = ts.sanitize({"limits": {"双色球": 999999}})
        self.assertEqual(cfg["limits"]["双色球"], ts.HARD_CAP)

    def test_default_limits_allow_more_than_old_caps(self):
        """新旧对比：旧上限是 乐透 200 / 数字 50，新默认必须都更大。"""
        self.assertGreater(ts.max_tickets("双色球"), 200)
        self.assertGreater(ts.max_tickets("排列3"), 50)

    def test_limits_are_uniform_1000_for_every_lottery(self):
        """所有彩种上限统一 1000（2026-11-04 用户要求：由 500 提到 1000）。

        为什么值得一条测试：上限曾逐彩种给（500/400/300/200/100），卡片上五种刻度；
        后来统一 500，2026-11-04 又整体提到 1000。只要有人「按彩种的理由」再调一个
        回去，滑轨又会变成参差不齐 —— 而这是**操作成本**，不是安全收益
        （真正的硬约束是 `ev/budget_gate` 的月度总额/单期上限）。

        ★ 上限是**承诺**：能拖到 1000 就必须真出得了 1000 注。数字型/乐透型的空间
          都够（排列3/3D 直选空间正好 1000）；双色球的**贪心覆盖**在 cap=3 + 池 8000
          下实测只到约 649 注，超出部分走 `_pad_to_groups` 的随机补齐
          （票面仍是 1000 张，只是覆盖优化退化成随机补），这条只管「数字一致」。
        ★ 上限必须 ≤ `HARD_CAP`，否则 `_resolve_limits` 会静默夹回去。
        """
        from config import LOTTERY_CONFIG

        self.assertEqual(set(ts.DEFAULT_LIMITS.values()), {1000},
                         f"默认上限不统一：{ts.DEFAULT_LIMITS}")
        for lot in LOTTERY_CONFIG:
            self.assertEqual(ts.max_tickets(lot), 1000, f"{lot} 上限不是 1000")
        # 兜底值也要跟着统一，否则新接入的彩种会掉回旧刻度
        self.assertEqual(ts._FALLBACK_LIMIT_LOTTO, 1000)
        self.assertEqual(ts._FALLBACK_LIMIT_DIGITAL, 1000)
        # ★ 默认值不得超过绝对上限（超过就会被静默夹回，表现为「改了没生效」）
        self.assertLessEqual(max(ts.DEFAULT_LIMITS.values()), ts.HARD_CAP)

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
    def test_requested_is_no_longer_a_pure_bypass(self):
        """★ 2026-10-30（任务 2）：`requested` 不再是纯旁路。

        原断言是 `resolve_groups("双色球", 123) == 123` —— 那意味着
        `cli.py predict --groups 2000` 能出 2000 注（4000 元），与月度额度无关。
        现在的契约按**来源**区分：
          · `auto`（默认，调度器/自愈）：不超过基线（自动路径永不加注）；
          · `manual`（人在键盘前）：可以超过基线，但受预算闸门夹取；
          · `research`：豁免（回测/试算，不占生产账）。
        """
        with _TmpStore():
            ts.save_config({"mode": "fixed", "fixed": {"双色球": 7}})
            from config import resolve_groups
            self.assertEqual(resolve_groups("双色球", 123), 7)                    # auto
            self.assertEqual(resolve_groups("双色球", 123, source="manual"), 123)  # 未启用闸门
            self.assertEqual(resolve_groups("双色球", 123, source="research"), 123)

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

    def test_low_redundancy_holds(self):
        # ★ 2026-10-24 修正契约：冗余低 = 没有可压缩空间 ⇒ 维持，不再 -1。
        #   原实现「只降不升」（上调仅靠 |z|>2 的噪声信号）会让注数单调下沉到 1 注。
        d = ts.decide_next_count("双色球", current=40, last_kpi={
            "启用": True, "压缩前注数": 100, "质量淘汰": 2, "重复淘汰": 1, "重叠淘汰": 3})
        self.assertEqual(d["next"], 40)
        self.assertIn("维持", d["reason"])

    def test_no_redundancy_drift_when_hovering_at_floor(self):
        """低冗余分支在任意 current 下都不下调（不得突破到 1 注）。"""
        kpi_low = {"启用": True, "压缩前注数": 100, "质量淘汰": 1,
                   "重复淘汰": 0, "重叠淘汰": 1}
        for cur in (1, 2, 5, 50):
            d = ts.decide_next_count("双色球", current=cur, last_kpi=kpi_low)
            self.assertEqual(d["next"], cur)

    def test_hit_rate_deviation_steps_up(self):
        """★ 已废弃的旧契约：原本「命中率异常高 ⇒ +2 注」。

        2026-10-24 移除。原因是这个方向在数学上没有依据：
        `E[中奖注数] = K·p`，p 恒定 ⇒ 加注不改变命中率，只增加成本。
        现在改为：偏离只写诊断，注数不变（正偏离是运气，不是 edge）。
        """
        d = ts.decide_next_count("双色球", current=40,
                                 last_kpi={"启用": True, "压缩前注数": 100,
                                           "质量淘汰": 0, "重复淘汰": 0, "重叠淘汰": 0},
                                 hit_rate=0.20, expected_rate=0.0671, hit_n=3000,
                                 n_batches=60)
        self.assertEqual(d["next"], 40, "命中率偏高不得自动加注")
        self.assertIsNotNone(d["z"])
        self.assertIn("加注不改变单注中奖概率", d["诊断"])

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

    def test_deviation_never_raises_count_even_with_huge_sample(self):
        """★★ 2026-10-24 语义修正：命中率偏离**永不加注**。

        原行为是「|z|>2 ⇒ +2 注」，两个致命问题：
          ① 期望线性性：加注只放大注数，单注概率 p 恒定 ⇒ 命中率不会回升，
             所以这条路径永远追不上，只会单向爬到上限（实证：双色球被推到
             200 注上限，26120/26121 都是 200）；
          ② z 原用「注数」当样本量，但同批票共享蓝球 ⇒ 有效样本是批次数。
        新契约：偏离只写诊断，`next` 必须等于 `current`。
        """
        mid_kpi = {"启用": True, "压缩前注数": 100, "质量淘汰": 20,
                   "重复淘汰": 0, "重叠淘汰": 15}
        # 小样本：诊断为「噪声」
        d = ts.decide_next_count("双色球", current=40, last_kpi=mid_kpi,
                                 hit_rate=0.02, expected_rate=0.0671,
                                 hit_n=3000, n_batches=4, anchor=40)
        self.assertEqual(d["next"], 40, "小样本时不应加注")
        self.assertFalse(d["样本够"])
        self.assertIn("噪声", d["诊断"])
        # 大样本：诊断指向「加注无数学依据」，同样不加注
        d = ts.decide_next_count("双色球", current=40, last_kpi=mid_kpi,
                                 hit_rate=0.02, expected_rate=0.0671,
                                 hit_n=3000, n_batches=40, anchor=40)
        self.assertEqual(d["next"], 40, "大样本时也不应加注")
        self.assertTrue(d["样本够"])
        self.assertIn("加注不改变单注中奖概率", d["诊断"])
        # 正偏离同样不该加注（那是运气，不是 edge）
        d = ts.decide_next_count("双色球", current=40, last_kpi=mid_kpi,
                                 hit_rate=0.15, expected_rate=0.0671,
                                 hit_n=3000, n_batches=40, anchor=40)
        self.assertEqual(d["next"], 40)

    def test_small_batch_count_is_reported_as_insufficient(self):
        """批次数 < 20 时必须显式说明「样本不足，判为噪声」。"""
        d = ts.decide_next_count("双色球", current=40, hit_rate=0.032,
                                 expected_rate=0.0671, hit_n=500, n_batches=4)
        self.assertFalse(d["样本够"])
        self.assertIn("噪声", d["诊断"])

    def test_redundancy_down_stops_at_floor(self):
        """冗余下调的硬下限是 DYNAMIC_FLOOR（1 注），不会降到 0 或负数。"""
        high = {"启用": True, "压缩前注数": 100, "质量淘汰": 50,
                "重复淘汰": 10, "重叠淘汰": 10}
        d = ts.decide_next_count("双色球", current=150, last_kpi=high, n_batches=30)
        self.assertEqual(d["next"], 148)              # 每期 -2
        d = ts.decide_next_count("双色球", current=3, last_kpi=high, n_batches=30)
        self.assertEqual(d["next"], 1)                # 夹在 DYNAMIC_FLOOR
        d = ts.decide_next_count("双色球", current=1, last_kpi=high, n_batches=30)
        self.assertEqual(d["next"], 1)


class LockTests(unittest.TestCase):
    """★ `锁定` = 用户调好的注数不被后台改回去。"""

    def test_locked_config_ignores_auto_apply(self):
        cfg = ts.sanitize({"mode": "dynamic", "锁定": True,
                           "dynamic": {"双色球": 60}, "fixed": {"双色球": 60}})
        self.assertTrue(cfg["锁定"])

    def test_apply_dynamic_result_respects_lock(self):
        import tempfile
        from pathlib import Path
        old = ts.STORE_PATH
        try:
            d = Path(tempfile.mkdtemp())
            ts.STORE_PATH = d / "ticket_size.json"
            ts.save_config({"mode": "dynamic", "锁定": True,
                            "dynamic": {"双色球": 60}, "fixed": {"双色球": 60}})
            ts.apply_dynamic_result("双色球", 20, "测试：不应生效")
            self.assertEqual(ts.effective_count("双色球"), 60)
            # force=True（用户手点「按策略调整」）必须生效
            ts.apply_dynamic_result("双色球", 20, "测试：手动", force=True)
            self.assertEqual(ts.effective_count("双色球"), 20)
        finally:
            ts.STORE_PATH = old

    def test_upper_cap_for_digital(self):
        cap = ts.max_tickets("福彩3D")
        d = ts.decide_next_count("福彩3D", current=cap,
                                 last_kpi={"启用": True, "压缩前注数": 10,
                                           "质量淘汰": 0, "重复淘汰": 0, "重叠淘汰": 0},
                                 hit_rate=0.5, expected_rate=0.001, hit_n=1000)
        self.assertLessEqual(d["next"], cap)


class DynamicApplyNoWriteTests(unittest.TestCase):
    """★ 2026-10-24：注数不变时不得写盘。

    原实现在 else 分支也无条件 `apply_dynamic_result` ⇒ 每次结算都整文件重写：
    policy.理由 被反复覆盖成「维持当前注数」，而 mtime 一直在变，
    排查时极易误判成「注数被改过」。
    """

    def test_no_write_when_count_unchanged(self):
        from data import feedback as fb

        with _TmpStore():
            ts.save_config({"mode": "dynamic", "dynamic": {"双色球": 40}})
            with patch.object(fb, "_recent_hit_stats",
                              return_value={"hit_n": 0, "hit_rate": None}), \
                 patch.object(fb, "_last_compress_kpi", return_value=None), \
                 patch.object(fb, "_expected_win_rate", return_value=None), \
                 patch.object(ts, "apply_dynamic_result") as m:
                d = fb._apply_dynamic_ticket_size("双色球", [])
                self.assertEqual(d["next"], 40)
                m.assert_not_called()

    def test_writes_when_count_changes(self):
        from data import feedback as fb

        with _TmpStore():
            ts.save_config({"mode": "dynamic", "dynamic": {"双色球": 40}})
            with patch.object(fb, "_recent_hit_stats",
                              return_value={"hit_n": 0, "hit_rate": None}), \
                 patch.object(fb, "_last_compress_kpi", return_value={
                     "启用": True, "压缩前注数": 100, "质量淘汰": 40,
                     "重复淘汰": 20, "重叠淘汰": 10}), \
                 patch.object(fb, "_expected_win_rate", return_value=None), \
                 patch.object(ts, "apply_dynamic_result") as m:
                d = fb._apply_dynamic_ticket_size("双色球", [])
                self.assertEqual(d["next"], 38)
                m.assert_called_once()


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
            self.assertEqual(item["当前生效"], ts.max_tickets("福彩3D"))

    def test_apply_requires_dynamic(self):
        with _TmpStore():
            self.client.post("/api/settings/ticket-size", json={"mode": "fixed"})
            r = self.client.post("/api/settings/ticket-size/apply", json={})
            self.assertEqual(r.status_code, 400)

    def test_post_dynamic_does_not_pollute_fixed(self):
        """★ 动态模式下保存只改 dynamic，不得覆盖 fixed（用户意图锚点）。

        2026-10-24 事故：前端把「动态值」同时提交到 fixed 与 dynamic，
        于是每点一次保存 fixed 就被动态值吃掉（实测 双色球 168→75、排列3 29→18）。
        """
        with _TmpStore():
            self.client.post("/api/settings/ticket-size",
                             json={"mode": "fixed", "fixed": {"双色球": 200}})
            self.client.post("/api/settings/ticket-size", json={"mode": "dynamic"})
            before = ts.sanitize(ts.load_config())["fixed"]["双色球"]
            self.assertEqual(before, 200)
            # 模拟旧前端：两个字段一起发
            self.client.post("/api/settings/ticket-size",
                             json={"mode": "dynamic",
                                   "fixed": {"双色球": 81},
                                   "dynamic": {"双色球": 81}})
            cfg = ts.sanitize(ts.load_config())
            self.assertEqual(cfg["fixed"]["双色球"], before, "fixed 被动态值污染")
            self.assertEqual(cfg["dynamic"]["双色球"], 81)

    def test_post_fixed_does_not_pollute_dynamic(self):
        with _TmpStore():
            self.client.post("/api/settings/ticket-size", json={"mode": "dynamic"})
            self.client.post("/api/settings/ticket-size",
                             json={"mode": "dynamic", "dynamic": {"双色球": 120}})
            self.client.post("/api/settings/ticket-size",
                             json={"mode": "fixed",
                                   "fixed": {"双色球": 60},
                                   "dynamic": {"双色球": 60}})
            cfg = ts.sanitize(ts.load_config())
            self.assertEqual(cfg["dynamic"]["双色球"], 120)
            self.assertEqual(cfg["fixed"]["双色球"], 60)

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
