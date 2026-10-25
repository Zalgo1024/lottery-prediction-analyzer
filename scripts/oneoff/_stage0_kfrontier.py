# -*- coding: utf-8 -*-
"""Stage 0 补充：K–P(并集) 前沿（带有效性校验）。

把「统计上判不出来」换成「结构上要多少注」。

★ 三个必须先过的有效性门（否则点数无意义）：
  1. `len(覆盖票) == K`：少给注数 ⇒ 成本口径被污染
  2. `放宽次数 == 0`：`select_tier_coverage` 的 RELAX_MAX=4 会把 cap 偷偷抬到 7
  3. `len(随机票) == K`：随机臂凑不满时两臂注数不同，比的是别的东西

★ 已知硬约束：`select_tier_coverage` 内部 `pool[:MAX_POOL=8000]`，
  双色球 cap=3 下该池的贪心容量只有 649 注 ⇒ K>649 在双色球上**结构上不可达**。
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, r"E:\707")

import ev.tier_target as TT
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import select_tier_coverage, exact_portfolio_coverage
from ev.selection import control_tickets
from ev.coverage_backtest import build_pool
from ev.lowcrowd_select import _greedy_capacity

POOL = 8000            # 与 MAX_POOL 一致：再大也会被截断
CAP = 3
KS = {
    "双色球": [10, 25, 50, 100, 200, 300, 400, 600],
    "大乐透": [10, 25, 50, 100, 200, 400, 800, 1600],
}
TARGETS = [
    ("双色球", {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("双色球", {"kind": "grade", "grade": "四等"}, "≥四等"),
    ("大乐透", {"kind": "payout", "min_payout": 10.0}, "≥10元"),
    ("大乐透", {"kind": "grade", "grade": "四等"}, "≥四等"),
]


def main() -> None:
    pools = {lot: build_pool(lot, POOL, seed=20261024)
             for lot in ("双色球", "大乐透")}
    iss = {lot: load_test_recs(lot)[-1].期号 for lot in ("双色球", "大乐透")}

    # 先报容量（硬约束）
    for lot in ("双色球", "大乐透"):
        c = _greedy_capacity(pools[lot], CAP)
        print(f"[容量] {lot} pool={POOL}（=MAX_POOL）cap={CAP} 贪心容量 = {c} 注"
              f" ⇒ K>{c} 结构上不可达")
    print()

    all_rows = []
    for lot, target, label in TARGETS:
        res = TT.resolve_conditions(lot, target, iss[lot])
        p1 = TT.single_ticket_probability(lot, res["conditions"], iss[lot])
        print(f"### {lot} {label}  单注 p={p1:.4e}  {res['rule_version']}")
        print(f"{'K':>5}{'P随机':>9}{'P覆盖':>9}{'Δ真实':>9}"
              f"{'独立基线':>9}{'注数':>6}{'放宽':>5}  有效")
        rows = []
        for K in KS[lot]:
            tc, meta = select_tier_coverage(lot, pools[lot], K, target,
                                           iss[lot], cap=CAP, seed=0)
            tr = control_tickets(lot, pools[lot], K, seed=0, cap=CAP)
            valid = (len(tc) == K and meta["放宽次数"] == 0 and len(tr) == K)
            why = []
            if len(tc) != K:
                why.append(f"覆盖只有 {len(tc)}")
            if meta["放宽次数"]:
                why.append(f"放宽 {meta['放宽次数']} 次(cap→{meta['重叠上限']})")
            if len(tr) != K:
                why.append(f"随机只有 {len(tr)}")
            if valid:
                pc = exact_portfolio_coverage(lot, tc, target, iss[lot])
                pr = exact_portfolio_coverage(lot, tr, target, iss[lot])
            else:
                pc = pr = float("nan")
            base = 1.0 - (1.0 - p1) ** K
            rows.append({"K": K, "P随机": pr, "P覆盖": pc,
                         "Δ真实": (pc - pr) if valid else None,
                         "独立基线": base, "有效": valid, "原因": ";".join(why)})
            print(f"{K:>5}{(pr if valid else float('nan')):>9.4f}"
                  f"{(pc if valid else float('nan')):>9.4f}"
                  f"{((pc-pr)*100 if valid else float('nan')):>+8.2f}pp"
                  f"{base:>9.4f}{len(tc):>6}{meta['放宽次数']:>5}  "
                  f"{'✓' if valid else '✗ ' + ';'.join(why)}")
        all_rows.append({"彩种": lot, "目标": label, "单注p": p1, "曲线": rows})
        print()

    print("=" * 100)
    print("各目标的有效增益峰值（不给外推，只报实测到的最大点）")
    for blk in all_rows:
        pts = [(r["K"], r["Δ真实"] * 100) for r in blk["曲线"]
               if r["有效"] and r["Δ真实"] is not None]
        if not pts:
            print(f"  {blk['彩种']} {blk['目标']}: 无有效点")
            continue
        kbest, dbest = max(pts, key=lambda x: x[1])
        print(f"  {blk['彩种']} {blk['目标']}: 峰值 Δ={dbest:+.2f}pp @ K={kbest}  "
              f"| 有效点 K ∈ {[k for k, _ in pts]}")
        print(f"      全曲线 " + " ".join(f"K{k}:{d:+.2f}" for k, d in pts))
    print("=" * 100)

    out = r"E:\707\output\stage0_kfrontier.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"常量": {"POOL": POOL, "CAP": CAP, "KS": KS},
                   "目标": all_rows}, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
