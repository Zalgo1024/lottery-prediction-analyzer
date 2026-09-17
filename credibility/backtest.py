"""
可信度层 · 严格样本外(OOS)滚动回测

方法（杜绝穿越）：
  1. 历史按开奖期号升序排列（chronological）。
  2. 对每个折叠 t（从 min_train 到 N-1）：只用 [0, t) 的历史统计（频率/遗漏）生成号码，
     再用 t 期的真实开奖验证。t 期数据绝不参与 t 期的取数与生成。
  3. 每折叠为每种策略生成 k 张票，对该期开奖打分后取均值，再跨折叠汇总。

与现有引擎的关系：生成器同构于 prediction/engine.py 的三策略，但训练窗口替换为历史切片，
且全程纯统计、无模型、无 pickle、零穿越。随机基线用解析零分布（见 baseline.py）。
"""

from typing import Dict, List
import hashlib

import numpy as np

from data.loader import load_lottery
from data.schema import get_schema

from .metrics import (
    STRATEGY_MAP,
    generate_ticket,
    match_score,
    draw_to_zones,
)

# 参与回测的策略（含随机基线用于内部交叉验证，随机基线不进 FDR）
_BACKTEST_KEYS = {
    "高频策略": "high_freq",
    "遗漏值策略": "missing",
    "区间均衡策略": "balanced",
    "随机基线": "random",
}


def oos_backtest(lottery_name: str, k: int = 8, min_train: int = 100,
                 seed: int = 20260825, max_folds: int = None) -> Dict:
    """
    严格 OOS 滚动回测。

    返回:
      {
        lottery_name, zones, n_records, n_folds, min_train, k,
        means:        {策略名: 样本外平均得分},
        per_fold:     {策略名: [各折叠均值]},
        observed_mu:  {策略名: 同上 means（别名，便于阅读）},
      }
    """
    data = load_lottery(lottery_name)
    schema = get_schema(lottery_name)
    chron = sorted(data.records, key=lambda r: r.期号 or 0)
    n = len(chron)
    if n < min_train + 2:
        raise ValueError(f"{lottery_name} 历史不足（{n} 期），无法回测（需 ≥ {min_train + 2}）")

    # 预计算每期开奖的分区号码
    draw_zones_all = [draw_to_zones(schema, rec) for rec in chron]

    # 为每个分区构建累积计数 / 最近出现位置（用于无穿越地取频率与遗漏）
    zone_struct = {}
    for zone in schema.zones:
        nums = list(range(zone.min, zone.max + 1))
        size = len(nums)
        present = np.zeros((n, size), dtype=np.int8)
        last_seen = np.full((n, size), -1, dtype=np.int32)
        cur = {num: -1 for num in nums}
        for t in range(n):
            for num in draw_zones_all[t].get(zone.name, []):
                idx = num - zone.min
                present[t, idx] = 1
                cur[num] = t
            for j, num in enumerate(nums):
                last_seen[t, j] = cur[num]
        cum = present.cumsum(axis=0)  # 含当前期的累积计数
        zone_struct[zone.name] = {
            "nums": nums, "cum": cum, "last_seen": last_seen,
            "min": zone.min, "size": size,
        }

    folds = list(range(min_train, n))
    if max_folds is not None and max_folds > 0:
        folds = folds[-max_folds:]

    sums = {name: 0.0 for name in _BACKTEST_KEYS}
    per_fold: Dict[str, List[float]] = {name: [] for name in _BACKTEST_KEYS}
    n_folds = len(folds)

    for t in folds:
        draw = draw_zones_all[t]
        # 用 [0, t) 窗口构造 freqs / missing
        freqs: Dict[str, Dict[int, float]] = {}
        missing: Dict[str, Dict[int, float]] = {}
        for zname, st in zone_struct.items():
            nums = st["nums"]
            cum_prev = st["cum"][t - 1] if t >= 1 else np.zeros(st["size"], dtype=np.int64)
            last_prev = st["last_seen"][t - 1] if t >= 1 else np.full(st["size"], -1, dtype=np.int32)
            f = {nums[j]: int(cum_prev[j]) for j in range(st["size"])}
            # 遗漏 = (t-1) - 最近出现；从未出现记为 t（极大）
            m = {}
            for j, num in enumerate(nums):
                ls = last_prev[j]
                m[num] = (t - 1 - ls) if ls >= 0 else t
            freqs[zname] = f
            missing[zname] = m

        for name, key in _BACKTEST_KEYS.items():
            # 稳定偏移（hash(name) 跨进程随机化，不可用于可复现实验）
            off = int.from_bytes(name.encode("utf-8"), "little") % 100000
            rng = np.random.default_rng(seed + t * 7 + off)
            sc = 0.0
            for _ in range(k):
                ticket = generate_ticket(key, schema, freqs, missing, rng)
                sc += match_score(schema, ticket, draw)
            fold_mean = sc / k
            sums[name] += fold_mean
            per_fold[name].append(fold_mean)

    means = {name: (sums[name] / n_folds if n_folds else 0.0) for name in _BACKTEST_KEYS}

    return {
        "lottery_name": lottery_name,
        "zones": [z.name for z in schema.zones],
        "n_records": n,
        "n_folds": n_folds,
        "min_train": min_train,
        "k": k,
        "means": means,
        "per_fold": per_fold,
    }
