"""
三档号码选择管线（三种**独立生成流程**，非同一批号码过滤）

档位口径（用户已确认）：
- general 一般档  = 现有 engine.predict 流程原样（零改动语义）
- robust  稳健档  = 多窗口 × 多种子构建候选池，跨窗口重打分求「共识」的集成号
                    （避雷：规避只在个别窗口下才排得上号的低鲁棒性组合）
- high    高鲁棒档 = 仅保留跨窗口一致的共识号（≥70% 窗口内排名进前 25%），最严格；
                    数量不足时放宽到 60% 并标注 coverage_relaxed，宁少勿凑

共识算法（防「生成计数被策略随机采样淹没」）：
1. 池构建：对每个 (窗口 w × 种子 s) 组合跑三策略生成 top_c 候选，并入池（保证多样性）
2. 跨窗口重打分：对池中**每个票面**在每个窗口下重算置信度（_score_candidates），
   按窗口内排名判定该窗口「命中」（top 50% = 半区命中；top 25% = 前列命中）
3. 稳健档共识分 = 半区命中率 × 跨窗口平均置信度
   高鲁棒档门槛 = 前列命中率 ≥ 70%（不足放宽 60%）

诚实口径（硬约束，文案禁止违反）：
鲁棒性/共识分度量的是「选号流程在多窗口与参数扰动下的稳定性与过拟合风险」，
**不代表号码中奖概率更高**。号码独立随机、单注 EV 恒负，
本模块产出的是流程稳定性元指标，不构成「预测有信号」类声明
（与 credibility 裁判层「假设全 rejected」结论不冲突，度量对象不同）。

三档全部写入 pending 参与开奖后反馈对比（记录带「档位」键）；
策略权重学习仅由一般档驱动（见 data/feedback.update_strategy_weights）。
"""

import logging
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from config import LOTTERY_CONFIG
from data.loader import load_lottery
from data.schema import schema_from_cfg
from prediction.engine import PredictSet, _window_candidates, _score_candidates

logger = logging.getLogger(__name__)

# 档位中文标签（进前端/反馈/pending）
TIER_LABELS = {"general": "一般", "robust": "稳健", "high": "高鲁棒"}

# 高鲁棒档窗口一致性硬门槛（≥70% 窗口排名进前 25%）；不足时放宽到 60%
HIGH_TIER_THRESHOLD = 0.70
HIGH_TIER_RELAXED = 0.60

# 诚实口径说明（结果必带）
HONEST_NOTE = (
    "鲁棒性/共识分度量选号流程在多窗口/参数扰动下的稳定性与过拟合风险，"
    "不代表号码中奖概率。"
)


def _zone_key(ps) -> tuple:
    """票面唯一键：((区名, 排序后号码元组), ...)。兼容 PredictSet 与票面 dict。"""
    zones = ps.zones if isinstance(ps, PredictSet) else ps
    return tuple(
        (name, tuple(sorted(int(n) for n in nums)))
        for name, nums in sorted(zones.items())
    )


def _build_pool(
    lottery_name: str,
    windows: Tuple[int, ...],
    seeds: int,
    seed_base: int,
    top_c: int,
) -> Tuple[List[dict], int, List[int]]:
    """
    跑全部 (窗口 × 种子) 组合生成候选，合并去重为池。

    返回 (pool_zones: 去重后的票面 zones 列表, total_combos, usable_windows)。
    数据不足的窗口自动跳过（历史 < 窗口长度即跳过）。
    """
    data = load_lottery(lottery_name)
    records = data.records  # records[0] = 最新
    n = len(records)
    usable = [w for w in windows if 10 < w <= n]
    if not usable:
        raise ValueError(
            f"{lottery_name} 历史不足（{n} 期），无法做窗口共识（最长窗口 {max(windows)}）"
        )

    seen = {}
    total_combos = 0
    for w in usable:
        for s in range(seeds):
            seed = seed_base + w * 1000 + s
            cands = _window_candidates(lottery_name, records[:w], top_c, seed)
            total_combos += 1
            for ps in cands:
                key = _zone_key(ps)
                if key not in seen:
                    seen[key] = {name: list(nums) for name, nums in ps.zones.items()}
    pool = list(seen.values())
    return pool, total_combos, usable


def _rescore_pool(
    lottery_name: str,
    pool_zones: List[dict],
    usable_windows: List[int],
) -> Tuple[Dict[tuple, dict], int]:
    """
    跨窗口重打分：池中每个票面在每个窗口下重算置信度并排名。

    返回 stats: {zone_key: {"per_window": {w: conf}, "mean_conf", "half_hits", "quarter_hits"}}，
          n_windows
    half_hits / quarter_hits = 该票面在多少个窗口内排名进前 50% / 前 25%。
    """
    data = load_lottery(lottery_name)
    records = data.records
    n_windows = len(usable_windows)

    keys = [_zone_key(z) for z in pool_zones]
    stats = {
        k: {"per_window": {}, "mean_conf": 0.0, "half_hits": 0, "quarter_hits": 0}
        for k in keys
    }

    for w in usable_windows:
        confs = _score_candidates(lottery_name, records[:w], pool_zones)
        order = sorted(range(len(confs)), key=lambda i: confs[i], reverse=True)
        half_n = max(1, len(order) // 2)
        quarter_n = max(1, len(order) // 4)
        half_set = set(order[:half_n])
        quarter_set = set(order[:quarter_n])
        for i, k in enumerate(keys):
            stats[k]["per_window"][w] = confs[i]
            if i in half_set:
                stats[k]["half_hits"] += 1
            if i in quarter_set:
                stats[k]["quarter_hits"] += 1

    for k in keys:
        vals = list(stats[k]["per_window"].values())
        stats[k]["mean_conf"] = sum(vals) / len(vals) if vals else 0.0
    return stats, n_windows


def _tickets_from_stats(
    pool_zones: List[dict],
    stats: Dict[tuple, dict],
    n_windows: int,
    tier: str,
    label: str,
    min_ratio: Optional[float] = None,
) -> List[PredictSet]:
    """
    按共识信息生成 PredictSet 列表（按共识分降序）。

    - robust: 共识分 = 半区命中率 × 平均置信度
    - high:   硬门槛 前列命中率 ≥ min_ratio；共识分 = 前列命中率 × 平均置信度
    """
    tickets: List[PredictSet] = []
    for zones in pool_zones:
        k = _zone_key(zones)
        st = stats[k]
        half_ratio = st["half_hits"] / n_windows if n_windows else 0.0
        quarter_ratio = st["quarter_hits"] / n_windows if n_windows else 0.0
        if tier == "high":
            if min_ratio is not None and quarter_ratio < min_ratio:
                continue
            score = quarter_ratio * st["mean_conf"]
        else:
            score = half_ratio * st["mean_conf"]
        ps = PredictSet(
            zones=zones,
            confidence=score,
            strategy=f"共识集成({label})",
            robustness={
                "半区命中率": round(half_ratio, 4),
                "前列命中率": round(quarter_ratio, 4),
                "入选窗口数": st["quarter_hits"] if tier == "high" else st["half_hits"],
                "窗口数": n_windows,
                "平均置信度": round(st["mean_conf"], 4),
                "组合数": None,  # 由调用方补
            },
            tier=label,
        )
        tickets.append(ps)
    tickets.sort(key=lambda x: x.confidence, reverse=True)
    return tickets


def _predict_robust_or_high(
    lottery_name: str,
    groups: int,
    tier: str,
    windows: Tuple[int, ...],
    seeds: int,
    seed_base: int,
) -> dict:
    """稳健档 / 高鲁棒档：多窗口×多种子建池 + 跨窗口重打分共识。"""
    label = TIER_LABELS[tier]

    # 每个组合取 top_c 候选进池：取 2×groups 让池足够覆盖（上限 120 控算力）
    top_c = min(max(groups * 2, 30), 120)
    pool_zones, total_combos, usable = _build_pool(
        lottery_name, windows, seeds, seed_base, top_c
    )
    stats, n_windows = _rescore_pool(lottery_name, pool_zones, usable)

    coverage_relaxed = False
    if tier == "high":
        tickets = _tickets_from_stats(pool_zones, stats, n_windows, "high", label,
                                      min_ratio=HIGH_TIER_THRESHOLD)
        # 宁少勿凑：数量不足再放宽到 60%，仍不足就保留少数并标注
        min_keep = max(5, groups // 5)
        if len(tickets) < min_keep:
            tickets = _tickets_from_stats(pool_zones, stats, n_windows, "high", label,
                                          min_ratio=HIGH_TIER_RELAXED)
            coverage_relaxed = True
    else:
        tickets = _tickets_from_stats(pool_zones, stats, n_windows, "robust", label)

    tickets = tickets[:groups]
    for ps in tickets:
        ps.robustness["组合数"] = total_combos

    from data.feedback import predict_target

    rt_issue, rt_day = predict_target(lottery_name)

    result = {
        "lottery_name": lottery_name,
        "预测日期": str(rt_day) if rt_day else "",
        "预测模式": f"robust_{label}",
        "号码组数": len(tickets),
        "预测号码": [ps.to_dict() for ps in tickets],
        "生成时间": datetime.now().isoformat(),
        "目标期号": rt_issue,
        "档位": label,
        "鲁棒性说明": HONEST_NOTE,
        "共识参数": {
            "windows": list(usable),
            "seeds": seeds,
            "组合数": total_combos,
            "每组合候选数": top_c,
            "候选池大小": len(pool_zones),
        },
    }
    if tier == "high":
        result["高鲁棒门槛"] = {
            "窗口一致性阈值": HIGH_TIER_RELAXED if coverage_relaxed else HIGH_TIER_THRESHOLD,
            "coverage_relaxed": coverage_relaxed,
        }
    if not tickets:
        # 宁少勿凑：无满足门槛的候选就不出号，给可操作提示而非凑数
        result["提示"] = (
            "当前窗口/参数下无跨窗口一致的候选（宁少勿凑）。"
            "建议：增加历史数据、调整 windows 覆盖更多窗口，或改用稳健档。"
        )
    return result


def predict_tiered(
    lottery_name: str,
    groups: int = 50,
    tier: str = "robust",
    windows: Tuple[int, ...] = (30, 50, 80, 120),
    seeds: int = 4,
    seed_base: int = 20260905,
    general_mode: str = "fresh",
    record_pending: bool = True,
) -> dict:
    """
    三档号码选择主入口。

    参数：
    - tier: "general" | "robust" | "high" | "all"（all = 三档各出一份，全部写 pending）
    - general_mode: 一般档传给 engine.predict 的模式（fresh/trained/...）
    - record_pending: 是否写入 pending（反馈闭环；三档全进对比，权重学习仅一般档）

    返回：
    - tier 单档 → 该档 result dict
    - tier="all" → {"一般": {...}, "稳健": {...}, "高鲁棒": {...}}
    """
    if tier not in ("general", "robust", "high", "all"):
        raise ValueError(f"未知档位: {tier}")

    def _run_one(t: str) -> dict:
        if t == "general":
            from prediction.engine import predict
            # record_pending=False：pending 落库统一在本函数末尾按 record_pending 决定，
            # 否则 predict 内部会先写一次，导致 record_pending=False 也拦不住（历史坑）。
            result = predict(lottery_name, groups=groups, mode=general_mode,
                             record_pending=False)
            result["档位"] = TIER_LABELS["general"]
            result["鲁棒性说明"] = HONEST_NOTE
            # 一般档票面补档位标签（engine.predict 的票不带鲁棒性字段，保持原样）
            for ps in result.get("预测号码", []):
                ps.setdefault("档位", TIER_LABELS["general"])
            return result
        return _predict_robust_or_high(
            lottery_name, groups, t, windows, seeds, seed_base
        )

    tiers = ["general", "robust", "high"] if tier == "all" else [tier]
    results = {TIER_LABELS[t]: _run_one(t) for t in tiers}

    if record_pending:
        from data.feedback import record_pending_prediction
        for label, result in results.items():
            try:
                record_pending_prediction(lottery_name, result)
            except Exception as e:
                logger.warning(f"记录 pending 预测失败({label}): {e}")

    if tier == "all":
        return results
    return results[TIER_LABELS[tier]]
