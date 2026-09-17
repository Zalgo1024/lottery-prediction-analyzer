"""
可信度层 · 反大众投注模型

思路（坦诚定位）：不提高中奖概率，而是降低"中奖后被多人平分"的风险。
若某些「人类偏好号码组合」（连号、同半区、等差序、生日号集中）一旦开出，
会吸引更多彩民选中同一注，头奖注数(一等奖注数)随之升高 → 你若也选它，中奖会被分摊。

数据可行性：
  - 双色球/大乐透：CSV 自带 一等奖注数 + 总投注额 → 可检验「热门组合是否更易多人平分」。
    为消除销量趋势混杂，用 头奖注数 / (总投注额/1e8) 归一化后做 Mann-Whitney U 检验。
  - 数字型：CSV 仅有期号/日期/号码，无每注投注分布 → 反大众模型不适用，明确标注。
"""

from typing import Dict, List, Tuple

import numpy as np
from scipy import stats

from data.loader import load_lottery
from data.schema import get_schema, is_redblue


def _pattern_flags(reds: List[int], size: int) -> List[str]:
    """人类偏好组合模式的启发式标记（仅作"热门程度"代理）。"""
    s = sorted(reds)
    flags: List[str] = []
    # 连续 ≥3
    run = 1
    for i in range(1, len(s)):
        run = run + 1 if s[i] == s[i - 1] + 1 else 1
        if run >= 3:
            flags.append("连续≥3")
            break
    # 同半区（全低 / 全高）
    low = size // 2
    if all(x <= low for x in s):
        flags.append("全低半区")
    if all(x >= low + 2 for x in s):
        flags.append("全高半区")
    # 等差序（存在 3 项等差数列）
    ss = set(s)
    for i in range(len(s)):
        for j in range(i + 1, len(s)):
            d = s[j] - s[i]
            if d > 0 and (s[j] + d) in ss:
                flags.append("等差序")
                break
        if "等差序" in flags:
            break
    # 生日号集中（≥4 个 ≤12）
    if sum(1 for x in s if x <= 12) >= 4:
        flags.append("生日号集中")
    return flags


def _is_popular(reds: List[int], size: int) -> bool:
    return len(_pattern_flags(reds, size)) > 0


def anti_crowd_report(lottery_name: str) -> Dict:
    if not is_redblue(lottery_name):
        return {
            "lottery_name": lottery_name,
            "applicable": False,
            "note": "数字型 CSV 仅有期号/日期/号码，无每注投注分布（限号/通吃机制另需官方数据），"
                    "反大众投注模型不适用。",
        }

    data = load_lottery(lottery_name)
    schema = get_schema(lottery_name)
    red_zone = schema.red_zone
    size = red_zone.size if red_zone else 33

    pop_norm: List[float] = []
    non_norm: List[float] = []

    for rec in data.records:
        j = rec.一等奖注数
        sales = rec.总投注额
        reds = rec.红球
        if j is None or sales is None or sales <= 0 or not reds:
            continue
        norm = j / (sales / 1e8)  # 每亿元销量对应的头奖注数
        if _is_popular(reds, size):
            pop_norm.append(norm)
        else:
            non_norm.append(norm)

    n_total = len(pop_norm) + len(non_norm)
    if len(pop_norm) < 5 or len(non_norm) < 5:
        return {
            "lottery_name": lottery_name, "applicable": True,
            "n_with_data": n_total, "n_popular": len(pop_norm),
            "note": "有效样本不足，无法稳健检验。",
        }

    pop_a = np.array(pop_norm)
    non_a = np.array(non_norm)
    try:
        u = stats.mannwhitneyu(pop_a, non_a, alternative="greater")
        mw_p = float(u.pvalue)
        mw_stat = float(u.statistic)
    except Exception:
        mw_p, mw_stat = 1.0, float("nan")

    pop_med = float(np.median(pop_a))
    non_med = float(np.median(non_a))
    pop_mean = float(pop_a.mean())
    non_mean = float(non_a.mean())

    if mw_p < 0.05 and pop_med > non_med:
        interp = ("检测到显著关联：热门偏好组合一旦开出，头奖注数（按销量归一化）显著高于其他组合。"
                  "回避这类组合可降低中奖被多人平分的风险——这是合法且可量化的边缘优势，"
                  "但它不改变中奖概率本身。")
    else:
        interp = ("未检测到「热门组合→更多头奖平分者」的显著关联。头奖注数主要由销量与随机性驱动，"
                  "反大众策略在本数据集上未显现可量化收益。")

    return {
        "lottery_name": lottery_name,
        "applicable": True,
        "n_with_data": n_total,
        "n_popular": len(pop_norm),
        "popular_median_norm": round(pop_med, 4),
        "nonpopular_median_norm": round(non_med, 4),
        "popular_mean_norm": round(pop_mean, 4),
        "nonpopular_mean_norm": round(non_mean, 4),
        "mw_stat": round(mw_stat, 1) if mw_stat == mw_stat else None,
        "mw_p": round(mw_p, 4),
        "interpretation": interp,
    }
