# -*- coding: utf-8 -*-
"""路径 C 主结果：MC直接贪心 × K 网格 × 多 seed 的可判性表。

上一轮发现（_stage0_design）：
    双色球 K=200  代理贪心 精确Δ +0.241pp  →  MC直接贪心 +2.724pp
    双色球 K=300  代理贪心 精确Δ +0.329pp  →  MC直接贪心 +1.596pp

即「换设计」比「加预算」的影响大一个数量级。本脚本把它做成可判性格点表。

口径
----
- **效应量 = 精确Δ**（`exact_portfolio_coverage`，全分布解析值，无抽样噪声）。
  实测Δ 含 ±SE 的抽样误差，拿它算 n* 会系统性低估所需期数 —— 上一版 pilot
  就是在这里把「边缘」误读成「可判」。
- **方差 = 实测配对差 SD**（真实开奖，逐期按当期规则版本判级）。
- 对照臂（random / 代理贪心）固定 seed=0；MC贪心臂跑 3 个 MC seed，
  用来看「换一批采样开奖，设计质量掉多少」。
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
from ev.tier_coverage import select_tier_coverage, exact_portfolio_coverage
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool

POOL = 8000
CAP = 3
M = 100_000
MC_SEEDS = (0, 1, 2)
TARGET = {"kind": "payout", "min_payout": 10.0}
Z_A, Z_B = 1.959964, 0.841621

CELLS = [("双色球", 100), ("双色球", 200), ("双色球", 300), ("双色球", 400),
         ("大乐透", 100), ("大乐透", 200), ("大乐透", 300), ("大乐透", 400)]

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


def _replay(tks, seq, gate_of, nred, ninner):
    A = _mat(tks, "红球", nred)
    B = _mat(tks, "蓝球", ninner)
    n = len(seq)
    hit = np.zeros(n, dtype=bool)
    for p, rec in enumerate(seq):
        rv = np.zeros(nred, dtype=np.int8)
        for x in rec.红球:
            rv[int(x) - 1] = 1
        bv = np.zeros(ninner, dtype=np.int8)
        vals = rec.蓝球
        vals = list(vals) if isinstance(vals, (list, tuple)) else [vals]
        for x in vals:
            bv[int(x) - 1] = 1
        G = gate_of(rec.期号)
        hit[p] = G[(A @ rv).astype(np.int16), (B @ bv).astype(np.int16)].any()
    return hit


def main() -> None:
    pools, seqs, gcache = {}, {}, {}
    for lot in ("双色球", "大乐透"):
        pools[lot] = build_pool(lot, POOL, seed=20261024)
        seqs[lot] = load_test_recs(lot)

    rows = []
    for lot, K in CELLS:
        seq = seqs[lot]
        iss = seq[-1].期号
        r = rules_for(lot, iss)
        nred = r.outer_range[1]
        n_in = 12 if lot == "大乐透" else int(r.inner_size)
        res = TT.resolve_conditions(lot, TARGET, iss)
        grades = res["grades"]
        p = TT.single_ticket_probability(lot, res["conditions"], iss)
        kp, floor = K * p, max(0.0, K * p - 1.0)
        n = len(seq)

        hm = int(r.outer_width)
        gate_now = np.zeros((hm + 1, n_in + 1), dtype=bool)
        for (hh, jj), g in r.grade_table.items():
            if hh <= hm and jj <= n_in and g in grades:
                gate_now[hh, jj] = True

        def gate_of(issue, lot=lot, cache=gcache):
            rr = rules_for(lot, issue)
            g = cache.get((lot, rr.rule_version))
            if g is None:
                gg = TT.resolve_conditions(lot, TARGET, issue)["grades"]
                w = 12 if lot == "大乐透" else int(rr.inner_size)
                tbl = np.zeros((int(rr.outer_width) + 1, w + 1), dtype=bool)
                for (hh, jj), gn in rr.grade_table.items():
                    if hh <= int(rr.outer_width) and jj <= w and gn in gg:
                        tbl[hh, jj] = True
                g = cache[(lot, rr.rule_version)] = tbl
            return g

        pool = pools[lot]
        A_all = _mat(pool, "红球", nred)
        B_all = _mat(pool, "蓝球", n_in)
        OV = (A_all.astype(np.int16) @ A_all.astype(np.int16).T).astype(np.int8)

        # ---- 固定对照臂（seed=0）
        tks_r = control_tickets(lot, pool, K, seed=0, cap=CAP)
        tks_p, meta_p = select_tier_coverage(lot, pool, K, TARGET, iss,
                                             cap=CAP, seed=0)
        hit_r = _replay(tks_r, seq, gate_of, nred, n_in)
        hit_p = _replay(tks_p, seq, gate_of, nred, n_in)
        ex_r = exact_portfolio_coverage(lot, tks_r, TARGET, iss)
        ex_p = exact_portfolio_coverage(lot, tks_p, TARGET, iss)
        m_r, m_p = float(hit_r.mean()), float(hit_p.mean())
        head = (kp - m_r) - floor

        base = {
            "彩种": lot, "K": K, "预算元": K * 2, "期数": n,
            "单注p": p, "K·p": kp, "超额下界": floor, "理论Δmax": head,
            "随机_精确P": ex_r, "随机_实测P": m_r,
            "代理_精确P": ex_p, "代理_实测P": m_p,
            "代理_精确Δ": ex_p - ex_r,
            "代理_实测Δ": m_p - m_r,
        }
        sd_p = float((hit_p.astype(float) - hit_r.astype(float)).std(ddof=1))
        base["代理_配对SD"] = sd_p

        print(f"[{lot} K={K}] 随机 精确{ex_r:.4f}/实测{m_r:.4f} | "
              f"代理 精确Δ{100*(ex_p-ex_r):+.3f}pp/实测Δ{100*(m_p-m_r):+.3f}pp | "
              f"Δmax {100*head:.2f}pp", flush=True)

        exs, sds, rejs, diffs = [], [], [], []
        for s in MC_SEEDS:
            t0 = time.time()
            rng = np.random.default_rng(20261024 + s)
            Rd, Bd = _sample_draws(rng, nred, int(r.outer_width), n_in,
                                   2 if lot == "大乐透" else 1, M)
            masks = _masks(A_all, B_all, Rd, Bd, gate_now)
            idx, rej = mc_greedy(masks, OV, K, CAP)
            tks_g = [pool[i] for i in idx]
            if len(tks_g) != K:
                print(f"    seed{s}: 出票不足 {len(tks_g)}/{K}，跳过", flush=True)
                continue
            hit_g = _replay(tks_g, seq, gate_of, nred, n_in)
            ex_g = exact_portfolio_coverage(lot, tks_g, TARGET, iss)
            d = hit_g.astype(float) - hit_r.astype(float)
            exs.append(ex_g)
            sds.append(float(d.std(ddof=1)))
            rejs.append(rej)
            diffs.append(float(hit_g.mean() - m_r))
            print(f"    MC seed{s}: 精确P {ex_g:.4f} (Δ {100*(ex_g-ex_r):+.3f}pp) | "
                  f"实测Δ {100*diffs[-1]:+.3f}pp | cap拒绝 {rej} | "
                  f"{time.time()-t0:.0f}s", flush=True)
            del masks, Rd, Bd

        ex_g = float(np.mean(exs))
        sd_g = float(np.mean(sds))
        diff_g = float(np.mean(diffs))
        se = sd_g / math.sqrt(n)
        dex = ex_g - ex_r
        nstar = ((Z_A + Z_B) * sd_g / dex) ** 2 if abs(dex) > 1e-15 else float("inf")
        row = dict(base)
        row.update({
            "MC_精确P": ex_g, "MC_精确Δ": dex,
            "MC_精确Δ_跨seed全距": (max(exs) - min(exs)) if len(exs) > 1 else 0.0,
            "MC_实测Δ": diff_g, "MC_实测SE": se, "MC_配对SD": sd_g,
            "MC_z_实测": diff_g / se if se > 0 else 0.0,
            "MC_z_精确": dex / se if se > 0 else 0.0,
            "MC_n*": nstar,
            "MC_捕获率": (dex / head) if head > 1e-12 else float("nan"),
            "MC_代理捕获率": (base["代理_精确Δ"] / head) if head > 1e-12 else float("nan"),
            "MC_cap拒绝": int(np.mean(rejs)),
            "MC采样数": M, "MC_seeds": list(MC_SEEDS),
        })
        row["判决"] = ("★可判" if math.isfinite(nstar) and nstar <= n else
                       ("边缘" if math.isfinite(nstar) and nstar <= 3 * n else "不可判"))
        rows.append(row)
        print(flush=True)

    print("=" * 150)
    print(f"{'彩种':<6}{'K':>5}{'预算':>7}{'代理Δ':>9}{'MC精确Δ':>10}{'跨seed距':>9}"
          f"{'SD(d)':>7}{'z_精确':>8}{'n*':>8}{'可用':>6}{'倍':>6}"
          f"{'捕获%':>7}{'代理捕获%':>9}{'Δmax':>8}  {'判决'}")
    print("-" * 150)
    for r in rows:
        mult = (r["MC_n*"] / r["期数"]) if math.isfinite(r["MC_n*"]) else float("inf")
        print(f"{r['彩种']:<6}{r['K']:>5}{r['预算元']:>7}"
              f"{r['代理_精确Δ']*100:>+8.3f}pp{r['MC_精确Δ']*100:>+9.3f}pp"
              f"{r['MC_精确Δ_跨seed全距']*100:>+8.3f}pp{r['MC_配对SD']:>7.3f}"
              f"{r['MC_z_精确']:>+8.2f}"
              f"{(format(r['MC_n*'], '.0f') if math.isfinite(r['MC_n*']) else 'inf'):>8}"
              f"{r['期数']:>6}{(format(mult, '.2f') if math.isfinite(mult) else 'inf'):>6}"
              f"{r['MC_捕获率']*100:>6.1f}%{r['MC_代理捕获率']*100:>8.1f}%"
              f"{r['理论Δmax']*100:>7.2f}pp  {r['判决']}")
    print("=" * 150)
    print("效应量一律用**精确Δ**（无抽样噪声）；方差用实测配对 SD。")
    print("n* = ((1.96+0.84)·SD/Δ)²，80% 功效 / 双侧 α=0.05。")

    out = r"E:\707\output\stage0_design2.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"常量": {"POOL": POOL, "CAP": CAP, "M": M,
                            "MC_SEEDS": list(MC_SEEDS)}, "格": rows},
                  f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
