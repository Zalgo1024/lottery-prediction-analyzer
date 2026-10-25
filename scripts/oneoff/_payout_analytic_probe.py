# -*- coding: utf-8 -*-
"""独立核对：金额网格的**解析期望**是否与官方返奖率量级相容。

为什么需要它：`_arena_money_check.py` 只证明 `money_series` == `evaluate_period`，
两者共用同一套常量/规则表 ⇒ 一致性通过但正确性未被检验（Stage 0b 的教训）。

这里走一条完全独立的路径：
    h ~ Hypergeom(外层总数, 外层球数, 投注数)
    j ~ Hypergeom(内层总数, 内层球数, 投注数)
    E[单注实得] = Σ_h Σ_j P(h)P(j) · grid̄[h, j]
grid̄ 用全部期号的网格**平均**（浮动奖就用实测均值，固定奖就是定值）。
再与「官方返奖率 × 单注成本」比（双色球/大乐透均为销售额的 ~51%）。
"""
from __future__ import annotations

import sys
from math import comb

sys.path.insert(0, r"E:\707")

import numpy as np  # noqa: E402

from ev.arena import payout as PX             # noqa: E402
from ev.profit.rules import rules_for         # noqa: E402
from ev.tier_backtest import load_test_recs    # noqa: E402
from ev.tier_coverage import _inner_pool_size  # noqa: E402

LOTS = ["双色球", "大乐透"]


def hyper(n_pop: int, n_succ: int, n_draw: int) -> np.ndarray:
    tot = comb(n_pop, n_draw)
    return np.array([comb(n_succ, k) * comb(n_pop - n_succ, n_draw - k) / tot
                     for k in range(n_succ + 1)])


def probe(lot: str, limit: int = 400, rule_mode: str = "periodic") -> None:
    recs = load_test_recs(lot)
    # ★ `current` 口径的判级版本必须取**全序列最后一期**，不是被截断样本的最后一期
    #   （否则 400 期样本会把「最新规则」退化成 2009 规则，反事实口径直接失效）。
    target = recs[-1].期号
    recs = recs[:limit] if limit else recs
    r = rules_for(lot, target)
    w_out = int(r.outer_width)
    m_in = max(jj for (_h, jj) in r.grade_table)
    n_in = _inner_pool_size(r, m_in)
    n_out = int(r.outer_range[1])
    k_out = max(hh for (hh, _j) in r.grade_table)
    k_in = m_in

    from data import prize_amounts as PA
    PA.preload(lot, [x.期号 for x in recs])

    grids = []
    per_grade = {}
    for rec in recs:
        ri = target if rule_mode == "current" else rec.期号
        g, meta = PX.amount_grid(lot, rec.期号, rule_issue=ri)
        if g.shape != (w_out + 1, m_in + 1):
            continue
        grids.append(g)
        for gr, a in meta["奖级金额"].items():
            per_grade.setdefault(gr, []).append(float(a))
    G = np.mean(np.stack(grids), axis=0)

    # 固定奖 / 浮动奖分别求和（按**判级版本**的 kind）
    fixed_g, float_g = np.zeros_like(G), np.zeros_like(G)
    names = PX.amount_grid(lot, recs[-1].期号, rule_issue=target)[1]["奖级网格"]
    for hh in range(G.shape[0]):
        for jj in range(G.shape[1]):
            gname = names[hh][jj]
            if not gname:
                continue
            rule = r.grades.get(gname)
            (fixed_g if (rule and rule.kind == "fixed") else float_g)[hh, jj] = G[hh, jj]

    ph = hyper(n_out, k_out, k_out)
    pj = hyper(n_in, k_in, k_in)
    exp_pay = float(ph @ G @ pj)
    exp_fixed = float(ph @ fixed_g @ pj)
    exp_float = float(ph @ float_g @ pj)

    print("=" * 96)
    print(f"{lot}  口径 {rule_mode}  样本 {len(grids)} 期（前 {limit} 期）  判级版本 {r.rule_version}")
    print(f"  外层 {k_out}/{n_out}  内层 {k_in}/{n_in}")
    print(f"  期望单注实得 = {exp_pay:.4f} 元 / 2.00 元  ⇒ 返奖率 {exp_pay / 2.0:.2%}")
    print(f"    ├ 固定奖部分 {exp_fixed:.4f} 元 ⇒ {exp_fixed / 2.0:.2%}")
    print(f"    └ 浮动奖部分 {exp_float:.4f} 元 ⇒ {exp_float / 2.0:.2%}")
    print(f"  参考：官方返奖率 ~51%；双色球固定奖口径既有值 24.32%，大乐透 27.75%")
    print("  各奖级（均值 / 官方常见值）:")
    for gr, vs in sorted(per_grade.items(), key=lambda kv: -np.mean(kv[1])):
        a = np.array(vs)
        print(f"    {gr:<6} 均值 {a.mean():>14,.1f}  中位 {np.median(a):>12,.0f}"
              f"  最小 {a.min():>10,.0f}  最大 {a.max():>14,.0f}  n={len(a)}")
    print("  网格（行=外层命中 h，列=内层命中 j）:")
    for i in range(G.shape[0]):
        print("    h=%d " % i + " ".join(f"{v:>11,.1f}" for v in G[i]))
    top = np.sort(G.ravel())[-3:]
    print(f"  网格前三大值: {[f'{v:,.0f}' for v in top]}")
    print(f"  概率加权前三格贡献: ")
    flat = []
    for hh in range(G.shape[0]):
        for jj in range(G.shape[1]):
            if hh < len(ph) and jj < len(pj):
                flat.append((ph[hh] * pj[jj] * G[hh, jj], hh, jj))
    flat.sort(reverse=True)
    for c, hh, jj in flat[:5]:
        print(f"    (h={hh}, j={jj}) 概率 {ph[hh]*pj[jj]:.3e} × 金额 {G[hh,jj]:,.0f}"
              f" = 贡献 {c:.4f} 元/注 ({c/2.0:.2%} 返奖率)")


if __name__ == "__main__":
    for lot in LOTS:
        for mode in ("periodic", "current"):
            probe(lot, rule_mode=mode)
