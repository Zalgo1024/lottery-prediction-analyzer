# -*- coding: utf-8 -*-
"""方案 B「采集频率」的护栏：窗口、节拍、幂等、失败可观测、接线存在。

这些用例**不许真的抓外网**：采集脚本用 tmp 下的替身，状态/日志/锁全部由
conftest 的隔离夹具指到 tmp。真要去抓外网的是人工 `--once` 与 21:05 的调度器。
"""
from __future__ import annotations

import inspect
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from web import evidence_schedule as ES


def _t(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2026, 11, 6, h, m, s)


def _fake_script(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fake_collector.py"
    p.write_text(body, encoding="utf-8")
    return p


# ---------------------------------------------------------------- ① 窗口边界
@pytest.mark.parametrize("h,m,want", [
    (20, 59, False), (21, 4, False), (21, 5, True), (21, 15, True),
    (21, 30, True), (21, 45, True), (21, 46, False), (22, 5, False),
])
def test_window_boundaries(h, m, want):
    assert ES.in_window(_t(h, m)) is want


def test_window_constants_match_declared_schedule():
    """窗口必须覆盖三类声明开奖时刻：21:15 / 21:25 / 21:30。"""
    for h, m in ((21, 15), (21, 25), (21, 30)):
        assert ES.in_window(_t(h, m)), f"{h:02d}:{m:02d} 未被窗口覆盖"


def test_next_due_minute_is_on_two_minute_grid():
    assert ES.next_due_minute(_t(21, 5)) == 21 * 60 + 5
    assert ES.next_due_minute(_t(21, 6)) == 21 * 60 + 7
    assert ES.next_due_minute(_t(21, 46)) is None


# ---------------------------------------------------------------- ② 节拍
def test_due_when_no_last_run():
    assert ES.is_due(_t(21, 5), None) is True
    assert ES.is_due(_t(21, 5), "") is True
    assert ES.is_due(_t(21, 5), "不是时间") is True


def test_due_respects_interval():
    now = _t(21, 9)
    assert ES.is_due(now, _t(21, 8).isoformat()) is False      # 60s
    assert ES.is_due(now, _t(21, 7).isoformat()) is True       # 120s
    assert ES.is_due(now, _t(21, 7, 10).isoformat()) is True   # 110s ≥ 110 容忍线


def test_due_after_previous_day_always_true():
    assert ES.is_due(_t(21, 5), datetime(2026, 11, 5, 21, 45).isoformat()) is True


def test_pure_functions_do_not_read_system_time():
    for fn in (ES.in_window, ES.is_due, ES.next_due_minute):
        src = inspect.getsource(fn)
        assert "datetime.now(" not in src and "date.today(" not in src, \
            f"{fn.__name__} 读了系统时间 ⇒ 窗口/节拍无法被测试钉住"


# ---------------------------------------------------------------- ③ 单次采集
def test_run_once_success_writes_state_and_log(tmp_path):
    script = _fake_script(tmp_path, "print('汇总：写入 0 / 幂等跳过 8 / 失败 0')\n")
    state = tmp_path / "state.json"
    log = tmp_path / "collect.log"
    lock = tmp_path / "collect.lock"
    r = ES.run_once(now=_t(21, 5), python=sys.executable, collector=script,
                    state_path=state, log_path=log, lock_path=lock)
    assert r["状态"] == "成功" and r["返回码"] == 0
    assert "汇总" in r and r["耗时"] >= 0
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["last_status"] == "成功" and st["runs_today"] == 1
    assert "成功" in log.read_text(encoding="utf-8")
    assert json.loads(lock.read_text(encoding="utf-8"))["持有"] is False, \
        "锁必须在结束后标记为已释放"


def test_run_once_nonzero_is_partial_failure_not_exception(tmp_path):
    script = _fake_script(tmp_path, "raise SystemExit(1)\n")
    r = ES.run_once(now=_t(21, 7), python=sys.executable, collector=script,
                    state_path=tmp_path / "s.json", log_path=tmp_path / "l.log",
                    lock_path=tmp_path / "k.lock")
    assert r["状态"] == "部分失败" and r["返回码"] == 1
    assert "非零" in r["原因"]


def test_run_once_timeout_is_recorded(monkeypatch, tmp_path):
    def _boom(*a, **kw):
        raise subprocess.TimeoutExpired(cmd="collect", timeout=1)

    monkeypatch.setattr(ES.subprocess, "run", _boom)
    r = ES.run_once(now=_t(21, 9), state_path=tmp_path / "s.json",
                    log_path=tmp_path / "l.log", lock_path=tmp_path / "k.lock")
    assert r["状态"] == "失败" and "超时" in r["原因"]
    assert "超时" in (tmp_path / "l.log").read_text(encoding="utf-8")


def test_run_once_unexpected_error_does_not_raise(monkeypatch, tmp_path):
    def _boom(*a, **kw):
        raise RuntimeError("模拟网络栈炸了")

    monkeypatch.setattr(ES.subprocess, "run", _boom)
    r = ES.run_once(now=_t(21, 11), state_path=tmp_path / "s.json",
                    log_path=tmp_path / "l.log", lock_path=tmp_path / "k.lock")
    assert r["状态"] == "失败" and "RuntimeError" in r["原因"]


def test_single_instance_lock_skips_second_run(tmp_path):
    lock = tmp_path / "k.lock"
    lock.write_text(json.dumps({"持有": True, "时间": _t(21, 5).isoformat(),
                                "pid": 99999}), encoding="utf-8")
    r = ES.run_once(now=_t(21, 5), state_path=tmp_path / "s.json",
                    log_path=tmp_path / "l.log", lock_path=lock)
    assert r["状态"] == "跳过" and "单实例锁" in r["原因"]
    assert json.loads(lock.read_text(encoding="utf-8"))["pid"] == 99999, \
        "别人的锁不该被这轮改写"


def test_stale_lock_is_taken_over_and_run_proceeds(tmp_path):
    lock = tmp_path / "k.lock"
    lock.write_text(json.dumps({"持有": True, "时间": "2000-01-01T00:00:00",
                                "pid": 1}), encoding="utf-8")
    script = _fake_script(tmp_path, "print('汇总：写入 0 / 幂等跳过 8 / 失败 0')\n")
    r = ES.run_once(now=_t(21, 5), python=sys.executable, collector=script,
                    state_path=tmp_path / "s.json", log_path=tmp_path / "l.log",
                    lock_path=lock)
    assert r["状态"] == "成功"
    assert json.loads(lock.read_text(encoding="utf-8"))["持有"] is False


def test_runs_today_resets_on_new_date(tmp_path):
    state = tmp_path / "s.json"
    state.write_text(json.dumps({
        "last_run": datetime(2026, 11, 5, 21, 45).isoformat(),
        "last_date": "2026-11-05", "runs_today": 21,
    }), encoding="utf-8")
    script = _fake_script(tmp_path, "print('汇总：OK')\n")
    ES.run_once(now=_t(21, 5), python=sys.executable, collector=script,
                state_path=state, log_path=tmp_path / "l.log",
                lock_path=tmp_path / "k.lock")
    st = json.loads(state.read_text(encoding="utf-8"))
    assert st["last_date"] == "2026-11-06" and st["runs_today"] == 1


# ---------------------------------------------------------------- ④ 窗口循环
def test_run_window_uses_due_and_stops_at_window_end(monkeypatch, tmp_path):
    times = iter([_t(21, 5), _t(21, 7), _t(21, 9), _t(21, 46)])
    calls = []

    def _fake_once(*, now=None, **kwargs):
        calls.append(now)
        st = ES.load_state(path=tmp_path / "s.json")
        st["last_run"] = now.isoformat()
        ES._save_state(st, path=tmp_path / "s.json")
        return {"状态": "成功", "返回码": 0, "耗时": 0.1, "汇总": "OK"}

    monkeypatch.setattr(ES, "run_once", _fake_once)
    r = ES.run_window(now_fn=lambda: next(times), sleep_fn=lambda s: None,
                      state_path=tmp_path / "s.json",
                      log_path=tmp_path / "l.log", lock_path=tmp_path / "k.lock")
    assert r["退出原因"] == "窗口结束"
    assert [x.minute for x in calls] == [5, 7, 9], "必须严格按 2 分钟节拍"


def test_run_window_outside_window_does_nothing(monkeypatch):
    monkeypatch.setattr(ES, "run_once", lambda **kw: pytest.fail("窗口外不该采集"))
    r = ES.run_window(now_fn=lambda: _t(20, 0), sleep_fn=lambda s: None)
    assert r["轮数"] == 0 and r["退出原因"] == "窗口结束"


# ---------------------------------------------------------------- ⑤ 状态与接线护栏
def test_status_discloses_no_evidence_note_constant():
    assert "未发现矛盾" in ES.NO_EVIDENCE_NOTE


def test_status_reports_window_and_last_run(tmp_path):
    st = ES.status(now=_t(21, 6), state_path=tmp_path / "s.json")
    assert st["当前在窗口内"] is True and st["该跑"] is True
    assert st["本次应采"] == "21:07" and st["节拍秒"] == 120
    assert st["窗口"] == "21:05-21:45"


def test_scheduler_hook_is_actually_called_in_tick():
    """接线护栏：改调度器时把钩子删掉会立刻红。"""
    src = (Path(__file__).resolve().parents[1] / "web" / "scheduler.py").read_text(
        encoding="utf-8")
    assert "self._maybe_collect_evidence(now)" in src
    assert "def _maybe_collect_evidence" in src


def test_collector_script_does_not_write_production_state():
    """采集器只读数据源、只写证据文件：不许出现写 CSV / pending / 预算的入口。"""
    src = (Path(__file__).resolve().parents[1] / "scripts" /
           "collect_draw_evidence.py").read_text(encoding="utf-8")
    for bad in ("record_spend", ".to_csv(", "save_pending", "start_auto_pipeline"):
        assert bad not in src, f"采集器引入了生产副作用：{bad}"


# ---------------------------------------------------------------- ⑥ 调度器钩子
def test_scheduler_hook_fires_inside_window_when_due(monkeypatch):
    import time as _time
    from web.scheduler import AutoScheduler

    monkeypatch.setattr(ES, "ENABLED", True)
    monkeypatch.setattr(ES, "run_once", lambda **kw: {"状态": "成功"})
    monkeypatch.setattr(ES, "load_state", lambda **kw: {"last_run": None})
    s = AutoScheduler()
    assert s._maybe_collect_evidence(_t(21, 5)) is True
    for _ in range(50):
        if not s._evidence_running:
            break
        _time.sleep(0.02)
    assert s._evidence_running is False, "采集线程结束后标志必须复位"


def test_scheduler_hook_is_noop_outside_window(monkeypatch):
    from web.scheduler import AutoScheduler
    monkeypatch.setattr(ES, "ENABLED", True)
    monkeypatch.setattr(ES, "run_once",
                        lambda **kw: pytest.fail("窗口外不该起采集"))
    s = AutoScheduler()
    assert s._maybe_collect_evidence(_t(20, 0)) is False


def test_scheduler_hook_respects_interval(monkeypatch):
    from web.scheduler import AutoScheduler
    monkeypatch.setattr(ES, "ENABLED", True)
    monkeypatch.setattr(ES, "run_once",
                        lambda **kw: pytest.fail("未到节拍不该起采集"))
    monkeypatch.setattr(ES, "load_state",
                        lambda **kw: {"last_run": _t(21, 4).isoformat()})
    s = AutoScheduler()
    assert s._maybe_collect_evidence(_t(21, 5)) is False   # 只隔 60s


# ---------------------------------------------------------------- ⑥ 调度器钩子
def test_scheduler_hook_fires_inside_window_when_due(monkeypatch):
    import time as _time
    from web.scheduler import AutoScheduler

    monkeypatch.setattr(ES, "ENABLED", True)
    monkeypatch.setattr(ES, "run_once", lambda **kw: {"状态": "成功"})
    monkeypatch.setattr(ES, "load_state", lambda **kw: {"last_run": None})
    s = AutoScheduler()
    assert s._maybe_collect_evidence(_t(21, 5)) is True
    for _ in range(50):
        if not s._evidence_running:
            break
        _time.sleep(0.02)
    assert s._evidence_running is False, "采集线程结束后标志必须复位"


def test_scheduler_hook_is_noop_outside_window(monkeypatch):
    from web.scheduler import AutoScheduler
    monkeypatch.setattr(ES, "ENABLED", True)
    monkeypatch.setattr(ES, "run_once",
                        lambda **kw: pytest.fail("窗口外不该起采集"))
    s = AutoScheduler()
    assert s._maybe_collect_evidence(_t(20, 0)) is False


def test_scheduler_hook_respects_interval(monkeypatch):
    from web.scheduler import AutoScheduler
    monkeypatch.setattr(ES, "ENABLED", True)
    monkeypatch.setattr(ES, "run_once",
                        lambda **kw: pytest.fail("未到节拍不该起采集"))
    monkeypatch.setattr(ES, "load_state",
                        lambda **kw: {"last_run": _t(21, 4).isoformat()})
    s = AutoScheduler()
    assert s._maybe_collect_evidence(_t(21, 5)) is False   # 只隔 60s
