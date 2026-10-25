# -*- coding: utf-8 -*-
"""Stage 0 精确口径：两臂票集的**模型精确** P(至少一注达标)。

为什么这个比 Δmax 硬：
  `Δmax = Kp - (1-(1-p)^K)` 里的 Kp 是「不相交上界」，在 K·p≥1 时饱和、在
  Σ通道权重 ≥ Kp 时退化成 Kp 本身，都不等于**可达**上限。
  而 `exact_portfolio_coverage` 对一组给定票集直接算精确概率
  （κ=0 时外层权重均匀 ≡ 公平开奖，不是独立性近似）⇒
      Δ真实 = P_exact(tier_cov 票集) - P_exact(random 票集)
  这就是该格「覆盖策略最多能拿多少」的可达值，用来替换 Δmax 重算 n*。

★ 注意：`select_tier_coverage` 除了规则版本之外**不看任何历史**
  ⇒ 同一规则版本下每期票集完全相同（重选只换 seed）。故本脚本每格只算一次。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time

sys.path.insert(0, r"E:\707")

import ev.tier_target as TT
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import select_tier_coverage, exact_portfolio_coverage
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool

POOL = 8000
CAP = 3
MIN_TRAIN = 1200
Z_A, Z_B = 1.959964, 0.841621

CELLS = [
    ("双色球", "生产", 200, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("双色球", "抬高", 360, {"kind": "grade", "grade": "四等"}, "≥四等"),
    ("大乐透", "生产", 38, {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("大乐透", "抬高", 1120, {"kind": "grade", "grade": "四等"}, "≥四等"),
]


def main() -> None:
    pools, issues, navail = {}, {}, {}
    for lot in ("双色球", "大乐透"):
        pools[lot] = build_pool(lot, POOL, seed=20261024)
        recs = load_test_recs(lot)
        issues[lot] = recs[-1].期号
        navail[lot] = {"全段": len(recs), "β段": max(0, len(recs) - MIN_TRAIN)}

    rows = []
    for lot, tier, K, target, label in CELLS:
        pool, iss = pools[lot], issues[lot]
        tks_r = control_tickets(lot, pool, K, seed=0, cap=CAP)
        tks_c, meta = select_tier_coverage(lot, pool, K, target, iss, cap=CAP, seed=0)

        t0 = time.time()
        p_r = exact_portfolio_coverage(lot, tks_r, target, iss, kappa=0.0)
        t_r = time.time() - t0
        t0 = time.time()
        p_c = exact_portfolio_coverage(lot, tks_c, target, iss, kappa=0.0)
        t_c = time.time() - t0

        d_real = p_c - p_r
        row = {
            "彩种": lot, "预算档": tier, "K": K, "主终点": label,
            "规则版本": meta["规则版本"],
            "P_随机票集": p_r, "P_覆盖票集": p_c, "Δ真实": d_real,
            "单注p": meta["单注达标概率"],
            "解析基线": meta["随机基线"],
            "Kp": meta["理论上限Kp"],
            "Σ通道权重": meta["通道权重和"],
            "耗时_随机": t_r, "耗时_覆盖": t_c,
            "n_full": navail[lot]["全段"], "n_beta": navail[lot]["β段"],
        }
        # 用 Δ真实 重算 n*：sd 取两臂各自伯努利的独立和（保守）
        v1, v2 = p_r * (1 - p_r), p_c * (1 - p_c)
        for name, rho in (("ρ0", 0.0), ("ρ.5", 0.5), ("ρ.85", 0.85)):
            sd = math.sqrt(max(v1 + v2 - 2 * rho * math.sqrt(v1 * v2), 0.0))
            row[f"sd_{name}"] = sd
            row[f"n*_{name}"] = (((Z_A + Z_B) * sd / d_real) ** 2
                                 if d_real > 0 else float("inf"))
        rows.append(row)

        print(f"[{lot} {tier} K={K} {label}]  规则版本 {meta['规则版本']}")
        print(f"   P_exact 随机票集 = {p_r:.6f}  ({t_r:.1f}s)")
        print(f"   P_exact 覆盖票集 = {p_c:.6f}  ({t_c:.1f}s)")
        print(f"   Δ真实 = {d_real*100:+.3f}pp"
              f"   （解析基线 {meta['随机基线']:.4f} / Kp {meta['理论上限Kp']:.4f}"
              f" / Δmax {(meta['理论上限Kp']-meta['随机基线'])*100:.3f}pp）")
        print(f"   按 Δ真实 重算 n*: ρ=0 → {row['n*_ρ0']:.0f}; "
              f"ρ=0.5 → {row['n*_ρ.5']:.0f}; ρ=0.85 → {row['n*_ρ.85']:.0f}")
        print(f"   可用: 全段 {row['n_full']} 期 / β段 {row['n_beta']} 期")
        print()

    print("=" * 118)
    print(f"{'彩种':<6}{'档':<4}{'K':>5} {'主终点':<7}{'P随机':>10}{'P覆盖':>10}"
          f"{'Δ真实':>10}{'解析Δmax':>10}{'捕获率':>8}{'n*(ρ0)':>9}{'n*(.5)':>9}"
          f"{'n*(.85)':>9}{'全段':>6}{'β段':>6}")
    print("-" * 118)
    for r in rows:
        dmax = r["Kp"] - r["解析基线"]
        cap_rate = (r["Δ真实"] / dmax * 100) if dmax > 0 else float("nan")
        print(f"{r['彩种']:<6}{r['预算档']:<4}{r['K']:>5} {r['主终点']:<8}"
              f"{r['P_随机票集']:>10.5f}{r['P_覆盖票集']:>10.5f}"
              f"{r['Δ真实']*100:>+9.3f}pp{dmax*100:>9.3f}pp"
              f"{cap_rate:>7.1f}%{r['n*_ρ0']:>9.0f}{r['n*_ρ.5']:>9.0f}"
              f"{r['n*_ρ.85']:>9.0f}{r['n_full']:>6}{r['n_beta']:>6}")
    print("=" * 118)
    print("捕获率 = Δ真实 / Δmax：覆盖贪心把解析上界兑现了多少。")
    print("Δ真实 ≤ 0 ⇒ 覆盖票集不比随机票集好，该格**不需要实验**（解析已否决）。")
    print()

    out = r"E:\707\output\stage0_exact.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"常量": {"POOL": POOL, "CAP": CAP, "MIN_TRAIN": MIN_TRAIN},
                   "格": rows}, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
