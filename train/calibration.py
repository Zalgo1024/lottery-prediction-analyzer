"""
校准评估模块（第三档升级 - 统一评分尺）

解决旧回测"只看中几个球"的盲区：模型输出的每号概率到底准不准？
提供：
- calibration_error：把模型给的概率与历史实际出现频率按概率分桶对比偏差（越低越好）
- calibration_curve：校准曲线数据点（预测概率 vs 实际频率），供报告画图
- combined_score：命中率 + 校准度 的加权综合分（越高越好），作为搜索/筛选的统一尺
- evaluate_prob_matrix：直接吃"实际 0/1 矩阵 + 预测概率矩阵"，输出全套指标

全部纯函数、确定性，便于在滚动回测里反复调用。
"""

from typing import Dict, List, Optional

import numpy as np


def calibration_error(
    probs: np.ndarray,
    y_true: np.ndarray,
    n_bins: int = 10,
    min_bin: int = 5,
) -> float:
    """
    概率校准误差（分桶加权）。
    probs: 一维数组，预测概率（0~1）
    y_true: 一维数组，实际 0/1
    返回：加权平均 |预测概率均值 - 实际频率|，范围 0~1，越低表示概率越准。
    """
    probs = np.asarray(probs, dtype=float).ravel()
    y_true = np.asarray(y_true, dtype=float).ravel()
    if probs.size == 0 or y_true.size == 0:
        return 1.0
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    total = 0.0
    weighted_err = 0.0
    for b in range(n_bins):
        lo, hi = edges[b], edges[b + 1]
        mask = (probs >= lo) & (probs < hi) if b < n_bins - 1 else (probs >= lo) & (probs <= hi)
        if mask.sum() < min_bin:
            continue
        bin_probs = probs[mask]
        bin_true = y_true[mask]
        mean_pred = float(bin_probs.mean())
        actual_freq = float(bin_true.mean())
        err = abs(mean_pred - actual_freq)
        weighted_err += err * mask.sum()
        total += mask.sum()
    if total == 0:
        return 1.0
    return weighted_err / total


def calibration_curve(
    probs: np.ndarray,
    y_true: np.ndarray,
    n_bins: int = 10,
) -> List[Dict]:
    """
    校准曲线数据点：每个概率桶的 (预测概率均值, 实际频率, 样本数)。
    横轴=预测概率，纵轴=实际频率，越贴近对角线越好。
    """
    probs = np.asarray(probs, dtype=float).ravel()
    y_true = np.asarray(y_true, dtype=float).ravel()
    edges = np.linspace(0.0, 1.0, n_bins + 1)
    points = []
    for b in range(n_bins):
        lo, hi = edges[b], edges[b + 1]
        mask = (probs >= lo) & (probs < hi) if b < n_bins - 1 else (probs >= lo) & (probs <= hi)
        if mask.sum() == 0:
            continue
        bin_probs = probs[mask]
        bin_true = y_true[mask]
        points.append({
            "bin": b,
            "range": [round(float(lo), 2), round(float(hi), 2)],
            "mean_pred": round(float(bin_probs.mean()), 4),
            "actual_freq": round(float(bin_true.mean()), 4),
            "count": int(mask.sum()),
        })
    return points


def combined_score(
    hit_rate: float,
    calib_err: float,
    calib_weight: float = 0.3,
) -> float:
    """
    统一综合分：命中率占 (1-w)，校准度占 w。
    hit_rate: 每号码预测命中率（0~1）
    calib_err: 校准误差（0~1，越低越好）
    返回：0~1，越高越好。
    """
    hit_rate = float(np.clip(hit_rate, 0.0, 1.0))
    calib_err = float(np.clip(calib_err, 0.0, 1.0))
    calib_quality = 1.0 - calib_err
    return hit_rate * (1.0 - calib_weight) + calib_quality * calib_weight


def evaluate_prob_matrix(
    y_true_matrix: np.ndarray,
    prob_matrix: np.ndarray,
    calib_weight: float = 0.3,
    n_bins: int = 10,
) -> Dict:
    """
    直接吃矩阵评估：
    y_true_matrix: (n_samples, n_numbers) 实际 0/1
    prob_matrix:   (n_samples, n_numbers) 预测概率
    返回全套指标：命中率、校准误差、综合分、校准曲线。
    """
    y_true_matrix = np.asarray(y_true_matrix, dtype=float)
    prob_matrix = np.asarray(prob_matrix, dtype=float)
    if y_true_matrix.size == 0:
        return {
            "hit_rate": 0.0,
            "calibration_error": 1.0,
            "combined_score": 0.0,
            "calibration_curve": [],
            "n_samples": 0,
        }
    hit_rate = float(np.mean(y_true_matrix == (prob_matrix >= 0.5)))
    calib_err = calibration_error(prob_matrix.ravel(), y_true_matrix.ravel(), n_bins=n_bins)
    score = combined_score(hit_rate, calib_err, calib_weight)
    curve = calibration_curve(prob_matrix.ravel(), y_true_matrix.ravel(), n_bins=n_bins)
    return {
        "hit_rate": round(hit_rate, 4),
        "calibration_error": round(float(calib_err), 4),
        "combined_score": round(float(score), 4),
        "calibration_curve": curve,
        "n_samples": int(y_true_matrix.shape[0]),
    }
