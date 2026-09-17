"""距离画像（ev/distance_metrics.py）回归测试

锁三个口径：
1. 基线分布来自真实开奖随机配对，比例和为 1，且中位数落在理论邻域
2. 观测分布 = 总可中位数 − 总命中（合法记录），非法记录被剔除
3. 中位数聚合（含偶数样本取中间均值）
"""

import pytest

import ev.distance_metrics as dm
from data.schema import get_schema


def test_baseline_sums_to_one_and_median_range():
    for lot in ("双色球", "排列5"):
        r = dm.distance_stats(lot, mc_trials=500, seed=1)
        total = sum(r["基线比例"].values())
        assert abs(total - 1.0) < 1e-9
        assert r["基线中位距离"] is not None


def test_ssq_baseline_median_near_theory():
    # 双色球 E[D] ≈ 7 − (6·6/33 + 1/16) ≈ 5.85 → 中位数应为 6
    r = dm.distance_stats("双色球", mc_trials=2000, seed=7)
    assert 5.0 <= r["基线中位距离"] <= 7.0


def test_observed_distribution_and_invalid_excluded(monkeypatch):
    choose = get_schema("排列5").total_choose  # 5
    fake = [
        {"期号": 1, "总命中": 3, "valid_prediction": True},   # D=2
        {"期号": 2, "总命中": 5, "valid_prediction": True},   # D=0
        {"期号": 3, "总命中": 5, "valid_prediction": False},  # 非法 → 剔除
        {"期号": 4, "总命中": 2, "valid_prediction": True},   # D=3
    ]
    monkeypatch.setattr(dm, "load_feedback_history", lambda lot: list(fake))
    r = dm.distance_stats("排列5", mc_trials=200, seed=3)
    assert r["样本数"] == 3
    assert r["观测分布"] == {"0": 1, "2": 1, "3": 1}
    assert r["观测中位距离"] == 2.0
    assert any("剔除" in n for n in r["备注"])


def test_median_even_sample(monkeypatch):
    fake = [
        {"期号": 1, "总命中": 5, "valid_prediction": True},  # D=0
        {"期号": 2, "总命中": 4, "valid_prediction": True},  # D=1
        {"期号": 3, "总命中": 3, "valid_prediction": True},  # D=2
        {"期号": 4, "总命中": 2, "valid_prediction": True},  # D=3
    ]
    monkeypatch.setattr(dm, "load_feedback_history", lambda lot: list(fake))
    r = dm.distance_stats("排列5", mc_trials=200, seed=3)
    assert r["观测中位距离"] == 1.5


def test_empty_history_verdict(monkeypatch):
    monkeypatch.setattr(dm, "load_feedback_history", lambda lot: [])
    r = dm.distance_stats("排列5", mc_trials=200, seed=3)
    assert r["样本数"] == 0
    assert "暂无可评估样本" in r["结论"]


def test_close_rate_fields():
    r = dm.distance_stats("排列5", mc_trials=500, seed=11)
    assert r["近距离率"] is None or 0.0 <= r["近距离率"] <= 1.0
    assert 0.0 <= r["基线近距离率"] <= 1.0
