# -*- coding: utf-8 -*-
"""Phase 2 市场假设研究的测试。

覆盖：
  - 自包含统计基础（χ² 上尾、二项精确双侧、Spearman）在已知点上正确；
  - 只读 loader 返回合理数据；
  - 出球顺序检验在有效彩种上不误报、在「整数序排列」彩种上正确排除（七乐彩）；
  - 滚动预测在真实摇出顺序上不宣称可预测（自适应≈理论）。
"""
from __future__ import annotations

from ev import market_backtest as mb
from ev import market_features as mf


# --------------------------------------------------------------- 统计基础

def test_chi2_sf_known():
    # χ²(1) 上尾：3.841 ≈ p=0.05；0 → 1.0；大值 → 趋近 0
    assert abs(mb.chi2_sf(3.841, 1) - 0.05) < 0.01
    assert mb.chi2_sf(0.0, 5) == 1.0
    assert mb.chi2_sf(50.0, 10) < 0.001


def test_binom_two_sided_known():
    # 观测落在众数 → p≈1（不拒绝）
    assert abs(mb.binom_two_sided(5, 10, 0.5) - 1.0) < 1e-6
    # 极端尾 → 很小
    p = mb.binom_two_sided(0, 10, 0.5)
    assert 0.0 < p < 0.01


def test_spearman_perfect():
    x = list(range(10))
    y = list(range(10))
    rho, prho = mb.spearman_rho(x, y)
    assert abs(rho - 1.0) < 1e-9
    assert prho < 0.05
    rho2, _ = mb.spearman_rho(x, list(reversed(y)))
    assert abs(rho2 + 1.0) < 1e-9


# --------------------------------------------------------------- 只读 loader

def test_load_seq_front_ssq_nonempty():
    rows = mf.load_seq_front("ssq", limit=50)
    assert len(rows) > 0
    issue, date, seq = rows[0]
    assert isinstance(seq, list) and len(seq) == 6


def test_load_prize_region_ssq():
    rows = mf.load_prize_region("ssq")
    assert len(rows) > 0
    assert all(isinstance(r[2], int) for r in rows)


def test_load_region_sales_fucai():
    rows = mf.load_region_sales("福彩", limit=200)
    assert len(rows) > 0
    ym, region, sales, _yoy, _cum = rows[0]
    assert len(ym) == 6 and ym.isdigit()  # 已规范为 YYYYMM


def test_province_sales_totals():
    tot = mf.province_sales_totals("福彩")
    assert "广东" in tot and tot["广东"] > 0


# --------------------------------------------------------------- 数据质量闸门

def test_is_draw_order_gate():
    # 双色球为真实摇出顺序；七乐彩 seq_front 是整数序排列
    assert mb._is_draw_order("ssq") is True
    assert mb._is_draw_order("qlc") is False


# --------------------------------------------------------------- 检验不误报

def test_uniformity_sd_no_false_deviation():
    u = mb.test_seq_front_uniformity("sd", limit=800)
    assert u["可用期数"] > 0
    assert "与均匀兼容" in u["结论"]  # Bonferroni 后不误报


def test_serial_sd_no_usable_dependence():
    s = mb.test_seq_front_serial("sd", limit=800)
    assert "无可用一阶依赖" in s["结论"]


def test_walk_forward_sd_no_signal():
    w = mb.walk_forward_order_predict("sd", window=150, limit=800)
    # 自适应命中率应≈理论 1/range=0.1，且不显著超过理论
    assert abs(w["自适应总命中率"] - w["理论命中率(均值)"]) < 0.02
    assert w["自适应_vs_理论_p"] >= 0.05


def test_qlc_order_tests_excluded():
    for fn in (mb.test_seq_front_uniformity, mb.test_seq_front_serial,
               mb.walk_forward_order_predict):
        r = fn("qlc", limit=500)
        assert "非真实摇出顺序" in r["结论"]


def test_province_jackpot_vs_sales_ssq_correlated():
    p = mb.province_jackpot_vs_sales("ssq")
    # 头奖注数与全省总销量强正相关（卖得多中得多），与公平一致
    assert p["Spearman_rho"] is not None and p["Spearman_rho"] > 0.5


def test_run_lottery_runs():
    r = mb.run_lottery("sd", window=150, limit=600)
    assert r["代码"] == "sd"
    assert "均匀性" in r and "滚动预测" in r and "分省头奖vs销量" in r
