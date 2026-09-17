"""
策略优化器（反馈闭环 - 阶段3）

基于反馈历史动态调整各策略权重，让 predict() 使用反馈优化的权重
而不是固定轮转。

核心逻辑：
- 加载 feedback 模块维护的策略权重
- 按权重加权随机选择策略（表现好的策略被选中概率更高）
- 无反馈数据时回退到均分权重（与旧行为一致）
"""

import logging
import random
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# 三种策略名称（与 prediction/engine.py 保持一致）
STRATEGY_NAMES = ["高频策略", "遗漏值策略", "区间均衡策略"]

# 第四种「策略」：训练好的 ML/统计模型。
# 它不是独立函数，而是按反馈权重分配组数后，由 predict() 用训练模型按概率采样生成。
# 只有反馈评估里 ML 家族产生过有效命中、且存在可用训练模型时，权重才会 > 0。
ML_STRATEGY = "ML策略"


def get_strategy_weights(lottery_name: str) -> Dict[str, float]:
    """
    获取指定彩票的策略权重（仅基础可执行策略）

    优先从反馈历史加载（阶段2维护），无反馈时返回均分默认值。
    注意：权重只统计 STRATEGY_NAMES（fresh 分发只能执行这 3 个策略）。
    其他策略名（统计训练/ML/规则优选）的反馈保留在历史中供战绩榜展示，
    但不参与权重分发，避免"无法执行的策略"被选中后静默退化。
    """
    try:
        from data.feedback import (
            load_strategy_weights,
            load_feedback_history,
            normalize_weights,
        )
        history = load_feedback_history(lottery_name)
        if history:
            # 有反馈数据，使用动态权重（基础策略 + ML 策略，缺失补 0 再归一化）
            # 用 normalize_weights 兼容旧权重文件里的 ML(logistic)/统计训练(Wxx) 键
            raw = normalize_weights(load_strategy_weights(lottery_name))
            candidates = {s: raw.get(s, 0.0) for s in STRATEGY_NAMES}
            ml_w = raw.get(ML_STRATEGY, 0.0)
            if ml_w > 0:
                candidates[ML_STRATEGY] = ml_w
            total = sum(candidates.values())
            if total > 0:
                weights = {s: v / total for s, v in candidates.items()}
            else:
                n = len(candidates)
                weights = {s: 1.0 / n for s in candidates}
            logger.info(f"使用反馈优化权重: {lottery_name}, weights={weights}")
            return weights
    except Exception as e:
        logger.warning(f"加载策略权重失败，使用默认均分: {e}")

    # 无反馈数据或加载失败，均分（不含 ML，因为还没有反馈积累权重）
    return {s: 1.0 / len(STRATEGY_NAMES) for s in STRATEGY_NAMES}


def weighted_strategy_choice(
    weights: Dict[str, float],
    exclude: Optional[List[str]] = None,
) -> str:
    """
    按权重加权随机选择一个策略

    参数：
    - weights: 策略名 → 权重
    - exclude: 需要排除的策略（已用过的）

    返回：选中的策略名
    """
    if exclude is None:
        exclude = []

    # 只从可执行策略中选（基础策略 + ML 策略；防止权重文件里出现非已知策略名）
    base = {k: v for k, v in weights.items() if k in STRATEGY_NAMES or k == ML_STRATEGY}
    if base:
        weights = base
    elif weights:
        # 权重里完全没有已知策略（异常情况），回退均分
        weights = {s: 1.0 / len(STRATEGY_NAMES) for s in STRATEGY_NAMES}
    else:
        weights = {s: 1.0 / len(STRATEGY_NAMES) for s in STRATEGY_NAMES}

    available = {k: v for k, v in weights.items() if k not in exclude}
    if not available:
        # 全部被排除，从全集中选
        available = dict(weights)

    names = list(available.keys())
    probs = list(available.values())
    total = sum(probs)
    if total <= 0:
        # 权重全为 0，均分
        probs = [1.0] * len(names)
        total = sum(probs)
    probs = [p / total for p in probs]

    return random.choices(names, weights=probs, k=1)[0]


def distribute_strategies(
    groups: int,
    weights: Dict[str, float],
) -> List[str]:
    """
    为 groups 组号码分配策略，尽量保证多样性 + 权重倾向

    规则：
    1. 先保证每个策略至少出现一次（如果 groups >= 3）
    2. 剩余的组按权重加权随机选择
    3. 避免连续相同策略

    返回：长度为 groups 的策略名列表
    """
    result = []

    # 候选集 = 基础策略 + 权重 > 0 的 ML 策略
    base = list(STRATEGY_NAMES)
    if weights.get(ML_STRATEGY, 0) > 0:
        base.append(ML_STRATEGY)

    if groups >= len(base):
        # 先各放一个（按权重降序）
        ordered = sorted(base, key=lambda s: weights.get(s, 0), reverse=True)
        result.extend(ordered)
        # 剩余的按权重加权选择，避免与上一个相同
        for _ in range(groups - len(STRATEGY_NAMES)):
            last = result[-1] if result else None
            chosen = weighted_strategy_choice(weights, exclude=[last] if last else None)
            result.append(chosen)
    else:
        # groups < 3，直接按权重选
        for _ in range(groups):
            last = result[-1] if result else None
            chosen = weighted_strategy_choice(weights, exclude=[last] if last else None)
            result.append(chosen)

    return result


def get_strategy_function(strategy_name: str):
    """根据策略名返回对应的策略函数（从 prediction/engine.py 导入）"""
    from prediction.engine import (
        _strategy_high_freq,
        _strategy_missing_value,
        _strategy_balanced,
    )
    mapping = {
        "高频策略": _strategy_high_freq,
        "遗漏值策略": _strategy_missing_value,
        "区间均衡策略": _strategy_balanced,
    }
    return mapping.get(strategy_name)


def get_feedback_info(lottery_name: str) -> dict:
    """
    获取反馈信息摘要（供 predict 结果附加）

    返回：
    - weights: 当前策略权重
    - has_feedback: 是否有反馈数据
    - feedback_count: 反馈记录数
    """
    try:
        from data.feedback import (
            load_strategy_weights,
            load_feedback_history,
            load_pending,
            normalize_weights,
        )
        history = load_feedback_history(lottery_name)
        weights = normalize_weights(load_strategy_weights(lottery_name))
        pending = load_pending(lottery_name)
        return {
            "weights": weights,
            "has_feedback": len(history) > 0,
            "feedback_count": len(history),
            "pending_count": len(pending),
        }
    except Exception as e:
        logger.warning(f"获取反馈信息失败: {e}")
        return {
            "weights": {s: 1.0 / len(STRATEGY_NAMES) for s in STRATEGY_NAMES},
            "has_feedback": False,
            "feedback_count": 0,
            "pending_count": 0,
        }
