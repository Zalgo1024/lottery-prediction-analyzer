"""
预测引擎（迭代版）

改进：
1. 三种号码生成策略：高频策略、遗漏值策略、区间均衡策略
2. 相似度去重（红球重叠≥4 算重复）
3. 置信度评分扩展到 4 维（含历史回测命中率）
4. trained 模式加载真实 ML 模型权重做预测
5. 预测附带历史拟合准确率
"""

import json
import logging
import math
import pickle
import random
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

from config import LOTTERY_CONFIG, PREDICT_DEFAULTS, TRAINING_DIR
from data.loader import load_lottery
from data.schema import LotteryData, schema_from_cfg
from data.holiday import next_draw_date
from pipeline.statistics import frequency_analysis, missing_value_analysis
from prediction.optimizer import ML_STRATEGY

logger = logging.getLogger(__name__)


# ============================================================
# 数据结构
# ============================================================

class PredictSet:
    """预测号码组数据结构，预留复式/胆拖接口

    阶段0/阶段2地基：内部以「分区字典」(zone_name -> 号码列表) 为唯一真相，
    reds/blues 作为红/蓝彩种的便捷视图保留（向后兼容）。
    """

    def __init__(
        self,
        reds: List[int] = None,
        blues: List[int] = None,
        zones: Optional[Dict[str, List[int]]] = None,
        confidence: float = 0.0,
        score_detail: Optional[dict] = None,
        set_type: str = "single",
        strategy: str = "",
        robustness: Optional[dict] = None,
        tier: Optional[str] = None,
    ):
        if zones is not None:
            self.zones = {k: sorted(v) for k, v in zones.items()}
        else:
            self.zones = {}
            if reds is not None:
                self.zones["红球"] = sorted(reds)
            if blues is not None:
                self.zones["蓝球"] = sorted(blues)
        self.reds = self.zones.get("红球", [])
        self.blues = self.zones.get("蓝球", [])
        self.confidence = confidence
        self.score_detail = score_detail or {}
        self.type = set_type
        self.strategy = strategy
        # 鲁棒性元数据（可选，三档管线 robust_tiers 专用；普通 predict 不填则不出现在 to_dict）
        # robustness 形如 {"共识比": float, "入选窗口数": int, "窗口数": int,
        #                  "平均置信度": float, "出现次数": int}
        # tier ∈ {"一般", "稳健", "高鲁棒"}（中文标签直接进前端/反馈）
        self.robustness = robustness
        self.tier = tier

    def to_dict(self) -> dict:
        d = {
            "置信度": round(self.confidence, 4),
            "评分明细": self.score_detail,
            "类型": self.type,
            "策略": self.strategy,
        }
        # 红/蓝彩种保留原字段，便于下游(反馈/前端)兼容
        if "红球" in self.zones:
            d["红球"] = self.reds
        if "蓝球" in self.zones:
            d["蓝球"] = self.blues
        # 通用：所有分区的号码（新彩种用此字段）
        d["号码"] = {k: v for k, v in self.zones.items()}
        # 三档管线可选字段（向后兼容：不填不出现，现有 117 测试不受影响）
        if self.robustness:
            d["鲁棒性"] = self.robustness
        if self.tier:
            d["档位"] = self.tier
        return d

    def __eq__(self, other):
        if not isinstance(other, PredictSet):
            return False
        return self.zones == other.zones

    def __hash__(self):
        return hash(tuple((k, tuple(v)) for k, v in sorted(self.zones.items())))


# ============================================================
# 去重（含相似度去重）
# ============================================================

def _is_too_similar(reds: List[int], blues: List[int], existing: set, max_overlap: int = 4) -> bool:
    """
    相似度去重：与已存在号码组红球重叠 >= max_overlap 即算重复
    """
    red_set = set(reds)
    for key in existing:
        existing_reds, existing_blues = key
        overlap = len(red_set & set(existing_reds))
        if overlap >= max_overlap:
            return True
    return False


def _dedup_and_record(reds: List[int], blues: List[int], existing: set, cfg: dict) -> Tuple[List[int], List[int], bool]:
    """
    去重：先检查完全相等，再检查相似度
    如果重复则尝试微调，返回 (reds, blues, is_valid)
    """
    schema = schema_from_cfg(cfg)
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_min, r_max = red_zone.range_tuple()
    b_min, b_max = blue_zone.range_tuple()
    r_count = red_zone.choose
    b_count = blue_zone.choose

    # 检查完全相等
    key = (tuple(sorted(reds)), tuple(sorted(blues)))
    if key in existing:
        # 微调：替换红球中最后一个为目标范围内未出现的号码
        for _ in range(10):
            new_n = random.randint(r_min, r_max)
            temp = list(reds)
            temp[-1] = new_n
            temp = sorted(set(temp))
            # 如果微调后数量不够，补一个
            while len(temp) < r_count:
                extra = random.randint(r_min, r_max)
                if extra not in temp:
                    temp.append(extra)
                temp.sort()
            new_key = (tuple(temp), tuple(sorted(blues)))
            if new_key not in existing:
                key = new_key
                reds = temp
                break
        else:
            # 微调失败，放弃这组
            return reds, blues, False

    # 检查相似度
    if _is_too_similar(reds, blues, existing, max_overlap=4):
        # 相似度太高，替换一半号码
        for _ in range(10):
            temp = list(reds)
            num_to_replace = max(1, r_count // 2)
            for idx in random.sample(range(len(temp)), num_to_replace):
                temp[idx] = random.randint(r_min, r_max)
            temp = sorted(set(temp))
            while len(temp) < r_count:
                extra = random.randint(r_min, r_max)
                if extra not in temp:
                    temp.append(extra)
                temp.sort()
            if not _is_too_similar(temp, blues, existing, max_overlap=4):
                reds = temp
                break
        else:
            return reds, blues, False

    # 记录**当前实际输出**的 key（不是微调前的旧 key）。
    # ⚠️ 旧代码直接 add 行 125 算的 key：相似分支微调成功后 reds 已变但 key 未更新，
    # 会把「微调前的幽灵号」写进 existing，后续号码只跟幽灵号比较 → 真实输出之间
    # 可能红球重叠 >= 4（实测双色球 300 组出现 102 对，其中 3 对重叠 5）。
    existing.add((tuple(sorted(reds)), tuple(sorted(blues))))
    return reds, blues, True


# ============================================================
# 三种号码生成策略
# ============================================================

def _strategy_high_freq(
    data: LotteryData, cfg: dict, existing: set, freqs: dict, missing: dict
) -> Optional[PredictSet]:
    """
    策略一：高频策略 — 从历史出现频率最高的号码中抽取
    适用场景：相信热门号码会继续出现
    """
    schema = schema_from_cfg(cfg)
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_min, r_max = red_zone.range_tuple()
    b_min, b_max = blue_zone.range_tuple()
    r_count = red_zone.choose
    b_count = blue_zone.choose

    red_freq = freqs["红球"]["freq"]
    blue_freq = freqs["蓝球"]["freq"]

    # 按频率排序，从 Top N 号码中随机抽取（N = 每期号码数的 5 倍）
    sorted_red = sorted(red_freq.items(), key=lambda x: x[1], reverse=True)
    sorted_blue = sorted(blue_freq.items(), key=lambda x: x[1], reverse=True)

    top_red = [n for n, _ in sorted_red[:r_count * 5]]
    top_blue = [n for n, _ in sorted_blue[:b_count * 5]]

    reds = sorted(random.sample(top_red, r_count))
    blues = sorted(random.sample(top_blue, b_count))

    reds, blues, valid = _dedup_and_record(reds, blues, existing, cfg)
    if not valid:
        return None

    return PredictSet(reds, blues, strategy="高频策略")


def _strategy_missing_value(
    data: LotteryData, cfg: dict, existing: set, freqs: dict, missing: dict
) -> Optional[PredictSet]:
    """
    策略二：遗漏值策略 — 从当前遗漏值最高的号码中抽取
    适用场景：相信长期未出号码会回补（赌冷号反弹）
    """
    schema = schema_from_cfg(cfg)
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_min, r_max = red_zone.range_tuple()
    b_min, b_max = blue_zone.range_tuple()
    r_count = red_zone.choose
    b_count = blue_zone.choose

    red_miss = missing["红球"]
    blue_miss = missing["蓝球"]

    # 遗漏值越高权重越大
    sorted_red = sorted(red_miss.items(), key=lambda x: x[1], reverse=True)
    sorted_blue = sorted(blue_miss.items(), key=lambda x: x[1], reverse=True)

    # 从遗漏值前 50% 的号码中抽取
    top_n_red = max(r_count * 3, len(sorted_red) // 2)
    top_red = [n for n, _ in sorted_red[:top_n_red]]
    top_blue = [n for n, _ in sorted_blue[:len(sorted_blue) // 2]]

    reds = sorted(random.sample(top_red, r_count))
    blues = sorted(random.sample(top_blue, b_count))

    reds, blues, valid = _dedup_and_record(reds, blues, existing, cfg)
    if not valid:
        return None

    return PredictSet(reds, blues, strategy="遗漏值策略")


def _strategy_balanced(
    data: LotteryData, cfg: dict, existing: set, freqs: dict, missing: dict
) -> Optional[PredictSet]:
    """
    策略三：区间均衡策略 — 按号码区间（1-11, 12-22, 23-33）均衡分配
    适用场景：相信开奖号码在各个区间均匀分布
    """
    schema = schema_from_cfg(cfg)
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_min, r_max = red_zone.range_tuple()
    b_min, b_max = blue_zone.range_tuple()
    r_count = red_zone.choose
    b_count = blue_zone.choose

    # 红球分 3 个区间（大乐透分 3 段）
    zones = [
        (r_min, r_min + (r_max - r_min) // 3),
        (r_min + (r_max - r_min) // 3 + 1, r_min + 2 * (r_max - r_min) // 3),
        (r_min + 2 * (r_max - r_min) // 3 + 1, r_max),
    ]
    # 每个区间至少分配 2 个号码
    per_zone = [r_count // len(zones)] * len(zones)
    remainder = r_count - sum(per_zone)
    for i in range(remainder):
        per_zone[i] += 1

    reds = []
    for i, (lo, hi) in enumerate(zones):
        pool = list(range(lo, hi + 1))
        needed = per_zone[i]
        if needed > len(pool):
            needed = len(pool)
        reds.extend(random.sample(pool, needed))

    # 蓝球全部号码中等概率随机
    blues = sorted(random.sample(range(b_min, b_max + 1), b_count))

    reds, blues, valid = _dedup_and_record(reds, blues, existing, cfg)
    if not valid:
        return None

    return PredictSet(reds, blues, strategy="区间均衡策略")


# ============================================================
# 增强置信度评分（4 维）
# ============================================================

def _compute_confidence_v2(
    reds: List[int],
    blues: List[int],
    red_freq: dict,
    blue_freq: dict,
    red_missing: dict,
    blue_missing: dict,
    total_records: int,
    historical_hit_rate: float = 0.0,
) -> Tuple[float, dict]:
    """
    增强版置信度评分（4 维）
    评分 = 冷热号 w1 + 遗漏偏差 w2 + 分布拟合度 w3 + 历史命中率 w4
    """
    w = PREDICT_DEFAULTS  # confidence_w1, w2, w3 from config

    # 1. 冷热号得分：频率越接近均值越高分
    if red_freq:
        all_vals = list(red_freq.values()) + list(blue_freq.values())
        avg_freq = sum(all_vals) / len(all_vals) if all_vals else 0
        max_freq = max(all_vals) if all_vals else 1
        hot_score = 0
        for n in reds:
            f = red_freq.get(n, 0)
            # 偏离均值的比率，越小越好
            deviation = abs(f - avg_freq) / max(max_freq - avg_freq, 1)
            hot_score += (1 - min(deviation, 1.0))
        for n in blues:
            f = blue_freq.get(n, 0)
            deviation = abs(f - avg_freq) / max(max_freq - avg_freq, 1)
            hot_score += (1 - min(deviation, 1.0))
        hot_score /= len(reds + blues)
    else:
        hot_score = 0.5

    # 2. 遗漏偏差得分：遗漏值接近理想值（均匀分布）得分高
    missing_score = 0
    for n in reds:
        mv = red_missing.get(n, 0)
        ideal = total_records / len(red_missing) if red_missing else 0
        if ideal > 0:
            deviation = min(abs(mv - ideal) / ideal, 1.0)
            missing_score += (1 - deviation)
        else:
            missing_score += 0.5
    for n in blues:
        mv = blue_missing.get(n, 0)
        ideal = total_records / len(blue_missing) if blue_missing else 0
        if ideal > 0:
            deviation = min(abs(mv - ideal) / ideal, 1.0)
            missing_score += (1 - deviation)
        else:
            missing_score += 0.5
    missing_score /= len(reds + blues)

    # 3. 区间分布拟合度
    if reds:
        zones = [(1, 11), (11, 22), (22, 34)]
        zone_counts = [sum(1 for r in reds if lo <= r < hi) for lo, hi in zones]
        ideal_per_zone = len(reds) / len(zones)
        spread_dev = sum(abs(c - ideal_per_zone) for c in zone_counts) / len(zones)
        spread_score = 1 - min(spread_dev / len(reds), 1.0)
    else:
        spread_score = 0.5

    # 4. 历史命中率维度（外部传入）
    hit_score = min(historical_hit_rate, 1.0)

    # 加权总分（w4 为历史命中率权重，硬编码为 0.15，从配置中拆分）
    w4 = 0.15
    w1 = w["confidence_w1"] * (1 - w4) / (w["confidence_w1"] + w["confidence_w2"] + w["confidence_w3"])
    w2 = w["confidence_w2"] * (1 - w4) / (w["confidence_w1"] + w["confidence_w2"] + w["confidence_w3"])
    w3 = w["confidence_w3"] * (1 - w4) / (w["confidence_w1"] + w["confidence_w2"] + w["confidence_w3"])

    total = w1 * hot_score + w2 * missing_score + w3 * spread_score + w4 * hit_score

    detail = {
        "冷热号得分": round(hot_score, 4),
        "遗漏偏差得分": round(missing_score, 4),
        "分布拟合度得分": round(spread_score, 4),
        "历史命中率得分": round(hit_score, 4),
        "权重": {"冷热号": round(w1, 3), "遗漏": round(w2, 3), "分布": round(w3, 3), "历史命中": w4},
    }
    return total, detail


# ============================================================
# 历史命中率计算
# ============================================================

def _compute_historical_hit_rate(data: LotteryData) -> float:
    """
    用历史数据计算近期（最近 50 期）各策略的平均命中率
    作为置信度评分的历史参考维度
    """
    cfg = LOTTERY_CONFIG[data.lottery_name]

    # 取最近 50 期作为回测窗口
    recent = data.records[:50]  # records[0] = 最新
    if len(recent) < 10:
        return 0.0

    # 简化：统计最近 50 期中热号（Top10）的出现比例
    freqs = frequency_analysis(data)
    sorted_red = sorted(freqs["红球"]["freq"].items(), key=lambda x: x[1], reverse=True)
    top10_red = {n for n, _ in sorted_red[:10]}

    hits = 0
    total = 0
    for record in recent:
        for n in record.红球:
            total += 1
            if n in top10_red:
                hits += 1

    return hits / max(total, 1)


# ============================================================
# 训练参数加载 + ML 模型预测
# ============================================================

def _get_trained_params(lottery_name: str) -> Optional[dict]:
    """加载最近一次训练的参数和模型

    红蓝拆分子模型（`*_blue_split`，train/blue_model.py 产物）优先于普通训练目录：
    它是「红蓝分开训练、最后组合」的成品；删除对应目录即回退旧模型。
    """
    if not TRAINING_DIR.exists():
        return None

    code = LOTTERY_CONFIG[lottery_name]["name_en"]
    records = sorted(
        [d for pat in (f"*_{code}", f"*_{lottery_name}") for d in TRAINING_DIR.glob(pat)],
        reverse=True,
    )
    # 排除 predict 后缀的目录
    records = [r for r in records if not r.name.endswith("_predict")]
    # 红蓝拆分子模型优先（同日多目录按时间戳取最新）
    split_dirs = sorted(TRAINING_DIR.glob(f"*_{code}_blue_split"), reverse=True)
    if split_dirs:
        records = split_dirs[:1] + records
    if not records:
        return None

    latest = records[0]
    params = {"record_dir": str(latest)}

    # 加载 config
    config_file = latest / "config.json"
    if config_file.exists():
        with open(config_file, encoding="utf-8") as f:
            params["config"] = json.load(f)

    # 加载 model_params
    model_file = latest / "model_params.json"
    if model_file.exists():
        with open(model_file, encoding="utf-8") as f:
            params["model_params"] = json.load(f)

    # 加载 ML 模型权重（pickle）
    # 注意：训练环境与运行环境 numpy 版本不一致时（如训练用 numpy2.x 序列化、
    # 运行时 numpy1.x 反序列化），pickle.load 会抛 ModuleNotFoundError('numpy._core')。
    # 这里捕获异常，降级为无 ML 模型（predict 会自动回退到统计/启发式策略），
    # 避免单次模型不兼容导致整个自动流水线崩溃。
    model_pkl = latest / "model.pkl"
    scaler_pkl = latest / "scaler.pkl"
    if model_pkl.exists():
        try:
            with open(model_pkl, "rb") as f:
                params["ml_model"] = pickle.load(f)
            if scaler_pkl.exists():
                with open(scaler_pkl, "rb") as f:
                    params["scaler"] = pickle.load(f)
        except Exception as e:
            logger.warning(f"加载 ML 模型失败（已跳过，回退统计策略）: {e}")
            params.pop("ml_model", None)
            params.pop("scaler", None)

    # 加载 statistical 参数包（无 ML 模型时使用）
    stat_pack_file = latest / "stat_pack.json"
    if stat_pack_file.exists():
        with open(stat_pack_file, encoding="utf-8") as f:
            params["stat_pack"] = json.load(f)

    return params


def _build_ml_features(
    data: LotteryData, window_size: int, feature_version: int = 1,
) -> "np.ndarray":
    """
    构建训练/预测共用的 ML 特征向量（1 × n_features）。
    `_predict_with_ml` 与红蓝拆分子模型（redblue_split_v1）共用此入口，
    保证「预测端特征 == 训练端特征」这一不变量只有一份实现。
    """
    from data.features import (
        build_feature_vector_v2,
        FEATURE_VERSION_RICH,
    )

    cfg = LOTTERY_CONFIG[data.lottery_name]
    schema = schema_from_cfg(cfg)
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_min, r_max = red_zone.range_tuple()
    b_min, b_max = blue_zone.range_tuple()

    # 用最近 window_size 期的数据构建特征
    recent = data.records[:window_size]
    if len(recent) < window_size:
        logger.warning(f"数据不足 window_size={window_size}，用全部 {len(recent)} 期")
        recent = data.records

    if feature_version == FEATURE_VERSION_RICH:
        # 丰富特征（动量/冷热斜率/共现），与训练端 build_features 版本 2 一致
        return np.array(build_feature_vector_v2(recent, cfg)).reshape(1, -1)

    # 版本 1：原内联特征（保持旧模型兼容）
    r_mid = (r_min + r_max) // 2
    r_third = (r_max - r_min + 1) // 3
    zone1_hi = r_min + r_third
    zone2_hi = r_min + 2 * r_third

    red_freq = {n: 0 for n in range(r_min, r_max + 1)}
    blue_freq = {n: 0 for n in range(b_min, b_max + 1)}
    for rec in recent:
        for n in rec.红球:
            red_freq[n] = red_freq.get(n, 0) + 1
        for n in rec.蓝球:
            blue_freq[n] = blue_freq.get(n, 0) + 1

    red_missing = {}
    for n in range(r_min, r_max + 1):
        gap = 0
        for rec in recent:
            if n in rec.红球:
                break
            gap += 1
        red_missing[n] = gap / window_size
    blue_missing = {}
    for n in range(b_min, b_max + 1):
        gap = 0
        for rec in recent:
            if n in rec.蓝球:
                break
            gap += 1
        blue_missing[n] = gap / window_size

    features = []
    for n in range(r_min, r_max + 1):
        features.append(red_freq[n] / window_size)
    for n in range(b_min, b_max + 1):
        features.append(blue_freq[n] / window_size)
    for n in range(r_min, r_max + 1):
        features.append(red_missing[n])
    for n in range(b_min, b_max + 1):
        features.append(blue_missing[n])

    odd_count = 0
    total_red_count = 0
    small_count = 0
    zone1_count = zone2_count = zone3_count = 0
    for rec in recent:
        for n in rec.红球:
            total_red_count += 1
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
    features.append(odd_count / max(total_red_count, 1))
    features.append(small_count / max(total_red_count, 1))
    features.append(zone1_count / max(total_red_count, 1))
    features.append(zone2_count / max(total_red_count, 1))
    features.append(zone3_count / max(total_red_count, 1))

    return np.array(features).reshape(1, -1)


def _predict_with_split_model(
    data: LotteryData, payload: dict, scaler, window_size: int,
    feature_version: int = 1,
) -> List[float]:
    """
    红蓝拆分子模型（format=redblue_split_v1）的组合推理：
    红球用逐号二分类出概率，蓝球用多分类头把类别概率摊回号码，
    最后拼成与旧格式相同的「红段+蓝段」扁平概率列表——下游采样组合逻辑零改动。
    """
    from train.blue_model import _mc_number_probs

    schema = schema_from_cfg(LOTTERY_CONFIG[data.lottery_name])
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_size, b_size = red_zone.size, blue_zone.size

    X = _build_ml_features(data, window_size, feature_version)
    if scaler is None:
        scaler = payload.get("red", {}).get("scaler")
    if scaler:
        X = scaler.transform(X)

    red_models = payload["red"]["models"]
    red_probs = np.zeros((1, r_size))
    for i, m in enumerate(red_models):
        if m is not None and i < r_size:
            red_probs[0, i] = m.predict_proba(X)[0, 1]

    heads = payload["blue"]["heads"]
    head_probs = [_mc_number_probs(m, X, b_size)[0] for m in heads]
    blue_probs = head_probs[0] if len(head_probs) == 1 else np.mean(head_probs, axis=0)

    return red_probs[0].tolist() + blue_probs.tolist()


def _predict_with_ml(
    data: LotteryData, ml_model, scaler, window_size: int,
    feature_version: int = 1,
) -> List[float]:
    """
    用训练好的 ML 模型（逻辑回归 / 随机森林）预测本期的号码概率
    返回所有号码（红+蓝）的出现概率列表

    支持两种模型格式：
    - list：逐号码二分类堆（旧格式）；
    - dict(format=redblue_split_v1)：红蓝拆分子模型（train/blue_model.py 产物），
      红蓝分别推理后组合，下游零改动。

    特征构建需与训练时一致：feature_version 决定标准/丰富特征。
    默认版本 1 走原内联特征（兼容旧模型）；版本 2 走统一特征模块的丰富特征。
    """
    if isinstance(ml_model, dict) and ml_model.get("format") == "redblue_split_v1":
        return _predict_with_split_model(data, ml_model, scaler, window_size, feature_version)

    X = _build_ml_features(data, window_size, feature_version)

    if scaler:
        X = scaler.transform(X)

    # 逐号码预测概率
    probabilities = []
    for i, model in enumerate(ml_model):
        if model is not None:
            prob = model.predict_proba(X)[0, 1]
        else:
            prob = 0.5
        probabilities.append(prob)

    return probabilities


def _sample_ml_group(
    red_probs: List[float],
    blue_probs: List[float],
    existing: set,
    cfg: dict,
    freqs: dict,
    missing: dict,
    data: "LotteryData",
    historical_hit_rate: float,
    model_type: str = "ML",
) -> Optional[PredictSet]:
    """
    用已算好的每号概率，按概率加权采样生成一组号码。
    供默认(fresh)预测里按反馈权重分配出来的「ML策略」组使用——
    共享一次 _predict_with_ml 的概率结果，避免每组重复推理。
    """
    schema = schema_from_cfg(cfg)
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_min, r_max = red_zone.range_tuple()
    b_min, b_max = blue_zone.range_tuple()
    r_count = red_zone.choose
    b_count = blue_zone.choose

    red_probs_arr = np.array(red_probs, dtype=float) + 0.01  # 避免零概率
    red_probs_arr = red_probs_arr / red_probs_arr.sum()
    blue_probs_arr = np.array(blue_probs, dtype=float) + 0.01
    blue_probs_arr = blue_probs_arr / blue_probs_arr.sum()

    try:
        red_indices = np.random.choice(
            list(range(r_min, r_max + 1)), size=r_count, replace=False, p=red_probs_arr
        )
        blue_indices = np.random.choice(
            list(range(b_min, b_max + 1)), size=b_count, replace=False, p=blue_probs_arr
        )
    except ValueError:
        return None

    reds = sorted(red_indices.tolist())
    blues = sorted(blue_indices.tolist())
    reds, blues, valid = _dedup_and_record(reds, blues, existing, cfg)
    if not valid:
        return None

    confidence, detail = _compute_confidence_v2(
        reds, blues,
        freqs["红球"]["freq"], freqs["蓝球"]["freq"],
        missing["红球"], missing["蓝球"],
        data.total_records, historical_hit_rate,
    )
    detail["策略模型"] = model_type
    ps = PredictSet(reds, blues, None, confidence, detail, strategy=ML_STRATEGY)
    return ps


# ============================================================
# 阶段2 泛型预测（数字型/乐透型，无红/蓝分区）
# 自包含实现，不改动下方红/蓝原逻辑，保证双色球/大乐透 零回归。
# ============================================================

def _g_key(zones: Dict[str, List[int]], schema) -> tuple:
    return tuple((z.name, tuple(sorted(zones.get(z.name, [])))) for z in schema.zones)


def _g_similar(key, existing, max_overlap) -> bool:
    """相似判定：**统计「完全相同的分区个数」**，不是「同位同号个数」。

    ⚠️ 语义说明（2026-09-01 查证）：key 的元素是 (区名, 该区号码元组)，
    因此 a == b 比较的是**整个分区**是否一致，而非逐位比较号码。
      - 数字型（每位一个分区、每区 1 号）→ 恰好等价于「同位同号数」；
      - 乐透型（红球区 + 蓝球区 = 2 个分区）same 上限 = 2，
        而 _g_max_overlap = max(1, int(7*0.6)) = 4 > 2 → **相似分支永不触发**。
    双色球/大乐透实际走 _dedup_and_record，不经过本函数，故无实际影响。
    """
    for ekey in existing:
        same = sum(1 for (zn, a), (ezn, b) in zip(key, ekey) if zn == ezn and a == b)
        if same >= max_overlap:
            return True
    return False


def _g_zone_random(z) -> List[int]:
    """按区规则重新随机采样该区号码（有序返回排序结果）。"""
    if z.repeatable:
        return sorted(random.randint(z.min, z.max) for _ in range(z.choose))
    return sorted(random.sample(range(z.min, z.max + 1), z.choose))


def _g_dedup(zones: Dict[str, List[int]], existing: set, schema, max_overlap: int) -> bool:
    """通用去重：完全重复、或同位同号数 >= max_overlap（相似）时，重新采样替换。

    ⚠️ 重试必须遍历**所有**分区：旧实现只改 zones[:len//2]（3 位彩种下等于只改第 1 位），
    冲突落在第 2/3 位时永远修不好，重试预算全部浪费——这是人海战术只出 37 组的原因之一。
    ⚠️ 两个分支（完全重复 / 相似）都必须检查 `_g_similar`：旧实现在完全重复分支里
    只判 `key not in existing` 就放行，会把相似号直接塞进 existing，破坏去重契约。
    """
    key = _g_key(zones, schema)
    if key not in existing and not _g_similar(key, existing, max_overlap):
        existing.add(key)
        return True
    for _ in range(_G_DEDUP_RETRY):
        for z in random.sample(schema.zones, len(schema.zones)):
            zones[z.name] = _g_zone_random(z)
            key = _g_key(zones, schema)
            if key not in existing and not _g_similar(key, existing, max_overlap):
                existing.add(key)
                return True
    return False


def _g_max_overlap(schema) -> int:
    """相似去重阈值：同位同号数 >= 该值视为相似，需替换。

    数字型阈值下限 = 2：3 位彩种按 0.6 比例算得 max(1, int(3*0.6)) = 1，
    即「任一位同号即相似」，会把 --groups 300 的人海出号压到 ~37 组（覆盖率 5%）。
    下限提到 2 后只拦「两两 >= 2 位相同」，3D/排列3 可出到 ~100 组。

    ⚠️ ~100 是数学上限不是 bug：要求任意两号最多 1 位相同（汉明距离 >= 2），
    由 Singleton 界 A_10(3,2) <= 10^(3-2+1) = 100，且可达（线性码 (a, b, -(a+b) mod 10)）。
    """
    raw = max(1, int(schema.total_choose * 0.6))
    if schema.is_sequence_type:
        return max(2, raw)
    return raw


# 数字型完全去重时的重采样轮数（每轮逐位重采一次，3 位彩种 = 20 轮 × 3 位 = 60 次机会）
_G_DEDUP_RETRY = 20


def _g_strategy_high_freq(data, cfg, schema, existing, freqs, missing) -> Optional["PredictSet"]:
    zones = {}
    for z in schema.zones:
        f = freqs[z.name]["freq"]
        top = [n for n, _ in sorted(f.items(), key=lambda x: x[1], reverse=True)[:z.choose * 5]]
        if len(top) < z.choose:
            top = list(range(z.min, z.max + 1))
        zones[z.name] = sorted(random.sample(top, z.choose))
    if _g_dedup(zones, existing, schema, _g_max_overlap(schema)):
        return PredictSet(zones=zones, strategy="高频策略")
    return None


def _g_strategy_missing(data, cfg, schema, existing, freqs, missing) -> Optional["PredictSet"]:
    zones = {}
    for z in schema.zones:
        m = missing[z.name]
        top = [n for n, _ in sorted(m.items(), key=lambda x: x[1], reverse=True)[:max(z.choose * 3, len(m) // 2)]]
        if len(top) < z.choose:
            top = list(range(z.min, z.max + 1))
        zones[z.name] = sorted(random.sample(top, z.choose))
    if _g_dedup(zones, existing, schema, _g_max_overlap(schema)):
        return PredictSet(zones=zones, strategy="遗漏值策略")
    return None


def _g_strategy_balanced(data, cfg, schema, existing, freqs, missing) -> Optional["PredictSet"]:
    zones = {}
    for z in schema.zones:
        if not z.ordered:
            # 乐透型：3 区间均衡
            lo, hi = z.range_tuple()
            third = (hi - lo + 1) // 3
            ranges = [(lo, lo + third), (lo + third + 1, lo + 2 * third), (lo + 2 * third + 1, hi)]
            per = [z.choose // 3] * 3
            for i in range(z.choose - sum(per)):
                per[i] += 1
            pick = []
            for (a, b), need in zip(ranges, per):
                pool = list(range(a, b + 1))
                pick += random.sample(pool, min(need, len(pool)))
            zones[z.name] = sorted(pick)
        else:
            # 数字型有序位：全范围随机
            zones[z.name] = sorted(random.sample(range(z.min, z.max + 1), z.choose))
    if _g_dedup(zones, existing, schema, _g_max_overlap(schema)):
        return PredictSet(zones=zones, strategy="区间均衡策略")
    return None


def _compute_confidence_generic(zones, freqs, missing, total_records, historical_hit_rate, schema):
    """泛型 4 维置信度（逐分区计算冷热/遗漏/分布）"""
    w = PREDICT_DEFAULTS
    all_vals = []
    for z in schema.zones:
        all_vals += list(freqs[z.name]["freq"].values())
    avg = sum(all_vals) / len(all_vals) if all_vals else 0
    maxf = max(all_vals) if all_vals else 1

    hot = 0
    tot = 0
    for z in schema.zones:
        for n in zones.get(z.name, []):
            f = freqs[z.name]["freq"].get(n, 0)
            dev = abs(f - avg) / max(maxf - avg, 1)
            hot += (1 - min(dev, 1.0))
            tot += 1
    hot_score = hot / max(tot, 1) if tot else 0.5

    miss = 0
    tot = 0
    for z in schema.zones:
        fdict = freqs[z.name]["freq"]
        ideal = total_records / len(fdict) if fdict else 0
        for n in zones.get(z.name, []):
            mv = missing[z.name].get(n, 0)
            if ideal > 0:
                dev = min(abs(mv - ideal) / ideal, 1.0)
                miss += (1 - dev)
            else:
                miss += 0.5
            tot += 1
    missing_score = miss / max(tot, 1)

    spread = 0
    tz = 0
    for z in schema.zones:
        lo, hi = z.range_tuple()
        third = (hi - lo + 1) // 3
        z1, z2 = lo + third, lo + 2 * third
        nums = zones.get(z.name, [])
        cnt = [0, 0, 0]
        for n in nums:
            if n <= z1:
                cnt[0] += 1
            elif n <= z2:
                cnt[1] += 1
            else:
                cnt[2] += 1
        ideal_p = len(nums) / 3 if nums else 0
        dev = sum(abs(c - ideal_p) for c in cnt) / max(len(nums), 1) if nums else 0
        spread += (1 - min(dev / max(len(nums), 1), 1.0)) if nums else 0.5
        tz += 1
    spread_score = spread / max(tz, 1)

    hit_score = min(historical_hit_rate, 1.0)
    w4 = 0.15
    denom = w["confidence_w1"] + w["confidence_w2"] + w["confidence_w3"]
    w1 = w["confidence_w1"] * (1 - w4) / denom
    w2 = w["confidence_w2"] * (1 - w4) / denom
    w3 = w["confidence_w3"] * (1 - w4) / denom
    total = w1 * hot_score + w2 * missing_score + w3 * spread_score + w4 * hit_score
    detail = {
        "冷热号得分": round(hot_score, 4),
        "遗漏偏差得分": round(missing_score, 4),
        "分布拟合度得分": round(spread_score, 4),
        "历史命中率得分": round(hit_score, 4),
        "权重": {"冷热号": round(w1, 3), "遗漏": round(w2, 3), "分布": round(w3, 3), "历史命中": w4},
    }
    return total, detail


def _g_historical_hit_rate(data, schema) -> float:
    recent = data.records[:50]
    if len(recent) < 10:
        return 0.0
    freqs = frequency_analysis(data)
    top_per_zone = {}
    for z in schema.zones:
        size = z.max - z.min + 1
        # 热号阈值 = 各区号码量的一半（数字型每区仅 0-9，取 5 个才有区分度；
        # 否则 top-全量会让命中率恒为 1.0，第4维置信度形同虚设）
        top_n = max(1, size // 2)
        top_per_zone[z.name] = {n for n, _ in sorted(freqs[z.name]["freq"].items(), key=lambda x: x[1], reverse=True)[:top_n]}
    hits = 0
    total = 0
    for rec in recent:
        for z in schema.zones:
            for n in rec.zone_numbers.get(z.name, []):
                total += 1
                if n in top_per_zone[z.name]:
                    hits += 1
    return hits / max(total, 1)


def _apply_prune(lottery_name: str, mode: str, predicted_sets: list):
    """低重叠剪枝（config.PRUNE_ENABLED）：裁掉互相高度相似的票 = **等效减注**。

    ⚠️ 诚实边界：不提升任何单注中奖概率（每注 p 是组合数常数），也不改变期望奖金
    （期望线性性），只改变注的分布形状、省掉重复覆盖的成本。见 ev/coverage 模块头。

    - `mode == "rule"`（用户显式规则）不干预；
    - 数字型（固定赔率、每期仅 5 注）在 ev/coverage 内部即跳过剪枝；
    - 失败一律降级返回原列表，绝不阻塞出号。

    返回 `(过滤后的 predicted_sets, KPI 或 None)`；调用方须随之更新 `"号码组数"`。
    """
    try:
        import config as _cfg
        if not getattr(_cfg, "PRUNE_ENABLED", False) or mode == "rule":
            return predicted_sets, None
        from ev.coverage import prune_predicted_sets
        kept, kpi = prune_predicted_sets(
            predicted_sets, lottery_name,
            nominal_n=len(predicted_sets),
            keep_per_strategy=getattr(_cfg, "PRUNE_MIN_PER_STRATEGY", 1),
            enabled=True,
        )
        if len(kept) == len(predicted_sets):
            return predicted_sets, kpi
        return [predicted_sets[i] for i in kept], kpi
    except Exception as e:  # pragma: no cover - 防御：剪枝永不阻塞出号
        logger.warning(f"低重叠剪枝失败({lottery_name}): {e}")
        return predicted_sets, None


def _predict_generic(lottery_name: str, data: LotteryData, cfg: dict, schema, groups: int, mode: str,
                     record_pending: bool = True) -> dict:
    """新彩种（数字型/乐透型）预测主入口：复用三策略 + 泛型置信度 + 去重。"""
    from prediction.optimizer import ML_STRATEGY, get_strategy_weights, distribute_strategies
    from data.feedback import record_pending_prediction, _next_issue
    from data.holiday import next_draw_date

    freqs = frequency_analysis(data)
    missing = missing_value_analysis(data)
    historical_hit_rate = _g_historical_hit_rate(data, schema)

    # trained 模式：数字型 statistical 训练的参数包（zone_freqs/zone_missing）覆盖实时统计，
    # 让训练模型真正参与加权采样与置信度；无参数包时退回实时统计（等价于 fresh）。
    use_trained_pack = False
    trained_window = None
    if mode == "trained":
        _trained = _get_trained_params(lottery_name)
        if _trained and _trained.get("stat_pack"):
            tf = _trained["stat_pack"]
            zf = tf.get("zone_freqs") or {}
            zm = tf.get("zone_missing") or {}
            if zf:
                # JSON 反序列化后键变字符串，需转回 int（与红/蓝 stat_pack 分支一致）
                zone_freqs = {z.name: {int(k): v for k, v in (zf.get(z.name, {}) or {}).items()} for z in schema.zones}
                zone_missing = {z.name: {int(k): v for k, v in (zm.get(z.name, {}) or {}).items()} for z in schema.zones}
                freqs = {z.name: {"freq": zone_freqs[z.name]} for z in schema.zones}
                missing = {z.name: zone_missing[z.name] for z in schema.zones}
                use_trained_pack = True
                trained_window = tf.get("best_window", 50)

    strat_fns = {
        "高频策略": lambda: _g_strategy_high_freq(data, cfg, schema, existing, freqs, missing),
        "遗漏值策略": lambda: _g_strategy_missing(data, cfg, schema, existing, freqs, missing),
        "区间均衡策略": lambda: _g_strategy_balanced(data, cfg, schema, existing, freqs, missing),
    }
    single_map = {"high_freq": "高频策略", "missing": "遗漏值策略", "balanced": "区间均衡策略"}

    existing = set()
    predicted_sets = []

    if mode in single_map:
        seq = [single_map[mode]] * groups
    else:
        try:
            weights = get_strategy_weights(lottery_name)
            seq = distribute_strategies(groups, weights)
        except Exception:
            seq = (["高频策略", "遗漏值策略", "区间均衡策略"] * ((groups // 3) + 1))[:groups]

    # 生成 + 唯一性去重（统一循环，替代原来的"主循环+轮转补齐"）。
    # ⚠️ 用独立 seen_ps 集合（PredictSet 已实现 __eq__/__hash__），**不碰 existing**——
    #    existing 是 _g_key 元组集合，契约归 _g_dedup/策略函数内部使用（相似去重）。
    #    seen_ps 负责跨策略的"完全重复"去重：3D/排列3 号码空间仅 1000，大 groups 下重复率高。
    # 重试预算 = max(groups*8, 1000)：号码空间耗尽时提前结束，避免死循环。
    seen_ps = set()
    max_attempts = max(groups * 8, 1000)
    attempts = 0
    g = 0
    while len(predicted_sets) < groups and attempts < max_attempts:
        attempts += 1
        sname = seq[g % len(seq)]
        g += 1
        if sname == ML_STRATEGY:
            sname = "高频策略"  # 新彩种暂无训练模型，降级为高频策略
        ps = strat_fns[sname]()
        if ps is None:
            continue
        if ps in seen_ps:
            continue  # 跨策略完全重复号码，跳过（唯一性）
        seen_ps.add(ps)
        conf, detail = _compute_confidence_generic(ps.zones, freqs, missing, data.total_records, historical_hit_rate, schema)
        ps.confidence = conf
        ps.score_detail = detail
        if use_trained_pack:
            ps.strategy = ML_STRATEGY
            ps.score_detail["策略模型"] = f"统计训练(W{trained_window})"
        else:
            ps.strategy = sname
        predicted_sets.append(ps)

    predicted_sets.sort(key=lambda x: x.confidence, reverse=True)

    # 低重叠剪枝：裁掉互相高度相似的票 = 等效减注（不改变单注中奖概率，见 ev/coverage）
    predicted_sets, _prune_kpi = _apply_prune(lottery_name, mode, predicted_sets)

    # 目标期号与预测日期统一口径（同源推算，杜绝 (26103, 9/8) 式错对）
    from data.feedback import predict_target
    target_issue, target_day = predict_target(lottery_name)

    result = {
        "lottery_name": lottery_name,
        "预测日期": str(target_day) if target_day else str(next_draw_date(lottery_name)),
        "预测模式": mode if mode in ("fresh", "rule", "high_freq", "missing", "balanced", "trained") else "fresh",
        "号码组数": len(predicted_sets),
        # 目标注数 = 本次请求的出号数量（生成时刻的动态配置快照）。体检用它与实际注数
        # 对比判断「生成端短缺」；不能拿体检时刻的实时配置对比——结算后动态滑轨会调值，
        # 会把正常的旧批次误报成「出号 N 注（期望 M）」。
        "目标注数": groups,
        "预测号码": [ps.to_dict() for ps in predicted_sets],
        "历史命中率(近50期)": round(historical_hit_rate, 4),
        "生成时间": datetime.now().isoformat(),
        "目标期号": target_issue or _next_issue(lottery_name),
    }
    if use_trained_pack:
        result["训练参考"] = {"模型类型": "statistical", "最优窗口": trained_window}
    if _prune_kpi is not None:
        result["剪枝统计"] = _prune_kpi
    if record_pending:
        try:
            record_pending_prediction(lottery_name, result)
        except Exception as e:
            logger.warning(f"记录 pending 预测失败: {e}")
    return result


# ============================================================
# 无副作用窗口候选（robust_tiers / credibility.robustness 复用）
# ============================================================

class _DataSlice:
    """轻量数据切片视图：只暴露 frequency_analysis / missing_value_analysis /
    _compute_historical_hit_rate 需要的属性（lottery_name / records / total_records）。"""

    def __init__(self, lottery_name: str, records: list):
        self.lottery_name = lottery_name
        self.records = records

    @property
    def total_records(self) -> int:
        return len(self.records)


def _window_candidates(lottery_name: str, records: list, k: int, seed: int) -> List[PredictSet]:
    """给定历史切片（records[0] = 最新），无副作用地产出 top-k 候选组。

    与 predict() 的区别（零回归约束）：
    - 不写 pending、不落 feedback、不读训练参数包（纯统计 + 策略层）
    - 通过 seed 固定 random / np.random，同 (records, seed) 输入输出完全可复现
    红/蓝彩种走 _strategy_* + _compute_confidence_v2；
    数字型/乐透型（无红蓝分区）走 _g_strategy_* + _compute_confidence_generic。
    返回按置信度降序的 PredictSet 列表（长度 ≤ k）。
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)
    data = _DataSlice(lottery_name, list(records))
    random.seed(seed)
    np.random.seed(seed % (2 ** 32))

    freqs = frequency_analysis(data)
    missing = missing_value_analysis(data)
    is_redblue = bool(schema.red_zone and schema.blue_zone)
    existing: set = set()

    if is_redblue:
        hist_rate = _compute_historical_hit_rate(data)

        def _conf(ps: PredictSet):
            return _compute_confidence_v2(
                ps.reds, ps.blues,
                freqs["红球"]["freq"], freqs["蓝球"]["freq"],
                missing["红球"], missing["蓝球"],
                data.total_records, hist_rate,
            )

        strat_fns = [
            ("高频策略", lambda: _strategy_high_freq(data, cfg, existing, freqs, missing)),
            ("遗漏值策略", lambda: _strategy_missing_value(data, cfg, existing, freqs, missing)),
            ("区间均衡策略", lambda: _strategy_balanced(data, cfg, existing, freqs, missing)),
        ]
    else:
        hist_rate = _g_historical_hit_rate(data, schema)

        def _conf(ps: PredictSet):
            return _compute_confidence_generic(
                ps.zones, freqs, missing, data.total_records, hist_rate, schema
            )

        strat_fns = [
            ("高频策略", lambda: _g_strategy_high_freq(data, cfg, schema, existing, freqs, missing)),
            ("遗漏值策略", lambda: _g_strategy_missing(data, cfg, schema, existing, freqs, missing)),
            ("区间均衡策略", lambda: _g_strategy_balanced(data, cfg, schema, existing, freqs, missing)),
        ]

    seen_ps = set()
    out: List[PredictSet] = []
    attempts = 0
    max_attempts = max(k * 8, 600)
    i = 0
    while len(out) < k and attempts < max_attempts:
        attempts += 1
        name, fn = strat_fns[i % len(strat_fns)]
        i += 1
        ps = fn()
        if ps is None or ps in seen_ps:
            continue
        seen_ps.add(ps)
        conf, detail = _conf(ps)
        ps.confidence = conf
        ps.score_detail = detail
        ps.strategy = name
        out.append(ps)

    out.sort(key=lambda x: x.confidence, reverse=True)
    return out


def _score_candidates(lottery_name: str, records: list, zones_list: List[dict]) -> List[float]:
    """用给定历史切片的统计量，对一组**已有票面**重算置信度（无副作用）。

    用途：robust_tiers 的跨窗口一致性重打分——同一票面在每个窗口下
    分别打分，比较窗口内排名，衡量「换个窗口它还排不排得上号」。
    zones_list 元素形如 {"红球":[...], "蓝球":[...]} 或 {"第1位":[x], ...}。
    返回与 zones_list 对齐的置信度列表。
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)
    data = _DataSlice(lottery_name, list(records))
    freqs = frequency_analysis(data)
    missing = missing_value_analysis(data)
    if schema.red_zone and schema.blue_zone:
        hist_rate = _compute_historical_hit_rate(data)

        def _conf(zones):
            return _compute_confidence_v2(
                zones.get("红球", []), zones.get("蓝球", []),
                freqs["红球"]["freq"], freqs["蓝球"]["freq"],
                missing["红球"], missing["蓝球"],
                data.total_records, hist_rate,
            )[0]
    else:
        hist_rate = _g_historical_hit_rate(data, schema)

        def _conf(zones):
            return _compute_confidence_generic(
                zones, freqs, missing, data.total_records, hist_rate, schema
            )[0]

    return [float(_conf(z)) for z in zones_list]


# ============================================================
# 主入口
# ============================================================

def predict(
    lottery_name: str,
    groups: int = None,
    mode: str = "fresh",
    record_pending: bool = True,
) -> dict:
    """
    预测主入口

    参数：
    - groups: 输出号码组数（1/5/10）
    - mode: fresh（即时统计）/ high_freq（高频策略）/ missing（遗漏值策略）/
            balanced（区间均衡策略）/ trained（加载 ML 模型）/ rule（规则优选）
    - record_pending: 是否把结果写入 pending（反馈闭环）。默认 True；
      冒烟脚本/临时试算/被测试间接调用时传 False，避免污染生产 pending。
    """
    if groups is None:
        groups = PREDICT_DEFAULTS["groups"]
    if groups > PREDICT_DEFAULTS["max_groups"]:
        groups = PREDICT_DEFAULTS["max_groups"]

    logger.info(f"预测: {lottery_name}, groups={groups}, mode={mode}")

    # 哨兵变量（替代 in dir() 脆弱判断）
    trained = None
    model_config = None
    _feedback_info = None

    data = load_lottery(lottery_name)
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)
    # 阶段2：非红/蓝彩种（数字型/乐透型）走泛型预测；红/蓝彩种走下方原逻辑（零回归）
    # ★ record_pending 必须透传（2026-09-20 修）：此前遗漏 → record_pending=False 的
    #   验证/诊断出号也会写生产 pending（rule 分支同类 bug 的泛型版）。
    if not (schema.red_zone and schema.blue_zone):
        return _predict_generic(lottery_name, data, cfg, schema, groups, mode,
                                record_pending=record_pending)
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_min, r_max = red_zone.range_tuple()
    b_min, b_max = blue_zone.range_tuple()
    r_count = red_zone.choose
    b_count = blue_zone.choose

    # ===== 规则优选模式（策略包3）：严格按用户规则生成号码 =====
    if mode == "rule":
        from prediction.rule_strategy import generate_rule_groups

        logger.info(f"规则优选策略: {lottery_name}, groups={groups}")
        rule_result = generate_rule_groups(lottery_name, data, groups=groups)
        if "error" in rule_result:
            logger.error(f"规则优选失败: {rule_result['error']}")
            return {
                "lottery_name": lottery_name,
                "预测日期": str(next_draw_date(lottery_name)),
                "预测模式": "rule",
                "号码组数": 0,
                "预测号码": [],
                "错误": rule_result["error"],
                "生成时间": datetime.now().isoformat(),
            }

        predicted_date = next_draw_date(lottery_name)
        from data.feedback import predict_target
        rule_issue, rule_day = predict_target(lottery_name)
        if rule_day:
            predicted_date = rule_day
        result = {
            "lottery_name": lottery_name,
            "预测日期": str(predicted_date),
            "预测模式": "rule",
            "号码组数": len(rule_result["号码组"]),
            "预测号码": rule_result["号码组"],
            "历史命中率(近50期)": round(_compute_historical_hit_rate(data), 4),
            "规则分档": rule_result.get("分档", {}),
            "生成时间": datetime.now().isoformat(),
            "目标期号": rule_issue,
        }
        # 记录 pending 预测（等待开奖后对比）；record_pending=False 时跳过
        # （冒烟/试算防污染生产 pending——2026-09-14 实锤：rule 分支曾无视该开关，
        #  一次 groups=5 的验证出号把生产 pending 同期记录整个覆盖）
        if record_pending:
            try:
                from data.feedback import record_pending_prediction
                record_pending_prediction(lottery_name, result)
            except Exception as e:
                logger.warning(f"记录 pending 预测失败: {e}")
        return result

    # 基础统计量
    freqs = frequency_analysis(data)
    missing = missing_value_analysis(data)
    historical_hit_rate = _compute_historical_hit_rate(data)

    # 三种生成策略
    strategies = [_strategy_high_freq, _strategy_missing_value, _strategy_balanced]

    existing = set()
    predicted_sets = []
    strategy_names = ["高频策略", "遗漏值策略", "区间均衡策略"]

    if mode == "trained":
        # trained 模式：优先加载 ML 模型，其次加载 statistical 参数包
        trained = _get_trained_params(lottery_name)
        if trained and trained.get("ml_model"):
            ml_model = trained["ml_model"]
            scaler = trained.get("scaler")
            model_config = trained.get("config", {})
            window_size = model_config.get("window", model_config.get("window_list", [50])[0]) if model_config else 50
            model_type = model_config.get("model_type", "statistical") if model_config else "statistical"
            feature_version = model_config.get("feature_version", 1) if model_config else 1

            # 用 ML 模型预测各号码概率（特征版本需与训练时一致）
            probs = _predict_with_ml(data, ml_model, scaler, window_size, feature_version=feature_version)

            # 按概率排序，生成多组号码（加入随机扰动）
            n_total = (r_max - r_min + 1) + (b_max - b_min + 1)
            red_probs = probs[:r_max - r_min + 1]
            blue_probs = probs[r_max - r_min + 1:]

            for g in range(groups):
                # 按 ML 概率加权随机抽样
                red_probs_arr = np.array(red_probs) + 0.01  # 避免零概率
                red_probs_arr = red_probs_arr / red_probs_arr.sum()

                # 首次采样确保不是纯随机
                red_indices = np.random.choice(
                    list(range(r_min, r_max + 1)),
                    size=r_count,
                    replace=False,
                    p=red_probs_arr,
                )
                blue_indices = np.random.choice(
                    list(range(b_min, b_max + 1)),
                    size=b_count,
                    replace=False,
                    p=np.array(blue_probs) / sum(blue_probs),
                )

                reds = sorted(red_indices.tolist())
                blues = sorted(blue_indices.tolist())

                reds, blues, valid = _dedup_and_record(reds, blues, existing, cfg)
                if not valid:
                    continue

                confidence, detail = _compute_confidence_v2(
                    reds, blues,
                    freqs["红球"]["freq"], freqs["蓝球"]["freq"],
                    missing["红球"], missing["蓝球"],
                    data.total_records, historical_hit_rate,
                )
                ps = PredictSet(reds, blues, None, confidence, detail, strategy=ML_STRATEGY)
                ps.score_detail["策略模型"] = model_type
                predicted_sets.append(ps)
        elif trained and trained.get("stat_pack"):
            # statistical trained 模式：用保存的频率表做加权采样
            pack = trained["stat_pack"]
            saved_red_freq = {int(k): v for k, v in pack["freqs"].items()}
            saved_blue_freq = {int(k): v for k, v in pack["blue_freqs"].items()}
            saved_red_missing = {int(k): v for k, v in pack["missing"].items()}
            saved_blue_missing = {int(k): v for k, v in pack["blue_missing"].items()}
            saved_window = pack.get("best_window", 50)

            logger.info(f"加载 statistical 参数包: window={saved_window}, loss={pack.get('best_loss', 'N/A')}")

            # 用保存的频率做加权随机采样生成号码
            for g in range(groups):
                # 红球加权采样
                red_numbers = list(saved_red_freq.keys())
                red_weights = np.array([saved_red_freq[n] for n in red_numbers], dtype=float)
                red_weights = red_weights / red_weights.sum()
                reds = sorted(np.random.choice(
                    red_numbers, size=r_count, replace=False, p=red_weights
                ).tolist())

                # 蓝球加权采样
                blue_numbers = list(saved_blue_freq.keys())
                blue_weights = np.array([saved_blue_freq[n] for n in blue_numbers], dtype=float)
                blue_weights = blue_weights / blue_weights.sum()
                blues = sorted(np.random.choice(
                    blue_numbers, size=b_count, replace=False, p=blue_weights
                ).tolist())

                reds, blues, valid = _dedup_and_record(reds, blues, existing, cfg)
                if not valid:
                    continue

                confidence, detail = _compute_confidence_v2(
                    reds, blues,
                    saved_red_freq, saved_blue_freq,
                    saved_red_missing, saved_blue_missing,
                    data.total_records, historical_hit_rate,
                )
                ps = PredictSet(reds, blues, None, confidence, detail, strategy=ML_STRATEGY)
                ps.score_detail["策略模型"] = f"统计训练(W{saved_window})"
                predicted_sets.append(ps)
        else:
            logger.warning("未找到任何训练记录，降级为 fresh 模式")
            # 降级到 fresh 也用多策略

    if not predicted_sets:
        # fresh 模式 + trained 降级 + 单策略模式：使用反馈优化权重选择策略（阶段3）
        from prediction.optimizer import (
            get_strategy_weights,
            distribute_strategies,
            get_strategy_function,
            get_feedback_info,
            weighted_strategy_choice,
        )

        random.seed(datetime.now().microsecond)

        # 单策略模式：强制所有组使用同一策略
        single_strategy_map = {
            "high_freq": "高频策略",
            "missing": "遗漏值策略",
            "balanced": "区间均衡策略",
        }
        if mode in single_strategy_map:
            strategy_sequence = [single_strategy_map[mode]] * groups
        else:
            weights = get_strategy_weights(lottery_name)
            strategy_sequence = distribute_strategies(groups, weights)

        # 若权重分配里出现了「ML策略」：预加载一次训练模型概率，供按权重分配的 ML 组共用
        ml_assignments = [i for i, s in enumerate(strategy_sequence) if s == ML_STRATEGY]
        ml_probs = None
        ml_model_type = None
        if ml_assignments:
            _trained = _get_trained_params(lottery_name)
            if _trained and _trained.get("ml_model"):
                _ml_model = _trained["ml_model"]
                _scaler = _trained.get("scaler")
                _mconfig = _trained.get("config", {})
                _w = _mconfig.get("window", _mconfig.get("window_list", [50])[0]) if _mconfig else 50
                _fv = _mconfig.get("feature_version", 1) if _mconfig else 1
                ml_probs = _predict_with_ml(data, _ml_model, _scaler, _w, feature_version=_fv)
                ml_model_type = _mconfig.get("model_type", "ML")
            else:
                # 无可用训练模型：把 ML 分配改写为反馈加权下的启发式，避免静默退化
                for i in ml_assignments:
                    strategy_sequence[i] = weighted_strategy_choice(weights, exclude=[ML_STRATEGY])

        # 策略名 → 函数映射
        strategy_fn_map = {
            "高频策略": _strategy_high_freq,
            "遗漏值策略": _strategy_missing_value,
            "区间均衡策略": _strategy_balanced,
        }

        for g in range(groups):
            strategy_name = strategy_sequence[g % len(strategy_sequence)]
            if strategy_name == ML_STRATEGY and ml_probs is not None:
                # 用训练模型按概率采样生成这一组
                ps = _sample_ml_group(
                    ml_probs[: r_max - r_min + 1], ml_probs[r_max - r_min + 1:],
                    existing, cfg, freqs, missing, data, historical_hit_rate,
                    model_type=ml_model_type or "ML",
                )
                if ps is None:
                    # 退化兜底：用高频策略补一组
                    ps = _strategy_high_freq(data, cfg, existing, freqs, missing)
                    if ps is None:
                        continue
                    ps.strategy = "高频策略"
                    ps.score_detail["策略模型"] = "退化兜底"
                predicted_sets.append(ps)
                continue
            strategy_fn = strategy_fn_map.get(strategy_name, _strategy_high_freq)
            ps = strategy_fn(data, cfg, existing, freqs, missing)
            if ps is None:
                continue

            confidence, detail = _compute_confidence_v2(
                ps.reds, ps.blues,
                freqs["红球"]["freq"], freqs["蓝球"]["freq"],
                missing["红球"], missing["蓝球"],
                data.total_records, historical_hit_rate,
            )
            ps.confidence = confidence
            ps.score_detail = detail
            ps.strategy = strategy_name
            predicted_sets.append(ps)

        # 记录反馈信息（供结果展示）
        _feedback_info = get_feedback_info(lottery_name)

    # 轮转补齐：如果某些策略没产出，用其他策略补
    round_robin_idx = 0
    while len(predicted_sets) < groups:
        strategy_fn = strategies[round_robin_idx % len(strategies)]
        ps = strategy_fn(data, cfg, existing, freqs, missing)
        if ps:
            confidence, detail = _compute_confidence_v2(
                ps.reds, ps.blues,
                freqs["红球"]["freq"], freqs["蓝球"]["freq"],
                missing["红球"], missing["蓝球"],
                data.total_records, historical_hit_rate,
            )
            ps.confidence = confidence
            ps.score_detail = detail
            ps.strategy = strategy_names[round_robin_idx % len(strategies)]
            predicted_sets.append(ps)
        round_robin_idx += 1
        if round_robin_idx > groups * 3:
            break

    # 按置信度排序
    predicted_sets.sort(key=lambda x: x.confidence, reverse=True)

    # 低重叠剪枝：裁掉互相高度相似的票 = 等效减注（不改变单注中奖概率，见 ev/coverage）
    predicted_sets, _prune_kpi = _apply_prune(lottery_name, mode, predicted_sets)

    # 预测日期（与目标期号同源推算，统一口径）
    from data.feedback import predict_target
    _tgt_issue, predicted_date = predict_target(lottery_name)
    if predicted_date is None:
        predicted_date = next_draw_date(lottery_name)

    # 预测模式标签：单策略模式保持原值，便于前端/战绩识别具体策略
    mode_label = mode if mode in ("fresh", "trained", "rule", "high_freq", "missing", "balanced") else "trained"

    result = {
        "lottery_name": lottery_name,
        "预测日期": str(predicted_date),
        "预测模式": mode_label,
        "号码组数": len(predicted_sets),
        "预测号码": [ps.to_dict() for ps in predicted_sets],
        "历史命中率(近50期)": round(historical_hit_rate, 4),
        "生成时间": datetime.now().isoformat(),
    }
    if _prune_kpi is not None:
        result["剪枝统计"] = _prune_kpi

    # 预填目标期号，供落库与闭环评估使用（record_pending_prediction 会复用此值）
    result["目标期号"] = _tgt_issue

    # trained 模式下附加 ML 参考信息
    if mode == "trained" and trained and trained.get("ml_model"):
        result["训练参考"] = {
            "来源": trained.get("record_dir", ""),
            "模型类型": (model_config or {}).get("model_type", "unknown"),
            "最优窗口": (model_config or {}).get("window", "N/A"),
        }

    # 附加反馈优化信息（阶段3）
    if _feedback_info is not None:
        result["反馈优化"] = {
            "是否启用": _feedback_info["has_feedback"],
            "反馈记录数": _feedback_info["feedback_count"],
            "待开奖预测数": _feedback_info["pending_count"],
            "策略权重": _feedback_info["weights"],
        }

    # 记录 pending 预测（等待开奖后对比）
    if record_pending:
        try:
            from data.feedback import record_pending_prediction
            record_pending_prediction(lottery_name, result)
        except Exception as e:
            logger.warning(f"记录 pending 预测失败: {e}")

    return result

# 注意：预测结果保存请使用 prediction/reporter.py 的 save_prediction_record()
