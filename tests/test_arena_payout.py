# -*- coding: utf-8 -*-
"""tests/test_arena_payout.py —— 净收益侧向量化结算的口径锁。

三条最要紧的：
1. **与唯一结算内核逐期逐分相等**（`evaluate_period`）—— 快路任何一处口径偏差
   都不会报错，只会让净收益悄悄偏几个点；
2. **预载不改变金额语义**（IO 批量化不许顺手改口径）；
3. **金额侧 ρ̂ 用样本方差**，不能沿用二值的 `p(1-p)`。
"""
from __future__ import annotations

import numpy as np
import pytest

from data import prize_amounts as PA
from ev.arena import periods as PD
from ev.arena import payout as MONEY
from ev.arena import stats as ST
from ev.arena.runner import _records_for
from ev.coverage_backtest import build_pool, evaluate_period
from ev.profit.rules import rules_for

LOT = "双色球"
K = 20


def _fixture(periods: int = 40):
    issues = PD.build_periods(LOT, periods)
    recs = _records_for(LOT, issues)
    pool = build_pool(LOT, 200, seed=7)
    return recs, pool[:K]


# ---------------------------------------------------------------- 对账
def test_money_matches_evaluate_period():
    """★ 地基：每期实得必须与逐票真值**逐期逐分相等**。"""
    recs, tks = _fixture(40)
    m = MONEY.money_series(LOT, tks, recs, rule_mode="periodic")
    cache = {}
    truth = np.array([evaluate_period(LOT, tks, r, r.期号, cache)["总实得"]
                      for r in recs], dtype=float)
    fast = np.asarray(m["每期实得"], dtype=float)
    assert fast.shape == truth.shape
    assert np.abs(fast - truth).max() <= 0.005, (
        f"最大逐期偏差 {np.abs(fast - truth).max():.4f} 元")


def test_money_matches_evaluate_period_when_versions_differ():
    """★★ `current` 口径（判级版本 ≠ 当期版本）也必须逐期逐分相等。

    这就是上面那条 bug 的对账缺口：旧版对账脚本把「判级版本≠当期版本」的期
    **直接跳过**，于是大乐透 current 口径 2,831 期里只验了 107 期。
    现在 `evaluate_period` 支持 `rule_issue=`，缺口补上。
    """
    all_issues = PD.build_periods("大乐透", None)
    issues = list(all_issues[:40]) + list(all_issues[-20:])   # 头段 + 尾段 ⇒ 必跨版本
    recs = _records_for("大乐透", issues)
    pool = build_pool("大乐透", 200, seed=7)
    tks = pool[:10]
    target = recs[-1].期号
    PA.preload("大乐透", [r.期号 for r in recs])
    m = MONEY.money_series("大乐透", tks, recs, rule_mode="current",
                           target_issue=target)
    cache = {}
    truth = np.array([evaluate_period("大乐透", tks, r, r.期号, cache,
                                      rule_issue=target)["总实得"]
                      for r in recs], dtype=float)
    fast = np.asarray(m["每期实得"], dtype=float)
    assert np.abs(fast - truth).max() <= 0.005, (
        f"最大逐期偏差 {np.abs(fast - truth).max():.4f} 元")
    # 夹具必须真的跨版本，否则这条测试相当于没测
    vers = {rules_for("大乐透", r.期号).rule_version for r in recs}
    assert rules_for("大乐透", target).rule_version in vers
    assert len(vers) > 1, f"夹具失效：60 期只覆盖 {vers}"


def test_cost_uses_actual_bet_count():
    """成本一律 Σ实际注数×2，禁用 n*K*2 的估算式。"""
    recs, tks = _fixture(20)
    m = MONEY.money_series(LOT, tks, recs, rule_mode="periodic")
    assert m["成本"] == pytest.approx(K * 2.0 * len(recs))
    assert np.allclose(m["每期成本"], K * 2.0)


def test_series_length_equals_periods():
    recs, tks = _fixture(25)
    m = MONEY.money_series(LOT, tks, recs, rule_mode="periodic")
    assert len(m["序列"]) == len(recs) == m["期数"]
    assert len(m["每期实得"]) == len(recs)
    assert np.allclose(m["序列"], m["每期实得"] - m["每期成本"])


def test_net_equals_gross_minus_cost():
    recs, tks = _fixture(15)
    m = MONEY.money_series(LOT, tks, recs, rule_mode="periodic")
    assert m["净收益"] == pytest.approx(m["总实得"] - m["成本"])


def test_net_per_100_is_ratio_of_totals():
    recs, tks = _fixture(15)
    m = MONEY.money_series(LOT, tks, recs, rule_mode="periodic")
    assert m["净收益每100元"] == pytest.approx(m["净收益"] / m["成本"] * 100)


# ---------------------------------------------------------------- 待定格的判据
def test_pending_mask_invariant():
    """`待定格` 必须与 `需逐票兜底` 一一对应，且待定格上金额恒为 0（不静默填数）。"""
    for iss in (3001, 26121):
        grid, meta = MONEY.amount_grid(LOT, iss)
        assert bool(meta["待定格"].any()) == bool(meta["需逐票兜底"])
        if meta["待定格"].any():
            assert (grid[meta["待定格"]] == 0.0).all()


def test_fallback_only_when_pending_cell_is_hit():
    """没命中待定奖级的期**不**回退逐票（早期期段 52% 的期都不该回退）。"""
    recs, tks = _fixture(60)
    m = MONEY.money_series(LOT, tks, recs, rule_mode="periodic")
    # 小额票集在 60 期里几乎不可能中一等/二等（待定奖级）
    assert m["逐票兜底期数"] == 0


# ---------------------------------------------------------------- 预载
def test_preload_does_not_change_amounts():
    """★ 预载只省 IO，不许改金额。"""
    PA.clear_preload()
    before = [PA.actual_per_bet(LOT, iss, g) for iss in (3001, 26121) for g in ("一等", "六等")]
    PA.preload(LOT, [3001, 26121])
    after = [PA.actual_per_bet(LOT, iss, g) for iss in (3001, 26121) for g in ("一等", "六等")]
    assert before == after


def test_preload_is_idempotent():
    PA.clear_preload()
    first = PA.preload(LOT, [26121])
    second = PA.preload(LOT, [26121])
    assert first["载入期数"] == 1
    assert second["载入期数"] == 0          # 已载入 ⇒ 跳过，不重复查库


# ---------------------------------------------------------------- 规则版本
def test_rule_issue_separates_grading_version():
    """`current` 模式：判级用最新版本，金额网格也必须按判级版本建。

    大乐透 5 个规则版本的固定奖额不同 ⇒ 「判成 2026 四等却发 2007 的钱」是错的。
    """
    old = rules_for("大乐透", 7001).rule_version
    new = rules_for("大乐透", 26120).rule_version
    assert old != new, "夹具失效：大乐透应当有多个规则版本"
    _g1, m_old = MONEY.amount_grid("大乐透", 7001, rule_issue=7001)
    _g2, m_new = MONEY.amount_grid("大乐透", 7001, rule_issue=26120)
    assert m_old["规则版本"] == old
    assert m_new["规则版本"] == new


def test_fixed_grade_never_looked_up_by_name_across_versions():
    """★★ 跨规则版本时**固定奖不许按奖级名查库**。

    踩过的坑（2026-10-24）：大乐透 2019 规则里 (2,1) 是九等 5 元，2026 规则里
    (2,1) 是七等 5 元。判级用 2026 表、金额按名字查 2019 期的「七等」（=100 元）
    ⇒ 固定奖整体放大 ~6 倍，current 口径返奖率算成 163.8%（应为固定奖 27.75%）。

    判据：跨版本时，2026 版的固定奖金额必须**恰好等于**规则表，与当期库里有
    多少钱无关；同版本时才允许用实测（派奖期 = 基本 + 派奖）。
    """
    from ev.profit.rules import rules_for as _rf
    new_rules = _rf("大乐透", 26120)
    assert new_rules.rule_version == "dlt-2026-02"

    for old_issue in (7001, 15001, 19001, 24001):
        amt, notes, _fb = MONEY.grade_amounts("大乐透", old_issue, rule_issue=26120)
        assert _rf("大乐透", old_issue).rule_version != "dlt-2026-02"
        for g, rule in new_rules.grades.items():
            if rule.kind != "fixed":
                continue
            assert amt[g] == pytest.approx(float(rule.fixed)), (
                f"{old_issue} 的 {g} 取了 {amt[g]}，应取判级版本固定额 {rule.fixed}")
            assert notes[g] == "规则表(判级版本)"

    # 同版本 ⇒ 允许用实测（派奖期口径），且口径标注不能是「判级版本」
    amt_same, notes_same, _fb2 = MONEY.grade_amounts("大乐透", 26120, rule_issue=26120)
    assert "规则表(判级版本)" not in notes_same.values()
    assert amt_same["一等"] > 0


def test_current_mode_dlt_return_rate_is_plausible():
    """反事实口径的期望返奖率必须落在官方量级（固定奖 27.75% + 浮动奖）。

    这是**量级判据**而不是精确值：只用来挡住「同名奖级错配」这类会放大数倍的
    实现缺陷。精确对账走 `scripts/oneoff/_arena_money_check.py`（逐期逐分）。
    """
    from math import comb
    from ev.profit.rules import rules_for as _rf
    from ev.tier_backtest import load_test_recs

    recs = load_test_recs("大乐透")[:120]
    from data import prize_amounts as PA
    PA.preload("大乐透", [r.期号 for r in recs])
    r = _rf("大乐透", recs[-1].期号)
    grids = []
    for rec in recs:
        g, _m = MONEY.amount_grid("大乐透", rec.期号, rule_issue=recs[-1].期号)
        grids.append(g)
    G = np.mean(np.stack(grids), axis=0)
    n_out, n_in = 35, 12

    def hyper(n_pop, n_succ, n_draw):
        tot = comb(n_pop, n_draw)
        return np.array([comb(n_succ, k) * comb(n_pop - n_succ, n_draw - k) / tot
                         for k in range(n_succ + 1)])

    exp_pay = float(hyper(n_out, 5, 5) @ G @ hyper(n_in, 2, 2))
    # 官方返奖率 51%；允许浮动奖用历史均值带来的偏差
    assert 0.7 < exp_pay < 1.6, f"期望单注实得 {exp_pay:.3f} 元 不在合理区间"
    assert int(r.outer_width) == 5


# ---------------------------------------------------------------- 税
def test_tax_is_a_separate_line():
    """主口径税前：`net` 不受税影响，税后单列。"""
    recs, tks = _fixture(20)
    m0 = MONEY.money_series(LOT, tks, recs, rule_mode="periodic", tax=False)
    m1 = MONEY.money_series(LOT, tks, recs, rule_mode="periodic", tax=True)
    assert m0["税"] == 0.0
    assert m0["净收益"] == pytest.approx(m1["净收益"])
    assert m1["税后净收益"] <= m1["净收益"] + 1e-9


# ---------------------------------------------------------------- 倍投（P7 内核）
def test_default_multipliers_equals_all_ones():
    recs, tks = _fixture(12)
    a = MONEY.money_series(LOT, tks, recs, rule_mode="periodic")
    b = MONEY.money_series(LOT, tks, recs, rule_mode="periodic",
                           multipliers=[1] * len(tks))
    assert np.allclose(a["每期实得"], b["每期实得"])
    assert a["成本"] == b["成本"]
    assert a["倍数分布"] == {1: len(tks)}


def test_multipliers_scale_money_not_hit_structure():
    """★ P7 铁律：倍投**只改金额与成本**，不改「哪一期中了」的结构。"""
    recs, tks = _fixture(20)
    m1 = MONEY.money_series(LOT, tks, recs, rule_mode="periodic")
    m2 = MONEY.money_series(LOT, tks, recs, rule_mode="periodic",
                            multipliers=[2] * len(tks))
    assert np.array_equal(m1["每期实得"] > 0, m2["每期实得"] > 0)
    assert np.allclose(m2["每期实得"], m1["每期实得"] * 2)
    assert np.allclose(m2["每期成本"], m1["每期成本"] * 2)
    assert m2["总注数每期"] == 2 * m1["总注数每期"]
    # 净额 = 2×实得 − 2×成本 ⇒ 恰好是净额的 2 倍
    assert np.allclose(m2["序列"], m1["序列"] * 2)


def test_mixed_multipliers_count_and_cost():
    """Σ倍数 = 每期实际注数；成本按 Σ倍数 累加（不是按去重票数）。"""
    recs, tks = _fixture(12)
    mix = np.array([2, 2] + [1] * (len(tks) - 2), dtype=int)
    m = MONEY.money_series(LOT, tks, recs, rule_mode="periodic", multipliers=mix)
    total = int(mix.sum())
    assert m["总注数每期"] == total
    assert m["成本"] == pytest.approx(total * 2.0 * len(recs))
    assert m["倍数分布"] == {1: len(tks) - 2, 2: 2}


def test_multipliers_length_must_match():
    recs, tks = _fixture(8)
    with pytest.raises(ValueError, match="倍数向量长度"):
        MONEY.money_series(LOT, tks, recs, multipliers=[1] * (len(tks) + 1))


def test_multipliers_must_be_positive():
    recs, tks = _fixture(8)
    bad = [1] * len(tks)
    bad[0] = 0
    with pytest.raises(ValueError, match="倍数必须 ≥ 1"):
        MONEY.money_series(LOT, tks, recs, multipliers=bad)


# ---------------------------------------------------------------- 统计层
def test_pair_money_uses_sample_variance():
    a = np.array([-400.0, 100.0, -400.0, 5000.0])
    b = np.array([-400.0, -400.0, 300.0, 900.0])
    r = ST.pair(a, b, issues_a=[1, 2, 3, 4], issues_b=[1, 2, 3, 4], metric="money")
    assert r["口径"] == "money"
    assert "A达标率" not in r                 # 金额侧没有「达标率」
    assert r["A均值"] == pytest.approx(float(a.mean()))
    # ★ 金额侧的 ρ̂ 必须由**样本方差**推出（二值那套 p(1-p) 会算出荒谬值）
    #   var(a-b) = va + vb − 2cov ⇒ cov = (va + vb − var(a−b)) / 2
    va, vb = float(a.var(ddof=1)), float(b.var(ddof=1))
    vd = float((a - b).var(ddof=1))
    cov = (va + vb - vd) / 2
    assert r["配对ρ̂"] == pytest.approx(cov / (va * vb) ** 0.5)


def test_pair_money_zero_variance_is_nan():
    """一臂严格常数 ⇒ ρ 无定义（nan），不能编一个数。"""
    a = np.array([-400.0, 100.0, 5000.0])
    b = np.array([-400.0, -400.0, -400.0])
    r = ST.pair(a, b, issues_a=[1, 2, 3], issues_b=[1, 2, 3], metric="money")
    assert np.isnan(r["配对ρ̂"])


def test_pair_binary_keeps_legacy_keys():
    a = np.array([1, 0, 1, 1])
    b = np.array([0, 0, 1, 0])
    r = ST.pair(a, b, issues_a=[1, 2, 3, 4], issues_b=[1, 2, 3, 4])
    assert r["口径"] == "binary"
    assert r["A达标率"] == pytest.approx(0.75)
    assert r["B达标率"] == pytest.approx(0.25)
    assert r["A均值"] == pytest.approx(0.75)


def test_pair_rejects_unknown_metric():
    with pytest.raises(ValueError, match="metric"):
        ST.pair(np.zeros(3), np.zeros(3), issues_a=[1, 2, 3], issues_b=[1, 2, 3],
                metric="payout")


def test_pair_still_rejects_mismatched_periods():
    with pytest.raises(ValueError, match="期窗不一致"):
        ST.pair(np.zeros(3), np.zeros(3), issues_a=[1, 2, 3], issues_b=[1, 2, 4],
                metric="money")
