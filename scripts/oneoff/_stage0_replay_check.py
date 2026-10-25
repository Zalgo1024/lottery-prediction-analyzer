# -*- coding: utf-8 -*-
"""Stage 0 真实回放核对（向量化 + 审计对账）。

为什么向量化
------------
实测 `ev.coverage_backtest.evaluate_period` ≈ **1.8 s/期（双色球 K=200）**，
1000 期 × 2 臂 = 1 小时/格，4 格就要 4 小时，且这直接决定 Stage 1 的成本。
故判级改成矩阵乘（h=红球交集、j=内层交集、查 rules.grade_table），
并**强制**用审计路径 `TT.target_is_hit`（走 data.feedback 分类器）对账；
不一致 > 0 则整份结果作废并打印首个反例。

三件事
------
1. **保真性**：向量化 vs target_is_hit 的逐票对账（30 期 × 全部票）。
2. **精确值核对**：`exact_portfolio_coverage` 的模型 P vs 真实开奖频率。
3. **配对差 SD**：真实 d_t 的均值与 SD ⇒ 所需期数，不依赖未知 ρ。
"""
from __future__ import annotations

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
AUDIT_PERIODS = 30
Z_A, Z_B = 1.959964, 0.841621

CELLS = [
    ("双色球", "生产", 200, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("双色球", "抬高", 360, {"kind": "grade", "grade": "四等"}, "≥四等"),
    ("大乐透", "生产", 38, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("大乐透", "抬高", 1120, {"kind": "grade", "grade": "四等"}, "≥四等"),
]


def _mat(tks, lottery, key, width):
    M = np.zeros((len(tks), width), dtype=np.int8)
    for i, t in enumerate(tks):
        v = t.get(key)
        vals = list(v) if isinstance(v, (list, tuple)) else [v]
        for x in vals:
            if x is not None:
                M[i, int(x) - 1] = 1
    return M


def _draw(rec, lottery, nred, ninner):
    rv = np.zeros(nred, dtype=np.int8)
    for x in rec.红球:
        rv[int(x) - 1] = 1
    bv = np.zeros(ninner, dtype=np.int8)
    blues = rec.蓝球
    vals = list(blues) if isinstance(blues, (list, tuple)) else [blues]
    for x in vals:
        bv[int(x) - 1] = 1
    return rv, bv


def main() -> None:
    pools, recs = {}, {}
    for lot in ("双色球", "大乐透"):
        pools[lot] = build_pool(lot, POOL, seed=20261024)
        recs[lot] = load_test_recs(lot)

    print(f"池 {POOL}；cap={CAP}；票集固定；审计对账 {AUDIT_PERIODS} 期 × 全部票", flush=True)
    print(flush=True)

    rows = []
    for lot, tier, K, target, label in CELLS:
        seq = recs[lot]
        iss = seq[-1].期号
        res = TT.resolve_conditions(lot, target, iss)
        grades = res["grades"]
        r = rules_for(lot, iss)
        nred, ninner = r.outer_range[1], int(r.inner_size)

        tks_c, meta = select_tier_coverage(lot, pools[lot], K, target, iss, cap=CAP, seed=0)
        tks_r = control_tickets(lot, pools[lot], K, seed=0, cap=CAP)
        Ac, Bc = _mat(tks_c, lot, "红球", nred), _mat(tks_c, lot, "蓝球", ninner)
        Ar, Br = _mat(tks_r, lot, "红球", nred), _mat(tks_r, lot, "蓝球", ninner)

        # ★ 达标门必须**按规则版本**建：大乐透有分段版本表，拿最新表扫全历史会把早期期
        #   判错（首次探针的 9~11 处不一致全部来自 7xxx 早期期号，就是这个原因）。
        gates: dict = {}

        def gate_of(issue):
            from ev.profit.rules import rules_for as _rf
            ver = _rf(lot, issue).rule_version
            g = gates.get(ver)
            if g is None:
                gg = TT.resolve_conditions(lot, target, issue)["grades"]
                rr = _rf(lot, issue)
                hm, jm = int(rr.outer_width), int(rr.inner_size)
                tbl = np.zeros((hm + 1, jm + 1), dtype=bool)
                for (hh, jj), gname in rr.grade_table.items():
                    if hh <= hm and jj <= jm and gname in gg:
                        tbl[hh, jj] = True
                g = gates[ver] = tbl
            return g

        # ---- ① 审计对账：向量化 vs target_is_hit
        # ★ 必须传**原始 target**，不能传 res：`target_is_hit` 见到 `grades` 键会直接
        #   用它、忽略 issue ⇒ 审计自己就变成「全历史用最新奖级」，和向量化的按版本分表
        #   不可比（第一版探针的 9 处不一致就是这么来的）。
        bad, first_bad = 0, None
        for rec in seq[:AUDIT_PERIODS]:
            rv, bv = _draw(rec, lot, nred, ninner)
            dz = {"红球": rec.红球, "蓝球": rec.蓝球}
            G = gate_of(rec.期号)
            for tks, A, B in ((tks_c, Ac, Bc), (tks_r, Ar, Br)):
                vec = G[(A @ rv).astype(np.int16), (B @ bv).astype(np.int16)]
                for i, t in enumerate(tks):
                    ref = TT.target_is_hit(lot, t, dz, rec.期号, target)
                    if bool(vec[i]) != bool(ref):
                        bad += 1
                        if first_bad is None:
                            first_bad = (str(rec.期号), i, bool(vec[i]), bool(ref), t)
        tag = "✓ 一致" if bad == 0 else f"✗ {bad} 处不一致"
        print(f"[{lot} {tier} K={K} {label}] 审计对账 {tag}", flush=True)
        if first_bad is not None:
            print(f"   首个反例 期号={first_bad[0]} 票#{first_bad[1]} "
                  f"向量化={first_bad[2]} 审计={first_bad[3]} 票={first_bad[4]}", flush=True)

        # ---- ② 回放（逐期按当期规则版本判级）
        from ev.profit.rules import rules_for as _rf
        vers = [_rf(lot, rec.期号).rule_version for rec in seq]
        t0 = time.time()
        n = len(seq)
        hc = np.zeros(n, dtype=bool)
        hr = np.zeros(n, dtype=bool)
        nc = np.zeros(n, dtype=np.int32)
        for p, rec in enumerate(seq):
            rv, bv = _draw(rec, lot, nred, ninner)
            G = gate_of(rec.期号)
            vc = G[(Ac @ rv).astype(np.int16), (Bc @ bv).astype(np.int16)]
            vr = G[(Ar @ rv).astype(np.int16), (Br @ bv).astype(np.int16)]
            hc[p], hr[p] = vc.any(), vr.any()
            nc[p] = vc.sum()
        dt = time.time() - t0

        # 规则版本构成（大乐透九级→七级，两套「四等」不是同一件事）
        from collections import Counter
        vc_cnt = Counter(vers)
        latest = vers[-1]
        mask = np.array([v == latest for v in vers])

        d = hc.astype(float) - hr.astype(float)
        m_c, m_r = float(hc.mean()), float(hr.mean())
        sd_d = float(d.std(ddof=1))
        se_d = sd_d / math.sqrt(n)
        diff = m_c - m_r
        v1, v2 = m_r * (1 - m_r), m_c * (1 - m_c)
        rho_hat = ((v1 + v2 - sd_d ** 2) / (2 * math.sqrt(v1 * v2))
                   if v1 > 0 and v2 > 0 else float("nan"))
        ex_c = exact_portfolio_coverage(lot, tks_c, target, iss)
        ex_r = exact_portfolio_coverage(lot, tks_r, target, iss)

        # 只取最新规则版本窗口：这样「精确值」与「实测值」才是同一套奖级
        nl = int(mask.sum())
        lc, lr = float(hc[mask].mean()), float(hr[mask].mean())
        dl = lc - lr
        sdl = float((hc[mask].astype(float) - hr[mask].astype(float)).std(ddof=1))
        sel = sdl / math.sqrt(nl)

        row = {
            "彩种": lot, "预算档": tier, "K": K, "主终点": label,
            "期数": n, "审计不一致": bad,
            "规则版本构成": dict(vc_cnt), "最新版本": latest, "最新版本期数": nl,
            # 全历史（按当期规则）
            "实测_随机": m_r, "实测_覆盖": m_c, "实测差": diff,
            "实测差_SE": se_d, "配对差SD": sd_d, "配对ρ估计": rho_hat,
            # 最新版本窗口（与精确值同口径）
            "窗口_随机": lr, "窗口_覆盖": lc, "窗口差": dl, "窗口差_SE": sel,
            # 精确值（最新版本）
            "精确_随机": ex_r, "精确_覆盖": ex_c, "精确差": ex_c - ex_r,
            "精确减窗口_随机": ex_r - lr, "精确减窗口_覆盖": ex_c - lc,
            "窗口抽样SE": math.sqrt(lr * (1 - lr) / nl) if nl else float("nan"),
            "覆盖臂单期平均达标注数": float(nc.mean()),
            "回放耗时秒": dt,
            "通道总数": meta["通道总数"], "覆盖通道数": meta["覆盖通道数"],
            "通道权重和": meta["通道权重和"],
        }
        row["n*_实测全历史"] = (float(((Z_A + Z_B) * sd_d / diff) ** 2)
                           if abs(diff) > 1e-15 else float("inf"))
        row["n*_实测窗口"] = (float(((Z_A + Z_B) * sdl / dl) ** 2)
                          if abs(dl) > 1e-15 else float("inf"))
        rows.append(row)

        print(f"   规则版本 {dict(vc_cnt)}；最新 {latest} 占 {nl}/{n} 期", flush=True)
        print(f"   {n} 期, 回放 {dt:.1f}s")
        print(f"   全历史  随机 {m_r:.4f} | 覆盖 {m_c:.4f} | 差 {diff*100:+.3f}pp "
              f"(SE {se_d*100:.3f}pp)", flush=True)
        if nl and nl < n:
            print(f"   最新版窗 {lr:.4f} | {lc:.4f} | 差 {dl*100:+.3f}pp "
                  f"(SE {sel*100:.3f}pp)")
        print(f"   精确(最新版) 随机 {ex_r:.4f} | 覆盖 {ex_c:.4f} | 差 "
              f"{(ex_c-ex_r)*100:+.3f}pp")
        print(f"   精确−窗口：随机 {ex_r-lr:+.4f}、覆盖 {ex_c-lc:+.4f} "
              f"(抽样 SE {row['窗口抽样SE']:.4f})")
        print(f"   配对差 SD 全历史 {sd_d:.4f} / 最新版窗 {sdl:.4f} "
              f"⇒ ρ̂ {rho_hat:+.3f} | 哨兵 {nc.mean():.2f} 注")
        print(f"   n* 全历史 {row['n*_实测全历史']:.0f} 期 / 最新版窗 "
              f"{row['n*_实测窗口']:.0f} 期", flush=True)
        print(flush=True)

    print("=" * 138)
    print(f"{'彩种':<6}{'档':<4}{'K':>5} {'主终点':<7}{'全历史差':>10}{'SE':>9}"
          f"{'最新版差':>10}{'SE':>9}{'精确差':>10}{'ρ̂':>7}{'SD(d)':>8}"
          f"{'n*全史':>9}{'n*窗口':>9}{'对账':>5}")
    print("-" * 138)
    for r in rows:
        print(f"{r['彩种']:<6}{r['预算档']:<4}{r['K']:>5} {r['主终点']:<8}"
              f"{r['实测差']*100:>+9.3f}pp{r['实测差_SE']*100:>8.3f}pp"
              f"{r['窗口差']*100:>+9.3f}pp{r['窗口差_SE']*100:>8.3f}pp"
              f"{r['精确差']*100:>+9.3f}pp"
              f"{r['配对ρ估计']:>7.3f}{r['配对差SD']:>8.3f}"
              f"{r['n*_实测全历史']:>9.0f}{r['n*_实测窗口']:>9.0f}"
              f"{r['审计不一致']:>5}")
    print("=" * 138)

    out = r"E:\707\output\stage0_replay.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"常量": {"POOL": POOL, "CAP": CAP,
                            "AUDIT_PERIODS": AUDIT_PERIODS}, "格": rows},
                  f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
