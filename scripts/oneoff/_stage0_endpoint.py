# -*- coding: utf-8 -*-
"""Stage 0 补充：换终点能不能救回功效。

`达标期占比` 是二值终点 ⇒ 单期方差 p(1-p)，且 ρ̂≈0 拿不到配对收益。
但 `exact_metrics` 还能给**达标注数分布**（精确，非模拟）：
若两臂的「单期达标注数均值」差得比比例大，且计数方差更小，计数终点的功效会高得多。

本脚本对每格、两臂算：
    P_达标（二值终点）
    E[达标注数]、Var、SD（计数终点，由精确分布给出）
    两个终点各自的 n*（80% 功效、双侧 α=0.05）
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, r"E:\707")

import ev.tier_target as TT
from ev.profit import evaluate as EV
from ev.profit import payouts as P
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import select_tier_coverage
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool

POOL = 8000
CAP = 3
Z_A, Z_B = 1.959964, 0.841621

CELLS = [
    ("双色球", "生产", 200, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("双色球", "抬高", 360, {"kind": "grade", "grade": "四等"}, "≥四等"),
    ("大乐透", "生产", 38, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("大乐透", "抬高", 1120, {"kind": "grade", "grade": "四等"}, "≥四等"),
]


def stats(lottery, tks, target, issue):
    res = TT.resolve_conditions(lottery, target, issue)
    order = [g for g in res["grades"] if g in TT._GRADE_ORDER]
    worst = max(order, key=TT._GRADE_ORDER.index)
    ctx = P.build_ctx(lottery, issue)
    m = EV.exact_metrics(lottery, list(tks), ctx, kappa=0.0,
                         want_dist=True, min_grade=worst)
    dist = m["达标注数分布"]
    mean = sum(k * v for k, v in dist.items())
    var = sum((k - mean) ** 2 * v for k, v in dist.items())
    return {
        "P": float(m["P_达标"]),
        "E注数": mean,
        "Var注数": var,
        "SD注数": math.sqrt(var),
        "P注数≥1": float(1.0 - dist.get(0, 0.0)),
        "最大注数": max(dist) if dist else 0,
        "worst": worst,
    }


def main() -> None:
    pools = {lot: build_pool(lot, POOL, seed=20261024) for lot in ("双色球", "大乐透")}
    iss = {lot: load_test_recs(lot)[-1].期号 for lot in ("双色球", "大乐透")}

    rows = []
    for lot, tier, K, target, label in CELLS:
        tc, _ = select_tier_coverage(lot, pools[lot], K, target, iss[lot], cap=CAP, seed=0)
        tr = control_tickets(lot, pools[lot], K, seed=0, cap=CAP)
        sc, sr = stats(lot, tc, target, iss[lot]), stats(lot, tr, target, iss[lot])

        dP = sc["P"] - sr["P"]
        dN = sc["E注数"] - sr["E注数"]
        # 二值终点：sd(d) = sqrt(Vc+Vr-2ρ√(VcVr))，ρ̂≈0 ⇒ 取 ρ=0
        sP = math.sqrt(sc["P"] * (1 - sc["P"]) + sr["P"] * (1 - sr["P"]))
        # 计数终点：同样取 ρ=0（保守，实测 ρ̂ 也≈0）
        sN = math.sqrt(sc["Var注数"] + sr["Var注数"])

        nP = ((Z_A + Z_B) * sP / dP) ** 2 if dP > 0 else float("inf")
        nN = ((Z_A + Z_B) * sN / dN) ** 2 if dN > 0 else float("inf")

        rows.append({
            "彩种": lot, "预算档": tier, "K": K, "主终点": label,
            "P_随机": sr["P"], "P_覆盖": sc["P"], "ΔP": dP,
            "E注数_随机": sr["E注数"], "E注数_覆盖": sc["E注数"], "ΔE注数": dN,
            "相对增幅": (dN / sr["E注数"]) if sr["E注数"] > 0 else float("nan"),
            "SD注数_随机": sr["SD注数"], "SD注数_覆盖": sc["SD注数"],
            "n*_二值": nP, "n*_计数": nN,
        })
        print(f"[{lot} {tier} K={K} {label}]  达标线 = {sc['worst']}")
        print(f"   二值终点 P: 随机 {sr['P']:.4f} → 覆盖 {sc['P']:.4f} "
              f"(Δ {dP*100:+.3f}pp), sd {sP:.4f} ⇒ n* = "
              f"{nP:.0f}" if math.isfinite(nP) else "   ∞")
        print(f"   计数终点 E: 随机 {sr['E注数']:.4f} → 覆盖 {sc['E注数']:.4f} "
              f"(Δ {dN:+.4f} 注, 相对 {100*dN/sr['E注数']:+.2f}%)")
        print(f"              SD {sr['SD注数']:.4f}/{sc['SD注数']:.4f}, "
              f"合并 {sN:.4f} ⇒ n* = {nN:.0f}")
        print(f"   计数终点相对二值的功效倍数 = "
              f"{(nP/nN if math.isfinite(nP) and math.isfinite(nN) else float('nan')):.2f}")
        print()

    print("=" * 118)
    print(f"{'彩种':<6}{'档':<4}{'K':>5} {'主终点':<7}{'ΔP':>10}{'ΔE注数':>10}"
          f"{'相对':>8}{'SD注数':>8}{'n*二值':>12}{'n*计数':>10}{'倍数':>7}")
    print("-" * 118)
    for r in rows:
        nP, nN = r["n*_二值"], r["n*_计数"]
        print(f"{r['彩种']:<6}{r['预算档']:<4}{r['K']:>5} {r['主终点']:<8}"
              f"{r['ΔP']*100:>+9.3f}pp{r['ΔE注数']:>10.4f}"
              f"{r['相对增幅']*100:>+7.2f}%"
              f"{math.sqrt(0.5*(r['SD注数_随机']**2+r['SD注数_覆盖']**2)):>8.3f}"
              f"{(f'{nP:.0f}' if math.isfinite(nP) else 'inf'):>12}"
              f"{(f'{nN:.0f}' if math.isfinite(nN) else 'inf'):>10}"
              f"{(nP/nN if math.isfinite(nP) and math.isfinite(nN) else float('nan')):>7.2f}")
    print("=" * 118)

    out = r"E:\707\output\stage0_endpoint.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
