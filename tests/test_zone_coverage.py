# -*- coding: utf-8 -*-
"""出号覆盖补全（`_ensure_zone_coverage`）的回归锁。

背景（2026-10-23）：七星彩 44 注按位独立采样，第 7 位（15 个取值）覆盖了 14 个、
**恰好漏掉开奖号 4** ⇒ 整批 44 注全空。补全后该批恢复 1 注六等奖。

锁定的契约：
  1. 注数永不改变（出号数量 = 最终注数，见 `_pad_to_groups`）；
  2. 永不产生完全重复的注；
  3. 已全覆盖时幂等（不改任何票）；
  4. 红/蓝型（choose>1）完全不动；
  5. 补全过程不读开奖信息（纯票集内部重构）。
"""

import pytest

from config import LOTTERY_CONFIG
from prediction.engine import (
    PredictSet,
    _ensure_zone_coverage,
    _ps_zone_key,
    schema_from_cfg,
)


def _mk(names, vals):
    return PredictSet(zones={nm: [v] for nm, v in zip(names, vals)})


def _names(schema):
    return [z.name for z in schema.zones]


def _flat(ps, width):
    return [ps.zones[f"第{i+1}位"][0] for i in range(width)]


def test_fills_missing_special_number():
    """七星彩 44 注缺第 7 位=4 ⇒ 必须补齐。"""
    sch = schema_from_cfg(LOTTERY_CONFIG["七星彩"])
    names = _names(sch)
    tickets, seen = [], set()
    for i in range(60):
        vals = [(i * 3) % 10, (i * 7) % 10, (i * 11) % 10,
                (i * 5) % 10, (i * 13) % 10, (i * 17) % 10,
                [v for v in range(15) if v != 4][i % 14]]
        ps = _mk(names, vals)
        k = _ps_zone_key(ps)
        if k in seen:
            continue
        seen.add(k)
        tickets.append(ps)
        if len(tickets) == 44:
            break

    assert 4 not in {t.zones["第7位"][0] for t in tickets}
    out, kpi = _ensure_zone_coverage(tickets, sch, lottery_name="七星彩")

    assert len(out) == len(tickets), "注数必须不变"
    assert {t.zones["第7位"][0] for t in out} == set(range(15)), "第 7 位必须全覆盖"
    assert kpi["补齐"].get("第7位") == 1
    assert kpi["缺口"].get("第7位") == 0


def test_no_duplicate_tickets():
    """补全不得把两注改成同一注。"""
    sch = schema_from_cfg(LOTTERY_CONFIG["七星彩"])
    names = _names(sch)
    tickets = [_mk(names, [0] * 6 + [i % 14]) for i in range(14)]
    out, _ = _ensure_zone_coverage(tickets, sch, lottery_name="七星彩")
    keys = [_ps_zone_key(t) for t in out]
    assert len(set(keys)) == len(keys)
    assert len(out) == 14


def test_idempotent_when_full_coverage():
    """已全覆盖 ⇒ 不再改动（幂等）。"""
    sch = schema_from_cfg(LOTTERY_CONFIG["七星彩"])
    names = _names(sch)
    tickets = [_mk(names, [0, 1, 2, 3, 4, 5, i % 15]) for i in range(15)]
    out1, kpi1 = _ensure_zone_coverage(tickets, sch, lottery_name="七星彩")
    assert kpi1["补齐"] == {} or set(kpi1["缺口"].values()) == {0}
    out2, kpi2 = _ensure_zone_coverage(out1, sch, lottery_name="七星彩")
    assert kpi2["补齐"] == {}
    assert [_ps_zone_key(t) for t in out2] == [_ps_zone_key(t) for t in out1]


def test_red_blue_untouched():
    """双色球/大乐透（choose>1）完全不动。"""
    for lot in ("双色球", "大乐透"):
        sch = schema_from_cfg(LOTTERY_CONFIG[lot])
        ps = PredictSet(reds=[1, 2, 3, 4, 5], blues=[1, 2])
        before = dict(ps.zones)
        out, kpi = _ensure_zone_coverage([ps], sch, lottery_name=lot)
        assert kpi["启用"] is False
        assert out[0].zones == before


def test_partial_coverage_when_tickets_insufficient():
    """注数 < 取值数时尽力覆盖，缺口如实上报（不是失败）。"""
    sch = schema_from_cfg(LOTTERY_CONFIG["福彩3D"])
    names = _names(sch)
    tickets = [_mk(names, [i, i, i]) for i in range(5)]
    out, kpi = _ensure_zone_coverage(tickets, sch, lottery_name="福彩3D")
    assert len(out) == 5
    for i in range(3):
        vals = {t.zones[f"第{i+1}位"][0] for t in out}
        assert len(vals) == 5, "5 注最多覆盖 5 个不同取值，必须全部用上"
        assert kpi["缺口"][f"第{i+1}位"] == 5


def test_empty_batch_safe():
    sch = schema_from_cfg(LOTTERY_CONFIG["七星彩"])
    out, kpi = _ensure_zone_coverage([], sch, lottery_name="七星彩")
    assert out == []
    assert kpi["启用"] is False


def test_malformed_ticket_aborts():
    """出现复式/非法形状时整体放弃，不做半截修改。"""
    sch = schema_from_cfg(LOTTERY_CONFIG["福彩3D"])
    names = _names(sch)
    good = _mk(names, [1, 2, 3])
    bad = PredictSet(zones={"第1位": [1, 2], "第2位": [2], "第3位": [3]})
    before = [dict(good.zones), dict(bad.zones)]
    out, kpi = _ensure_zone_coverage([good, bad], sch, lottery_name="福彩3D")
    assert kpi["启用"] is False
    assert [t.zones for t in out] == before


@pytest.mark.parametrize("lot,width", [("七星彩", 7), ("福彩3D", 3), ("排列5", 5)])
def test_never_changes_ticket_count(lot, width):
    """任意注数下注数守恒，且（入参本身无重复时）不产生重复注。"""
    import random

    sch = schema_from_cfg(LOTTERY_CONFIG[lot])
    names = _names(sch)
    rng = random.Random(20261023)
    for n in (1, 5, 13, 44):
        seen, tickets = set(), []
        guard = 0
        while len(tickets) < n and guard < n * 200:
            guard += 1
            vals = [rng.randint(int(z.min), int(z.max)) for z in sch.zones]
            ps = _mk(names, vals)
            k = _ps_zone_key(ps)
            if k in seen:
                continue
            seen.add(k)
            tickets.append(ps)
        assert len(tickets) == n
        out, _ = _ensure_zone_coverage(tickets, sch, lottery_name=lot)
        assert len(out) == n
        keys = [_ps_zone_key(t) for t in out]
        assert len(set(keys)) == len(keys)


@pytest.mark.parametrize("lot", ["七星彩", "福彩3D"])
def test_preexisting_duplicates_are_not_multiplied(lot):
    """入参已有重复注时，补全不得**新增**碰撞（只能保持或减少重复对数）。"""
    from collections import Counter

    sch = schema_from_cfg(LOTTERY_CONFIG[lot])
    names = _names(sch)
    # 全部同号 ⇒ 极端重复输入
    tickets = [_mk(names, [int(z.min) for z in sch.zones]) for _ in range(12)]
    before = Counter(_ps_zone_key(t) for t in tickets)
    out, _ = _ensure_zone_coverage(tickets, sch, lottery_name=lot)
    after = Counter(_ps_zone_key(t) for t in out)
    assert len(out) == len(tickets), "注数必须不变"
    pairs_before = sum(c * (c - 1) // 2 for c in before.values())
    pairs_after = sum(c * (c - 1) // 2 for c in after.values())
    assert pairs_after <= pairs_before

