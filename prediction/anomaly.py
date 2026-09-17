"""
异常检测模块（迭代版）

四种检测方法：
1. 滑动窗口卡方检验 — 窗口内号码分布 vs 均匀分布
2. Benford 定律检验 — 号码首位数分布是否合规
3. 号码区间偏离检测 — 各区间号码频次 vs 理论值
4. 冷热号突变检测 — 近期频率 vs 历史均值的 z-score

输出：
- CSV 批量导出（含各维度分数）
- 分布对比直方图
- 年度异常趋势
- --threshold all 一键三档对比
"""

import csv
import json
import logging
import math
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from scipy import stats as scipy_stats

from config import ANOMALY_THRESHOLDS, ANOMALY_DEFAULT, LOTTERY_CONFIG, TRAINING_DIR
from data.loader import load_lottery
from data.schema import get_schema, is_redblue, schema_from_cfg, record_zone_numbers

logger = logging.getLogger(__name__)

# matplotlib
try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    # 配置中文字体：SimHei（黑体）
    _CN_FONT = "SimHei"
    try:
        fm = matplotlib.font_manager
        if not any(f.name == _CN_FONT for f in fm.fontManager.ttflist):
            raise RuntimeError(f"Font {_CN_FONT} not found")
        plt.rcParams["font.sans-serif"] = [_CN_FONT, "Microsoft YaHei", "sans-serif"]
        plt.rcParams["axes.unicode_minus"] = False
    except Exception as e:
        logger.warning(f"中文字体 {_CN_FONT} 配置失败: {e}")
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False
    logger.warning("matplotlib unavailable, skipping charts")


# ============================================================
# 检测方法 1: 滑动窗口卡方检验（红球分布均匀性）
# ============================================================

def _chi_square_sliding_window(
    data, cfg, window_size: int, z_threshold: float, p_threshold: float
) -> List[dict]:
    """滑动窗口卡方检验：检测窗口期内号码分布是否偏离均匀分布"""
    r_min, r_max = cfg["red_range"]
    results = []
    window_counts = [0] * (r_max - r_min + 1)

    for i, record in enumerate(data.records):
        for n in record.红球:
            window_counts[n - r_min] += 1
        if i >= window_size:
            old = data.records[i - window_size]
            for n in old.红球:
                window_counts[n - r_min] -= 1

        if i >= window_size - 1:
            total = sum(window_counts)
            if total > 0:
                expected = total / len(window_counts)
                chi2, p = scipy_stats.chisquare(f_obs=window_counts, f_exp=[expected] * len(window_counts))
                z = math.sqrt(chi2)
                # 卡方检验正确判定量是 p 值（z=√chi2 不是 z-score，随窗口增大必然变大）
                # 修正：以 p < p_threshold 为异常判定，z 仅作参考展示
                if p < p_threshold:
                    # 实际偏差度：窗口内最大号码频次相对期望的偏离比例
                    # 大样本下微小偏差也会统计显著，需结合实质偏差判断等级
                    max_dev = max(abs(c - expected) / max(expected, 1) for c in window_counts)
                    level = "严重" if max_dev > 0.8 else "可疑"
                    # 找最偏离的 3 个号码（正偏差=过热，负偏差=过冷）
                    deviations = [
                        (r_min + idx, c, (c - expected) / max(expected, 1))
                        for idx, c in enumerate(window_counts)
                    ]
                    deviations.sort(key=lambda x: abs(x[2]), reverse=True)
                    top_hot = [n for n, c, d in deviations if d > 0][:3]
                    top_cold = [n for n, c, d in deviations if d < 0][:3]
                    # 窗口内全部号码频次（供前端展开查看完整分布）
                    window_freq = [
                        {"号码": r_min + idx, "频次": c}
                        for idx, c in enumerate(window_counts)
                    ]
                    results.append({
                        "类型": "卡方检验(频率均匀性)",
                        "期号": record.期号,
                        "日期": str(record.开奖日期 or ""),
                        "窗口期": f"{data.records[i - window_size + 1].期号}~{record.期号}",
                        "z_score": round(z, 4),
                        "p_value": round(p, 6),
                        "窗口总频次": total,
                        "相关号码": {
                            "最热": top_hot,
                            "最冷": top_cold,
                            "期望频次": round(expected, 1),
                        },
                        "窗口号码频次": window_freq,
                        "最大偏差比例": round(max_dev, 4),
                        "等级": level,
                    })
    return results


# ============================================================
# 检测方法 2: Benford 定律检验（号码首位数分布）
# ============================================================

def _benford_test(data, cfg) -> List[dict]:
    """
    Benford 定律检验
    自然数据集的首位数分布应服从 P(d) = log10(1 + 1/d)
    Benford 定律常用于财务造假检测，也适用于彩票号码检验
    注意：完全随机的均匀分布号码不一定严格服从 Benford，所以此方法仅供参考
    """
    r_min, r_max = cfg["red_range"]
    # 统计所有红球号码的首位数
    first_digit_counts = Counter()
    for record in data.records:
        for n in record.红球:
            first_digit = int(str(n)[0])  # 首位数
            first_digit_counts[first_digit] += 1

    # 如果号码范围是 01-33，号码 10-33 的首位是 1-3，01-09 的首位是 1-9
    # 小范围号码不完全适用 Benford，仅作参考
    total = sum(first_digit_counts.values())
    if total == 0:
        return []

    benford_expected = {}
    for d in range(1, 10):
        benford_expected[d] = math.log10(1 + 1 / d) * total

    # 卡方检验：观测值 vs Benford 预期
    observed = [first_digit_counts.get(d, 0) for d in range(1, 10)]
    expected = [benford_expected[d] for d in range(1, 10)]

    # 只对有效数字 >= 2 的首位做检验（1-9每个至少2个样本）
    if min(expected) < 2:
        logger.debug("Benford: 样本量太小，跳过")
        return []

    chi2, p = scipy_stats.chisquare(f_obs=observed, f_exp=expected)
    z = math.sqrt(chi2)

    # 等级按 p 值分级（sqrt(chi2) 随样本量增长，不是标准 z-score，不能用作固定阈值）
    level = "严重" if p < 0.001 else "可疑" if p < 0.01 else "正常"
    # 号码范围受限(1-33)时首位数天然偏向1/2/3，Benford 不适用，降级为"参考"避免误报
    if level in ("严重", "可疑"):
        level = "参考"

    return [{
        "类型": "Benford定律(首位数)",
        "期号": data.records[0].期号,  # 标识为整体检验
        "日期": "",
        "窗口期": "全量数据",
        "z_score": round(z, 4),
        "p_value": round(p, 6),
        "窗口总频次": total,
        "等级": level,
        "首位数分布": {str(d): round(first_digit_counts.get(d, 0) / total, 4) for d in range(1, 10)},
    }]


# ============================================================
# 检测方法 3: 号码区间偏离检测
# ============================================================

def _zone_deviation_test(data, cfg, z_threshold: float) -> List[dict]:
    """
    号码区间偏离检测
    将红球按数值分 N 个区间，检测各区间实际频次 vs 理论均匀频次的偏差
    """
    r_min, r_max = cfg["red_range"]
    total_nums = r_max - r_min + 1

    # 区间划分（根据号码数量自适应）
    if total_nums <= 33:
        zone_count = 3  # 1-11, 12-22, 23-33
        zones = [(1, 11), (11, 22), (22, 34)]  # (lo, hi) 左闭右开
    else:
        zone_count = 5
        zone_size = total_nums // zone_count
        zones = [(r_min + i * zone_size, r_min + (i + 1) * zone_size) for i in range(zone_count)]

    zone_names = [f"区间{i+1}" for i in range(zone_count)]
    window_size = 50
    zone_counts = [0] * zone_count
    num_counts = {n: 0 for n in range(r_min, r_max + 1)}  # 号码级计数（找区间内最热号码）
    results = []

    for i, record in enumerate(data.records):
        for n in record.红球:
            num_counts[n] = num_counts.get(n, 0) + 1
            for zi, (lo, hi) in enumerate(zones):
                if lo <= n < hi:
                    zone_counts[zi] += 1
                    break
        if i >= window_size:
            old = data.records[i - window_size]
            for n in old.红球:
                num_counts[n] = max(num_counts.get(n, 0) - 1, 0)
                for zi, (lo, hi) in enumerate(zones):
                    if lo <= n < hi:
                        zone_counts[zi] -= 1
                        break

        if i >= window_size - 1:
            total = sum(zone_counts)
            if total > 0:
                expected_per_zone = total / zone_count
                deviations = [abs(c - expected_per_zone) / max(expected_per_zone, 1) for c in zone_counts]
                max_dev = max(deviations)
                if max_dev > 0.3:  # 偏离超过 30%
                    chi2 = sum((c - expected_per_zone)**2 / max(expected_per_zone, 1) for c in zone_counts)
                    z = math.sqrt(chi2)
                    if z > z_threshold:
                        # 找各区间内最热的号码（滑动窗口内）
                        zone_hot = {}
                        for zi, (lo, hi) in enumerate(zones):
                            nums_in_zone = [(n, num_counts.get(n, 0)) for n in range(lo, min(hi, r_max + 1))]
                            if nums_in_zone:
                                hot_n, hot_c = max(nums_in_zone, key=lambda x: x[1])
                                zone_hot[zone_names[zi]] = (hot_n, hot_c)
                        results.append({
                            "类型": "区间偏离检测",
                            "期号": record.期号,
                            "日期": str(record.开奖日期 or ""),
                            "窗口期": f"{data.records[i - window_size + 1].期号}~{record.期号}",
                            "z_score": round(z, 4),
                            "p_value": 0.0,
                            "窗口总频次": total,
                            "最大偏差比例": round(max_dev, 4),
                            "等级": "严重" if max_dev > 0.6 else "可疑",
                            "区间分布": {zone_names[zi]: zone_counts[zi] for zi in range(zone_count)},
                            "相关号码": {k: {"号码": v[0], "频次": v[1]} for k, v in zone_hot.items()},
                        })
    return results


# ============================================================
# 检测方法 4: 冷热号突变检测
# ============================================================

def _hot_cold_shift_detection(data, cfg, z_threshold: float) -> List[dict]:
    """
    冷热号突变检测
    检测每个号码的近期频率 vs 历史平均频率是否发生显著偏离
    近期窗口 = 30 期，历史窗口 = 全部数据
    红球和蓝球分开统计（避免 1-16 号段重叠导致重复/混淆）
    """
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]
    red_numbers = list(range(r_min, r_max + 1))
    blue_numbers = list(range(b_min, b_max + 1))

    # 全局频率（红球/蓝球分开）
    global_red = {n: 0 for n in red_numbers}
    global_blue = {n: 0 for n in blue_numbers}
    for record in data.records:
        for n in record.红球:
            if n in global_red:
                global_red[n] += 1
        for n in record.蓝球:
            if n in global_blue:
                global_blue[n] += 1

    total_global_red = sum(global_red.values())
    total_global_blue = sum(global_blue.values())
    global_freq_red = {n: c / max(total_global_red, 1) for n, c in global_red.items()}
    global_freq_blue = {n: c / max(total_global_blue, 1) for n, c in global_blue.items()}

    # 近期频率（最近 30 期，红球/蓝球分开）
    recent_size = 30
    recent = data.records[:recent_size]
    recent_red = {n: 0 for n in red_numbers}
    recent_blue = {n: 0 for n in blue_numbers}
    for record in recent:
        for n in record.红球:
            if n in recent_red:
                recent_red[n] += 1
        for n in record.蓝球:
            if n in recent_blue:
                recent_blue[n] += 1

    total_recent_red = sum(recent_red.values()) or 1
    total_recent_blue = sum(recent_blue.values()) or 1
    recent_freq_red = {n: c / total_recent_red for n, c in recent_red.items()}
    recent_freq_blue = {n: c / total_recent_blue for n, c in recent_blue.items()}

    # 近期窗口有效期数（数据不足30期时用实际值）
    effective_n = min(recent_size, len(data.records))

    results = []
    # 红球检测（z-score 用近期窗口样本量，否则分母极小导致 z 虚高全是"严重"）
    for n in red_numbers:
        gf, rf = global_freq_red[n], recent_freq_red[n]
        if gf > 0:
            std_est = math.sqrt(gf * (1 - gf) / max(effective_n, 1))
            z = (rf - gf) / max(std_est, 0.0001)
            if abs(z) > z_threshold:
                results.append(_shift_result(n, "红球", z, gf, rf, recent_red[n], data))
    # 蓝球检测
    for n in blue_numbers:
        gf, rf = global_freq_blue[n], recent_freq_blue[n]
        if gf > 0:
            std_est = math.sqrt(gf * (1 - gf) / max(effective_n, 1))
            z = (rf - gf) / max(std_est, 0.0001)
            if abs(z) > z_threshold:
                results.append(_shift_result(n, "蓝球", z, gf, rf, recent_blue[n], data))

    # 按 z-score 绝对值排序，返回前 20 条
    results.sort(key=lambda x: abs(x["z_score"]), reverse=True)
    return results[:20]


def _shift_result(n, num_type, z, global_freq, recent_freq, recent_count, data) -> dict:
    """构造冷热号突变结果条目"""
    trend = "显著升温" if z > 0 else "显著降温"
    # 等级分级：z 为标准正态分位数（修正后），>4 严重（极罕见），>3 可疑
    level = "严重" if abs(z) > 4 else "可疑"
    return {
        "类型": f"冷热号突变({num_type})",
        "期号": data.records[0].期号,
        "日期": "",
        "窗口期": f"近30期 vs 全量",
        "z_score": round(z, 4),
        "p_value": 0.0,
        "窗口总频次": int(recent_count),
        "等级": level,
        "号码": n,
        "趋势": trend,
        "全局频率": round(global_freq, 6),
        "近期频率": round(recent_freq, 6),
    }


# ============================================================
# 检测方法 5: 连号检测（单期极端 + 时间窗口聚集）
# ============================================================

def _consecutive_runs(nums):
    """对排序后的号码，返回 (最长连号长度, 连号组数, 最长那组号码)"""
    if not nums:
        return 0, 0, []
    s = sorted(nums)
    runs = []
    cur = [s[0]]
    for x in s[1:]:
        if x == cur[-1] + 1:
            cur.append(x)
        else:
            runs.append(cur)
            cur = [x]
    runs.append(cur)
    max_len = max(len(r) for r in runs)
    run_groups = [r for r in runs if len(r) >= 2]
    longest = max(runs, key=len)
    return max_len, len(run_groups), longest


def _consecutive_detection(data, cfg, z_threshold: float, p_threshold: float) -> List[dict]:
    """
    连号检测（两类信号）：
    1) 单期极端：某期最长连号长度在历史分布中属于罕见尾部（z-score 高）
       —— 例如 6 个红球里出现 4 连号，纯随机极少发生
    2) 时间窗口聚集：最近 W 期内"含连号（>=2连）"的期数显著高于历史基准率
       —— 直接对应"连着好几期都是连号"这种时间上的重复模式
    """
    red_numbers = list(range(cfg["red_range"][0], cfg["red_range"][1] + 1))
    red_count = cfg["red_count"]

    # 逐期最长连号长度
    run_lengths = []
    for rec in data.records:
        ml, _, _ = _consecutive_runs([n for n in rec.红球 if n in red_numbers])
        run_lengths.append(ml)

    n = len(run_lengths)
    if n == 0:
        return []
    mean_rl = sum(run_lengths) / n
    var_rl = sum((x - mean_rl) ** 2 for x in run_lengths) / n
    std_rl = math.sqrt(var_rl) if var_rl > 0 else 0.0001
    base_rate = sum(1 for x in run_lengths if x >= 2) / n  # 含连号基准率

    results = []

    # --- 1) 单期极端连号 ---
    # 仅关注较罕见的连号长度：双色球 >=4连 / 大乐透 >=3连 才值得报
    # （>=2连在双色球里约占 65%、大乐透约 50%，太常见不算异常）
    rare_threshold = 4 if red_count >= 6 else 3
    single = []
    for i, rec in enumerate(data.records):
        ml, groups, longest = _consecutive_runs([x for x in rec.红球 if x in red_numbers])
        if ml < rare_threshold:
            continue
        z = (ml - mean_rl) / std_rl if std_rl > 0 else 0
        level = "严重" if ml >= rare_threshold + 1 else "可疑"
        single.append({
            "类型": "连号检测(单期极端)",
            "期号": rec.期号,
            "日期": str(rec.开奖日期 or ""),
            "窗口期": rec.期号,
            "z_score": round(z, 4),
            "p_value": 0.0,
            "窗口总频次": ml,
            "等级": level,
            "最长连号": ml,
            "连号组数": groups,
            "连号号码": longest,
            "排序红球": sorted(rec.红球),
            "z": z,
        })
    # 按 最长连号 降序、z 降序取最极端的若干，避免淹没明细表
    single.sort(key=lambda d: (-d["最长连号"], -d["z"]))
    for d in single[:12]:
        d.pop("z", None)
        results.append(d)

    # --- 2) 时间窗口聚集 ---
    # 用经验分位数判定（≥2连占比太高，z 检验会失效）：
    # 15 期内"含连号"期数进入历史前 5%（p95）即视为一段"连号聚集期"
    W = 15
    if n >= W:
        window_obs = []
        for i in range(W - 1, n):
            window = run_lengths[i - W + 1:i + 1]
            window_obs.append(sum(1 for x in window if x >= 2))
        if window_obs:
            p95 = sorted(window_obs)[max(0, int(len(window_obs) * 0.95) - 1)]
            p99 = sorted(window_obs)[max(0, int(len(window_obs) * 0.99) - 1)]
            threshold = max(p95, 4)
            clusters = []
            for i in range(W - 1, n):
                obs = sum(1 for x in run_lengths[i - W + 1:i + 1] if x >= 2)
                if obs >= threshold:
                    issues = [data.records[j].期号 for j in range(i - W + 1, i + 1) if run_lengths[j] >= 2]
                    level = "严重" if obs >= p99 else "可疑"
                    clusters.append({
                        "start": i - W + 1, "end": i,
                        "obs": int(obs), "expected": round(base_rate * W, 1),
                        "issues": issues, "level": level,
                    })
    # 贪心去重叠：保留不重叠、最强的聚集，避免相邻窗口重复报同一段
    clusters.sort(key=lambda c: (-c["obs"], c["start"]))
    selected = []
    used = set()
    for c in clusters:
        if any(idx in used for idx in range(c["start"], c["end"] + 1)):
            continue
        selected.append(c)
        for idx in range(c["start"], c["end"] + 1):
            used.add(idx)
        if len(selected) >= 10:
            break
    for c in selected:
        rec = data.records[c["end"]]
        lo_no = min(data.records[c["start"]].期号, rec.期号)
        hi_no = max(data.records[c["start"]].期号, rec.期号)
        results.append({
            "类型": "连号聚集检测(时间窗口)",
            "期号": rec.期号,
            "日期": str(rec.开奖日期 or ""),
            "窗口期": f"{lo_no}~{hi_no}",
            "z_score": round(c["obs"] / max(W, 1), 4),
            "p_value": 0.0,
            "窗口总频次": c["obs"],
            "等级": c["level"],
            "连号期数": c["obs"],
            "期望期数": c["expected"],
            "占比": round(c["obs"] / W, 3),
            "涉及期号": sorted(c["issues"], key=lambda x: str(x)),
        })
    return results


# ============================================================
# 检测方法 6: 号码形态检测（排序分布 / 跨度-间距结构）
# ============================================================

def _shape_detection(data, cfg, z_threshold: float) -> List[dict]:
    """
    号码形态检测（排序分布）：
    对每期红球升序排列后，考察其"跨度/间距结构"是否异常。
    异常形态包括：过于聚拢（号码挤在很小跨度内）、过于发散（号码极端铺开到大跨度）。
    与冷热号不同——它看的是"每期内部号码的排列形状"，而非频率冷热。
    """
    red_numbers = list(range(cfg["red_range"][0], cfg["red_range"][1] + 1))
    spreads = []
    for rec in data.records:
        rs = sorted(x for x in rec.红球 if x in red_numbers)
        if len(rs) >= 2:
            spreads.append(rs[-1] - rs[0])
    n = len(spreads)
    if n < 10:
        return []
    mean_sp = sum(spreads) / n
    std_sp = math.sqrt(sum((x - mean_sp) ** 2 for x in spreads) / n) or 0.0001

    results = []
    for rec in data.records:
        rs = sorted(x for x in rec.红球 if x in red_numbers)
        if len(rs) < 2:
            continue
        sp = rs[-1] - rs[0]
        gaps = [rs[k + 1] - rs[k] for k in range(len(rs) - 1)]
        min_gap = min(gaps)
        max_gap = max(gaps)
        z = (sp - mean_sp) / std_sp
        if abs(z) <= z_threshold:
            continue  # 形态正常不报
        if z < 0:
            shape = "聚拢（号码挤在较小跨度内）"
        else:
            shape = "发散（号码极端铺开到大跨度）"
        level = "严重" if abs(z) > z_threshold * 2 else "可疑"
        results.append({
            "类型": "号码形态检测(排序分布)",
            "期号": rec.期号,
            "日期": str(rec.开奖日期 or ""),
            "窗口期": rec.期号,
            "z_score": round(z, 4),
            "p_value": 0.0,
            "窗口总频次": sp,
            "等级": level,
            "跨度": sp,
            "最小间距": min_gap,
            "最大间距": max_gap,
            "形态": shape,
            "排序红球": rs,
        })
    return results


# ============================================================
# 检测方法 7: 周期性检测（自相关 + 单号等间隔）
# ============================================================

def _periodicity_detection(data, cfg) -> List[dict]:
    """
    周期性检测：
    1) 自相关：对逐期"连号对数"序列做滞后自相关，寻找显著周期
       —— 直接对应"连着好几期都是连号"这类时间上的重复模式（lag=1 自相关升高）
    2) 单号周期：某号码在历史上是否以近乎固定的间隔重复出现（>=3 次近似等间隔）
    注：彩票本质无真实周期，本方法仅作为统计现象参考。
    """
    red_numbers = list(range(cfg["red_range"][0], cfg["red_range"][1] + 1))
    n = len(data.records)
    if n < 30:
        return []

    # 特征序列：每期红球中的连号对数
    feat = []
    for rec in data.records:
        rs = sorted(x for x in rec.红球 if x in red_numbers)
        pairs = 0
        for k in range(len(rs) - 1):
            if rs[k + 1] == rs[k] + 1:
                pairs += 1
        feat.append(pairs)

    results = []
    # --- 1) 自相关 ---
    max_lag = min(30, n // 3)
    best_lag = None
    best_corr = 0.0
    significant_lags = []
    fmean = sum(feat) / n
    fvar = sum((x - fmean) ** 2 for x in feat) or 0.0001
    for lag in range(1, max_lag + 1):
        cov = 0.0
        for i in range(n - lag):
            cov += (feat[i] - fmean) * (feat[i + lag] - fmean)
        cov /= (n - lag)
        corr = cov / fvar
        if corr > 0.15 and abs(corr) > abs(best_corr):
            best_corr = corr
            best_lag = lag
        if corr > 0.15:
            significant_lags.append({"lag": lag, "corr": round(corr, 4)})
    if best_lag is not None:
        results.append({
            "类型": "周期性检测(自相关)",
            "期号": data.records[-1].期号,
            "日期": str(data.records[-1].开奖日期 or ""),
            "窗口期": "全量数据",
            "z_score": round(best_corr, 4),
            "p_value": 0.0,
            "窗口总频次": best_lag,
            "等级": "参考",
            "显著周期": [s["lag"] for s in significant_lags[:5]],
            "最大自相关系数": round(best_corr, 4),
            "说明": f"连号特征在滞后 {best_lag} 期处出现显著自相关（r={best_corr:.3f}），提示号码形态可能呈现约 {best_lag} 期的周期性重复",
        })

    # --- 2) 单号周期 ---
    pos_by_num = {x: [] for x in red_numbers}
    for idx, rec in enumerate(data.records):
        for x in rec.红球:
            if x in pos_by_num:
                pos_by_num[x].append(idx)
    periodic_numbers = []
    for num, positions in pos_by_num.items():
        if len(positions) < 3:
            continue
        gaps = [positions[k + 1] - positions[k] for k in range(len(positions) - 1)]
        gmean = sum(gaps) / len(gaps)
        gstd = math.sqrt(sum((g - gmean) ** 2 for g in gaps) / len(gaps))
        if gmean >= 8 and (gstd / max(gmean, 1)) < 0.25:  # 间隔高度一致且不太密
            periodic_numbers.append({
                "号码": num,
                "间隔": round(gmean, 1),
                "出现次数": len(positions),
                "首期": data.records[positions[0]].期号,
                "末期": data.records[positions[-1]].期号,
            })
    if periodic_numbers:
        periodic_numbers.sort(key=lambda p: p["出现次数"], reverse=True)
        results.append({
            "类型": "周期性检测(单号等间隔)",
            "期号": data.records[-1].期号,
            "日期": str(data.records[-1].开奖日期 or ""),
            "窗口期": "全量数据",
            "z_score": round(len(periodic_numbers), 4),
            "p_value": 0.0,
            "窗口总频次": len(periodic_numbers),
            "等级": "参考",
            "说明": "以下号码在历史上以高度固定的间隔重复出现（疑似周期，仅供参考）",
            "周期号码": periodic_numbers[:10],
        })
    # 两类周期性信号都未检出时，给出一条"无显著周期"的参考结论，避免页面空白
    if not results:
        results.append({
            "类型": "周期性检测(自相关)",
            "期号": data.records[-1].期号,
            "日期": str(data.records[-1].开奖日期 or ""),
            "窗口期": "全量数据",
            "z_score": 0.0,
            "p_value": 0.0,
            "窗口总频次": 0,
            "等级": "参考",
            "说明": "未检测到显著周期性：连号特征在各滞后处的自相关均 < 0.15，单个号码也无高度等间隔重复——符合随机预期（彩票本身不存在真实周期）",
        })
    return results


# ============================================================
# 数字型/乐透型通用检测方法（无红/蓝分区，按 Schema.zones 遍历）
# 与上方 7 个红/蓝专用方法一一对应；双色球/大乐透 仍走上方原逻辑（零回归）。
# ============================================================

def _g_chi_square_sliding_window(data, schema, window_size, z_threshold, p_threshold):
    """逐区滑动窗口卡方：每个分区号码分布 vs 均匀分布"""
    results = []
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        span = hi - lo + 1
        wc = [0] * span
        for i, record in enumerate(data.records):
            for n in record_zone_numbers(record, zone):
                wc[n - lo] += 1
            if i >= window_size:
                for n in record_zone_numbers(data.records[i - window_size], zone):
                    wc[n - lo] -= 1
            if i >= window_size - 1:
                total = sum(wc)
                if total > 0:
                    expected = total / span
                    chi2, p = scipy_stats.chisquare(f_obs=wc, f_exp=[expected] * span)
                    z = math.sqrt(chi2)
                    if p < p_threshold:
                        max_dev = max(abs(c - expected) / max(expected, 1) for c in wc)
                        level = "严重" if max_dev > 0.8 else "可疑"
                        deviations = [(lo + idx, c, (c - expected) / max(expected, 1)) for idx, c in enumerate(wc)]
                        deviations.sort(key=lambda x: abs(x[2]), reverse=True)
                        top_hot = [n for n, c, d in deviations if d > 0][:3]
                        top_cold = [n for n, c, d in deviations if d < 0][:3]
                        window_freq = [{"号码": lo + idx, "频次": c} for idx, c in enumerate(wc)]
                        results.append({
                            "类型": f"卡方检验(频率均匀性)-{zone.name}",
                            "期号": record.期号, "日期": str(record.开奖日期 or ""),
                            "窗口期": f"{data.records[i - window_size + 1].期号}~{record.期号}",
                            "z_score": round(z, 4), "p_value": round(p, 6),
                            "窗口总频次": total,
                            "相关号码": {"最热": top_hot, "最冷": top_cold, "期望频次": round(expected, 1)},
                            "窗口号码频次": window_freq, "最大偏差比例": round(max_dev, 4), "等级": level,
                        })
    return results


def _g_benford_test(data, schema):
    """数字型首位数范围受限（0-9），Benford 定律不适用，跳过"""
    return []


def _g_zone_deviation_test(data, schema, z_threshold):
    """逐区区间偏离（按分区号码范围自适应划分子区间）"""
    results = []
    window_size = 50
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        total_nums = hi - lo + 1
        zone_count = 3 if total_nums <= 33 else 5
        zone_size = max(1, total_nums // zone_count)
        zones_ranges = [(lo + i * zone_size, lo + (i + 1) * zone_size) for i in range(zone_count)]
        zones_ranges[-1] = (zones_ranges[-1][0], hi + 1)  # 左闭右开，确保含 hi
        zone_names = [f"{zone.name}区间{i+1}" for i in range(zone_count)]
        zone_counts = [0] * zone_count
        num_counts = {n: 0 for n in range(lo, hi + 1)}
        for i, record in enumerate(data.records):
            for n in record_zone_numbers(record, zone):
                num_counts[n] = num_counts.get(n, 0) + 1
                for zi, (zlo, zhi) in enumerate(zones_ranges):
                    if zlo <= n < zhi:
                        zone_counts[zi] += 1
                        break
            if i >= window_size:
                old = data.records[i - window_size]
                for n in record_zone_numbers(old, zone):
                    num_counts[n] = max(num_counts.get(n, 0) - 1, 0)
                    for zi, (zlo, zhi) in enumerate(zones_ranges):
                        if zlo <= n < zhi:
                            zone_counts[zi] -= 1
                            break
            if i >= window_size - 1:
                total = sum(zone_counts)
                if total > 0:
                    expected_per = total / zone_count
                    deviations = [abs(c - expected_per) / max(expected_per, 1) for c in zone_counts]
                    max_dev = max(deviations)
                    if max_dev > 0.3:
                        chi2 = sum((c - expected_per) ** 2 / max(expected_per, 1) for c in zone_counts)
                        z = math.sqrt(chi2)
                        if z > z_threshold:
                            zone_hot = {}
                            for zi, (zlo, zhi) in enumerate(zones_ranges):
                                nums = [(n, num_counts.get(n, 0)) for n in range(zlo, min(zhi, hi + 1))]
                                if nums:
                                    hot_n, hot_c = max(nums, key=lambda x: x[1])
                                    zone_hot[zone_names[zi]] = (hot_n, hot_c)
                            results.append({
                                "类型": f"区间偏离检测-{zone.name}",
                                "期号": record.期号, "日期": str(record.开奖日期 or ""),
                                "窗口期": f"{data.records[i - window_size + 1].期号}~{record.期号}",
                                "z_score": round(z, 4), "p_value": 0.0, "窗口总频次": total,
                                "最大偏差比例": round(max_dev, 4),
                                "等级": "严重" if max_dev > 0.6 else "可疑",
                                "区间分布": {zone_names[zi]: zone_counts[zi] for zi in range(zone_count)},
                                "相关号码": {k: {"号码": v[0], "频次": v[1]} for k, v in zone_hot.items()},
                            })
    return results


def _g_hot_cold_shift_detection(data, schema, z_threshold):
    """逐区冷热号突变（近期频率 vs 全局频率 z-score）"""
    results = []
    recent_size = 30
    recent = data.records[:recent_size]
    effective_n = min(recent_size, len(data.records))

    def _shift(zone, n, z, gf_n, rf_n, recent_cnt):
        trend = "显著升温" if z > 0 else "显著降温"
        level = "严重" if abs(z) > 4 else "可疑"
        return {
            "类型": f"冷热号突变({zone.name})", "期号": data.records[0].期号, "日期": "",
            "窗口期": "近30期 vs 全量", "z_score": round(z, 4), "p_value": 0.0,
            "窗口总频次": int(recent_cnt), "等级": level, "号码": n, "趋势": trend,
            "全局频率": round(gf_n, 6), "近期频率": round(rf_n, 6),
        }

    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        numbers = list(range(lo, hi + 1))
        global_freq = {n: 0 for n in numbers}
        for record in data.records:
            for n in record_zone_numbers(record, zone):
                if n in global_freq:
                    global_freq[n] += 1
        total_global = sum(global_freq.values())
        gf = {n: c / max(total_global, 1) for n, c in global_freq.items()}
        recent_freq = {n: 0 for n in numbers}
        for record in recent:
            for n in record_zone_numbers(record, zone):
                if n in recent_freq:
                    recent_freq[n] += 1
        total_recent = sum(recent_freq.values()) or 1
        rf = {n: c / total_recent for n, c in recent_freq.items()}
        for n in numbers:
            if gf[n] > 0:
                std_est = math.sqrt(gf[n] * (1 - gf[n]) / max(effective_n, 1))
                z = (rf[n] - gf[n]) / max(std_est, 0.0001)
                if abs(z) > z_threshold:
                    results.append(_shift(zone, n, z, gf[n], rf[n], recent_freq[n]))
    results.sort(key=lambda x: abs(x["z_score"]), reverse=True)
    return results[:20]


def _seq_of(rec, schema):
    """把一期所有分区号码按 zone 顺序拼成一个有序序列（供连号/形态/跨度使用）"""
    out = []
    for z in schema.zones:
        out.extend(record_zone_numbers(rec, z))
    return out


def _g_consecutive_detection(data, schema, z_threshold, p_threshold):
    """数字型连号：单期按"分区顺序串联"后的号码序列连号 + 时间窗口聚集"""
    results = []
    run_lengths = []
    for rec in data.records:
        ml, _, _ = _consecutive_runs(_seq_of(rec, schema))
        run_lengths.append(ml)
    n = len(run_lengths)
    if n == 0:
        return []
    mean_rl = sum(run_lengths) / n
    var_rl = sum((x - mean_rl) ** 2 for x in run_lengths) / n
    std_rl = math.sqrt(var_rl) if var_rl > 0 else 0.0001
    base_rate = sum(1 for x in run_lengths if x >= 2) / n
    rare_threshold = 3
    single = []
    for i, rec in enumerate(data.records):
        ml, groups, longest = _consecutive_runs(_seq_of(rec, schema))
        if ml < rare_threshold:
            continue
        z = (ml - mean_rl) / std_rl if std_rl > 0 else 0
        level = "严重" if ml >= rare_threshold + 1 else "可疑"
        single.append({
            "类型": "连号检测(单期极端)", "期号": rec.期号, "日期": str(rec.开奖日期 or ""),
            "窗口期": rec.期号, "z_score": round(z, 4), "p_value": 0.0, "窗口总频次": ml,
            "等级": level, "最长连号": ml, "连号组数": groups, "连号号码": longest,
            "排序红球": sorted(_seq_of(rec, schema)), "z": z,
        })
    single.sort(key=lambda d: (-d["最长连号"], -d["z"]))
    for d in single[:12]:
        d.pop("z", None)
        results.append(d)

    W = 15
    if n >= W:
        window_obs = []
        for i in range(W - 1, n):
            window = run_lengths[i - W + 1:i + 1]
            window_obs.append(sum(1 for x in window if x >= 2))
        if window_obs:
            p95 = sorted(window_obs)[max(0, int(len(window_obs) * 0.95) - 1)]
            p99 = sorted(window_obs)[max(0, int(len(window_obs) * 0.99) - 1)]
            threshold = max(p95, 4)
            clusters = []
            for i in range(W - 1, n):
                obs = sum(1 for x in run_lengths[i - W + 1:i + 1] if x >= 2)
                if obs >= threshold:
                    issues = [data.records[j].期号 for j in range(i - W + 1, i + 1) if run_lengths[j] >= 2]
                    level = "严重" if obs >= p99 else "可疑"
                    clusters.append({"start": i - W + 1, "end": i, "obs": int(obs),
                                     "expected": round(base_rate * W, 1), "issues": issues, "level": level})
    clusters.sort(key=lambda c: (-c["obs"], c["start"]))
    selected = []
    used = set()
    for c in clusters:
        if any(idx in used for idx in range(c["start"], c["end"] + 1)):
            continue
        selected.append(c)
        for idx in range(c["start"], c["end"] + 1):
            used.add(idx)
        if len(selected) >= 10:
            break
    for c in selected:
        rec = data.records[c["end"]]
        lo_no = min(data.records[c["start"]].期号, rec.期号)
        hi_no = max(data.records[c["start"]].期号, rec.期号)
        results.append({
            "类型": "连号聚集检测(时间窗口)", "期号": rec.期号, "日期": str(rec.开奖日期 or ""),
            "窗口期": f"{lo_no}~{hi_no}", "z_score": round(c["obs"] / max(W, 1), 4), "p_value": 0.0,
            "窗口总频次": c["obs"], "等级": c["level"], "连号期数": c["obs"],
            "期望期数": c["expected"], "占比": round(c["obs"] / W, 3),
            "涉及期号": sorted(c["issues"], key=lambda x: str(x)),
        })
    return results


def _g_shape_detection(data, schema, z_threshold):
    """数字型排序形态：按分区顺序串联序列的跨度/间距结构"""
    spreads = []
    for rec in data.records:
        rs = sorted(_seq_of(rec, schema))
        if len(rs) >= 2:
            spreads.append(rs[-1] - rs[0])
    n = len(spreads)
    if n < 10:
        return []
    mean_sp = sum(spreads) / n
    std_sp = math.sqrt(sum((x - mean_sp) ** 2 for x in spreads) / n) or 0.0001
    results = []
    for rec in data.records:
        rs = sorted(_seq_of(rec, schema))
        if len(rs) < 2:
            continue
        sp = rs[-1] - rs[0]
        gaps = [rs[k + 1] - rs[k] for k in range(len(rs) - 1)]
        min_gap = min(gaps)
        max_gap = max(gaps)
        z = (sp - mean_sp) / std_sp
        if abs(z) <= z_threshold:
            continue
        if z < 0:
            shape = "聚拢（号码挤在较小跨度内）"
        else:
            shape = "发散（号码极端铺开到大跨度）"
        level = "严重" if abs(z) > z_threshold * 2 else "可疑"
        results.append({
            "类型": "号码形态检测(排序分布)", "期号": rec.期号, "日期": str(rec.开奖日期 or ""),
            "窗口期": rec.期号, "z_score": round(z, 4), "p_value": 0.0, "窗口总频次": sp,
            "等级": level, "跨度": sp, "最小间距": min_gap, "最大间距": max_gap,
            "形态": shape, "排序红球": rs,
        })
    return results


def _g_periodicity_detection(data, schema):
    """数字型周期性：逐区单号等间隔重复（彩票本质无周期，仅作参考）"""
    results = []
    n = len(data.records)
    if n < 30:
        return []
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        numbers = list(range(lo, hi + 1))
        pos_by_num = {x: [] for x in numbers}
        for idx, rec in enumerate(data.records):
            for x in record_zone_numbers(rec, zone):
                if x in pos_by_num:
                    pos_by_num[x].append(idx)
        periodic_numbers = []
        for num, positions in pos_by_num.items():
            if len(positions) < 3:
                continue
            gaps = [positions[k + 1] - positions[k] for k in range(len(positions) - 1)]
            gmean = sum(gaps) / len(gaps)
            gstd = math.sqrt(sum((g - gmean) ** 2 for g in gaps) / len(gaps))
            if gmean >= 8 and (gstd / max(gmean, 1)) < 0.25:
                periodic_numbers.append({"号码": num, "间隔": round(gmean, 1),
                    "出现次数": len(positions),
                    "首期": data.records[positions[0]].期号,
                    "末期": data.records[positions[-1]].期号})
        if periodic_numbers:
            periodic_numbers.sort(key=lambda p: p["出现次数"], reverse=True)
            results.append({
                "类型": f"周期性检测(单号等间隔)-{zone.name}",
                "期号": data.records[-1].期号, "日期": str(data.records[-1].开奖日期 or ""),
                "窗口期": "全量数据", "z_score": round(len(periodic_numbers), 4), "p_value": 0.0,
                "窗口总频次": len(periodic_numbers), "等级": "参考",
                "说明": f"{zone.name} 以下号码历史上以高度固定间隔重复出现（疑似周期，仅供参考）",
                "周期号码": periodic_numbers[:10],
            })
    if not results:
        results.append({
            "类型": "周期性检测(单号等间隔)",
            "期号": data.records[-1].期号, "日期": str(data.records[-1].开奖日期 or ""),
            "窗口期": "全量数据", "z_score": 0.0, "p_value": 0.0, "窗口总频次": 0, "等级": "参考",
            "说明": "未检测到显著周期性：单个号码无高度等间隔重复——符合随机预期（彩票本身不存在真实周期）",
        })
    return results


def _g_generate_anomaly_charts(data, schema, lottery_name, threshold, output_dir):
    """数字型分布对比图：每个分区一张子图（实际频率 vs 理论均匀频率）"""
    if not _HAS_MPL:
        return []
    zones = schema.zones
    n = len(zones)
    if n == 0:
        return []
    ncols = min(3, n) if n > 1 else 1
    nrows = math.ceil(n / ncols)
    fig, axes = plt.subplots(nrows, ncols, figsize=(6 * ncols, 4 * nrows))
    axes = np.atleast_1d(axes).flatten()
    for zi, zone in enumerate(zones):
        lo, hi = zone.range_tuple()
        counts = {i: 0 for i in range(lo, hi + 1)}
        for r in data.records:
            for nn in record_zone_numbers(r, zone):
                counts[nn] = counts.get(nn, 0) + 1
        total = sum(counts.values())
        actual = [counts[k] / total for k in range(lo, hi + 1)] if total else [0] * (hi - lo + 1)
        theory = zone.choose / (hi - lo + 1)
        ax = axes[zi]
        ax.bar(range(lo, hi + 1), actual, alpha=0.7, label="实际频率", color="#4ECDC4")
        ax.axhline(y=theory, color="red", linestyle="--", label=f"理论({theory:.4f})")
        ax.set_title(f"{lottery_name} — {zone.name} 分布")
        ax.set_xlabel(f"{zone.name}号码")
        ax.set_ylabel("频率")
        ax.legend(fontsize=8)
    for j in range(zi + 1, len(axes)):
        axes[j].axis("off")
    plt.tight_layout()
    chart_path = output_dir / f"{lottery_name}_distribution.png"
    fig.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return [str(chart_path)]


# ============================================================
# 主入口
# ============================================================

def detect_anomalies(
    lottery_name: str,
    threshold: str = "normal",
    output_dir: Optional[Path] = None,
    chart: bool = True,
) -> dict:
    """
    批量异常检测（增强版：4 种方法联合检测）

    参数：
    - threshold: strict | normal | loose | all（一键三档对比）
    - output_dir: 输出目录（默认 training/ 下按时间戳创建）
    - chart: 是否生成分布对比直方图
    """
    config = ANOMALY_THRESHOLDS.get(threshold, ANOMALY_THRESHOLDS["normal"])
    if threshold == "all":
        # all 模式：分别跑三个阈值，返回对比结果
        results_all = {}
        for t in ["strict", "normal", "loose"]:
            results_all[t] = detect_anomalies(lottery_name, t, output_dir, chart=False)
        return _merge_threshold_comparison(lottery_name, results_all, output_dir)

    z_threshold = config["z_score"]
    p_threshold = config["p_value"]
    logger.info(f"异常检测: {lottery_name}, 阈值={threshold} (z>{z_threshold})")

    data = load_lottery(lottery_name)
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = get_schema(lottery_name)
    is_rb = is_redblue(lottery_name)

    # 检测方法串联：双色球/大乐透走原红/蓝 7 法（零回归）；数字型走通用分区 7 法
    all_anomalies = []

    if is_rb:
        # 1. 卡方检验
        all_anomalies.extend(
            _chi_square_sliding_window(data, cfg, window_size=100, z_threshold=z_threshold, p_threshold=p_threshold)
        )
        # 2. Benford 定律
        all_anomalies.extend(_benford_test(data, cfg))
        # 3. 区间偏离
        all_anomalies.extend(_zone_deviation_test(data, cfg, z_threshold=z_threshold))
        # 4. 冷热号突变
        all_anomalies.extend(_hot_cold_shift_detection(data, cfg, z_threshold=z_threshold))
        # 5. 连号检测（单期极端 + 时间窗口聚集）
        all_anomalies.extend(_consecutive_detection(data, cfg, z_threshold=z_threshold, p_threshold=p_threshold))
        # 6. 号码形态检测（排序分布 / 跨度-间距结构）
        all_anomalies.extend(_shape_detection(data, cfg, z_threshold=z_threshold))
        # 7. 周期性检测（连号特征自相关 + 单号等间隔）
        all_anomalies.extend(_periodicity_detection(data, cfg))
    else:
        all_anomalies.extend(_g_chi_square_sliding_window(data, schema, window_size=100, z_threshold=z_threshold, p_threshold=p_threshold))
        all_anomalies.extend(_g_benford_test(data, schema))
        all_anomalies.extend(_g_zone_deviation_test(data, schema, z_threshold=z_threshold))
        all_anomalies.extend(_g_hot_cold_shift_detection(data, schema, z_threshold=z_threshold))
        all_anomalies.extend(_g_consecutive_detection(data, schema, z_threshold=z_threshold, p_threshold=p_threshold))
        all_anomalies.extend(_g_shape_detection(data, schema, z_threshold=z_threshold))
        all_anomalies.extend(_g_periodicity_detection(data, schema))

    # 按 z_score 排序
    all_anomalies.sort(key=lambda x: abs(x.get("z_score", 0)), reverse=True)

    # 构造代表性明细：每类方法保底取 z 最高的一条（确保连号/形态/周期等低 z 方法不被
    # 卡方/区间的高 z 淹没），其余名额按 z 降序补足，总计上限 250
    by_type = defaultdict(list)
    for a in all_anomalies:
        by_type[a["类型"]].append(a)  # all_anomalies 已按 z 降序，[0] 即该类最高
    guaranteed = [v[0] for v in by_type.values()]
    guaranteed_types = {a["类型"] for a in guaranteed}
    rest = [a for a in all_anomalies if a["类型"] not in guaranteed_types][:250 - len(guaranteed)]
    representative = guaranteed + rest

    # 统计各类方法异常数
    type_counts = Counter(a["类型"] for a in all_anomalies)

    # 确定输出目录
    if output_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        code = LOTTERY_CONFIG[lottery_name]["name_en"]
        output_dir = TRAINING_DIR / f"{timestamp}_{code}_anomaly"
        output_dir.mkdir(parents=True, exist_ok=True)

    # 导出 CSV
    csv_path = _export_anomaly_csv(lottery_name, all_anomalies, threshold, output_dir)

    # 生成图表
    chart_paths = []
    if chart and _HAS_MPL:
        if is_rb:
            chart_paths.extend(_generate_anomaly_charts(data, cfg, lottery_name, threshold, output_dir))
        else:
            chart_paths.extend(_g_generate_anomaly_charts(data, schema, lottery_name, threshold, output_dir))

    result = {
        "lottery_name": lottery_name,
        "阈值配置": config,
        "阈值等级": threshold,
        "总期数": len(data.records),
        "异常总条数": len(all_anomalies),
        "各方法异常数": dict(type_counts),
        "异常明细": representative,
        "导出文件": str(csv_path) if csv_path else "",
        "图表文件": chart_paths,
        "输出目录": str(output_dir),
    }

    return result


def _merge_threshold_comparison(lottery_name: str, results: dict, output_dir: Optional[Path]) -> dict:
    """合并三档阈值对比结果"""
    if output_dir is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        code = LOTTERY_CONFIG[lottery_name]["name_en"]
        output_dir = TRAINING_DIR / f"{timestamp}_{code}_anomaly_all"
        output_dir.mkdir(parents=True, exist_ok=True)

    comparison = {}
    for t, r in results.items():
        # 聚合"分区方法名"为"基础方法名"（数字型按分区展开，聚合回基方法，便于对比表）
        agg = {}
        for mname, cnt in r["各方法异常数"].items():
            base = mname.split("-")[0]
            agg[base] = agg.get(base, 0) + cnt
        comparison[t] = {
            "阈值": r["阈值配置"],
            "异常总条数": r["异常总条数"],
            "各方法异常数": agg,
        }

    # 生成对比报告（动态方法名，兼容双色球/大乐透与数字型）
    report_path = output_dir / "comparison.md"
    all_methods = sorted({m for t in comparison for m in comparison[t]["各方法异常数"]})
    header = "| 阈值 | z_score | 异常总条数 | " + " | ".join(all_methods) + " |"
    sep = "|------|---------|-----------|" + "|".join(["------"] * len(all_methods)) + "|"
    lines = [f"# {lottery_name} 异常检测 — 三档阈值对比", "", header, sep]
    for t in ["strict", "normal", "loose"]:
        c = comparison[t]
        tc = c["各方法异常数"]
        cells = " | ".join(str(tc.get(m, 0)) for m in all_methods)
        lines.append(f"| {t} | {c['阈值']['z_score']} | {c['异常总条数']} | {cells} |")
    lines.append("")
    lines.append(f"生成时间: {datetime.now().isoformat()}")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    return {
        "lottery_name": lottery_name,
        "阈值等级": "all",
        "总期数": results["normal"]["总期数"],
        "三档对比": comparison,
        "对比报告": str(report_path),
        "输出目录": str(output_dir),
    }


# ============================================================
# CSV 导出（增强字段）
# ============================================================

def _export_anomaly_csv(
    lottery_name: str,
    records: List[dict],
    threshold: str,
    output_dir: Path,
) -> Optional[Path]:
    """增强 CSV 导出：含所有检测维度"""
    if not records:
        logger.info("无异常记录，跳过导出")
        return None

    output_path = output_dir / f"{lottery_name}_anomaly_{threshold}.csv"

    fieldnames = [
        "类型", "期号", "日期", "窗口期", "z_score", "p_value",
        "等级", "窗口总频次", "号码", "趋势", "全局频率", "近期频率",
        "最长连号", "连号组数", "连号期数", "期望期数", "跨度",
        "最小间距", "最大间距", "形态", "显著周期", "周期号码", "涉及期号",
    ]
    with open(output_path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for r in records:
            row = {k: r.get(k, "") for k in fieldnames}
            writer.writerow(row)

    logger.info(f"异常 CSV: {output_path} ({len(records)} 条)")
    return output_path


# ============================================================
# 分布对比直方图
# ============================================================

def _generate_anomaly_charts(data, cfg, lottery_name: str, threshold: str, output_dir: Path) -> List[str]:
    """生成分布对比直方图"""
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]
    charts = []

    # 1. 红球号码出现频率直方图（实际 vs 理论）
    fig, axes = plt.subplots(1, 2, figsize=(14, 5))

    red_counts = {i: 0 for i in range(r_min, r_max + 1)}
    for r in data.records:
        for n in r.红球:
            red_counts[n] = red_counts.get(n, 0) + 1
    total_red = sum(red_counts.values())
    actual_freq = [red_counts[n] / total_red for n in range(r_min, r_max + 1)]
    theoretical = [cfg["red_count"] / (r_max - r_min + 1) / cfg["red_count"] * len(data.records) / len(data.records)] * len(actual_freq)
    # 修正: 理论频率 = 每期 red_count 个号 / 总号码数
    theory_val = cfg["red_count"] / (r_max - r_min + 1)
    theoretical = [theory_val] * len(actual_freq)

    ax = axes[0]
    ax.bar(range(r_min, r_max + 1), actual_freq, alpha=0.7, label="实际频率", color="#4ECDC4")
    ax.axhline(y=theory_val, color="red", linestyle="--", label=f"理论频率({theory_val:.4f})")
    ax.set_xlabel("红球号码")
    ax.set_ylabel("出现频率")
    ax.set_title(f"{lottery_name} — 红球分布直方图")
    ax.legend()

    # 2. 蓝球号码出现频率直方图
    blue_counts = {i: 0 for i in range(b_min, b_max + 1)}
    for r in data.records:
        for n in r.蓝球:
            blue_counts[n] = blue_counts.get(n, 0) + 1
    total_blue = sum(blue_counts.values())
    actual_blue = [blue_counts[n] / total_blue for n in range(b_min, b_max + 1)]
    theory_blue = cfg["blue_count"] / (b_max - b_min + 1)

    ax = axes[1]
    ax.bar(range(b_min, b_max + 1), actual_blue, alpha=0.7, label="实际频率", color="#FF6B6B")
    ax.axhline(y=theory_blue, color="red", linestyle="--", label=f"理论频率({theory_blue:.4f})")
    ax.set_xlabel("蓝球号码")
    ax.set_ylabel("出现频率")
    ax.set_title(f"{lottery_name} — 蓝球分布直方图")
    ax.legend()

    plt.tight_layout()
    chart_path = output_dir / f"{lottery_name}_distribution.png"
    fig.savefig(chart_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(chart_path))

    # 3. z-score 时间线（如果卡方结果足够多）
    sample_chi = [r for r in data.records[:len(data.records)]][::50]
    if len(sample_chi) > 2:
        fig, ax = plt.subplots(figsize=(14, 4))
        z_scores = []
        labels = []
        window_size = 50
        wc = [0] * (r_max - r_min + 1)
        for i, r in enumerate(data.records):
            for n in r.红球:
                wc[n - r_min] += 1
            if i >= window_size:
                old = data.records[i - window_size]
                for n in old.红球:
                    wc[n - r_min] -= 1
            if i >= window_size - 1 and i % 20 == 0:
                total = sum(wc)
                if total > 0:
                    exp = total / len(wc)
                    chi2 = sum((c - exp)**2 / exp for c in wc if exp > 0)
                    z_scores.append(math.sqrt(chi2))
                    labels.append(r.期号)

        if z_scores:
            ax.plot(range(len(z_scores)), z_scores, color="#45B7D1", linewidth=0.8)
            ax.axhline(y=ANOMALY_THRESHOLDS["normal"]["z_score"], color="orange", linestyle="--", label="normal 阈值")
            ax.axhline(y=ANOMALY_THRESHOLDS["strict"]["z_score"], color="red", linestyle="--", label="strict 阈值")
            ax.set_xlabel("时间（每20期采样）")
            ax.set_ylabel("卡方 z-score")
            ax.set_title(f"{lottery_name} — 卡方 z-score 时间线")
            ax.legend()
            # 每隔 50 个点标一期号
            tick_step = max(1, len(labels) // 20)
            ax.set_xticks(range(0, len(z_scores), tick_step))
            ax.set_xticklabels([labels[i] for i in range(0, len(labels), tick_step)], rotation=45, fontsize=8)

            chart_path = output_dir / f"{lottery_name}_zscore_timeline.png"
            fig.savefig(chart_path, dpi=150, bbox_inches="tight")
            plt.close(fig)
            charts.append(str(chart_path))

    return charts


# ============================================================
# CLI 入口
# ============================================================

def run_cli(lottery_name: str, threshold: str = None) -> None:
    """CLI 入口（增强输出）"""
    if threshold is None:
        threshold = ANOMALY_DEFAULT

    result = detect_anomalies(lottery_name, threshold)

    if threshold == "all":
        # 三档对比输出
        print(f"\n===== {lottery_name} 异常检测 — 三档阈值对比 =====")
        print(f"总期数: {result['总期数']}")
        print(f"")
        methods = sorted({m for t in ["strict", "normal", "loose"] for m in result["三档对比"][t]["各方法异常数"]})
        # 动态表头（兼容双色球/大乐透与数字型）
        def _col(s, w):
            return f"{s:>{w}}"
        head = f"{_col('阈值',8)} | {_col('z阈值',6)} | {_col('总条数',6)} | " + " | ".join(_col(m, max(6, len(m))) for m in methods) + " |"
        sep = f"{'─'*8}─┼─{'─'*6}─┼─{'─'*6}─┼─" + "─┼─".join("─" * max(6, len(m)) for m in methods) + "─┤"
        print(head)
        print(sep)
        for t in ["strict", "normal", "loose"]:
            c = result["三档对比"][t]
            tc = c["各方法异常数"]
            cells = " | ".join(_col(str(tc.get(m, 0)), max(6, len(m))) for m in methods)
            print(f"{_col(t,8)} | {_col(c['阈值']['z_score'],6)} | {_col(c['异常总条数'],6)} | {cells} |")
        if result.get("对比报告"):
            print(f"\n对比报告: {result['对比报告']}")
        return

    print(f"\n===== {lottery_name} 异常检测 =====")
    print(f"阈值等级: {threshold} (z>{result['阈值配置']['z_score']})")
    print(f"总期数: {result['总期数']}")
    print(f"异常总条数: {result['异常总条数']}")
    print(f"各方法检测结果:")
    for method, count in result.get("各方法异常数", {}).items():
        print(f"  {method}: {count} 条")

    if result["异常明细"]:
        print(f"\n异常明细（Top10）:")
        for r in result["异常明细"][:10]:
            extra = ""
            if r.get("号码"):
                extra = f" 号码{r['号码']}({r.get('趋势','')})"
            if r.get("首位数分布"):
                extra = " [首位数分布见 CSV]"
            print(f"  [{r['等级']:3s}] {r['类型']:16s} 期号{r.get('期号','')} z={r.get('z_score',0):.2f} {extra}")

    if result["导出文件"]:
        print(f"\nCSV: {result['导出文件']}")
    if result.get("图表文件"):
        for p in result["图表文件"]:
            print(f"图表: {p}")
    print(f"输出目录: {result['输出目录']}")
