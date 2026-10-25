# -*- coding: utf-8 -*-
"""ev/coverage_backtest.py —— Phase 1 奖级覆盖策略回测（研究工具，不接生产）

目标（用户给定）：在固定每期注数 K 下，检验「覆盖分散选号」是否比「同池随机 /
同池按分取 top-K」更常做到「每期至少一注达到指定奖级」。

诚实边界（与项目一致，写死在注释与报告里）：
  - 各号码组合等可能 ⇒ 换选号**不提高**单注中奖概率 / 平均中奖次数；
  - 固定 K 下减少票间重叠**可能**提高「至少中一注」概率，但**同时改变**多注同中
    的可能性；是否有实际改善，**必须按彩种 + 奖金门槛回测**，不可假定；
  - 排列5/3D/排列3 奖级 ≠ 5 元，5 元以上优先看 双色球 / 大乐透 / 七星彩。

度量（四类不可互相替代，用户点名）：
  1. 至少中 5 元 的期占比（≥5 元门槛）
  2. 至少中 10 元 的期占比（≥10 元门槛）
  3. 中奖次数（所有票中奖注数合计，含多注同中）
  4. 净收益 = 总实得 − K×2 元

回放口径：固定候选池（uniform random，无前视泄漏）+ 一次性选 K 注，对历史开奖序列
逐期结算。在 i.i.d. 抽奖下，固定 K 注集的「至少中一注」期望 = 每期重选的期望，
故固定票集回放是期望的无偏、更快估计量（方差不同但期望一致），并严格按时间顺序评估。

复用：data/feedback 判级函数、ev/payout.payout_eff（浮动奖级实得）、
ev/selection 的 _rand_tickets / _greedy_pick / control_tickets / select_coverage。
"""
from __future__ import annotations

import json
from typing import Any, Dict, FrozenSet, List, Optional, Tuple

import numpy as np

# ---------------------------------------------------------------- 常量

# 研究用固定 K（取当前 ticket_size.json 生效值，与生产对齐；比较一律用它，
# 避免「中奖次数被注数变化带偏」）。这是**研究常量**，不改动生产动态配置。
STUDY_K = {
    "双色球": 200, "大乐透": 38, "排列5": 5, "福彩3D": 5, "排列3": 50, "七星彩": 44,
}

# K 敏感度扫描网格：ssq/dlt K=200 ≫ 蓝球数，5 元门槛大概率饱和，小 K 才能暴露覆盖效应。
K_GRID = {
    "双色球": [10, 25, 50, 100, 200],
    "大乐透": [10, 25, 38, 50, 100],
    "七星彩": [10, 25, 44, 50, 100],
    "排列5": [5, 10, 25, 50],
    "福彩3D": [5, 10, 25, 50],
    "排列3": [5, 10, 25, 50],
}

# ★ 原先这里有一张手写的 TIER_PAYOUT（第三份九级固定奖表）。它在大乐透 2026-02
#   改七级之后全错（七等按 100 元算，实际 5 元）。现已删除：
#   固定奖一律走 `ev.profit.rules.rules_for(lottery, issue)`（按规则版本），
#   金额优先取 `data.prize_amounts.actual_per_bet`（当期实测，含派奖相加）。

# qxc 浮动奖级名义值（仅用于净收益估计；一等奖/二等奖中出极罕见，且历史数据常缺，
# 用名义值并在报告中标注，不影响「≥5元/≥10元」门槛判定）。
QXC_FLOATING_NOMINAL = {"一等奖": 5_000_000, "二等奖": 200_000}


# ---------------------------------------------------------------- 判级 + 实得

def _flatten_zones(zones: Dict[str, Any]) -> List[int]:
    """把 {分区名: [数字...]} 拍平成按位序排列的数字列表（按分区名里的数字排序）。"""
    items = sorted(
        zones.items(),
        key=lambda kv: int("".join(c for c in kv[0] if c.isdigit()) or 0),
    )
    out: List[int] = []
    for _, v in items:
        if isinstance(v, (list, tuple)):
            out.extend(int(x) for x in v)
        else:
            out.append(int(v))
    return out


def _floating_payout_redblue(lottery: str, ticket: Dict[str, Any], issue: Any,
                            tier: str, cache: Dict[str, Any]) -> float:
    """红蓝浮动奖级（ssq/dlt 一/二/三等）当期实得，按 (ticket,issue) 缓存。"""
    from ev.payout import payout_eff
    key = json.dumps(ticket, ensure_ascii=False, sort_keys=True) + f"|{issue}|{tier}"
    if key in cache:
        return cache[key]
    try:
        r = payout_eff(lottery, ticket, issue=issue)
        row = next((x for x in r.get("奖级明细", []) if x["奖级"] == tier), None)
        v = float(row["实得单注"]) if (row and row.get("实得单注")) else 0.0
    except Exception:
        v = 0.0
    cache[key] = v
    return v


def grade_ticket(lottery: str, ticket: Dict[str, Any], actual_rec: Any,
                 issue: Any, cache: Dict[str, Any], *,
                 rule_issue: Any = None) -> Tuple[str, float]:
    """单注对单期开奖的 (奖级, 实得单注元, 口径)。未中 → ("未中", 0.0, "规则表")。

    复用 data/feedback 的判级单一真源；金额按**规则版本**结算（_settle_by_rules）：
    固定奖 → 规则表/实测；浮动奖红蓝走库里实测或 payout_eff 兜底、qxc 走名义值。

    `rule_issue`（★ 反事实口径，默认 None = 与 `issue` 相同）
        「按哪个版本的规则判级」。`current` 口径下传目标期号 ⇒ 判级用最新规则表。
        此时**金额也必须跟随判级版本**，否则会出现「判成 2026 的七等、发 2019 的
        七等 100 元」——大乐透 2019 规则里 (2,1) 是九等 5 元、2026 规则里是七等 5 元，
        同名奖级跨版本含义已变，按名字查库会把固定奖整体放大约 6 倍。
    """
    from data.feedback import (_classify_dlt_prize, _classify_prize, _qxc_prize,
                               _settle_pl)

    grading = rule_issue if rule_issue is not None else issue

    if lottery in ("双色球", "大乐透"):
        ph = set(ticket.get("红球", []))
        bh = set(ticket.get("蓝球", []))
        ah = set((actual_rec.zone_numbers or {}).get("红球", []))
        ab = set((actual_rec.zone_numbers or {}).get("蓝球", []))
        rh = len(ph & ah)
        bb = len(bh & ab)
        tier = (_classify_dlt_prize(rh, bb, grading) if lottery == "大乐透"
                else _classify_prize(rh, bb))
        if tier == "未中":
            return ("未中", 0.0, "规则表")
        return _settle_by_rules(lottery, issue, tier, cache, rule_issue=rule_issue,
                                fallback=lambda: _floating_payout_redblue(
                                    lottery, ticket, issue, tier, cache))

    if lottery == "七星彩":
        pred = ticket.get("号码") or []
        actual = _flatten_zones(actual_rec.zone_numbers or {})
        zh = {}
        for i in range(min(7, len(pred), len(actual))):
            zh[f"第{i + 1}位"] = 1 if int(pred[i]) == int(actual[i]) else 0
        tier, fixed = _qxc_prize({"分区命中": zh})
        if tier is None:
            return ("未中", 0.0, "规则表")
        g = tier.replace("奖", "")                      # "一等奖" → "一等"
        return _settle_by_rules("七星彩", issue, g, cache, rule_issue=rule_issue,
                                fallback=lambda: float(QXC_FLOATING_NOMINAL.get(tier, 0.0)))

    # 数字型（排列5/3D/排列3）：走官方规则 _settle_pl（直选/组选/未中）。
    pred = ticket.get("号码") or []
    fb = {
        "预测号码": _nums_to_zones(pred, lottery),
        "实际号码": actual_rec.zone_numbers or {},
        "期号": issue,
    }
    pl, _mode, level = _settle_pl(lottery, fb)
    if level is None:
        return ("未中", 0.0, "规则表")
    return (level, float(pl + 2.0), "规则表")   # _settle_pl 返回 模拟盈亏 = 实得 − 2


def _settle_by_rules(lottery: str, issue: Any, grade: str,
                     cache: Dict[str, Any], fallback,
                     rule_issue: Any = None) -> Tuple[str, float, str]:
    """按**规则版本**取该奖级的实得单注金额。返回 (奖级, 金额, 口径)。

    顺序：固定奖 → 规则表金额（除非库里有实测，派奖期会是「基本+派奖」）；
          浮动奖 → 库里当期实测单注奖金 → 兜底 fallback（payout_eff / 名义值）。

    ★ `rule_issue` 与 `issue` 的**奖级含义**可能不同（大乐透 5 个版本）：
      两者版本不同时，固定奖一律取**判级版本**的固定额，**不按名字查库**。
      按名字查库只在「同版本」时成立（派奖期要用实测）。

    ★ 期号不可解析（合成期号如 "TEST"）→ 回退现行版本并在口径里显式标注，
      **不静默降级**；期号可解析但早于最早版本（RuleVersionError）→ 向上抛，
      真实早期期号不许用新规则结算。
    """
    from data.issue_key import IssueKeyError
    from data.prize_amounts import actual_per_bet
    from ev.profit.rules import rules_for

    grading = rule_issue if rule_issue is not None else issue
    try:
        rules = rules_for(lottery, grading)
        basis = "规则表"
    except IssueKeyError:
        rules = rules_for(lottery)
        basis = "规则表(期号不可解析→现行版)"
    rule = rules.grades.get(grade)
    if rule is None:
        return (grade, 0.0, "未知奖级")

    # 判级版本 ≠ 当期版本 ⇒ 同名奖级含义已变，禁止按名字查库
    mixed = False
    if rule_issue is not None and str(rule_issue) != str(issue):
        try:
            mixed = rules_for(lottery, issue).rule_version != rules.rule_version
        except Exception:
            mixed = True

    if rule.kind == "fixed":
        if mixed:
            return (grade, float(rule.fixed), "规则表(判级版本)")
        amt, note = actual_per_bet(lottery, issue, grade, rule.fixed)
        if amt is None:
            return (grade, float(rule.fixed), basis)
        return (grade, float(amt), note)
    amt, note = actual_per_bet(lottery, issue, grade, None)
    if amt is not None:
        return (grade, float(amt), note)
    return (grade, float(fallback() or 0.0), "payout_eff")


def _nums_to_zones(nums: List[int], lottery: str) -> Dict[str, List[int]]:
    """把数字列表还原成 {第N位:[d]} 分区 dict（_settle_pl 用）。"""
    return {f"第{i + 1}位": [int(x)] for i, x in enumerate(nums)}


def evaluate_period(lottery: str, tickets: List[Dict[str, Any]], actual_rec: Any,
                    issue: Any, cache: Dict[str, Any],
                    target_grades: Optional[FrozenSet[str]] = None, *,
                    rule_issue: Any = None) -> Dict[str, Any]:
    """单期：对 K 张票逐注判级 + 取实得，返回四类指标。

    `target_grades`：已解析好的「达标奖级集」（来自 `ev.tier_target.resolve_conditions`）。
      给了就在返回里加 `"达标"`（本期是否**有任一注**达到 ≥目标奖级）。
      ★ 零额外成本：奖级 `_tier` 本来就是 `grade_ticket` 的返回值，只是以前被丢掉了。
      ★ 与 `≥10元` 的口径差异：`≥10元` 看**当期实际实得**（含派奖），`达标` 看**奖级**
        是否在集合里（规则表口径）⇒ 派奖期两者可能不一致，这是刻意的：
        选号阶段不许看当期实际派奖（前视）。

    `rule_issue`：反事实口径（判级与金额都按该期号的规则版本）。默认 None = 当期。
    """
    hit5 = False
    hit10 = False
    hit_target = False
    wins = 0
    total = 0.0
    notes: Dict[str, int] = {}
    for t in tickets:
        _tier, payout, note = grade_ticket(lottery, t, actual_rec, issue, cache,
                                          rule_issue=rule_issue)
        notes[note] = notes.get(note, 0) + 1
        if payout and payout > 0:
            wins += 1
            total += payout
            if payout >= 5:
                hit5 = True
            if payout >= 10:
                hit10 = True
        if target_grades is not None and _tier and _tier != "未中" and _tier in target_grades:
            hit_target = True
    out = {
        "命中注数": wins,
        "总实得": round(total, 2),
        "≥5元": hit5,
        "≥10元": hit10,
        "结算口径": notes,      # 可审计：实测 / 实测+派奖 / 规则表 / payout_eff
    }
    if target_grades is not None:
        out["达标"] = hit_target
    return out


# ---------------------------------------------------------------- 候选池

def _rand_digit(lottery: str, m: int, rng: np.random.Generator) -> List[Dict[str, Any]]:
    """数字型随机合法候选票（_rand_tickets 只覆盖红蓝/七星彩，这里补数字型）。"""
    n = 5 if lottery == "排列5" else 3
    out = []
    for _ in range(m):
        out.append({"号码": [int(rng.integers(0, 10)) for _ in range(n)]})
    return out


def build_pool(lottery: str, m: int = 2000, seed: int = 0) -> List[Dict[str, Any]]:
    """uniform random 候选池（无前视泄漏，不依赖历史开奖）。"""
    rng = np.random.default_rng(seed)
    if lottery in ("双色球", "大乐透", "七星彩"):
        from ev.selection import _rand_tickets
        return _rand_tickets(lottery, m, rng)
    return _rand_digit(lottery, m, rng)


def _concentration_score(lottery: str, ticket: Dict[str, Any]) -> int:
    """确定性的「聚集代理」评分：号码之和越小越「中心/小号」，人类偏好越强。
    用于 top_score 臂（零泄漏，不读历史），与 coverage 的分散形成对照。"""
    if lottery in ("双色球", "大乐透"):
        return sum(ticket.get("红球", [])) + sum(ticket.get("蓝球", []))
    nums = ticket.get("号码") or []
    return sum(nums)


# ---------------------------------------------------------------- 选择器包装

def _sel_coverage(lottery: str, pool: List[Dict[str, Any]], K: int, seed: int):
    from ev.selection import select_coverage
    return select_coverage(lottery, pool, K, 5, seed)


def _sel_random(lottery: str, pool: List[Dict[str, Any]], K: int, seed: int):
    from ev.selection import control_tickets
    return control_tickets(lottery, pool, K, seed)


def _make_sel_topscore(lottery: str, pool: List[Dict[str, Any]]):
    from ev.selection import _greedy_pick, _OVERLAP_CAP
    # 一次性按聚集评分排序（确定性、零泄漏）；运行时再低重叠挑 K。
    order = sorted(range(len(pool)),
                   key=lambda i: _concentration_score(lottery, pool[i]))
    cap = _OVERLAP_CAP.get(lottery, 3)

    def sel(K: int, seed: int):
        scored = [(0.0, pool[i]) for i in order]
        picked = _greedy_pick(scored, K, cap, lottery)
        return [p["号码"] for p in picked]

    return sel


_SELECTORS = {
    "coverage": _sel_coverage,
    "random": _sel_random,
    "top_score": None,   # 需 _make_sel_topscore 预绑定
}


# ---------------------------------------------------------------- 回放

def replay(lottery: str, tickets: List[Dict[str, Any]], periods: Optional[int] = None,
           seed: int = 0) -> Dict[str, Any]:
    """对固定 K 注集，按时间顺序逐期结算历史开奖，累计四类指标。

    periods=None 用全量；否则取最近 periods 期。返回含期数/各类期占比/中奖次数/净收益的 dict。
    """
    from data.loader import load_lottery
    data = load_lottery(lottery)
    recs = sorted((r for r in data.records if r.期号), key=lambda r: r.期号)
    if periods:
        recs = recs[-periods:]
    K = len(tickets)
    cache: Dict[str, Any] = {}
    n = len(recs)
    c5 = c10 = 0
    wins = 0
    total = 0.0
    for rec in recs:
        ev = evaluate_period(lottery, tickets, rec, rec.期号, cache)
        if ev["≥5元"]:
            c5 += 1
        if ev["≥10元"]:
            c10 += 1
        wins += ev["命中注数"]
        total += ev["总实得"]
    cost = n * K * 2.0
    return {
        "彩种": lottery,
        "K": K,
        "期数": n,
        "至少中5元期占比": round(c5 / n, 4) if n else 0.0,
        "至少中10元期占比": round(c10 / n, 4) if n else 0.0,
        "中奖次数": wins,
        "单期平均中奖注数": round(wins / n, 4) if n else 0.0,
        "总实得": round(total, 2),
        "净收益": round(total - cost, 2),
        "单期平均净收益": round((total - cost) / n, 4) if n else 0.0,
    }


def run_arm(lottery: str, mode: str, K: int, periods: Optional[int] = None,
            pool_size: int = 2000, seed: int = 0) -> Dict[str, Any]:
    """跑单条臂（coverage / random / top_score）：建池 → 选 K → 回放。"""
    pool = build_pool(lottery, pool_size, seed)
    if mode == "top_score":
        sel = _make_sel_topscore(lottery, pool)
        tickets = sel(K, seed)
    else:
        tickets = _SELECTORS[mode](lottery, pool, K, seed)
    res = replay(lottery, tickets, periods=periods, seed=seed)
    res["臂"] = mode
    return res


# ---------------------------------------------------------------- 对照 + K 敏感度

def compare(lottery: str, K_grid: Optional[List[int]] = None,
            periods: Optional[int] = None, pool_size: int = 2000,
            seed: int = 0) -> Dict[str, Any]:
    """三臂对照 + K 敏感度扫描，报告四类指标。

    arms: coverage（覆盖分散）/ random（同池随机阴性对照）/ top_score（同池按聚集分取top-K）。
    三者唯一差异 = 选号准则，候选池与 K 完全相同 ⇒ 干净隔离「覆盖」效应。
    """
    K_grid = K_grid or K_GRID.get(lottery, [STUDY_K.get(lottery, 10)])
    table = []
    for K in K_grid:
        row = {"K": K}
        for mode in ("coverage", "random", "top_score"):
            r = run_arm(lottery, mode, K, periods=periods, pool_size=pool_size, seed=seed)
            row[f"{mode}_≥5元"] = r["至少中5元期占比"]
            row[f"{mode}_≥10元"] = r["至少中10元期占比"]
            row[f"{mode}_中奖次数"] = r["中奖次数"]
            row[f"{mode}_净收益"] = r["净收益"]
        table.append(row)
    return {
        "彩种": lottery,
        "期数窗口": periods,
        "候选池大小": pool_size,
        "说明": ("固定候选池（无前视）+ 一次性选 K 注，按时间顺序逐期结算；"
                 "在 i.i.d. 下等于每期重选的期望估计。coverage=覆盖分散；"
                 "random=同池随机(阴性对照)；top_score=同池按聚集分取top-K(对照)。"
                 "单注中奖概率与选号无关，本回放只测『K 注间重叠结构』对四类指标的影响。"),
        "对照表": table,
    }


# ---------------------------------------------------------------- 命令行

def _fmt_table(cmp: Dict[str, Any]) -> str:
    rows = cmp["对照表"]
    modes = ("coverage", "random", "top_score")
    lines = [f"彩种={cmp['彩种']}  期数窗口={cmp['期数窗口']}  候选池={cmp['候选池大小']}"]
    lines.append("K | " + " | ".join(
        f"{m}_≥5元 / ≥10元 / 中奖 / 净收" for m in modes))
    for r in rows:
        cells = []
        for m in modes:
            cells.append(f"{r[f'{m}_≥5元']:.3f} / {r[f'{m}_≥10元']:.3f} / "
                         f"{r[f'{m}_中奖次数']} / {r[f'{m}_净收益']}")
        lines.append(f"{r['K']:>3} | " + " | ".join(cells))
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    periods = None
    lots = []
    for a in args:
        if a.isdigit():
            periods = int(a)
        else:
            lots.append(a)
    lots = lots or ["双色球", "大乐透", "七星彩"]
    for lot in lots:
        cmp = compare(lot, periods=periods) if periods else compare(lot)
        print(_fmt_table(cmp))
        print()
