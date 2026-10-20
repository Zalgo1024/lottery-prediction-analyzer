"""自动流水线核心 + 启动恢复补跑：口径一致性回归测试

背景（2026-09-11 用户报障）：看板「大乐透 自动化简报」显示的开奖号停在 26102，
而本地 CSV 已有 26103。根因是三条触发路径口径不一致：

  1. 调度器 22:05 / Web 手动 → 完整流水线（出号 + 写简报）  ✅
  2. worker 事件驱动（开奖数据到达）→ 只做 evaluate + robustness  ❌ 不出号、不写简报
  3. 启动恢复（App 启动补滞后数据）→ 只做 fetch + evaluate        ❌ 同上

后果：非每日彩种（双色球/大乐透/七星彩，一周只开 2-3 次）在 App 关机跨过
开奖时段后，整整一期丢号；且看板卡片会长期停在上一期开奖号。

本测试锁死修复后的三条不变量：
  - 核心预测步骤对「下期已有 pending」去重（三条路径并发触发是幂等的）
  - 事件驱动对非每日彩种会入队 auto_pipeline，对每日彩种不会
  - 启动时若简报期号落后于本地最新期号，会触发补跑（覆盖「数据已同步但没跑过流水线」）
"""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import web.startup_recovery as sr_mod
from web.startup_recovery import ensure_pipelines_fresh
import web.utils as utils_mod
from web.utils import run_auto_pipeline_core


def _fake_draw(issue=26103, red=(3, 16, 17, 32, 35), blue=(2, 12)):
    """构造一个最小可用的开奖记录（模拟 data.loader.load_lottery 的返回）"""
    rec = SimpleNamespace(期号=issue, 开奖日期="2026-09-09",
                          红球=list(red), 蓝球=list(blue),
                          zone_numbers={"红球": list(red), "蓝球": list(blue)})
    return SimpleNamespace(records=[rec], total_records=1)


class AutoPipelineCoreTests(unittest.TestCase):
    """核心流水线：预测步骤的去重闸门"""

    def setUp(self):
        self.predict_calls = []
        self.saved_pending = None

        def _fake_predict(lottery_name=None, groups=5, mode="fresh", record_pending=True):
            self.predict_calls.append((lottery_name, groups, mode, record_pending))
            return {
                "预测日期": "2026-09-12", "号码组数": groups, "预测模式": mode,
                "目标期号": 26104,
                "预测号码": [{"红球": [1, 2, 3, 4, 5], "蓝球": [6, 7], "策略": "高频策略"}],
            }

        self.rec_run_patch = patch("data.automation_status.record_run")
        self._patches = [
            patch("data.fetcher.update_lottery_data",
                  return_value={"fetched": 0, "latest_before": 26103, "latest_after": 26103}),
            patch("data.feedback.evaluate_pending_predictions",
                  return_value={"evaluated_count": 0, "new_feedback_count": 0,
                                "new_feedback_records": [], "updated_weights": {}}),
            patch("prediction.engine.predict", side_effect=_fake_predict),
            patch("prediction.reporter.save_prediction_record",
                  side_effect=lambda pred: Path("training") / "fake_predict"),
            patch("prediction.anomaly.detect_anomalies",
                  return_value={"异常总条数": 0, "异常明细": []}),
            patch("pipeline.step6_ev.current_period_advice",
                  return_value={"单注期望": -0.762, "是否值得买": False}),
            patch("data.loader.load_lottery", return_value=_fake_draw()),
            self.rec_run_patch,
        ]
        self.record_run = None
        for p in self._patches:
            m = p.start()
            if p is self.rec_run_patch:
                self.record_run = m
            self.addCleanup(p.stop)

    def _run(self, pending, target_issue=26104, params=None):
        base = {"mode": "fresh", "groups": 100}
        if params:
            base.update(params)
        with patch("data.feedback._next_issue", return_value=target_issue), \
             patch("data.feedback.load_pending", return_value=pending):
            return run_auto_pipeline_core("大乐透", base)

    def test_generates_prediction_when_next_issue_has_none(self):
        res = self._run(pending=[])
        self.assertEqual(len(self.predict_calls), 1)
        self.assertEqual(self.predict_calls[0], ("大乐透", 100, "fresh", True))
        self.assertTrue(any("预测: " in s for s in res["steps"]))
        self.record_run.assert_called_once()

    def test_skips_prediction_when_next_issue_already_pending(self):
        """三条触发路径并发时，后到的必须复用已有 pending 而不是再写一份。

        ★ 2026-10-11「出号数量 = 最终注数」：复用的前提是组数与当前出号数量一致
          （本次 groups=100），否则会按新数量重新生成。
        """
        pending = [{"目标期号": 26104, "预测号码": [{"红球": [9]}] * 100}]
        res = self._run(pending=pending)
        self.assertEqual(self.predict_calls, [])          # 没有重复出号
        self.assertTrue(any("跳过预测" in s for s in res["steps"]))
        self.assertIn("复用下一期", res["prediction"])

    def test_regenerates_when_pending_count_mismatches_groups(self):
        """★ 2026-10-11：已有 pending 的组数与当前出号数量不符 → 重新生成。

        场景：用户在看板改了滑轨注数，旧的下一期预测组数与之不符，
        必须按新数量重出（覆盖旧 pending），否则改了设置也不生效。
        """
        pending = [{"目标期号": 26104, "预测号码": [{"红球": [9]}] * 7}]
        res = self._run(pending=pending)
        self.assertEqual(self.predict_calls, [("大乐透", 100, "fresh", True)])
        self.assertTrue(any("重新生成" in s for s in res["steps"]))

    # ---------- 2026-10-18：生产 pending 防覆盖（真实事故回归） ----------
    # 背景：一次 auto_batch 验证出号带 groups=5（6 彩种 × 3 模式），把下一期
    # 生产 pending 全量覆盖成 5 注，滑轨 200/38/50/44 静默失效。以下用例锁死防护。

    def test_record_pending_false_skips_production_write(self):
        """显式 record_pending=False → predict 拿到 False，且步骤里标明仅试算。"""
        res = self._run(pending=[], params={"record_pending": False})
        self.assertEqual(self.predict_calls, [("大乐透", 100, "fresh", False)])
        self.assertTrue(any("仅试算" in s for s in res["steps"]))

    def test_record_pending_defaults_to_true(self):
        """不传该参数 = 生产行为不变（默认落库）。"""
        self._run(pending=[])
        self.assertTrue(self.predict_calls[0][3])

    def test_record_pending_accepts_string_false(self):
        """Web JSON 常见字符串表达（"false"/"0"）也必须被识别为不落库。"""
        for raw in ("false", "0", "no", "off", False):
            self.predict_calls.clear()
            self._run(pending=[], params={"record_pending": raw})
            self.assertFalse(self.predict_calls[0][3], f"{raw!r} 未被识别为不落库")

    def test_explicit_groups_differs_from_slide_warns(self):
        """显式注数 ≠ 看板滑轨 → 必须留痕（日志 + 步骤），避免静默覆盖。"""
        def _fake_resolve(lottery, requested=None):
            return 200 if requested is None else requested   # 滑轨 200 / 显式原样

        with patch.object(utils_mod, "resolve_groups", side_effect=_fake_resolve), \
             self.assertLogs("web.utils", level="WARNING") as cm:
            res = self._run(pending=[], params={"groups": 5})
        self.assertEqual(self.predict_calls[0][1], 5)
        self.assertTrue(any("≠ 滑轨" in s for s in res["steps"]))
        self.assertTrue(any("record_pending=false" in m for m in cm.output))

    def test_slide_groups_does_not_warn(self):
        """走滑轨（不传 groups）时不应出现「≠ 滑轨」提示。"""
        def _fake_resolve(lottery, requested=None):
            return 200

        with patch.object(utils_mod, "resolve_groups", side_effect=_fake_resolve):
            res = self._run(pending=[], params={"groups": None})
        self.assertFalse(any("≠ 滑轨" in s for s in res["steps"]))
        self.assertEqual(self.predict_calls[0][1], 200)

    def test_core_runs_without_task_id(self):
        """worker / 启动恢复调用时不传 task_id，不应因缺少任务记录而报错。"""
        res = self._run(pending=[])
        self.assertEqual(res["lottery"], "大乐透")
        self.record_run.assert_called_once()
        # 简报必须带上本次抓到的开奖号（看板卡片就靠它显示「最新开奖」）
        kwargs = self.record_run.call_args.kwargs
        self.assertEqual(kwargs["draw_numbers"]["期号"], 26103)


class AutoBatchRecordPendingTests(unittest.TestCase):
    """批流水线必须把 record_pending 透传到 batch 与每个子任务（2026-10-18 事故回归）。

    事故形态：auto_batch(groups=5, 6 彩种 × 3 模式) 把下一期生产 pending
    全量覆盖成 5 注。修复要求验证性批跑能显式「只算不落库」。
    """

    def _start(self, params):
        calls = []

        def _fake_create_task(t, p):
            calls.append((t, p))
            return "tid"

        fake_thread = MagicMock()
        with patch.object(utils_mod, "create_task", side_effect=_fake_create_task), \
             patch("threading.Thread", return_value=fake_thread):
            tid = utils_mod.start_auto_batch(["双色球"], ["fresh"], params)
        return tid, calls, fake_thread

    def test_batch_params_carry_record_pending_false(self):
        tid, calls, fake_thread = self._start({"groups": 5, "record_pending": False})
        self.assertEqual(tid, "tid")
        self.assertEqual(calls[0][1].get("record_pending"), False)
        fake_thread.start.assert_called_once()

    def test_batch_params_default_record_pending_true(self):
        _, calls, _ = self._start({"groups": 5})
        self.assertEqual(calls[0][1].get("record_pending"), True)


class StartupRecoveryFreshnessTests(unittest.TestCase):
    """启动时：简报落后于本地开奖数据 → 触发补跑"""

    def setUp(self):
        self.triggered = []
        self._patches = [
            patch.object(sr_mod, "_trigger_pipeline_if_needed",
                         side_effect=lambda lot: self.triggered.append(lot)),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _status(self, brief_issues: dict):
        return {"last_run_by_lottery": {
            lot: {"draw_numbers": {"期号": issue}} for lot, issue in brief_issues.items()
        }}

    def _run(self, brief_issues, csv_latest):
        with patch("data.automation_status.get_status", return_value=self._status(brief_issues)), \
             patch("data.loader.load_lottery", side_effect=lambda n: _fake_draw(csv_latest[n])):
            return ensure_pipelines_fresh()

    def test_triggers_when_brief_lags_csv(self):
        """用户实际遇到的形态：本地已有 26103，简报还停在 26102。"""
        out = self._run(
            brief_issues={"双色球": 26105, "大乐透": 26102, "七星彩": None},
            csv_latest={"双色球": 26105, "大乐透": 26103, "七星彩": 26104},
        )
        self.assertIn("大乐透", self.triggered)
        self.assertIn("七星彩", self.triggered)   # 无简报记录也算落后
        self.assertNotIn("双色球", self.triggered)  # 简报已是最新
        self.assertEqual(out["大乐透"]["brief_issue"], 26102)
        self.assertEqual(out["大乐透"]["latest"], 26103)

    def test_daily_lotteries_never_triggered(self):
        """数字型每天开奖 + 每晚 22:05 必有完整流水线 → 启动时不重复触发。"""
        self._run(
            brief_issues={},
            csv_latest={"双色球": 26105, "大乐透": 26103, "七星彩": 26104},
        )
        self.assertNotIn("排列5", self.triggered)
        self.assertNotIn("福彩3D", self.triggered)

    def test_no_trigger_when_everything_fresh(self):
        self._run(
            brief_issues={"双色球": 26105, "大乐透": 26103, "七星彩": 26104},
            csv_latest={"双色球": 26105, "大乐透": 26103, "七星彩": 26104},
        )
        self.assertEqual(self.triggered, [])


if __name__ == "__main__":
    unittest.main()
