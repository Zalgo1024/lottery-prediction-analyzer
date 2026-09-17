"""距离型评估指标（诚实看板 ④ 距离画像）

思路借鉴 chengstone/LotteryPredict 的 Range-K/相似度度量，但口径更严：

不看"中没中"，看"差多远"。对每条已评估的合法预测定义距离
    D = 总可中位数 - 实际命中数
- 双色球/大乐透: D = 红球未中数 + 蓝球未中数（0..7）
- 数字型:       D = 错位个数（0..N）

基线（随机猜测能达到的距离分布）不靠参数假设，直接从真实开奖中
随机抽两条互相比较——两条独立开奖之间的距离分布，就是"瞎猜"
与开奖的距离分布（同口径、同年代结构、无分布假设）。

用途：验证预测的距离分布是否与随机基线重合（证伪视角的可视化），
同时给"差一位/差两位"这类近似命中一个直观刻画。
"""

import logging
from collections import Counter
from typing import Any, Dict, List, Optional

import numpy as np

from data.feedback import load_feedback_history
from data.loader import load_lottery
from data.schema import get_schema, is_redblue

logger = logging.getLogger(__name__)


def _median_from_counts(counts: Dict[int, int]) -> Optional[float]:
    total = sum(counts.values())
    if total <= 0:
        return None
    items = sorted(counts.items())
    if total % 2 == 1:
        acc = 0
        for k, c in items:
            acc += c
            if acc >= (total + 1) // 2:
                return float(k)
    else:
        acc = 0
        lo = None
        for k, c in items:
            acc += c
            if lo is None and acc >= total // 2:
                lo = k
            if acc >= total // 2 + 1:
                return (lo + k) / 2 if lo != k else float(k)
    return float(items[-1][0])


def _pair_distance(a: Dict[str, List[int]], b: Dict[str, List[int]],
                   redblue: bool) -> int:
    """两条开奖（或预测 vs 开奖）之间的距离 D。"""
    if redblue:
        d = 0
        for name, choose in (("红球", a.get("红球", [])), ("蓝球", a.get("蓝球", []))):
            other = b.get(name, [])
            inter = len(set(map(int, choose)) & set(map(int, other)))
            d += len(choose) - inter
        return d
    n = 0
    for name, vals in a.items():
        other = b.get(name, [])
        if not vals or not other:
            continue
        if int(vals[0]) != int(other[0]):
            n += 1
    return n


def distance_stats(lottery: str, mc_trials: int = 5000,
                   seed: Optional[int] = None) -> Dict[str, Any]:
    """距离画像：观测分布 vs 随机基线。

    返回字段：
    - 观测分布 / 基线比例：{距离k: 条数或比例}
    - 观测中位距离 / 基线中位距离
    - 近距离率（D<=1 的占比，观测与基线）
    - 结论：三选一的口径化判读
    """
    redblue = is_redblue(lottery)
    schema = get_schema(lottery)
    choose = schema.total_choose

    history = load_feedback_history(lottery)
    records = history if isinstance(history, list) else history.get("records", [])
    valid = [r for r in records
             if r.get("valid_prediction", True)
             and r.get("总命中") is not None]
    invalid_n = len(records) - len(valid)

    obs: Counter = Counter()
    for r in valid:
        obs[choose - int(r["总命中"])] += 1

    # ---- 基线：真实开奖随机配对 ----
    draws = [d.zone_numbers for d in load_lottery(lottery).records
             if getattr(d, "zone_numbers", None)]
    base: Counter = Counter()
    rng = np.random.default_rng(seed)
    if len(draws) >= 2:
        i = rng.integers(0, len(draws), mc_trials)
        j = rng.integers(0, len(draws), mc_trials)
        for a, b in zip(i, j):
            if a == b:
                continue
            base[_pair_distance(draws[a], draws[b], redblue)] += 1
    base_total = sum(base.values())

    def _dist(c: Counter) -> Dict[str, float]:
        t = sum(c.values())
        return {str(k): v / t for k, v in sorted(c.items())} if t else {}

    n_obs = sum(obs.values())
    obs_med = _median_from_counts({int(k): v for k, v in obs.items()})
    base_med = _median_from_counts(dict(base))
    near_obs = (obs.get(0, 0) + obs.get(1, 0)) / n_obs if n_obs else None
    near_base = (base.get(0, 0) + base.get(1, 0)) / base_total if base_total else None

    if not n_obs:
        verdict = "暂无可评估样本（pending 评估后即有数据）"
    elif obs_med is None or base_med is None:
        verdict = "基线样本不足，无法判读"
    else:
        diff = obs_med - base_med
        ndiff = (near_obs - near_base) if (near_obs is not None and near_base is not None) else None
        if abs(diff) < 0.5:
            verdict = ("观测距离分布与随机基线基本重合——预测未表现出超越随机的接近度"
                       "（与证伪结论一致）")
        elif diff <= -0.5:
            verdict = "预测中位距离显著小于基线（值得用假设闸门进一步检验的信号）"
        else:
            verdict = "预测中位距离大于基线（比随机瞎猜还远，通常是出号过度集中于少数号）"
        if ndiff is not None and abs(diff) < 0.5 and abs(ndiff) < 0.05:
            verdict += "；近距离率（D≤1）同样与基线无实质差异"

    notes = [f"剔除非法/未评估记录 {invalid_n} 条",
             f"基线 = 真实开奖随机配对 {base_total} 对（无参数假设）"]
    if n_obs and n_obs < 30:
        notes.append(f"观测样本仅 {n_obs} 条，分布仅供参考")

    return {
        "彩种": lottery,
        "类型": "红蓝球" if redblue else "按位数字型",
        "总可中位数": choose,
        "样本数": n_obs,
        "观测分布": {str(k): v for k, v in sorted(obs.items())},
        "基线比例": _dist(base),
        "观测中位距离": obs_med,
        "基线中位距离": base_med,
        "近距离率": near_obs,
        "基线近距离率": near_base,
        "结论": verdict,
        "备注": notes,
    }
