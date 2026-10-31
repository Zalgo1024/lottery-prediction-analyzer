# -*- coding: utf-8 -*-
"""ev/arena/replay.py —— 唯一的结算实现（向量化），所有臂共用。

为什么重写而不复用 `ev.coverage_backtest.evaluate_period`
---------------------------------------------------------
`evaluate_period` 是**逐票纯 Python** 判级，实测 ≈1.8 s/期（双色球 K=200）。
按方案 §7 的成本估算，一个彩种一档 500 期 × 多臂就要 20+ 小时 ⇒ 不可行。
本模块把判级压成 `(h, j) → 达标` 的二维查表 + 一次矩阵乘，同格 0.2 s/期。

★ 但「快路」必须有审计对账：脚本 `scripts/oneoff/_stage0_replay_check.py`
  已把本实现与 `ev.tier_target.target_is_hit` 逐票对账，
  双色球 33,600 票 / 大乐透 69,480 票 **0 处不一致**。

规则版本口径（★ 这是大乐透的核心坑）
------------------------------------
大乐透有 5 个规则版本（期数 367/698/735/1031/107），旧版本含九等/八等小奖，
「≥10元」在旧规则下命中率高得多（实测 0.75 vs 0.21）。于是：

  · `rule_mode="periodic"` 逐期按**当期**规则判级 —— 这是历史事实口径；
  · `rule_mode="current"`  全部期按**最新**规则的达标门判 —— 反事实口径。

两者**不许混用**：效应量（精确枚举，用同一期号）与方差必须同口径，
否则 Δ 在低达标率下算、SD 在高达标率下测 ⇒ n* 失真（Stage 0c 实测：
大乐透两种口径的 n* 差 2.3~6 倍，判决不稳定）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

import ev.tier_target as TT
from ev.arena import COST_PER_BET
from ev.arena.design import _onehot, outcome_gate
from ev.arena.types import ArmResult
from ev.profit.rules import rules_for

RULE_MODES = ("periodic", "current")


def draw_vectors(lottery: str, records: Sequence[Any],
                 n_out: int, n_in: int) -> Tuple[np.ndarray, np.ndarray]:
    """开奖记录 → one-hot `(R, B)`，形状 `(n, n_out)` / `(n, n_in)`。"""
    n = len(records)
    R = np.zeros((n, n_out), dtype=np.int8)
    B = np.zeros((n, n_in), dtype=np.int8)
    for p, rec in enumerate(records):
        for x in (rec.红球 or []):
            R[p, int(x) - 1] = 1
        v = rec.蓝球
        for x in (list(v) if isinstance(v, (list, tuple)) else [v]):
            if x is not None:
                B[p, int(x) - 1] = 1
    return R, B


def _gate_for(lottery: str, target: Any, issue: Any, cache: Dict[str, np.ndarray]
              ) -> Tuple[np.ndarray, int, int]:
    ver = rules_for(lottery, issue).rule_version
    g = cache.get(ver)
    if g is None:
        g, _w_out, _m_in, _n = outcome_gate(lottery, target, issue)
        cache[ver] = g
    return g


def replay(lottery: str, tickets: Sequence[Dict[str, Any]],
           records: Sequence[Any], target: Any, *,
           rule_mode: str = "current",
           target_issue: Any = None,
           arm: str = "",
           cost_per_bet: float = COST_PER_BET,
           with_money: bool = True,
           tax: bool = False,
           multipliers: Optional[Sequence[int]] = None) -> ArmResult:
    """把一组**固定**票集在真实开奖序列上回放。

    tickets 全程固定（不逐期重选）⇒ 各臂的差异只来自票集本身，
    不混入「重选频率」这个 Stage 0 未控制的变量。

    返回 `ArmResult`：hit = 单期是否达标（主终点）、nhit = 单期达标注数（哨兵）；
    `with_money=True`（默认）时额外给出净收益侧：
      `payout`(每期实得, 税前) / `net`(每期净额) / `money`(汇总，见 payout.money_series)。

    ★ 净收益侧复用本函数已算好的 (h, j) 命中矩阵，不重算一遍。

    Stage 2 倍投（`multipliers`，2026-10-25 新增）
    --------------------------------------------
    `multipliers[i]` = 第 i 张票下几倍（长度须 == 票数、取值 ≥ 1）。
    **倍数只进金额与成本，绝不进命中结构** —— 本函数里 `hit`/`nhit` 完全由
    `tickets` 的去重票集算出，代码路径上根本拿不到 `multipliers`
    ⇒ 「倍投不计入命中概率提升」是**结构性保证**，不是口头约定。

    传 `multipliers` 时：
      · `每期注数` = 去重票数（达标口径的基数）；
      · `每期实际注数` = Σ倍数（成本口径的基数，二者不同就必须分开报）；
      · `cost` = Σ每期实际注数 × cost_per_bet（不是 `n*K*2`）。
    """
    if rule_mode not in RULE_MODES:
        raise ValueError(f"rule_mode 必须是 {RULE_MODES} 之一，收到 {rule_mode!r}")
    recs = list(records)
    if not recs:
        raise ValueError("开奖记录为空")
    if target_issue is None:
        target_issue = recs[-1].期号

    mult = None
    if multipliers is not None:
        mult = np.asarray(list(multipliers), dtype=np.int64)
        if mult.size != len(list(tickets)):
            raise ValueError(f"倍数向量长度 {mult.size} != 票数 {len(tickets)}")
        if mult.size and int(mult.min()) < 1:
            raise ValueError("倍数必须 ≥ 1（倍投不会把某注的下注数降到 0）")

    _g, _w, m_in, _n = outcome_gate(lottery, target, target_issue)
    from ev.tier_coverage import _inner_pool_size
    n_in = _inner_pool_size(rules_for(lottery, target_issue), m_in)
    n_out = int(rules_for(lottery, target_issue).outer_range[1])

    A = _onehot(tickets, "红球", n_out)
    Bm = _onehot(tickets, "蓝球", n_in)
    R, B = draw_vectors(lottery, recs, n_out, n_in)

    h = (A.astype(np.int16) @ R.T.astype(np.int16))     # (K, n)
    j = (Bm.astype(np.int16) @ B.T.astype(np.int16))    # (K, n)

    n = len(recs)
    if rule_mode == "current":
        gate, _w_out, _mm, _n = outcome_gate(lottery, target, target_issue)
        ok = gate[h, j]                                  # (K, n) bool
    else:
        cache: Dict[str, np.ndarray] = {}
        ok = np.zeros(h.shape, dtype=bool)
        for p, rec in enumerate(recs):
            g = _gate_for(lottery, target, rec.期号, cache)
            ok[:, p] = g[h[:, p], j[:, p]]

    # ★Stage 2：hit / nhit **不读 mult**（倍投不得改变命中结构）。
    hit = ok.any(axis=0)
    nhit = ok.sum(axis=0).astype(np.int64)
    per_period = int(mult.sum()) if mult is not None else len(list(tickets))
    nb = np.full(n, per_period, dtype=np.int64)

    money: Dict[str, Any] = {}
    payout = net = None
    if with_money:
        from ev.arena import payout as MONEY
        money = MONEY.money_series(lottery, tickets, recs, rule_mode=rule_mode,
                                   target_issue=target_issue, tax=tax,
                                   h=h, j=j, cost_per_bet=cost_per_bet,
                                   multipliers=mult)
        payout = money["每期实得"]
        net = money["序列"]

    meta = {"规则口径": rule_mode, "目标期号": str(target_issue),
            "每期注数": len(list(tickets)), "哨兵口径": "单期达标注数",
            "成本口径": "Σ每期实际注数×单注成本"}
    if mult is not None:
        meta.update({
            "每期实际注数": per_period,
            "倍数分布": {int(m): int((mult == m).sum()) for m in sorted(set(int(x) for x in mult))},
            "倍投": True,
            "★倍投口径": "倍数只进金额与成本；hit/nhit 由去重票集决定，与倍数无关",
        })
    else:
        meta["倍投"] = False

    return ArmResult(臂=arm, 彩种=lottery, issues=[r.期号 for r in recs],
                     hit=hit, nhit=nhit, cost=float(nb.sum()) * float(cost_per_bet),
                     payout=payout, net=net, money=money,
                     # ★ 命中矩阵透出（2026-10-30）：供 tier_metrics 在同一批票上
                     #   复用算多门（≥10元 / ≥四等 / ≥三等），不重跑 replay。
                     #   它们是**本函数已经算完要丢掉**的中间量，透出的边际成本为 0。
                     h=h, j=j,
                     meta=meta)
