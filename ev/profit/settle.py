# -*- coding: utf-8 -*-
"""ev/profit/settle.py —— 开奖后自动结算（影子台账回填）。

把 ledger 里「已记录但未结算」的期次，对照**实际开奖**，逐注判奖、汇总实得，
回填净收益。口径与 ev/payout.base_payout 一致（浮动奖取「当期实际单注奖金」，
即真实中奖者分到的那一份；固定奖取官方固定额）。

税率用 merged-issue 口径（TX.BASIS_ISSUE）：整组 S 中奖合计超 10000 才按 20% 计征，
与 decide 决策口径一致。

★ 只回填不新增行（Ledger.append 语义：结算=写回原行）；对某期无实际开奖数据则跳过。
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from ev.profit import ledger, payouts as P, rules as RULES
from ev.profit import tax as TX

logger = logging.getLogger(__name__)


def _actual_record(lottery: str, issue: Any):
    """取实际开奖记录（含 zone_numbers）；无则 None。"""
    from data.loader import load_lottery
    d = load_lottery(lottery)
    recs = getattr(d, "records", None) or []
    for r in recs:
        if str(getattr(r, "期号", None)) == str(issue):
            return r
    return None


def _ordered_digits(zones: Dict[str, Any], schema) -> List[int]:
    """按 schema 区序把 zone_numbers 拍平成有序数字列表（位置即信息）。"""
    out: List[int] = []
    if not isinstance(zones, dict):
        return out
    for z in schema.zones:
        v = zones.get(z.name)
        if isinstance(v, (list, tuple)):
            out.extend(int(x) for x in v)
    return out


def _ticket_digits(lottery: str, ticket: dict) -> Dict[str, List[int]]:
    """从票面提取判奖所需数字：双/大给 {红,蓝}，数字型/七星彩给 {全部}。"""
    if lottery in ("双色球", "大乐透"):
        return {
            "红": [int(x) for x in (ticket.get("红球") or ticket.get("前区") or [])],
            "蓝": [int(x) for x in (ticket.get("蓝球") or ticket.get("后区") or [])],
        }
    nums = ticket.get("号码")
    if nums is None:
        from ev.profit import outcomes as O
        r = RULES.rules_for(lottery)
        nums = O._outer_digits(r, ticket)
    return {"全部": [int(x) for x in nums]}


def _grade(lottery: str, ticket: dict, actual_zones: Dict[str, Any],
           schema, issue: str) -> Tuple[Optional[str], float]:
    """判奖级并取实得单注。返回 (奖级 or None, 实得元)。"""
    from ev.payout import base_payout
    from data.feedback import _classify_prize, _classify_dlt_prize, _group_winning

    if lottery == "双色球":
        td = _ticket_digits(lottery, ticket)
        rh = len(set(td["红"]) & set(actual_zones.get("红球") or actual_zones.get("前区") or []))
        bh = len(set(td["蓝"]) & set(actual_zones.get("蓝球") or actual_zones.get("后区") or []))
        g = _classify_prize(rh, bh)
        if g == "未中":
            return None, 0.0
        pay, _ = base_payout(lottery, g, issue)
        return g, float(pay or 0.0)
    if lottery == "大乐透":
        td = _ticket_digits(lottery, ticket)
        rh = len(set(td["红"]) & set(actual_zones.get("红球") or actual_zones.get("前区") or []))
        bh = len(set(td["蓝"]) & set(actual_zones.get("蓝球") or actual_zones.get("后区") or []))
        g = _classify_dlt_prize(rh, bh, issue=issue)
        if g == "未中":
            return None, 0.0
        pay, _ = base_payout(lottery, g, issue)
        return g, float(pay or 0.0)
    if lottery in ("福彩3D", "排列3", "排列5"):
        td = _ticket_digits(lottery, ticket)
        act = _ordered_digits(actual_zones, schema)
        level, payout, _mode = _group_winning(lottery, td["全部"], act, issue)
        if level is None:
            return None, 0.0
        return level, float(payout or 0.0)
    if lottery == "七星彩":
        td = _ticket_digits(lottery, ticket)
        pred = td["全部"]
        act = _ordered_digits(actual_zones, schema)
        X = sum(1 for i in range(6) if i < len(pred) and i < len(act) and pred[i] == act[i])
        Y = 1 if len(pred) > 6 and len(act) > 6 and pred[6] == act[6] else 0
        T = X + Y
        if T == 7:
            g = "一等奖"
        elif X == 6 and Y == 0:
            g = "二等奖"
        elif X == 5 and Y == 1:
            g = "三等奖"
        elif T == 5:
            g = "四等奖"
        elif T == 4:
            g = "五等奖"
        elif T >= 3 or Y == 1:
            g = "六等奖"
        else:
            g = None
        if g is None:
            return None, 0.0
        if g in ("一等奖", "二等奖"):
            pay, _ = base_payout(lottery, g, issue)
            return g, float(pay or 0.0)
        fixed = {"三等奖": 3000.0, "四等奖": 500.0, "五等奖": 30.0, "六等奖": 5.0}
        return g, fixed.get(g, 0.0)
    return None, 0.0


def settle_issue(lottery: str, issue: Any) -> Dict[str, Any]:
    """结算单期（已记录未结算），对照实际开奖回填。"""
    rows = ledger._read(lottery)
    target = str(issue)
    row = next((r for r in rows if str(r.get("期号")) == target), None)
    if row is None:
        return {"ok": False, "期号": target, "原因": "台账无该期记录"}
    if row.get("结算"):
        return {"ok": True, "期号": target, "跳过": True, "原因": "已结算"}

    tickets = row.get("号码") or []
    if not tickets:
        return {"ok": False, "期号": target, "原因": "该期无记录号码"}

    from data.schema import get_schema, normalize_zone_names
    schema = get_schema(lottery)
    rec = _actual_record(lottery, issue)
    if rec is None:
        return {"ok": False, "期号": target, "原因": "无实际开奖数据（未开奖或数据滞后）"}
    az = normalize_zone_names(rec.zone_numbers, schema)

    gross = 0.0
    hits = 0
    detail: Dict[str, int] = {}
    for t in tickets:
        g, pay = _grade(lottery, t, az, schema, target)
        if g is not None:
            hits += 1
            detail[g] = detail.get(g, 0) + 1
            gross += pay

    cost = len(tickets) * float(RULES.rules_for(lottery).cost)
    tax = TX.tax_on(gross, basis=TX.BASIS_ISSUE)
    ledger.settle(lottery, issue, gross, tax, cost)
    return {
        "ok": True, "期号": target, "彩种": lottery,
        "中奖注数": hits, "奖级分布": detail,
        "税前奖金": round(gross, 2), "税": round(tax, 2),
        "成本": round(cost, 2), "净收益": round(gross - tax - cost, 2),
    }


def settle_pending(lottery: str) -> Dict[str, Any]:
    """结算该彩种所有「已记录、未结算、且已开奖」的期次。返回汇总。"""
    rows = ledger._read(lottery)
    pending = [r for r in rows if not r.get("结算") and r.get("号码")]
    if not pending:
        return {"彩种": lottery, "新结算期数": 0, "已结算期数": sum(1 for r in rows if r.get("结算"))}
    newly = 0
    skipped = 0
    for r in pending:
        res = settle_issue(lottery, r["期号"])
        if res.get("ok") and not res.get("跳过"):
            newly += 1
        else:
            skipped += 1
    return {
        "彩种": lottery, "新结算期数": newly, "跳过": skipped,
        "已结算期数": sum(1 for r in rows if r.get("结算")),
    }
