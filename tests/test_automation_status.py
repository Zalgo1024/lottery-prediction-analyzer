# -*- coding: utf-8 -*-
"""- 自动化状态追踪：旋转历史窗口不能淘汰主彩种（双/大）的最近一次记录。

真实事故：数字型/七星彩每日 3-4 次流水把大乐透（周几彩种）条目轮转出
MAX_HISTORY=20 窗口 → 看板误报"该彩种尚未运行过自动流水线"。
修复：last_run_per_lottery 持久映射不受历史窗口淘汰。
"""
import json
from pathlib import Path

import pytest

from data import automation_status as A


@pytest.fixture(autouse=True)
def _isolated_status(tmp_path, monkeypatch):
    """每次测试用独立 status.json 文件，避免污染真实日志。"""
    f = tmp_path / "status.json"
    monkeypatch.setattr(A, "STATUS_FILE", f)
    yield f


def _record(lot, success=True, msg="ok", t="2026-09-05T22:05:00"):
    A.record_run(lot, success, msg, steps=[msg], duration_sec=1.0,
                 predictions=[], draw_numbers={"期号": 26001, "红球": [1, 2, 3],
                                              "蓝球": [4], "日期": "2026-09-05"})


def test_per_lottery_map_populated(tmp_path):
    _record("大乐透")
    _record("双色球")
    state = json.loads(Path(tmp_path / "status.json").read_text(encoding="utf-8"))
    assert set(state["last_run_per_lottery"]) == {"大乐透", "双色球"}


def test_history_rotation_does_not_drop_per_lottery(tmp_path):
    """数字型高频流水不能把周几彩种挤掉。"""
    _record("大乐透")                       # 真实最近一次（时间是 now）
    # 接下来灌满 50 条数字型（达到 MAX_HISTORY 上限）
    for i in range(A.MAX_HISTORY):
        _record("七星彩")
    status = A.get_status()
    by_lot = status["last_run_by_lottery"]
    assert "大乐透" in by_lot, "大乐透应仍在持久映射中"
    assert len(status["history"]) == A.MAX_HISTORY


def test_per_lottery_picks_newest_on_duplicate_writes(tmp_path):
    _record("双色球")
    _record("双色球")
    status = A.get_status()
    by_lot = status["last_run_by_lottery"]
    assert "双色球" in by_lot
    # 第二次写一定是最近时间（ISO 字符串字典序 == 时间序）
    assert by_lot["双色球"]["time"] >= status["last_run"]["time"]


def test_backfill_old_status_file(tmp_path):
    """模拟升级前留下的 status.json 无 last_run_per_lottery：首次加载自动回填。"""
    f = tmp_path / "status.json"
    f.write_text(json.dumps({
        "last_run": {"time": "2026-09-01T22:05:00", "lottery": "大乐透",
                     "success": True, "message": "ok", "steps": [], "duration_sec": 1,
                     "predictions": [], "draw_numbers": {}, "hit_summary": "",
                     "hit_records": [], "anomaly_flags": [], "retrain": None},
        "history": [
            {"time": "2026-09-01T22:05:00", "lottery": "大乐透",
             "success": True, "message": "ok", "steps": [], "duration_sec": 1,
             "predictions": [], "draw_numbers": {}, "hit_summary": "",
             "hit_records": [], "anomaly_flags": [], "retrain": None},
            {"time": "2026-08-31T22:06:00", "lottery": "双色球",
             "success": True, "message": "ok", "steps": [], "duration_sec": 1,
             "predictions": [], "draw_numbers": {}, "hit_summary": "",
             "hit_records": [], "anomaly_flags": [], "retrain": None},
        ],
        "failed_count": 0, "last_success": "2026-09-01T22:05:00", "last_failed": None,
    }, ensure_ascii=False), encoding="utf-8")
    # _load 自动触发回填并回写
    state = A._load()
    assert "last_run_per_lottery" in state
    assert set(state["last_run_per_lottery"]) == {"大乐透", "双色球"}
    # 写入盘后应包含新字段
    on_disk = json.loads(f.read_text(encoding="utf-8"))
    assert "last_run_per_lottery" in on_disk


def test_get_status_exposes_by_lottery_even_with_empty_history(tmp_path):
    """- 关键测试：旋转历史清空后，大乐透仍能从持久映射露出。"""
    _record("大乐透", t="2026-09-02T22:05:30")
    # 模拟 history 被人为清空（极少见，但模拟极端轮转）
    f = Path(tmp_path / "status.json")
    state = json.loads(f.read_text(encoding="utf-8"))
    state["history"] = []                    # 历史清空
    state["last_run"] = None
    f.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    status = A.get_status()
    assert "大乐透" in status["last_run_by_lottery"]


def test_corrupted_file_returns_default(tmp_path):
    """损坏的 JSON 不应让 _load 崩溃，应返回默认结构。"""
    f = tmp_path / "status.json"
    f.write_text("\xff\xfe not valid utf-8 \xff", encoding="utf-8")
    state = A._load()
    assert state["last_run"] is None
    assert state["last_run_per_lottery"] == {}