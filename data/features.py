"""
特征构造模块（第三档升级 - 地基）

把"喂给模型的数据"做厚，供训练与预测统一取数，避免两边各写一套导致不一致。

特征版本：
- 版本 1（标准）：与 train/engine.py 原 _build_features 完全等价，保证旧模型可加载
- 版本 2（丰富）：在标准版基础上增加
    1) 近期窗口频率（近 10 / 近 30 期）—— 捕捉短期热度/动量
    2) 遗漏斜率（近期遗漏相对窗口前半段的升降）—— 捕捉冷热切换速度
    3) 共现特征（每个号码与当期最热门 5 个号的同出率）—— 捕捉号码间的组合结构

阶段0地基：所有"红/蓝"硬编码改为遍历彩票结构 Schema 的区(zones)，
双色球/大乐透的区顺序固定为 [红球, 蓝球]，故数值与旧逻辑完全一致。

防偷看未来：所有特征只用"截至目标期之前"的窗口数据，绝不含目标期自身信息。
可复现：纯确定性计算，无随机。
"""

from typing import List, Tuple

import numpy as np

from config import LOTTERY_CONFIG
from data.schema import DrawRecord, schema_from_cfg, record_zone_numbers

FEATURE_VERSION_STANDARD = 1
FEATURE_VERSION_RICH = 2

# 共现特征里参考的"热门号码"数量
COOC_TOP_K = 5
# 近期窗口大小列表（动量特征）
RECENT_WINDOWS = [10, 30]


def _zone_numbers(rec: DrawRecord, zone) -> List[int]:
    """从开奖记录中取出某区的号码列表（兼容 DrawRecord 的 红球/蓝球 属性）"""
    return record_zone_numbers(rec, zone)


def _main_zone_bounds(schema) -> Tuple[int, int, int, int, int]:
    """返回主区(首个区)的大小比分界点、三区分界点（与旧 _zone_bounds 等价）"""
    main = schema.zones[0]
    r_min, r_max = main.range_tuple()
    r_mid = (r_min + r_max) // 2
    r_third = (r_max - r_min + 1) // 3
    zone1_hi = r_min + r_third
    zone2_hi = r_min + 2 * r_third
    return r_min, r_max, r_mid, zone1_hi, zone2_hi


def _window_zone_freq(window: List[DrawRecord], schema) -> dict:
    """统计窗口内每个区各号码出现频次（返回 {区名: {号码: 次数}}）"""
    freqs = {}
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        f = {n: 0 for n in range(lo, hi + 1)}
        for rec in window:
            for n in _zone_numbers(rec, zone):
                f[n] = f.get(n, 0) + 1
        freqs[zone.name] = f
    return freqs


def _omission_gap_newest_first(window: List[DrawRecord], schema) -> dict:
    """
    按"窗口给定顺序（原逻辑为最新在前）"计算遗漏 gap：
    从窗口起点（最新）往后扫，直到某号首次出现，记其间隔；未出现记 window_size。
    返回 {区名: {号码: gap}}。
    """
    wlen = max(len(window), 1)
    gaps = {}
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        g = {n: wlen for n in range(lo, hi + 1)}
        for n in range(lo, hi + 1):
            gap = 0
            for rec in window:
                if n in _zone_numbers(rec, zone):
                    break
                gap += 1
            g[n] = gap
        gaps[zone.name] = g
    return gaps


def _current_omission_chrono(window_sorted: List[DrawRecord], schema) -> dict:
    """
    按时间正序（最旧→最新）计算"截至最新一期的当前遗漏"（距上次出现的期数）。
    用于丰富版的遗漏斜率。返回 {区名: {号码: 遗漏}}。
    """
    omis = {}
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        last = {n: None for n in range(lo, hi + 1)}
        for idx, rec in enumerate(window_sorted):
            for n in _zone_numbers(rec, zone):
                last[n] = idx
        wlen = max(len(window_sorted), 1)
        omis[zone.name] = {
            n: (wlen - 1 - last[n]) if last[n] is not None else wlen
            for n in range(lo, hi + 1)
        }
    return omis


def _companion_cooc(window_sorted: List[DrawRecord], schema) -> dict:
    """
    共现特征（组合结构的最小版）：
    对每个号码，计算它与"窗口内最热门 5 个号"的平均同出率。
    同出率 = 在多少比例的窗口期里，该号与热门号同时出现。
    返回 {区名: {号码: 共现值}}。
    """
    freqs = _window_zone_freq(window_sorted, schema)
    cooc = {}
    wlen = max(len(window_sorted), 1)
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        f = freqs[zone.name]
        top = [n for n, _ in sorted(f.items(), key=lambda x: x[1], reverse=True)[:COOC_TOP_K]]
        result = {n: 0.0 for n in range(lo, hi + 1)}
        for n in range(lo, hi + 1):
            cnt = 0
            for rec in window_sorted:
                if n in _zone_numbers(rec, zone):
                    co = sum(1 for t in top if t != n and t in _zone_numbers(rec, zone))
                    cnt += co / max(len(top) - (1 if n in top else 0), 1)
            result[n] = cnt / wlen
        cooc[zone.name] = result
    return cooc


def build_feature_vector_v1(window: List[DrawRecord], cfg: dict) -> list:
    """
    版本 1：与 train/engine._build_features 完全等价（保持旧模型兼容）。
    window 顺序沿用调用方传入（最新在前），以保证遗漏 gap 计算一致。
    """
    schema = schema_from_cfg(cfg)
    zones = schema.zones
    r_min, r_max, r_mid, zone1_hi, zone2_hi = _main_zone_bounds(schema)
    window_size = max(len(window), 1)

    freqs = _window_zone_freq(window, schema)
    gaps = _omission_gap_newest_first(window, schema)
    gaps_norm = {z: {n: v / window_size for n, v in g.items()} for z, g in gaps.items()}

    feat = []
    for zone in zones:
        for n in range(zone.min, zone.max + 1):
            feat.append(freqs[zone.name][n] / window_size)
    for zone in zones:
        for n in range(zone.min, zone.max + 1):
            feat.append(gaps_norm[zone.name][n])

    # 结构特征（主区奇偶/大小/三区占比）
    main = zones[0]
    odd_count = 0
    total = 0
    small_count = 0
    zone1_count = zone2_count = zone3_count = 0
    for rec in window:
        for n in _zone_numbers(rec, main):
            total += 1
            if n % 2 == 1:
                odd_count += 1
            if n <= r_mid:
                small_count += 1
            if n <= zone1_hi:
                zone1_count += 1
            elif n <= zone2_hi:
                zone2_count += 1
            else:
                zone3_count += 1
    feat.append(odd_count / max(total, 1))
    feat.append(small_count / max(total, 1))
    feat.append(zone1_count / max(total, 1))
    feat.append(zone2_count / max(total, 1))
    feat.append(zone3_count / max(total, 1))
    return feat


def build_feature_vector_v2(window: List[DrawRecord], cfg: dict) -> list:
    """
    版本 2（丰富）：在标准版基础上增加动量、冷热斜率、共现三类特征。
    window 可为任意顺序，内部按 期号 正序排好后再算。
    """
    schema = schema_from_cfg(cfg)
    zones = schema.zones
    r_min, r_max, r_mid, zone1_hi, zone2_hi = _main_zone_bounds(schema)
    window_sorted = sorted(window, key=lambda r: r.期号 if r.期号 else 0)
    window_size = max(len(window_sorted), 1)

    # —— 标准版基础块（频率/遗漏/结构）——
    freqs = _window_zone_freq(window_sorted, schema)
    omis = _current_omission_chrono(window_sorted, schema)

    # —— 丰富块 1：近期窗口频率（动量）——
    recent_freqs = {}
    for K in RECENT_WINDOWS:
        sub = window_sorted[-K:] if len(window_sorted) >= K else window_sorted
        rf = _window_zone_freq(sub, schema)
        recent_freqs[K] = (rf, max(len(sub), 1))

    # —— 丰富块 2：遗漏斜率（近期遗漏 - 前半段遗漏）——
    half = max(window_size // 2, 1)
    first_half = window_sorted[:half]
    second_half = window_sorted[half:]
    omis_first = _current_omission_chrono(first_half, schema)
    omis_second = _current_omission_chrono(second_half, schema)

    # —— 丰富块 3：共现特征 ——
    cooc = _companion_cooc(window_sorted, schema)

    # 组装：先放标准块（与 v1 同语义，但遗漏用当前遗漏而非 gap，更合理）
    feat = []
    for zone in zones:
        for n in range(zone.min, zone.max + 1):
            feat.append(freqs[zone.name][n] / window_size)
    for zone in zones:
        for n in range(zone.min, zone.max + 1):
            feat.append(omis[zone.name][n] / window_size)

    # 结构特征（主区）
    main = zones[0]
    odd_count = 0
    total = 0
    small_count = 0
    zone1_count = zone2_count = zone3_count = 0
    for rec in window_sorted:
        for n in _zone_numbers(rec, main):
            total += 1
            if n % 2 == 1:
                odd_count += 1
            if n <= r_mid:
                small_count += 1
            if n <= zone1_hi:
                zone1_count += 1
            elif n <= zone2_hi:
                zone2_count += 1
            else:
                zone3_count += 1
    feat.append(odd_count / max(total, 1))
    feat.append(small_count / max(total, 1))
    feat.append(zone1_count / max(total, 1))
    feat.append(zone2_count / max(total, 1))
    feat.append(zone3_count / max(total, 1))

    # 丰富块追加（每个号码）
    for zone in zones:
        lo, hi = zone.range_tuple()
        for n in range(lo, hi + 1):
            # 近期频率（动量）
            for K in RECENT_WINDOWS:
                rf, denom = recent_freqs[K]
                feat.append(rf[zone.name][n] / denom)
            # 遗漏斜率（正=近期变冷）
            # 兼容说明：旧版 features.py 解包 _current_omission_chrono 时，
            # blue_omis_first/second 误取了红球遗漏（函数返回 (red, blue)），
            # 导致蓝球斜率实际使用了红球遗漏。此处原样保留该行为，
            # 保证与历史 rich 模型特征逐元素一致（零破坏）。后续如需修正，
            # 应作为显式版本升级单独处理。
            if zone.name == "蓝球" and schema.red_zone is not None:
                slope = (omis_second[schema.red_zone.name][n] - omis_first[schema.red_zone.name][n]) / window_size
            else:
                slope = (omis_second[zone.name][n] - omis_first[zone.name][n]) / window_size
            feat.append(slope)
            # 共现
            feat.append(cooc[zone.name][n])
    return feat


def build_features(
    records: List[DrawRecord],
    window_size: int,
    lottery_name: str,
    version: int = FEATURE_VERSION_STANDARD,
) -> Tuple[np.ndarray, np.ndarray]:
    """
    为整个数据集构建特征矩阵与标签（与 train/engine._build_features 同接口）。
    records: **最新在前**（loader 按期号降序排列，records[0] 是最新一期）。

    ⚠️ 时间方向（2026-08-31 修复）：
    原实现取 `window = records[i:i+ws]`、`target = records[i+ws]`。
    因为索引越大 = 时间越早，这等于**用更晚的开奖去预测更早的一期**（反向预测），
    与线上推理 `prediction/engine.py:505` 的语义（用最近 ws 期预测下一期）相反。
    现改为 `window = records[i+1 : i+1+ws]`（更早的 ws 期）、`target = records[i]`（更晚的一期），
    使训练语义 = 线上语义 = 「用过去 ws 期预测下一期」。

    返回 (X, y)，y 为每号码独立二分类标签。
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)
    n_samples = len(records) - window_size
    if n_samples <= 0:
        return np.array([]), np.array([])

    builder = build_feature_vector_v1 if version == FEATURE_VERSION_STANDARD else build_feature_vector_v2

    features = []
    labels = []
    for i in range(n_samples):
        # window 必须比 target 更早（索引更大），与线上预测方向一致
        window = records[i + 1:i + 1 + window_size]
        target = records[i]
        feat = builder(window, cfg)
        features.append(feat)
        label = []
        for zone in schema.zones:
            lo, hi = zone.range_tuple()
            label += [1 if n in _zone_numbers(target, zone) else 0 for n in range(lo, hi + 1)]
        labels.append(label)
    return np.array(features), np.array(labels)


def feature_dim(cfg: dict, version: int = FEATURE_VERSION_STANDARD) -> int:
    """返回指定版本的每样本特征维度（供预测端对齐）"""
    schema = schema_from_cfg(cfg)
    total_size = sum(z.size for z in schema.zones)
    # 标准块：频率+遗漏 各 total_size，结构 5（主区）
    base = total_size * 2 + 5
    if version == FEATURE_VERSION_RICH:
        # 每号码追加：近期窗口数 + 斜率 + 共现
        extra_per = len(RECENT_WINDOWS) + 1 + 1
        base += total_size * extra_per
    return base
