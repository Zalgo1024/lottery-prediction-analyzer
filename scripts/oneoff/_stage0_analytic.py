# -*- coding: utf-8 -*-
"""Stage 0 解析可行性预检（解析量，不跑策略）。

对每个 格 = (彩种, 预算档, 主终点) × 每个 窗 = (全段 / β段) 算：
    p     = 单注达标概率
    Kp    = min(1, K*p)            不相交上界（K*p>=1 ⇒ 饱和，Δmax 无效）
    base  = 1-(1-p)**K             随机基线（近似）
    Dmax  = Kp - base              覆盖最多能拿到的差
    sd_d(rho) = sqrt(V1+V2-2*rho*sqrt(V1*V2))   配对差序列的单期 SD
    n*(rho)   = ((z_.975+z_.80)*sd_d/Dmax)**2
    临界rho   = 使 n* 恰好等于可用期数的配对相关系数

★ 两套窗必须分开算（关键）：
   「全段」= random vs tier_cov：两臂都不拟合任何参数 ⇒ 可用全部期数。
   「β段」= 含 lowcrowd/prod_mix/hybrid/profit：这些臂要 fit β，前 MIN_TRAIN 期无分位
            ⇒ 可用 n - MIN_TRAIN 期。统一拿窄窗去算主假设的功效会低估一半。

rho 未知（正是 pilot 要测的）⇒ 给 0 / 0.5 / 0.85 三档，别当结论用。
"""
from __future__ import annotations

import json
import math
import os
import sys

sys.path.insert(0, r"E:\707")

import ev.tier_target as TT
from ev.tier_backtest import load_test_recs
from ev.lowcrowd_select import _greedy_capacity
from ev.coverage_backtest import build_pool

Z_A = 1.959964       # 双侧 alpha=0.05
Z_B = 0.841621       # 单侧 80% 功效
POOL = 8000
CAP = 3
MIN_TRAIN = 1200     # ev/popularity_proxy.MIN_TRAIN

CELLS = [
    # (彩种, 预算档, K, target, 主终点描述)
    ("双色球", "生产", 200, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("双色球", "抬高", 360, {"kind": "grade", "grade": "四等"}, "≥四等"),
    ("大乐透", "生产", 38, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("大乐透", "抬高", 1120, {"kind": "grade", "grade": "四等"}, "≥四等"),
]
WINDOWS = ("全段", "β段")
RHOS = (0.0, 0.5, 0.85)


def sd_diff(p1: float, p2: float, rho: float) -> float:
    v1 = p1 * (1.0 - p1)
    v2 = p2 * (1.0 - p2)
    v = v1 + v2 - 2.0 * rho * math.sqrt(v1 * v2)
    return math.sqrt(max(v, 0.0))


def n_star(sd: float, dmax: float) -> float:
    if dmax <= 0:
        return float("inf")
    return ((Z_A + Z_B) * sd / dmax) ** 2


def rho_crit(base: float, kp: float, dmax: float, avail: int):
    """使 n* 恰好 == avail 的配对相关 ρ（解析式）。"""
    if dmax <= 0 or kp >= 1.0 - 1e-12 or avail <= 0:
        return None
    need_sd = math.sqrt(avail) * dmax / (Z_A + Z_B)
    v1 = base * (1 - base)
    v2 = kp * (1 - kp)
    if v1 <= 0 or v2 <= 0:
        return None
    return (v1 + v2 - need_sd ** 2) / (2 * math.sqrt(v1 * v2))


def main() -> None:
    recs, avail = {}, {}
    for lot in ("双色球", "大乐透"):
        r = load_test_recs(lot)
        recs[lot] = r
        avail[lot] = {"全段": len(r), "β段": max(0, len(r) - MIN_TRAIN)}
        print(f"[数据] {lot}: 共 {len(r)} 期（{r[0].期号}→{r[-1].期号}）"
              f" | 全段 {avail[lot]['全段']} 期 | β段 {avail[lot]['β段']} 期"
              f"（扣 MIN_TRAIN={MIN_TRAIN}）")
    print()

    caps = {}
    for lot in ("双色球", "大乐透"):
        pool = build_pool(lot, POOL, seed=20261024)
        full = _greedy_capacity(pool, CAP)
        print(f"[容量] {lot} pool={POOL} cap={CAP}: 整池贪心容量 = {full} 注"
              f"（★ 均匀随机池；门控后的真实容量只会更小）")
        for K in sorted({c[2] for c in CELLS if c[0] == lot}):
            caps[(lot, K)] = full
            print(f"       K={K}: 需求 K/0.6 = {K/0.6:.0f} ⇒ "
                  f"{'通过' if full >= K/0.6 else '不足'}")
    print()

    rows = []
    for lot, tier, K, target, label in CELLS:
        r = recs[lot]
        res = TT.resolve_conditions(lot, target, r[-1].期号)
        p = TT.single_ticket_probability(lot, res["conditions"], r[-1].期号)
        kp = min(1.0, K * p)
        base = 1.0 - (1.0 - p) ** K
        dmax = kp - base
        saturated = kp >= 1.0 - 1e-12

        for win in WINDOWS:
            av = avail[lot][win]
            row = {
                "彩种": lot, "预算档": tier, "K": K, "主终点": label, "窗": win,
                "规则版本": res["rule_version"],
                "达标奖级": sorted(res["grades"]),
                "条件格数": len(res["conditions"]),
                "单注p": p, "Kp": kp, "Kp饱和": saturated,
                "随机基线": base, "Δmax": dmax,
                "可用期数": av,
                "容量": caps[(lot, K)], "容量需求": K / 0.6,
                "判据1": dmax <= 0.005,
                "判据3": caps[(lot, K)] < K / 0.6,
                "临界ρ": rho_crit(base, kp, dmax, av),
            }
            for rho in RHOS:
                sd = sd_diff(base, kp, rho)
                row[f"sd(ρ={rho})"] = sd
                row[f"n*(ρ={rho})"] = n_star(sd, dmax)

            if row["判据1"] or row["判据3"]:
                row["结论"] = "不可判（硬判据）"
            elif saturated:
                row["结论"] = "解析失效·须 pilot"
            elif row["n*(ρ=0.0)"] <= av:
                row["结论"] = "可判（ρ=0 就够）"
            elif row["n*(ρ=0.85)"] > av:
                row["结论"] = f"不可判（ρ=0.85 需 {row['n*(ρ=0.85)']:.0f} 期 > {av}）"
            else:
                row["结论"] = f"边缘（需 ρ≥{row['临界ρ']:.2f}）"
            rows.append(row)

    print("=" * 142)
    print(f"{'彩种':<6}{'档':<4}{'K':>5} {'主终点':<7}{'窗':<5}{'单注p':>10}"
          f"{'K·p':>7}{'基线':>8}{'Δmax':>10}{'可用':>6}{'容量':>6}"
          f"{'n*(ρ0)':>8}{'n*(ρ.5)':>9}{'n*(ρ.85)':>9}{'临界ρ':>7}  {'结论':<26}")
    print("-" * 142)
    for r in rows:
        dm = "   饱和!" if r["Kp饱和"] else f"{r['Δmax']*100:>7.3f}pp"
        rc = "  —  " if r["临界ρ"] is None else f"{r['临界ρ']:>6.2f}"
        print(f"{r['彩种']:<6}{r['预算档']:<4}{r['K']:>5} {r['主终点']:<8}{r['窗']:<6}"
              f"{r['单注p']:>10.2e}{r['Kp']:>7.3f}{r['随机基线']:>8.4f}{dm:>10}"
              f"{r['可用期数']:>6}{r['容量']:>6}"
              f"{r['n*(ρ=0.0)']:>8.0f}{r['n*(ρ=0.5)']:>9.0f}{r['n*(ρ=0.85)']:>9.0f}"
              f"{rc:>7}  {r['结论']:<26}")
    print("=" * 142)
    print("注：Δmax 单位 pp；n* 为所需期数（80% 功效、双侧 α=0.05）。")
    print("★ K·p ≥ 1 ⇒ 不相交上界饱和为 1，Δmax 是虚构值 ⇒ 解析式不可用，只能靠 pilot 实测臂间差。")
    print("★ 全段窗适用 random vs tier_cov（都不拟合参数）；β段窗适用于含 lowcrowd/prod_mix/hybrid/profit 的对照。")
    print()

    for lot, tier, K, target, label in CELLS:
        print(f"### {lot} {tier} K={K} {label}")
        for r in [x for x in rows if (x["彩种"], x["预算档"], x["K"]) == (lot, tier, K)]:
            dm_txt = "饱和（K·p ≥ 1）" if r["Kp饱和"] else f"{r['Δmax']*100:.3f}pp"
            print(f"  [{r['窗']}] 可用 {r['可用期数']} 期 | p={r['单注p']:.4e} "
                  f"K·p={r['Kp']:.4f} 基线={r['随机基线']:.4f} Δmax={dm_txt}")
            if not r["Kp饱和"]:
                line = (f"         sd: {r['sd(ρ=0.0)']:.4f}/{r['sd(ρ=0.5)']:.4f}/"
                        f"{r['sd(ρ=0.85)']:.4f} ↦ n*: {r['n*(ρ=0.0)']:.0f}/"
                        f"{r['n*(ρ=0.5)']:.0f}/{r['n*(ρ=0.85)']:.0f}")
                if r["临界ρ"] is not None:
                    line += f" | 临界ρ={r['临界ρ']:.3f}"
                print(line)
            print(f"         ⇒ {r['结论']}")
        print()

    out = r"E:\707\output\stage0_analytic.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"常量": {"POOL": POOL, "CAP": CAP, "MIN_TRAIN": MIN_TRAIN,
                            "Z_A": Z_A, "Z_B": Z_B, "RHOS": RHOS},
                   "可用期数": avail, "格": rows}, f,
                  ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
