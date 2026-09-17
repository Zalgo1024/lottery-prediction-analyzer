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


def expected_value(lottery_name: str) -> dict:
    """
    单注彩票期望收益 E(X) = Σ(奖金 × 概率)
    返回各奖级贡献 + 总计
    """
    data = get_prize_data(lottery_name)
    payout = LOTTERY_CONFIG[lottery_name].get("payout", {}) or {}
    ticket_price = payout.get("cost", 2)
    total_ev = -ticket_price  # 先扣本金
    breakdown = []

    for level in data["levels"]:
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


def _estimate_floating_prize(lottery_name: str, prize_name: str) -> int:
    """
    根据历史数据估算浮动奖的期望金额
    一等奖：历史均值约 500-1000 万
    二等奖：历史均值约 10-50 万
    """
    estimates = {
        "双色球": {"一等奖": 5_000_000, "二等奖": 200_000},
        "大乐透": {"一等奖": 5_000_000, "二等奖": 150_000},
    }
    return estimates.get(lottery_name, {}).get(prize_name, 0)


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
