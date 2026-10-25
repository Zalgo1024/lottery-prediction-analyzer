# -*- coding: utf-8 -*-
"""pilot 结果诊断：实测差 vs 精确差，谁是可信效应量？

pilot 报出的实测 Δ 系统性大于精确 Δ（双色球 6 格全部如此），
且同格两 seed 的实测差能差 3.7pp。在宣布「可判」之前必须先回答：

  1. 实测−精确 是否超过抽样误差？超了就是口径问题，不是运气。
  2. 若口径不同，power 分析该用哪个效应量？
     ⇒ 用**精确差**（模型真值，无抽样噪声）+ **实测 SD(d)**（真实方差）。
     实测差含 ±SE 的抽样误差，拿它算 n* 会系统性低估所需期数。
  3. 同格两 seed 的实测差之差，说明「设计间变异」有多大？
     若设计间 SD ≳ 效应量，则**单次出票的结果不可外推**。
"""
from __future__ import annotations

import json
import math
import os
import sys

import numpy as np

sys.path.insert(0, r"E:\707")

Z_A, Z_B = 1.959964, 0.841621
SRC = r"E:\707\output\stage0_pilot.json"


def main() -> None:
    with open(SRC, encoding="utf-8") as f:
        data = json.load(f)
    rows = data["格"]

    print("=" * 140)
    print("① 实测差 vs 精确差（同一批票集）")
    print("=" * 140)
    print(f"{'彩种':<6}{'K':>5}{'sd':>3}{'实测差':>10}{'SE':>8}{'精确差':>10}"
          f"{'实测−精确':>11}{'z':>7}  {'判读'}")
    print("-" * 140)
    agg = {}
    for r in rows:
        se = r["实测差_SE"]
        gap = r["实测差"] - r["精确差"]
        z = gap / se if se > 0 else 0.0
        flag = "一致(≤2σ)" if abs(z) <= 2 else ("实测偏高" if z > 0 else "实测偏低")
        print(f"{r['彩种']:<6}{r['K']:>5}{r['seed']:>3}"
              f"{r['实测差']*100:>+9.3f}pp{se*100:>7.3f}pp"
              f"{r['精确差']*100:>+9.3f}pp{gap*100:>+10.3f}pp{z:>+7.2f}  {flag}")
        agg.setdefault((r["彩种"], r["K"]), []).append(r)
    print("=" * 140)
    print()

    print("=" * 140)
    print("② power 分析：效应量用**精确差**，方差用**实测 SD(d)**")
    print("=" * 140)
    print(f"{'彩种':<6}{'K':>5}{'预算':>7}{'精确差':>10}{'SD(d)':>8}{'SE':>8}"
          f"{'z_exact':>9}{'n*':>9}{'可用':>7}{'倍数':>7}  {'判决'}")
    print("-" * 140)
    out_rows = []
    for (lot, K), rs in sorted(agg.items()):
        ex = float(np.mean([r["精确差"] for r in rs]))
        sd = float(np.mean([r["配对差SD"] for r in rs]))
        n = rs[0]["可用期数"]
        se = sd / math.sqrt(n)
        z = ex / se if se > 0 else 0.0
        nstar = ((Z_A + Z_B) * sd / ex) ** 2 if abs(ex) > 1e-15 else float("inf")
        ok = math.isfinite(nstar) and nstar <= n
        # 设计间变异：同格两 seed 实测差的全距
        sp = max(r["实测差"] for r in rs) - min(r["实测差"] for r in rs)
        verdict = ("★可判" if ok else
                   ("边缘(1~3×)" if math.isfinite(nstar) and nstar <= 3 * n else "不可判"))
        out_rows.append({
            "彩种": lot, "K": K, "预算元": rs[0]["预算元"],
            "精确差": ex, "SD(d)": sd, "SE": se, "z_exact": z,
            "n*": nstar, "可用期数": n, "seed实测全距": sp, "判决": verdict,
        })
        mult = (nstar / n) if math.isfinite(nstar) else float("inf")
        print(f"{lot:<6}{K:>5}{rs[0]['预算元']:>7}"
              f"{ex*100:>+9.3f}pp{sd:>8.3f}{se*100:>7.3f}pp{z:>+9.2f}"
              f"{(format(nstar, '.0f') if math.isfinite(nstar) else 'inf'):>9}"
              f"{n:>7}{(format(mult, '.2f') if math.isfinite(mult) else 'inf'):>7}"
              f"  {verdict}")
    print("=" * 140)
    print()

    print("=" * 140)
    print("③ 设计间变异：同格换一个 seed，实测差能差多少？")
    print("=" * 140)
    print(f"{'彩种':<6}{'K':>5}{'seed0 Δ':>10}{'seed1 Δ':>10}{'全距':>9}"
          f"{'精确差均值':>11}  {'设计间SD/效应'}")
    print("-" * 140)
    for (lot, K), rs in sorted(agg.items()):
        ds = [r["实测差"] for r in rs]
        ex = float(np.mean([r["精确差"] for r in rs]))
        sp = max(ds) - min(ds)
        # 两 seed 实测差之差的标准误：sd(d)/sqrt(n) * sqrt(2)（近似独立）
        sd = float(np.mean([r["配对差SD"] for r in rs]))
        n = rs[0]["可用期数"]
        se_sp = sd / math.sqrt(n) * math.sqrt(2)
        ratio = (sp / abs(ex)) if abs(ex) > 1e-12 else float("nan")
        note = ""
        if abs(ex) > 1e-12 and sp > abs(ex):
            note = f"  ⚠ 设计间全距({sp*100:.2f}pp) > 效应({ex*100:.2f}pp)"
        elif abs(ex) > 1e-12:
            note = ""
        print(f"{lot:<6}{K:>5}{ds[0]*100:>+9.3f}pp"
              f"{(ds[1]*100 if len(ds) > 1 else float('nan')):>+9.3f}pp"
              f"{sp*100:>+8.3f}pp{ex*100:>+10.3f}pp"
              f"{ratio:>9.2f}{note}")
    print("=" * 140)
    print("注：全距含抽样误差（SE_sp ≈ sd·√2/√n），所以设计间变异被**高估**；")
    print("    这里只报量级，不作检验。")
    print()

    out = r"E:\707\output\stage0_pilot_diag.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"power": out_rows}, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
