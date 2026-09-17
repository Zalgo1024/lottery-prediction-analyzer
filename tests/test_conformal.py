# -*- coding: utf-8 -*-
"""P3-2 共形预测保证的测试：核心保证有效性（不撒谎）+ 真实数据校准合理性。

理论部分用合成 iid Bernoulli 直接 MC 验证分箱共形（+1 构造）的误保证率上界：
  P(发出声称 X% ∧ 下一期没中) ≤ 1-X%   —— 有限样本、无分布假设。
真实数据部分核对：随机单注校准命中率 ≈ 解析 π；大注数饱和；覆盖设计满覆盖
确定性标注；P2-2 变点联动（大乐透只用现代段）；数字型精确公式；确定性复现。
"""
import random

import pytest

from combo.conformal import (
    _single_ticket_pi,
    _tier_matrix,
    certified_level,
    claim_supported,
    cp_lower,
    minimal_certified_K,
    plan,
)

SSQ_ANALYTIC_ANY = 0.06709   # 双色球随机单注中奖(≥六等) 超几何精算值


# ---------------------------------------------------------------- 理论（合成）

class TestConformalTheory:
    def test_certified_level_never_100(self):
        assert certified_level(10, 10) == pytest.approx(10 / 11)
        assert certified_level(0, 10) == 0.0
        assert certified_level(7, 0) == 0.0
        assert certified_level(10, 10) < 1.0     # 分布自由方法给不出 100%

    def test_claim_supported_threshold(self):
        n = 200
        assert claim_supported(195, n, 0.95)     # 195 ≥ 201*0.95=190.95
        assert not claim_supported(190, n, 0.95)
        assert claim_supported(0, 0, 0.5) is False  # n=0 恒不声称

    def test_claim_validity_mc(self):
        """声称 X=75% 的误保证率 P(声称 ∧ 下一期没中) ≤ 25%（理论 MC 验证）。"""
        rng = random.Random(42)
        n, x, p, reps = 400, 0.75, 0.8, 8000
        bad = fired = 0
        for _ in range(reps):
            k = sum(1 for _ in range(n) if rng.random() < p)
            test = rng.random() < p
            if claim_supported(k, n, x):
                fired += 1
                if not test:
                    bad += 1
        assert fired / reps > 0.5          # p=0.8 > 0.75 → 声称应频繁发出
        assert bad / reps <= 0.25 + 0.015  # 误保证率 ≤ 1-X（MC 噪声 ±0.5%）

    def test_claim_rare_when_p_below_x(self):
        """p=0.7 时 X=80% 的声称几乎不该发出（诚实性：不虚报）。"""
        rng = random.Random(7)
        n, x, p, reps = 300, 0.80, 0.7, 3000
        fired = 0
        for _ in range(reps):
            k = sum(1 for _ in range(n) if rng.random() < p)
            fired += int(claim_supported(k, n, x))
        assert fired / reps < 0.01

    def test_cp_lower_coverage(self):
        """CP95% 单侧下界：P(p_L ≤ p_true) ≥ 0.95（精确二项性质）。"""
        rng = random.Random(1)
        p, n, reps = 0.9, 400, 4000
        cover = 0
        for _ in range(reps):
            k = sum(1 for _ in range(n) if rng.random() < p)
            cover += int(cp_lower(k, n, 0.95) <= p)
        assert cover / reps >= 0.94

    def test_cp_lower_extremes(self):
        assert cp_lower(0, 100, 0.95) == 0.0
        assert cp_lower(100, 100, 0.95) >= 0.95
        assert cp_lower(60, 100, 0.95) < 0.60  # 保守：下界低于点估计

    def test_minimal_K_monotone_structure(self):
        """first_hit 单调结构：minimal K = 命中所需最小票序的第 need 小值。"""
        fh = [3, None, 1, 7, None, 2, 5, None, 1, 4]   # 10 期，有限命中 7 个
        # k(K)=#{first_hit≤K}：K=1→2, 2→3, 3→4, 4→5, 5→6, 7→7（封顶 7）
        assert minimal_certified_K(fh, 10, 0.5) == 5    # need=⌈11·0.5⌉=6
        assert minimal_certified_K(fh, 10, 0.9) is None  # need=⌈11·0.9⌉=10>7
        assert minimal_certified_K([None] * 10, 10, 0.5) is None


# ---------------------------------------------------------------- 真实数据

class TestRealData:
    def test_ssq_single_ticket_matches_analytic(self):
        """双色球 random any K=1：校准命中率 ≈ 解析 π=0.06709（±二项噪声）。"""
        r = plan("双色球", target="any", K_grid=[1], seed=3)
        row = r["对照表"][0]
        assert row["K注数"] == 1
        assert 0.058 <= row["校准命中率"] <= 0.076
        assert 0.045 <= row["回放实测"] <= 0.09
        # 诚实性：单注命中率 ~7% → 任何 ≥50% 档都不该声称
        assert not any(row["声称档位"].values())

    def test_ssq_many_tickets_saturate_and_claims_fire(self):
        """双色球 random any K=300：票池覆盖所有蓝号 → 每期必有 ≥1 票中(六等)；
        声称 99% 档发出且回放达标、门控无 ❌。"""
        r = plan("双色球", target="any", K_grid=[300], seed=1)
        row = r["对照表"][0]
        assert row["校准命中率"] == 1.0
        assert row["回放实测"] == 1.0
        assert row["共形保证档"] >= 0.999
        assert all(row["声称档位"].values())       # 含 99%
        assert all(not v.startswith("❌")
                   for v in r["不撒谎门控"].values())
        assert r["最小达标K"]["99%"] <= 300

    def test_ssq_cov_t2_full_deterministic_floor(self):
        """双色球 cover_t2 满覆盖：任意开奖 ≥1 票红≥2（数学事实，票数随贪心种子变）。
        共形档 G=n/(n+1)（恒<1 是方法天花板），确定性单独标注。"""
        r = plan("双色球", target="red2", mode="cover_t2", seed=0)
        row = r["对照表"][0]
        n_pool = r["票池"]["票数"]
        assert row["K注数"] == n_pool
        assert n_pool >= 36                  # 理论下界 LB=⌈C(33,2)/C(6,2)⌉
        assert row["校准命中率"] == 1.0
        assert row["回放实测"] == 1.0
        assert row["满覆盖确定性"] is True
        assert row["共形保证档"] < 1.0
        assert r["确定性说明"] and "满覆盖" in r["确定性说明"]
        assert all(not v.startswith("❌") for v in r["不撒谎门控"].values())

    def test_ssq_red3_honest_note(self):
        """双色球 red3 是红球地板非奖级——诚实说明必须明示。"""
        r = plan("双色球", target="red3", mode="cover_t3", seed=0)
        assert "不是奖级" in r["诚实说明"]

    def test_dlt_modern_window_changepoint_link(self):
        """大乐透 P2-2 变点联动：2013-08-19(期号13096) 之前的段必须被排除。"""
        r = plan("大乐透", target="any", K_grid=[300], seed=2)
        ms = r["现代段"]
        assert ms["检出"], "大乐透应有 2013 红球变点检出记录"
        assert ms["首期"] > 13096
        assert ms["期数"] >= 1800

    def test_digital_exact_formula(self):
        """数字型：固定赔率 → 精确公式表（共形无校准对象）。"""
        r3 = plan("排列3")
        assert r3["mode"].startswith("exact")
        rows = {row["K注数"]: row["直选命中率(精确)"] for row in r3["对照表"]}
        assert rows[1] == pytest.approx(0.001)
        assert rows[1000] == 1.0
        r5 = plan("排列5")
        rows5 = {row["K注数"]: row["直选命中率(精确)"] for row in r5["对照表"]}
        assert rows5[1] == pytest.approx(1e-5)
        assert rows5[100000] == 1.0

    def test_deterministic_same_seed(self):
        """同种子两次执行 → 完全一致（票池可复现）。"""
        a = plan("双色球", target="any", K_grid=[1, 16, 50], seed=9)
        b = plan("双色球", target="any", K_grid=[1, 16, 50], seed=9)
        ka = [row["校准命中率"] for row in a["对照表"]]
        kb = [row["校准命中率"] for row in b["对照表"]]
        assert ka == kb
        assert a["最小达标K"] == b["最小达标K"]

    def test_monotone_in_K(self):
        """校准命中率随 K 单调不减（票池前 K 张结构的必然性质）。"""
        r = plan("双色球", target="any",
                 K_grid=[1, 2, 5, 10, 20, 50, 100], seed=5)
        rates = [row["校准命中率"] for row in r["对照表"]]
        assert rates == sorted(rates)
