"""
Step 4: 风险度量与统计波动类
方差、标准差、信息熵、蒙特卡洛模拟、置信区间
"""

import logging
import math
from typing import Dict, List, Optional

import numpy as np
from scipy import stats as scipy_stats

from config import LOTTERY_CONFIG
from data.schema import LotteryData, get_schema, is_redblue
from pipeline.step1_probability import get_prize_data, comb

logger = logging.getLogger(__name__)


def variance_and_std(lottery_name: str) -> dict:
    """
    方差与标准差 — 衡量彩票收益的波动幅度
    Var(X) = E[(X - E(X))²] = Σ(x_i - μ)² * P_i
    """
    data = get_prize_data(lottery_name)
    payout = LOTTERY_CONFIG[lottery_name].get("payout", {}) or {}
    ticket_price = payout.get("cost", 2)

    # 计算期望值 μ
    mu = -ticket_price  # 先扣本金
    for level in data["levels"]:
        if level.get("is_floating"):
            amount = _est_float(lottery_name, level["prize_name"])
        else:
            amount = level.get("fixed_amount", 0)
        mu += amount * level["probability"]

    # 计算方差 Var = Σ(x_i - μ)² * P_i
    variance = 0.0
    # 不中奖的情况
    no_win_prob = 1 - data["total_win_probability"]
    variance += (-ticket_price - mu) ** 2 * no_win_prob
    # 各奖级
    for level in data["levels"]:
        if level.get("is_floating"):
            amount = _est_float(lottery_name, level["prize_name"])
        else:
            amount = level.get("fixed_amount", 0)
        net = amount - ticket_price
        variance += (net - mu) ** 2 * level["probability"]

    std = math.sqrt(variance)

    return {
        "lottery_name": lottery_name,
        "期望收益": round(mu, 4),
        "方差": round(variance, 2),
        "标准差": round(std, 2),
        "变异系数(CV)": round(std / abs(mu), 4) if mu != 0 else None,
        "说明": "方差越大，收益波动越剧烈。彩票标准差远大于期望值，风险极高。",
    }


def _est_float(lottery: str, prize: str) -> int:
    estimates = {
        "双色球": {"一等奖": 5_000_000, "二等奖": 200_000},
        "大乐透": {"一等奖": 5_000_000, "二等奖": 150_000},
    }
    return estimates.get(lottery, {}).get(prize, 0)


def information_entropy(lottery_name: str) -> dict:
    """
    信息熵 H(X) = -Σ P_i * log₂(P_i)
    衡量随机事件的不确定性
    """
    if is_redblue(lottery_name):
        cfgs = {
            "双色球": (33, 6, 16, 1),
            "大乐透": (35, 5, 12, 2),
        }
        r_total, r_pick, b_total, b_pick = cfgs[lottery_name]

        # 每个号码出现的概率（均匀分布下的理论熵）
        red_prob = r_pick / r_total
        blue_prob = b_pick / b_total

        # 红球熵 = 单个号码的熵 × 号码数
        red_entropy = r_total * (-red_prob * math.log2(red_prob) - (1 - red_prob) * math.log2(1 - red_prob))
        blue_entropy = b_total * (-blue_prob * math.log2(blue_prob) - (1 - blue_prob) * math.log2(1 - blue_prob))

        return {
            "lottery_name": lottery_name,
            "红球信息熵": round(red_entropy, 4),
            "蓝球信息熵": round(blue_entropy, 4),
            "总信息熵": round(red_entropy + blue_entropy, 4),
            "含义": "熵值越高，开奖结果的不确定性越大。彩票熵值远高于普通随机事件。",
        }

    # 数字型/乐透型：按分区计算理论熵（每区均匀分布 → 每位 log2(容量)）
    schema = get_schema(lottery_name)
    zone_entropy = {}
    total = 0.0
    for z in schema.zones:
        h = math.log2(z.size) if z.size > 1 else 0.0
        zone_entropy[z.name] = round(h, 4)
        total += h
    return {
        "lottery_name": lottery_name,
        "各分区信息熵": zone_entropy,
        "总信息熵": round(total, 4),
        "含义": "熵值越高，开奖结果的不确定性越大。数字型每位的理论熵为 log2(每位可取值数)。",
    }


def monte_carlo_simulation(
    lottery_name: str,
    simulations: int = 100000,
    n_per_draw: int = 1,
    ticket_price: int = 2,
) -> dict:
    """
    蒙特卡洛模拟 — 大规模随机仿真开奖结果
    模拟 simulations 次开奖，每次买 n_per_draw 注
    """
    if is_redblue(lottery_name):
        cfg = LOTTERY_CONFIG[lottery_name]

        r_range = cfg["red_range"]
        r_count = cfg["red_count"]
        b_range = cfg["blue_range"]
        b_count = cfg["blue_count"]

        # 随机生成号码
        np.random.seed(42)
        reds = np.random.randint(r_range[0], r_range[1] + 1, size=(simulations, r_count))
        blues = np.random.randint(b_range[0], b_range[1] + 1, size=(simulations, b_count))

        # 简单模型：计算各号码出现频率
        red_freq = np.zeros(r_range[1] + 1)
        blue_freq = np.zeros(b_range[1] + 1)

        for i in range(simulations):
            for n in reds[i]:
                red_freq[n] += 1
            for n in blues[i]:
                blue_freq[n] += 1

        red_freq = red_freq / simulations
        blue_freq = blue_freq / simulations

        # 理论频率
        red_theory = r_count / (r_range[1] - r_range[0] + 1)
        blue_theory = b_count / (b_range[1] - b_range[0] + 1)

        # 模拟误差
        red_rmse = math.sqrt(np.mean((red_freq[r_range[0]:] - red_theory) ** 2))
        blue_rmse = math.sqrt(np.mean((blue_freq[b_range[0]:] - blue_theory) ** 2))

        return {
            "lottery_name": lottery_name,
            "模拟参数": {"模拟次数": simulations, "每注号码数": n_per_draw},
            "红球模拟": {
                "理论频率": round(red_theory, 6),
                "模拟频率均值": round(float(np.mean(red_freq[r_range[0]:])), 6),
                "模拟误差(RMSE)": round(float(red_rmse), 6),
            },
            "蓝球模拟": {
                "理论频率": round(blue_theory, 6),
                "模拟频率均值": round(float(np.mean(blue_freq[b_range[0]:])), 6),
                "模拟误差(RMSE)": round(float(blue_rmse), 6),
            },
        }

    # 数字型：逐区模拟开奖频率 vs 理论均匀分布
    schema = get_schema(lottery_name)
    np.random.seed(42)
    zone_sims = {}
    for z in schema.zones:
        draws = np.random.randint(z.min, z.max + 1, size=simulations)
        freq = np.bincount(draws, minlength=z.max + 1).astype(float) / simulations
        theory = 1.0 / z.size
        rmse = math.sqrt(np.mean((freq[z.min:] - theory) ** 2))
        zone_sims[z.name] = {
            "理论频率": round(theory, 6),
            "模拟频率均值": round(float(np.mean(freq[z.min:])), 6),
            "模拟误差(RMSE)": round(float(rmse), 6),
        }
    return {
        "lottery_name": lottery_name,
        "模拟参数": {"模拟次数": simulations, "每注号码数": n_per_draw},
        "各分区模拟": zone_sims,
    }


def confidence_interval(lottery_name: str, n_draws: int = 1000) -> dict:
    """
    置信区间 — 对中奖概率的统计估计
    用中心极限定理估算 n_draws 期中奖次数的置信区间
    """
    data = get_prize_data(lottery_name)
    p = data["total_win_probability"]

    # 期望中奖次数
    expected = n_draws * p
    # 标准差
    std = math.sqrt(n_draws * p * (1 - p))

    # 95% 置信区间
    z_95 = 1.96
    ci_95 = (expected - z_95 * std, expected + z_95 * std)

    # 99% 置信区间
    z_99 = 2.576
    ci_99 = (expected - z_99 * std, expected + z_99 * std)

    return {
        "lottery_name": lottery_name,
        "期数": n_draws,
        "单期中奖概率": round(p, 6),
        "期望中奖次数": round(expected, 2),
        "95%置信区间": (round(ci_95[0], 2), round(ci_95[1], 2)),
        "99%置信区间": (round(ci_99[0], 2), round(ci_99[1], 2)),
        "解释": f"在 {n_draws} 期投注中，有 95% 的概率中奖次数落在 [{ci_95[0]:.1f}, {ci_95[1]:.1f}] 区间内",
    }


def historical_volatility(data: LotteryData) -> dict:
    """
    基于历史数据的实际波动分析
    计算各号码出现频率的实际标准差 vs 理论标准差
    """
    if is_redblue(data.lottery_name):
        cfg = LOTTERY_CONFIG[data.lottery_name]
        r_min, r_max = cfg["red_range"]
        b_min, b_max = cfg["blue_range"]

        # 红球频率
        red_freq = {i: 0 for i in range(r_min, r_max + 1)}
        for r in data.records:
            for n in r.红球:
                red_freq[n] = red_freq.get(n, 0) + 1

        red_actual_std = float(np.std(list(red_freq.values())) / len(data.records))
        red_theory_prob = cfg["red_count"] / (r_max - r_min + 1)
        red_theory_std = math.sqrt(red_theory_prob * (1 - red_theory_prob) / (r_max - r_min + 1))

        # 蓝球频率
        blue_freq = {i: 0 for i in range(b_min, b_max + 1)}
        for r in data.records:
            for n in r.蓝球:
                blue_freq[n] = blue_freq.get(n, 0) + 1

        blue_actual_std = float(np.std(list(blue_freq.values())) / len(data.records))
        blue_theory_prob = cfg["blue_count"] / (b_max - b_min + 1)
        blue_theory_std = math.sqrt(blue_theory_prob * (1 - blue_theory_prob) / (b_max - b_min + 1))

        return {
            "lottery_name": data.lottery_name,
            "总期数": len(data.records),
            "红球": {
                "理论频率标准差": round(red_theory_std, 6),
                "实际频率标准差": round(red_actual_std, 6),
                "偏离度": f"{(red_actual_std / red_theory_std - 1) * 100:.2f}%" if red_theory_std > 0 else "N/A",
            },
            "蓝球": {
                "理论频率标准差": round(blue_theory_std, 6),
                "实际频率标准差": round(blue_actual_std, 6),
                "偏离度": f"{(blue_actual_std / blue_theory_std - 1) * 100:.2f}%" if blue_theory_std > 0 else "N/A",
            },
            "结论": "实际标准差越接近理论值，说明开奖越接近随机均匀分布；偏离越大，可能存在异常。",
        }

    # 数字型：逐区频率波动
    schema = get_schema(data.lottery_name)
    zone_vol = {}
    for z in schema.zones:
        freq = {i: 0 for i in range(z.min, z.max + 1)}
        for r in data.records:
            for n in r.zone_numbers.get(z.name, []):
                freq[n] = freq.get(n, 0) + 1
        actual_std = float(np.std(list(freq.values())) / len(data.records))
        theory_prob = z.choose / z.size
        theory_std = math.sqrt(theory_prob * (1 - theory_prob) / z.size)
        zone_vol[z.name] = {
            "理论频率标准差": round(theory_std, 6),
            "实际频率标准差": round(actual_std, 6),
            "偏离度": f"{(actual_std / theory_std - 1) * 100:.2f}%" if theory_std > 0 else "N/A",
        }
    return {
        "lottery_name": data.lottery_name,
        "总期数": len(data.records),
        "各分区": zone_vol,
        "结论": "实际标准差越接近理论值，说明开奖越接近随机均匀分布；偏离越大，可能存在异常。",
    }


def run(data: LotteryData) -> dict:
    """
    Step4 主入口
    """
    name = data.lottery_name
    logger.info(f"Step4: 风险度量与统计波动 — {name}")

    var_info = variance_and_std(name)
    entropy = information_entropy(name)
    mc = monte_carlo_simulation(name, simulations=50000)
    ci = confidence_interval(name, n_draws=1000)
    hist_vol = historical_volatility(data)

    return {
        "step": 4,
        "lottery_name": name,
        "方差与标准差": var_info,
        "信息熵": entropy,
        "蒙特卡洛模拟": mc,
        "置信区间": ci,
        "历史波动分析": hist_vol,
    }
