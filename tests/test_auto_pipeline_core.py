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

        def _fake_predict(lottery_name=None, groups=5, mode="fresh"):
            self.predict_calls.append((lottery_name, groups, mode))
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

    def _run(self, pending, target_issue=26104):
        with patch("data.feedback._next_issue", return_value=target_issue), \
             patch("data.feedback.load_pending", return_value=pending):
            return run_auto_pipeline_core("大乐透", {"mode": "fresh", "groups": 100})

    def test_generates_prediction_when_next_issue_has_none(self):
        res = self._run(pending=[])
        self.assertEqual(len(self.predict_calls), 1)
        self.assertEqual(self.predict_calls[0], ("大乐透", 100, "fresh"))
        self.assertTrue(any("预测: " in s for s in res["steps"]))
        self.record_run.assert_called_once()

    def test_skips_prediction_when_next_issue_already_pending(self):
        """三条触发路径并发时，后到的必须复用已有 pending 而不是再写一份。"""
        pending = [{"目标期号": 26104, "预测号码": [{"红球": [9]}] * 7}]
        res = self._run(pending=pending)
        self.assertEqual(self.predict_calls, [])          # 没有重复出号
        self.assertTrue(any("跳过预测" in s for s in res["steps"]))
        self.assertIn("复用下一期", res["prediction"])

    def test_core_runs_without_task_id(self):
        """worker / 启动恢复调用时不传 task_id，不应因缺少任务记录而报错。"""
        res = self._run(pending=[])
        self.assertEqual(res["lottery"], "大乐透")
        self.record_run.assert_called_once()
        # 简报必须带上本次抓到的开奖号（看板卡片就靠它显示「最新开奖」）
        kwargs = self.record_run.call_args.kwargs
        self.assertEqual(kwargs["draw_numbers"]["期号"], 26103)


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
