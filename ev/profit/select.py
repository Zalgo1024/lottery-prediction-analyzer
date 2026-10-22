# -*- coding: utf-8 -*-
"""ev/profit/select.py —— 在「净EV 门槛」下选票。

与低拥挤策略的区别（本模块存在的理由）
------------------------------------
低拥挤策略按**冷门度排序**；本模块按**净EV 排序**，而净EV 含两条通道：
  ① 机制通道：冷组合在 κ>0 下被开出的概率更高（P(ω) ∝ μ^(−κ)）
  ② 分摊通道：冷组合**少人买**，中了之后同奖级共分的人少 ⇒ 单注实得更厚
实测（双色球 20 注）：κ=0 时冷票 EV −0.876 已优于热票 −1.015
——通道②不需要任何机制假设；通道①再叠加一层。

⭐ 排序用的是**代理 EV**（不是对每个候选做全枚举），出号后必须用
   `evaluate.exact_metrics` 复核；两者在 κ>0 时的排序一致率见测试。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from ev.payout import grade_probabilities, pool_share_expected_batch
from ev.profit import outcomes as O
from ev.profit import payouts as P
from ev.profit import tilt as T
from ev.profit.rules import rules_for

PROD_OVERLAP_CAP = 3          # 与生产 _dedup_and_record 同口径


def _uniform_prob(r, h: int, j: int) -> float:
    """均匀下「外层命中 h 且内层类别 j」的组合精确概率。"""
    w = r.outer_width
    n = r.outer_range[1] - r.outer_range[0] + 1
    if r.outer_choose:
        p_h = math.comb(w, h) * math.comb(n - w, w - h) / math.comb(n, w)
    else:
        p_h = math.comb(w, h) * (n - 1) ** (w - h) / n ** w
    return p_h * r.inner_multiplicity.get(j, 0) / r.inner_size


def _overlap_ok(reds, chosen, cap: int) -> bool:
    s = set(int(x) for x in reds)
    return all(len(s & set(int(y) for y in c)) <= cap for c in chosen)


def _key_of(r) -> str:
    return {"红球": "红球", "前区": "红球", "前6位": "号码", "全部位数": "号码"}[r.outer_label]


def ev_proxy(lottery: str, rows: np.ndarray, scores: np.ndarray,
             ctx: P.Ctx, kappa: float) -> np.ndarray:
    """候选组合的**代理单注 EV**（向量化）。

    = Σ_{h,j} p_j·P(h,j)·pay(c, h, j) × 机制通道因子 exp(−κs)/E[exp(−κs)]
    浮动奖的 pay 用该票自己的 μ（少人买 ⇒ 分奖少）。
    """
    r = rules_for(lottery)
    p_j = O.inner_class_prob(r)
    gprobs = grade_probabilities(lottery)
    offset = 0.0
    tl = T.outer_tilt(lottery)
    if tl is not None:
        offset = tl.offset

    ev = np.zeros(len(rows), dtype=np.float64)
    for (h, j), g in r.grade_table.items():
        if h > r.outer_width:
            continue
        gr = r.grades[g]
        p = _uniform_prob(r, h, j)
        if p <= 0:
            continue
        w = float(p_j[j]) * p
        if gr.kind == "fixed":
            ev += w * float(gr.fixed)
        else:
            pool = ctx.payout_basis(g)
            if not pool:
                continue
            cap = float(gr.cap) if gr.cap else P.CAP_DEFAULT
            mu = np.maximum(ctx.total_tickets * gprobs.get(g, 0.0) *
                            np.exp(scores + offset), 0.0)
            # 唯一值降本（与 evaluate 同款技巧）
            uniq, inv = np.unique(mu, return_inverse=True)
            pay_u = pool_share_expected_batch(float(pool), uniq, cap)
            ev += w * pay_u[inv]

    if tl is not None and kappa > 0:
        f = np.exp(-kappa * scores)
        ev = ev * (f / f.mean())
    return ev


def select_profit_tickets(lottery: str, K: int, ctx: P.Ctx, kappa: float = 0.0,
                          seed: int = 0, pool_size: int = 20000,
                          overlap_cap: int = PROD_OVERLAP_CAP,
                          blue: Optional[int] = 1) -> Dict[str, Any]:
    """按净EV 代理排序选 K 注（受重叠上限约束）。"""
    r = rules_for(lottery)
    U = O.outer_universe(lottery)
    tl = T.outer_tilt(lottery)

    rng = np.random.default_rng(seed)
    if pool_size >= len(U):
        idx = np.arange(len(U))
    else:
        idx = np.sort(rng.choice(len(U), size=pool_size, replace=False))
    rows = U[idx]
    scores = (tl.scores[idx] if tl is not None else np.zeros(len(idx)))

    ev = ev_proxy(lottery, rows, scores, ctx, kappa)
    order = np.argsort(-ev)

    chosen_rows: List[List[int]] = []
    chosen_ev: List[float] = []
    for i in order:
        row = [int(x) for x in rows[i]]
        if _overlap_ok(row, chosen_rows, overlap_cap):
            chosen_rows.append(row)
            chosen_ev.append(float(ev[i]))
        if len(chosen_rows) >= K:
            break

    tickets = []
    for row in chosen_rows:
        t: Dict[str, Any] = {_key_of(r): sorted(row)}
        if r.name == "双色球":
            t["蓝球"] = [int(blue or 1)]
        elif r.name == "大乐透":
            t["蓝球"] = [1, 2]
        elif r.name == "七星彩":
            t["号码"] = sorted(row) + [int(blue or 0)]
        tickets.append(t)

    return {
        "票": tickets,
        "元数据": {
            "彩种": lottery, "注数": len(tickets), "κ": float(kappa),
            "候选池": int(len(idx)), "重叠上限": int(overlap_cap),
            "EV代理范围": [min(chosen_ev), max(chosen_ev)] if chosen_ev else [None, None],
            "口径": "净EV 代理排序（非逐候选全枚举）；出号后用 exact_metrics 复核",
            "诚实声明": ("κ=0 且无分摊差异时全部候选同分、排序无意义；"
                         "本模型下差异只来自「少人买 ⇒ 分奖少」这一条，"
                         "拥挤度本身是代理分数（见 CROWD_PROXY_CAVEAT）。"),
        },
    }
