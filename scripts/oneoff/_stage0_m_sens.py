# -*- coding: utf-8 -*-
"""M 敏感性 + MC 目标保真对账。

两件事：
1. MC 采样数 M 是不是瓶颈？若 M 变大 ⇒ 精确Δ 变高，说明当前是「排序噪声受限」，
   继续加 M 就能把效应推大（纯工程量，不需要更多期次）。
2. MC 估计的 f 与精确 f 差多少？若接近，说明 MC 目标与真目标一致，
   「贪心在优化真目标」这句话才站得住。
"""
from __future__ import annotations

import heapq
import json
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"E:\707")

import ev.tier_target as TT
from ev.profit.rules import rules_for
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import select_tier_coverage, exact_portfolio_coverage
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool

POOL = 8000
CAP = 3
TARGET = {"kind": "payout", "min_payout": 10.0}
CELLS = [("双色球", 200), ("双色球", 300), ("大乐透", 300)]
MS = (100_000, 250_000)

POP = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)


def _mat(tks, key, width):
    Mx = np.zeros((len(tks), width), dtype=np.int8)
    for i, t in enumerate(tks):
        v = t.get(key)
        vals = list(v) if isinstance(v, (list, tuple)) else [v]
        for x in vals:
            if x is not None:
                Mx[i, int(x) - 1] = 1
    return Mx


def _sample_draws(rng, n_out, k_out, n_in, k_in, m):
    oi = np.argsort(rng.random((m, n_out)), axis=1)[:, :k_out]
    ii = np.argsort(rng.random((m, n_in)), axis=1)[:, :k_in]
    R = np.zeros((m, n_out), dtype=np.float32)
    B = np.zeros((m, n_in), dtype=np.float32)
    np.put_along_axis(R, oi, 1.0, axis=1)
    np.put_along_axis(B, ii, 1.0, axis=1)
    return R, B


def _masks(A, Bm, R, B, gate, chunk):
    P, m = A.shape[0], R.shape[0]
    out = np.zeros((P, (m + 7) // 8), dtype=np.uint8)
    Rt = np.ascontiguousarray(R.T.astype(np.float32))
    Bt = np.ascontiguousarray(B.T.astype(np.float32))
    Af, Bf = A.astype(np.float32), Bm.astype(np.float32)
    for s in range(0, P, chunk):
        e = min(P, s + chunk)
        h = (Af[s:e] @ Rt).astype(np.int16)
        j = (Bf[s:e] @ Bt).astype(np.int16)
        out[s:e] = np.packbits(gate[h, j], axis=1)
    return out


def mc_greedy(masks, OV, K, cap):
    P, nbytes = masks.shape
    cov = np.zeros(nbytes, dtype=np.uint8)
    cov_inv = np.full(nbytes, 0xFF, dtype=np.uint8)
    sel: list[int] = []
    rej = 0
    heap = [(-float(POP[masks[i] & cov_inv].sum(dtype=np.int64)), i, -1)
            for i in range(P)]
    heapq.heapify(heap)
    step = 0
    while len(sel) < K and heap:
        _, i, stamp = heapq.heappop(heap)
        if i in sel:
            continue
        if stamp != step:
            g = float(POP[masks[i] & cov_inv].sum(dtype=np.int64))
            heapq.heappush(heap, (-g, i, step))
            continue
        if sel and int(OV[i, sel].max()) > cap:
            rej += 1
            continue
        sel.append(i)
        cov |= masks[i]
        cov_inv = ~cov
        step += 1
    return sel, rej, cov


def main() -> None:
    rows = []
    for lot, K in CELLS:
        seq = load_test_recs(lot)
        iss = seq[-1].期号
        r = rules_for(lot, iss)
        nred = r.outer_range[1]
        n_in = 12 if lot == "大乐透" else int(r.inner_size)
        res = TT.resolve_conditions(lot, TARGET, iss)
        grades = res["grades"]
        hm = int(r.outer_width)
        gate = np.zeros((hm + 1, n_in + 1), dtype=bool)
        for (hh, jj), g in r.grade_table.items():
            if hh <= hm and jj <= n_in and g in grades:
                gate[hh, jj] = True

        pool = build_pool(lot, POOL, seed=20261024)
        A = _mat(pool, "红球", nred)
        B = _mat(pool, "蓝球", n_in)
        OV = (A.astype(np.int16) @ A.astype(np.int16).T).astype(np.int8)
        tks_r = control_tickets(lot, pool, K, seed=0, cap=CAP)
        ex_r = exact_portfolio_coverage(lot, tks_r, TARGET, iss)
        print(f"[{lot} K={K}] 随机 精确P {ex_r:.5f}", flush=True)

        for M in MS:
            t0 = time.time()
            rng = np.random.default_rng(20261024)
            Rd, Bd = _sample_draws(rng, nred, int(r.outer_width), n_in,
                                   2 if lot == "大乐透" else 1, M)
            chunk = 400 if M <= 100_000 else 150
            masks = _masks(A, B, Rd, Bd, gate, chunk)
            idx, rej, cov = mc_greedy(masks, OV, K, CAP)
            dt = time.time() - t0
            tks_g = [pool[i] for i in idx]
            f_mc = float(POP[cov].sum(dtype=np.int64)) / M
            ex_g = exact_portfolio_coverage(lot, tks_g, TARGET, iss)
            row = {"彩种": lot, "K": K, "M": M,
                   "MC估计f": f_mc, "精确P": ex_g, "MC−精确": f_mc - ex_g,
                   "精确Δ": ex_g - ex_r, "cap拒绝": rej, "耗时秒": dt}
            rows.append(row)
            print(f"   M={M:>7}  精确P {ex_g:.5f} (Δ {100*(ex_g-ex_r):+.3f}pp) | "
                  f"MC估计 {f_mc:.5f} (MC−精确 {100*(f_mc-ex_g):+.3f}pp) | "
                  f"拒绝 {rej} | {dt:.0f}s", flush=True)
            del masks, Rd, Bd, cov
        print(flush=True)

    print("=" * 110)
    print(f"{'彩种':<6}{'K':>5}{'M':>9}{'精确Δ':>10}{'MC估计f':>10}{'精确P':>10}"
          f"{'MC−精确':>10}{'耗时':>7}")
    print("-" * 110)
    for r in rows:
        print(f"{r['彩种']:<6}{r['K']:>5}{r['M']:>9}"
              f"{r['精确Δ']*100:>+9.3f}pp{r['MC估计f']:>10.5f}{r['精确P']:>10.5f}"
              f"{r['MC−精确']*100:>+9.3f}pp{r['耗时秒']:>6.0f}s")
    print("=" * 110)

    out = r"E:\707\output\stage0_m_sens.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"行": rows}, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
