# -*- coding: utf-8 -*-
"""tests/test_coverage_backtest.py —— Phase 1 覆盖策略回测的增量测试（不碰现有红线）。

仅验证新增模块的正确性：确定性、指标语义、ssq K=200 饱和检查、select_coverage 契约。
不修改任何现有选号/预测逻辑，故不影响 test_prediction_group_count / test_ticket_size /
test_gap_select / test_robust_tiers。
"""
from types import SimpleNamespace

from ev import coverage_backtest as cb


def _ssq_rec(red, blue):
    return SimpleNamespace(zone_numbers={"红球": list(red), "蓝球": list(blue)})


def _ssq_ticket(red, blue):
    return {"红球": list(red), "蓝球": list(blue)}


def test_evaluate_period_metrics_semantics():
    """已知开奖下，命中注数 / ≥5元 / ≥10元 判定正确。"""
    draw = _ssq_rec([1, 2, 3,4,5,6], [16])
    cache = {}
    a = _ssq_ticket([7,8,9,10,11,12], [16])   # 0红+1蓝 → 六等(5元)
    b = _ssq_ticket([1,2,3,4,5,7], [1])        # 5红+0蓝 → 四等(200元)
    c = _ssq_ticket([7,8,9,10,11,12], [1])     # 全错 → 未中
    ev = cb.evaluate_period("双色球", [a, b, c], draw, "TEST", cache)
    assert ev["命中注数"] == 2
    assert ev["≥5元"] is True
    assert ev["≥10元"] is True
    assert ev["总实得"] == 205.0


def test_evaluate_period_no_win():
    draw = _ssq_rec([1, 2, 3, 4, 5, 6], [16])
    c = _ssq_ticket([7, 8, 9, 10, 11, 12], [1])
    ev = cb.evaluate_period("双色球", [c], draw, "TEST", {})
    assert ev["命中注数"] == 0
    assert ev["≥5元"] is False
    assert ev["≥10元"] is False
    assert ev["总实得"] == 0.0


def test_select_coverage_returns_k_tickets():
    """select_coverage 从同池返回 K 张合法票，格式与 select_tickets['选中'] 对齐。"""
    from ev.selection import select_coverage
    pool = cb.build_pool("双色球", 500, seed=7)
    picked = select_coverage("双色球", pool, 30, 5, seed=7)
    assert len(picked) == 30
    for t in picked:
        assert set(t.keys()) >= {"红球", "蓝球"}
        assert len(t["红球"]) == 6 and len(t["蓝球"]) == 1


def test_determinism_same_seed():
    """固定 seed 下 run_arm 结果可复现（无随机漂移）。"""
    r1 = cb.run_arm("双色球", "coverage", K=50, periods=120, pool_size=800, seed=42)
    r2 = cb.run_arm("双色球", "coverage", K=50, periods=120, pool_size=800, seed=42)
    assert r1["至少中5元期占比"] == r2["至少中5元期占比"]
    assert r1["净收益"] == r2["净收益"]
    assert r1["中奖次数"] == r2["中奖次数"]


def test_ssq_k200_saturation_no_false_gain():
    """ssq K=200 ≫ 16 蓝球 ⇒ coverage 与 random 在 5 元门槛都应近饱和（差异在噪声内）。

    防止把『覆盖率提升』误报为增益：当 K 足够大、门槛只要求命中蓝球时，两种选号
    都会接近 100% 至少中 5 元，coverage 不应显著优于 random。
    """
    cov = cb.run_arm("双色球", "coverage", K=200, periods=300, pool_size=1500, seed=1)
    rnd = cb.run_arm("双色球", "random", K=200, periods=300, pool_size=1500, seed=1)
    assert cov["至少中5元期占比"] >= 0.99
    assert rnd["至少中5元期占比"] >= 0.99
    assert abs(cov["至少中5元期占比"] - rnd["至少中5元期占比"]) <= 0.05


def test_compare_returns_three_arms():
    """compare 对每 K 给出 coverage/random/top_score 三臂指标。"""
    cmp = cb.compare("双色球", K_grid=[10, 25], periods=100, pool_size=600, seed=3)
    assert cmp["彩种"] == "双色球"
    assert len(cmp["对照表"]) == 2
    for row in cmp["对照表"]:
        for m in ("coverage", "random", "top_score"):
            assert f"{m}_≥5元" in row
            assert f"{m}_净收益" in row
