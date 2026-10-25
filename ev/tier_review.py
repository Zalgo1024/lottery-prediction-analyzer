# -*- coding: utf-8 -*-
"""ev/tier_review.py —— 命中收益按目标奖级分级的诊断复盘内核。

为什么有这个模块
----------------
2026-10-25 的奖级分级复盘（`scripts/oneoff/_tier_review.py`）里有两个函数
承担了全部口径纪律，当时是一次性脚本的局部定义：

1. `gate_ok`：按规则口径给出 (K, n) 达标布尔矩阵。两条口径分开——
   `current`（用现行规则重算历史开奖，反事实）与 `periodic`（按当年实际
   规则结算，历史事实）。两者**不总相等**：双色球只有 1 个规则版本 ⇒
   构造性等价；大乐透有 5 个版本且「≥四等奖」的条件集跨版本有三套
   （{(4,2),(5,0),(5,1),(5,2)} / +(3,2),(4,1) / {(4,1),...}），
   current 与 periodic 会给出**不同**的达标矩阵。
2. `tier_breakdown`：按 **(规则版本, 奖级名) 双键**分桶累计命中票次与金额。

为什么分桶键必须带规则版本（2026-10-24 事故的直接教训）
------------------------------------------------------
大乐透 5 个规则版本里同名奖级的含金量完全不同：四等奖 3000 元(2007/09)
→ 200 元(2014) → 300 元(2026)。若按奖级名跨版本合并分桶，或判级用新版、
金额按名字查旧版，固定奖会整体放大约 6 倍（实测返奖率 163.8%）。
本模块的每个桶都以 (规则版本, 奖级名) 为键，**绝不让同名奖级跨版本合并**。

三条口径纪律（调用方必须守住）
----------------------------
· 金额口径与 `ev.arena.payout.money_series` 同路径（`amount_grid` /
  逐票 `grade_ticket` 兜底），调用方应断言两者总额逐分相等；
· `需逐票兜底` 只按「本期真的命中了金额未定的奖级」触发（`待定格` 命中判断），
  不按「名单非空」整期回退；
· 本模块只读规则表与开奖记录做复盘计算，**不写 pending、不接生产**。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ev.arena import payout as MONEY
from ev.arena.design import outcome_gate
from ev.coverage_backtest import grade_ticket
from ev.profit.rules import rules_for
from data import prize_amounts as PA


# ---------------------------------------------------------------- 达标矩阵
def gate_ok(lottery: str, target: Any, h: np.ndarray, j: np.ndarray,
            recs: Sequence[Any], mode: str, target_issue: Any) -> np.ndarray:
    """按口径给出 (K, n) 达标布尔矩阵。

    current  ：全部期用 target_issue（最新期）解析的规则门 —— 反事实口径；
    periodic ：逐期用**当期**规则门 —— 历史事实口径。
    ★ 两口径的 gate 都只读规则表（期号→版本），不读开奖结果 ⇒ 无前视。
    """
    if mode == "current":
        gate, _w, _m, _n = outcome_gate(lottery, target, target_issue)
        assert gate.shape[0] > int(h.max()) and gate.shape[1] > int(j.max()), \
            f"current gate 形状 {gate.shape} 不兼容命中矩阵范围 h≤{int(h.max())} j≤{int(j.max())}"
        return gate[h, j]
    cache: dict = {}
    ok = np.zeros(h.shape, dtype=bool)
    for p, rec in enumerate(recs):
        ver = rules_for(lottery, rec.期号).rule_version
        g = cache.get(ver)
        if g is None:
            g, _w, _m, _n = outcome_gate(lottery, target, rec.期号)
            assert g.shape[0] > int(h[:, p].max()) and g.shape[1] > int(j[:, p].max()), \
                f"periodic gate（{ver}）形状 {g.shape} 不兼容 h≤{int(h[:, p].max())} j≤{int(j[:, p].max())}"
            cache[ver] = g
        ok[:, p] = g[h[:, p], j[:, p]]
    return ok


# ---------------------------------------------------------------- 分级拆解
def tier_breakdown(lottery: str, tickets: Sequence[Dict[str, Any]],
                   recs: Sequence[Any], h: np.ndarray, j: np.ndarray,
                   mode: str, target_issue: Any,
                   mult: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """按 (规则版本, 奖级名) 分桶统计命中票次与金额（金额口径与 money_series 一致）。

    返回 buckets / 总额 / 逐票兜底期数。mult 为倍投向量（None=全 1）。
    ★ periodic 口径下同名奖级跨版本已变义 ⇒ 分桶键带规则版本，绝不跨版本合并奖级名。
    """
    PA.preload(lottery, [r.期号 for r in recs])
    if mult is None:
        mult = np.ones(len(tickets), dtype=np.int64)
    buckets: dict = {}
    total = 0.0
    fb: List[str] = []
    for p, rec in enumerate(recs):
        rule_issue = target_issue if mode == "current" else rec.期号
        grid, meta = MONEY.amount_grid(lottery, rec.期号, rule_issue=rule_issue)
        names = meta["奖级网格"]
        ver = meta["规则版本"]
        hp, jp = h[:, p], j[:, p]
        if meta["需逐票兜底"] and meta["待定格"][hp, jp].any():
            fb.append(str(rec.期号))
            cache: dict = {}
            for k, t in enumerate(tickets):
                tier, amt, _note = grade_ticket(lottery, t, rec, rec.期号, cache,
                                                rule_issue=rule_issue)
                amt = float(amt or 0.0) * int(mult[k])
                if amt > 0 and tier and tier != "未中":
                    b = buckets.setdefault((ver, tier), {"命中票次": 0, "金额": 0.0, "期集合": set()})
                    b["命中票次"] += int(mult[k])
                    b["金额"] += amt
                    b["期集合"].add(p)
                    total += amt
            continue
        pays = grid[hp, jp]
        for k in range(len(tickets)):
            amt = float(pays[k]) * int(mult[k])
            if amt <= 0:
                continue
            nm = names[int(hp[k])][int(jp[k])]
            if not nm:
                continue
            b = buckets.setdefault((ver, nm), {"命中票次": 0, "金额": 0.0, "期集合": set()})
            b["命中票次"] += int(mult[k])
            b["金额"] += amt
            b["期集合"].add(p)
            total += amt
    out_buckets = {}
    for (ver, g), b in sorted(buckets.items()):
        out_buckets[f"{ver}|{g}"] = {"命中票次": b["命中票次"],
                                     "金额": round(b["金额"], 2),
                                     "涉及期数": len(b["期集合"])}
    return {"buckets": out_buckets, "总额": round(total, 2),
            "逐票兜底期数": len(fb), "逐票兜底期号": fb[:20]}


# ---------------------------------------------------------------- 差额归因
def attribute_delta(lottery: str, tickets: Sequence[Dict[str, Any]],
                    recs: Sequence[Any], h: np.ndarray, j: np.ndarray,
                    mult_a: np.ndarray, mult_b: np.ndarray,
                    mode: str, target_issue: Any) -> Dict[str, Any]:
    """按奖级归因两个倍投向量的实得差 Δ = Σ_k (mB_k − mA_k)·pay_kp。

    用于回答「单倍 vs 倍投的金额差是不是少数大奖撑出来的」。
    分桶键与 `tier_breakdown` 一致：(规则版本, 奖级名)。
    """
    PA.preload(lottery, [r.期号 for r in recs])
    diff = np.asarray(mult_b, dtype=np.int64) - np.asarray(mult_a, dtype=np.int64)
    nz = np.nonzero(diff)[0]
    per_grade: dict = {}
    per_period: dict = {}
    for p, rec in enumerate(recs):
        rule_issue = target_issue if mode == "current" else rec.期号
        grid, meta = MONEY.amount_grid(lottery, rec.期号, rule_issue=rule_issue)
        names = meta["奖级网格"]
        ver = meta["规则版本"]
        hp, jp = h[:, p], j[:, p]
        if meta["需逐票兜底"] and meta["待定格"][hp[nz], jp[nz]].any():
            cache: dict = {}
            for k in nz:
                tier, amt, _note = grade_ticket(lottery, tickets[k], rec, rec.期号, cache,
                                                rule_issue=rule_issue)
                d = float(amt or 0.0) * int(diff[k])
                if d == 0 or not tier or tier == "未中":
                    continue
                key = f"{ver}|{tier}"
                per_grade[key] = per_grade.get(key, 0.0) + d
                per_period[p] = per_period.get(p, 0.0) + d
            continue
        pays = grid[hp, jp]
        for k in nz:
            d = float(pays[k]) * int(diff[k])
            if d == 0:
                continue
            nm = names[int(hp[k])][int(jp[k])]
            if not nm:
                continue
            key = f"{ver}|{nm}"
            per_grade[key] = per_grade.get(key, 0.0) + d
            per_period[p] = per_period.get(p, 0.0) + d
    top = sorted(per_period.items(), key=lambda kv: -abs(kv[1]))[:5]
    tot = sum(per_period.values())
    return {"按奖级": {k: round(v, 2) for k, v in sorted(per_grade.items(), key=lambda kv: -abs(kv[1]))},
            "总额": round(tot, 2),
            "最大5期": [{"期号": str(recs[p].期号), "Δ": round(v, 2)} for p, v in top],
            "最大单期占总额比": (abs(top[0][1]) / abs(tot)) if tot else None}
