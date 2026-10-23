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


def prize_probabilities_ssq(issue: Optional[str] = None) -> dict:
    """
    双色球各奖级概率 —— **由 ev/profit/rules.rules_for 生成**（与大乐透同一套做法）。

    ★ 旧实现手写 comb()，已与官方规则表漂移（2026-10-23 修）：
        · 六等奖只算「0红+1蓝」，**漏了 1红+1蓝 / 2红+1蓝** ⇒ 六等奖组合数少算约 2/3；
        · 凭空多出一个「福运奖（3红+0蓝，奖池≥15亿）」，而双色球规则表**没有该奖级**
          ⇒ 3红+0蓝 是**未中**，把它算成中奖会虚增总中奖概率约 1.2 个百分点。
      现在从单一真源 `rules_for("双色球").grade_table` 反查，六等 = 0/1/2 红 + 1 蓝，
      与判级函数 `_classify_prize` 同源（不再可能各自漂移）。

    返回 dict：{levels: [...], total_combinations, total_win_probability}
    """
    from ev.profit.rules import _GRADE_ORDER, rules_for

    rules = rules_for("双色球", issue)
    total = rules.outcome_size          # C(33,6) × C(16,1) = 17,721,088
    ow = rules.outer_width
    lo, hi = rules.outer_range
    n = hi - lo + 1

    # 逐 (红球命中 h, 蓝球命中 j) 格精确计数 → 按奖级聚合
    counts: Dict[str, int] = {}
    conds: Dict[str, List[str]] = {}
    for (h, j), g in rules.grade_table.items():
        cells = comb(ow, h) * comb(n - ow, ow - h) * rules.inner_multiplicity[j]
        counts[g] = counts.get(g, 0) + cells
        conds.setdefault(g, []).append(f"{h}+{j}")

    # ★ _build_grades 先固定后浮动，顺序是 三/四/五/六/一/二等 ⇒ 必须按级数重排
    levels: List[dict] = []
    for g in sorted(rules.grades, key=_GRADE_ORDER.index):
        r = rules.grades[g]
        lv: dict = {"prize_name": g,
                    "condition": " / ".join(sorted(conds[g], key=lambda s: -int(s[0]))),
                    "win_combs": counts[g]}
        if r.kind == "floating":
            lv["is_floating"] = True
        else:
            lv["fixed_amount"] = r.fixed
        levels.append(lv)

    total_win = 0.0
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


def prize_probabilities_dlt(issue: Optional[str] = None) -> List[dict]:
    """
    大乐透各奖级概率 —— **由 ev/profit/rules.rules_for 按期生成**。

    大乐透历史上奖级数变更多次（8/6/9/7 级五段，见 data/prize_versions.py），
    旧实现手写 2019 九级表，对 2026-02 之后的期全错。现在按期号取规则版本：
    issue=None ⇒ 现行版本（2026-02 起七级）。
    返回列表，每项含 prize_name, conditions, probability, is_floating
    """
    from ev.profit.rules import rules_for

    rules = rules_for("大乐透", issue)
    total = rules.outcome_size          # C(35,5)×C(12,2) = 21,425,712
    ow = rules.outer_width
    lo, hi = rules.outer_range
    n = hi - lo + 1

    # 逐 (前区命中 h, 后区命中 j) 格精确计数 → 按奖级聚合
    counts: Dict[str, int] = {}
    conds: Dict[str, List[str]] = {}
    for (h, j), g in rules.grade_table.items():
        cells = comb(ow, h) * comb(n - ow, ow - h) * rules.inner_multiplicity[j]
        counts[g] = counts.get(g, 0) + cells
        conds.setdefault(g, []).append(f"{h}+{j}")

    levels: List[dict] = []
    for g, r in rules.grades.items():          # grades 已按奖级升序
        lv: dict = {"prize_name": g,
                    "condition": " / ".join(sorted(conds[g], key=lambda s: -int(s[0]))),
                    "win_combs": counts[g]}
        if r.kind == "floating":
            lv["is_floating"] = True
        else:
            lv["fixed_amount"] = r.fixed
        levels.append(lv)

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


def get_prize_data(lottery_name: str, issue: Optional[str] = None) -> dict:
    """
    统一入口：返回该彩种的中奖概率数据。
    双色球/大乐透走原逻辑（零回归）；数字型走通用分区计算。
    `issue` 仅对大乐透有效：按期号取规则版本（8/6/9/7 级五段）。
    """
    if is_redblue(lottery_name):
        if lottery_name == "双色球":
            return prize_probabilities_ssq(issue)
        return prize_probabilities_dlt(issue)
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
