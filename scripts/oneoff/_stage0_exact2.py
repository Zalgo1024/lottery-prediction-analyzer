# -*- coding: utf-8 -*-
"""Stage 0：精确 P(并集) 的**无近似**枚举器 + 三方对账。

为什么需要它
------------
`exact_portfolio_coverage` 走 `ev.profit.evaluate.exact_metrics` 的 `P_达标`，
后者在「命中数桶」内用 `1-(1-p)^H` —— 即假设同桶各票的达标事件**独立**。
但内层槽位极小（双色球蓝球 16 选 1、大乐透 12 选 2），同桶票的内层命中是
**互斥/负相关**的：开奖蓝球只有 1 个，H 张票里至多 1 张能拿到 j=1。
独立近似因此会**低估** P(至少一注达标)，且票的蓝球越分散低估越多 ——
这正好会让「覆盖臂」被低估得比「随机臂」更多，制造虚假的正向 Δ。

本文件做的事
------------
1. `exact_union_prob`：外层**全枚举** × 内层**全枚举**，位掩码求并，无任何独立性假设。
2. 三方对账：新枚举器 vs `exact_portfolio_coverage` vs 均匀 MC 回放（独立真值）。
3. 量化「独立近似」的偏差方向与幅度，判断它是否大到能动摇可判格结论。
"""
from __future__ import annotations

import math
import os
import sys
import time
from itertools import combinations

import numpy as np

sys.path.insert(0, r"E:\707")

import ev.tier_target as TT
from ev.profit.rules import rules_for
from ev.profit.outcomes import outer_encoding, _encode_outer, tickets_to_outer
from ev.tier_coverage import select_tier_coverage, exact_portfolio_coverage
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool


# ---------------------------------------------------------------- 位掩码工具
def inner_pool_size(r) -> int:
    """内层**池大小**（双色球 16、大乐透 12）。

    ⚠️ `r.inner_size` 是内层**结局总数**（双色球 C(16,1)=16、大乐透 C(12,2)=66），
       不是池大小。双色球两者恰好相等（16 选 1），大乐透不等（66 vs 12）——
       直接拿 `inner_size` 当池大小会枚举出 C(66,2)=2145 个伪内层结局。
       这里用 C(n, m) == inner_size 反解 n。
    """
    import math as _m
    m_in = max(jj for (_h, jj) in r.grade_table)
    total = int(r.inner_size)
    for n in range(m_in, 64):
        if _m.comb(n, m_in) == total:
            return n
    raise ValueError(f"无法从 inner_size={total}、m={m_in} 反解内层池大小")


def _popcount_u64(x: np.ndarray) -> np.ndarray:
    """uint64 数组的 popcount（不依赖 numpy 版本）。"""
    b = np.ascontiguousarray(x).view(np.uint8).reshape(-1, 8)
    return np.unpackbits(b, axis=1).sum(axis=1, dtype=np.int64)


# ---------------------------------------------------------------- 精确枚举器
def exact_union_prob(lottery: str, tickets, target, issue=None,
                     *, chunk: int = 256, verbose: bool = False) -> float:
    """整组票「至少一注达标」的**精确**概率（外层均匀 × 内层均匀，无独立性近似）。

    做法：
      外层结局 ρ 全枚举（双色球 C(33,6)=1,107,568；大乐透 C(35,5)=324,632）；
      内层结局 B 全枚举（双色球 C(16,1)=16；大乐透 C(12,2)=66）；
      对每张票 i、每个 h 预计算「哪些内层结局 B 使 (h, |B∩b_i|) 达标」的位掩码；
      对每个 ρ 取 K 张票的掩码按位或 ⇒ 达标的内层结局数 ⇒ 除以 N_in。
    """
    r = rules_for(lottery, issue)
    res = TT.resolve_conditions(lottery, target, issue)
    grades = set(res["grades"])

    w_out = int(r.outer_width)
    # 开奖抽几个内层号 = grade_table 的 j 上限（双色球 1，大乐透 2）
    m_in = max(jj for (_h, jj) in r.grade_table)
    n_in = inner_pool_size(r)             # 内层池大小（16 / 12），不是 inner_size
    K = len(tickets)

    inners = np.array(list(combinations(range(n_in), m_in)), dtype=np.int8)  # (N_in, m_in)
    N_in = inners.shape[0]
    W = (N_in + 63) // 64

    gate = np.zeros((w_out + 1, m_in + 1), dtype=bool)
    for (hh, jj), gname in r.grade_table.items():
        if hh <= w_out and jj <= m_in and gname in grades:
            gate[hh, jj] = True

    # ---- 每张票的内层命中数 j_i(t)  (K, N_in)
    blue_sets = np.zeros((K, n_in), dtype=np.int8)
    for i, t in enumerate(tickets):
        v = t.get("蓝球")
        vals = list(v) if isinstance(v, (list, tuple)) else [v]
        for x in vals:
            if x is not None:
                blue_sets[i, int(x) - 1] = 1
    IM = np.zeros((n_in, N_in), dtype=np.int16)          # (内层号, 内层结局)
    IM[inners.ravel(), np.repeat(np.arange(N_in), m_in)] = 1
    j_mat = blue_sets @ IM                               # (K, N_in)

    # ---- mask[h, i] : (w_out+1, K, W) uint64
    mask = np.zeros((w_out + 1, K, W), dtype=np.uint64)
    for h in range(w_out + 1):
        ok = gate[h][j_mat]                              # (K, N_in) bool
        for i in range(K):
            bits = np.flatnonzero(ok[i])
            for t in bits:
                mask[h, i, t >> 6] |= np.uint64(1 << (t & 63))

    # ---- 外层枚举
    E_all = outer_encoding(lottery)                      # (M_out, outer_size) uint8
    r_enc = rules_for(lottery, issue)
    E_tk = _encode_outer(r_enc, tickets_to_outer(lottery, tickets)).T.copy()  # (d, K)
    M_out = E_all.shape[0]

    idx = np.arange(K, dtype=np.int64)[None, :]
    total = np.zeros(M_out, dtype=np.int64)
    t0 = time.time()
    for s in range(0, M_out, chunk):
        e = min(s + chunk, M_out)
        hits = (E_all[s:e].astype(np.float32) @ E_tk.astype(np.float32)).astype(np.int64)
        sel = mask[hits, idx]                            # (c, K, W)
        orred = np.bitwise_or.reduce(sel, axis=1)        # (c, W)
        cnt = np.zeros(orred.shape[0], dtype=np.int64)
        for w in range(W):
            cnt += _popcount_u64(orred[:, w])
        total[s:e] = cnt
        if verbose and s % (chunk * 200) == 0:
            print(f"     {s}/{M_out}  {time.time()-t0:.0f}s", flush=True)

    return float(total.mean()) / N_in


# ---------------------------------------------------------------- MC 均匀回放
def mc_replay(lottery: str, tickets, target, issue, m: int, seed: int = 7):
    """均匀开奖 MC 回放（独立真值，无任何解析近似）。"""
    rng = np.random.default_rng(seed)
    r = rules_for(lottery, issue)
    res = TT.resolve_conditions(lottery, target, issue)
    grades = set(res["grades"])
    w_out = int(r.outer_width)
    n_out = int(r.outer_range[1])
    m_in = max(jj for (_h, jj) in r.grade_table)
    n_in = inner_pool_size(r)

    gate = np.zeros((w_out + 1, m_in + 1), dtype=bool)
    for (hh, jj), gname in r.grade_table.items():
        if hh <= w_out and jj <= m_in and gname in grades:
            gate[hh, jj] = True

    K = len(tickets)
    A = np.zeros((K, n_out), dtype=np.int8)
    Bm = np.zeros((K, n_in), dtype=np.int8)
    for i, t in enumerate(tickets):
        for x in t.get("红球", ()):
            A[i, int(x) - 1] = 1
        v = t.get("蓝球")
        for x in (list(v) if isinstance(v, (list, tuple)) else [v]):
            if x is not None:
                Bm[i, int(x) - 1] = 1

    hit = 0
    n_hits = np.zeros(m, dtype=np.int32)
    blk = 20000
    done = 0
    while done < m:
        c = min(blk, m - done)
        rnd = rng.random((c, n_out))
        pick = np.argpartition(rnd, w_out - 1, axis=1)[:, :w_out]
        Rm = np.zeros((c, n_out), dtype=np.int8)
        np.put_along_axis(Rm, pick, 1, axis=1)
        rnd2 = rng.random((c, n_in))
        pick2 = np.argpartition(rnd2, m_in - 1, axis=1)[:, :m_in]
        Im = np.zeros((c, n_in), dtype=np.int8)
        np.put_along_axis(Im, pick2, 1, axis=1)
        h = (A.astype(np.int16) @ Rm.T.astype(np.int16))   # (K, c)
        j = (Bm.astype(np.int16) @ Im.T.astype(np.int16))
        ok = gate[h, j]
        n_hits[done:done + c] = ok.sum(axis=0)
        hit += int(ok.any(axis=0).sum())
        done += c
    return hit / m, float(n_hits.mean())


def legacy_portfolio_coverage(lottery, tickets, target=None, issue=None) -> float:
    """修复**前**的口径（2026-10-24 之前）：走 `exact_metrics(min_grade=...).P_达标`，
    后者在命中数桶内用 `1-(1-p)^H`（同桶票独立假设）。内联在此以便随时复现偏差。"""
    from ev.profit import evaluate as EV
    from ev.profit import payouts as P

    res = TT.resolve_conditions(lottery, target, issue)
    grades = res["grades"]
    order = [g for g in grades if g in TT._GRADE_ORDER]
    if not order:
        raise ValueError(f"{lottery} 目标 {res['label']} 不含「X等」")
    worst = max(order, key=TT._GRADE_ORDER.index)
    ctx = P.build_ctx(lottery, issue)
    m = EV.exact_metrics(lottery, list(tickets), ctx, kappa=0.0,
                         want_dist=False, min_grade=worst)
    return float(m["P_达标"])


# ---------------------------------------------------------------- 主流程
POOL = 8000
CAP = 3
M_MC = 300_000

CELLS = [
    ("双色球", 200, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("双色球", 360, {"kind": "grade", "grade": "四等"}, "≥四等"),
    ("大乐透", 200, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("大乐透", 400, {"kind": "grade", "grade": "四等"}, "≥四等"),
]


def main() -> None:
    pools = {lot: build_pool(lot, POOL, seed=20261024) for lot in ("双色球", "大乐透")}
    print(f"池 {POOL}｜cap={CAP}｜MC 回放 {M_MC:,} 期（均匀开奖，独立真值）")
    print(flush=True)

    rows = []
    for lot, K, target, label in CELLS:
        iss = 2026100
        tks_c, _ = select_tier_coverage(lot, pools[lot], K, target, iss, cap=CAP, seed=0)
        tks_r = control_tickets(lot, pools[lot], K, seed=0, cap=CAP)
        print(f"[{lot} K={K} {label}]", flush=True)

        vals = {}
        for name, tks in (("随机", tks_r), ("覆盖", tks_c)):
            t0 = time.time()
            p_new = exact_union_prob(lot, tks, target, iss)
            t_new = time.time() - t0
            p_old = exact_portfolio_coverage(lot, tks, target, iss)
            p_leg = legacy_portfolio_coverage(lot, tks, target, iss)
            t1 = time.time()
            f_mc, nh = mc_replay(lot, tks, target, iss, M_MC, seed=11)
            t_mc = time.time() - t1
            se = math.sqrt(max(f_mc * (1 - f_mc), 1e-12) / M_MC)
            vals[name] = dict(新枚举=p_new, 旧近似=p_leg, MC=f_mc, SE=se,
                              新耗时=t_new, MC耗时=t_mc, 达标注数=nh)
            print(f"   {name}: 新枚举 {p_new:.6f} ({t_new:.0f}s) | "
                  f"旧口径 {p_leg:.6f} | MC {f_mc:.6f} ±{se:.6f} ({t_mc:.0f}s)",
                  flush=True)
            print(f"        新−MC = {(p_new-f_mc)*100:+.4f}pp "
                  f"({(p_new-f_mc)/se:+.2f}σ) | 旧−MC = {(p_leg-f_mc)*100:+.4f}pp "
                  f"({(p_leg-f_mc)/se:+.2f}σ)", flush=True)

        d_new = vals["覆盖"]["新枚举"] - vals["随机"]["新枚举"]
        d_old = vals["覆盖"]["旧近似"] - vals["随机"]["旧近似"]
        d_mc = vals["覆盖"]["MC"] - vals["随机"]["MC"]
        se_d = math.sqrt(vals["覆盖"]["SE"] ** 2 + vals["随机"]["SE"] ** 2)
        rows.append(dict(彩种=lot, K=K, 主终点=label,
                         新Δ=d_new, 旧Δ=d_old, MCΔ=d_mc, MC_SE=se_d,
                         旧减MC=d_old - d_mc, 新减MC=d_new - d_mc))
        print(f"   Δ: 新枚举 {d_new*100:+.3f}pp | 旧近似 {d_old*100:+.3f}pp | "
              f"MC {d_mc*100:+.3f}pp ±{se_d*100:.3f}pp", flush=True)
        print(f"   偏差: 旧近似−MC {(d_old-d_mc)*100:+.3f}pp | "
              f"新枚举−MC {(d_new-d_mc)*100:+.3f}pp", flush=True)
        print(flush=True)

    print("=" * 118)
    print(f"{'彩种':<6}{'K':>5} {'主终点':<8}{'新枚举Δ':>11}{'旧近似Δ':>11}{'MCΔ':>11}"
          f"{'MC_SE':>9}{'旧−MC':>10}{'新−MC':>10}")
    print("-" * 118)
    for r in rows:
        print(f"{r['彩种']:<6}{r['K']:>5} {r['主终点']:<9}"
              f"{r['新Δ']*100:>+10.3f}pp{r['旧Δ']*100:>+10.3f}pp"
              f"{r['MCΔ']*100:>+10.3f}pp{r['MC_SE']*100:>8.3f}pp"
              f"{r['旧减MC']*100:>+9.3f}pp{r['新减MC']*100:>+9.3f}pp")
    print("=" * 118)
    print("判读：新枚举 vs MC 若在 ±3σ 内 ⇒ 新枚举无偏；"
          "旧近似−MC 的符号与幅度 = 独立性近似的偏差。")

    out = r"E:\707\output\stage0_exact2.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    import json
    with open(out, "w", encoding="utf-8") as f:
        import json as _j
        _j.dump(rows, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
