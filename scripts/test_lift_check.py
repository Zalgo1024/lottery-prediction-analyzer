# -*- coding: utf-8 -*-
"""lift_check 契约测试：分档切点、同分同档、命中率/Lift 口径、覆盖率诚实报告。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts.lift_check import analyze, bin_index, collect, quantile_bins


def _rec(score, grade="未中", valid=True):
    r = {"置信度": score, "中奖等级": grade, "valid_prediction": valid}
    if grade == "直选":
        r["中奖玩法"] = "直选"
    return r


# ------------------------------------------------------------
# 1. 分档
# ------------------------------------------------------------

def test_quantile_bins_returns_n_minus_1_cuts():
    scores = [i / 100 for i in range(100)]          # 0.00~0.99 共 100 个唯一值
    cuts = quantile_bins(scores, 5)
    assert len(cuts) == 4
    assert cuts == sorted(cuts)


def test_same_score_same_bin():
    """同分必须落同一档（切点按唯一值取， ties 不拆档）。"""
    scores = [0.1] * 50 + [0.9] * 50
    cuts = quantile_bins(scores, 5)
    for s in scores:
        assert bin_index(s, cuts, 5) == bin_index(scores[0], cuts, 5)
    assert bin_index(0.9, cuts, 5) >= bin_index(0.1, cuts, 5)


def test_bin_index_monotonic():
    cuts = [0.2, 0.4, 0.6, 0.8]
    # 切点值本身留在低档（score > cut 才升级）；超过最后切点即顶档
    assert [bin_index(s, cuts, 5) for s in (0.0, 0.2, 0.21, 0.81, 0.99)] == [0, 0, 1, 4, 4]
    assert bin_index(0.4, cuts, 5) == 1 and bin_index(0.6, cuts, 5) == 2


# ------------------------------------------------------------
# 2. lift 口径
# ------------------------------------------------------------

def test_analyze_lift_higher_score_bin_higher_rate():
    """合成数据：低分全不中、中段全中、高分半中 → 各档 Lift 与口径核对。"""
    recs = []
    for i in range(40):
        recs.append(_rec(0.1 + i * 0.001))                     # 低分段：不中
    for i in range(20):
        recs.append(_rec(0.5 + i * 0.001, "六等"))             # 中段：全中
    for i in range(40):
        recs.append(_rec(0.9 + i * 0.001, "六等" if i % 2 else "未中"))  # 高分段：半中
    res = analyze(recs, 5)
    assert res is not None
    assert res["total_n"] == 100 and res["total_hits"] == 40   # 20 + 20
    assert abs(res["overall"] - 0.4) < 1e-9
    lift = {r["档"]: r["Lift"] for r in res["rows"] if r["注数"]}
    assert lift["Q1"] == 0.0                                   # 最低档全不中
    assert lift["Q5"] > lift["Q1"]
    assert max(lift.values()) > 1.5                            # 中段全中档 Lift≈2
    # 桶注数合计 = 总数
    assert sum(r["注数"] for r in res["rows"]) == 100


def test_analyze_handles_tiny_sample():
    res = analyze([_rec(0.5)], 5)
    assert res is not None and res["rows"][0]["注数"] == 1


# ------------------------------------------------------------
# 3. collect：真预测口径 + 缺分诚实计数
# ------------------------------------------------------------

def test_collect_counts_missing_scores(monkeypatch):
    import data.feedback as fb
    data = [
        {"置信度": 0.4, "中奖等级": "未中", "valid_prediction": True},
        {"置信度": 0.6, "中奖等级": "六等", "valid_prediction": True},
        {"中奖等级": "六等", "valid_prediction": True},            # 无分（历史）
        {"置信度": 0.9, "中奖等级": "六等", "valid_prediction": False},  # 训练（默认排除）
    ]
    monkeypatch.setattr(fb, "load_feedback_history", lambda n, lookback=None: list(data))
    scored, missing, total = collect("双色球", include_train=False)
    assert len(scored) == 2 and missing == 1 and total == 4
    scored_t, missing_t, _ = collect("双色球", include_train=True)
    assert len(scored_t) == 3 and missing_t == 1
    # 命中判定走 中奖等级/中奖玩法
    assert [r["_hit"] for r in scored] == [0, 1]
