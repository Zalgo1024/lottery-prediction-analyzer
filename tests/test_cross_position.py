# -*- coding: utf-8 -*-
"""credibility/cross_position.py 的口径锁。

重点钉两件事：
① 同号率的零假设必须用**实测边际**（Σ p̂x·p̂y），不能被边际不均匀骗成「依赖」；
② 合成数据里注入依赖时必须被检出 —— 否则「未检出」是检验本身失灵，不是结论。
"""
import numpy as np
import pytest

from credibility import cross_position as CP


class _Rec:
    """最小开奖记录替身（digit_matrix 只读 zone_numbers / 期号 / 开奖日期）。"""

    def __init__(self, idx, digits, date=None):
        self.期号 = idx
        self.zone_numbers = {f"第{i + 1}位": [d] for i, d in enumerate(digits)}
        self.开奖日期 = date


def _matrix(recs, n_pos=3):
    from data.schema import Zone, get_schema
    # 用真实 schema 取位名/K，记录用替身
    schema = get_schema("排列3")
    los = [int(z.min) for z in schema.zones]
    Ks = [int(z.max) - int(z.min) + 1 for z in schema.zones]
    mat = np.array([[d - lo for d, lo in zip(r.zone_numbers[f"第{i+1}位"], los)]
                    for r in recs for i in range(min(n_pos, len(los)))],
                   dtype=int).reshape(len(recs), min(n_pos, len(los)))
    return mat, Ks[:n_pos]


# ---------------------------------------------------------------- 同号率
def test_same_rate_null_for_independent_digits():
    rng = np.random.default_rng(3)
    n = 20000
    x = rng.integers(0, 10, n)
    y = rng.integers(0, 10, n)
    r = CP.same_rate_test(x, y, 10, 10)
    assert abs(r["z"]) < 3.0
    assert r["过效应门"] is False
    assert r["零假设同号率"] == pytest.approx(0.1, abs=5e-3)


def test_same_rate_detects_injected_dependence():
    """★ 注入「同号率 ×1.6」的依赖必须被 |z| ≥ 3 抓到。"""
    rng = np.random.default_rng(5)
    n = 12000
    x = rng.integers(0, 10, n)
    same = rng.random(n) < 0.16
    y = np.where(same, x, rng.integers(0, 10, n))
    r = CP.same_rate_test(x, y, 10, 10)
    assert r["z"] > 5.0
    assert r["过效应门"] is True
    assert r["相对偏差"] > 0.3


def test_same_rate_null_uses_observed_marginals_not_one_over_k():
    """★ 地基：边际严重不均匀但位间独立时，z 必须≈0。

    若拿 1/K 当零假设，这里会冒出巨大的假「依赖」（正是七星彩第 7 位
    0-9→0-14 那个口径伪影的同一类错误）。
    """
    rng = np.random.default_rng(9)
    n = 30000
    # 两位都只取 {0,1} 各半 ⇒ 真实同号率 = 0.5，而 1/K = 0.1
    x = rng.integers(0, 2, n)
    y = rng.integers(0, 2, n)
    r = CP.same_rate_test(x, y, 10, 10)
    assert abs(r["z"]) < 3.0, f"边际被误判成依赖：z={r['z']}"
    assert r["零假设同号率"] == pytest.approx(0.5, abs=1e-2)
    assert r["同号率"] == pytest.approx(0.5, abs=1e-2)


def test_same_rate_handles_empty():
    r = CP.same_rate_test(np.array([], dtype=int), np.array([], dtype=int), 10, 10)
    assert r["状态"] == "无数据"


# ---------------------------------------------------------------- 独立性 χ²
def test_pair_independence_passes_on_independent_data():
    rng = np.random.default_rng(13)
    n = 20000
    x = rng.integers(0, 10, n)
    y = rng.integers(0, 10, n)
    r = CP.pair_independence(x, y, 10, 10)
    assert r["p"] > 0.05
    assert r["过效应门"] is False


def test_pair_independence_detects_injected_dependence():
    rng = np.random.default_rng(17)
    n = 15000
    x = rng.integers(0, 10, n)
    same = rng.random(n) < 0.22
    y = np.where(same, x, rng.integers(0, 10, n))
    r = CP.pair_independence(x, y, 10, 10)
    assert r["p"] < 1e-6
    assert r["Cramér_V"] > 0.05


def test_pair_independence_drops_empty_categories():
    """某取值从未出现时不能把 df 虚高（会让 p 偏大、掩盖依赖）。"""
    x = np.array([0, 1] * 500)
    y = np.array([0, 1] * 500)
    r = CP.pair_independence(x, y, 10, 10)
    assert r["df"] == 1


# ---------------------------------------------------------------- 对角增益
def test_diagonal_gain_is_about_one_under_independence():
    rng = np.random.default_rng(19)
    n = 40000
    x = rng.integers(0, 10, n)
    y = rng.integers(0, 10, n)
    r = CP.diagonal_gain(x, y, 10, 10)
    assert r["最大实测增益"] < 1.3
    assert r["最大增益上界"] < 1.6


def test_diagonal_gain_rises_when_diagonal_is_inflated():
    rng = np.random.default_rng(23)
    n = 40000
    x = rng.integers(0, 10, n)
    same = rng.random(n) < 0.25
    y = np.where(same, x, rng.integers(0, 10, n))
    r = CP.diagonal_gain(x, y, 10, 10)
    assert r["最大实测增益"] > 1.5
    assert r["最大增益上界"] > r["最大实测增益"] - 1e-12


# ---------------------------------------------------------------- 真实数据
def test_digit_matrix_for_pl3():
    mat, names, Ks, n = CP.digit_matrix("排列3")
    assert mat.shape == (n, 3)
    assert Ks == [10, 10, 10]
    assert n > 7000
    assert mat.min() >= 0 and mat.max() <= 9


def test_digit_matrix_qxc_truncates_to_current_segment():
    mat, names, Ks, n = CP.digit_matrix("七星彩")
    assert Ks == [10] * 6 + [15]
    assert n < 1500                           # 现行段期数远少于全史 3405


@pytest.mark.parametrize("lot", CP.DIGIT_LOTS)
def test_analyze_runs_and_reports(lot):
    r = CP.analyze(lot)
    assert r["状态"] == "ok"
    assert r["位对"] == len(r["明细"])
    assert isinstance(r["实质检出"], list)
    assert r["判决"].startswith(("未检出", "⚠ 检出"))


def test_no_cross_position_dependence_detected_in_real_data():
    """★ 结论锁：四条数字型彩种的已测位对全部未过效应量门。

    若哪天这里失败，先查是不是口径变了（七星彩分段 / BH 范围 / 效应量门），
    再谈「发现」——与项目其余裁判同一纪律。
    """
    for lot in CP.DIGIT_LOTS:
        r = CP.analyze(lot)
        assert r["实质检出"] == [], f"{lot} 出现实质检出：{r['实质检出']}"
        assert r["最大|z|"] < 4.0


def test_render_markdown_lists_all_lots():
    md = CP.render_markdown(CP.run_all(lots=("排列3", "七星彩")))
    assert "排列3" in md and "七星彩" in md
    assert "效应量门" in md
