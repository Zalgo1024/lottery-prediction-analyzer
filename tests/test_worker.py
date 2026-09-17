"""L4 第2期：task_store 认领/心跳/孤儿回收 + ContinuousWorker 队列与事件驱动"""
import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from web.task_store import TaskStore
import web.task_store as task_store_mod
import web.worker as worker_mod
from web.worker import ContinuousWorker, WORKER_TASK_TYPES


class ClaimReclaimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(Path(self._tmp.name) / "tasks.db")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)

    def test_claim_next_claims_oldest_pending_atomically(self):
        id1 = self.store.create("robustness_light", {"lottery": "福彩3D"})
        id2 = self.store.create("evaluate", {"lottery": "福彩3D"})

        t1 = self.store.claim_next(WORKER_TASK_TYPES, "w1")
        t2 = self.store.claim_next(WORKER_TASK_TYPES, "w1")
        t3 = self.store.claim_next(WORKER_TASK_TYPES, "w1")

        self.assertEqual(t1["id"], id1)  # 最旧优先
        self.assertEqual(t1["status"], "running")
        self.assertEqual(t1["worker_id"], "w1")
        self.assertIsNotNone(t1["heartbeat_at"])
        self.assertEqual(t2["id"], id2)
        self.assertIsNone(t3)  # 无可认领

    def test_claim_next_ignores_non_worker_types(self):
        auto_id = self.store.create("auto", {"lottery": "双色球"})
        rb_id = self.store.create("robustness_full", {"lottery": "双色球"})

        t = self.store.claim_next(WORKER_TASK_TYPES, "w1")

        self.assertEqual(t["id"], rb_id)  # 旧版 auto 任务不归 worker 管
        self.assertEqual(self.store.get(auto_id)["status"], "pending")

    def test_reclaim_orphans_only_stale_heartbeat(self):
        fresh_id = self.store.create("robustness_light", {"lottery": "福彩3D"})
        self.store.claim_next(WORKER_TASK_TYPES, "w1")  # fresh_id 变 running，心跳=now
        stale_id = self.store.create("robustness_full", {"lottery": "双色球"})
        old = (datetime.now() - timedelta(seconds=3600)).isoformat(timespec="seconds")
        self.store.update(stale_id, status="running", worker_id="dead-worker")
        with self.store._get_conn() as conn:
            conn.execute("UPDATE tasks SET heartbeat_at=? WHERE id=?", (old, stale_id))
            conn.commit()
        other_id = self.store.create("auto", {"lottery": "双色球"})  # 非 worker 类型
        self.store.update(other_id, status="running")  # 无心跳，但类型不受管

        n = self.store.reclaim_orphans(WORKER_TASK_TYPES, stale_seconds=600)

        self.assertEqual(n, 1)
        self.assertEqual(self.store.get(stale_id)["status"], "failed")
        self.assertIn("孤儿", self.store.get(stale_id)["error"])
        self.assertEqual(self.store.get(fresh_id)["status"], "running")
        self.assertEqual(self.store.get(other_id)["status"], "running")

    def test_heartbeat_only_touches_running_tasks(self):
        tid = self.store.create("evaluate", {"lottery": "双色球"})
        self.store.claim_next(WORKER_TASK_TYPES, "w1")
        before = self.store.get(tid)["heartbeat_at"]

        self.store.heartbeat(tid)
        after = self.store.get(tid)["heartbeat_at"]
        self.assertGreaterEqual(after, before)

        self.store.update(tid, status="done")
        self.store.heartbeat(tid)
        self.assertEqual(self.store.get(tid)["status"], "done")

    def test_has_pending_params_match(self):
        self.store.create("robustness_light", {"lottery": "双色球"})
        self.assertTrue(self.store.has_pending(("robustness_light",), {"lottery": "双色球"}))
        self.assertFalse(self.store.has_pending(("robustness_light",), {"lottery": "大乐透"}))
        self.assertTrue(self.store.has_pending(("robustness_light",)))


class WorkerEventTests(unittest.TestCase):
    """事件驱动：期号变化 → 入队（数字型 light / 乐透型 full + evaluate），带去重"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        self.store = TaskStore(tmp / "tasks.db")
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.store.close)
        self.worker = ContinuousWorker(mode="embedded", worker_id="w-test")
        self.event_file = tmp / "event_state.json"

        self._patches = [
            patch.object(task_store_mod, "task_store", self.store),
            patch.object(worker_mod, "EVENT_STATE_FILE", self.event_file),
        ]
        for p in self._patches:
            p.start()
            self.addCleanup(p.stop)

    def _set_issue(self, value):
        return patch("credibility.robustness._latest_issue", return_value=value)

    def test_first_poll_records_baseline_without_enqueue(self):
        with self._set_issue(500):
            self.worker._poll_events()
        self.assertEqual(len(self.store_has_tasks()), 0)
        state = json.loads(self.event_file.read_text(encoding="utf-8"))
        self.assertEqual(state["双色球"]["last_issue"], 500)

    def test_issue_change_enqueues_evaluate_and_robustness(self):
        # 双色球=乐透型 → robustness_full；福彩3D=数字型 → robustness_light
        with self._set_issue(500):
            self.worker._poll_events()
        with patch("credibility.robustness._latest_issue",
                   side_effect=lambda lot: 501 if lot == "双色球" else 500):
            self.worker._poll_events()

        types = [(t["type"], t["params"]["lottery"])
                 for t in self.store_has_tasks()]
        self.assertIn(("evaluate", "双色球"), types)
        self.assertIn(("robustness_full", "双色球"), types)
        self.assertNotIn(("robustness_full", "福彩3D"), types)
        self.assertGreater(self.worker._stats["events_enqueued"], 0)

    def test_same_issue_no_duplicate_enqueue(self):
        with self._set_issue(500):
            self.worker._poll_events()
        with self._set_issue(501):
            self.worker._poll_events()
            n1 = len(self.store_has_tasks())
            self.worker._poll_events()  # 期号未再变 → 不重复入队
            n2 = len(self.store_has_tasks())
        self.assertEqual(n1, n2)
        self.assertGreater(n1, 0)

    def test_issue_change_enqueues_auto_pipeline_only_for_non_daily(self):
        """非每日彩种（双色球/大乐透/七星彩）在开奖数据到达时还要补齐下一期预测 +
        看板简报；数字型每日开奖且每晚 22:05 调度器必跑一次完整流水线，不重复入队。

        回归背景（2026-09-11 用户报障）：事件驱动原本只入队 evaluate + robustness，
        开奖后不出号也不刷新简报 → 大乐透卡片长期停在上一期（26102），
        且下一期（26104）预测整期丢失。
        """
        with self._set_issue(500):
            self.worker._poll_events()
        with patch("credibility.robustness._latest_issue",
                   side_effect=lambda lot: 501 if lot in ("双色球", "福彩3D") else 500):
            self.worker._poll_events()

        types = [(t["type"], t["params"]["lottery"])
                 for t in self.store_has_tasks()]
        self.assertIn(("auto_pipeline", "双色球"), types)      # 非每日 → 补号
        self.assertNotIn(("auto_pipeline", "福彩3D"), types)   # 每日 → 不补

    def test_auto_pipeline_is_worker_claimable(self):
        """auto_pipeline 必须在 WORKER_TASK_TYPES 内且已注册处理器，否则任务永远不被认领。"""
        self.assertIn("auto_pipeline", WORKER_TASK_TYPES)
        self.assertIn("auto_pipeline", worker_mod.TASK_HANDLERS)

    def test_pending_dedup_skips_existing_same_type_lottery(self):
        self.store.create("evaluate", {"lottery": "双色球"})  # 已有 pending
        with self._set_issue(500):
            self.worker._poll_events()
        with self._set_issue(501):
            self.worker._poll_events()
        types = [(t["type"], t["params"]["lottery"])
                 for t in self.store_has_tasks()]
        # evaluate 不重复建，robustness_full 正常入队
        self.assertEqual(types.count(("evaluate", "双色球")), 1)
        self.assertIn(("robustness_full", "双色球"), types)
        # last_issue 仍被推进（不会因去重卡住）
        state = json.loads(self.event_file.read_text(encoding="utf-8"))
        self.assertEqual(state["双色球"]["last_issue"], 501)

    def store_has_tasks(self):
        conn = self.store._get_conn()
        rows = conn.execute("SELECT * FROM tasks").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["params"] = json.loads(d["params"] or "{}")
            except (json.JSONDecodeError, TypeError):
                pass
            out.append(d)
        return out


if __name__ == "__main__":
    unittest.main()
