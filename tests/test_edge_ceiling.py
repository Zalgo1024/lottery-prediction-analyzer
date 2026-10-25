# -*- coding: utf-8 -*-
"""ev/edge_ceiling.py 的口径锁。

这些测试钉的不是「结果等于多少」，而是**统计构造的正确性**：
需求侧换算、非中心 χ² 检出力、最坏分摊必须 ≤ 等分、以及两个错误口径
（箱形上界、跨池交集）确实被正确识别为错误。
"""
import math

import numpy as np
import pytest

from ev import edge_ceiling as EC


# ---------------------------------------------------------------- 基础换算
def test_sidak_matches_bonferroni_at_k1():
    assert EC.sidak_threshold(1, 0.05, 2) == pytest.approx(1.959963985, abs=1e-6)
    assert EC.sidak_threshold(1, 0.05, 1) == pytest.approx(1.644853627, abs=1e-6)


def test_sidak_is_looser_than_bonferroni_but_tighter_than_nothing():
    K = 33
    t = EC.sidak_threshold(K, 0.05, 2)
    bonf = float(__import__("scipy").stats.norm.ppf(1 - 0.05 / (2 * K)))
    assert EC.sidak_threshold(1, 0.05, 2) < t < bonf


def test_sidak_monotone_in_k():
    ts = [EC.sidak_threshold(K, 0.05, 2) for K in (2, 5, 10, 33, 100)]
    assert all(b > a for a, b in zip(ts, ts[1:]))


# ---------------------------------------------------------------- 备择构造
@pytest.mark.parametrize("K,c", [(10, 1), (33, 6), (35, 5), (16, 1), (12, 2)])
def test_shift_alternative_conserves_mass(K, c):
    """★ top-c 抬到 a、其余压到 b，必须满足 c·a + (K−c)·b = c —— 否则就是
    「凭空多出概率」，那正是箱形上界犯的错。"""
    for delta in (0.01, 0.05, 0.15, 0.5):
        a, b, lam = EC.shift_alternative(K, c, delta)
        assert c * a + (K - c) * b == pytest.approx(c, abs=1e-12)
        assert a > EC.Pool("x", "z", K, c, 1, np.zeros(K, int)).p0
        assert b >= 0.0
        assert lam > 0.0


def test_shift_alternative_zero_delta_is_uniform():
    a, b, lam = EC.shift_alternative(10, 1, 0.0)
    assert a == pytest.approx(0.1)
    assert b == pytest.approx(0.1)
    assert lam == pytest.approx(0.0, abs=1e-15)


# ---------------------------------------------------------------- 检出力
def test_pool_power_at_null_equals_alpha():
    """δ=0 时拒绝率就是名义水平（type I error），这是检出力口径的地基。"""
    for alpha in (0.05, 0.01):
        p = EC.pool_power(3000, 10, 1, 0.0, alpha=alpha)
        assert p == pytest.approx(alpha, rel=1e-9)


def test_pool_power_increases_with_n_and_delta():
    base = EC.pool_power(3000, 10, 1, 0.05, alpha=0.05)
    assert EC.pool_power(3000, 10, 1, 0.10, alpha=0.05) > base
    assert EC.pool_power(6000, 10, 1, 0.05, alpha=0.05) > base
    assert EC.pool_power(3000, 10, 1, 0.5, alpha=0.05) > 0.99


def test_detectable_shift_is_inverse_of_power():
    """δ_min 必须真的把检出力顶到目标值（不然 n_required 全错）。"""
    d = EC.detectable_shift(5000, 10, 1, alpha=0.05, power=0.8)
    assert 0 < d < EC.max_shift(10, 1)
    assert EC.pool_power(5000, 10, 1, d, alpha=0.05) == pytest.approx(0.8, abs=5e-3)


def test_n_required_is_inverse_of_detectable_shift():
    n = EC.n_required(0.1, 10, 1, 0.10, alpha=0.05, power=0.8)
    assert n > 0
    assert EC.pool_power(n, 10, 1, 0.10, alpha=0.05) == pytest.approx(0.8, abs=5e-3)


def test_lottery_pool_alternative_is_more_powerful_than_single_value():
    """★ 乐透池的备择是 c 个值**同时**偏移 ⇒ λ 远大于单值偏移。
    早期版本按「一个值偏移」算检出力，系统性低估。"""
    lam_single = EC.shift_alternative(33, 1, 0.10)[2]
    lam_multi = EC.shift_alternative(33, 6, 0.10)[2]
    assert lam_multi > lam_single


# ---------------------------------------------------------------- 跨池并集
def _pool(K, c, N, seed=0, scale=1.0):
    cnt = np.full(K, int(N * c / K), dtype=np.int64)
    return EC.Pool("t", f"p{K}x{c}", K, c, N, cnt,
                   {"区间": [0, K - 1]}) if scale == 1.0 else None


def test_joint_power_is_union_not_product():
    """★ 跨池必须取并集（任一池显现即可），交集会把 7 因子压到 1e-5。"""
    pools = [_pool(10, 1, 5000), _pool(10, 1, 5000)]
    jp = EC.joint_power(pools, 0.2, alpha=0.05)
    single = EC.pool_power(5000, 10, 1, 0.2, alpha=0.05 / 2)
    assert jp > single                     # 并集严格大于单池
    assert jp <= 1.0


def test_joint_power_monotone():
    pools = [_pool(10, 1, 3000), _pool(10, 1, 3000), _pool(10, 1, 3000)]
    a = EC.joint_power(pools, 0.05, alpha=0.05)
    b = EC.joint_power(pools, 0.15, alpha=0.05)
    assert b > a


def test_worst_distribution_never_beats_equal_split():
    """★ 判决用最坏分摊，所以它必须 ≤ 等分 —— 否则「取最坏」这句话是假的。"""
    pools = [_pool(10, 1, 4000), _pool(15, 1, 900)]
    wd = EC.worst_distribution_power(pools, 2.0, alpha=0.05, n_draw=400)
    assert wd["检出力"] <= wd["等分检出力"] + 1e-12
    assert len(wd["分摊"]) == 2
    assert sum(wd["分摊"]) == pytest.approx(1.0, abs=1e-9)


def test_worst_distribution_finds_a_vertex_for_lopsided_pools():
    """一个池极小样本时，最坏分摊应把大部分增益压给它（那里最难看出）。"""
    pools = [_pool(10, 1, 8000), _pool(15, 1, 500)]
    wd = EC.worst_distribution_power(pools, 2.0, alpha=0.05, n_draw=800)
    assert wd["分摊"][1] > 0.5


# ---------------------------------------------------------------- 上界口径
def test_ceiling_gain_respects_chi2_boundary():
    """★ 上界必须落在 χ² 临界面上：区域外的点要付超额 χ²。"""
    rng = np.random.default_rng(7)
    K, c, N = 10, 1, 4000
    cnt = rng.multinomial(N, np.full(K, 1.0 / K))
    pool = EC.Pool("t", "z", K, c, N, cnt, {"区间": [0, K - 1]})
    cg = pool.ceiling_gain(alpha=0.05)
    assert cg["chi2_at_ceiling"] == pytest.approx(cg["chi2_crit"], rel=2e-3)
    assert cg["gain"] >= cg["gain_point"] - 1e-12
    # 上界时 top-c 取 a、其余取 b，仍须守恒
    assert c * cg["a"] + (K - c) * cg["b"] == pytest.approx(c, abs=1e-9)


def test_box_gain_overstates_ceiling():
    """★ 箱形口径（忽略 Σp=c）必须**高于**约束口径 —— 这是保留它唯一的意义。"""
    rng = np.random.default_rng(11)
    K, c, N = 33, 6, 3518
    # 每期不放回抽 c 个 ⇒ 单球边际 = c/K；用 N·c 次均匀多项模拟计数边际
    cnt = rng.multinomial(N * c, np.full(K, 1.0 / K))
    pool = EC.Pool("t", "red", K, c, N, cnt, {"区间": [1, K]})
    box = pool.box_gain(0.05)[0]
    cei = pool.ceiling_gain(0.05)["gain"]
    assert box > cei


def test_ceiling_gain_matches_point_when_no_deviation():
    """完全均匀的计数 ⇒ 上界只由置信区域撑开，点估计应≈1。"""
    K, c, N = 10, 1, 10000
    cnt = np.full(K, N // K)
    pool = EC.Pool("t", "z", K, c, N, cnt, {"区间": [0, K - 1]})
    cg = pool.ceiling_gain(0.05)
    assert cg["gain_point"] == pytest.approx(1.0, abs=1e-9)
    assert cg["gain"] > 1.0


# ---------------------------------------------------------------- 真实数据
def test_zone_pools_structure_for_ssq():
    pools = EC.zone_pools("双色球")
    assert [p.K for p in pools] == [33, 16]
    assert [p.choose for p in pools] == [6, 1]
    red = pools[0]
    assert red.counts.sum() == red.N * 6
    assert red.N > 3000


def test_zone_pools_qxc_seventh_position_uses_current_segment():
    """七星彩第 7 位只取现行段 ⇒ 期数必须显著少于其它位。"""
    pools = EC.zone_pools("七星彩")
    n7 = next(p.N for p in pools if p.K == 15)
    n1 = next(p.N for p in pools if p.分区 == "第1位")
    assert n7 < n1 / 2


def test_dlt_modern_only_shrinks_and_cleans():
    """大乐透早期段红球边际不均匀（P2-2 变点）⇒ 现代段期数更少且 χ² 不再爆炸。"""
    full = EC.zone_pools("大乐透", modern_only=False)
    modern = EC.zone_pools("大乐透", modern_only=True)
    assert modern[0].N < full[0].N
    red_f = full[0].ceiling_gain(0.05)
    red_m = modern[0].ceiling_gain(0.05)
    assert red_f["chi2_p"] < 1e-3        # 全史确实被早期段污染
    assert red_m["chi2_p"] > 0.05


@pytest.mark.parametrize("lot", EC.LOTS)
def test_analyze_produces_verdict(lot):
    r = EC.analyze_lottery(lot)
    assert r["状态"] == "ok"
    assert r["判决"] in ("可判：可获利偏差被数据排除", "不可判：检出力不足")
    assert 0.0 <= r["最坏分摊检出力"] <= 1.0
    assert r["最坏分摊检出力"] <= r["联合检出力"] + 1e-9
    assert r["G_req"] == pytest.approx(1.0 / r["r0"])


def test_digit_games_are_judged_and_wide_pools_are_not():
    """★ 结论结构：小池 + 高返奖 + 长样本 ⇒ 可判；宽池 + 低返奖 ⇒ 检出力不足。
    这条锁的是「为什么」而不只是「是多少」——如果哪天某彩种反了，得先查口径。"""
    v = {nm: EC.analyze_lottery(nm)["判决"] for nm in EC.LOTS}
    for nm in ("福彩3D", "排列3", "排列5"):
        assert v[nm].startswith("可判"), f"{nm} 应可判，实得 {v[nm]}"
    for nm in ("大乐透", "七星彩"):
        assert v[nm].startswith("不可判"), f"{nm} 应不可判，实得 {v[nm]}"


def test_delta_req_matches_break_even_arithmetic():
    """δ_req 必须真的把 EV 顶到 1.0：r0 · (1+δ_req)^因子数 == 1。"""
    for lot in EC.LOTS:
        r = EC.analyze_lottery(lot)
        g = (1.0 + r["δ_req"]) ** r["因子数"]
        assert r["r0"] * g == pytest.approx(1.0, rel=1e-9)


def test_render_markdown_has_both_tables():
    md = EC.render_markdown(EC.run_all(lots=("排列3", "双色球")))
    assert "最坏分摊检出力" in md
    assert "逐池明细" in md
    assert "排列3" in md and "双色球" in md
