# -*- coding: utf-8 -*-
"""路径 C：把「捕获率」提上去——直接在 P(并集) 上做贪心，而不是在通道权重代理上。

为什么值得做
------------
E[达标注数] = K·p 与选号无关（已证），所以可改善的量只有一个：
「超额」E[(X−1)^+] 的压缩，其下界是 max(0, K·p−1)。

    大乐透 ≥10元 K=300：Δmax = 13.2pp，现生产代理贪心只拿到 2.27pp（17%）。

现生产 `select_tier_coverage` 贪心的目标是**通道权重覆盖**（代理），不是 P(并集)。
f(S) = P(∪A_t) 是**单调子模**的（覆盖函数），所以在 f 上直接贪心有 (1−1/e) 保证，
且代理与真目标之间的落差是可以实测的。

怎么做
------
1. 均匀采样 M 个开奖（前区 5/35、后区 2/12），用**位图**存「每张票在每个采样开奖上是否达标」。
2. 在 MC 估计的 f 上做 **lazy greedy（CELF）**，带生产口径的 cap 重叠约束。
3. MC 只用于**排序**；最终判定一律用
   - `exact_portfolio_coverage`（精确模型 P，无抽样噪声）
   - 真实开奖回放（逐期按当期规则版本判级）

诚实边界
--------
- 这是**组合设计**的改进，不涉及任何对开奖的预测；E[X]=K·p 恒等不变。
- MC 排序有噪声 ⇒ 只报「改进了多少」，不报「这是最优设计」。
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
M = 100_000           # MC 采样开奖数（只用于排序）
TARGET = {"kind": "payout", "min_payout": 10.0}

# (彩种, K)
CELLS = [("大乐透", 200), ("大乐透", 300), ("大乐透", 400),
         ("双色球", 200), ("双色球", 300)]

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


def _sample_draws(rng, n_outer, k_outer, n_inner, k_inner, m):
    """均匀采样 m 个开奖，返回 (k_outer×m, k_inner×m) 的 one-hot 转置矩阵。"""
    oi = np.argsort(rng.random((m, n_outer)), axis=1)[:, :k_outer]
    ii = np.argsort(rng.random((m, n_inner)), axis=1)[:, :k_inner]
    R = np.zeros((m, n_outer), dtype=np.float32)
    B = np.zeros((m, n_inner), dtype=np.float32)
    np.put_along_axis(R, oi, 1.0, axis=1)
    np.put_along_axis(B, ii, 1.0, axis=1)
    return R, B


def _masks(A, Bm, R, B, gate, chunk=400):
    """逐块算 (h,j) → 达标位图，打包成 uint8 位图 (P, M/8)。"""
    P = A.shape[0]
    out = np.zeros((P, R.shape[0] // 8 + (1 if R.shape[0] % 8 else 0)), dtype=np.uint8)
    Rt = np.ascontiguousarray(R.T)   # (n_outer, m)
    Bt = np.ascontiguousarray(B.T)   # (n_inner, m)
    Af = A.astype(np.float32)
    Bf = Bm.astype(np.float32)
    for s in range(0, P, chunk):
        e = min(P, s + chunk)
        h = (Af[s:e] @ Rt).astype(np.int16)
        j = (Bf[s:e] @ Bt).astype(np.int16)
        bit = gate[h, j]
        out[s:e] = np.packbits(bit, axis=1)
    return out


def _gain(masks, cov_inv, row):
    return float(POP[masks[row] & cov_inv].sum(dtype=np.int64))


def mc_greedy(masks, OV, K, cap, m):
    """Lazy greedy（CELF）直接在 P(并集) 上最大化，带 cap 重叠约束。"""
    P = masks.shape[0]
    nbytes = masks.shape[1]
    cov = np.zeros(nbytes, dtype=np.uint8)
    cov_inv = np.full(nbytes, 0xFF, dtype=np.uint8)
    sel: list[int] = []
    relax = 0
    heap = []
    for i in range(P):
        heapq.heappush(heap, (-_gain(masks, cov_inv, i), i, -1))
    step = 0
    while len(sel) < K and heap:
        negg, i, stamp = heapq.heappop(heap)
        if i in sel:
            continue
        if stamp != step:
            g = _gain(masks, cov_inv, i)
            heapq.heappush(heap, (-g, i, step))
            continue
        # cap 约束：与**每张**已选票的红球重叠 ≤ cap
        if sel and int(OV[i, sel].max()) > cap:
            relax += 1
            continue          # 丢弃，不再入堆
        sel.append(i)
        cov |= masks[i]
        cov_inv = ~cov
        step += 1
    return sel, relax


def _replay(tks, seq, lot, gate_of, nred, ninner):
    A = _mat(tks, "红球", nred)
    B = _mat(tks, "蓝球", ninner)
    n = len(seq)
    hit = np.zeros(n, dtype=bool)
    cnt = np.zeros(n, dtype=np.int32)
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
        v = G[(A @ rv).astype(np.int16), (B @ bv).astype(np.int16)]
        hit[p] = v.any()
        cnt[p] = int(v.sum())
    return hit, cnt


def main() -> None:
    pools, seqs, gates_cache = {}, {}, {}
    for lot in ("双色球", "大乐透"):
        pools[lot] = build_pool(lot, POOL, seed=20261024)
        seqs[lot] = load_test_recs(lot)

    rows = []
    for lot, K in CELLS:
        seq = seqs[lot]
        iss = seq[-1].期号
        r = rules_for(lot, iss)
        nred = r.outer_range[1]
        n_inner_balls = 12 if lot == "大乐透" else int(r.inner_size)
        res = TT.resolve_conditions(lot, TARGET, iss)
        grades = res["grades"]
        p = TT.single_ticket_probability(lot, res["conditions"], iss)
        kp, floor = K * p, max(0.0, K * p - 1.0)

        # 最新规则版本的门（与精确口径一致）
        hm, jm = int(r.outer_width), n_inner_balls
        gate_now = np.zeros((hm + 1, jm + 1), dtype=bool)
        for (hh, jj), g in r.grade_table.items():
            if hh <= hm and jj <= jm and g in grades:
                gate_now[hh, jj] = True

        def gate_of(issue, lot=lot, cache=gates_cache):
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
        B_all = _mat(pool, "蓝球", n_inner_balls)

        # ---- 三臂票集
        tks_r = control_tickets(lot, pool, K, seed=0, cap=CAP)
        tks_p, meta_p = select_tier_coverage(lot, pool, K, TARGET, iss,
                                             cap=CAP, seed=0)
        t0 = time.time()
        rng = np.random.default_rng(20261024)
        Rd, Bd = _sample_draws(rng, nred, int(r.outer_width),
                               n_inner_balls, 2 if lot == "大乐透" else 1, M)
        masks = _masks(A_all, B_all, Rd, Bd, gate_now)
        OV = (A_all.astype(np.int16) @ A_all.astype(np.int16).T).astype(np.int8)
        idx, relax = mc_greedy(masks, OV, K, CAP, M)
        tks_g = [pool[i] for i in idx]
        dt_mc = time.time() - t0
        print(f"[{lot} K={K}] MC贪心 {dt_mc:.1f}s，放宽 {relax} 次，"
              f"出票 {len(tks_g)}/{K}", flush=True)

        # ---- 评估
        hit_r, _ = _replay(tks_r, seq, lot, gate_of, nred, n_inner_balls)
        hit_p, _ = _replay(tks_p, seq, lot, gate_of, nred, n_inner_balls)
        hit_g, cnt_g = _replay(tks_g, seq, lot, gate_of, nred, n_inner_balls)

        ex_r = exact_portfolio_coverage(lot, tks_r, TARGET, iss)
        ex_p = exact_portfolio_coverage(lot, tks_p, TARGET, iss)
        ex_g = exact_portfolio_coverage(lot, tks_g, TARGET, iss)

        m_r, m_p, m_g = float(hit_r.mean()), float(hit_p.mean()), float(hit_g.mean())
        excess_r, excess_p, excess_g = kp - m_r, kp - m_p, kp - m_g
        # Δmax 用**随机臂实测**作基准（同一批真实开奖）
        head = excess_r - floor
        n = len(seq)

        def _pair(hit_a, hit_b):
            d = hit_a.astype(float) - hit_b.astype(float)
            sd = float(d.std(ddof=1))
            diff = float(hit_a.mean() - hit_b.mean())
            se = sd / math.sqrt(n)
            return diff, se, sd, (diff / se if se > 0 else 0.0)

        for name, tks, ex, m, exc, hit in (
            ("代理贪心(现生产)", tks_p, ex_p, m_p, excess_p, hit_p),
            ("MC直接贪心", tks_g, ex_g, m_g, excess_g, hit_g),
        ):
            diff, se, sd, z = _pair(hit, hit_r)
            row = {
                "彩种": lot, "K": K, "臂": name,
                "单注p": p, "K·p": kp, "超额下界": floor, "理论Δmax": head,
                "精确P": ex, "精确Δ": ex - ex_r,
                "实测P": m, "实测Δ": diff, "实测SE": se, "z": z,
                "配对SD": sd,
                "超额": exc, "捕获率": (diff / head) if head > 1e-12 else float("nan"),
                "精确捕获率": ((ex - ex_r) / head) if head > 1e-12 else float("nan"),
                "达标注数": float(cnt_g.mean()) if name == "MC直接贪心" else None,
                "放宽次数": relax if name == "MC直接贪心" else meta_p.get("放宽次数", 0),
                "通道总数": meta_p["通道总数"], "覆盖通道数": meta_p["覆盖通道数"],
                "MC采样数": M, "MC耗时秒": dt_mc,
            }
            rows.append(row)
            print(f"   {name:<16} 精确P {ex:.4f} (Δ {100*(ex-ex_r):+.3f}pp) | "
                  f"实测P {m:.4f} (Δ {100*diff:+.3f}pp ± {100*se:.3f}pp, z={z:+.2f}) | "
                  f"捕获 {100*row['捕获率']:.1f}%", flush=True)
        print(f"   {'随机(对照)':<16} 精确P {ex_r:.4f} | 实测P {m_r:.4f} | "
              f"理论Δmax {100*head:.2f}pp", flush=True)
        print(flush=True)

    print("=" * 128)
    print(f"{'彩种':<6}{'K':>5}{'臂':<18}{'精确P':>9}{'精确Δ':>10}{'实测Δ':>10}"
          f"{'SE':>8}{'z':>7}{'捕获%':>8}{'精确捕获%':>10}{'Δmax(pp)':>10}")
    print("-" * 128)
    for r in rows:
        print(f"{r['彩种']:<6}{r['K']:>5}{r['臂']:<18}{r['精确P']:>9.4f}"
              f"{r['精确Δ']*100:>+9.3f}pp{r['实测Δ']*100:>+9.3f}pp"
              f"{r['实测SE']*100:>7.3f}pp{r['z']:>+7.2f}"
              f"{r['捕获率']*100:>7.1f}%{r['精确捕获率']*100:>9.1f}%"
              f"{r['理论Δmax']*100:>9.2f}pp")
    print("=" * 128)

    out = r"E:\707\output\stage0_design.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"常量": {"POOL": POOL, "CAP": CAP, "M": M}, "行": rows},
                  f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
