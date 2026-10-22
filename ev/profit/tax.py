# -*- coding: utf-8 -*-
"""ev/profit/tax.py —— 偶然所得税（同日同彩种合并口径）。

用户指定口径：**同期合并计**
    把同一彩种、同一期、整组 S 的所有中奖金额加总，超过 10000 元则按 20% 全额计征。

★ 必须在**逐结局**层面计税再求期望，不能对期望收税：
      E[0.2·gross·1{gross>10000}] ≠ 0.2·E[gross]·P(gross>10000)
  示数函数不可交换，而整组 gross 恰好有质量堆在 3000 / 6000 / 10000 附近
  （三等 3000 的 3~4 注），阈值附近的跳跃会被完全抹平。

★ 断崖提示：gross 从 10000 → 10001，税从 0 → 2000.2，净收益从 9998 掉到 7998.8。
  因此「净盈利概率」的分布在 1 万处有跳变，**不许做平滑/正态近似**。
"""
from __future__ import annotations

from typing import Any, Dict

TAX_RATE = 0.20
TAX_THRESHOLD = 10000.0

BASIS_ISSUE = "issue_aggregate"
BASIS_TICKET = "per_ticket"

BASIS_NOTE = {
    BASIS_ISSUE: ("同期同彩种合并计税：整组 S 的中奖金额合计 > 10000 元时按 20% 全额计征。"
                  "官方实务按「一次中奖收入」认定，本模型按合并口径处理，属**保守化**选择。"),
    BASIS_TICKET: "逐注计税：每一注中奖金额 > 10000 元时按 20% 全额计征。",
}


def tax_on(gross: float, basis: str = BASIS_ISSUE,
           threshold: float = TAX_THRESHOLD, rate: float = TAX_RATE) -> float:
    """按指定口径对**一个总额**计税。严格大于阈值才征（10000 整不收税）。"""
    if gross is None or gross <= 0:
        return 0.0
    if basis == BASIS_ISSUE:
        return rate * gross if gross > threshold else 0.0
    raise ValueError(f"tax_on 只处理合并口径；逐注口径请对单注金额逐次调用: {basis}")


def tax_on_ticket(amount: float, threshold: float = TAX_THRESHOLD,
                  rate: float = TAX_RATE) -> float:
    """逐注口径（仅作对照展示，不作判决）。"""
    if amount is None or amount <= 0:
        return 0.0
    return rate * amount if amount > threshold else 0.0


def net_of(gross: float, n_tickets: int, cost: float,
           basis: str = BASIS_ISSUE) -> Dict[str, Any]:
    """单期结算：gross → 税 → 净收益。字段保持税前/税后分离。"""
    tax = tax_on(gross, basis=basis)
    spend = float(n_tickets) * float(cost)
    return {
        "注数": int(n_tickets),
        "成本": spend,
        "税前奖金": float(gross),
        "税": tax,
        "税后奖金": float(gross) - tax,
        "净收益": float(gross) - tax - spend,
        "税口径": basis,
        "税口径说明": BASIS_NOTE[basis],
    }
