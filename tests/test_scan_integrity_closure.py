"""体检脚本 scan_data_integrity 的休市感知（2026-10-02）：滞后=开奖日历口径。

- 真数据用例只读真实 CSV；
- 合成数据用例 monkeypatch 目标模块自身的属性（si.pick_hist_file / si.read_csv_any），
  不碰 data.feedback / data.loader（项目 monkeypatch 铁律：patch 目标模块自己的属性）。
"""

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "scan_data_integrity", BASE / "scripts" / "scan_data_integrity.py")
si = importlib.util.module_from_spec(_spec)
sys.modules.setdefault("scan_data_integrity", si)
_spec.loader.exec_module(si)


def _row(issue, d):
    return {"期号": issue, "开奖日期": d, "号码1": "1", "号码2": "2",
            "号码3": "3", "号码4": "4", "号码5": "5"}


def test_lag_zero_during_closure_real_data():
    """真数据（只读）：休市日 10-02 下排列5 最新 09-30 → 滞后 0、距今 2、无假缺失。"""
    rep = si.scan_lottery("排列5", date(2026, 10, 2))
    assert rep["滞后天数"] == 0
    assert rep["距今天数"] == 2
    assert rep["疑似未抓取的开奖日"] == []


def test_miss_excludes_closure_days(monkeypatch):
    """合成数据：latest=09-28（排列5 09-29/09-30 开、10-01 休市）→ miss 不含 10-01。"""
    rows = [_row("2026260", "2026-09-26"), _row("2026261", "2026-09-27"),
            _row("2026262", "2026-09-28")]
    monkeypatch.setattr(si, "pick_hist_file", lambda name: Path("fake.csv"))
    monkeypatch.setattr(si, "read_csv_any", lambda path: (rows, "utf-8"))
    rep = si.scan_lottery("排列5", date(2026, 10, 2))
    miss = rep["疑似未抓取的开奖日"]
    assert miss == ["2026-09-29", "2026-09-30"]      # 10-01~10-04 休市剔除
    assert rep["滞后天数"] == 2                       # 开奖日历口径：最近应开奖 09-30
    assert rep["距今天数"] == 4                       # 自然日差仅参考


def test_lag_counts_real_gap_outside_closure():
    """非休市期正常报滞后：latest=09-26、today=09-30 → 每日彩种滞后 4 天
    （27/28/29/30 都是排列5开奖日，expected=09-30）。"""
    rows = [_row("2026259", "2026-09-25"), _row("2026260", "2026-09-26")]
    rep = _run_scan(rows, date(2026, 9, 30))
    assert rep["滞后天数"] == 4
    assert rep["疑似未抓取的开奖日"] == ["2026-09-27", "2026-09-28", "2026-09-29"]


def _run_scan(rows, today):
    orig_pick, orig_read = si.pick_hist_file, si.read_csv_any
    si.pick_hist_file = lambda name: Path("fake.csv")
    si.read_csv_any = lambda path: (rows, "utf-8")
    try:
        return si.scan_lottery("排列5", today)
    finally:
        si.pick_hist_file, si.read_csv_any = orig_pick, orig_read


def test_daily_lotteries_no_false_alarm_during_closure():
    """六彩种真数据（只读）在休市日 10-02 下滞后天数均为 0。"""
    for lot in ("排列3", "排列5", "福彩3D", "双色球", "大乐透", "七星彩"):
        rep = si.scan_lottery(lot, date(2026, 10, 2))
        assert rep["滞后天数"] == 0, (lot, rep["滞后天数"])


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
