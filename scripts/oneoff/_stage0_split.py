# -*- coding: utf-8 -*-
"""分半检验：design4 里「实测Δ 系统性小于精确Δ」（8 格中 7 格）是噪声还是系统性？

为什么必须查
------------
偏差方向是**危险的**：若真实效应 < 精确值，用精确Δ 算 n* 会**低估**所需期数，
把「边缘」误读成「可判」。所以必须判断它是一次抽样噪声还是可复现的系统偏移。

方法
----
把历史开奖序列切成**前后两个不相交半段**，各自算实测Δ，与同一精确Δ 比较。
  · 两半同号  ⇒ 系统性（真实开奖分布 ≠ 均匀，或池与开奖序列的交互）
  · 两半异号  ⇒ 该偏移是这一批开奖序列的抽样噪声，不可复现

注意：这 8 个格**不是 8 次独立观测**——它们共用同一批开奖序列、同一个随机臂，
且覆盖臂票集随 K 嵌套。所以符号检验的名义 p 值不可用，分半才是有效判据。
"""
from __future__ import annotations

import heapq
import json
import math
import os
import sys
import time

import numpy as np

sys.path.insert(0, r"E:\707")

import ev.tier_target as TT
from ev.profit.rules import rules_for
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import select_tier_coverage
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "_s0_exact2", r"E:\707\scripts\oneoff\_stage0_exact2.py")
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
exact_union_prob = _mod.exact_union_prob

POOL = 8000
CAP = 3
M = 100_000
TARGET = {"kind": "payout", "min_payout": 10.0}
Z_A, Z_B = 1.959964, 0.841621

# 两个最优可判格 + 一个边缘格
CELLS = [("双色球", 100), ("双色球", 200), ("双色球", 300),
         ("大乐透", 200), ("大乐透", 300)]

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


def _masks(A, Bm, R, B, gate, chunk=400):
    P = A.shape[0]
    m = R.shape[0]
    out = np.zeros((P, (m + 7) // 8), dtype=np.uint8)
    Rt = np.ascontiguousarray(R.T.astype(np.float32))
    Bt = np.ascontiguousarray(B.T.astype(np.float32))
    Af = A.astype(np.float32)
    Bf = Bm.astype(np.float32)
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
    heap = []
    for i in range(P):
        heapq.heappush(heap, (-float(POP[masks[i] & cov_inv].sum(dtype=np.int64)), i, -1))
    step = 0
    while len(sel) < K and heap:
        negg, i, stamp = heapq.heappop(heap)
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
    return sel, rej


def _replay_range(tks, seq, lo, hi, gate, nred, ninner):
    A = _mat(tks, "红球", nred)
    B = _mat(tks, "蓝球", ninner)
    n = hi - lo
    hit = np.zeros(n, dtype=bool)
    for p in range(n):
        rec = seq[lo + p]
        rv = np.zeros(nred, dtype=np.int8)
        for x in rec.红球:
            rv[int(x) - 1] = 1
        bv = np.zeros(ninner, dtype=np.int8)
        vals = rec.蓝球
        vals = list(vals) if isinstance(vals, (list, tuple)) else [vals]
        for x in vals:
            bv[int(x) - 1] = 1
        hit[p] = gate[(A @ rv).astype(np.int16), (B @ bv).astype(np.int16)].any()
    return hit


def main() -> None:
    pools, seqs = {}, {}
    for lot in ("双色球", "大乐透"):
        pools[lot] = build_pool(lot, POOL, seed=20261024)
        seqs[lot] = load_test_recs(lot)

    rows = []
    for lot, K in CELLS:
        seq = seqs[lot]
        iss = seq[-1].期号
        r = rules_for(lot, iss)
        nred = r.outer_range[1]
        m_in = max(jj for (_h, jj) in r.grade_table)
        n_in = _mod.inner_pool_size(r)
        res = TT.resolve_conditions(lot, TARGET, iss)
        grades = set(res["grades"])

        hm = int(r.outer_width)
        gate = np.zeros((hm + 1, n_in + 1), dtype=bool)
        for (hh, jj), g in r.grade_table.items():
            if hh <= hm and jj <= n_in and g in grades:
                gate[hh, jj] = True

        n = len(seq)
        half = n // 2
        pool = pools[lot]
        A_all = _mat(pool, "红球", nred)
        B_all = _mat(pool, "蓝球", n_in)
        OV = (A_all.astype(np.int16) @ A_all.astype(np.int16).T).astype(np.int8)

        tks_r = control_tickets(lot, pool, K, seed=0, cap=CAP)
        rng = np.random.default_rng(20261024)
        Rd, Bd = _sample_draws(rng, nred, int(r.outer_width), n_in, m_in, M)
        masks = _masks(A_all, B_all, Rd, Bd, gate)
        idx, rej = mc_greedy(masks, OV, K, CAP)
        tks_g = [pool[i] for i in idx]
        del masks, Rd, Bd
        if len(tks_g) != K:
            print(f"[{lot} K={K}] 出票不足 {len(tks_g)}/{K}，跳过", flush=True)
            continue

        ex_r = exact_union_prob(lot, tks_r, TARGET, iss, chunk=1024)
        ex_g = exact_union_prob(lot, tks_g, TARGET, iss, chunk=1024)
        dex = ex_g - ex_r

        h_r = _replay_range(tks_r, seq, 0, n, gate, nred, n_in)
        h_g = _replay_range(tks_g, seq, 0, n, gate, nred, n_in)
        d_all = h_g.astype(float) - h_r.astype(float)
        sd_all = float(d_all.std(ddof=1))
        dm_all = float(d_all.mean())

        segs = []
        for tag, lo, hi in (("前半", 0, half), ("后半", half, n)):
            a = _replay_range(tks_r, seq, lo, hi, gate, nred, n_in)
            b = _replay_range(tks_g, seq, lo, hi, gate, nred, n_in)
            d = b.astype(float) - a.astype(float)
            nn = hi - lo
            segs.append(dict(段=tag, 期数=nn, Δ=float(d.mean()),
                             SE=float(d.std(ddof=1) / math.sqrt(nn)),
                             SD=float(d.std(ddof=1)),
                             随机=float(a.mean()), 覆盖=float(b.mean())))

        same = (segs[0]["Δ"] - dex) * (segs[1]["Δ"] - dex) > 0
        nstar = ((Z_A + Z_B) * sd_all / dex) ** 2 if abs(dex) > 1e-15 else float("inf")
        row = dict(彩种=lot, K=K, 预算元=K * 2, 期数=n,
                   随机_精确P=ex_r, 覆盖_精确P=ex_g, 精确Δ=dex,
                   全段实测Δ=dm_all, 全段SE=sd_all / math.sqrt(n), 配对SD=sd_all,
                   **{"n*": nstar},
                   cap拒绝=rej, 段=segs, 两半同号=bool(same))
        rows.append(row)

        print(f"[{lot} K={K}] 随机精确 {ex_r:.6f} → 覆盖精确 {ex_g:.6f} "
              f"(Δ {dex*100:+.3f}pp)", flush=True)
        print(f"   全段 {n} 期: 实测Δ {dm_all*100:+.3f}pp "
              f"(SE {sd_all/math.sqrt(n)*100:.3f}pp) | 实测−精确 "
              f"{(dm_all-dex)*100:+.3f}pp | n* {row['n*']:.0f}", flush=True)
        for s in segs:
            print(f"   {s['段']} {s['期数']} 期: 随机 {s['随机']:.4f} | "
                  f"覆盖 {s['覆盖']:.4f} | Δ {s['Δ']*100:+.3f}pp "
                  f"(SE {s['SE']*100:.3f}pp) | 实测−精确 "
                  f"{(s['Δ']-dex)*100:+.3f}pp "
                  f"[{(s['Δ']-dex)/s['SE']:+.2f}σ]", flush=True)
        print(f"   ⇒ 两半{'同号（提示系统性）' if same else '异号（提示抽样噪声）'}",
              flush=True)
        print(flush=True)

    print("=" * 128)
    print(f"{'彩种':<6}{'K':>5}{'精确Δ':>10}{'全段Δ':>10}{'前半Δ':>10}{'后半Δ':>10}"
          f"{'前半−精确':>11}{'后半−精确':>11}{'同号?':>7}")
    print("-" * 128)
    for r in rows:
        a, b = r["段"][0], r["段"][1]
        print(f"{r['彩种']:<6}{r['K']:>5}{r['精确Δ']*100:>+9.3f}pp"
              f"{r['全段实测Δ']*100:>+9.3f}pp{a['Δ']*100:>+9.3f}pp"
              f"{b['Δ']*100:>+9.3f}pp{(a['Δ']-r['精确Δ'])*100:>+10.3f}pp"
              f"{(b['Δ']-r['精确Δ'])*100:>+10.3f}pp"
              f"{'是' if r['两半同号'] else '否':>7}")
    print("=" * 128)
    print("判据：两半异号 ⇒ 该偏移是这一批开奖序列的抽样噪声，"
          "精确Δ 可用；两半同号 ⇒ 需进一步查真实开奖分布。")

    out = r"E:\707\output\stage0_split.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
