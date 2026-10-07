# -*- coding: utf-8 -*-
"""流行度分位口径测试（P0 / 地基变更）。

锁定两条此前会**静默**出错的契约：

  ① 打分必须**逐特征累加**（`_score_std`），不能用 `Fz @ beta`。
     全组合分数存在大量完全并列（双色球 1,107,568 组合 / 214,817 个不同分数，
     最大并列组 149）。BLAS 的 gemv 在**不同行数**下累加顺序不同 →
     同一组合「批量打分」与「单行打分」差 1 ULP；落进并列组边界时分位可跳
     最多 149/M ≈ 1.3e-4，且方向由舍入决定（不是零均值噪声）。
     2026-10-03 实测：期3002/3003/3004 三条不一致（差 1.5e-5）。

  ② 分位必须用**中点秩** (count(<v) + count(≤v)) / (2M)，不是 count(≤v)/M。
     对并列数据：count(≤)/M 的期望 = 1 − P/M² ≥ 0.5（**系统性正偏**），
     且偏的方向恰好**把「缝隙」量小**；中点秩的期望恒为 0.5
     （E[u] = [P + ½Σg²]/M² = ½，由 2P + Σg² = M² 得）。
"""
import numpy as np
import pytest

import ev.popularity as pop

_M = 100          # 合成的「全组合」总数
# 并列结构：三组完全并列，组大小 30/40/30，值 0/1/2
_GROUP_VALS = np.array([0.0] * 30 + [1.0] * 40 + [2.0] * 30)
_GROUP_IDS = np.array([0] * 30 + [1] * 40 + [2] * 30)


# ---------------------------------------------------------------- 打分一致性

def test_score_std_is_rowcount_independent():
    """① 逐特征累加 ⇒ 批量与单行的结果**位级相同**（不是「接近」）。"""
    rng = np.random.default_rng(20261003)
    F = rng.normal(size=(500, 9))
    m_ = rng.normal(size=9)
    s_ = rng.uniform(0.5, 2.0, size=9)
    beta = rng.normal(size=9)

    full = pop._score_std(F, m_, s_, beta)
    assert full.shape == (500,)
    bad = [i for i in range(500)
           if pop._score_std(F[i:i + 1], m_, s_, beta)[0] != full[i]]
    assert not bad, f"{len(bad)} 行批量/单行不位级相同，前几行 {bad[:5]}"


def test_matmul_is_not_rowcount_stable():
    """① 立论：矩阵乘**确实会**按行数分叉 —— 所以必须用 _score_std。

    构造 = 元素量级悬殊（10^-3 ~ 10^7），让累加顺序影响最后几位。
    gemv（1 行）与 gemm（500 行）走不同累加顺序，实测行 37/111/148/185/259/296
    位级不同（差 ~1e-9）。这不是理论担忧，是本机实跑出来的。
    """
    rng = np.random.default_rng(20261003)
    F = rng.normal(size=(500, 9)) * (10.0 ** rng.integers(-3, 8, size=9))
    beta = rng.normal(size=9) * (10.0 ** rng.integers(-3, 8, size=9))
    m_ = np.zeros(9)
    s_ = np.ones(9)

    batch = F @ beta
    forked = [i for i in range(500) if (F[i:i + 1] @ beta)[0] != batch[i]]
    assert forked, "构造失效：矩阵乘两边位级相同，本测试失去意义（需换构造）"

    # 同一份数据下 _score_std 必须处处位级一致
    got = pop._score_std(F, m_, s_, beta)
    assert all(pop._score_std(F[i:i + 1], m_, s_, beta)[0] == got[i] for i in forked)


def test_score_std_same_formula_as_matmul():
    """① 副证：与矩阵乘是同一个公式（只是累加顺序不同），差 ≤ 1e-12。"""
    rng = np.random.default_rng(7)
    F = rng.normal(size=(200, 9))
    m_ = rng.normal(size=9)
    s_ = rng.uniform(0.5, 2.0, size=9)
    beta = rng.normal(size=9)
    ref = ((F - m_) / s_) @ beta
    got = pop._score_std(F, m_, s_, beta)
    assert np.max(np.abs(ref - got)) < 1e-12


def test_score_formula_ver_enters_fingerprint(monkeypatch):
    """① 打分公式版本必须进指纹——否则磁盘 npz 旧分数会静默命中。"""
    b = np.arange(9.0)
    m_ = np.zeros(9)
    s_ = np.ones(9)
    monkeypatch.setattr(pop, "_SCORE_FORMULA_VER", "va")
    fa = pop._scores_fingerprint(b, m_, s_)
    monkeypatch.setattr(pop, "_SCORE_FORMULA_VER", "vb")
    fb = pop._scores_fingerprint(b, m_, s_)
    assert fa != fb


def test_fingerprint_moves_with_beta():
    m_ = np.zeros(9)
    s_ = np.ones(9)
    f1 = pop._scores_fingerprint(np.zeros(9), m_, s_)
    f2 = pop._scores_fingerprint(np.ones(9), m_, s_)
    assert f1 != f2


# ---------------------------------------------------------------- 中点秩无偏性

def _patch_synth(monkeypatch, vals_sorted):
    """把打分链短路：own 直接等于传入值，scores_sorted 直接等于合成全组合分数。"""
    m_ = np.zeros(9)
    s_ = np.ones(9)
    beta = np.array([1.0] + [0.0] * 8)     # 只取第 0 个特征 ⇒ _score_std 恒等于该列

    def _feat(R):
        F = np.zeros((len(R), 9))
        # 组合 → 其分数：按行号映射回合成值，保证「开出的组合」与该组值一致。
        # 用切片而不是整体赋值，单行/批量两种调用下同一行必须得到同一个值
        # （否则「薄封装一致性」那条测试就成了自证）。
        F[:, 0] = vals_sorted[:len(R)]
        return F

    monkeypatch.setattr(pop, "features_matrix", _feat)
    monkeypatch.setattr(pop, "_get_scores_cached",
                        lambda *a, **k: (np.sort(vals_sorted), [], []))
    return beta, m_, s_


def test_midrank_expected_half_under_ties(monkeypatch):
    """② 并列数据下中点秩的期望**恰好** 0.5（公平开奖无偏）。"""
    beta, m_, s_ = _patch_synth(monkeypatch, _GROUP_VALS)
    R = np.zeros((_M, 9), dtype=np.int16)
    u = pop.drawn_quantiles("双色球", R, beta=beta, mu_=m_, sd_=s_, persist=False)

    # 闭式期望：E[u] = (P + ½Σg²) / M²，其中 P = 严格小于的有序对数
    g = np.bincount(_GROUP_IDS, minlength=3)
    P = int(sum(g[i] * g[j] for i in range(3) for j in range(i + 1, 3)))
    closed = (P + 0.5 * float((g ** 2).sum())) / _M ** 2
    assert closed == pytest.approx(0.5), "该合成结构应满足 2P + Σg² = M²"
    assert 2 * P + int((g ** 2).sum()) == _M ** 2

    # 中点秩期望恒为 0.5；偏差只剩浮点累加误差（~1e-16），
    # 与下面 count(≤) 那 0.17 的系统性正偏不在一个量级。
    assert float(u.mean()) == pytest.approx(0.5, abs=1e-12)
    # 逐组核对中点秩取值
    exp_per_group = [(0, 30, 0.15), (30, 70, 0.5), (70, 100, 0.85)]  # (lt, le, u)
    for gi, (lt, le, want) in enumerate(exp_per_group):
        assert float(u[_GROUP_IDS == gi][0]) == pytest.approx(want)
    assert len(np.unique(u)) == 3


def test_count_le_is_biased_upward_under_ties(monkeypatch):
    """② 反证：count(≤)/M 在同一数据上系统性正偏（方向=把缝隙量小）。"""
    beta, m_, s_ = _patch_synth(monkeypatch, _GROUP_VALS)
    R = np.zeros((_M, 9), dtype=np.int16)
    u = pop.drawn_quantiles("双色球", R, beta=beta, mu_=m_, sd_=s_, persist=False)

    ss = np.sort(_GROUP_VALS)
    lt = np.searchsorted(ss, ss, side="left")      # 这里按「每个组合自身」算
    le = np.searchsorted(ss, ss, side="right")
    eager = (le / _M)
    assert float(eager.mean()) > float(u.mean())
    assert float(eager.mean()) - float(u.mean()) == pytest.approx(
        (0.5 * float((np.bincount(_GROUP_IDS, minlength=3) ** 2).sum())) / _M ** 2)
    # 极端并列（全等）时偏差最大：mid-rank ≡ 0.5，count(≤)/M ≡ 1.0
    assert lt.min() == 0 and le.max() == _M


def test_midrank_equals_count_le_when_no_ties(monkeypatch):
    """② 边界：全不并列时中点秩退化为标准分位 (rank+1)/(M+1) 的邻域，期望仍 0.5。"""
    vals = np.arange(1.0, _M + 1.0)                # 全不并列，值 1..100
    beta, m_, s_ = _patch_synth(monkeypatch, vals)
    R = np.zeros((_M, 9), dtype=np.int16)
    u = pop.drawn_quantiles("双色球", R, beta=beta, mu_=m_, sd_=s_, persist=False)
    # 值 v=k 时 (lt+le)/2M = (k-1+k)/200
    want = (2.0 * vals - 1.0) / (2.0 * _M)
    assert np.allclose(u, want)
    assert float(u.mean()) == pytest.approx(0.5, abs=1e-12)


# ---------------------------------------------------------------- 入口一致性

def test_popularity_quantile_delegates_to_drawn_quantiles(monkeypatch):
    """单注入口必须是批量入口的薄封装（此前两条路径各算一遍 own ⇒ 1 ULP 分叉）。"""
    beta, m_, s_ = _patch_synth(monkeypatch, _GROUP_VALS)
    R = np.zeros((_M, 9), dtype=np.int16)
    batch = pop.drawn_quantiles("双色球", R, beta=beta, mu_=m_, sd_=s_, persist=False)
    single = pop.popularity_quantile("双色球", [1, 2, 3, 4, 5, 6],
                                     beta=beta, mu_=m_, sd_=s_)
    assert single == float(batch[0])


def test_drawn_quantiles_nan_when_model_nogo(monkeypatch):
    """β 缺失且模型未 Go ⇒ 全 NaN（调用方必须显式降级，不许静默给数）。"""
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": False, "report": {}})
    u = pop.drawn_quantiles("双色球", np.zeros((3, 9), dtype=np.int16))
    assert u.shape == (3,)
    assert np.isnan(u).all()


# ---------------------------------------------------------------- 描述性报告口径

def test_actual_draw_report_is_flagged_in_sample(monkeypatch):
    """描述性报告必须自标「非独立证据」——否则会被当成裁决依据。"""
    rows = {"reds": np.zeros((40, 9), dtype=np.int16)}
    monkeypatch.setattr(pop, "_load_draw_rows", lambda lot: rows)
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": True, "beta": np.array([1.0] + [0.0] * 8),
                                     "m_": np.zeros(9), "s_": np.ones(9), "report": {}})
    monkeypatch.setattr(pop, "drawn_quantiles",
                        lambda lot, R, **k: np.linspace(0.0, 1.0, len(R)))
    rep = pop.actual_draw_quantile_report("双色球")
    assert rep["in_sample"] is True and rep["独立证据"] is False
    assert "walk-forward" in rep["结论"]
    assert rep["mean_u"] == pytest.approx(0.5, abs=1e-6)
