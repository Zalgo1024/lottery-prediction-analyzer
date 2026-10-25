# -*- coding: utf-8 -*-
"""tests/test_hybrid_select.py —— P5 联合目标选号（`select_joint`）的契约。

这里锁三件事，任何一件松了都会让这一层退化成「看起来新其实什么都没改」：

  1. **保真性**：`cov_source="proxy"` 必须与 `select_lowcrowd` **逐位按序一致**。
     ⇒ 新方法先把老路径原样复现一遍，才谈得上"新"的东西带来了差别。
     （比方案里写的 Jaccard ≥ 0.8 更强：同样的打分、同样的窗内顺序就该完全一样。）

  2. **λ 是真杠杆**：λ=0 与 λ=0.5 选出的票集必须真的不同（Jaccard < 0.9）。
     ⇒ 防止重蹈「按 −λ·z(crowd) 纯排序 ⇒ λ 是数学空操作」（ρ = −1.000000）那个坑。
     ★ 这条还盯着一个更隐蔽的坑：两条通道量纲差一个数量级以上时，
       直接线性加权会让 λ 在 [0.3,1] 区间整体失效 —— **λ=0.5 与 λ=1.0 完全同解**
       就是症状（实测过：未做窗口内 z 标准化时 Jaccard=1.0）。

  3. **输入对齐**：`u` 与 `pool` 长度不等必须**抛**，不许 broadcast 后静默错位。
"""
from __future__ import annotations

import inspect
import numpy as np
import pytest

from ev import coverage_backtest as cb
from ev import lowcrowd_select as ls

LOT = "双色球"
TARGET = {"kind": "payout", "min_payout": 10.0}
ISSUE = 26121
POOL_N = 3000
K = 100


def _pool_u(n: int = POOL_N, seed: int = 7):
    pool = cb.build_pool(LOT, n, seed=seed)
    u = np.random.default_rng(11).permutation(np.linspace(0.0, 1.0, len(pool)))
    return pool, u


def _key(t):
    return tuple(sorted(t["红球"])) + (int(t["蓝球"][0]),)


def _seq(tickets):
    return [_key(t) for t in tickets]


def _set(tickets):
    return set(_seq(tickets))


def _jaccard(a, b):
    A, B = _set(a), _set(b)
    return len(A & B) / len(A | B) if (A | B) else 1.0


def _joint(pool, u, **kw):
    return ls.select_joint(LOT, pool, K, u, target=TARGET, issue=ISSUE,
                           seed=0, m_fit=20_000, union_pool_cap=1500, **kw)


# ---------------------------------------------------------------- ① 保真性
def test_proxy_channel_reproduces_select_lowcrowd_bitwise():
    """★ 最强保真性：proxy 通道下，逐位**且按序**完全一致。"""
    pool, u = _pool_u()
    old, _ = ls.select_lowcrowd(LOT, pool, K, u, lam=0.5, gate_q=0.30, cap=3)
    new, _ = ls.select_joint(LOT, pool, K, u, lam=0.5, gate_q=0.30, cap=3,
                             cov_source="proxy")
    assert _seq(old) == _seq(new), "新路径没有复现老路径 ⇒ 不可用作新方法"


def test_proxy_fidelity_holds_across_lambda():
    """保真性不能只在 λ=0.5 成立 —— 端点也要成立（含 λ=0 / λ=1）。"""
    pool, u = _pool_u()
    for lam in (0.0, 0.25, 0.75, 1.0):
        old, _ = ls.select_lowcrowd(LOT, pool, K, u, lam=lam, gate_q=0.30, cap=3)
        new, _ = ls.select_joint(LOT, pool, K, u, lam=lam, gate_q=0.30, cap=3,
                                 cov_source="proxy")
        assert _seq(old) == _seq(new), f"λ={lam} 上保真性破裂"


# ---------------------------------------------------------------- ② λ 是真杠杆
def test_lambda_is_not_a_no_op_on_union_channel():
    pool, u = _pool_u()
    t0, _ = _joint(pool, u, lam=0.0, cov_source="union")
    t5, _ = _joint(pool, u, lam=0.5, cov_source="union")
    j = _jaccard(t0, t5)
    assert j < 0.9, f"λ=0 与 λ=0.5 选出几乎同一批票（Jaccard={j:.3f}）⇒ λ 是空操作"


def test_lambda_zero_beats_lambda_one_on_out_of_sample_union():
    """λ=0（纯覆盖）的**样本外并集覆盖率**必须 ≥ λ=1（纯冷门）。

    ★ 为什么不用 `覆盖.选中覆盖键`（通道键数）当单调性度量：那是 **Σ通道权重**
      式的上界，Stage 1 已经实测「优化上界 ≠ 优化 P(并集)」（union_greedy 比
      tier_coverage 高 3pp 量级）。用上界去测新算法，等于又犯一次同一个错。
      ⇒ 这里用**独立评估流**（seed + eval_seed_offset）上的真实覆盖率。
    """
    from ev.arena.design import (_POP8, outcome_gate, outcome_masks,
                                 sample_draws, union_rate)
    from ev.profit.rules import rules_for

    pool, u = _pool_u()
    gate, w_out, m_in, n_in = outcome_gate(LOT, TARGET, ISSUE)
    r = rules_for(LOT, ISSUE)
    rng = np.random.default_rng(int(10_000))          # 独立评估流，不复用拟合种子
    Re, Be = sample_draws(rng, int(r.outer_range[1]), int(r.outer_width),
                          n_in, m_in, 20_000)
    rates = {}
    for lam in (0.0, 1.0):
        tk, _ = _joint(pool, u, lam=lam, cov_source="union")
        m = outcome_masks(tk, LOT, Re, Be, gate)
        rates[lam] = union_rate(m, list(range(len(tk))))
    assert rates[0.0] >= rates[1.0] - 1e-9, \
        f"λ=0（纯覆盖）的样本外覆盖率 {rates[0.0]:.4f} < λ=1（纯冷门）{rates[1.0]:.4f}"


def test_lambda_endpoints_are_maximally_different():
    pool, u = _pool_u()
    t0, _ = _joint(pool, u, lam=0.0, cov_source="union")
    t1, _ = _joint(pool, u, lam=1.0, cov_source="union")
    assert _jaccard(t0, t1) < 0.5, "λ 两端应显著不同"


# ---------------------------------------------------------------- ③ 输入与参数校验
def test_u_length_mismatch_raises():
    pool, u = _pool_u()
    with pytest.raises(ValueError, match="必须逐元素对齐"):
        ls.select_joint(LOT, pool, K, u[:-7], lam=0.5, cov_source="proxy")


def test_unknown_cov_source_raises():
    pool, u = _pool_u()
    with pytest.raises(ValueError, match="未知 cov_source"):
        ls.select_joint(LOT, pool, K, u, cov_source="whatever")


def test_union_masks_shape_mismatch_raises():
    """masks 行数必须 = 候选数 r；否则 `masks[i]` 会错位到别的票上且**不报错**"""
    with pytest.raises(ValueError, match="候选数"):
        ls._joint_beam_pick(10, [[] for _ in range(10)],
                            np.zeros((10, 1), dtype=np.int32),
                            np.linspace(0, 1, 10), 3, 3, 0.5,
                            cov_source="union",
                            masks=np.zeros((7, 4), dtype=np.uint8))


def test_union_without_masks_raises():
    with pytest.raises(ValueError, match="必须给"):
        ls._joint_beam_pick(5, [], np.zeros((5, 1), dtype=np.int32),
                            np.linspace(0, 1, 5), 2, 3, 0.5,
                            cov_source="union")


def test_non_redblue_lottery_rejected_by_union_masks():
    """联合并集通道只对红蓝型实现；数字型必须明确报错，不许静默退化。"""
    pool, u = _pool_u(n=200)
    with pytest.raises(ValueError, match="红蓝型"):
        ls.joint_union_masks("排列3", pool[:5], TARGET, ISSUE, seed=0, m_fit=1000)


# ---------------------------------------------------------------- ④ 不接生产的边界
def test_select_joint_writes_nothing_and_keeps_old_selector_intact():
    """新增不得引入落盘（限本次新增的三个函数，不含模块内既有的读配置代码）。"""
    import inspect as ins
    new_src = "".join(inspect.getsource(f) for f in
                      (ls.select_joint, ls._joint_beam_pick, ls.joint_union_masks))
    for kw in ("save_pending", "record_pending", "write_json", "open("):
        assert kw not in new_src, f"新增函数里出现了疑似落盘入口：{kw}"
    # 老三个函数仍被既有的回归测试锁着，这里只确认它们还在且签名未被替换
    assert "u" in ins.signature(ls.select_lowcrowd).parameters
    assert "lam" in ins.signature(ls._beam_pick).parameters
    assert "t" in ins.signature(ls.keys_of).parameters
