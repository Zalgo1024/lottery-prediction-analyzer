"""
训练评估指标：选号命中率 vs 随机基线 + 显著性检验

核心问题：
- 旧版 `best_hit_rate` 是「号码级准确率」（把预测为0且真实也为0的号码算对），
  因为彩票号码天然绝大多数不开，导致基线已高达 ~85%，营造虚假有效感。
- 真正该看的是「选号命中率」：按规则买一组号码，平均能中几个号，
  并与「闭着眼睛随机选一组」的理论期望做对比。
"""

import math
from typing import Dict, List, Tuple

import numpy as np

from config import LOTTERY_CONFIG
from data.schema import schema_from_cfg, record_zone_numbers, is_redblue


def _theoretical_zone_hits(choose: int, total: int) -> float:
    """从 total 个号码中选 choose 个，单期期望命中数（超几何分布期望）。"""
    if total <= 0:
        return 0.0
    # 每个真实开奖号码被预测覆盖的概率 = choose / total
    # 期望命中 = 开奖该区的号码数 * choose / total，但开奖该区号码数=choose
    return choose * choose / total


def compute_random_baseline(lottery_name: str) -> Dict[str, float]:
    """
    计算「随机选号」的理论期望命中数（闭式解，无需 Monte Carlo）。

    返回：
      {
        "expected_total_hits": 总期望命中,
        "expected_red_hits": 红球期望（仅红蓝型）,
        "expected_blue_hits": 蓝球期望（仅红蓝型）,
        "expected_zone_hits": {区名: 期望命中},
        "notes": 说明字符串
      }
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)

    zone_expected = {}
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        total = hi - lo + 1
        zone_expected[zone.name] = _theoretical_zone_hits(zone.choose, total)

    if is_redblue(lottery_name):
        red_zone = schema.red_zone
        blue_zone = schema.blue_zone
        r_lo, r_hi = red_zone.range_tuple()
        b_lo, b_hi = blue_zone.range_tuple()
        red_exp = _theoretical_zone_hits(red_zone.choose, r_hi - r_lo + 1)
        blue_exp = _theoretical_zone_hits(blue_zone.choose, b_hi - b_lo + 1)
        return {
            "expected_total_hits": red_exp + blue_exp,
            "expected_red_hits": red_exp,
            "expected_blue_hits": blue_exp,
            "expected_zone_hits": zone_expected,
            "notes": "红/蓝独立超几何期望",
        }
    else:
        total_exp = sum(zone_expected.values())
        return {
            "expected_total_hits": total_exp,
            "expected_red_hits": None,
            "expected_blue_hits": None,
            "expected_zone_hits": zone_expected,
            "notes": "数字型逐区独立期望求和",
        }


def compute_selection_hits(
    lottery_name: str,
    pred_by_zone: Dict[str, List[int]],
    actual_record,
) -> Dict[str, float]:
    """
    计算一组预测号码相对真实开奖的命中数。

    pred_by_zone: {区名: [号码列表]}，长度需符合该区 choose 数。
    actual_record: data.schema.DrawRecord 或类似对象，需支持 rec.红球/蓝球
                   或 record_zone_numbers(rec, zone)。
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)

    red_hits = blue_hits = 0
    total_hits = 0
    zone_hits = {}

    if is_redblue(lottery_name):
        red_zone = schema.red_zone
        blue_zone = schema.blue_zone
        pred_red = set(pred_by_zone.get(red_zone.name, []))
        pred_blue = set(pred_by_zone.get(blue_zone.name, []))
        actual_red = set(actual_record.红球)
        actual_blue = set(actual_record.蓝球)
        red_hits = len(pred_red & actual_red)
        blue_hits = len(pred_blue & actual_blue)
        total_hits = red_hits + blue_hits
        zone_hits[red_zone.name] = float(red_hits)
        zone_hits[blue_zone.name] = float(blue_hits)
    else:
        for zone in schema.zones:
            pred_nums = set(pred_by_zone.get(zone.name, []))
            actual_nums = set(record_zone_numbers(actual_record, zone))
            h = len(pred_nums & actual_nums)
            zone_hits[zone.name] = float(h)
            total_hits += h

    return {
        "total_hits": float(total_hits),
        "red_hits": float(red_hits) if is_redblue(lottery_name) else None,
        "blue_hits": float(blue_hits) if is_redblue(lottery_name) else None,
        "zone_hits": zone_hits,
    }


def _selection_hits_from_prob_matrix(
    lottery_name: str,
    prob_matrix: np.ndarray,
    y_true: np.ndarray,
) -> Tuple[float, float, float]:
    """
    对 ML 输出的概率矩阵，按「每区取概率 top-k」生成选号，再算平均命中数。

    prob_matrix: (n_samples, n_numbers)，每行是每个号码出现的预测概率。
    y_true:      (n_samples, n_numbers) 二值标签。

    返回：(mean_total_hits, mean_red_hits, mean_blue_hits)
           数字型后两者为 None
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)
    n_samples = prob_matrix.shape[0]

    total_hits_list = []
    red_hits_list = []
    blue_hits_list = []

    offset = 0
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        size = hi - lo + 1
        z_probs = prob_matrix[:, offset:offset + size]
        z_true = y_true[:, offset:offset + size]
        # 每行取 top choose 个索引
        choose = zone.choose
        # argsort 升序，取最后 choose 个
        top_idx = np.argpartition(z_probs, -choose, axis=1)[:, -choose:]
        # 构造 one-hot 预测
        pred_onehot = np.zeros_like(z_probs)
        for i in range(n_samples):
            pred_onehot[i, top_idx[i]] = 1
        hits = (pred_onehot * z_true).sum(axis=1)
        total_hits_list.append(hits)
        if is_redblue(lottery_name):
            if zone.name == schema.red_zone.name:
                red_hits_list.append(hits)
            elif zone.name == schema.blue_zone.name:
                blue_hits_list.append(hits)
        offset += size

    total_hits = np.sum(total_hits_list, axis=0)
    mean_total = float(np.mean(total_hits))
    mean_red = float(np.mean(np.sum(red_hits_list, axis=0))) if red_hits_list else None
    mean_blue = float(np.mean(np.sum(blue_hits_list, axis=0))) if blue_hits_list else None
    return mean_total, mean_red, mean_blue


def _hypergeometric_variance(choose: int, total: int) -> float:
    """超几何分布方差：从 total 中选 choose 个，命中数的方差。"""
    if total < 2:
        return 0.0
    p = choose / total
    return choose * p * (1 - p) * (total - choose) / (total - 1)


def _random_selection_variance(lottery_name: str) -> float:
    """随机选号下，单期总命中数的理论方差（逐区独立求和）。"""
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)
    total_var = 0.0
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        total_var += _hypergeometric_variance(zone.choose, hi - lo + 1)
    return total_var


def evaluate_selection_vs_random(
    lottery_name: str,
    actual_total_hits_observed: float,
    n_periods: int,
) -> Dict[str, float]:
    """
    把观测到的平均选号命中数，与随机基线做显著性检验。

    参数：
      actual_total_hits_observed: 观测到的平均每期总命中数
      n_periods: 评估期数（样本量）

    返回：
      {
        "expected_total_hits": 随机基线期望,
        "observed_total_hits": 观测平均,
        "total_advantage": 观测 - 期望,
        "n_periods": 评估期数,
        "std_error": 随机基线标准误,
        "z_score": z 统计量,
        "p_value": 单尾 p 值,
        "significant_05": 是否在 0.05 显著,
        "significance_note": 显著性结论文字
      }
    """
    baseline = compute_random_baseline(lottery_name)
    expected = baseline["expected_total_hits"]
    advantage = actual_total_hits_observed - expected

    var_random = _random_selection_variance(lottery_name)
    std_error = math.sqrt(var_random / n_periods) if n_periods > 0 and var_random > 0 else 0.0
    z = advantage / std_error if std_error > 0 else 0.0
    # 单尾 p
    p_value = 0.5 * (1 + math.erf(-z / math.sqrt(2)))

    sig = p_value < 0.05
    note = (
        "模型显著优于随机" if sig and advantage > 0
        else "模型显著差于随机" if sig and advantage < 0
        else "与随机无显著差异"
    )

    return {
        "expected_total_hits": expected,
        "observed_total_hits": actual_total_hits_observed,
        "total_advantage": advantage,
        "n_periods": n_periods,
        "std_error": std_error,
        "z_score": z,
        "p_value": p_value,
        "significant_05": sig,
        "significance_note": note,
    }
