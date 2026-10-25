# -*- coding: utf-8 -*-
"""设计3 的口径修正版：**方差与效应量必须同口径**。

design3 暴露的问题
------------------
大乐透实测（按**逐期当期规则**判级）0.7536 vs 精确（**最新规则**）0.2101，差 38pp。
原因：大乐透有 5 个规则版本（367/698/735/1031/107 期），旧版本含九等/八等小奖，
「≥10元」在旧规则下命中率高得多。于是

    效应量 Δ 在「最新规则 + 低达标率(0.21)」下算，
    方差 SD 在「跨版本 + 高达标率(0.75)」下测  ⇒ 两者不同口径，n* 失真。

修正
----
回放一律用**最新规则的达标门**判全部历史期。这是反事实判级（历史期本不该用
最新规则），但：
  · 开奖号码序列仍是真实的；
  · 奖级定义与效应量口径一致；
  · 目的只是测「该票集在该奖级定义下的逐期波动」，不是测历史真实达标率。
双色球只有 1 个规则版本，两口径等价，本脚本对其是纯复核。

同时输出「按版本判级」的旧口径实测作对照，把差异显式摆出来。
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
        n_in = 12 if lot == "大乐透" else int(r.inner_size)
        res = TT.resolve_conditions(lot, TARGET, iss)
        grades = set(res["grades"])
        p1 = TT.single_ticket_probability(lot, res["conditions"], iss)
        kp, floor = K * p1, max(0.0, K * p1 - 1.0)
        n = len(seq)

        # ★ 统一口径：全部期用**最新规则**的达标门
        hm = int(r.outer_width)
        gate_now = np.zeros((hm + 1, n_in + 1), dtype=bool)
        for (hh, jj), g in r.grade_table.items():
            if hh <= hm and jj <= n_in and g in grades:
                gate_now[hh, jj] = True

        # 对照：逐期按当期规则（旧口径，只作展示）
        gcache: dict = {}

        def gate_by_version(issue):
            rr = rules_for(lot, issue)
            g = gcache.get(rr.rule_version)
            if g is None:
                gg = TT.resolve_conditions(lot, TARGET, issue)["grades"]
                w = 12 if lot == "大乐透" else int(rr.inner_size)
                tbl = np.zeros((int(rr.outer_width) + 1, w + 1), dtype=bool)
                for (hh, jj), gn in rr.grade_table.items():
                    if hh <= int(rr.outer_width) and jj <= w and gn in gg:
                        tbl[hh, jj] = True
                g = gcache[rr.rule_version] = tbl
            return g

        vers = [rules_for(lot, rec.期号).rule_version for rec in seq]
        nver = len(set(vers))

        def _replay(tks, use_version: bool):
            A = _mat(tks, "红球", nred)
            B = _mat(tks, "蓝球", n_in)
            hit = np.zeros(n, dtype=bool)
            nh = np.zeros(n, dtype=np.int32)
            for p_, rec in enumerate(seq):
                rv = np.zeros(nred, dtype=np.int8)
                for x in rec.红球:
                    rv[int(x) - 1] = 1
                bv = np.zeros(n_in, dtype=np.int8)
                vals = rec.蓝球
                vals = list(vals) if isinstance(vals, (list, tuple)) else [vals]
                for x in vals:
                    bv[int(x) - 1] = 1
                G = gate_by_version(rec.期号) if use_version else gate_now
                v = G[(A @ rv).astype(np.int16), (B @ bv).astype(np.int16)]
                hit[p_] = v.any()
                nh[p_] = v.sum()
            return hit, nh

        pool = pools[lot]
        A_all = _mat(pool, "红球", nred)
        B_all = _mat(pool, "蓝球", n_in)
        OV = (A_all.astype(np.int16) @ A_all.astype(np.int16).T).astype(np.int8)

        tks_r = control_tickets(lot, pool, K, seed=0, cap=CAP)
        tks_p, _ = select_tier_coverage(lot, pool, K, TARGET, iss, cap=CAP, seed=0)
        hit_r, nh_r = _replay(tks_r, False)
        hit_r_v, _ = _replay(tks_r, True)
        hit_p, _ = _replay(tks_p, False)
        m_r, m_p = float(hit_r.mean()), float(hit_p.mean())

        t0 = time.time()
        ex_r = exact_union_prob(lot, tks_r, TARGET, iss, chunk=1024)
        ex_p = exact_union_prob(lot, tks_p, TARGET, iss, chunk=1024)
        print(f"[{lot} K={K}] 规则版本 {nver} 个 | 随机 精确{ex_r:.6f} / "
              f"统一口径实测{float(hit_r.mean()):.4f} / 按版本实测"
              f"{float(hit_r_v.mean()):.4f} | 枚举 {time.time()-t0:.0f}s", flush=True)

        head = (kp - m_r) - floor
        sd_p = float((hit_p.astype(float) - hit_r.astype(float)).std(ddof=1))
        base = {
            "彩种": lot, "K": K, "预算元": K * 2, "期数": n, "规则版本数": nver,
            "单注p": p1, "K·p": kp, "理论Δmax": head,
            "随机_精确P": ex_r, "随机_实测P": m_r, "随机_按版本实测P": float(hit_r_v.mean()),
            "代理_精确P": ex_p, "代理_实测P": m_p,
            "代理_精确Δ": ex_p - ex_r, "代理_实测Δ": m_p - m_r,
            "代理_配对SD": sd_p, "代理_精确减实测": (ex_p - ex_r) - (m_p - m_r),
            "哨兵_随机达标注数": float(nh_r.mean()),
        }
        print(f"    代理 Δ: 精确 {100*(ex_p-ex_r):+.3f}pp | 实测 "
              f"{100*(m_p-m_r):+.3f}pp | 差 {100*base['代理_精确减实测']:+.3f}pp | "
              f"Δmax {100*head:.2f}pp", flush=True)

        exs, sds, rejs, diffs, nzs = [], [], [], [], []
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
            hit_g, nh_g = _replay(tks_g, False)
            ex_g = exact_union_prob(lot, tks_g, TARGET, iss, chunk=1024)
            d = hit_g.astype(float) - hit_r.astype(float)
            exs.append(ex_g)
            sds.append(float(d.std(ddof=1)))
            rejs.append(rej)
            nzs.append(float(nh_g.mean()))
            diffs.append(float(hit_g.mean() - m_r))
            print(f"    MC seed{s}: 精确P {ex_g:.6f} (Δ {100*(ex_g-ex_r):+.3f}pp) | "
                  f"实测Δ {100*diffs[-1]:+.3f}pp | 哨兵 {nh_g.mean():.3f} 注 | "
                  f"cap拒绝 {rej} | {time.time()-t0:.0f}s", flush=True)
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
            "MC_z_精确": dex / se if se > 0 else 0.0,
            "MC_z_实测": diff_g / se if se > 0 else 0.0,
            "MC_n*": nstar, "MC_精确减实测": dex - diff_g,
            "MC_捕获率": (dex / head) if head > 1e-12 else float("nan"),
            "代理_捕获率": (base["代理_精确Δ"] / head) if head > 1e-12 else float("nan"),
            "MC_cap拒绝": int(np.mean(rejs)),
            "哨兵_MC达标注数": float(np.mean(nzs)),
            "MC采样数": M, "MC_seeds": list(MC_SEEDS),
        })
        row["判决"] = ("★可判" if math.isfinite(nstar) and nstar <= n else
                       ("边缘" if math.isfinite(nstar) and nstar <= 3 * n else "不可判"))
        rows.append(row)
        print(flush=True)

    print("=" * 160)
    print(f"{'彩种':<6}{'K':>5}{'预算':>7}{'版本':>5}{'随机精确P':>10}{'随机实测P':>10}"
          f"{'代理Δ':>9}{'MC精确Δ':>10}{'跨seed距':>9}{'SD(d)':>7}"
          f"{'z_精确':>8}{'n*':>9}{'可用':>6}{'倍':>6}{'捕获%':>7}  {'判决'}")
    print("-" * 160)
    for r in rows:
        mult = (r["MC_n*"] / r["期数"]) if math.isfinite(r["MC_n*"]) else float("inf")
        print(f"{r['彩种']:<6}{r['K']:>5}{r['预算元']:>7}{r['规则版本数']:>5}"
              f"{r['随机_精确P']:>10.4f}{r['随机_实测P']:>10.4f}"
              f"{r['代理_精确Δ']*100:>+8.3f}pp{r['MC_精确Δ']*100:>+9.3f}pp"
              f"{r['MC_精确Δ_跨seed全距']*100:>+8.3f}pp{r['MC_配对SD']:>7.3f}"
              f"{r['MC_z_精确']:>+8.2f}"
              f"{(format(r['MC_n*'], '.0f') if math.isfinite(r['MC_n*']) else 'inf'):>9}"
              f"{r['期数']:>6}{(format(mult, '.2f') if math.isfinite(mult) else 'inf'):>6}"
              f"{r['MC_捕获率']*100:>6.1f}%  {r['判决']}")
    print("=" * 160)

    print()
    print("口径核对（统一用最新规则判级后的实测 vs 精确）")
    print(f"{'彩种':<6}{'K':>5}{'臂':<7}{'精确Δ':>10}{'实测Δ':>10}{'差':>10}"
          f"{'SE':>9}{'z':>7}   {'随机精确P':>10}{'随机实测P':>11}{'差':>10}")
    for r in rows:
        se = r["MC_实测SE"]
        for arm, de, dm in (("代理", r["代理_精确Δ"], r["代理_实测Δ"]),
                            ("MC", r["MC_精确Δ"], r["MC_实测Δ"])):
            z = (dm - de) / se if se > 0 else 0.0
            print(f"{r['彩种']:<6}{r['K']:>5}{arm:<8}"
                  f"{de*100:>+9.3f}pp{dm*100:>+9.3f}pp{(dm-de)*100:>+9.3f}pp"
                  f"{se*100:>8.3f}pp{z:>+7.2f}")
        print(f"{'':<19}{'':>10}{'':>10}{'':>10}{'':>9}{'':>7}   "
              f"{r['随机_精确P']:>10.4f}{r['随机_实测P']:>11.4f}"
              f"{(r['随机_实测P']-r['随机_精确P'])*100:>+9.3f}pp")
    print()
    print("效应量 = exact_union_prob（外层×内层全枚举，与均匀 MC 对账 ±1.3σ）")
    print("方差   = 真实开奖回放的配对差 SD，全历史统一用**最新规则**判级（与效应量同口径）")
    print("n* = ((1.96+0.84)·SD/Δ)²，80% 功效 / 双侧 α=0.05")

    out = r"E:\707\output\stage0_design4.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"常量": {"POOL": POOL, "CAP": CAP, "M": M,
                            "MC_SEEDS": list(MC_SEEDS),
                            "口径": "回放统一用最新规则达标门；效应量为无近似枚举"},
                   "格": rows}, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
