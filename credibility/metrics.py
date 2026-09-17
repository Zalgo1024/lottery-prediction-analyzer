"""
可信度层 · 核心度量与号码生成

提供与 prediction/engine.py 三种策略"同构"的轻量号码生成器（高频/遗漏/均衡/随机），
以及一个与彩票结构无关的通用得分函数 match_score。

为什么同构：我们要回答的不是"哪个策略更神"，而是"这些策略相对纯随机有没有显著优势"。
因此生成器必须复刻现有策略的取数逻辑，只是把训练窗口换成回测折叠所用的历史切片。
"""

from typing import Dict, List, Optional

import numpy as np

from data.schema import LotterySchema, Zone

# 对外策略名 -> 内部生成器 key（与现有引擎三策略对齐）
STRATEGY_MAP = {
    "高频策略": "high_freq",
    "遗漏值策略": "missing",
    "区间均衡策略": "balanced",
}

# 随机基线（不参与多重比较，仅作为零模型）
RANDOM_KEY = "random"


# ============================================================
# 通用得分
# ============================================================

def match_score(schema: LotterySchema, ticket: Dict[str, List[int]],
                draw: Dict[str, List[int]]) -> float:
    """
    计算单张彩票(ticket) 相对某期开奖(draw) 的命中得分。

    - 乐透型分区（无序、不可重复，如 红球/蓝球）：得分 = 两集合交集大小。
    - 数字型分区（按位有序，如 第1位..第7位）：得分 = 同位同号个数（逐位精确比对）。

    返回所有分区得分之和。
    """
    total = 0.0
    for zone in schema.zones:
        t = ticket.get(zone.name, [])
        d = draw.get(zone.name, [])
        if zone.ordered:
            # 逐位精确比对（有序分区每位独立）
            for a, b in zip(t, d):
                if a == b:
                    total += 1.0
        else:
            # 集合交集（无序分区）
            total += len(set(t) & set(d))
    return total


def expected_random_score(schema: LotterySchema) -> float:
    """
    纯随机合法彩票的期望得分（零模型基准）。

    - 有序分区（每位独立均匀）：贡献 = choose × (1/size) = choose/size。
    - 无序分区（从 size 个里无放回抽 choose 个，开奖也抽 choose 个）：
      单号落入开奖集合概率 = draw_choose/size，期望交集 = choose × draw_choose/size = choose²/size。
    """
    mu = 0.0
    for zone in schema.zones:
        s = zone.size
        c = zone.choose
        if zone.ordered:
            mu += c / s
        else:
            mu += (c * c) / s
    return mu


def variance_random_score(schema: LotterySchema) -> float:
    """
    纯随机合法彩票「单张」得分的方差（各分区独立，方差可加）。

    无序分区：X = #(ticket ∩ draw)，超几何结构，含有限总体协方差修正：
        p = d/s,  d=draw_choose, s=size, c=ticket_choose
        Var = c·p(1-p) + c(c-1)·d(d-s)/(s²(s-1))
    有序分区：每位 Bernoulli(1/s)，c 位独立 → Var = c·(1/s)(1-1/s)。
    """
    var = 0.0
    for zone in schema.zones:
        s = zone.size
        c = zone.choose
        if zone.ordered:
            p = 1.0 / s
            var += c * p * (1 - p)
        else:
            d = c  # 开奖同样抽 choose 个
            p = d / s
            cov = d * (d - s) / (s * s * (s - 1))  # 有限总体协方差（恒为负）
            var += c * p * (1 - p) + c * (c - 1) * cov
    return var


# ============================================================
# 策略同构生成器
# ============================================================

def _ranked_top(zone: Zone, value_map: Dict[int, float], multiplier: int,
                rng: np.random.Generator) -> List[int]:
    """按 value_map 降序取前 max(choose×multiplier, size//2) 个号码，再无放回抽 choose 个。"""
    nums_all = list(range(zone.min, zone.max + 1))
    ranked = sorted(nums_all, key=lambda n: value_map.get(n, 0.0), reverse=True)
    top = ranked[: max(zone.choose * multiplier, zone.size // 2)]
    if len(top) < zone.choose:
        top = nums_all
    sel = rng.choice(top, size=zone.choose, replace=False)
    return sorted(int(x) for x in sel)


def generate_ticket(key: str, schema: LotterySchema,
                    freqs: Dict[str, Dict[int, float]],
                    missing: Dict[str, Dict[int, float]],
                    rng: np.random.Generator) -> Dict[str, List[int]]:
    """
    生成一张符合 schema 结构的彩票号码（分区字典）。

    key ∈ {high_freq, missing, balanced, random}
    freqs/missing: 分区名 -> {号码: 值}，由回测折叠的「历史切片」统计得到。
    """
    zones: Dict[str, List[int]] = {}
    for zone in schema.zones:
        lo, hi = zone.min, zone.max
        if key == RANDOM_KEY:
            if not zone.repeatable:
                sel = rng.choice(range(lo, hi + 1), size=zone.choose, replace=False)
            else:
                sel = rng.integers(lo, hi + 1, size=zone.choose)
            zones[zone.name] = sorted(int(x) for x in sel)
        elif key == "high_freq":
            zones[zone.name] = _ranked_top(zone, freqs.get(zone.name, {}), 5, rng)
        elif key == "missing":
            zones[zone.name] = _ranked_top(zone, missing.get(zone.name, {}), 3, rng)
        elif key == "balanced":
            if not zone.ordered:
                third = (hi - lo + 1) // 3
                ranges = [(lo, lo + third), (lo + third + 1, lo + 2 * third),
                          (lo + 2 * third + 1, hi)]
                per = [zone.choose // 3] * 3
                for i in range(zone.choose - sum(per)):
                    per[i] += 1
                pick = []
                for (a, b), need in zip(ranges, per):
                    pool = list(range(a, b + 1))
                    pick += rng.choice(pool, size=min(need, len(pool)),
                                       replace=False).tolist()
                zones[zone.name] = sorted(int(x) for x in pick)
            else:
                # 数字型有序分区：沿用引擎逻辑 = 全范围均匀（等价于随机，作为诚实对照）
                if not zone.repeatable:
                    sel = rng.choice(range(lo, hi + 1), size=zone.choose, replace=False)
                else:
                    sel = rng.integers(lo, hi + 1, size=zone.choose)
                zones[zone.name] = sorted(int(x) for x in sel)
        else:
            raise ValueError(f"未知生成策略: {key}")
    return zones


def draw_to_zones(schema: LotterySchema, rec) -> Dict[str, List[int]]:
    """把某期开奖记录转成 分区名 -> 号码列表（供 match_score 使用）。"""
    from data.schema import record_zone_numbers
    out: Dict[str, List[int]] = {}
    for zone in schema.zones:
        out[zone.name] = list(record_zone_numbers(rec, zone))
    return out
