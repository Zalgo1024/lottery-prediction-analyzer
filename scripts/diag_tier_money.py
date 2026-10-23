# -*- coding: utf-8 -*-
"""诊断：达标回测里「中奖注数一致、净收益却差 19 pp」到底是运气还是实现差异。

## 为什么需要这个脚本

`ev/tier_backtest._replay` 的 `净收益/100元` 用的是**已实现实得**。双色球 K=200 / 500 期
（10 万注）下「三等」期望命中只有 **0.91 次**，而每中一次 = 3000 元 = 1.5 pp。
于是某臂中 2 次、另一臂中 0 次，就能凭空拉开 3 pp 并翻转臂间排序 —— 这不是策略差异。

本脚本把**每臂每奖级的命中次数**直接对齐解析概率，并给出「剔除单期最大实得后」
的净收益，用来区分：
  · 判级分布有问题（实现偏差）→ 实测/期望比系统性偏离 1；
  · 只是稀有奖抽签 → 各奖级比值都 ≈1，剔除最大期后各臂一起收敛。

## 用法

    E:/Python/python.exe scripts/diag_tier_money.py [彩种] [K] [期数] [seed]

默认 双色球 200 期 500 seed 0。实测（2026-10-23）：
    random           -74.25  三等 2 次  剔除最大期 -75.78
    existing_coverage -76.94 三等 0 次  剔除最大期 -77.17
    tier_coverage    -74.50  三等 2 次  剔除最大期 -76.03
    ⇒ 三臂同分布，差异全部来自三等中奖次数（期望 0.91 次）。
"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOT = sys.argv[1] if len(sys.argv) > 1 else "双色球"
K = int(sys.argv[2]) if len(sys.argv) > 2 else 200
PERIODS = int(sys.argv[3]) if len(sys.argv) > 3 else 500
SEED = int(sys.argv[4]) if len(sys.argv) > 4 else 0
RESELECT = 25
POOL = 2000
_ALL_GRADES = ("一等", "二等", "三等", "四等", "五等", "六等", "七等", "八等", "九等")


def main() -> int:
    from ev.coverage_backtest import build_pool, grade_ticket
    from ev.tier_backtest import _select, load_test_recs

    recs = load_test_recs(LOT, PERIODS)
    if not recs:
        print(f"无开奖记录：{LOT}")
        return 1
    print(f"{LOT} 期数={len(recs)} K={K} seed={SEED} 池={POOL} 重选间隔={RESELECT}")

    pp = _analytic_probabilities(LOT)
    for arm in ("random", "existing_coverage", "tier_coverage"):
        cache = {}
        grades = Counter()
        total = 0.0
        staked = 0
        per_issue = []
        for blk, start in enumerate(range(0, len(recs), RESELECT)):
            chunk = recs[start:start + RESELECT]
            pool = build_pool(LOT, POOL, seed=SEED * 100003 + blk)
            tks, _ = _select(arm, LOT, pool, K, "四等", chunk[0].期号,
                             seed=SEED * 1009 + blk, cap=3)
            for rec in chunk:
                pay = 0.0
                for t in tks:
                    tier, p, _ = grade_ticket(LOT, t, rec, rec.期号, cache)
                    grades[tier or "未中"] += 1
                    if p and p > 0:
                        pay += p
                total += pay
                staked += len(tks)
                per_issue.append((pay, rec.期号))
        cost = staked * 2.0
        per_issue.sort(reverse=True)
        top = per_issue[0][0] if per_issue else 0.0
        print("=" * 78)
        print(f"{arm}: 注数={staked} 实得={total:.0f} 成本={cost:.0f} "
              f"净/100元={100 * (total - cost) / cost:.2f}")
        print(f"  最大三期实得: {[(round(p, 1), i) for p, i in per_issue[:3]]}")
        print(f"  剔除最大一期后净/100元 = "
              f"{100 * (total - top - cost) / cost:.2f}")
        print("  奖级  计数      实测占比     解析占比     实测/解析")
        for g in _ALL_GRADES + ("未中",):
            c = grades.get(g, 0)
            if c == 0 and g not in pp:
                continue
            exp = pp.get(g, 0.0)
            ratio = (c / (exp * staked)) if exp > 0 and staked else float("nan")
            print(f"  {g:<4} {c:<8} {c / staked:.6f}   {exp:.6f}     {ratio:.3f}")
    return 0


def _analytic_probabilities(lottery: str):
    """解析单注各奖级概率（单一真源：pipeline.step1_probability）。"""
    from pipeline.step1_probability import (prize_probabilities_dlt,
                                           prize_probabilities_ssq)

    if lottery == "双色球":
        d = prize_probabilities_ssq()
    elif lottery == "大乐透":
        d = prize_probabilities_dlt()
    else:
        return {}
    out = {lv["prize_name"]: float(lv["probability"]) for lv in d["levels"]}
    out["未中"] = 1.0 - sum(out.values())
    return out


if __name__ == "__main__":
    raise SystemExit(main())
