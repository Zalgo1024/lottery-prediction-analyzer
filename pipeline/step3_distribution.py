"""
Step 3: 统计分布与多次试验模型
二项分布、几何分布（首次中奖期望）、中心极限定理
"""

import logging
import math
from typing import Dict, List, Optional

import numpy as np
from scipy import stats as scipy_stats

from config import LOTTERY_CONFIG
from data.schema import LotteryData
from pipeline.step1_probability import get_prize_data, comb

logger = logging.getLogger(__name__)


def binomial_probability(
    lottery_name: str,
    n_trials: int,
    k_wins: int,
    prize_level: Optional[str] = None,
) -> float:
    """
    二项分布：P(X=k) = C(n,k) * p^k * (1-p)^(n-k)
    计算 n 期投注中恰好中 k 次的概率

    参数：
    - prize_level: 指定奖级，None = 至少六等奖及以上
    """
    data = get_prize_data(lottery_name)

    if prize_level:
        p = 0.0
        for level in data["levels"]:
            p += level["probability"]
            if level["prize_name"] == prize_level:
                break
    else:
        p = data["total_win_probability"]

    return comb(n_trials, k_wins) * (p ** k_wins) * ((1 - p) ** (n_trials - k_wins))


def binomial_cumulative(
    lottery_name: str,
    n_trials: int,
    k_min: int = 1,
    prize_level: Optional[str] = None,
) -> float:
    """
    二项分布累积概率：P(X ≥ k_min)
    计算 n 期投注中至少中 k_min 次的概率
    """
    total = 0.0
    for k in range(k_min, n_trials + 1):
        total += binomial_probability(lottery_name, n_trials, k, prize_level)
    return total


def geometric_distribution(
    lottery_name: str,
    prize_level: Optional[str] = None,
) -> dict:
    """
    几何分布 — 首次中奖期望
    P(X=k) = (1-p)^(k-1) * p
    E(X) = 1/p

    返回首次中奖的期望期数和各期概率
    """
    data = get_prize_data(lottery_name)

    if prize_level:
        p = 0.0
        for level in data["levels"]:
            p += level["probability"]
            if level["prize_name"] == prize_level:
                break
    else:
        p = data["total_win_probability"]

    expected_k = 1 / p if p > 0 else float("inf")

    # 计算前 50 期的累积概率
    cumulative = 0.0
    probs = []
    for k in range(1, 51):
        prob = ((1 - p) ** (k - 1)) * p
        cumulative += prob
        if k <= 20 or k % 10 == 0:
            probs.append({"期数": k, "概率": round(prob, 6), "累积概率": round(cumulative, 6)})

    # 中位数（累积概率达到 50% 的期数）
    median_k = math.ceil(-math.log(2) / math.log(1 - p)) if p < 1 else 1

    return {
        "单次中奖概率": round(p, 8),
        "期望首次中奖期数": round(expected_k, 2),
        "中位数首次中奖期数": median_k,
        "50期内累积中奖概率": round(cumulative, 6),
        "概率明细": probs,
    }


def simulate_repeated_betting(
    lottery_name: str,
    n_trials: int = 100,
    simulations: int = 10000,
    ticket_price: int = 2,
) -> dict:
    """
    蒙特卡洛模拟多次投注的盈亏分布
    模拟 simulations 次，每次买 n_trials 期
    """
    data = get_prize_data(lottery_name)

    # 简化模拟：只判断是否中奖，不细化奖金（因为固定奖金额小，浮动奖波动大）
    p = data["total_win_probability"]

    # 用二项分布模拟
    np.random.seed(42)
    wins = np.random.binomial(n=n_trials, p=p, size=simulations)

    # 统计
    mean_wins = float(np.mean(wins))
    std_wins = float(np.std(wins))
    max_wins = int(np.max(wins))
    min_wins = int(np.min(wins))
    zero_wins_ratio = float(np.mean(wins == 0))

    # 净收益（每次中奖平均奖金：数字型用精确全中奖金，红蓝仍用示意值 5 元）
    payout = LOTTERY_CONFIG[lottery_name].get("payout", {}) or {}
    avg_prize_per_win = float(payout.get("exact_match") or 5.0)
    net_profits = wins * avg_prize_per_win - n_trials * ticket_price
    mean_profit = float(np.mean(net_profits))
    profit_std = float(np.std(net_profits))
    profit_ratio = float(np.mean(net_profits > 0))

    return {
        "lottery_name": lottery_name,
        "模拟参数": {"每轮期数": n_trials, "模拟次数": simulations, "单注成本": ticket_price},
        "中奖次数统计": {
            "均值": round(mean_wins, 2),
            "标准差": round(std_wins, 2),
            "最大": max_wins,
            "最小": min_wins,
            "一次都没中概率": round(zero_wins_ratio * 100, 2),
        },
        "简化盈亏统计": {
            "平均净收益": round(mean_profit, 2),
            "收益标准差": round(profit_std, 2),
            "盈利比例": round(profit_ratio * 100, 2),
            "说明": "每注中奖按平均 5 元估算，仅作趋势参考",
        },
    }


def central_limit_theorem_demo(lottery_name: str) -> dict:
    """
    中心极限定理演示
    展示大量独立投注后，平均收益趋近于整体数学期望
    """
    data = get_prize_data(lottery_name)
    p = data["total_win_probability"]

    ticket_price = 2
    single_ev = -ticket_price + p * 5  # 简化估算（示意）

    # 不同样本量的均值分布
    sample_sizes = [10, 100, 1000, 10000]
    np.random.seed(42)
    demo = []
    for n in sample_sizes:
        samples = np.random.binomial(n=n, p=p, size=5000)
        avg_wins = samples / n
        avg_profit = avg_wins * 5 - ticket_price
        demo.append({
            "样本量": n,
            "样本均值(中奖率)": round(float(np.mean(avg_wins)), 6),
            "理论值(中奖率)": round(p, 6),
            "标准差": round(float(np.std(avg_wins)), 6),
            "随样本量增加均值趋于理论值": "是" if abs(np.mean(avg_wins) - p) < 0.01 else "否",
        })

    return {
        "lottery_name": lottery_name,
        "理论中奖率": round(p, 6),
        "简化单注期望收益": round(single_ev, 4),
        "CLT 演示": demo,
        "结论": "随着样本量增大，样本平均收益趋近于整体数学期望（负期望值），"
                "解释了长期投注必然亏损的数学原理。",
    }


def run(data: LotteryData) -> dict:
    """
    Step3 主入口
    """
    name = data.lottery_name
    logger.info(f"Step3: 统计分布与多次试验 — {name}")

    # 二项分布：买 100 期恰好中 0~5 次的概率
    binom_probs = {}
    for k in range(0, 11):
        binom_probs[f"中{k}次"] = round(binomial_probability(name, 100, k), 6)

    # 至少中一次
    cum_at_least_1 = binomial_cumulative(name, 100, k_min=1)

    # 几何分布：首次中奖期望
    geo = geometric_distribution(name)

    # 蒙特卡洛模拟
    sim = simulate_repeated_betting(name, n_trials=100, simulations=5000)

    # 中心极限定理
    clt = central_limit_theorem_demo(name)

    return {
        "step": 3,
        "lottery_name": name,
        "二项分布(100期)": {
            "各中奖次数概率": binom_probs,
            "至少中一次概率": round(cum_at_least_1, 6),
        },
        "几何分布(首次中奖)": geo,
        "蒙特卡洛模拟": sim,
        "中心极限定理": clt,
    }
