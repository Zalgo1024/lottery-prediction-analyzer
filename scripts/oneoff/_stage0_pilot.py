# -*- coding: utf-8 -*-
"""Stage 0 · pilot：扫 K，找「可判格」。

为什么还要再扫一遍
------------------
判决表用的 4 个格子是**方案预先固定**的（生产 K / 抬高 K × 两个主终点），
四格全部不可判。但 K 前沿显示「Δ 对 K 不是单调的」：

    大乐透 ≥10元   K=38:+0.24pp   K=200:+2.87pp   K=400:+1.73pp

即「不可判」可能是**格子选错了**，不是问题本身不可判。本脚本不做新策略，
只把 (彩种 × 终点 × K) 网格上的可判性测出来。

三件必须一起报的数
------------------
1. **实测 Δ + 配对差 SD** ⇒ z / p / n*（不再依赖未知 ρ，直接测）。
2. **捕获率** = Δ / Δmax：因为 E[达标注数]=K·p 与选号无关，
   可改善的量只有「超额」`E[(X−1)^+]`，其下界是 `max(0, K·p−1)`。
   捕获率回答「现在的选号器吃掉了多少理论空间」——这是路径 C 的核心问题。
3. **分半稳定性**：前半 / 后半各自的 Δ。符号翻转 ⇒ 即使 z 显著也不可信。

多重比较
--------
K 是**事后**挑的（我们先扫了曲线才看到 K=200 的峰），所以必须按
「扫过多少个格子」做 BH 校正，否则峰值自带选择偏差。这里把
(彩种 × 终点 × K) 的每一个格子都算一个假设，seed 只作重复不作假设。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from collections import Counter

import numpy as np

sys.path.insert(0, r"E:\707")

import ev.tier_target as TT
from ev.profit.rules import rules_for
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import select_tier_coverage, exact_portfolio_coverage
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool

POOL = 8000          # = ev.tier_coverage.MAX_POOL，生产口径
CAP = 3              # = PROD_OVERLAP_CAP，不是 selection._OVERLAP_CAP['双色球']=4
SEEDS = (0, 1)
Z_A, Z_B = 1.959964, 0.841621

TARGET_10 = {"kind": "payout", "min_payout": 10.0}

# (彩种, 终点标签, K)
GRID = [
    ("双色球", "≥10元", 50),
    ("双色球", "≥10元", 100),
    ("双色球", "≥10元", 200),
    ("双色球", "≥10元", 300),
    ("双色球", "≥10元", 400),
    ("双色球", "≥10元", 600),
    ("大乐透", "≥10元", 50),
    ("大乐透", "≥10元", 100),
    ("大乐透", "≥10元", 150),
    ("大乐透", "≥10元", 200),
    ("大乐透", "≥10元", 300),
    ("大乐透", "≥10元", 400),
]


def _mat(tks, key, width):
    M = np.zeros((len(tks), width), dtype=np.int8)
    for i, t in enumerate(tks):
        v = t.get(key)
        vals = list(v) if isinstance(v, (list, tuple)) else [v]
        for x in vals:
            if x is not None:
                M[i, int(x) - 1] = 1
    return M


def _draw(rec, nred, ninner):
    rv = np.zeros(nred, dtype=np.int8)
    for x in rec.红球:
        rv[int(x) - 1] = 1
    bv = np.zeros(ninner, dtype=np.int8)
    blues = rec.蓝球
    vals = list(blues) if isinstance(blues, (list, tuple)) else [blues]
    for x in vals:
        bv[int(x) - 1] = 1
    return rv, bv


def _norm_sf(z):
    """标准正态双尾 p。"""
    return math.erfc(abs(z) / math.sqrt(2.0))


def _bh(pvals):
    """Benjamini–Hochberg，返回 q 值（与输入同序）。"""
    m = len(pvals)
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [0.0] * m
    prev = 1.0
    for rank, i in enumerate(reversed(order), start=1):
        k = m - rank + 1
        prev = min(prev, pvals[i] * m / k)
        q[i] = prev
    return q


def main() -> None:
    pools, recs = {}, {}
    for lot in ("双色球", "大乐透"):
        t0 = time.time()
        pools[lot] = build_pool(lot, POOL, seed=20261024)
        recs[lot] = load_test_recs(lot)
        print(f"[{lot}] 池 {len(pools[lot])} 张 / 历史 {len(recs[lot])} 期 "
              f"（{time.time()-t0:.1f}s）", flush=True)
    print(flush=True)

    rows = []
    for lot, label, K in GRID:
        seq = recs[lot]
        iss = seq[-1].期号
        r = rules_for(lot, iss)
        nred, ninner = r.outer_range[1], int(r.inner_size)

        # 单注达标概率 p（最新版本口径，用于 K·p 与捕获率）
        # ★ single_ticket_probability 吃的是 **conditions**，不是 target dict
        res_now = TT.resolve_conditions(lot, TARGET_10, iss)
        p = TT.single_ticket_probability(lot, res_now["conditions"], iss)

        gates: dict = {}

        def gate_of(issue):
            rr = rules_for(lot, issue)
            g = gates.get(rr.rule_version)
            if g is None:
                gg = TT.resolve_conditions(lot, TARGET_10, issue)["grades"]
                hm, jm = int(rr.outer_width), int(rr.inner_size)
                tbl = np.zeros((hm + 1, jm + 1), dtype=bool)
                for (hh, jj), gname in rr.grade_table.items():
                    if hh <= hm and jj <= jm and gname in gg:
                        tbl[hh, jj] = True
                g = gates[rr.rule_version] = tbl
            return g

        for seed in SEEDS:
            t0 = time.time()
            tks_c, meta = select_tier_coverage(lot, pools[lot], K, TARGET_10,
                                               iss, cap=CAP, seed=seed)
            tks_r = control_tickets(lot, pools[lot], K, seed=seed, cap=CAP)
            dt_sel = time.time() - t0
            if len(tks_c) != K or len(tks_r) != K:
                print(f"[{lot} K={K} seed={seed}] ✗ 出票不足 "
                      f"({len(tks_c)}/{len(tks_r)} vs {K})，跳过", flush=True)
                continue

            Ac, Bc = _mat(tks_c, "红球", nred), _mat(tks_c, "蓝球", ninner)
            Ar, Br = _mat(tks_r, "红球", nred), _mat(tks_r, "蓝球", ninner)

            n = len(seq)
            hc = np.zeros(n, dtype=bool)
            hr = np.zeros(n, dtype=bool)
            nc = np.zeros(n, dtype=np.int32)
            nr = np.zeros(n, dtype=np.int32)
            t0 = time.time()
            for idx, rec in enumerate(seq):
                rv, bv = _draw(rec, nred, ninner)
                G = gate_of(rec.期号)
                vc = G[(Ac @ rv).astype(np.int16), (Bc @ bv).astype(np.int16)]
                vr = G[(Ar @ rv).astype(np.int16), (Br @ bv).astype(np.int16)]
                hc[idx], hr[idx] = vc.any(), vr.any()
                nc[idx], nr[idx] = vc.sum(), vr.sum()
            dt_rp = time.time() - t0

            d = hc.astype(float) - hr.astype(float)
            m_c, m_r = float(hc.mean()), float(hr.mean())
            diff = m_c - m_r
            sd_d = float(d.std(ddof=1))
            se_d = sd_d / math.sqrt(n)
            z = diff / se_d if se_d > 0 else 0.0
            pv = _norm_sf(z)
            v1, v2 = m_r * (1 - m_r), m_c * (1 - m_c)
            rho_hat = ((v1 + v2 - sd_d ** 2) / (2 * math.sqrt(v1 * v2))
                       if v1 > 0 and v2 > 0 else float("nan"))

            kp = min(1.0, K * p)
            ex_r = m_r  # 实测口径
            excess_r = K * p - m_r          # 随机臂超额
            excess_c = K * p - m_c          # 覆盖臂超额
            floor = max(0.0, K * p - 1.0)   # 超额的理论下界
            headroom = excess_r - floor     # 完全可达的最大 Δ
            capture = (diff / headroom) if headroom > 1e-12 else float("nan")

            # 分半稳定性
            h = n // 2
            d1 = float(hc[:h].mean() - hr[:h].mean())
            d2 = float(hc[h:].mean() - hr[h:].mean())

            ex_c = exact_portfolio_coverage(lot, tks_c, TARGET_10, iss)
            exr = exact_portfolio_coverage(lot, tks_r, TARGET_10, iss)

            row = {
                "彩种": lot, "终点": label, "K": K, "seed": seed,
                "预算元": K * 2,
                "单注p": p, "K·p": K * p,
                "实测_随机": m_r, "实测_覆盖": m_c, "实测差": diff,
                "实测差_SE": se_d, "z": z, "p": pv,
                "配对差SD": sd_d, "配对ρ估计": rho_hat,
                "精确_随机": exr, "精确_覆盖": ex_c, "精确差": ex_c - exr,
                "精确减实测_随机": exr - m_r, "精确减实测_覆盖": ex_c - m_c,
                "哨兵_随机臂达标注数": float(nr.mean()),
                "哨兵_覆盖臂达标注数": float(nc.mean()),
                "超额_随机": excess_r, "超额_覆盖": excess_c,
                "超额下界": floor, "理论Δmax": headroom, "捕获率": capture,
                "前半Δ": d1, "后半Δ": d2, "分半同号": (d1 * d2 > 0),
                "n*": (float(((Z_A + Z_B) * sd_d / diff) ** 2)
                       if abs(diff) > 1e-15 else float("inf")),
                "可用期数": n,
                "重叠上限": meta.get("重叠上限"), "放宽次数": meta.get("放宽次数", 0),
                "通道总数": meta["通道总数"], "覆盖通道数": meta["覆盖通道数"],
                "通道权重和": meta["通道权重和"],
                "选号秒": dt_sel, "回放秒": dt_rp,
            }
            rows.append(row)
            print(f"[{lot} {label} K={K:>4} seed={seed}] "
                  f"随机 {m_r:.4f} 覆盖 {m_c:.4f} 差 {diff*100:+.3f}pp "
                  f"SE {se_d*100:.3f}pp z={z:+.2f} p={pv:.4f} "
                  f"| 捕获率 {capture*100:.1f}% "
                  f"| 分半 {d1*100:+.2f}/{d2*100:+.2f} "
                  f"| {dt_sel+dt_rp:.1f}s", flush=True)
        print(flush=True)

    # ---- 多重比较：格子 = (彩种, 终点, K)，seed 取 p 的中位作为该格代表
    cells = {}
    for r in rows:
        cells.setdefault((r["彩种"], r["终点"], r["K"]), []).append(r["p"])
    keys = sorted(cells)
    pcell = [float(np.median(cells[k])) for k in keys]
    qs = _bh(pcell)

    print("=" * 132)
    print(f"{'彩种':<6}{'终点':<7}{'K':>5}{'预算':>7}{'Δ(pp)':>9}{'SE':>8}"
          f"{'z':>7}{'p':>9}{'q(BH)':>9}{'SD(d)':>7}{'ρ̂':>7}"
          f"{'Δmax(pp)':>10}{'捕获%':>7}{'n*':>9}{'可用':>6}  {'判决'}")
    print("-" * 132)
    qmap = {k: q for k, q in zip(keys, qs)}
    for r in rows:
        k = (r["彩种"], r["终点"], r["K"])
        q = qmap[k]
        ok_z = (q < 0.05) and r["实测差"] > 0
        ok_n = math.isfinite(r["n*"]) and r["n*"] <= r["可用期数"]
        stable = r["分半同号"]
        if ok_z and ok_n and stable:
            verdict = "★可判"
        elif ok_z and stable:
            verdict = "可判(n*>可用,以实测z为准)"
        elif r["实测差"] > 0 and ok_n:
            verdict = "边缘"
        else:
            verdict = "不可判"
        print(f"{r['彩种']:<6}{r['终点']:<8}{r['K']:>5}{r['预算元']:>7}"
              f"{r['实测差']*100:>+8.3f}pp{r['实测差_SE']*100:>7.3f}pp"
              f"{r['z']:>+7.2f}{r['p']:>9.4f}{q:>9.4f}"
              f"{r['配对差SD']:>7.3f}{r['配对ρ估计']:>+7.3f}"
              f"{r['理论Δmax']*100:>9.2f}pp{r['捕获率']*100:>6.1f}%"
              f"{(format(r['n*'], '.0f') if math.isfinite(r['n*']) else 'inf'):>9}"
              f"{r['可用期数']:>6}  {verdict} (seed {r['seed']})")
    print("=" * 132)
    print("判决口径：q(BH) < 0.05 且 Δ>0 且 分半同号 ⇒ 可判。"
          "n* 是「用实测 SD 复现同效应所需期数」，>可用期数 说明本次显著含运气成分。")
    print()

    print("### 捕获率（回答「选号器吃掉了多少理论空间」）")
    print(f"{'彩种':<6}{'终点':<8}{'K':>5}{'K·p':>8}{'超额_随机':>10}"
          f"{'超额_覆盖':>10}{'下界':>8}{'Δmax(pp)':>10}{'实测Δ(pp)':>11}{'捕获%':>8}")
    print("-" * 132)
    for r in rows:
        print(f"{r['彩种']:<6}{r['终点']:<8}{r['K']:>5}{r['K·p']:>8.4f}"
              f"{r['超额_随机']:>10.4f}{r['超额_覆盖']:>10.4f}"
              f"{r['超额下界']:>8.4f}{r['理论Δmax']*100:>9.2f}pp"
              f"{r['实测差']*100:>+10.3f}pp{r['捕获率']*100:>7.1f}%")
    print("=" * 132)

    out = r"E:\707\output\stage0_pilot.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"常量": {"POOL": POOL, "CAP": CAP, "SEEDS": list(SEEDS)},
                   "格": rows}, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
