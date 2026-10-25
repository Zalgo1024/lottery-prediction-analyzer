# -*- coding: utf-8 -*-
"""Stage 0 判决汇总：把三份探针结果合成「可判 / 不可判」表。

判决只用两个量
--------------
· **实测效应**  = Δ真实（`exact_portfolio_coverage` 两臂之差，已与真实开奖对过账）
· **oracle 效应** = Δmax（不相交上界，任何选号器都拿不到的乐观上限）
· **配对差 SD** = 真实回放实测（ρ̂≈0 ⇒ 不能用配对救功效）
· n* = ((z.975 + z.80) · SD(d) / 效应)²，80% 功效、双侧 α=0.05
"""
from __future__ import annotations

import json
import math

Z_A, Z_B = 1.959964, 0.841621


def load(p):
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def fmt(x, unit=""):
    if x is None:
        return "—"
    if not math.isfinite(x):
        return "inf"
    if abs(x) >= 1e6:
        return f"{x:.2e}"
    return f"{x:.0f}"


def main() -> None:
    ana = load(r"E:\707\output\stage0_analytic.json")
    exa = load(r"E:\707\output\stage0_exact.json")
    rep = load(r"E:\707\output\stage0_replay.json")

    # 只取「全段」窗（random vs tier_cov 都不拟合参数）
    a_by = {(r["彩种"], r["预算档"], r["K"]): r for r in ana["格"] if r["窗"] == "全段"}
    e_by = {(r["彩种"], r["预算档"], r["K"]): r for r in exa["格"]}
    r_by = {(r["彩种"], r["预算档"], r["K"]): r for r in rep["格"]}

    print("=" * 132)
    print("Stage 0 判决表（双色球 3518 期 / 大乐透 2938 期；只比 tier_cov vs random）")
    print("=" * 132)
    hdr = (f"{'格':<30}{'实测效应':>10}{'oracle':>10}{'SD(d)':>8}{'ρ̂':>7}"
           f"{'n*实测':>10}{'n*oracle':>11}{'可用':>6}{'缺口':>9}  判决")
    print(hdr)
    print("-" * 132)

    out = []
    for k, a in a_by.items():
        e, rp = e_by[k], r_by[k]
        d_true = e["Δ真实"]
        dmax = a["Δmax"]
        sat = a["Kp饱和"]
        sd = rp["配对差SD"]
        avail = a["可用期数"]

        def nstar(eff):
            if eff <= 0:
                return float("inf")
            return ((Z_A + Z_B) * sd / eff) ** 2

        n_true = nstar(d_true)
        n_orac = nstar(dmax) if not sat else float("inf")
        name = f"{k[0]} {k[1]} K={k[2]}"
        # oracle 也不够 ⇒ 无论选号器多好都判不出
        if sat:
            verdict = "oracle 上界为空 ⇒ 只能实测"
        elif n_orac > avail:
            verdict = f"不可判（oracle 也差 {n_orac/avail:.1f}×）"
        elif n_true > avail:
            verdict = f"不可判（实测差 {n_true/avail:.0f}×）"
        else:
            verdict = "可判"
        gap = n_true / avail if math.isfinite(n_true) else float("inf")
        print(f"{name:<30}{d_true*100:>+9.3f}pp{dmax*100:>9.3f}pp{sd:>8.3f}"
              f"{rp['配对ρ估计']:>7.3f}"
              f"{fmt(n_true):>10}{fmt(n_orac):>11}{avail:>6}"
              f"{(f'{gap:.0f}×' if math.isfinite(gap) else 'inf'):>9}  {verdict}")
        out.append({
            "格": name, "实测效应": d_true, "oracle效应": None if sat else dmax,
            "配对差SD": sd, "rho": rp["配对ρ估计"], "可用期数": avail,
            "n*实测": None if not math.isfinite(n_true) else n_true,
            "n*oracle": None if not math.isfinite(n_orac) else n_orac,
            "缺口倍数": None if not math.isfinite(gap) else gap,
            "判决": verdict,
            "审计不一致": rp["审计不一致"],
        })
    print("=" * 132)
    print("实测效应 = 精确口径的两臂差（已对真实开奖核对，偏差 ≤2σ）。")
    print("oracle效应 = 不相交上界 Kp−(1−(1−p)^K)，任何选号器都达不到 ⇒ 用它算的 n* 是**乐观下限**。")
    print("ρ̂ ≈ 0 ⇒ 配对设计不降低差值方差，计划 §2.2「配对会救回功效」被实测否定。")
    print()

    # 关键结论：oracle 也不够的格子
    infeasible = [o for o in out if o["n*oracle"] is None or o["n*oracle"] > o["可用期数"]]
    print(f"★ oracle 上界下仍不可判：{len(infeasible)}/{len(out)} 格")
    for o in infeasible:
        no = o["n*oracle"]
        print(f"   · {o['格']}: oracle 需 "
              f"{'∞（上界饱和）' if no is None else f'{no:.0f}'} 期 vs 可用 "
              f"{o['可用期数']} 期")
    print()

    with open(r"E:\707\output\stage0_verdict.json", "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    print(r"已写 E:\707\output\stage0_verdict.json")


if __name__ == "__main__":
    main()
