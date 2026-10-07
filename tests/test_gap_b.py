# -*- coding: utf-8 -*-
"""B 轨（开奖号冷门度分位）测试。

锁定的契约：
  ① **不循环论证** —— 评估第 t 期时，拟合只用了 `< t` 的数据，且每期恰好评估一次；
  ② 分位检验：ū 偏冷能检出、偏热能检出方向但**不判缝隙**（单侧）、均匀不误报；
  ③ γ 的标尺：k_A/k_B 必须为正，且「ū<0.5 ⇒ γ>0」符号一致；
  ④ 模型未 Go → γ = None（显式降级，不拿 1 顶替）；
  ⑤ γ 不显著时 mechanism_gamma 必须返回 0.0（γ=0 红线）；
  ⑥ **标准误口径**：判定与 γ 一律用长块 bootstrap se，解析 se 只作参照；
  ⑦ **γ=0 红线的口径稳健性**：换 se 口径不得让红线误触发，也不得漏掉真信号。
"""
import math
from datetime import date, timedelta

import numpy as np
import pytest

import ev.popularity as pop
from ev.mechanism import gap_cache
from ev.mechanism.common import DrawFrame, prize_probs
from ev.mechanism.gap_b_drawn_quantile import (
    fit_gamma, gamma_scale, quantile_test, walk_forward_quantiles,
)


def _wf_frame(n=400, seed=5):
    rng = np.random.default_rng(seed)
    k = 6
    reds = np.array([np.sort(rng.choice(np.arange(1, 34), k, replace=False))
                     for _ in range(n)], dtype=np.int16)
    base = date(2010, 1, 1)
    return DrawFrame(
        lottery="双色球",
        issues=[str(2010000 + i) for i in range(n)],
        dates=[base + timedelta(days=3 * i) for i in range(n)],
        reds=reds, prev=np.roll(reds, 1, axis=0),
        y1=rng.poisson(8.0, size=n).astype(float),
        y2=rng.poisson(120.0, size=n).astype(float),
        sales=np.full(n, 2.9e8), dropped=0)


# ------------------------------------------------------ ① 不循环论证

def test_walk_forward_never_uses_future_and_scores_each_period_once(monkeypatch):
    """① 核心：拟合窗口严格早于评估窗口；每期恰好被评估一次。"""
    fr = _wf_frame(400)
    min_train, step = 200, 50
    fit_sizes, drawn_batches = [], []

    def fake_fit(reds, prev, y, N, fit_null=True):
        fit_sizes.append((len(reds), np.array(reds, copy=True)))
        d = len(reds)
        return {"beta": np.zeros(9), "m_": np.zeros(9), "s_": np.ones(9),
                "n": d, "_rows": d}

    def fake_drawn(lot, R, beta=None, mu_=None, sd_=None, persist=True):
        R = np.asarray(R)
        drawn_batches.append(np.array(R, copy=True))
        return np.full(len(R), 0.45)

    monkeypatch.setattr(pop, "fit_beta", fake_fit)
    monkeypatch.setattr(pop, "drawn_quantiles", fake_drawn)
    monkeypatch.setattr(pop, "get_popularity_model", lambda lot: {"go": True})

    wf = walk_forward_quantiles("双色球", frame=fr, min_train=min_train, step=step)

    assert wf["walk_forward"] is True and wf["n_leak"] == 0
    # 拟合窗口长度必须恰好是各切点 c
    assert [s for s, _ in fit_sizes] == list(range(min_train, 400, step))
    # 拟合窗口的最后一行下标 = c-1 < c（评估窗口起点）——用真实红球比对
    for (sz, rows), c in zip(fit_sizes, range(min_train, 400, step)):
        assert sz == c
        assert np.array_equal(rows, fr.reds[:c]), "拟合窗口不是 [:c]"
        assert not np.array_equal(rows, fr.reds[:c + 1]), "拟合窗口多吃了 1 期"
    # 评估窗口拼起来必须正好是 [min_train:]，一期不多一期不少
    joined = np.vstack(drawn_batches)
    assert joined.shape[0] == 400 - min_train
    assert np.array_equal(joined, fr.reds[min_train:]), "评估期集合不等于 [min_train:]"
    assert wf["n"] == 400 - min_train
    assert int(wf["idx"].min()) >= min_train


def test_walk_forward_skips_when_too_short():
    """① 样本 ≤ min_train → 显式不适用，不许输出空结论。"""
    wf = walk_forward_quantiles("双色球", frame=_wf_frame(100), min_train=1200)
    assert wf["applicable"] is False
    assert "min_train" in wf["note"]


def test_walk_forward_not_applicable_for_non_lotto():
    wf = walk_forward_quantiles("福彩3D")
    assert wf["applicable"] is False
    assert wf["walk_forward"] is True and wf["n_leak"] == 0


# ------------------------------------------------------ ② 分位检验

def _mk_wf(mean_u, n=2000, seed=1, sd=None):
    rng = np.random.default_rng(seed)
    # 用 Beta 构造均值可控、支撑在 (0,1) 的样本
    if mean_u == 0.5:
        u = rng.uniform(size=n)
    else:
        k = np.sqrt(mean_u * (1 - mean_u)) * 4
        a, b = mean_u * k, (1 - mean_u) * k
        u = rng.beta(a, b, size=n)
    return {"applicable": True, "walk_forward": True, "n_leak": 0,
            "n": n, "u": u, "模型Go": True, "note": ""}


def _mk_wf_blocked(mean_u, n=2000, seed=1, step=50, sd_block=0.10):
    """构造**块相关**的 u 序列：同一 cut（长度 `step`）内共享一个随机偏移。

    这是 walk-forward 的真实生成机制（β 每 `step` 期重拟合一次 ⇒ 同 cut 内
    共享同一 β ⇒ u_t 块内相关）。**不能用 i.i.d. 序列代替**做长/短块对比：
    实测 i.i.d. 下长块 se 更窄的次数约 4/10（方向随机），只有真块内相关
    才让长块严格更宽（实测 10/10）。
    """
    rng = np.random.default_rng(seed)
    n_blk = int(np.ceil(n / step))
    offs = rng.normal(0.0, sd_block, size=n_blk)
    u = rng.uniform(size=n) + np.repeat(offs, step)[:n]
    u = u + (mean_u - float(u.mean()))          # 平移到目标均值，保留块内结构
    return {"applicable": True, "walk_forward": True, "n_leak": 0, "step": step,
            "n": n, "u": np.clip(u, 1e-6, 1 - 1e-6), "模型Go": True, "note": ""}


def test_uniform_quantiles_do_not_fire():
    """② 公平开奖（u ~ U(0,1)）不得误报。"""
    qt = quantile_test(_mk_wf(0.5), B=400)
    assert qt["mean_u"] == pytest.approx(0.5, abs=0.02)
    assert qt["gap"] is False, qt["note"]


def test_cold_shifted_quantiles_are_detected():
    """② ū 显著偏冷（0.45，n=2000）→ 检出。"""
    qt = quantile_test(_mk_wf(0.45), B=400)
    assert qt["mean_u"] == pytest.approx(0.45, abs=0.02)
    assert qt["gap"] is True, qt["note"]
    assert qt["ci95"][1] < 0.5
    assert qt["z"] < -2.58


def test_hot_shift_is_not_a_gap():
    """② 单侧：ū 偏热（0.55）不是"偏冷"，不得判缝隙。"""
    qt = quantile_test(_mk_wf(0.55), B=400)
    assert qt["mean_u"] == pytest.approx(0.55, abs=0.02)
    assert qt["gap"] is False
    assert qt["ci95"][0] > 0.5


def test_small_effect_blocked_even_if_significant():
    """② 效应量门控：ū=0.498 在大样本下 z 显著，但幅度不足 → 不判。"""
    qt = quantile_test(_mk_wf(0.498, n=20000), B=300)
    assert qt["gap"] is False
    assert ("最小效应量" in qt["note"]) or ("CI" in qt["note"])


def test_ks_and_walk_forward_fields_surface():
    """② KS 与 walk-forward 自检字段必须透传到检验结果里。"""
    qt = quantile_test(_mk_wf(0.45), B=300)
    assert qt["walk_forward"] is True and qt["n_leak"] == 0
    assert qt["ks_p"] is not None and qt["ks_p"] < 0.05


# ------------------------------------------------------ ③④ γ 标尺

def _patch_scores(monkeypatch, scores):
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": True, "beta": np.zeros(9),
                                     "m_": np.zeros(9), "s_": np.ones(9)})
    monkeypatch.setattr(pop, "_get_scores_cached",
                        lambda *a, **k: (np.sort(np.asarray(scores, float)), [], []))


def test_gamma_scale_positive_and_matches_definition(monkeypatch):
    """③ k_A = Cov(e^w, z)、k_B = Cov(u, z) 必须为正，且与手算一致。"""
    rng = np.random.default_rng(3)
    z = rng.normal(size=20000)
    _patch_scores(monkeypatch, z)
    sc = gamma_scale("双色球")
    assert sc is not None and sc["k_A"] > 0 and sc["k_B"] > 0
    zs = np.sort(z)
    ew = np.exp(zs - np.log(np.exp(zs).mean()))
    _, inv, counts = np.unique(zs, return_inverse=True, return_counts=True)
    lt = np.concatenate([[0], np.cumsum(counts)[:-1]])[inv]
    u = (lt + lt + counts[inv]) / (2.0 * len(zs))
    assert sc["k_A"] == pytest.approx(float(np.cov(ew, zs, ddof=0)[0, 1]), rel=1e-9)
    assert sc["k_B"] == pytest.approx(float(np.cov(u, zs, ddof=0)[0, 1]), rel=1e-9)


def test_gamma_scale_none_when_model_not_go(monkeypatch):
    """④ 模型未 Go → 没有 z 标尺 → 必须返回 None，不许拿 1 顶替。"""
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": False, "report": {}})
    assert gamma_scale("大乐透") is None


def test_fit_gamma_sign_convention(monkeypatch):
    """③ 约定：ū<0.5 ⇒ γ_B>0；R<1 ⇒ γ_A>0；两面一致时合并显著。"""
    monkeypatch.setattr(
        "ev.mechanism.gap_b_drawn_quantile.gamma_scale",
        lambda lot, frame=None: {"k_A": 0.03, "k_B": 0.30, "M": 1000, "cbar": 1.0,
                                 "sd_w": 0.17})
    qtest = {"applicable": True, "mean_u": 0.45, "se": 0.01, "n": 2000}
    a = {"applicable": True, "主口径": {"R": 0.95, "se_块bootstrap": 0.01}}
    g = fit_gamma("双色球", wf={"applicable": True}, qtest=qtest, a_result=a)
    assert g["gamma_B"]["gamma"] > 0 and g["gamma_A"]["gamma"] > 0
    m = g["gamma_合并"]
    assert m["gamma"] > 0 and m["显著"] is True
    assert m["ci95"][0] > 0
    assert "冷门偏置" in g["结论"]


def test_fit_gamma_hot_side_is_negative(monkeypatch):
    """③ 反号：ū>0.5（偏热）⇒ γ_B<0，合并后不显著。"""
    monkeypatch.setattr(
        "ev.mechanism.gap_b_drawn_quantile.gamma_scale",
        lambda lot, frame=None: {"k_A": 0.03, "k_B": 0.30, "M": 1000, "cbar": 1.0,
                                 "sd_w": 0.17})
    qtest = {"applicable": True, "mean_u": 0.55, "se": 0.01, "n": 2000}
    a = {"applicable": True, "主口径": {"R": 1.02, "se_块bootstrap": 0.01}}
    g = fit_gamma("双色球", wf={"applicable": True}, qtest=qtest, a_result=a)
    assert g["gamma_B"]["gamma"] < 0 and g["gamma_A"]["gamma"] < 0
    assert g["gamma_合并"]["显著"] is False
    assert "不主张" in g["结论"]


def test_fit_gamma_is_none_without_scale(monkeypatch):
    """④ 无 z 标尺 → γ 不可折算，但必须说清原因。"""
    monkeypatch.setattr("ev.mechanism.gap_b_drawn_quantile.gamma_scale",
                        lambda lot, frame=None: None)
    g = fit_gamma("大乐透", wf={"applicable": True},
                  qtest={"applicable": True, "mean_u": 0.49, "se": 0.01, "n": 100})
    assert g["gamma_合并"] is None
    assert "未 Go" in g["note"]


# ------------------------------------------------------ ⑤ γ=0 红线

def test_mechanism_gamma_zero_when_not_significant(monkeypatch):
    """⑤ γ 不显著 / 取不到 → 必须返回 0.0（等价"不做任何倾斜"）。"""
    from ev import gap
    monkeypatch.setattr(gap, "measure",
                        lambda lot, **k: {"gamma": {"gamma_合并": {
                            "gamma": 0.2, "se": 0.3, "显著": False}}})
    assert gap.mechanism_gamma("双色球") == 0.0


def test_mechanism_gamma_positive_when_significant(monkeypatch):
    from ev import gap
    monkeypatch.setattr(gap, "measure",
                        lambda lot, **k: {"gamma": {"gamma_合并": {
                            "gamma": 0.2, "se": 0.03, "显著": True}}})
    assert gap.mechanism_gamma("双色球") == pytest.approx(0.2)


def test_mechanism_gamma_zero_on_exception(monkeypatch):
    """⑤ 任何异常都不得把非 0 的 γ 漏给下游。"""
    from ev import gap

    def boom(lot, **k):
        raise RuntimeError("x")
    monkeypatch.setattr(gap, "measure", boom)
    assert gap.mechanism_gamma("双色球") == 0.0


# ------------------------------------------------------ 缓存

def test_gap_cache_roundtrip_and_sig_guard(monkeypatch, tmp_path):
    """缓存必须带数据指纹；指纹变了就不许命中（防数据更新后吃旧结果）。"""
    monkeypatch.setattr(gap_cache, "CACHE_DIR", tmp_path / "mechanism")
    assert gap_cache.load("双色球", "s1") is None
    gap_cache.save("双色球", {"x": 1}, "s1")
    assert gap_cache.load("双色球", "s1")["x"] == 1
    assert gap_cache.load("双色球", "s2") is None, "指纹不匹配却命中了旧缓存"
    assert gap_cache.load("双色球")["x"] == 1, "不带指纹时应直接读"
    assert gap_cache.clear("双色球") == 1
    assert gap_cache.load("双色球", "s1") is None


# ------------------------------------------------------ ⑥ 标准误口径（长块）
#
# 这一节锁的是本轮修掉的核心病：B 轨曾用 U(0,1) 解析 se 判 z，而解析 se 假定
# u 序列 i.i.d. —— 可 u 每 step 期共享同一 β，本就有块内相关 ⇒ se 假窄。
# 与 `ev/gap.py` 报告口径「标准误一律用经验离散度 + 长块 bootstrap」对齐。

def test_long_block_se_is_used_for_verdict_not_analytic():
    """⑥ 判定吃的必须是长块 se，解析 se 不得进判定。"""
    qt = quantile_test(_mk_wf(0.5), B=400)
    assert qt["se"] == qt["se_长块bootstrap"], "se 字段已不再是长块 se"
    assert "长块" in qt["判定口径"]
    assert "解析" not in qt["z_零分布"], qt["z_零分布"]
    assert qt["长块长度"] > qt["短块长度"]


def test_analytic_se_is_kept_but_labelled_reference_only():
    """⑥ 解析口径保留作参照（读者要能看到它），但必须写死「不参与判定」。"""
    n = 2000
    qt = quantile_test(_mk_wf(0.5, n=n), B=400)
    # se 字段一律四舍五入到 6 位小数 ⇒ 容差按舍入量级给
    assert qt["se_解析"] == pytest.approx(math.sqrt(1.0 / 12.0 / n), abs=1e-6)
    assert "不参与判定" in qt["解析se_说明"]
    assert qt["ci95_解析"] != qt["ci95"], "解析 CI 与判定 CI 不应相同"


def test_long_block_never_shorter_than_beta_refit_period():
    """⑥ 长块必须 ≥ β 的重拟合周期（u 的块内相关至少延伸到 step 期）。"""
    qt = quantile_test(_mk_wf_blocked(0.5, n=1000, step=200, sd_block=0.05), B=300)
    assert qt["长块长度"] >= 200, f"长块 {qt['长块长度']} < β 重拟合周期 200"
    assert qt["长块长度"] >= qt["短块长度"]


def test_long_block_covers_one_year_and_beta_period():
    """⑥ 锁死块长公式：长块 = max(round(n/12), step)，用返回值断言而非看代码。"""
    a = quantile_test(_mk_wf_blocked(0.5, n=2303, step=50), B=200)
    assert a["长块长度"] == round(2303 / 12) == 192
    b = quantile_test(_mk_wf_blocked(0.5, n=1725, step=50), B=200)
    assert b["长块长度"] == 144
    assert a["长块长度"] >= 50 and b["长块长度"] >= 50
    assert a["长块长度"] >= a["短块长度"] and b["长块长度"] >= b["短块长度"]


def test_long_block_verdict_is_never_more_aggressive_under_block_correlated_u():
    """⑥ 块相关数据上，长块口径不得比短块口径更激进。

    ⚠️ 只在**块相关**数据上断言。i.i.d. 序列上长短块 se 方向随机
    （实测 4/10 长块更窄），那种数据不代表真实生成机制。
    """
    for seed in range(6):
        wf = _mk_wf_blocked(0.47, seed=seed)
        qt = quantile_test(wf, B=400)
        assert qt["ci95"][1] >= qt["ci95_短块"][1] - 1e-12, (seed, "CI 上界变窄")
        assert qt["z"] >= qt["z_短块"] - 1e-12, (seed, "z 变得更激进")
        if qt["gap"]:
            forced = quantile_test(wf, B=400, long_block=qt["短块长度"])
            assert forced["gap"] is True, (seed, "长块检出、短块却没检出")


def test_long_block_length_actually_reaches_the_bootstrap():
    """⑥ 注入的块长必须真的进到 bootstrap 里（不同块长 → 不同 se）。

    刻意**不断言「块越长 CI 越宽」**：实测该单调性在 10 个 seed 里违例 6 次
    （移动块 bootstrap 的随机性所致），断言它会 flaky。这里只锁「块长被采纳」。
    """
    wf = _mk_wf_blocked(0.47, n=2000, step=50)
    ses = {}
    for b in (20, 50, 100, 167):
        qt = quantile_test(wf, B=400, long_block=b)
        assert qt["长块长度"] == b
        ses[b] = qt["se_长块bootstrap"]
    assert len({round(v, 10) for v in ses.values()}) == 4, ses


def test_real_scale_verdict_unchanged_after_long_block():
    """⑥ 真实量级回归锁：n=2303 / ū≈0.4958 / step=50 ⇒ 长块 192、判 none。"""
    for seed in (1, 2, 3):
        wf = _mk_wf_blocked(0.4958, n=2303, seed=seed, step=50)
        qt = quantile_test(wf, B=400)
        assert qt["长块长度"] == 192
        assert qt["gap"] is False, (seed, qt["note"])


def test_real_pipeline_never_falls_back_to_short_se():
    """⑥ 真实管线产物必须自带长块字段 ⇒ fit_gamma 的短块回退在生产路径不可达。"""
    qt = quantile_test(_mk_wf(0.5), B=200)
    assert qt["se_长块bootstrap"] and qt["se_长块bootstrap"] > 0
    assert qt["se"] == qt["se_长块bootstrap"]


# ------------------------------------------------------ ⑦ γ=0 红线的口径稳健性

def _gamma_env(monkeypatch, k_A=0.03, k_B=0.30):
    monkeypatch.setattr(
        "ev.mechanism.gap_b_drawn_quantile.gamma_scale",
        lambda lot, frame=None: {"k_A": k_A, "k_B": k_B, "M": 1000,
                                 "cbar": 1.0, "sd_w": 0.17})


# 效应量对齐：γ_B = (0.5-0.494)/0.30 = 0.02、γ_A = (1-0.9994)/0.03 = 0.02
_WEAK_U, _WEAK_R = 0.494, 0.9994
# 强信号：γ_B = (0.5-0.35)/0.30 = 0.5、γ_A = (1-0.985)/0.03 = 0.5
_STRONG_U, _STRONG_R = 0.35, 0.985


def _gq(mean_u, se_long=None, se_short=None):
    d = {"applicable": True, "mean_u": mean_u, "n": 5000}
    if se_long is not None:
        d["se_长块bootstrap"] = se_long
    if se_short is not None:
        d["se_块bootstrap"] = se_short
    return d


def _ga(R, se_long=None, se_short=None):
    d = {"applicable": True, "主口径": {"R": R}}
    if se_long is not None:
        d["主口径"]["se_长块bootstrap"] = se_long
    if se_short is not None:
        d["主口径"]["se_块bootstrap"] = se_short
    return d


def test_gamma_zero_redline_holds_under_both_se_conventions(monkeypatch):
    """⑦★ 红线不能因标准误口径的选择而漂移。

    同一个**弱**效应（γ=0.02，远小于噪声），无论 se 取窄（只有短块字段）
    还是宽（只有长块字段）、或真实管线形态（两者都给、长块 = 3×短块），
    都必须判为**不显著** ⇒ `mechanism_gamma` 返回 0.0 ⇒ 不做任何倾斜。
    """
    from ev import gap
    _gamma_env(monkeypatch)
    variants = {
        "只有短块(窄)": (_gq(_WEAK_U, se_short=0.01), _ga(_WEAK_R, se_short=0.001)),
        "只有长块(宽)": (_gq(_WEAK_U, se_long=0.03), _ga(_WEAK_R, se_long=0.003)),
        "真实管线(长=3×短)": (_gq(_WEAK_U, 0.03, 0.01), _ga(_WEAK_R, 0.003, 0.001)),
    }
    sems = {}
    for name, (q, a) in variants.items():
        g = fit_gamma("双色球", wf={}, qtest=q, a_result=a)
        assert g["gamma_合并"]["显著"] is False, (name, g["结论"])
        sems[name] = g["gamma_合并"]["se"]
        monkeypatch.setattr(gap, "measure",
                            lambda lot, _g=g, **k: {"gamma": {"gamma_合并": _g["gamma_合并"]}})
        assert gap.mechanism_gamma("双色球") == 0.0, name
    assert sems["只有长块(宽)"] > sems["只有短块(窄)"], "长块 se 应更保守"


def test_gamma_real_signal_survives_both_se_conventions(monkeypatch):
    """⑦ 反向：真信号（γ=0.5）不能因为口径变严就被漏掉。"""
    _gamma_env(monkeypatch)
    for name, (q, a) in {
        "只有短块(窄)": (_gq(_STRONG_U, se_short=0.03), _ga(_STRONG_R, se_short=0.003)),
        "只有长块(宽)": (_gq(_STRONG_U, se_long=0.06), _ga(_STRONG_R, se_long=0.006)),
    }.items():
        g = fit_gamma("双色球", wf={}, qtest=q, a_result=a)
        assert g["gamma_合并"]["显著"] is True, (name, g["结论"])
        assert "冷门偏置" in g["结论"]


def test_gamma_significance_moves_only_when_long_block_se_moves(monkeypatch):
    """⑦ 变异测试：把长块 se 放大 6 倍必须让显著性翻转。

    这条锁的是「判定**真的**吃 `se_长块bootstrap`」—— 防止有人把 `fit_gamma`
    悄悄改回读短块，而测试因为效应量太大照样全绿。
    """
    _gamma_env(monkeypatch)
    base_q, base_a = _gq(_STRONG_U, 0.03), _ga(_STRONG_R, 0.003)
    wide_q, wide_a = _gq(_STRONG_U, 0.18), _ga(_STRONG_R, 0.018)
    assert fit_gamma("双色球", wf={}, qtest=base_q,
                     a_result=base_a)["gamma_合并"]["显著"] is True
    assert fit_gamma("双色球", wf={}, qtest=wide_q,
                     a_result=wide_a)["gamma_合并"]["显著"] is False


def test_analytic_se_is_never_read_by_gamma(monkeypatch):
    """⑦ 变异测试：把解析口径改成极端值，γ 必须逐位不变。

    证明「解析 se 只作参照」不是文档承诺，而是真的没有任何读取路径。
    """
    _gamma_env(monkeypatch)
    base = fit_gamma("双色球", wf={},
                     qtest=_gq(_WEAK_U, 0.02, 0.01),
                     a_result=_ga(_WEAK_R, 0.002, 0.001))
    wild_q = dict(_gq(_WEAK_U, 0.02, 0.01),
                  se_解析=1e-9, z_解析=-9999.0, ci95_解析=[0.0, 1.0])
    wild = fit_gamma("双色球", wf={}, qtest=wild_q,
                     a_result=_ga(_WEAK_R, 0.002, 0.001))
    assert base["gamma_合并"] == wild["gamma_合并"]


def test_fit_gamma_falls_back_to_short_se_and_labels_it(monkeypatch):
    """⑦ 取不到长块字段时必须【显式回退 + 标注】，不许静默换口径。"""
    _gamma_env(monkeypatch)
    g = fit_gamma("双色球", wf={},
                  qtest=_gq(_WEAK_U, se_short=0.01),
                  a_result=_ga(_WEAK_R, se_short=0.001))
    assert g["se_口径"] == {"gamma_B": "短块bootstrap(回退)",
                            "gamma_A": "短块bootstrap(回退)"}
    assert g["gamma_合并"] is not None
    # 真实管线里 qtest 的 `se` 字段就是长块，不能被误标成短块回退
    g2 = fit_gamma("双色球", wf={},
                   qtest={"applicable": True, "mean_u": _WEAK_U, "n": 5000,
                          "se": 0.02},
                   a_result=None)
    assert g2["se_口径"]["gamma_B"] == "字段se（真实管线里即长块）"
    assert "短块" not in g2["se_口径"]["gamma_B"]
