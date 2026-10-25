# -*- coding: utf-8 -*-
"""大乐透：排除早期异常段后的稳健性复核。

背景
----
边际均匀性检验（_stage0_uniformity_diag）显示大乐透红球 χ²=90.33(df=34) 爆表，
但**分段后只集中在两个早期规则段**：

    dlt-2007-05 (367 期) χ²=117.50  p<0.0001
    dlt-2009-10 (698 期) χ²=133.87  p<0.0001
    dlt-2014-05 (735 期) χ²= 25.87  p=0.84
    dlt-2019-02 (1031期) χ²= 39.81  p=0.23
    dlt-2026-02 (107 期) χ²= 25.20  p=0.86

且早期两段之间 ρ=+0.857（同向），与后三段几乎无关（-0.31~+0.21）。
数据完整性无损（每期 5 个红球、无越界、无期内重复）。

⇒ 偏离是**早期段的既有状态**，后期不复现。它既可能是早期数据源/规则口径问题，
   也可能是真实的早期偏离 —— 无论哪种，都**不能**拿来做任何推论（项目红线）；
   但它会污染「全历史实测」，故本文件用**后三段（2014 起）**重算一遍。

注意：期数从 2938 降到 1873，SD 与实测Δ 都要重测，n* 的可判门限也随之收紧。
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
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool

import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "_s0_exact2", r"E:\707\scripts\oneoff\_stage0_exact2.py")
_mod = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_mod)
exact_union_prob = _mod.exact_union_prob

LOT = "大乐透"
POOL = 8000
CAP = 3
M = 100_000
TARGET = {"kind": "payout", "min_payout": 10.0}
Z_A, Z_B = 1.959964, 0.841621
CLEAN_VERS = {"dlt-2014-05", "dlt-2019-02", "dlt-2026-02"}

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


def _hit(tks, seqs, gate, nred, ninner):
    A = _mat(tks, "红球", nred)
    B = _mat(tks, "蓝球", ninner)
    n = len(seqs)
    hit = np.zeros(n, dtype=bool)
    for p, rec in enumerate(seqs):
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
    seq_all = load_test_recs(LOT)
    iss = seq_all[-1].期号
    r = rules_for(LOT, iss)
    nred = r.outer_range[1]
    m_in = max(jj for (_h, jj) in r.grade_table)
    n_in = _mod.inner_pool_size(r)
    res = TT.resolve_conditions(LOT, TARGET, iss)
    grades = set(res["grades"])
    hm = int(r.outer_width)
    gate = np.zeros((hm + 1, n_in + 1), dtype=bool)
    for (hh, jj), g in r.grade_table.items():
        if hh <= hm and jj <= n_in and g in grades:
            gate[hh, jj] = True

    clean = [rec for rec in seq_all
             if rules_for(LOT, rec.期号).rule_version in CLEAN_VERS]
    print(f"{LOT}：全历史 {len(seq_all)} 期 → 干净子样本 {len(clean)} 期"
          f"（仅 {'/'.join(sorted(CLEAN_VERS))}）", flush=True)
    print(flush=True)

    pool = build_pool(LOT, POOL, seed=20261024)
    A_all = _mat(pool, "红球", nred)
    B_all = _mat(pool, "蓝球", n_in)
    OV = (A_all.astype(np.int16) @ A_all.astype(np.int16).T).astype(np.int8)

    rows = []
    for K in (200, 300, 400):
        tks_r = control_tickets(LOT, pool, K, seed=0, cap=CAP)
        rng = np.random.default_rng(20261024)
        Rd, Bd = _sample_draws(rng, nred, hm, n_in, m_in, M)
        masks = _masks(A_all, B_all, Rd, Bd, gate)
        idx, rej = mc_greedy(masks, OV, K, CAP)
        tks_g = [pool[i] for i in idx]
        del masks, Rd, Bd
        if len(tks_g) != K:
            print(f"[K={K}] 出票不足，跳过", flush=True)
            continue

        ex_r = exact_union_prob(LOT, tks_r, TARGET, iss, chunk=1024)
        ex_g = exact_union_prob(LOT, tks_g, TARGET, iss, chunk=1024)
        dex = ex_g - ex_r

        out = {"K": K, "预算元": K * 2, "随机_精确P": ex_r, "覆盖_精确P": ex_g,
               "精确Δ": dex, "cap拒绝": rej}
        for tag, sq in (("全历史", seq_all), ("干净段", clean)):
            hr = _hit(tks_r, sq, gate, nred, n_in)
            hg = _hit(tks_g, sq, gate, nred, n_in)
            d = hg.astype(float) - hr.astype(float)
            n = len(sq)
            sd = float(d.std(ddof=1))
            se = sd / math.sqrt(n)
            dm = float(d.mean())
            out[f"{tag}_期数"] = n
            out[f"{tag}_随机实测P"] = float(hr.mean())
            out[f"{tag}_实测Δ"] = dm
            out[f"{tag}_SE"] = se
            out[f"{tag}_配对SD"] = sd
            out[f"{tag}_实测−精确"] = dm - dex
            out[f"{tag}_z实测"] = dm / se if se > 0 else 0.0
            out[f"{tag}_n*_精确"] = (((Z_A + Z_B) * sd / dex) ** 2
                                     if abs(dex) > 1e-15 else float("inf"))
            out[f"{tag}_n*_实测"] = (((Z_A + Z_B) * sd / dm) ** 2
                                     if abs(dm) > 1e-15 else float("inf"))
        n_clean = out["干净段_期数"]
        out["干净段_判决"] = (
            "★可判" if out["干净段_n*_精确"] <= n_clean else
            ("边缘" if out["干净段_n*_精确"] <= 3 * n_clean else "不可判"))
        out["干净段_判决_实测口径"] = (
            "★可判" if math.isfinite(out["干净段_n*_实测"]) and
            out["干净段_n*_实测"] <= n_clean else
            ("边缘" if math.isfinite(out["干净段_n*_实测"]) and
             out["干净段_n*_实测"] <= 3 * n_clean else "不可判"))
        rows.append(out)
        print(f"[K={K}] 随机精确 {ex_r:.6f} → 覆盖精确 {ex_g:.6f} "
              f"(Δ {dex*100:+.3f}pp)", flush=True)
        for tag in ("全历史", "干净段"):
            print(f"   {tag} {out[f'{tag}_期数']} 期: 随机实测 "
                  f"{out[f'{tag}_随机实测P']:.4f} | 实测Δ "
                  f"{out[f'{tag}_实测Δ']*100:+.3f}pp "
                  f"(SE {out[f'{tag}_SE']*100:.3f}pp) | 实测−精确 "
                  f"{out[f'{tag}_实测−精确']*100:+.3f}pp | "
                  f"n*(精确Δ) {out[f'{tag}_n*_精确']:.0f} | "
                  f"n*(实测Δ) {out[f'{tag}_n*_实测']:.0f}", flush=True)
        print(f"   ⇒ 干净段判决（效应量用精确Δ）{out['干净段_判决']}；"
              f"（用实测Δ）{out['干净段_判决_实测口径']}", flush=True)
        print(flush=True)

    print("=" * 132)
    print(f"{'K':>5}{'预算':>7}{'精确Δ':>10}{'干净期数':>9}{'干净实测Δ':>11}"
          f"{'SE':>9}{'实测−精确':>11}{'SD':>7}"
          f"{'n*精确':>9}{'n*实测':>9}{'判决':>8}{'判决(实测)':>10}")
    print("-" * 132)
    for r_ in rows:
        print(f"{r_['K']:>5}{r_['预算元']:>7}{r_['精确Δ']*100:>+9.3f}pp"
              f"{r_['干净段_期数']:>9}{r_['干净段_实测Δ']*100:>+10.3f}pp"
              f"{r_['干净段_SE']*100:>8.3f}pp"
              f"{r_['干净段_实测−精确']*100:>+10.3f}pp"
              f"{r_['干净段_配对SD']:>7.3f}"
              f"{r_['干净段_n*_精确']:>9.0f}{r_['干净段_n*_实测']:>9.0f}"
              f"{r_['干净段_判决']:>9}{r_['干净段_判决_实测口径']:>11}")
    print("=" * 132)

    outp = r"E:\707\output\stage0_dlt_clean.json"
    os.makedirs(os.path.dirname(outp), exist_ok=True)
    with open(outp, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {outp}")


if __name__ == "__main__":
    main()
