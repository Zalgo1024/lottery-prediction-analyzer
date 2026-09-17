"""
统计概览模块
各号码出现频率、冷热号排名、遗漏值分析
"""

import logging
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import LOTTERY_CONFIG
from data.loader import load_lottery
from data.schema import LotteryData, schema_from_cfg

logger = logging.getLogger(__name__)


def frequency_analysis(data: LotteryData) -> Dict[str, dict]:
    """
    各号码出现频率统计
    旧彩种返回 { "红球": {...}, "蓝球": {...} }（结构不变）；
    新彩种（阶段2）按 Schema.zones 返回 { "<区名>": {freq, total_draws, choose_per_draw}, ... }
    """
    cfg = LOTTERY_CONFIG[data.lottery_name]

    # 旧彩种：保持原红/蓝输出结构（零回归）
    if "red_range" in cfg:
        r_min, r_max = cfg["red_range"]
        b_min, b_max = cfg["blue_range"]
        red_freq = {i: 0 for i in range(r_min, r_max + 1)}
        blue_freq = {i: 0 for i in range(b_min, b_max + 1)}
        for record in data.records:
            for n in record.红球:
                if n in red_freq:
                    red_freq[n] += 1
            for n in record.蓝球:
                if n in blue_freq:
                    blue_freq[n] += 1
        total_records = len(data.records)
        return {
            "红球": {
                "freq": red_freq,
                "total_draws": total_records,
                "red_count_per_draw": cfg["red_count"],
            },
            "蓝球": {
                "freq": blue_freq,
                "total_draws": total_records,
                "blue_count_per_draw": cfg["blue_count"],
            },
        }

    # 新彩种：泛型分区频率
    schema = schema_from_cfg(cfg, data.lottery_name)
    result = {}
    for zone in schema.zones:
        freq = {i: 0 for i in range(zone.min, zone.max + 1)}
        for record in data.records:
            for n in record.zone_numbers.get(zone.name, []):
                if n in freq:
                    freq[n] += 1
        result[zone.name] = {
            "freq": freq,
            "total_draws": len(data.records),
            "choose_per_draw": zone.choose,
        }
    return result


def hot_cold_ranking(
    data: LotteryData, top_n: int = 10
) -> Dict[str, dict]:
    """
    冷热号排名（泛型：遍历 frequency_analysis 的所有分区键）
    """
    freqs = frequency_analysis(data)

    result = {}
    for ball_type in freqs.keys():
        freq_dict = freqs[ball_type]["freq"]
        sorted_nums = sorted(freq_dict.items(), key=lambda x: x[1], reverse=True)

        result[ball_type] = {
            "hot": sorted_nums[:top_n],
            "cold": sorted_nums[-top_n:][::-1] if top_n <= len(sorted_nums) else sorted_nums[::-1],
        }
    return result


def missing_value_analysis(data: LotteryData) -> Dict[str, Dict[int, int]]:
    """
    遗漏值分析 — 每个号码当前连续未出现的期数（从最新到最旧遍历）
    旧彩种返回 { "红球": {...}, "蓝球": {...} }；新彩种按区名返回。
    """
    cfg = LOTTERY_CONFIG[data.lottery_name]

    # 旧彩种：原红/蓝逻辑（零回归）
    if "red_range" in cfg:
        r_min, r_max = cfg["red_range"]
        b_min, b_max = cfg["blue_range"]
        total = len(data.records)
        red_missing = {i: total for i in range(r_min, r_max + 1)}
        blue_missing = {i: total for i in range(b_min, b_max + 1)}
        seen_red = set()
        seen_blue = set()
        for i, record in enumerate(data.records):
            for n in record.红球:
                if n not in seen_red:
                    red_missing[n] = i
                    seen_red.add(n)
            for n in record.蓝球:
                if n not in seen_blue:
                    blue_missing[n] = i
                    seen_blue.add(n)
            if len(seen_red) == len(red_missing) and len(seen_blue) == len(blue_missing):
                break
        return {"红球": red_missing, "蓝球": blue_missing}

    # 新彩种：泛型分区遗漏
    schema = schema_from_cfg(cfg, data.lottery_name)
    total = len(data.records)
    result = {}
    for zone in schema.zones:
        miss = {i: total for i in range(zone.min, zone.max + 1)}
        seen = set()
        for i, record in enumerate(data.records):
            for n in record.zone_numbers.get(zone.name, []):
                if n not in seen:
                    miss[n] = i
                    seen.add(n)
            if len(seen) == len(miss):
                break
        result[zone.name] = miss
    return result


def summary_stats(data: LotteryData) -> dict:
    """
    综合统计摘要（泛型：遍历所有分区键，兼容红/蓝与新彩种）
    """
    freqs = frequency_analysis(data)
    missing = missing_value_analysis(data)
    ranking = hot_cold_ranking(data, top_n=5)

    zones_out = {}
    for zname, fdict in freqs.items():
        choose = fdict.get("red_count_per_draw") or fdict.get("blue_count_per_draw") or fdict.get("choose_per_draw", 1)
        zones_out[zname] = {
            "total_numbers": len(fdict["freq"]),
            "理论频率": choose / len(fdict["freq"]),
            "实际频率均值": float(np.mean(list(fdict["freq"].values()))),
            "频率标准差": float(np.std(list(fdict["freq"].values()))),
            "hot_5": ranking[zname]["hot"][:5],
            "cold_5": ranking[zname]["cold"][:5],
            "最大遗漏": max(missing[zname].items(), key=lambda x: x[1]),
        }

    result = {
        "lottery_name": data.lottery_name,
        "total_records": data.total_records,
        "zones": zones_out,
    }
    return result
