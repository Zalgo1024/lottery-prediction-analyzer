# -*- coding: utf-8 -*-
"""ev/profit/conservative_ev.py —— 保守情景下的净 EV 与「要不要考虑倍投」的判决。

任务 4 的三条要求，逐条落到这里
--------------------------------
① **继续全单倍**：本模块不提供倍投参数，也不接受倍数向量。
   倍投不改变任何一注的中奖概率、不产生新结局 ⇒ 同预算下 `P(并集)` 只会下降
   （同样的钱买了更少的组合）。要谈倍投必须先过本模块的判决。
② **奖金分摊情景下的净 EV**：直接复用 `ev.profit.delta_scenario.evaluate()`
   （δ 只作用一等、税后、按当期规则版本分摊），不做第二套实现。
③ **只有保守假设下仍有正期望，才考虑倍数分配**：`verdict()` 只认**最保守那一格**
   （δ=0 且奖池 −30% 且税后）的净 EV 符号。别的格为正不算通过。

为什么判决几乎是确定的「不参与」
--------------------------------
彩票资金按游戏规则分为奖金、发行费与公益金，只有奖金部分构成返奖来源，
故长期返奖率显著低于 1 —— 这不是可以通过选号或倍投弥补的缺口。
`delta_scenario.structural_bound()` 给出的硬界是：即使 δ→1（我独享且被单注封顶截断），
一等奖贡献最多抬到 `P(一等) × 封顶税后`；若这个上限仍补不上「转正所需缺口」，
则**任何奖池规模、任何分摊假设下都不可能转正**。这条判决不需要情景假设。

★ 「未通过」的正确读法：不是「已证明永远不可能盈利」，而是
  「在**当前可得的数据与已登记的情景网格内**，找不到正期望的证据」。
  本项目没有分组合的实际购买量（死数据），所以 δ 只能是假设，不能是结论。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from ev.mechanism.narrative import EV_NEGATIVE_CAVEAT
from ev.profit import delta_scenario as DS
from ev.profit.payouts import build_ctx

__all__ = ["CONSERVATIVE_DELTA", "CONSERVATIVE_POOL_ERROR",
           "conservative_scenario", "grid", "verdict", "HONEST_NOTE"]

# 保守情景的定义：无冷门优势（δ=0）+ 奖池比预测低 30% + 税后
CONSERVATIVE_DELTA = 0.0
CONSERVATIVE_POOL_ERROR = -0.30

HONEST_NOTE = (
    "本判决只用**当期已知**的规则版本、奖池与销量预测（PIT 口径，不含未来）。"
    "δ（冷门优势）是假设情景变量而不是实测规律：项目没有分组合的实际购买量，"
    "冷门分数只是代理 ⇒ 结论以区间形式成立，引用时不得取单点。"
    "「未通过」= 在所登记的情景网格内找不到正期望的证据，不等于证明永远不可能。"
    + EV_NEGATIVE_CAVEAT
)


def conservative_scenario(lottery: str, issue: Any, *,
                          sales_override: Optional[float] = None) -> Dict[str, Any]:
    """最保守那一格的净 EV（δ=0、奖池 −30%、税后）。

    返回 `evaluate()` 的结果，另加 `情景名` 与 `是否正期望`。
    """
    ctx = build_ctx(lottery, issue, sales_override=sales_override)
    row = DS.evaluate(ctx, CONSERVATIVE_DELTA, CONSERVATIVE_POOL_ERROR)
    row["情景名"] = f"δ={CONSERVATIVE_DELTA} + 奖池 {CONSERVATIVE_POOL_ERROR:+.0%} + 税后"
    row["是否正期望"] = bool(row["净EV_元每注"] > 0)
    return row


def grid(lottery: str, issue: Any, *,
         deltas: Sequence[float] = DS.DELTA_GRID,
         pool_errors: Sequence[float] = DS.POOL_ERROR_GRID,
         sales_override: Optional[float] = None) -> Dict[str, Any]:
    """完整的 (δ × 奖池误差) 网格 + 结构性上限。区间形态，不取单点。"""
    ctx = build_ctx(lottery, issue, sales_override=sales_override)
    cells = []
    for d in deltas:
        for e in pool_errors:
            r = DS.evaluate(ctx, float(d), float(e))
            cells.append({"δ": float(d), "奖池误差": float(e),
                          "净EV_元每注": r["净EV_元每注"],
                          "净EV_元每百元": r["净EV_元每百元"]})
    bound = DS.structural_bound(ctx)
    pos = [c for c in cells if c["净EV_元每注"] > 0]
    return {
        "彩种": lottery, "期号": str(issue),
        "规则版本": ctx.rules.rule_version,
        "票价": float(ctx.rules.avg_ticket_price),
        "格": cells,
        "正情景数": len(pos),
        "全网格格数": len(cells),
        "最好的格": max(cells, key=lambda c: c["净EV_元每注"]) if cells else None,
        "结构性上限": bound,
        "警告": list(ctx.warnings),
        "诚实边界": HONEST_NOTE,
    }


def verdict(lottery: str, issue: Any, *,
            sales_override: Optional[float] = None) -> Dict[str, Any]:
    """「要不要考虑倍数分配」的判决（只看最保守那一格）。

    三档输出（**没有**「建议加倍」这一档，因为倍投不会改善任何一注的期望）：
      · `不参与`   —— 保守情景净 EV ≤ 0；
      · `仅情景内正期望` —— 保守情景 > 0 但结构性上限仍不足以支撑「靠倍投追盈利」；
      · `可进一步评估倍数分配` —— 保守情景 > 0 **且** 结构性上限显示缺口可被补上。
        这一档在当前数据下不可达；若将来可达，必须先登记新的预注册块再动手。
    """
    con = conservative_scenario(lottery, issue, sales_override=sales_override)
    g = grid(lottery, issue, sales_override=sales_override)
    bound = g["结构性上限"]
    can_close = bool(bound.get("δ上限能否补上缺口"))

    if con["净EV_元每注"] > 0 and can_close:
        verdict_text = "可进一步评估倍数分配"
        why = ("保守情景已为正，且一等奖封顶上限足以补上缺口 ⇒ 可进入倍数分配的独立评估"
               "（必须先登记新预注册块）")
    elif con["净EV_元每注"] > 0:
        verdict_text = "仅情景内正期望"
        why = ("只在某些情景假设下为正；结构性上限仍补不上缺口 ⇒ "
               "不得据情景单点放大投入")
    else:
        verdict_text = "不参与"
        why = ("最保守情景净 EV ≤ 0，且一等奖封顶的结构性上限补不上缺口 ⇒ "
               "全单倍参与；倍投只会放大方差与回撤，不改善期望")

    return {
        "彩种": lottery, "期号": str(issue),
        "判决": verdict_text, "理由": why,
        "保守情景": con,
        "保守净EV_元每注": con["净EV_元每注"],
        "保守净EV_元每百元": con["净EV_元每百元"],
        "正情景数": g["正情景数"], "格数": g["全网格格数"],
        "结构性上限能否补缺口": can_close,
        "倍投口径": "全单倍（本模块不接受倍数向量：倍投不产生新结局，只减少组合数）",
        "全网格最好": g["最好的格"],
        "警告": g["警告"],
        "诚实边界": HONEST_NOTE,
    }
