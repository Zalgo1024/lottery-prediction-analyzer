# -*- coding: utf-8 -*-
"""位置型彩种的**逐位**一阶 Markov 排序（离线研究，不接生产选号）。

为什么单独一个模块（2026-11-07）
------------------------------
位置型彩种（排列3/5、福彩3D、七星彩）的「位置」是语义的一部分：第 1 位开 5 与
第 3 位开 5 是不同的结果。任何 `sort()`、任何「把一注票当成号码集合」的处理都会把
这层信息抹掉 —— 而抹掉之后不会报错。

外部参考程序对这类彩种做了「位内 Markov + 跨位共现 + 频率动量 + 遗漏 + 邻号」，
并明确「不排序、不去重」。这里只借**结构**，不借结论（它的样本只有 72–180 期、
且没有检验）。所以本模块只做三件事，且都能逐位复算：

  ① 位内一阶 Markov：`P(本期该位=y | 上期该位=x)`；
  ② 遗漏节奏：`当前遗漏 / 历史平均间隔`（越接近 1 越"按节奏该出"）；
  ③ 邻号强度：历史 `y-1 / y / y+1` 的频次占比。

★ **跨位共现未实现**：定不出一个能对外解释、且不会被误读的定义，与其拼一个
  解释不清的分数，不如留空并写明（见返回值里的 `未实现`）。
★ 输出是排序表，不是中奖概率；不产出票、不改注数、不写 pending、不占预算。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional

from config import LOTTERY_CONFIG
from data.schema import get_schema

__all__ = ["NOTES", "applicable", "position_models", "rank_positions"]

NOTES = (
    "只建模三个量：位内一阶 Markov（上期该位→本期该位）、遗漏节奏（当前遗漏/历史平均间隔）、"
    "邻号强度（y±1 的历史频次占比）。跨位共现未实现。",
    "输出是排序表，不是中奖概率；同一输入必然同一输出，可逐位复算。",
    "不接生产选号、不改注数、不写 pending、不占预算；要用它必须先预注册并进独立评估窗。",
)

ALPHA = 0.5          # 拉普拉斯平滑，只影响「没有转移记录」时的均分，不改排序口径
W_MARKOV, W_OMIT, W_NEIGH = 0.60, 0.25, 0.15


def applicable(lottery: str) -> Dict[str, Any]:
    """该彩种能不能做逐位建模；不能则给原因（缺席必须显式，不静默跳过）。"""
    if lottery not in LOTTERY_CONFIG:
        return {"适用": False, "原因": f"未注册彩种：{lottery}"}
    zones = list(get_schema(lottery).zones)
    if not zones:
        return {"适用": False, "原因": "该彩种没有分区定义"}
    bad = [z.name for z in zones if not z.ordered]
    if bad:
        return {"适用": False,
                "原因": f"非位置型分区：{'、'.join(bad)}"
                        "（逐位建模要求每位独立、按序、只取 1 个数）"}
    return {"适用": True, "位置数": len(zones),
            "跳过": [f"{z.name}（draw_only，票面不选号，不建模）"
                     for z in zones if z.draw_only]}


def _digits(rec, zones) -> List[Optional[int]]:
    """把一条开奖记录拆成「每位一个数字」；缺位给 None（不臆造 0）。"""
    zn = getattr(rec, "zone_numbers", None) or {}
    out: List[Optional[int]] = []
    for z in zones:
        vals = zn.get(z.name)
        if not vals:
            out.append(None)
            continue
        try:
            out.append(int(list(vals)[0]))
        except (TypeError, ValueError):
            out.append(None)
    return out


def _load_seq(lottery: str, zones, window: Optional[int]):
    from data.loader import load_lottery

    recs = [r for r in (load_lottery(lottery).records or []) if getattr(r, "期号", None)]
    recs.sort(key=lambda r: r.期号)          # loader 返回降序，必须重排
    if window:
        recs = recs[-int(window):]
    return recs, [_digits(r, zones) for r in recs]


def position_models(lottery: str, *, window: Optional[int] = None) -> Dict[str, Any]:
    """逐位的转移计数、频次、历史间隔、当前遗漏（原始量，不归一化）。"""
    ok = applicable(lottery)
    if not ok["适用"]:
        return ok

    zones = list(get_schema(lottery).zones)
    recs, seq = _load_seq(lottery, zones, window)
    if len(recs) < 30:
        return {"适用": False, "原因": f"样本不足（{len(recs)} 期 < 30），不做逐位建模"}

    n = len(seq)
    models = []
    for i, z in enumerate(zones):
        if z.draw_only:
            continue
        trans: Dict[int, Dict[int, int]] = defaultdict(lambda: defaultdict(int))
        freq: Dict[int, int] = defaultdict(int)
        gaps: Dict[int, List[int]] = defaultdict(list)
        last_seen: Dict[int, int] = {}
        for t, row in enumerate(seq):
            v = row[i]
            if v is None:
                continue
            freq[v] += 1
            if v in last_seen:
                gaps[v].append(t - last_seen[v])
            last_seen[v] = t
            prev = seq[t - 1][i] if t > 0 else None
            if prev is not None:
                trans[prev][v] += 1
        models.append({
            "位置": z.name, "最小": int(z.min), "最大": int(z.max),
            "票面数": int(z.ticket_count),
            "转移": {k: dict(v) for k, v in trans.items()},
            "频次": dict(freq),
            "平均间隔": {k: sum(v) / len(v) for k, v in gaps.items() if v},
            # 当前遗漏：从最后一期往回数，直到该位开出 y；从未出现 ⇒ n
            "当前遗漏": {y: (n - 1 - last_seen[y] if y in last_seen else n)
                         for y in range(z.min, z.max + 1)},
            "上期": seq[-1][i] if n else None,
            "期数": n,
        })
    return {"适用": True, "彩种": lottery, "期数": n, "位置": models,
            "口径": list(NOTES)}


def rank_positions(lottery: str, *, window: Optional[int] = None) -> Dict[str, Any]:
    """逐位给出「下期该位取哪个数字」的排序（分数 ∈ [0,1]，仅为可复核的展示）。

    分数 = 0.60·Markov + 0.25·遗漏节奏 + 0.15·邻号强度
      · Markov  = `(count(上期x→y) + α) / (count(上期x→*) + α·位数)`；没有转移记录时退化为均分
      · 遗漏节奏 = `min(1, 当前遗漏 / 历史平均间隔)`；没有历史间隔时给 0.5（不假装有节奏）
      · 邻号强度 = `freq(y-1)+freq(y)+freq(y+1)` 占该位总频次的比例
    """
    m = position_models(lottery, window=window)
    if not m.get("适用"):
        return m

    out = []
    for pm in m["位置"]:
        lo, hi = pm["最小"], pm["最大"]
        span = hi - lo + 1
        freq, total = pm["频次"], (sum(pm["频次"].values()) or 1)
        prev = pm["上期"]
        row = pm["转移"].get(prev, {}) if prev is not None else {}
        row_total = sum(row.values())

        ranked = []
        for y in range(lo, hi + 1):
            if row_total:
                p_markov = (row.get(y, 0) + ALPHA) / (row_total + ALPHA * span)
            else:
                p_markov = 1.0 / span                       # 无转移记录 ⇒ 均分，不偏向任何数
            avg_gap = pm["平均间隔"].get(y)
            if avg_gap and avg_gap > 0:
                omit_ratio = min(1.0, pm["当前遗漏"].get(y, 0) / avg_gap)
            else:
                omit_ratio = 0.5                            # 没有节奏可说 ⇒ 中性
            neigh = (freq.get(y - 1, 0) + freq.get(y, 0) + freq.get(y + 1, 0)) / total
            score = W_MARKOV * p_markov + W_OMIT * omit_ratio + W_NEIGH * neigh
            ranked.append({
                "数码": y,
                "分": round(score, 6),
                "Markov": round(p_markov, 6),
                "遗漏节奏": round(omit_ratio, 6),
                "邻号强度": round(neigh, 6),
            })
        ranked.sort(key=lambda x: (-x["分"], x["数码"]))
        out.append({"位置": pm["位置"], "上期": prev, "排序": ranked})

    return {"适用": True, "彩种": lottery, "期数": m["期数"], "位置": out,
            "口径": list(NOTES),
            "未实现": "跨位共现（定不出可复核且不会被误读的定义，留空而不拼凑）"}
