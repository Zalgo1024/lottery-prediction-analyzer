# -*- coding: utf-8 -*-
"""对账：向量化净收益结算（ev/arena/payout.py） vs 唯一结算内核 evaluate_period。

★ 为什么必须对账
    快路把「每期每奖级一次取金额」压成一张 (h,j) 金额网格。任何一处口径偏差
    （规则版本选错、派奖没相加、浮动奖兜底走错分支）都不会报错，只会让净收益
    悄悄偏几个百分点 —— 那正是 Stage 0c 差点被「+703σ」骗过去的那类事故。

★ 对账口径
    `periodic` 模式（判级与金额都用当期规则版本）←→ `evaluate_period` 的 `总实得`，
    要求**逐期逐分相等**（不是平均相等）。

    `current` 模式（全期按最新规则判级）是**反事实**口径：`evaluate_period` 现在
    支持 `rule_issue=`（判级版本）与 `issue=`（当期版本）分离，故**全部期**都要对账。
    ★ 旧版本这里曾「跳过判级版本≠当期版本的期」—— 那正好把唯一坏掉的期段排除在
      对账之外（大乐透 2,831 期里跳掉 2,724 期），让「0 差异」变成假象。
      教训与 Stage 0b 同源：**跳过 = 没验**。

★ 分层抽样
    不能只取尾部：大乐透 5 个规则版本横跨 2007~2026，只抽尾部会全部落在最新版，
    旧版本的固定奖额与派奖路径完全测不到。
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import numpy as np

from ev.arena import periods as PD
from ev.arena import payout as MONEY
from ev.arena.arms import select_arm
from ev.arena.runner import _records_for
from ev.coverage_backtest import build_pool, evaluate_period
from ev.profit.rules import rules_for

K = {"双色球": 200, "大乐透": 38}
PER_VERSION = 30          # 每个规则版本抽多少期
CAP = 3


def _by_version(lottery: str, recs: List[Any]) -> Dict[str, List[int]]:
    out: Dict[str, List[int]] = {}
    for p, r in enumerate(recs):
        v = rules_for(lottery, r.期号).rule_version
        out.setdefault(v, []).append(p)
    return out


def check(lottery: str, *, rule_mode: str = "periodic") -> Dict[str, Any]:
    issues = PD.build_periods(lottery, None)
    recs = _records_for(lottery, issues)
    pool = build_pool(lottery, 8000, seed=20261024)
    target = "≥10元"
    target_issue = recs[-1].期号

    tks, _meta = select_arm("random", lottery, pool, K[lottery], target, target_issue,
                            seed=0, cap=CAP, params={"n_periods": len(issues)})
    h, j = MONEY.hit_matrices(lottery, tks, recs, target_issue)

    groups = _by_version(lottery, recs)
    picks: List[int] = []
    for ver, idxs in groups.items():
        step = max(1, len(idxs) // PER_VERSION)
        picks += idxs[::step][:PER_VERSION]
    picks.sort()

    # 预载（真值路径也走同一份缓存；这是 IO 批量化，不影响口径）
    from data import prize_amounts as PA
    PA.preload(lottery, [recs[p].期号 for p in picks])

    cache: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    bad: List[Dict[str, Any]] = []
    skipped_current = 0
    target_ver = rules_for(lottery, target_issue).rule_version
    for p in picks:
        rec = recs[p]
        this_ver = rules_for(lottery, rec.期号).rule_version
        rule_issue = target_issue if rule_mode == "current" else rec.期号
        # ---- 真值：逐票 grade_ticket（唯一结算内核），判级/金额版本分离
        truth = evaluate_period(lottery, tks, rec, rec.期号, cache,
                                rule_issue=rule_issue)["总实得"]
        # ---- 快路：金额网格 + 花式索引
        grid, meta = MONEY.amount_grid(lottery, rec.期号, rule_issue=rule_issue)
        if meta["需逐票兜底"] and meta["待定格"][h[:, p], j[:, p]].any():
            bad.append({"期号": str(rec.期号), "类型": "需逐票兜底",
                        "奖级": meta["需逐票兜底"]})
            continue
        fast = float(grid[h[:, p], j[:, p]].sum())

        rows.append({"期号": str(rec.期号), "规则版本": this_ver,
                     "判级版本": meta["规则版本"], "真值": truth, "快路": fast,
                     "差": fast - truth})
        if abs(fast - truth) > 0.005:          # 分位（真值已 round 到 0.01）
            bad.append(rows[-1])

    diffs = np.array([r["快路"] - r["真值"] for r in rows], dtype=float) if rows else np.zeros(0)
    return {
        "彩种": lottery, "规则口径": rule_mode, "注数": len(tks),
        "期数": len(recs), "抽样期数": len(rows),
        "规则版本分布": {k: len(v) for k, v in groups.items()},
        "不一致数": len([b for b in bad if b.get("类型") != "需逐票兜底"]),
        "需逐票兜底": len([b for b in bad if b.get("类型") == "需逐票兜底"]),
        "跳过期数": skipped_current,
        "判级版本": target_ver if rule_mode == "current" else "（逐期）",
        "最大绝对差": float(np.abs(diffs).max()) if diffs.size else 0.0,
        "实得合计_真值": float(sum(r["真值"] for r in rows)),
        "实得合计_快路": float(sum(r["快路"] for r in rows)),
        "不一致明细": bad[:10],
    }


def main() -> int:
    out: List[Dict[str, Any]] = []
    for lot in ("双色球", "大乐透"):
        for mode in ("periodic", "current"):
            r = check(lot, rule_mode=mode)
            out.append(r)
            print(f"\n=== {lot} / {mode} ===", flush=True)
            print(f"  票集 {r['注数']} 注 | 全量 {r['期数']} 期 | 抽样 {r['抽样期数']} 期 "
                  f"| 规则版本 {r['规则版本分布']}", flush=True)
            print(f"  不一致 {r['不一致数']} | 需逐票兜底 {r['需逐票兜底']} | "
                  f"跳过期 {r['跳过期数']} | 判级版本 {r['判级版本']} | "
                  f"最大绝对差 {r['最大绝对差']:.4f}", flush=True)
            print(f"  实得合计 真值 {r['实得合计_真值']:,.2f} vs 快路 {r['实得合计_快路']:,.2f}",
                  flush=True)
            if r["不一致明细"]:
                for b in r["不一致明细"]:
                    print(f"    ! {b}", flush=True)

    total_bad = sum(r["不一致数"] for r in out)
    total_fb = sum(r["需逐票兜底"] for r in out)
    print(f"\n合计：不一致 {total_bad} | 需逐票兜底 {total_fb}")
    print("判决：" + ("通过（逐期逐分相等）" if total_bad == 0 and total_fb == 0
                     else "★ 未通过，先查口径再谈结论"))
    return 0 if total_bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
