"""
ev/coverage.py —— 撞号度量 + 低重叠剪枝（有效覆盖率 KPI）

## 诚实定位（写死，防误读）

本模块**不提升中奖概率**：

- 每注命中概率是组合数学常数（双 1/14.9、大 1/15.0、七星 1/12.3、3D·排3 1/166.7、
  排5 1/10万），N 注「至少中一次」= 1-(1-p)^N 只随注数变化；
- **期望线性性**：N 注期望奖金 = N × 单注期望，与选号方式无关；
- 剪枝唯一能做的事：把「互相高度相似的注」裁掉。100 注若彼此高度相似，有效覆盖
  可能只相当于数十注独立随机 → 裁掉冗余后中奖率几乎不变，而成本显著下降。
  这叫**等效减注**，不是提率。

## 撞号口径

- 双色球 / 大乐透：红球（前区）交集个数 > cap → 撞号（蓝球/后区不参与，与
  `ev/selection._OVERLAP_CAP` 一致）；
- 七星彩：7 位**按位**相同个数 > cap → 撞号（勿误用多重集）；
- 数字型（福彩3D/排列3/排列5）：号码**多重集**相同 → 撞号（直选/组选通吃）。
  数字型固定赔率、注数已降到 5 注，默认不剪枝（只统计覆盖率）。
"""
from __future__ import annotations

import logging
import math
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from ev.selection import _OVERLAP_CAP

logger = logging.getLogger(__name__)

_LOTTO = ("双色球", "大乐透", "七星彩")
_SEQ_LOTTERY = ("七星彩",)
_DIGITAL_FIXED = ("福彩3D", "排列3", "排列5")
_DIGITAL_DIGITS = {"福彩3D": 3, "排列3": 3, "排列5": 5}

HONEST_NOTE = (
    "剪枝仅改变注的分布形状（降低撞号 = 等效减注），不提升任何单注中奖概率"
    "（每注 p 是组合数常数，期望奖金与选号无关）"
)


# ------------------------------------------------------------
# 票面 → 重叠键
# ------------------------------------------------------------

def _zone_order(name: Any) -> Tuple[int, str]:
    """分区名排序键：「第2位」→(2,'第2位')，无数字的排最后。"""
    m = re.search(r"(\d+)", str(name))
    return (int(m.group(1)) if m else 10 ** 6, str(name))


def _as_dict(ticket: Any) -> Dict[str, Any]:
    """兼容 PredictSet 对象与普通 dict。"""
    if hasattr(ticket, "to_dict"):
        try:
            return ticket.to_dict()
        except Exception:  # pragma: no cover - 防御
            pass
    return ticket if isinstance(ticket, dict) else {}


def ticket_key(ticket: Any, lottery: str) -> Tuple[str, tuple]:
    """把票面压成可比较的重叠键：('RB'|'SEQ'|'MS', tuple)。"""
    d = _as_dict(ticket)
    if "红球" in d:
        return ("RB", tuple(sorted(int(x) for x in (d.get("红球") or []))))

    zones = d.get("号码")
    if isinstance(zones, dict):
        flat: List[int] = []
        for zn in sorted(zones.keys(), key=_zone_order):
            flat.extend(int(x) for x in (zones.get(zn) or []))
    elif isinstance(zones, (list, tuple)):
        flat = [int(x) for x in zones]
    else:
        flat = []

    if lottery in _SEQ_LOTTERY:
        return ("SEQ", tuple(flat))
    return ("MS", tuple(sorted(flat)))


def pair_overlap(ka: Tuple[str, tuple], kb: Tuple[str, tuple], lottery: str) -> int:
    """两张票的重叠度（口径见模块头注释）。键类型不一致时按 0 处理。"""
    kind_a, va = ka
    kind_b, vb = kb
    if kind_a == "RB" and kind_b == "RB":
        return len(set(va) & set(vb))
    if kind_a == "SEQ" and kind_b == "SEQ":
        return sum(1 for x, y in zip(va, vb) if x == y)
    if kind_a == "MS" and kind_b == "MS":
        return 1 if va == vb else 0
    return 0


# ------------------------------------------------------------
# 随机基线（解析式）
# ------------------------------------------------------------

def random_baseline_overlap(lottery: str) -> float:
    """随机独立两张票的期望重叠度（解析式，作为"未优化时的自然水平"参照）。

    - 双色球：红球交集期望 = k·K/N = 6×6/33 ≈ 1.0909（超几何一阶矩）
    - 大乐透：5×5/35 ≈ 0.7143
    - 七星彩：前 6 位各 1/10 + 第 7 位 1/15 ≈ 0.6667
    - 数字型：多重集相同的概率 = 1/C(10+d-1, d)（3D→1/220、排列5→1/2002）
    """
    if lottery == "双色球":
        return 6 * 6 / 33
    if lottery == "大乐透":
        return 5 * 5 / 35
    if lottery == "七星彩":
        return 6 * (1 / 10) + 1 * (1 / 15)
    d = _DIGITAL_DIGITS.get(lottery)
    if d:
        return 1.0 / math.comb(10 + d - 1, d)
    return 0.0


def default_cap(lottery: str) -> int:
    """该彩种的重叠上限（与 ev/selection 口径一致）。"""
    return int(_OVERLAP_CAP.get(lottery, 3))


# ------------------------------------------------------------
# 度量
# ------------------------------------------------------------

def _avg_pairwise(keys: Sequence[Tuple[str, tuple]], lottery: str) -> float:
    n = len(keys)
    if n < 2:
        return 0.0
    tot = 0
    cnt = 0
    for i in range(n):
        for j in range(i + 1, n):
            tot += pair_overlap(keys[i], keys[j], lottery)
            cnt += 1
    return tot / cnt if cnt else 0.0


def _clash_pair_rate(keys: Sequence[Tuple[str, tuple]], lottery: str, cap: int) -> float:
    """撞号对占比：重叠 > cap 的无序对比例。"""
    n = len(keys)
    if n < 2:
        return 0.0
    bad = cnt = 0
    for i in range(n):
        for j in range(i + 1, n):
            cnt += 1
            if pair_overlap(keys[i], keys[j], lottery) > cap:
                bad += 1
    return bad / cnt if cnt else 0.0


def _kpi(lottery: str, nominal_n: int, keys: Sequence[Tuple[str, tuple]],
         strategies: Sequence[str], cap: int, keep_per_strategy: int,
         pruned: bool) -> Dict[str, Any]:
    kept_n = len(keys)
    avg_pair = _avg_pairwise(keys, lottery)
    base = random_baseline_overlap(lottery)
    return {
        "彩种": lottery,
        "名义注数": int(nominal_n),
        "剪枝后注数": kept_n,
        "减注等效": int(max(0, nominal_n - kept_n)),
        "有效覆盖率": round(kept_n / nominal_n, 4) if nominal_n else 1.0,
        "平均两两重叠": round(avg_pair, 4),
        "随机基线重叠": round(base, 4),
        "重叠压降": round(base - avg_pair, 4),
        "撞号对占比": round(_clash_pair_rate(keys, lottery, cap), 4),
        "重叠上限cap": cap,
        "每策略保底": keep_per_strategy,
        "策略覆盖数": len({s for s in strategies if s}),
        "剪枝开关": bool(pruned),
        "诚实声明": HONEST_NOTE,
    }


def coverage_stats(lottery: str, tickets: Sequence[Any], cap: Optional[int] = None) -> Dict[str, Any]:
    """只度量不剪枝：给定一批票，报告撞号程度与去重后的"等效注数"。"""
    cap = default_cap(lottery) if cap is None else int(cap)
    keys = [ticket_key(t, lottery) for t in tickets]
    strategies = [str(_as_dict(t).get("策略") or "") for t in tickets]
    return _kpi(lottery, len(tickets), keys, strategies, cap, 1, False)


# ------------------------------------------------------------
# 低重叠剪枝
# ------------------------------------------------------------

def _pick_low_overlap(items_by_strategy: Dict[str, List[Dict[str, Any]]],
                      items_all: List[Dict[str, Any]], lottery: str,
                      cap: int, keep_per_strategy: int, target: int) -> List[Dict[str, Any]]:
    """两阶段确定性贪心：

    阶段 1（每策略保底）：按策略轮转，每策略先选一张与已选全部合规的票；
        若该策略所有票都违规，则**保底优先于 cap**，直接取组内置信度最高的一张
        （保证归因/权重学习仍能看到每个策略）。
    阶段 2（低重叠补足）：剩余票按置信度降序扫描，仅当与已选全部票重叠 ≤ cap 才入选，
        直到达到 target。
    """
    selected: List[Dict[str, Any]] = []
    used: set = set()

    def _ok(it: Dict[str, Any]) -> bool:
        return all(pair_overlap(it["key"], u["key"], lottery) <= cap for u in selected)

    for _ in range(max(0, int(keep_per_strategy))):
        for _st, group in items_by_strategy.items():
            cand = next((g for g in group if g["i"] not in used and _ok(g)), None)
            if cand is None:
                cand = next((g for g in group if g["i"] not in used), None)
            if cand is not None:
                selected.append(cand)
                used.add(cand["i"])

    for it in items_all:
        if len(selected) >= target:
            break
        if it["i"] in used:
            continue
        if _ok(it):
            selected.append(it)
            used.add(it["i"])

    selected.sort(key=lambda x: x["i"])
    return selected


def prune_predicted_sets(predicted_sets: Sequence[Any], lottery: str, *,
                         nominal_n: Optional[int] = None, cap: Optional[int] = None,
                         keep_per_strategy: int = 1, enabled: bool = True
                         ) -> Tuple[List[int], Dict[str, Any]]:
    """对一批已按置信度降序排列的票做低重叠剪枝。

    返回 `(保留下标列表(升序), KPI 字典)`。

    - 调用方须保证 `predicted_sets` 已按置信度降序（保留优先级 = 置信度）；
    - 数字型（固定赔率）与 `enabled=False` 时不剪枝，只出 KPI；
    - 用返回的下标过滤原列表即可保持 `"号码组数" == len("预测号码")`。
    """
    tickets = list(predicted_sets)
    n = len(tickets)
    nominal_n = n if nominal_n is None else max(0, min(int(nominal_n), n))
    cap = default_cap(lottery) if cap is None else int(cap)

    dicts = [_as_dict(t) for t in tickets]
    items = [{"i": i, "strategy": str(dicts[i].get("策略") or ""),
              "key": ticket_key(tickets[i], lottery)} for i in range(n)]

    prune = bool(enabled) and lottery not in _DIGITAL_FIXED and n > 0
    if not prune:
        keys = [it["key"] for it in items]
        strategies = [it["strategy"] for it in items]
        return list(range(n)), _kpi(lottery, nominal_n, keys, strategies, cap,
                                    keep_per_strategy, False)

    by_strategy: Dict[str, List[Dict[str, Any]]] = {}
    for it in items:  # items 已是置信度降序
        by_strategy.setdefault(it["strategy"], []).append(it)

    selected = _pick_low_overlap(by_strategy, items, lottery, cap,
                                 keep_per_strategy, nominal_n)
    kept = [it["i"] for it in selected]
    return kept, _kpi(lottery, nominal_n,
                      [it["key"] for it in selected],
                      [it["strategy"] for it in selected],
                      cap, keep_per_strategy, True)


def _greedy_indices(keys: Sequence[Tuple[str, tuple]], lottery: str, cap: int) -> List[int]:
    """纯度量用：按给定顺序贪心保留低重叠下标（不涉及策略保底）。"""
    kept: List[int] = []
    for i, k in enumerate(keys):
        if all(pair_overlap(k, keys[j], lottery) <= cap for j in kept):
            kept.append(i)
    return kept


def effective_count(lottery: str, tickets: Sequence[Any], cap: Optional[int] = None) -> int:
    """等效注数：贪心去撞号后还能留下的注数（纯度量，不改输入）。

    与 `prune_predicted_sets` 的区别：这里**强制去重**（数字型也计算），
    因为它只回答"这批票的覆盖效率如何"，不改变出号。
    """
    cap = default_cap(lottery) if cap is None else int(cap)
    if lottery in _DIGITAL_FIXED:
        cap = 0  # 数字型：多重集相同才算撞号
    keys = [ticket_key(t, lottery) for t in tickets]
    return len(_greedy_indices(keys, lottery, cap))


# ------------------------------------------------------------
# 质量+重叠压缩（2026-09-14：用户要求"训练后尽可能压低出号数量"）
# ------------------------------------------------------------

def compress_tickets(
    tickets: Sequence[Any],
    lottery: str,
    *,
    keep_ratio: Optional[float] = None,
    quality_factor: Optional[float] = None,
    min_keep: Optional[int] = None,
    cap: Optional[int] = None,
) -> Tuple[List[int], Dict[str, Any]]:
    """按「推荐分降序 + 质量线 + 去重 + 低重叠 + 目标上限」压缩票面列表。

    与 `prune_predicted_sets` 的区别：那只剪违例、尽量保全（真实数据下近似
    no-op）；本函数带**硬性目标注数上限**（keep_ratio×n）与**质量线**
    （推荐分 < 中位数×quality_factor 直接淘汰），会真正减注。

    诚实边界：减注省成本，单注中奖概率不变（期望线性性）；"质量"= 推荐分
    （策略权重学习产物），保留的是"历史表现好的策略留下的票"，不构成
    "选得更准"。数字型与 n ≤ min_keep 原样返回。

    返回 `(保留下标列表(升序), KPI dict)`。
    """
    from config import (TICKET_COMPRESS_ENABLED, TICKET_COMPRESS_KEEP_RATIO,
                        TICKET_COMPRESS_QUALITY, TICKET_COMPRESS_MIN_KEEP)

    keep_ratio = TICKET_COMPRESS_KEEP_RATIO if keep_ratio is None else float(keep_ratio)
    quality_factor = (TICKET_COMPRESS_QUALITY if quality_factor is None
                      else float(quality_factor))
    min_keep = TICKET_COMPRESS_MIN_KEEP if min_keep is None else int(min_keep)
    cap = default_cap(lottery) if cap is None else int(cap)

    n = len(tickets)
    noop_kpi: Dict[str, Any] = {"压缩前注数": n, "压缩后注数": n, "目标上限": n,
                                "质量淘汰": 0, "重复淘汰": 0, "重叠淘汰": 0,
                                "回填": 0, "启用": bool(TICKET_COMPRESS_ENABLED)}
    if (not TICKET_COMPRESS_ENABLED) or n <= max(1, min_keep) \
            or lottery in _DIGITAL_FIXED:
        noop_kpi["说明"] = "未压缩（总开关关闭 / 注数≤保底 / 数字型不压缩）"
        return list(range(n)), noop_kpi

    dicts = [_as_dict(t) for t in tickets]

    def _score(d: Dict[str, Any]) -> float:
        s = d.get("推荐分", d.get("置信度"))
        try:
            return float(s)
        except (TypeError, ValueError):
            return 0.0

    scores = [_score(d) for d in dicts]
    keys = [ticket_key(tickets[i], lottery) for i in range(n)]
    order = sorted(range(n), key=lambda i: (-scores[i], i))

    # 1) 质量线：低于 中位数×quality_factor 直接淘汰
    s_sorted = sorted(scores)
    median = (s_sorted[n // 2] if n % 2
              else (s_sorted[n // 2 - 1] + s_sorted[n // 2]) / 2)
    floor = median * quality_factor
    quality_ok = [i for i in order if scores[i] >= floor]
    n_quality = n - len(quality_ok)

    # 2) 去重 + 3) 低重叠贪心 + 4) 目标上限（分数降序扫描，选满即停）
    target = max(min_keep, math.ceil(n * keep_ratio))
    kept: List[int] = []
    seen_keys: set = set()
    n_dup = n_overlap = 0
    for i in quality_ok:
        if len(kept) >= target:
            break
        k = keys[i]
        if k in seen_keys:
            n_dup += 1
            continue
        if all(pair_overlap(k, keys[j], lottery) <= cap for j in kept):
            kept.append(i)
            seen_keys.add(k)
        else:
            n_overlap += 1

    # 5) 保底回填：不足 min_keep 时按分数从被淘汰者回填（不再管重叠，仍去重）
    backfilled = 0
    if len(kept) < min_keep:
        chosen = set(kept)
        for i in order:
            if len(kept) >= min_keep:
                break
            if i in chosen or keys[i] in seen_keys:
                continue
            kept.append(i)
            seen_keys.add(keys[i])
            backfilled += 1

    kept.sort()
    kpi = {
        "压缩前注数": n, "压缩后注数": len(kept), "目标上限": target,
        "质量线": round(floor, 4), "质量淘汰": n_quality,
        "重复淘汰": n_dup, "重叠淘汰": n_overlap, "回填": backfilled,
        "启用": True, "说明": HONEST_NOTE,
    }
    return kept, kpi


if __name__ == "__main__":  # pragma: no cover - 手工速查
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    from config import LOTTERY_CONFIG
    print("撞号随机基线（解析式）与默认重叠上限：")
    for _lot in LOTTERY_CONFIG:
        print(f"  {_lot:<6} 基线重叠 {random_baseline_overlap(_lot):.4f}   cap={default_cap(_lot)}")
