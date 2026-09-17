"""
Step 5: 风控与最优投注模型
凯利公式（最优投注比例）、夏普比率（风险调整收益）、假设检验（公平性）、博弈论分析
"""

import logging
import math
from typing import Dict, List, Optional

import numpy as np
from scipy import stats as scipy_stats

from config import LOTTERY_CONFIG
from data.schema import LotteryData, get_schema, is_redblue
from pipeline.step1_probability import get_prize_data

logger = logging.getLogger(__name__)


def kelly_criterion(lottery_name: str) -> dict:
    """
    凯利公式（凯利准则）：f* = (b * p - q) / b
    其中 b = 赔率, p = 胜率, q = 败率 = 1 - p
    计算最优投注比例（占本金比例）

    彩票为负期望值，f* < 0，数学上不建议投注
    """
    data = get_prize_data(lottery_name)
    payout = LOTTERY_CONFIG[lottery_name].get("payout", {}) or {}
    ticket_price = payout.get("cost", 2)

    # 对每个奖级计算凯利比例
    results = []
    for level in data["levels"]:
        p = level["probability"]
        if p == 0:
            continue

        if level.get("is_floating"):
            amount = _est_float(lottery_name, level["prize_name"])
        else:
            amount = level.get("fixed_amount", 0)

        # 赔率 b = (奖金 - 本金) / 本金
        b = (amount - ticket_price) / ticket_price if ticket_price > 0 else 0
        q = 1 - p

        # 凯利比例
        if b > 0:
            f_star = (b * p - q) / b
        else:
            f_star = -q  # 赔率为 0 或负时不应投注

        results.append({
            "prize": level["prize_name"],
            "胜率": round(p, 8),
            "赔率": round(b, 2),
            "凯利比例": round(f_star, 8),
            "建议": "不建议投注" if f_star <= 0 else "可考虑",
        })

    # 整体凯利（用总中奖概率）
    p_total = data["total_win_probability"]
    # 综合赔率（用期望奖金估算）
    expected_prize = 0
    for level in data["levels"]:
        if level.get("is_floating"):
            amount = _est_float(lottery_name, level["prize_name"])
        else:
            amount = level.get("fixed_amount", 0)
        expected_prize += amount * level["probability"]

    b_total = (expected_prize - ticket_price) / ticket_price if ticket_price > 0 else 0
    if b_total > 0:
        f_total = (b_total * p_total - (1 - p_total)) / b_total
    else:
        f_total = -1

    return {
        "lottery_name": lottery_name,
        "票价": ticket_price,
        "期望奖金": round(expected_prize, 4),
        "总中奖概率": round(p_total, 6),
        "综合赔率": round(b_total, 4),
        "综合凯利比例": round(f_total, 6),
        "结论": f"f* = {f_total:.6f} < 0，彩票为负期望值，{lottery_name} 在数学上不建议投注"
               if f_total < 0 else f"f* = {f_total:.6f}",
        "各奖级明细": results,
    }


def _est_float(lottery: str, prize: str) -> int:
    estimates = {
        "双色球": {"一等奖": 5_000_000, "二等奖": 200_000},
        "大乐透": {"一等奖": 5_000_000, "二等奖": 150_000},
    }
    return estimates.get(lottery, {}).get(prize, 0)


def sharpe_ratio(lottery_name: str, risk_free_rate: float = 0.025) -> dict:
    """
    夏普比率：SR = (E(Rp) - Rf) / σp
    衡量风险调整后收益

    参数：
    - risk_free_rate: 无风险收益率（年化，默认 2.5% ≈ 国债）
    """
    data = get_prize_data(lottery_name)
    payout = LOTTERY_CONFIG[lottery_name].get("payout", {}) or {}
    ticket_price = payout.get("cost", 2)

    # 计算期望收益率
    expected_prize = 0
    for level in data["levels"]:
        if level.get("is_floating"):
            amount = _est_float(lottery_name, level["prize_name"])
        else:
            amount = level.get("fixed_amount", 0)
        expected_prize += amount * level["probability"]

    # 单期期望收益率
    expected_return = (expected_prize - ticket_price) / ticket_price

    # 标准差（用 step4 的逻辑简化）
    no_win_prob = 1 - data["total_win_probability"]
    variance = (-1 - expected_return) ** 2 * no_win_prob
    for level in data["levels"]:
        if level.get("is_floating"):
            amount = _est_float(lottery_name, level["prize_name"])
        else:
            amount = level.get("fixed_amount", 0)
        r = (amount - ticket_price) / ticket_price
        variance += (r - expected_return) ** 2 * level["probability"]

    std_dev = math.sqrt(variance)

    # 夏普比率（年化，假设每天开奖，一年约 150 期）
    periods_per_year = 150
    annual_return = (1 + expected_return) ** periods_per_year - 1
    annual_std = std_dev * math.sqrt(periods_per_year)

    sr = (annual_return - risk_free_rate) / annual_std if annual_std > 0 else 0

    return {
        "lottery_name": lottery_name,
        "单期期望收益率": round(expected_return * 100, 4),
        "单期收益标准差": round(std_dev * 100, 4),
        "年化期望收益率": round(annual_return * 100, 4),
        "年化标准差": round(annual_std * 100, 4),
        "无风险利率": f"{risk_free_rate * 100}%",
        "夏普比率": round(sr, 4),
        "评价": "差（远低于 0，不值得投资）" if sr < 0 else "一般" if sr < 1 else "良好",
        "对比": "上证指数夏普比率约 0.3-0.5，标普500约 0.5-0.8，彩票为显著负值",
    }


def hypothesis_test_fairness(data: LotteryData) -> dict:
    """
    假设检验 — 检测彩票开奖是否公平（服从均匀分布）
    用卡方检验比较各号码出现频率与理论均匀分布
    """
    alpha = 0.05  # 显著性水平

    if is_redblue(data.lottery_name):
        cfg = LOTTERY_CONFIG[data.lottery_name]
        r_min, r_max = cfg["red_range"]
        b_min, b_max = cfg["blue_range"]

        # 红球卡方检验
        observed_red = [0] * (r_max + 1)
        for r in data.records:
            for n in r.红球:
                observed_red[n] += 1

        total_red_draws = len(data.records) * cfg["red_count"]
        expected_red = total_red_draws / (r_max - r_min + 1)
        observed_red = observed_red[r_min:]

        chi2_red, p_red = scipy_stats.chisquare(f_obs=observed_red, f_exp=[expected_red] * len(observed_red))

        # 蓝球卡方检验
        observed_blue = [0] * (b_max + 1)
        for r in data.records:
            for n in r.蓝球:
                observed_blue[n] += 1

        total_blue_draws = len(data.records) * cfg["blue_count"]
        expected_blue = total_blue_draws / (b_max - b_min + 1)
        observed_blue = observed_blue[b_min:]

        chi2_blue, p_blue = scipy_stats.chisquare(f_obs=observed_blue, f_exp=[expected_blue] * len(observed_blue))

        return {
            "lottery_name": data.lottery_name,
            "总期数": len(data.records),
            "红球检验": {
                "卡方统计量": round(chi2_red, 4),
                "p值": round(p_red, 6),
                "自由度": len(observed_red) - 1,
                "结论": "无显著证据拒绝均匀分布（开奖公平）" if p_red > alpha else "存在显著偏离（需关注）",
            },
            "蓝球检验": {
                "卡方统计量": round(chi2_blue, 4),
                "p值": round(p_blue, 6),
                "自由度": len(observed_blue) - 1,
                "结论": "无显著证据拒绝均匀分布（开奖公平）" if p_blue > alpha else "存在显著偏离（需关注）",
            },
            "说明": "卡方检验 H₀: 号码服从均匀分布（公平开奖）；H₁: 号码分布不均匀（可能异常）",
        }

    # 数字型：逐区卡方检验
    schema = get_schema(data.lottery_name)
    zone_tests = {}
    for z in schema.zones:
        observed = [0] * (z.max + 1)
        for r in data.records:
            for n in r.zone_numbers.get(z.name, []):
                # 数据若超出声明分区范围（如七星彩第7位出现10-14），动态扩容，避免越界
                if n < 0 or n > z.max:
                    while len(observed) <= n:
                        observed.append(0)
                observed[n] += 1
        total_draws = len(data.records) * z.choose
        expected = total_draws / len(observed)  # 按实际覆盖的范围均匀分配
        chi2, p = scipy_stats.chisquare(f_obs=observed, f_exp=[expected] * len(observed))
        zone_tests[z.name] = {
            "卡方统计量": round(chi2, 4),
            "p值": round(p, 6),
            "自由度": len(observed) - 1,
            "结论": "无显著证据拒绝均匀分布（开奖公平）" if p > alpha else "存在显著偏离（需关注）",
        }
    return {
        "lottery_name": data.lottery_name,
        "总期数": len(data.records),
        "各分区检验": zone_tests,
        "说明": "卡方检验 H₀: 每位数字服从均匀分布（公平开奖）；H₁: 分布不均匀（可能异常）",
    }


def game_theory_analysis(lottery_name: str) -> dict:
    """
    博弈论分析 — "剪刀石头布"模型
    彩票本质是玩家先手选号，庄家后手开奖的不对称博弈

    核心论点：
    1. 买家先手：选择固定号码
    2. 庄家后手：开奖前截止投注，掌握当期所有投注数据
    3. 信息不对称：庄家掌握投注分布，可以（理论上）规避热门号码
    4. 庄家优势：负期望值 + 后手信息优势

    这里分析：如果庄家存在"规避"行为，热门号码中奖概率是否异常偏低
    """
    return {
        "lottery_name": lottery_name,
        "博弈模型": "剪刀石头布 — 买家先手 vs 庄家后手",
        "买家劣势": [
            "先手出招：选定号码后锁定，不可更改",
            "信息劣势：不知道当期整体投注分布",
            "成本固定：每注 2 元",
        ],
        "庄家优势": [
            "后手出招：截止投注后才开奖，掌握全部投注数据",
            "负期望值：数学上已经保证庄家长期盈利",
            "返奖率控制：通过奖级结构控制总派发奖金在 50%-70%",
        ],
        "博弈结论": "即使不考虑庄家主观操纵，彩票的数学结构（负期望值+后手优势）"
                    "已确保庄家长期稳赢。这本质上是庄家拥有规则制定权的非对称博弈。",
        "检测方法": "可通过假设检验（hot-hand fallacy test）检测热门号码是否系统性规避",
    }


def run(data: LotteryData) -> dict:
    """
    Step5 主入口
    """
    name = data.lottery_name
    logger.info(f"Step5: 风控与最优投注 — {name}")

    kelly = kelly_criterion(name)
    sr = sharpe_ratio(name)
    fairness = hypothesis_test_fairness(data)
    game = game_theory_analysis(name)

    return {
        "step": 5,
        "lottery_name": name,
        "凯利准则": kelly,
        "夏普比率": sr,
        "假设检验(公平性)": fairness,
        "博弈论分析": game,
    }
