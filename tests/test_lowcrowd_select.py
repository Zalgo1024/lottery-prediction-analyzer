# -*- coding: utf-8 -*-
"""tests/test_lowcrowd_select.py —— 低拥挤选号器的契约测试。

最重要的一条：**证明 λ 不是空操作**。
纯 `draw_score = -λ·z(crowd)` 排序对任意 λ>0 都选出同一批号（μ 单调 ⇒ 一等奖实得
单调 ⇒ 与现有净 EV 选号严格同序，实测 Spearman ρ = -1.000000），λ 是数学空操作。
所以本模块的两个真杠杆是：

  1) gate_q —— 改**候选集合**（分位门控）；
  2) λ     —— 改**覆盖 ↔ 冷门的相对权重**（λ=0 纯覆盖 / λ=1 纯冷门）。

测试把这两点钉死，顺带钉死共线事实本身（防止后人删掉论证后重新实现成空操作）。
"""
import numpy as np
import pytest

from ev import coverage_backtest as cb
from ev import lowcrowd_select as ls

POOL_N = 5000
K = 100


def _pool_u(n=POOL_N, seed=7):
    """(pool, u, idx_of)：u 用置换过的 linspace 造，保证确定性与满秩。"""
    pool = cb.build_pool("双色球", n, seed=seed)
    u = np.random.default_rng(11).permutation(np.linspace(0.0, 1.0, len(pool)))
    idx_of = {id(t): i for i, t in enumerate(pool)}
    return pool, u, idx_of


def _sig(tickets):
    return sorted(tuple(sorted(t["红球"])) + (int(t["蓝球"][0]),) for t in tickets)


def _cov_keys(tickets):
    ks = set()
    for t in tickets:
        ks.update(ls.keys_of(t))
    return len(ks)


def _mean_u(tickets, u, idx_of):
    return float(np.mean([u[idx_of[id(t)]] for t in tickets]))


def _max_overlap(tickets):
    mx = 0
    for i in range(len(tickets)):
        for j in range(i + 1, len(tickets)):
            mx = max(mx, len(set(tickets[i]["红球"]) & set(tickets[j]["红球"])))
    return mx


# ------------------------------------------------------------ ① 覆盖键口径

def test_keys_of_ticket_is_35():
    """C(6,4)=15 个红球 4-子集 + C(6,3)×蓝=20 个 (3-子集,蓝) = 35。"""
    t = {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}
    ks = ls.keys_of(t)
    assert len(ks) == ls.KEYS_PER_TICKET == 35
    assert len(set(ks)) == 35                  # 编码不能撞车（3/4 子集各有偏移）
    # 蓝球不同 ⇒ 3-子集键不同，但 4-子集键不变（≥10 元通道里 4 红不看蓝）
    ks2 = ls.keys_of({"红球": [1, 2, 3, 4, 5, 6], "蓝球": [8]})
    assert len(set(ks) & set(ks2)) == 15


# ------------------------------------------------------------ ② 基本契约

def test_returns_k_tickets_respecting_overlap_cap():
    pool, u, _ = _pool_u()
    tk, meta = ls.select_lowcrowd("双色球", pool, K, u, lam=0.5, gate_q=0.30, cap=3)
    assert len(tk) == K
    assert meta["实出注数"] == K
    assert _max_overlap(tk) <= 3, "重叠上限 3 是生产口径，选号器不得放宽"


def test_deterministic_for_same_input():
    pool, u, _ = _pool_u()
    a, ma = ls.select_lowcrowd("双色球", pool, K, u, lam=0.5, gate_q=0.30, cap=3)
    b, mb = ls.select_lowcrowd("双色球", pool, K, u, lam=0.5, gate_q=0.30, cap=3)
    assert _sig(a) == _sig(b)
    assert ma["门控"] == mb["门控"]


def test_nan_crowding_returns_nothing_not_fake_scores():
    """拥挤度不可用 ⇒ 一张都不出（由调用方显式降级），不许用 0 分顶替当「最冷」。"""
    pool, _, _ = _pool_u(200)
    tk, meta = ls.select_lowcrowd("双色球", pool, 50, np.full(len(pool), np.nan))
    assert tk == []
    assert "降级" in meta


# ------------------------------------------------------------ ③ 门控是真杠杆

def test_gate_changes_candidate_set():
    """分位门控必须真的改变候选集合 —— 否则它只是个装饰参数。"""
    pool, u, _ = _pool_u()
    a, ma = ls.select_lowcrowd("双色球", pool, K, u, lam=0.5, gate_q=0.20, cap=3)
    b, mb = ls.select_lowcrowd("双色球", pool, K, u, lam=0.5, gate_q=1.00, cap=3)
    assert ma["门控"]["门控后候选数"] < mb["门控"]["门控后候选数"]
    assert _sig(a) != _sig(b), "门控 0.20 与 1.00 选出完全相同的票 ⇒ 门控没生效"


def test_gate_relaxation_is_counted_honestly():
    """请求分位凑不满时会上阶梯放宽，**放宽次数必须跟着计数**。

    历史坑：候选数不足的那一档被 `continue` 跳过后没计数，元数据出现
    「请求 0.01 / 生效 0.20 / 放宽 0 次」这种自相矛盾的说法。
    """
    pool, u, _ = _pool_u()
    _, meta = ls.select_lowcrowd("双色球", pool, K, u, lam=0.5, gate_q=0.01, cap=3)
    g = meta["门控"]
    assert g["请求分位"] == 0.01
    assert g["生效分位"] > 0.01
    assert g["放宽次数"] >= 1, "生效分位已放宽，放宽次数却报 0"


def test_insufficient_capacity_reports_gap_instead_of_silently_padding():
    """凑不满 K 时必须写明缺口，不许静默补随机票（那会把策略归因稀释掉）。"""
    pool, u, _ = _pool_u(800)
    tk, meta = ls.select_lowcrowd("双色球", pool, 3000, u, lam=0.5, gate_q=1.0, cap=0)
    assert len(tk) < 3000
    assert meta["实出注数"] == len(tk)
    assert "不足" in meta


# ------------------------------------------------------------ ④ λ 不是空操作

def test_lambda_trades_coverage_against_coldness():
    """λ=0 偏覆盖、λ=1 偏冷门：覆盖键更多 / 选中的平均拥挤分位更低，且两集合不同。"""
    pool, u, idx = _pool_u()
    a, _ = ls.select_lowcrowd("双色球", pool, K, u, lam=0.0, gate_q=0.30, cap=3)
    b, _ = ls.select_lowcrowd("双色球", pool, K, u, lam=1.0, gate_q=0.30, cap=3)
    cov_a, cov_b = _cov_keys(a), _cov_keys(b)
    mu_a, mu_b = _mean_u(a, u, idx), _mean_u(b, u, idx)
    assert cov_a >= cov_b, f"λ=0 覆盖({cov_a}) 不应低于 λ=1 覆盖({cov_b})"
    assert mu_b <= mu_a, f"λ=1 平均拥挤分位({mu_b:.4f}) 不应高于 λ=0 ({mu_a:.4f})"
    assert _sig(a) != _sig(b), "λ=0 与 λ=1 选出完全相同的票 ⇒ λ 是空操作"


def test_lambda_zero_hits_theoretical_ceiling_at_this_scale():
    """纯覆盖时每注 35 键互不碰撞（重叠≤3 ⇒ 4-子集天然不撞），覆盖键 = K×35。"""
    pool, u, _ = _pool_u()
    a, meta = ls.select_lowcrowd("双色球", pool, K, u, lam=0.0, gate_q=0.30, cap=3)
    assert _cov_keys(a) == meta["覆盖"]["理论上限"] == ls.KEYS_PER_TICKET * K


def test_ladder_defaults_are_monotone_and_end_at_no_gate():
    seq = ls._ladder(0.30)
    assert seq[0] == 0.30 and seq[-1] == 1.0
    assert all(seq[i] < seq[i + 1] for i in range(len(seq) - 1))
    assert ls._ladder(1.0) == [1.0]


# ------------------------------------------------------------ ⑤ 共线事实（为什么要这样设计）

def test_pure_crowd_sort_is_collinear_with_net_ev():
    """★ 共线：μ 越大（越热门）⇒ 一等奖实得 E[min(cap,池/(1+X))] 越小，严格单调。

    这就是「按低拥挤降序」≡「按一等奖实得降序」≡ 现有净 EV 选号的数学原因，
    也是纯 `-λ·z(crowd)` 排序对任意 λ>0 都是空操作的根因。ρ 必须 = -1。
    """
    from ev.payout import pool_share_expected_batch
    from ev.popularity import batch_mu, get_popularity_model
    from scipy.stats import spearmanr

    params = get_popularity_model("双色球")
    assert params.get("go"), "双色球 popularity 模型必须已 Go，否则首版拥挤度无从谈起"
    pool, _, _ = _pool_u(400)
    R = np.array([t["红球"] for t in pool], dtype=np.int16)
    mus = batch_mu("双色球", R, 4e8, 1.0 / 17721088, params)
    assert np.isfinite(mus).all()
    eff1 = pool_share_expected_batch(1e8, mus)
    rho = float(spearmanr(eff1, mus).statistic)
    assert rho == pytest.approx(-1.0, abs=1e-9), f"ρ={rho}：共线断了 ⇒ 上面的论证需要重写"


def test_design_rationale_is_kept_in_module():
    """论证写在模块 docstring 里；删掉它，下一个人会重新实现成空操作。"""
    for token in ("空操作", "门控", "加权", "共线"):
        assert token in (ls.__doc__ or ""), f"docstring 缺「{token}」"


# ------------------------------------------------------------ ⑥ 元数据口径

def test_meta_carries_caliber_and_caveat():
    pool, u, _ = _pool_u(500)
    _, meta = ls.select_lowcrowd("双色球", pool, 50, u, lam=0.5, gate_q=0.30, cap=3,
                                 beta_caliber="冻结β（与 ev/selection 同口径）")
    assert meta["β口径"].startswith("冻结β")
    assert meta["λ"] == 0.5
    assert meta["重叠上限"] == 3
    assert "循环性声明" in meta and meta["循环性声明"]
    assert meta["覆盖"]["目标奖级"].startswith("≥10 元")
    assert meta["z口径"]
    assert "全池分位" in meta["门控"]


def test_load_params_has_three_scenarios_and_default_is_middle():
    """λ 是**情景参数**，三档都必须留着可配置 —— 不许只剩一个从历史反推的点估计。"""
    p = ls.load_params()
    assert p["档位名"] == "中性"
    names = [d["名"] for d in p["情景档位"]]
    assert names == ["偏覆盖", "中性", "偏冷门"]
    lams = [d["λ"] for d in p["情景档位"]]
    assert len(set(lams)) == 3 and all(0.0 < x < 1.0 for x in lams)
    for name in names:
        q = ls.load_params(name)
        assert q["档位名"] == name and 0.0 < q["λ"] < 1.0
