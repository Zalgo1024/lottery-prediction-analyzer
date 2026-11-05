# -*- coding: utf-8 -*-
"""ev/prefix_real.py —— **真实生产票序**的前缀读数（离线、只读、不接生产）。

与 `ev/prefix_curve.py` 的区别
------------------------------
`prefix_curve` 用离线三臂回放生成的票序（K=100、反事实假设只买前 3 注）；
本模块读 `training/feedback/*_feedback_history.json` 里**当时真实出号**的反馈，
按出号时写下的 `批次内序号` 还原那一批票的顺序 ⇒ 首次给出「实际拿到手的前 n 注」
口径的读数，而不是模拟出来的票序。

★ 三条必须随结果一起披露的局限
  1. `批次内序号` 是**结算时**写进反馈记录的，项目没有「开奖前冻结排序」的历史
     快照；只有 `批次时间 <= 开奖日期`（日级精度）可作旁证 ⇒ 不能声称「排名已在
     开奖前冻结」，也不能排除事后重排的可能。
  2. 没有逐期真实购买记录 ⇒ 仍是「假设每期买前 n 注」，不是实测战绩。
  3. 可用期数只有 19~39 期，远小于上一轮正式读数给出的 `n*_保守`
     （双色球 n=3 / ≥5元 需上万期）⇒ **不做 BH、不做统计判决、不写「检出」**，
     只给描述性读数与功效限制。

★ 纪律：生产出号 / 结算 / 看板不 import 本模块；不改 `ticket_size.json`、
  不改预算闸门、不改推送、不改下注量。只读反馈文件，不写任何项目状态文件。
"""
from __future__ import annotations

import json
import random
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from data.feedback import FEEDBACK_DIR
from ev import prefix_curve as PC
from ev.batch import group_batches
from ev.profit.rules import _GRADE_ORDER, rules_for

#: ★ 主口径与档表从前缀研究 import，不另立第二真源。
PRIMARY_N = PC.PRIMARY_N
PREFIX_KS = PC.PREFIX_KS

UNIT_PRICE = 2.0
GRADE_GATE = "四等"
_GATE_RANK = _GRADE_ORDER.index(GRADE_GATE)
#: 整批 K 注只作对照，且逐期 K 不同；独立成档并标「非实际购买」。
WHOLE_BATCH_KEY = "整批"

COUNTERFACTUAL_NOTE = (
    "★ 反事实：假设每期都买了**真实生产票序**的前 n 注（主口径 n=3，沿用前缀研究"
    "的授权假设值，非实测）。项目没有逐期真实购买记录，「并非每次都买」这一维"
    "不建模挑期行为，只作披露。与上一轮不同：票序来自真实出号反馈，不是离线模拟。")
FREEZE_LIMIT_NOTE = (
    "★ 冻结性局限：票序按反馈记录里的 `批次内序号` 还原，该字段是**结算时**写入的，"
    "没有开奖前的排序冻结快照；只有 `批次时间 <= 开奖日期`（日级精度）可作旁证。"
    "因此这是「可得的最接近真实的票序」，不是「已自证开奖前冻结的票序」。")
NO_VERDICT_NOTE = (
    "★ 本模块不做多重校正、不做统计判决、不写「检出」：可用期数远小于前缀研究"
    "给出的 `n*_保守`，任何「未检出 / 未优于」都包含功效不足，只表示已测维度在"
    "该样本量下未见差异，不构成证明。顺序对照只诊断排序是否承载信息，不主张策略有效。")
PRIOR_ZERO_NOTE = (
    "固定奖层面 E[达标注数] = n·p 与选哪些票无关 ⇒ 前 n 注与同批随机 n 注的"
    "期望回款本就相同；若出现稳定正差，先查实现缺陷 / 奖级口径，不主张选号优势。")
DENOMINATOR_NOTE = (
    "分母纪律：窗口内**每一个**开奖期都建行；有可还原批次的记「参与」，其余记"
    "「未参与 + 原因」。主判据分母 = 参与期数，另报按全期轴的期率作对照；"
    "单档 K < n 的期记「不适用 + 原因」，不许静默剔除后缩小分母。")


def _as_float(value: Any) -> Optional[float]:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value)
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def grade_rank(name: Any) -> Optional[int]:
    """奖级名 → 等级序（0=一等）。两套写法（「六等奖」/「六等」）去尾「奖」。

    玩法名（直选 / 组选3 / 选十中零）与「未中」不在等级序里 ⇒ 返回 None，
    调用方必须据此把「≥四等」标成**不适用**，不能当作 0。
    """
    g = str(name or "").strip()
    if not g or g == "未中":
        return None
    if g.endswith("奖"):
        g = g[:-1]
    return _GRADE_ORDER.index(g) if g in _GRADE_ORDER else None


def grade_capable(lottery: str) -> Tuple[bool, str]:
    """「≥四等」门槛是否适用：查**现行规则表**，不手写彩种清单。"""
    try:
        grades = set(rules_for(lottery).grades or {})
    except Exception as exc:                       # 规则缺失时显式标不适用
        return False, f"现行规则表不可用（{type(exc).__name__}）⇒ 不适用"
    if GRADE_GATE in grades:
        return True, f"现行规则含「{GRADE_GATE}」奖级 ⇒ 适用（rank≤{_GATE_RANK}）"
    return False, ("现行规则的奖级名是玩法名（直选 / 组选 / 选十中零），"
                   "不是「一等…N 等」等级序号 ⇒ 「≥四等」门槛不适用")


def _ticket_of(rec: Dict[str, Any], seq: int, *, unit_price: float) -> Dict[str, Any]:
    pay = _as_float(rec.get("实得"))
    unknown = bool(rec.get("金额未知"))
    valid = rec.get("valid_prediction") is not False
    # 金额未知 / 无效预测都不进求和，各自单独计数（缺席 ≠ 通过）。
    if unknown or not valid:
        pay = None
    price = _as_float(rec.get("单价"))
    price_missing = price is None
    grade = rec.get("中奖等级") or rec.get("中奖玩法") or "未中"
    nums: Dict[str, Any]
    if "预测红球" in rec or "预测蓝球" in rec:
        nums = {"红球": list(rec.get("预测红球") or []),
                "蓝球": list(rec.get("预测蓝球") or [])}
    else:
        nums = dict(rec.get("预测号码") or {})
    return {
        "序号": seq, "号码": nums, "实得": pay,
        "金额未知": unknown, "单价": unit_price if price_missing else price,
        "单价缺失": price_missing, "中奖等级": str(grade),
        "奖级序": grade_rank(grade),
        "有效": valid,
        "台账域": rec.get("台账域"),
    }


def load_real_orders(lottery: str, *, records: Optional[Sequence[dict]] = None,
                     axis: Optional[Sequence[Any]] = None,
                     feedback_dir: Optional[Path] = None,
                     unit_price: float = UNIT_PRICE) -> Dict[str, Any]:
    """只读归组 → 还原真实出号票序；每一项排除都写原因，不静默 filter。

    `records` / `axis` 显式传入时用于测试与复现；否则读反馈文件与开奖 loader。
    """
    src = Path(feedback_dir) if feedback_dir is not None else Path(FEEDBACK_DIR)
    if records is None:
        path = src / f"{lottery}_feedback_history.json"
        records = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    records = [r for r in (records or []) if isinstance(r, dict)]

    if axis is None:
        from ev import tier_backtest as TB
        axis = [str(r.期号) for r in TB.load_test_recs(lottery, None)]
    axis = [str(x) for x in (axis or [])]

    groups = group_batches(records, lottery)
    excluded: List[Dict[str, str]] = []
    counts = {"批次总数": len(groups), "explicit批数": 0,
              "derived批数": 0, "序号不完整批数": 0,
              "前视不通过批数": 0, "重出批数": 0, "通过批数": 0}
    by_issue: Dict[str, List[Dict[str, Any]]] = {}

    for g in groups:
        if g.source != "explicit":
            counts["derived批数"] += 1
            excluded.append({"期号": str(g.issue), "批次": g.batch_id,
                             "原因": "derived：批次号与出号序号均为推算，不满足票序还原前提"})
            continue
        counts["explicit批数"] += 1
        issue = str(g.issue)
        seqs: List[Optional[int]] = []
        for r in g.records:
            try:
                seqs.append(int(r["批次内序号"]))
            except (KeyError, TypeError, ValueError):
                seqs.append(None)
        if None in seqs or sorted(x for x in seqs if x is not None) != list(range(1, g.size + 1)):
            counts["序号不完整批数"] += 1
            excluded.append({"期号": issue, "批次": g.batch_id,
                             "原因": "批次内序号缺失 / 不连续 / 重复 ⇒ 不能还原票序"})
            continue

        # 前视：只有「批次时间 <= 开奖日期」可作旁证，日级精度；缺字段不判通过。
        bts = [d for d in (_parse_dt(r.get("批次时间")) for r in g.records) if d]
        dds = [d for d in (_parse_dt(r.get("开奖日期")) for r in g.records) if d]
        if not bts or not dds:
            counts["前视不通过批数"] += 1
            excluded.append({"期号": issue, "批次": g.batch_id,
                             "原因": "批次时间或开奖日期缺失 ⇒ 前视无法判定，不判通过"})
            continue
        if min(bts).date() > min(dds).date():
            counts["前视不通过批数"] += 1
            excluded.append({"期号": issue, "批次": g.batch_id,
                             "原因": "批次时间晚于开奖日期 ⇒ 前视检查不通过"})
            continue

        # ★ 按出号时写下的序号显式排序，绝不依赖 JSON 里的记录顺序。
        ordered = sorted(zip((x for x in seqs if x is not None), g.records),
                         key=lambda t: t[0])
        tickets = [_ticket_of(r, int(s), unit_price=unit_price) for s, r in ordered]
        by_issue.setdefault(issue, []).append({
            "批次号": g.batch_id, "批次时间": min(bts).isoformat(),
            "开奖日期": min(dds).date().isoformat(),
            "期号内批号": g.issue_batch_index,
            "期号内批数": g.issue_batch_count,
            "K": len(tickets), "票序": tickets,
        })

    # 同期多批：主口径只取「出号时间最早」的那批；其余批进排除明细，不许事后挑。
    participating: Dict[str, Dict[str, Any]] = {}
    for issue, batches in by_issue.items():
        batches.sort(key=lambda b: (b["期号内批号"] if b["期号内批号"] is not None
                                   else 10 ** 6, b["批次时间"], b["批次号"]))
        main = batches[0]
        participating[issue] = main
        counts["通过批数"] += 1
        for extra in batches[1:]:
            counts["重出批数"] += 1
            excluded.append({"期号": issue, "批次": extra["批次号"],
                             "原因": "同期多批（重出批）：主口径只取第 1 批，"
                                     "其余批不进主判据；不许事后挑表现最好的批"})

    # 全期轴：按参与期在开奖序列中的首尾切片，避免字符串比较的期号长度假设。
    idx = [i for i, x in enumerate(axis) if x in participating]
    window_axis = axis[idx[0]:idx[-1] + 1] if idx else sorted(participating)
    axis_available = bool(idx)
    not_part = [x for x in window_axis if x not in participating]
    denom = {
        "开奖期数": len(window_axis),
        "参与期数": len(participating),
        "未参与期数": len(not_part),
        "未参与期号": not_part,
        "未参与说明": (DENOMINATOR_NOTE if axis_available else
                       "★ 开奖序列中找不到参与期号 ⇒ 分母退化为参与期本身，"
                       "窗口内开奖期数不可判定（缺席 ≠ 通过）"),
        "开奖轴可用": axis_available,
    }
    return {
        "彩种": lottery, "反馈目录": str(src), "反馈记录数": len(records),
        "期号轴": window_axis, "参与": participating, "排除明细": excluded,
        "排除计数": counts, "分母": denom,
        "注数范围": ([min(b["K"] for b in participating.values()),
                      max(b["K"] for b in participating.values())]
                     if participating else []),
        "等级门槛": dict(zip(("适用", "依据"), grade_capable(lottery))),
    }


def _settle(tickets: Sequence[dict], n: int, *, grade_ok: bool) -> Dict[str, Any]:
    pref = list(tickets[:n])
    pay = sum(t["实得"] for t in pref if t["实得"] is not None)
    cost = sum(t["单价"] for t in pref)
    ge5 = any(t["实得"] is not None and t["实得"] >= 5.0 for t in pref)
    tier = (any(t["奖级序"] is not None and t["奖级序"] <= _GATE_RANK for t in pref)
            if grade_ok else None)
    return {
        "≥5元": int(bool(ge5)), "≥四等": (int(bool(tier)) if tier is not None else None),
        "实得": round(pay, 2), "成本": round(cost, 2),
        "净收益": round(pay - cost, 2),
        "金额未知注数": sum(1 for t in pref if t["金额未知"]),
        "无效注数": sum(1 for t in pref if not t["有效"]),
        "单价缺失注数": sum(1 for t in pref if t["单价缺失"]),
    }


def _summarize(rows: Sequence[dict], n: int, *, denominator: int,
               axis_periods: int, grade_ok: bool,
               grade_reason: str) -> Dict[str, Any]:
    """★ 期率三个分母并列，绝不静默缩小：可用期 / 参与期 / 全期轴。

    K<n 的期在「可用期分母」下不进分母，是条件率；为避免把它读成缩分母，
    另给「参与期分母」（不适用期按 0 计入，保守下限）与「全期轴分母」两列。
    """
    usable = [r for r in rows if r["可用"]]
    na = [r for r in rows if not r["可用"]]
    hits5 = sum(r["≥5元"] for r in usable)
    hits4 = sum(r["≥四等"] for r in usable) if grade_ok else None
    def ratio(hits: Optional[float], base: int) -> Optional[float]:
        return round(hits / base, 6) if hits is not None and base else None
    total_pay = round(sum(r["实得"] for r in usable), 2)
    total_cost = round(sum(r["成本"] for r in usable), 2)
    # ★ 扣掉「单期总实得」的最大值，不是某一注的最大值：大奖翻转的是整期读数。
    top = max((r["实得"] for r in usable), default=0.0)
    net = round(total_pay - total_cost, 2)
    net_ex = round(total_pay - top - total_cost, 2)
    return {
        "n": n, "参与期数": denominator, "全期轴期数": axis_periods,
        "可用期数": len(usable), "不适用期数": len(na),
        "不适用原因": sorted({r["不适用原因"] for r in na if r["不适用原因"]}),
        "≥5元期率": ratio(hits5, len(usable)),
        "≥5元期率_参与期分母": ratio(hits5, denominator),
        "≥5元期率_全期轴分母": ratio(hits5, axis_periods),
        "≥四等期率": ratio(hits4, len(usable)),
        "≥四等期率_参与期分母": ratio(hits4, denominator),
        "≥四等状态": "适用" if grade_ok else f"不适用：{grade_reason}",
        "实得合计": total_pay, "成本合计": total_cost,
        "净收益": net,
        # 单期大奖会翻转净收益符号 ⇒ 剔除那期实得后并列报一次，成本不变。
        "净收益_剔除单期最大实得": net_ex,
        "被单期实得翻转": bool((net >= 0) != (net_ex >= 0)),
        "单期最大实得": round(top, 2),
        "金额未知注数": sum(r["金额未知注数"] for r in usable),
        "无效注数": sum(r["无效注数"] for r in usable),
        "单价缺失注数": sum(r["单价缺失注数"] for r in usable),
        "K范围": ([min(r["K"] for r in usable), max(r["K"] for r in usable)]
                  if usable else []),
    }


def _draw(seed: int, issue: str, n: int, K: int, *, exclude_prefix: bool) -> Optional[List[int]]:
    lo = n if exclude_prefix else 0
    if K - lo < n:
        return None
    digits = int(issue) if str(issue).isdigit() else 0
    rng = random.Random(seed * 1000003 + n * 1009 + digits + (7001 if exclude_prefix else 0))
    return rng.sample(range(lo, K), n)


def _compare(prefix: Sequence[int], sample: Sequence[Optional[int]]) -> Dict[str, Any]:
    pairs = [(a, b) for a, b in zip(prefix, sample) if b is not None]
    if not pairs:
        return {"状态": "未测（无可用期）", "原因": "全部期注数不足，无法抽样对照"}
    diffs = [a - b for a, b in pairs]
    tot = sum(abs(d) for d in diffs)
    return {
        "状态": "已测（描述性，不进显著检验）", "配对期数": len(pairs),
        "实测Δ": round(sum(diffs) / len(diffs), 6),
        "差值之和": sum(diffs),
        "单期占Δ比": (round(max(abs(d) for d in diffs) / tot, 6) if tot else None),
        "集中度说明": ("Δ 只由单期撑起 ⇒ 不可判" if tot and
                       max(abs(d) for d in diffs) / tot >= 0.9 else "—"),
    }


def _window_stability(series: Dict[str, Any], ks: Sequence[int]) -> Dict[str, Any]:
    """方案第四判据的「不同时间窗口」维度（描述性、不进 BH）。

    切分沿用 `prefix_curve.window_split` 单一真源，不另写一套；
    ★ 前后半各需 ≥3 期，不足时显式标「未测 + 原因」——写 0 等于把没测写成稳定。
    """
    out: Dict[str, Any] = {}
    for n in ks:
        usable = [r for r in series[str(n)] if r["可用"]]
        w = PC.window_split(len(usable))
        if not w["可测"]:
            out[str(n)] = {
                "状态": "未测（可用期数不足）",
                "原因": "评估窗前后半各需 ≥3 期；不足时写 0 等于把没测写成稳定",
                "可用期数": len(usable), "切分": w}
            continue
        cut, half, ev = w["探索窗期数"], w["前半"], w["评估窗期数"]
        vals = [r["≥5元"] for r in usable[cut:]]
        a = sum(vals[:half]) / half
        b = sum(vals[half:]) / (ev - half)
        out[str(n)] = {
            "状态": "已测（描述性，不进 BH）", "可用期数": len(usable),
            "≥5元期率_评估窗前半": round(a, 6),
            "≥5元期率_评估窗后半": round(b, 6),
            "前后半差": round(abs(a - b), 6),
            # ★ 前后半同为 0 是「评估窗零命中」，不是「稳定」：写 0 等于把没测写成稳定。
            "评估窗无信息": bool(a == 0 and b == 0),
            "切分": w}
    return out


def run_prefix_real(lottery: str, *, records=None, axis=None,
                    feedback_dir: Optional[Path] = None,
                    prefix_ks: Optional[Sequence[int]] = None,
                    seeds: Sequence[int] = (0, 1, 2, 3, 4),
                    unit_price: float = UNIT_PRICE) -> Dict[str, Any]:
    """真实生产票序 → 前缀曲线 + 顺序对照。纯函数，不写盘、不改生产状态。"""
    ks = tuple(int(x) for x in (prefix_ks if prefix_ks is not None else PREFIX_KS))
    seeds = tuple(int(s) for s in seeds)
    orders = load_real_orders(lottery, records=records, axis=axis,
                              feedback_dir=feedback_dir, unit_price=unit_price)
    grade_ok, grade_reason = grade_capable(lottery)
    issues = orders["期号轴"]
    participating = orders["参与"]
    denom = orders["分母"]

    all_docs = [str(n) for n in ks] + [WHOLE_BATCH_KEY]
    series: Dict[str, Any] = {k: [] for k in all_docs}
    for issue in issues:
        batch = participating.get(issue)
        K = batch["K"] if batch else 0
        for key in all_docs:
            whole = key == WHOLE_BATCH_KEY
            n = K if whole else int(key)      # 整批档逐期取全部 K 注
            na_reason = ""
            if batch is None:
                na_reason = "窗口内开奖但无可还原出号批次（未参与期）"
            elif not whole and K < n:
                na_reason = f"该期实际出号 K={K} < n={n}"
            if na_reason:
                series[key].append({
                    "期号": issue, "可用": 0, "K": K,
                    "不适用原因": na_reason, "非实际购买": whole,
                    "≥5元": 0, "≥四等": (0 if grade_ok else None),
                    "实得": 0.0, "成本": 0.0, "净收益": 0.0,
                    "金额未知注数": 0, "无效注数": 0, "单价缺失注数": 0})
                continue
            row = _settle(batch["票序"], n, grade_ok=grade_ok)
            row.update({"期号": issue, "可用": 1, "K": K,
                        "不适用原因": "", "非实际购买": whole})
            series[key].append(row)

    curves: Dict[str, Any] = {}
    for key in all_docs:
        c = _summarize(series[key], None if key == WHOLE_BATCH_KEY else int(key),
                       denominator=denom["参与期数"],
                       axis_periods=denom["开奖期数"],
                       grade_ok=grade_ok, grade_reason=grade_reason)
        c["档"] = key
        c["非实际购买"] = key == WHOLE_BATCH_KEY
        curves[key] = c

    # 顺序对照：同批随机抽 n 注（全体 / 排除前 n 注），多种子；只诊断排序信息量。
    order_cmp: Dict[str, Any] = {}
    for n in ks:
        rows = series[str(n)]
        usable = [r for r in rows if r["可用"]]
        pref5 = [r["≥5元"] for r in usable]
        pref_issue = [r["期号"] for r in usable]
        arms: Dict[str, Any] = {}
        for label, exclude in (("全体随机", False), ("排除前缀", True)):
            per_seed: List[Dict[str, Any]] = []
            all_means: List[float] = []
            for s in seeds:
                vals: List[Optional[int]] = []
                for issue in pref_issue:
                    K = participating[issue]["K"]
                    idx = _draw(s, issue, n, K, exclude_prefix=exclude)
                    if idx is None:
                        vals.append(None)
                        continue
                    r = _settle([participating[issue]["票序"][i] for i in idx],
                                n, grade_ok=grade_ok)
                    vals.append(r["≥5元"])
                cmp = _compare(pref5, vals)
                per_seed.append({"seed": s, **cmp})
                if cmp.get("实测Δ") is not None:
                    all_means.append(cmp["实测Δ"])
            mean = (round(sum(all_means) / len(all_means), 6)
                    if all_means else None)
            spread = (round(max(all_means) - min(all_means), 6)
                      if all_means else None)
            # ★ 抽样噪声与 Δ 同量级时必须点名：否则 ±0.07 会被读成「排序有效」。
            # ★ 文案禁写竖线：markdown 表格单元格里的 `|` 会被当列分隔符撑破表格。
            noise = ("—" if mean is None or spread is None else
                     ("跨 seed 全距 ≥ Δ绝对值 ⇒ 抽样噪声与信号同量级，本样本不可分辨"
                      if spread >= abs(mean) else
                      "跨 seed 全距 < Δ绝对值（仍非统计判决）"))
            arms[label] = {
                "状态": ("已测" if all_means else "未测（无可配对期）"),
                "逐seed": per_seed,
                "实测Δ_跨seed均值": mean,
                "实测Δ_跨seed全距": spread,
                "噪声判读": noise,
                "单期占Δ比_跨seed最大": max(
                    (s["单期占Δ比"] for s in per_seed
                     if s.get("单期占Δ比") is not None), default=None),
                "未配对期数": sum(1 for r in usable
                                  if r["K"] < (2 * n if exclude else n)),
            }
        order_cmp[str(n)] = arms

    return {
        "彩种": lottery, "主口径n": PRIMARY_N, "前缀档": list(ks),
        "seeds": list(seeds), "期号表": issues,
        "口径说明": {
            "反事实": COUNTERFACTUAL_NOTE, "冻结性局限": FREEZE_LIMIT_NOTE,
            "判决限制": NO_VERDICT_NOTE, "先验零": PRIOR_ZERO_NOTE,
            "分母": DENOMINATOR_NOTE,
            "整批对照": (PC.NOT_PURCHASED_NOTE +
                       " 本模块逐期 K 不同（生产注数会漂移），整批档按当期实际 K 结算，"
                       "只作对照，不进主判据。"),
            "顺序对照": ("对照臂 = 同一批票里随机抽 n 注（全体 / 排除前 n 注）。"
                        "两臂成本相同、票池相同、开奖相同 ⇒ 差值只反映**顺序是否承载信息**，"
                        "不是「策略 vs 随机策略」的判决，更不是有效性结论。"),
            "术语": PC.TERMINOLOGY_NOTE,
        },
        "数据体检": {k: v for k, v in orders.items()
                     if k not in ("参与", "期号轴")},
        "期号轴": issues, "分母": denom,
        "曲线": curves, "逐期": series, "顺序对照": order_cmp,
        "稳定性": _window_stability(series, ks),
        "判决": {
            "状态": "未做统计判决（样本量不足）",
            "原因": ("可用期数仅 %d 期；前缀研究给出的 n*_保守 为上万期量级，"
                     "不做 BH、不做配对检验 ⇒ 只报描述性读数，"
                     "不写「检出 / 未检出」结论。任何「未见差异」都包含功效不足。"
                     % denom["参与期数"]),
        },
    }
