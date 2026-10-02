import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from data.holiday import is_holiday
from web.scheduler import AutoScheduler

# 注入固定日期，不依赖真实时钟：2026-09-30（周三，非休市，排列3 每日开奖）。
# 2026-10-02 改造后休市期 _next_run 语义变化（休市窗口不算开奖日），
# 用真实 datetime.now() 的用例在 10-01~10-04 期间会挂。
FIXED_NOW = datetime(2026, 9, 30, 10, 0)


class SchedulerTests(unittest.TestCase):
    def test_launch_does_not_deadlock_when_lock_is_already_held(self):
        scheduler = AutoScheduler()
        scheduler._save = lambda: None

        with patch("web.utils.start_auto_pipeline", return_value="fake-task"):
            with scheduler._lock:
                launched = scheduler._launch("双色球")

        self.assertTrue(launched)
        self.assertEqual(scheduler.state["双色球"]["last_task_id"], "fake-task")
        self.assertEqual(scheduler.state["双色球"]["last_status"], "running")

    def test_next_future_run_is_in_the_future(self):
        """展示口径：next_run 必须是未来时间，不能是"最近一个已过去的应触发时刻"。

        回归背景：看板「下次运行」列曾经直接用 _next_run（最近一个已过去的时间点），
        非每日开奖彩种会显示成两天前的日期，看起来像调度停摆。
        """
        scheduler = AutoScheduler()
        scheduler._save = lambda: None
        now = FIXED_NOW
        for lot in ("双色球", "大乐透", "七星彩", "排列3", "福彩3D", "排列5"):
            nf = scheduler._next_future_run(lot, now=now)
            due = scheduler._next_run(lot, now=now)
            self.assertIsNotNone(nf, lot)
            self.assertGreater(nf, now, f"{lot} 的 next_run 应为未来时间")
            self.assertIsNotNone(due, lot)
            # due 是"最近一个应触发的日期"（今天或更早），不保证已过 22:05
            self.assertLessEqual(due.date(), now.date(), f"{lot} 的 due_run 不应是未来日期")
            # 每日开奖彩种：下一次不会超过 24 小时（休市期例外——休市窗口内没有开奖日）
            if lot in ("排列3", "福彩3D", "排列5") and not is_holiday(now.date()):
                self.assertLess((nf - now).total_seconds(), 24 * 3600, lot)

    def test_get_status_exposes_overdue_for_missed_draw_day(self):
        """该跑没跑要能被看板识别出来（overdue=True）。

        ⚠️ 必须注入固定时刻：overdue 的判据是「当日应触发时刻(22:05)是否已过」。
        用真实 `datetime.now()` 时，每天 00:00~22:05 之间 `_next_run` 返回的
        是**今天**的 22:05（未来时刻），任何 last_run 都判不出 overdue
        → 这条用例一天里只有 22:05~24:00 能过（2026-09-14 跨零点实测挂掉）。
        """
        scheduler = AutoScheduler()
        scheduler._save = lambda: None
        fixed = datetime(2026, 9, 30, 23, 30)   # 周三非休市；注入固定时刻勿用真实时钟
        due = scheduler._next_run("排列3", now=fixed)
        scheduler.state["排列3"] = {
            "last_run": (due - timedelta(days=1)).isoformat(),
            "last_status": "done",
        }

        row = next(x for x in scheduler.get_status(now=fixed)["lotteries"]
                   if x["lottery"] == "排列3")
        self.assertIn("next_run", row)
        self.assertIn("due_run", row)
        self.assertIn("overdue", row)
        self.assertTrue(row["overdue"])
        self.assertGreater(datetime.fromisoformat(row["next_run"]), fixed)

    def test_overdue_is_false_before_todays_trigger_time(self):
        """当天 22:05 之前不该报 overdue —— 今天这班还没到点。

        与上一条配套，锁住"未到点就不算漏跑"的语义；同时说明跨零点后
        昨天那班漏跑不会被继续标记为 overdue（overdue 只认当天）。
        """
        scheduler = AutoScheduler()
        scheduler._save = lambda: None
        fixed = datetime(2026, 9, 30, 8, 0)     # 周三非休市；注入固定时刻勿用真实时钟
        scheduler.state["排列3"] = {
            "last_run": (fixed - timedelta(days=2)).isoformat(),
            "last_status": "done",
        }
        row = next(x for x in scheduler.get_status(now=fixed)["lotteries"]
                   if x["lottery"] == "排列3")
        self.assertFalse(row["overdue"])


if __name__ == "__main__":
    unittest.main()
