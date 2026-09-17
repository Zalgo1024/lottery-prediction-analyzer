"""
鲁棒性/过拟合专用计算层（credibility/robustness.py）测试

覆盖：指标结构与取值范围 / 排名与散度工具 / 置换校准 / verdict 规则与效应量门控 /
     七星彩现代段裁剪 / 票面打分入口。
真实数据源（排列5/七星彩），全程 write=False 不落盘。
"""

import math

import numpy as np
import pytest

from credibility.robustness import (
    _jaccard,
    _kendall_tau,
    _ranks_desc,
    _top_number_sets,
    _verdict,
    compute_robustness_report,
    light_check,
    load_trend,
    param_sensitivity,
    pool_drift,
    score_candidate_sets,
    train_test_gap,
    window_stability,
    _modern_cutoff,
)


# ------------------------------------------------------------
# 工具函数
# ------------------------------------------------------------

def test_jaccard_basics():
    assert _jaccard({1, 2}, {1, 2}) == 1.0
    assert _jaccard({1}, {2}) == 0.0
    assert _jaccard(set(), set()) == 1.0
    assert abs(_jaccard({1, 2, 3}, {2, 3, 4}) - 0.5) < 1e-9


def test_kendall_tau_extremes():
    assert _kendall_tau([0, 1, 2], [0, 1, 2]) == 1.0      # 同序
    assert _kendall_tau([0, 1, 2], [2, 1, 0]) == -1.0     # 反序
    assert -1.0 <= _kendall_tau([0, 1, 2, 3], [1, 0, 3, 2]) <= 1.0


def test_ranks_desc():
    assert _ranks_desc([0.9, 0.1, 0.5]) == [0, 2, 1]


# ------------------------------------------------------------
# 号码级热度画像
# ------------------------------------------------------------

def test_top_number_sets_prefers_hot():
    from data.schema import get_schema
    schema = get_schema("排列5")

    class _PS:
        def __init__(self, zones):
            self.zones = zones

    # "第1位" 中 5 反复出现 → 必在 top 半区；无人选的号不在
    cands = [_PS({"第1位": [5], "第2位": [1], "第3位": [2], "第4位": [3], "第5位": [4]})
             for _ in range(20)]
    tops = _top_number_sets(cands, schema, frac=0.5)
    assert 5 in tops["第1位"]
    # 其余位均匀覆盖 → top 半区大小 = max(1, size//2)
    assert len(tops["第1位"]) == max(1, 10 // 2)


# ------------------------------------------------------------
# 真实数据指标（轻量参数）
# ------------------------------------------------------------

def test_window_stability_structure_and_range():
    r = window_stability("排列5", windows=(30, 60), k=20)
    assert 0.0 <= r["stability"] <= 1.0
    assert 0.0 <= r["cross_jaccard"] <= 1.0
    assert r["noise_floor"] > 0.0  # 号码级画像下噪声地板必为正
    assert len(r["pairs"]) == 1


def test_param_sensitivity_structure():
    r = param_sensitivity("排列5", base_window=50, k=20)
    assert 0.0 <= r["sensitivity"] <= 1.0
    assert -1.0 <= r["mean_kendall_tau"] <= 1.0


def test_train_test_gap_calibrated():
    """gap + 置换零分布：结构完整，gap 有界，p95 ≥ p90 ≥ p50。"""
    r = train_test_gap("排列5", min_train=100, k=2, max_folds=5, n_perm=3)
    assert -1.0 <= r["gap"] <= 1.0
    assert r["n_folds"] == 5
    assert r["n_perm"] == 3
    assert r["null_p95"] >= r["null_p90"] >= r["null_p50"] - 1e-9


def test_pool_drift_modern_segment():
    """七星彩必须裁剪到现代段（第7位 0-14），期号阈值来自变点文件。"""
    cutoff = _modern_cutoff("七星彩")
    assert cutoff == 20111  # P2-2 变点：2020-11-08 第7位 0-9→0-14
    assert _modern_cutoff("排列5") == 0  # 无变点 → 不裁剪
    r = pool_drift("七星彩", recent_n=30, base_n=100, n_perm=30)
    assert r["modern_cutoff_issue"] == 20111
    assert isinstance(r["alarm"], bool)
    for z in r["zones"]:
        assert 0.0 <= z["js"] <= math.log(2) + 1e-9


def test_light_check_no_write():
    """light_check 不落盘、结构完整。

    ⚠️ 断言用「调用前后条数不变」而非「文件为空」：trend 文件是真实运行产物，
    常驻 worker 每期都会追加（2026-09-08 实测：worker 写入导致断言 ==[] 恒失败），
    测试必须与既有数据无关。
    """
    before = len(load_trend("排列5"))
    r = light_check("排列5", write=False)
    assert r["mode"] == "light"
    assert r["verdict"] in ("high_robust", "stable", "watch", "overfit_risk")
    assert isinstance(r["alarms"], list)
    assert "不代表号码中奖概率" in r["说明"]
    assert len(load_trend("排列5")) == before  # 未写入


# ------------------------------------------------------------
# verdict 规则（合成 metrics，不碰真实数据）
# ------------------------------------------------------------

def _gap(gap, p95, p90=0.0):
    return {"gap": gap, "in_mean": 0.5, "out_mean": 0.4,
            "null_p50": 0.0, "null_p90": p90, "null_p95": p95, "n_folds": 5, "n_perm": 3}


def test_verdict_overfit_requires_effect_gate():
    """gap 超 P95 但 |gap| < 0.05 → 不告警（效应量门控）；指标优秀 → high_robust。"""
    v, alarms = _verdict({"train_test_gap": _gap(0.03, p95=-0.05, p90=0.05)})
    assert v == "high_robust"
    assert alarms == []


def test_verdict_overfit_fires():
    """gap 超 P95 且超过效应量门控 → overfit_risk。"""
    v, alarms = _verdict({"train_test_gap": _gap(0.30, p95=-0.05)})
    assert v == "overfit_risk"
    assert any("过拟合" in a for a in alarms)


def test_verdict_watch_on_stability():
    """窗口稳定性过低 → watch。"""
    v, alarms = _verdict({"window_stability": {"stability": 0.05, "pairs": []}})
    assert v == "watch"
    assert any("窗口稳定性" in a for a in alarms)


def test_verdict_high_robust_requires_good_metrics():
    """无告警但稳定性一般 → stable；全部优秀 → high_robust。"""
    ws = {"stability": 0.30, "pairs": []}
    v, _ = _verdict({"window_stability": ws})
    assert v == "stable"
    ws["stability"] = 0.80
    v, alarms = _verdict({"window_stability": ws})
    assert v == "high_robust" and alarms == []


# ------------------------------------------------------------
# 全量报告（结构）+ 打分入口
# ------------------------------------------------------------

def test_full_report_structure():
    r = compute_robustness_report("排列5", mode="full", write=False)
    assert r["verdict"] in ("high_robust", "stable", "watch", "overfit_risk")
    for key in ("window_stability", "param_sensitivity", "pool_drift", "train_test_gap"):
        assert key in r["metrics"]
    assert "不代表号码中奖概率" in r["说明"]


def test_score_candidate_sets():
    from data.loader import load_lottery
    records = load_lottery("排列5").records[:50]
    zones = [{"第1位": [i], "第2位": [i], "第3位": [i], "第4位": [i], "第5位": [i]}
             for i in range(3)]
    scores = score_candidate_sets("排列5", zones, records=records)
    assert len(scores) == 3
    assert all(isinstance(s, float) and 0.0 <= s <= 1.0 for s in scores)
