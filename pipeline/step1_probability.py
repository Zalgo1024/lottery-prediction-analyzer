"""
Step 1: 基础概率计算
古典概率、组合数、各奖级概率、条件概率、贝叶斯更新、累计中奖概率、保本概率
"""

import logging
import math
from typing import Dict, List, Optional, Tuple

from config import LOTTERY_CONFIG
from data.schema import LotteryData, get_schema, is_redblue

logger = logging.getLogger(__name__)


def comb(n: int, r: int) -> int:
    """组合数 C(n, r)"""
    if r < 0 or r > n:
        return 0
    return math.comb(n, r)


def total_combinations(lottery_name: str) -> int:
    """计算总组合数"""
    cfg = LOTTERY_CONFIG[lottery_name]
    red_comb = comb(cfg["red_range"][1], cfg["red_count"])
    blue_comb = comb(cfg["blue_range"][1], cfg["blue_count"])
    return red_comb * blue_comb


def prize_probabilities_ssq() -> List[dict]:
    """
    双色球各奖级概率（2026 新规，含福运奖）
    返回列表，每项含 prize_name, conditions, probability, is_floating
    """
    # 总组合数 C(33,6) * C(16,1) = 17,721,088
    total = total_combinations("双色球")

    # 各奖级中奖组合数
    # 一等奖: 6红 + 1蓝 = C(6,6)*C(27,0) * C(1,1)*C(15,0) = 1
    c1 = comb(6, 6) * comb(27, 0) * comb(1, 1) * comb(15, 0)

    # 二等奖: 6红 + 0蓝 = C(6,6)*C(27,0) * C(1,0)*C(15,1) = 15
    c2 = comb(6, 6) * comb(27, 0) * comb(1, 0) * comb(15, 1)

    # 三等奖: 5红 + 1蓝 = C(6,5)*C(27,1) * C(1,1)*C(15,0) = 162
    c3 = comb(6, 5) * comb(27, 1) * comb(1, 1) * comb(15, 0)

    # 四等奖: 5红+0蓝 或 4红+1蓝
    c4a = comb(6, 5) * comb(27, 1) * comb(1, 0) * comb(15, 1)  # 5+0
    c4b = comb(6, 4) * comb(27, 2) * comb(1, 1) * comb(15, 0)  # 4+1
    c4 = c4a + c4b

    # 五等奖: 4红+0蓝 或 3红+1蓝
    c5a = comb(6, 4) * comb(27, 2) * comb(1, 0) * comb(15, 1)  # 4+0
    c5b = comb(6, 3) * comb(27, 3) * comb(1, 1) * comb(15, 0)  # 3+1
    c5 = c5a + c5b

    # 六等奖: 0红+1蓝 = C(6,0)*C(27,6) * C(1,1)*C(15,0)
    c6 = comb(6, 0) * comb(27, 6) * comb(1, 1) * comb(15, 0)

    # 福运奖（2026新增，奖池≥15亿时生效）: 3红+0蓝
    c_fuyun = comb(6, 3) * comb(27, 3) * comb(1, 0) * comb(15, 1)

    levels = [
        {"prize_name": "一等奖", "condition": "6红+1蓝", "win_combs": c1, "is_floating": True},
        {"prize_name": "二等奖", "condition": "6红+0蓝", "win_combs": c2, "is_floating": True},
        {"prize_name": "三等奖", "condition": "5红+1蓝", "win_combs": c3, "fixed_amount": 3000},
        {"prize_name": "四等奖", "condition": "5红+0蓝 / 4红+1蓝", "win_combs": c4, "fixed_amount": 200},
        {"prize_name": "五等奖", "condition": "4红+0蓝 / 3红+1蓝", "win_combs": c5, "fixed_amount": 10},
        {"prize_name": "六等奖", "condition": "0红+1蓝", "win_combs": c6, "fixed_amount": 5},
    ]

    # 计算概率
    total_win = 0
    for level in levels:
        p = level["win_combs"] / total
        level["probability"] = p
        level["概率倒数"] = f"1/{total // level['win_combs']}" if level['win_combs'] > 0 else "0"
        total_win += p

    # 福运奖（条件性）
    p_fuyun = c_fuyun / total
    levels.append({
        "prize_name": "福运奖(条件)",
        "condition": "3红+0蓝（奖池≥15亿）",
        "win_combs": c_fuyun,
        "probability": p_fuyun,
        "概率倒数": f"1/{total // c_fuyun}",
        "fixed_amount": 5,
    })
    total_win += p_fuyun

    return {
        "levels": levels,
        "total_combinations": total,
        "total_win_probability": total_win,
    }


def prize_probabilities_dlt() -> List[dict]:
    """
    大乐透各奖级概率（2023 新规：9 奖级，条件与 ev/rollover.py::_dlt_fixed 一致，
    已对照体彩官网《"超级大乐透"游戏中奖介绍》核实）。
    旧 7 级表（三等=5+0/4+2 等）为 2023 前规则，已废弃。
    """
    total = total_combinations("大乐透")

    # 前区C(35,5)=324632, 后区C(12,2)=66, 总=21,425,712
    # 一等奖: 5+2
    c1 = comb(5, 5) * comb(30, 0) * comb(2, 2) * comb(10, 0)  # 1
    # 二等奖: 5+1
    c2 = comb(5, 5) * comb(30, 0) * comb(2, 1) * comb(10, 1)  # 20
    # 三等奖: 5+0 → 10000
    c3 = comb(5, 5) * comb(30, 0) * comb(2, 0) * comb(10, 2)  # 45
    # 四等奖: 4+2 → 3000
    c4 = comb(5, 4) * comb(30, 1) * comb(2, 2) * comb(10, 0)  # 150
    # 五等奖: 4+1 → 300
    c5 = comb(5, 4) * comb(30, 1) * comb(2, 1) * comb(10, 1)  # 3000
    # 六等奖: 3+2 → 200
    c6 = comb(5, 3) * comb(30, 2) * comb(2, 2) * comb(10, 0)  # 4350
    # 七等奖: 4+0 → 100
    c7 = comb(5, 4) * comb(30, 1) * comb(2, 0) * comb(10, 2)  # 6750
    # 八等奖: 3+1 或 2+2 → 15
    c8a = comb(5, 3) * comb(30, 2) * comb(2, 1) * comb(10, 1)  # 87000
    c8b = comb(5, 2) * comb(30, 3) * comb(2, 2) * comb(10, 0)  # 20300
    c8 = c8a + c8b
    # 九等奖: 3+0 或 2+1 或 1+2 或 0+2 → 5
    c9a = comb(5, 3) * comb(30, 2) * comb(2, 0) * comb(10, 2)  # 195750
    c9b = comb(5, 2) * comb(30, 3) * comb(2, 1) * comb(10, 1)  # 913500
    c9c = comb(5, 1) * comb(30, 4) * comb(2, 2) * comb(10, 0)  # 137025
    c9d = comb(5, 0) * comb(30, 5) * comb(2, 2) * comb(10, 0)  # 142506
    c9 = c9a + c9b + c9c + c9d

    levels = [
        {"prize_name": "一等奖", "condition": "5+2", "win_combs": c1, "is_floating": True},
        {"prize_name": "二等奖", "condition": "5+1", "win_combs": c2, "is_floating": True},
        {"prize_name": "三等奖", "condition": "5+0", "win_combs": c3, "fixed_amount": 10000},
        {"prize_name": "四等奖", "condition": "4+2", "win_combs": c4, "fixed_amount": 3000},
        {"prize_name": "五等奖", "condition": "4+1", "win_combs": c5, "fixed_amount": 300},
        {"prize_name": "六等奖", "condition": "3+2", "win_combs": c6, "fixed_amount": 200},
        {"prize_name": "七等奖", "condition": "4+0", "win_combs": c7, "fixed_amount": 100},
        {"prize_name": "八等奖", "condition": "3+1 / 2+2", "win_combs": c8, "fixed_amount": 15},
        {"prize_name": "九等奖", "condition": "3+0 / 2+1 / 1+2 / 0+2", "win_combs": c9, "fixed_amount": 5},
    ]

    total_win = 0
    for level in levels:
        p = level["win_combs"] / total
        level["probability"] = p
        level["概率倒数"] = f"1/{total // level['win_combs']}" if level['win_combs'] > 0 else "0"
        total_win += p

    return {
        "levels": levels,
        "total_combinations": total,
        "total_win_probability": total_win,
    }


def prize_probabilities_generic(lottery_name: str) -> dict:
    """
    数字型/乐透型彩种的通用中奖概率（仅建模「精确全中」最高档）
    总组合数 = 各分区组合数之积：
        - 可重复（数字型按位）：每区容量 ** choose
        - 不可重复（乐透型）：C(容量, choose)
    精确全中概率 = 1 / 总组合数（数字型逐位 0-9，如 排列5 = 10^5 = 100000）。
    """
    schema = get_schema(lottery_name)
    cfg = LOTTERY_CONFIG[lottery_name]

    zone_combs = []
    for z in schema.zones:
        n = z.size  # max - min + 1
        if z.repeatable:
            zone_combs.append(n ** z.choose)
        else:
            zone_combs.append(comb(n, z.choose))
    total = 1
    for c in zone_combs:
        total *= c

    payout = cfg.get("payout", {}) or {}
    exact = payout.get("exact_match", 0)
    head_p = 1.0 / total if total else 0.0

    levels = [{
        "prize_name": "精确全中",
        "condition": f"全部{len(schema.zones)}位精确匹配",
        "win_combs": 1,
        "fixed_amount": exact,
        "probability": head_p,
        "概率倒数": f"1/{total}",
    }]
    return {
        "levels": levels,
        "total_combinations": total,
        "total_win_probability": head_p,
    }


def get_prize_data(lottery_name: str) -> dict:
    """
    统一入口：返回该彩种的中奖概率数据。
    双色球/大乐透走原逻辑（零回归）；数字型走通用分区计算。
    """
    if is_redblue(lottery_name):
        if lottery_name == "双色球":
            return prize_probabilities_ssq()
        return prize_probabilities_dlt()
    return prize_probabilities_generic(lottery_name)


def conditional_probability(
    lottery_name: str, known_reds: int, known_blues: int,
    matched_reds: int, matched_blues: int,
) -> float:
    """
    条件概率：已知已匹配了 known_reds 个红球和 known_blues 个蓝球，
    求最终匹配 matched_reds 个红球和 matched_blues 个蓝球的概率

    适用场景：已知部分号码命中，计算剩余号码中奖概率
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    remain_red = cfg["red_count"] - known_reds
    remain_blue = cfg["blue_count"] - known_blues
    need_red = matched_reds - known_reds
    need_blue = matched_blues - known_blues

    if need_red < 0 or need_blue < 0 or need_red > remain_red or need_blue > remain_blue:
        return 0.0

    # 剩余号码空间中，需要选中的组合数
    total_red_pool = cfg["red_range"][1] - known_reds
    total_blue_pool = cfg["blue_range"][1] - known_blues

    # 从剩余红球池中选 need_red 个，从剩余蓝球池中选 need_blue 个
    red_ways = comb(remain_red, need_red) * comb(total_red_pool - remain_red, remain_red - need_red)
    blue_ways = comb(remain_blue, need_blue) * comb(total_blue_pool - remain_blue, remain_blue - need_blue)

    total_ways = comb(total_red_pool, remain_red) * comb(total_blue_pool, remain_blue)

    if total_ways == 0:
        return 0.0
    return (red_ways * blue_ways) / total_ways


def bayesian_update(
    prior_prob: float,
    likelihood: float,
    evidence_prob: float,
) -> float:
    """
    贝叶斯更新：P(A|B) = P(B|A) * P(A) / P(B)

    参数：
    - prior_prob: 先验概率 P(A)
    - likelihood: 似然度 P(B|A) — 在 A 成立下观察到 B 的概率
    - evidence_prob: 证据概率 P(B) — 观察到 B 的总概率

    返回后验概率 P(A|B)
    """
    if evidence_prob == 0:
        return 0.0
    return (likelihood * prior_prob) / evidence_prob


def cumulative_win_probability(
    lottery_name: str, n_tickets: int, prize_level: Optional[str] = None
) -> float:
    """
    累计中奖概率：购买 n 张不同号码至少中一次奖的概率
    如果指定 prize_level，则计算至少中该奖级及以上的概率
    公式：P(至少中一次) = 1 - (1 - p)^n
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

    return 1 - (1 - p) ** n_tickets


def break_even_probability(
    lottery_name: str, ticket_price: int = 2
) -> dict:
    """
    保本概率 & 盈亏平衡点
    计算期望奖金 >= 投注成本的最小中奖条件
    """
    data = get_prize_data(lottery_name)
    payout = LOTTERY_CONFIG[lottery_name].get("payout", {}) or {}
    ticket_price = payout.get("cost", ticket_price)

    # 按固定奖计算保本
    cumulative_prob = 0.0
    cumulative_bonus = 0.0
    break_level = None

    for level in data["levels"]:
        if level.get("is_floating"):
            continue
        amount = level.get("fixed_amount", 0)
        cumulative_prob += level["probability"]
        cumulative_bonus += amount * level["probability"]
        if amount >= ticket_price and break_level is None:
            break_level = level["prize_name"]

    return {
        "ticket_price": ticket_price,
        "expected_bonus_per_ticket": cumulative_bonus,
        "break_even_prize": break_level,
        "中奖概率(含所有奖级)": data["total_win_probability"],
    }


def run(data: LotteryData) -> dict:
    """
    Step1 主入口：执行所有基础概率计算
    """
    name = data.lottery_name
    logger.info(f"Step1: 基础概率计算 — {name}")

    prize_data = get_prize_data(name)

    # 保本分析
    be = break_even_probability(name)

    # 条件概率示例：双色球/大乐透才有「已知部分命中求更高奖级」的玩法；
    # 数字型仅精确全中一档，无此分级条件概率。
    if is_redblue(name):
        cp = conditional_probability(name, known_reds=3, known_blues=1,
                                     matched_reds=6, matched_blues=1)
        cp_example = {"已知3红+1蓝中一等奖概率": cp}
    else:
        cp_example = {"说明": "数字型仅「精确全中」一档，无分级条件概率示例"}

    # 贝叶斯更新示例：历史热号调整
    # 假设某个号码历史出现频率 p=0.04，某期开奖该号码区域信号出现概率 0.8，
    # 信号误报率 0.3，贝叶斯更新后
    bayes = bayesian_update(
        prior_prob=0.04,
        likelihood=0.8,
        evidence_prob=0.8 * 0.04 + 0.3 * (1 - 0.04),
    )

    # 累计概率：买 10 张、100 张
    cum10 = cumulative_win_probability(name, n_tickets=10)
    cum100 = cumulative_win_probability(name, n_tickets=100)

    result = {
        "step": 1,
        "lottery_name": name,
        "total_combinations": prize_data["total_combinations"],
        "head_prize_probability": prize_data["levels"][0]["probability"],
        "total_win_probability": prize_data["total_win_probability"],
        "prize_levels": prize_data["levels"],
        "conditional_probability_example": cp_example,
        "bayesian_update_example": {
            "prior": 0.04,
            "posterior": round(bayes, 6),
        },
        "cumulative_probability": {
            "买10期至少中一次": round(cum10, 6),
            "买100期至少中一次": round(cum100, 6),
        },
        "break_even": be,
    }
    return result
