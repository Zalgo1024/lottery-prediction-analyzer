# -*- coding: utf-8 -*-
"""tests/test_lowcrowd_backtest.py —— 四臂对照回测的契约 + 反假增益测试。

★ 这里锁的不是「策略有效」，而是**不许把噪声写成结论**：
  1. 四臂必须逐期对齐（同一批开奖期），否则配对符号检验会被 zip 静默截断；
  2. 主终点是「单期平均中奖注数」——各组合等可能 ⇒ 换选号不改变它。
     四臂在这上面出现显著差异，第一解释是**实现缺陷或前视泄漏**，不是策略有效；
  3. 判定口径「组间差 vs 跨 seed 全距」必须在代码里写死，不许事后挑指标；
  4. ≥5 元在生产注数下恒饱和（六等只需命中蓝球），只作报告不进判定；
  5. 块 bootstrap 的块长必须 ≥ β 的 walk-forward 重拟合步长（同块共享 β ⇒
     拆得更细就是把自相关当独立样本，区间假窄）。

真实数据只在一个 slow 用例里跑最小规模，其余全部用合成数据打桩，保证秒级。
"""
from types import SimpleNamespace

import numpy as np
import pytest

from ev import lowcrowd_backtest as lb


# ---------------------------------------------------------------- 打桩工具
def _rec(issue, red=(1, 2, 3, 4, 5, 6), blue=(16,)):
    return SimpleNamespace(期号=issue, zone_numbers={"红球": list(red), "蓝球": list(blue)})


def _fake_recs(n=60):
    return [_rec(f"2026{i:04d}") for i in range(n)]


def _patch_data(monkeypatch, n=60, block_size=20, min_train=20):
    """把回放数据与 β 分块全打桩：纯合成，秒级，且能精确控制窗口对齐。"""
    recs = _fake_recs(n)
    monkeypatch.setattr(lb, "load_replay_recs",
                        lambda lottery: (recs, {"issues": [r.期号 for r in recs]},
                                         {r.期号: i for i, r in enumerate(recs)}))
    blocks = []
    for c in range(min_train, n, block_size):
        blocks.append({"切点": c, "止": min(c + block_size, n),
                       "期号": [recs[i].期号 for i in range(c, min(c + block_size, n))],
                       "params": {"beta": np.zeros(9), "m_": np.zeros(9), "s_": np.ones(9)}})
    monkeypatch.setattr(lb, "beta_blocks", lambda *a, **k: blocks)
    monkeypatch.setattr("ev.popularity_proxy.MIN_TRAIN", min_train)
    return recs


def _patch_arms(monkeypatch, pool_size=200):
    """四臂的选号全部换成同池不同切片 —— 只测回放 plumbing，不测选号质量。"""
    def _pool(lottery, size, seed):
        rng = np.random.default_rng(seed)
        return [{"红球": sorted(int(x) for x in rng.choice(33, 6, replace=False) + 1),
                 "蓝球": [int(rng.integers(1, 17))]} for _ in range(size)]

    monkeypatch.setattr("ev.coverage_backtest.build_pool", _pool)
    from ev import lowcrowd_select as ls
    from ev import popularity_proxy as pp

    monkeypatch.setattr(pp, "crowd_quantiles",
                        lambda lottery, tks, **k: np.linspace(0.0, 1.0, len(tks)))
    monkeypatch.setattr(ls, "select_lowcrowd",
                        lambda lottery, pool, K, u, **k: (pool[:K], {"门控": {}}))
    monkeypatch.setattr(lb, "prod_mix_tickets", lambda lottery, win, K, w, s: win and [_x for _x in _dummy(K)])
    monkeypatch.setattr(lb, "_netev_tickets", lambda lottery, pool, K, s, i: pool[:K])
    monkeypatch.setattr(lb, "_random_tickets",
                        lambda lottery, pool, K, s, cap=None: pool[:K])
    return _pool


def _dummy(K):
    rng = np.random.default_rng(0)
    for _ in range(K):
        yield {"红球": sorted(int(x) for x in rng.choice(33, 6, replace=False) + 1),
               "蓝球": [int(rng.integers(1, 17))]}


# ---------------------------------------------------------------- ① 回放指标口径

def test_replay_plan_metrics_semantics():
    """已知开奖：命中注数 / ≥5元 / ≥10元 / 总实得 / 净收益(=总实得 − 期数×K×2元)。"""
    draw = _rec("2026001", red=(1, 2, 3, 4, 5, 6), blue=(16,))
    a = {"红球": [7, 8, 9, 10, 11, 12], "蓝球": [16]}    # 0红+1蓝 → 六等 5 元
    b = {"红球": [1, 2, 3, 4, 5, 7], "蓝球": [1]}        # 5红 → 四等 200 元
    c = {"红球": [7, 8, 9, 10, 11, 12], "蓝球": [1]}     # 未中
    r = lb.replay_plan("双色球", [("1", draw, [a, b, c]), ("2", draw, [a, b, c])], {})
    assert r["期数"] == 2 and r["K"] == 3
    assert r["中奖次数"] == 4
    assert r["单期平均中奖注数"] == 2.0
    assert r["≥5元期占比"] == 1.0 and r["≥10元期占比"] == 1.0
    assert r["总实得"] == 410.0
    assert r["净收益"] == 410.0 - 2 * 3 * 2.0


def test_replay_plan_empty():
    assert lb.replay_plan("双色球", []) == {"期数": 0}


# ---------------------------------------------------------------- ② 四臂逐期对齐（★）

@pytest.mark.parametrize("periods", [50, None])
def test_all_arms_land_on_same_periods(monkeypatch, periods):
    """四臂必须覆盖同一批开奖期 —— 否则配对差符号检验会被 zip 静默截断成对错的期。

    全量（periods=None）时低拥挤臂的 β 需要 MIN_TRAIN 期训练历史，
    故公共窗口从 MIN_TRAIN 起算，**其余三臂也必须跟着从那里起算**。
    """
    n = 60
    min_train = 20
    _patch_data(monkeypatch, n=n, block_size=20, min_train=min_train)
    _patch_arms(monkeypatch)
    got = {}
    for arm in lb.ARMS:
        r = lb.run_arm("双色球", arm, periods=periods, K=20, seed=0, pool_size=200)
        got[arm] = r
        assert r["期数"] > 0, f"{arm} 没出任何期：{r}"
    counts = {a: r["期数"] for a, r in got.items()}
    assert len(set(counts.values())) == 1, f"四臂期数不一致（配对比较会失效）：{counts}"
    # 全量时必须从 MIN_TRAIN 起算，不能从 0 起算
    if periods is None:
        assert got["lowcrowd"]["回放起点"] == min_train
        assert counts["lowcrowd"] == n - min_train


def test_series_lengths_match_period_count(monkeypatch):
    """序列长度必须等于期数 —— 保证下游逐期配对能一一对应。"""
    _patch_data(monkeypatch, n=60, block_size=20, min_train=20)
    _patch_arms(monkeypatch)
    for arm in lb.ARMS:
        r = lb.run_arm("双色球", arm, periods=50, K=20, seed=0, pool_size=200)
        assert len(r["序列"]["≥10元"]) == r["期数"]
        assert len(r["序列"]["中奖注数"]) == r["期数"]


def test_unknown_arm_raises(monkeypatch):
    _patch_data(monkeypatch)
    _patch_arms(monkeypatch)
    with pytest.raises(ValueError):
        lb.run_arm("双色球", "不存在的臂", periods=50, K=10, pool_size=100)


# ---------------------------------------------------------------- ③ 反假增益判定口径

def test_sign_test_symmetric_and_empty():
    r = lb.sign_test([1, 0, 1, 0], [0, 1, 0, 1])
    assert r["正期数"] == 2 and r["负期数"] == 2 and r["有效期数"] == 4
    assert r["p值"] == 1.0
    r0 = lb.sign_test([1, 1, 1], [1, 1, 1])       # 完全无差异
    assert r0["有效期数"] == 0 and r0["p值"] is None


def _ar1(n=800, rho=0.9, seed=3):
    """强正自相关序列：用来暴露「块长取 1」把自相关当独立样本 ⇒ 区间假窄。"""
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n)
    x = np.empty(n)
    x[0] = e[0]
    for t in range(1, n):
        x[t] = rho * x[t - 1] + e[t]
    return x


def test_block_bootstrap_ci_constant_series_is_degenerate():
    """常数序列没有抽样可变性 ⇒ 区间退化成一个点，这是自检锚点。"""
    r = lb.block_bootstrap_ci([3.0] * 120, block=30, reps=200, seed=0)
    assert r["配对期数"] == 120 and r["块长"] == 30
    assert r["差均值"] == 3.0 and r["CI下限"] == 3.0 and r["CI上限"] == 3.0
    assert r["含零"] is False
    assert lb.block_bootstrap_ci([0.0] * 120, block=30, reps=200, seed=0)["含零"] is True


def test_block_bootstrap_ci_degrades_on_short_series():
    assert "降级" in lb.block_bootstrap_ci([], block=50)
    assert "降级" in lb.block_bootstrap_ci([1.0], block=50)


def test_block_bootstrap_ci_is_seed_deterministic():
    d = list(_ar1(300))
    a = lb.block_bootstrap_ci(d, block=50, reps=300, seed=11)
    assert a == lb.block_bootstrap_ci(d, block=50, reps=300, seed=11)
    # 换种子必须换重抽样 —— 防止「区间」其实是写死的常量
    assert lb.block_bootstrap_ci(d, block=50, reps=300, seed=12) != a


def test_block_bootstrap_ci_block_capped_at_series_length():
    assert lb.block_bootstrap_ci(list(range(20)), block=50, reps=100, seed=0)["块长"] == 20


def test_block_bootstrap_ci_longer_block_is_wider_on_autocorrelated_series():
    """★ 块长取 1（≈ 逐期独立）会低估自相关序列的方差；取 ≥ β 重拟合步长才不假窄。"""
    d = list(_ar1(800, rho=0.9))
    narrow = lb.block_bootstrap_ci(d, block=1, reps=800, seed=5)
    wide = lb.block_bootstrap_ci(d, block=lb.BETA_STEP, reps=800, seed=5)
    w_narrow = narrow["CI上限"] - narrow["CI下限"]
    w_wide = wide["CI上限"] - wide["CI下限"]
    assert w_wide > w_narrow, (narrow, wide)


def test_block_bootstrap_step_matches_beta_refit_period():
    """★ 默认块长必须等于 β 的 walk-forward 重拟合步长：同块共享 β ⇒ 不能拆得比它更细。"""
    import inspect

    assert lb.BETA_STEP == 50
    assert inspect.signature(lb.beta_blocks).parameters["step"].default == lb.BETA_STEP


def test_pair_metrics_are_the_preregistered_endpoints():
    """配对指标事前写死：主终点 + 次终点，不许事后挑；饱和的 ≥5 元不在其中。"""
    assert tuple(lb.PAIR_METRICS) == ("中奖注数", "≥10元")
    assert "≥5元" not in lb.PAIR_METRICS


def test_paired_report_pairs_two_series_and_flags_truncation():
    a = {"臂": "lowcrowd", "序列": {"≥10元": [1, 0, 1, 1, 0, 1], "中奖注数": [3, 1, 4, 2, 0, 5]}}
    b = {"臂": "random", "序列": {"≥10元": [1, 1, 1, 0], "中奖注数": [2, 2, 3, 3]}}
    r = lb.paired_report(a, b, metric="≥10元", block=2, reps=100, seed=0)
    assert r["配对期数"] == 4      # 按较短者对齐
    assert r["截断"] is True       # 长度不等必须标出，不许静默拿错期配对
    assert r["臂A"] == "lowcrowd" and r["臂B"] == "random"
    assert "块bootstrap" in r and "符号检验" in r


def test_paired_report_missing_series_degrades():
    assert "降级" in lb.paired_report({"臂": "x"}, {"臂": "y", "序列": {"≥10元": [1]}})
    assert "降级" in lb.paired_report({"臂": "x", "序列": {}}, {"臂": "y", "序列": {}})


def test_agg_paired_counts_zero_inclusion_not_min_pvalue():
    """多种子聚合只数「区间含零」的次数 —— 不许挑最小 p 值当结论。"""
    def _rep(zero):
        return {"配对期数": 100, "块bootstrap": {"含零": zero},
                "符号检验": {"p值": 0.001}}

    agg = lb._agg_paired([_rep(True), _rep(True), _rep(False)])
    assert agg["运行次数"] == 3 and agg["含零次数"] == 2 and agg["不含零次数"] == 1
    assert lb._agg_paired([])["运行次数"] == 0


def test_compare_emits_paired_block_bootstrap_vs_random(monkeypatch):
    """compare 必须把「块 bootstrap + 符号检验」一起产出，且只按 PAIR_METRICS 成对比较。"""

    def _fake_run(lottery, arm, periods=None, K=200, seed=0, **kw):
        rng = np.random.default_rng(seed + (0 if arm == "random" else 1))
        n = 120
        return {"臂": arm, "期数": n, "seed": seed,
                "序列": {"≥10元": [int(x) for x in rng.integers(0, 2, n)],
                         "中奖注数": [int(x) for x in rng.integers(0, 6, n)],
                         "实得": [float(x) for x in rng.integers(0, 40, n)]}}

    monkeypatch.setattr(lb, "run_arm", _fake_run)
    out = lb.compare("双色球", arms=("net_ev", "random"), windows=(500,), seeds=(0, 1),
                     lam_grid=(), gate_grid=(), bootstrap=True,
                     bootstrap_reps=60, block=20)
    blk = out["窗口"][0]
    assert set(blk["配对vs随机"]) == {"net_ev"}
    assert set(blk["配对vs随机"]["net_ev"]) == set(lb.PAIR_METRICS)
    reps = blk["配对vs随机"]["net_ev"]["≥10元"]
    assert reps["运行次数"] == 2 and len(reps["各seed"]) == 2
    assert all("块bootstrap" in r and "符号检验" in r for r in reps["各seed"])
    # 关掉开关就不产出 —— 默认路径不被改动
    out2 = lb.compare("双色球", arms=("net_ev", "random"), windows=(500,), seeds=(0,),
                      lam_grid=(), gate_grid=(), bootstrap=False)
    assert "配对vs随机" not in out2["窗口"][0]


def _ar1(n=600, rho=0.9, seed=3):
    """强正自相关序列：用来暴露「块长取 1」把自相关当独立样本 ⇒ CI 假窄。"""
    rng = np.random.default_rng(seed)
    e = rng.standard_normal(n)
    x = np.empty(n, dtype=float)
    x[0] = e[0]
    for t in range(1, n):
        x[t] = rho * x[t - 1] + e[t]
    return list(x)


def test_block_bootstrap_ci_degenerate_and_empty():
    """常数序列没有抽样可变性 ⇒ CI 退化成一个点；空/单期 ⇒ 降级而不是给个假区间。"""
    r = lb.block_bootstrap_ci([3.0] * 120, block=30, reps=200, seed=0)
    assert r["配对期数"] == 120 and r["块长"] == 30
    assert r["差均值"] == 3.0
    assert r["CI下限"] == 3.0 and r["CI上限"] == 3.0
    assert r["含零"] is False and "不含 0" in r["判定"]
    r0 = lb.block_bootstrap_ci([0.0] * 60, block=30, reps=100, seed=0)
    assert r0["含零"] is True and "含 0" in r0["判定"]
    assert "降级" in lb.block_bootstrap_ci([], block=50)
    assert "降级" in lb.block_bootstrap_ci([1.0], block=50)


def test_block_bootstrap_ci_is_deterministic_and_caps_block():
    d = _ar1(300)
    a = lb.block_bootstrap_ci(d, block=50, reps=300, seed=11)
    assert lb.block_bootstrap_ci(d, block=50, reps=300, seed=11) == a
    assert lb.block_bootstrap_ci(d, block=50, reps=300, seed=12) != a  # 换种子必须换重抽样
    # 块长超过序列长度时收敛到 n，而不是越界
    assert lb.block_bootstrap_ci(list(range(20)), block=50, reps=100, seed=0)["块长"] == 20


def test_block_bootstrap_ci_brackets_the_point_estimate():
    d = _ar1(400)
    r = lb.block_bootstrap_ci(d, block=40, reps=500, seed=2)
    assert r["CI下限"] <= r["差均值"] <= r["CI上限"]


def test_block_bootstrap_ci_longer_block_is_wider_on_autocorrelated_series():
    """★ 块长取 1（≈逐期独立）会把自相关当独立样本 ⇒ CI 假窄。"""
    d = _ar1(800, rho=0.9)
    narrow = lb.block_bootstrap_ci(d, block=1, reps=800, seed=5)
    wide = lb.block_bootstrap_ci(d, block=lb.BETA_STEP, reps=800, seed=5)
    w1 = narrow["CI上限"] - narrow["CI下限"]
    w2 = wide["CI上限"] - wide["CI下限"]
    assert w2 > w1, (narrow, wide)


def test_block_length_default_equals_beta_refit_step():
    """★ 默认块长 = walk-forward β 的重拟合步长：同块共享 β，不许拆得比它更细。"""
    import inspect

    assert lb.BETA_STEP == 50
    assert inspect.signature(lb.beta_blocks).parameters["step"].default == lb.BETA_STEP


def test_paired_report_pairs_same_periods_and_flags_truncation():
    a = {"臂": "lowcrowd", "序列": {"≥10元": [1, 0, 1, 1, 0, 1], "中奖注数": [3, 1, 4, 2, 0, 5]}}
    b = {"臂": "random", "序列": {"≥10元": [1, 1, 1, 0], "中奖注数": [2, 2, 3, 3]}}
    r = lb.paired_report(a, b, metric="≥10元", block=2, reps=100, seed=0)
    assert r["配对期数"] == 4      # 按较短者对齐
    assert r["截断"] is True       # 长度不等必须标出，不许静默拿错期的配对
    assert r["臂A"] == "lowcrowd" and r["臂B"] == "random"
    assert "块bootstrap" in r and "符号检验" in r


def test_paired_report_missing_series_degrades():
    assert "降级" in lb.paired_report({"臂": "x"}, {"臂": "y", "序列": {"≥10元": [1]}})
    assert "降级" in lb.paired_report({"臂": "x", "序列": {}}, {"臂": "y", "序列": {}})


def test_agg_paired_counts_zero_inclusion_not_min_pvalue():
    """多种子聚合只数「区间含零」的次数——不许挑最小的 p 值当结论。"""

    def _rep(zero):
        return {"配对期数": 100,
                "块bootstrap": {"含零": zero, "CI下限": -1.0, "CI上限": 1.0},
                "符号检验": {"p值": 0.001}}

    agg = lb._agg_paired([_rep(True), _rep(True), _rep(False)])
    assert agg["运行次数"] == 3 and agg["含零次数"] == 2 and agg["不含零次数"] == 1
    assert lb._agg_paired([])["运行次数"] == 0


def test_compare_emits_paired_bootstrap_vs_random(monkeypatch):
    """compare 必须成对产出块 bootstrap + 符号检验，且指标只取事前写死的 PAIR_METRICS。"""

    def _fake_run(lottery, arm, periods=None, K=200, seed=0, **kw):
        rng = np.random.default_rng(seed + (0 if arm == "random" else 1))
        n = 120
        return {"臂": arm, "期数": n, "seed": seed,
                "序列": {"≥10元": [int(x) for x in rng.integers(0, 2, n)],
                         "中奖注数": [int(x) for x in rng.integers(0, 6, n)],
                         "实得": [float(x) for x in rng.integers(0, 40, n)]}}

    monkeypatch.setattr(lb, "run_arm", _fake_run)
    out = lb.compare("双色球", arms=("net_ev", "random"), windows=(500,), seeds=(0, 1),
                     lam_grid=(), gate_grid=(), bootstrap=True,
                     bootstrap_reps=60, block=20)
    blk = out["窗口"][0]
    assert set(blk["配对vs随机"]) == {"net_ev"}
    assert set(blk["配对vs随机"]["net_ev"]) == set(lb.PAIR_METRICS)
    reps = blk["配对vs随机"]["net_ev"]["≥10元"]
    assert reps["运行次数"] == 2 and len(reps["各seed"]) == 2
    assert all("块bootstrap" in r and "符号检验" in r for r in reps["各seed"])
    # 关掉开关就不产出 ⇒ 默认路径的行为没被改动
    out2 = lb.compare("双色球", arms=("net_ev", "random"), windows=(500,), seeds=(0,),
                      lam_grid=(), gate_grid=(), bootstrap=False)
    assert "配对vs随机" not in out2["窗口"][0]


def test_seed_spread_reports_range():
    s = lb.seed_spread([0.10, 0.12, 0.20])
    assert s["全距"] == 0.10
    assert s["min"] == 0.10 and s["max"] == 0.20
    assert lb.seed_spread([]) == {}


def test_verdict_calls_noise_when_diff_within_seed_spread():
    low = {"均值": 0.30, "全距": 0.08}
    base = {"均值": 0.28, "全距": 0.09}
    assert "噪声内" in lb.verdict(low, base)
    # 差大于全距 ⇒ 不许直接宣布「有效」，而是要求先排查泄漏/缺陷
    low2 = {"均值": 0.60, "全距": 0.02}
    base2 = {"均值": 0.30, "全距": 0.02}
    v = lb.verdict(low2, base2)
    assert "噪声内" not in v and "核查" in v


def test_primary_endpoint_is_mean_wins_not_ge5():
    """主终点 = 单期平均中奖注数；≥5 元只作报告（生产注数下恒饱和）。"""
    doc = lb.__doc__ or ""
    assert "单期平均中奖注数" in doc
    assert "≥10 元" in doc
    assert lb.SATURATION_NOTE and "饱和" in lb.SATURATION_NOTE
    for k in ("≥5元期占比", "≥10元期占比", "单期平均中奖注数", "净收益"):
        assert k in lb.replay_plan("双色球", [("1", _rec("1"), [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [16]}])], {})


def test_four_arms_are_declared():
    assert set(lb.ARMS) == {"lowcrowd", "prod_mix", "net_ev", "random"}
    # 情景网格：λ 与 gate 都是**网格**，不是一个从历史反推的点估计
    assert len(lb.LAM_GRID) >= 3 and len(lb.GATE_GRID) >= 3
    assert lb.DEFAULT_SEEDS and len(lb.DEFAULT_SEEDS) >= 3


def test_overlap_cap_defaults_to_production_caliber():
    """★ 重叠上限必须默认是 3（生产 `_dedup_and_record` 口径），不是 `_OVERLAP_CAP=4`。

    实测（窗口 500，K=200，λ=0.5/gate=0.30）：lowcrowd ≥10 元 cap=3 → 0.748、cap=4 → 0.630，
    而同池随机几乎不受影响（0.812 / 0.800）。用 4 会把 cap 差异误当成选号差异。
    """
    assert lb.PROD_OVERLAP_CAP == 3
    from ev.selection import _OVERLAP_CAP
    assert _OVERLAP_CAP["双色球"] == 4      # 两条口径并存，故必须显式指定
    assert lb.PROD_OVERLAP_CAP != _OVERLAP_CAP["双色球"]


def test_random_arm_uses_the_same_cap(monkeypatch):
    """随机对照必须与低拥挤臂用同一个 cap，否则对照就不是「唯一差异=排序」了。"""
    _patch_data(monkeypatch, n=60, block_size=20, min_train=20)
    _patch_arms(monkeypatch)
    seen = {}

    def _spy(lottery, pool, K, s, cap=None):
        seen["cap"] = cap
        return pool[:K]

    monkeypatch.setattr(lb, "_random_tickets", _spy)
    lb.run_arm("双色球", "random", periods=50, K=20, seed=0, pool_size=200)
    assert seen["cap"] == lb.PROD_OVERLAP_CAP


def test_frozen_weights_avoid_future_info():
    """回放只能用冻结均分权重 —— 生产权重文件里含未来信息，用了就是前视泄漏。"""
    assert set(lb.FROZEN_WEIGHTS) == {"高频策略", "遗漏值策略", "区间均衡策略"}
    assert all(abs(v - 1 / 3) < 1e-9 for v in lb.FROZEN_WEIGHTS.values())


# ---------------------------------------------------------------- ④ 真数据最小冒烟（slow）

@pytest.mark.slow
def test_real_data_four_arms_align_and_no_false_gain():
    """真数据最小规模：四臂仍逐期对齐，且「单期平均中奖注数」不得出现大幅背离。

    注意断言方向：这里不许写成「低拥挤臂更好」。若四臂的主终点差超过噪声量级，
    第一解释永远是实现缺陷 / 前视泄漏，而不是策略有效。
    """
    rows = {}
    for arm in lb.ARMS:
        rows[arm] = lb.run_arm("双色球", arm, periods=40, K=20, seed=0, pool_size=600)
    alive = {a: r for a, r in rows.items() if r.get("期数")}
    if len(alive) < 2:
        pytest.skip("本机没有可用的开奖/模型数据，跳过真数据冒烟")
    assert len({r["期数"] for r in alive.values()}) == 1
    # 固定注数对照：每期注数必须四臂一致，否则净收益比较掺的是注数差异
    for a, r in alive.items():
        assert (r["每期注数min"], r["每期注数max"]) == (20, 20), f"{a} 注数不满：{r}"
    base = [r["单期平均中奖注数"] for r in alive.values()]
    # 主终点跨臂不应出现量级差异（K=20、40 期下允许噪声，但不允许成倍背离）
    assert max(base) <= max(min(base) * 3, min(base) + 5), f"主终点跨臂背离过大：{ {a: r['单期平均中奖注数'] for a, r in alive.items()} }"

# ---------------------------------------------------------------- ⑥ 默认口径必须轻

def test_compare_defaults_are_light():
    """★ 裸调 compare() 必须是轻量档：默认就 30 分钟的重活是个陷阱。"""
    import inspect

    assert lb.DEFAULT_WINDOWS == (500,)
    assert lb.DEFAULT_SEEDS == (0, 1, 2)
    assert len(lb.DEFAULT_LAM_GRID) * len(lb.DEFAULT_GATE_GRID) <= 6
    # 全量口径单独一套常量，供 full=True 复现已发表结论
    assert lb.TIER1_WINDOWS[-1] is None
    assert lb.TIER1_SEEDS == (0, 1, 2, 3, 4)
    assert not hasattr(lb, "K_GRID_SMALL"), "无人引用的常量不该留着误导"
    sig = inspect.signature(lb.compare).parameters
    # 用 None 当哨兵：否则 full=True 且显式传 windows=(500,) 会被误判成「没传」
    assert sig["windows"].default is None and sig["seeds"].default is None
    assert sig["full"].default is False


def _stub_run_for_defaults(monkeypatch, calls, n=12):
    def _fake_run(lottery, arm, periods=None, K=200, seed=0, **kw):
        calls.append((periods, seed))
        return {"臂": arm, "期数": n,
                "序列": {"≥10元": [0] * n, "中奖注数": [1] * n, "实得": [0.0] * n}}
    monkeypatch.setattr(lb, "run_arm", _fake_run)


def test_default_compare_only_runs_500_and_six_scenarios(monkeypatch):
    calls = []
    _stub_run_for_defaults(monkeypatch, calls)
    out = lb.compare("双色球", arms=("net_ev", "random"), bootstrap=False)
    assert {p for p, _ in calls} == {500}
    assert out["情景窗口"] == 500
    assert len(out["低拥挤情景"]) == len(lb.DEFAULT_LAM_GRID) * len(lb.DEFAULT_GATE_GRID)


def test_full_true_restores_tier1_caliber(monkeypatch):
    """full=True 必须回到 4 窗口 × 5 seed，且情景窗口显式跟随 windows[-1]。"""
    calls = []
    _stub_run_for_defaults(monkeypatch, calls)
    out = lb.compare("双色球", arms=("net_ev", "random"), full=True, bootstrap=False)
    assert {p for p, _ in calls} == set(lb.TIER1_WINDOWS)
    assert {s for _, s in calls} == set(lb.TIER1_SEEDS)
    assert out["情景窗口"] is None            # = 全量
    assert out["口径"].startswith("全量")
