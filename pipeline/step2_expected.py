"""
Step 2: 收益与期望类
数学期望（期望收益）、返奖率、投注回报率、现金流折现（分期奖金现值）
"""

import logging
from typing import Dict, List, Optional

from config import LOTTERY_CONFIG
from data.schema import LotteryData
from pipeline.step1_probability import get_prize_data

logger = logging.getLogger(__name__)


def _is_placeholder_table(levels: List[dict]) -> bool:
    """step1 的奖级表是否只是「精确全中」占位符（= 该彩种未被 step1 覆盖）。"""
    return (len(levels) == 1
            and str((levels[0] or {}).get("prize_name") or "") == "精确全中")


def _expected_via_profit(lottery_name: str, ticket_price: float) -> dict:
    """用 `ev.profit` 的规则表算期望（七乐彩 7 档 / 快乐8 按玩法 39 档）。

    ★ 2026-11-02 修复：step1 的 `get_prize_data` 只覆盖 4 个数字型 + 双/大。
      对七乐彩/快乐8 它返回**单档占位**「精确全中，金额 0」，于是统计页与看板
      建议打出「单注期望收益 **−2.0** 元 / 各奖级期望明细：精确全中 0 元」——
      这不是"这两个彩种期望特别差"，而是把「模型不覆盖」画成了
      「只有一个奖级、而且不派奖」。金额未知的档一律**排除出求和并单独列出**，
      不得当 0（当 0 会偏低，按上限虚报会偏高）。
    """
    from ev.payout import grade_probabilities
    from ev.profit.rules import rules_for

    rules = rules_for(lottery_name)
    probs = grade_probabilities(lottery_name)

    breakdown: List[dict] = []
    unknown: List[str] = []
    gained = 0.0
    for grade, p in probs.items():
        rule = rules.grades.get(grade)
        if rule is None:
            amt = None
        elif rule.kind == "fixed":
            amt = float(rule.fixed) if rule.fixed is not None else None
        else:
            amt = float(rule.nominal) if rule.nominal is not None else None
        if amt is None:
            unknown.append(grade)
        else:
            gained += amt * p
        breakdown.append({
            "prize": grade,
            "amount": amt,
            "probability": p,
            "expected_value": None if amt is None else round(amt * p, 8),
        })

    total_ev = gained - ticket_price
    conclusion = (f"名义期望 {total_ev:.4f} 元/注（按规则表名义单注金额，未扣税）；"
                  f"长期为负，且与选号无关。")
    out = {
        "lottery_name": lottery_name,
        "ticket_price": ticket_price,
        "单注期望收益": round(total_ev, 6),
        "各奖级期望明细": breakdown,
        "口径": f"ev.profit 规则表 {rules.rule_version}（名义金额，实际派奖含限赔/封顶）",
        "结论": conclusion,
    }
    if lottery_name == "快乐8":
        # 快乐8 的奖级表随玩法变（选一~选十），这里按默认玩法算，必须说明。
        out["玩法"] = "选十（默认；选一~选九的档位与返奖率不同，见 cli.py payout --play）"
    if unknown:
        out["金额未知档"] = unknown
        out["结论"] = conclusion + (
            f"另有 {len(unknown)} 档名义金额未知（{'、'.join(unknown)}），未计入求和 ⇒ 该数值偏低。")
    return out


def expected_value(lottery_name: str) -> dict:
    """
    单注彩票期望收益 E(X) = Σ(奖金 × 概率)
    返回各奖级贡献 + 总计
    """
    data = get_prize_data(lottery_name)
    payout = LOTTERY_CONFIG[lottery_name].get("payout", {}) or {}
    ticket_price = payout.get("cost", 2)
    levels = data.get("levels") or []

    # ★ 未被 step1 覆盖的彩种：改走 ev.profit 真源，不要拿占位档算出一个假 EV。
    if _is_placeholder_table(levels):
        return _expected_via_profit(lottery_name, float(ticket_price))

    total_ev = -ticket_price  # 先扣本金
    breakdown = []

    for level in levels:
        if level.get("is_floating"):
            # 浮动奖用历史均值估算
            amount = _estimate_floating_prize(lottery_name, level["prize_name"])
        else:
            amount = level.get("fixed_amount", 0)

        ev = amount * level["probability"]
        total_ev += ev

        breakdown.append({
            "prize": level["prize_name"],
            "amount": amount,
            "probability": level["probability"],
            "expected_value": round(ev, 6),
        })

    return {
        "lottery_name": lottery_name,
        "ticket_price": ticket_price,
        "单注期望收益": round(total_ev, 6),
        "各奖级期望明细": breakdown,
        "结论": f"期望收益为 {total_ev:.4f} 元，{'负期望值，长期投注必然亏损' if total_ev < 0 else '正期望值'}"
    }


def _estimate_floating_prize(lottery_name: str, prize_name: str) -> float:
    """浮动奖单注金额的历史估算 —— 单一真源在 `data.prize_table`。

    ★ 历史上这里是三份手写副本之一（step2/step4/step5 各一份），键写「一等奖」而
      规则层返回「一等」⇒ 查不到 ⇒ 浮动奖在 EV 里**静默算成 0**。
    """
    from data.prize_table import estimate_floating_prize

    return estimate_floating_prize(lottery_name, prize_name)


def historical_return_rate(data: LotteryData) -> dict:
    """
    基于历史数据的实际返奖率分析
    计算每期的：总奖金 / 总投注额
    """
    if not data.records or not hasattr(data.records[0], "总投注额"):
        return {"error": "数字型彩种无历史投注额数据，跳过实际返奖率统计"}

    returns = []
    total_bonus_all = 0
    total_wager_all = 0
    valid_periods = 0

    for record in data.records:
        wager = record.总投注额
        if not wager or wager == 0:
            continue

        # 估算该期总奖金（一等奖+二等奖+固定奖估算）
        bonus = 0
        if record.一等奖注数 and record.一等奖奖金:
            bonus += record.一等奖注数 * record.一等奖奖金
        if record.二等奖注数 and record.二等奖奖金:
            bonus += record.二等奖注数 * record.二等奖奖金
        # 固定奖按中奖注数估算（简化：用理论中奖率 * 投注额 / 2 * 平均固定奖金）
        # 此处仅统计一二等奖实际数据
        # 完整的固定奖需要各奖级注数数据

        rate = bonus / wager if wager > 0 else 0
        returns.append(rate)
        total_bonus_all += bonus
        total_wager_all += wager
        valid_periods += 1

    if not returns:
        return {"error": "无有效数据"}

    # 加权平均返奖率
    weighted_return = total_bonus_all / total_wager_all if total_wager_all > 0 else 0

    return {
        "lottery_name": data.lottery_name,
        "valid_periods": valid_periods,
        "总返奖率(仅一+二等奖)": round(weighted_return * 100, 4),
        "最大单期返奖率": round(max(returns) * 100, 4),
        "最小单期返奖率": round(min(returns) * 100, 4),
        "平均返奖率": round(sum(returns) / len(returns) * 100, 4),
        "说明": "返奖率仅统计一、二等奖实际派发奖金，不含固定奖（缺各奖级注数数据）",
    }


def payout_rate_official(lottery_name: str) -> dict:
    """
    理论返奖率（官方指标）
    返奖率 = 总派发奖金 / 总投注金额
    彩票官方综合返奖率通常为 50%-70%
    """
    data = get_prize_data(lottery_name)
    payout = LOTTERY_CONFIG[lottery_name].get("payout", {}) or {}
    ticket_price = payout.get("cost", 2)
    expected_payout = 0  # 期望单注派奖金额

    for level in data["levels"]:
        if level.get("is_floating"):
            amount = _estimate_floating_prize(lottery_name, level["prize_name"])
        else:
            amount = level.get("fixed_amount", 0)
        expected_payout += amount * level["probability"]

    rate = expected_payout / ticket_price

    return {
        "lottery_name": lottery_name,
        "ticket_price": ticket_price,
        "期望单注派奖": round(expected_payout, 6),
        "理论返奖率": round(rate * 100, 4),
        "说明": f"理论返奖率 {rate*100:.2f}%，在行业 50%-70% 范围内" if 0.5 <= rate <= 0.7 else f"理论返奖率 {rate*100:.2f}%，请注意核实",
    }


def discounted_cashflow(
    total_prize: float,
    years: int,
    annual_rate: float = 0.03,
) -> dict:
    """
    现金流折现 — 分期领取奖金的现值
    PV = Σ(FV_t / (1 + r)^t)

    参数：
    - total_prize: 奖金总额
    - years: 分期年数
    - annual_rate: 折现率（年化，默认 3%）
    """
    annual_payment = total_prize / years
    pv_total = 0.0
    schedule = []

    for t in range(1, years + 1):
        pv = annual_payment / ((1 + annual_rate) ** t)
        pv_total += pv
        schedule.append({"year": t, "payment": round(annual_payment, 2), "pv": round(pv, 2)})

    return {
        "total_nominal": total_prize,
        "years": years,
        "discount_rate": annual_rate,
        "pv_total": round(pv_total, 2),
        "折价比例": f"{(1 - pv_total / total_prize) * 100:.2f}%",
        "分期明细": schedule,
    }


def run(data: LotteryData) -> dict:
    """
    Step2 主入口
    """
    name = data.lottery_name
    logger.info(f"Step2: 收益与期望计算 — {name}")

    ev = expected_value(name)
    historical = historical_return_rate(data)
    official_rate = payout_rate_official(name)

    # 现金流折现示例：头奖 1000 万分 30 年领取
    dcf = discounted_cashflow(total_prize=10_000_000, years=30, annual_rate=0.03)

    return {
        "step": 2,
        "lottery_name": name,
        "expected_value": ev,
        "historical_return_rate": historical,
        "official_return_rate": official_rate,
        "discounted_cashflow_example": dcf,
    }
