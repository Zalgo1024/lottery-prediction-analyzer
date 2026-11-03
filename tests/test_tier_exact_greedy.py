# -*- coding: utf-8 -*-
"""`ev/tier_exact_greedy`：精确边际增益贪心。

这一批测试的核心只有一句话：**报出来的那个数，必须能被权威实现独立复算出来**。
所以每个「精确概率」都拿 `ev.tier_coverage.exact_union_probability`（另一条实现路径）
再算一遍，要求逐位相等 —— 不是近似相等，是 ==。
"""

from __future__ import annotations

import math
import random

import pytest

from ev import tier_exact_greedy as G
from ev.tier_coverage import exact_union_probability, select_tier_coverage

LOT = "双色球"
TGT4 = {"kind": "grade", "grade": "四等"}
TGT5 = {"kind": "payout", "min_payout": 5}

# ≥四等 的解析值：一/二/三/四等之和 = 7873 / 17,721,088
_P_GE4 = 7873 / 17721088


def _pool(n: int = 16, seed: int = 7):
    """确定性候选池（随机取样，保证相互重叠通常 ≤3，否则 cap 会立刻卡死）。"""
    rng = random.Random(seed)
    out, seen = [], set()
    while len(out) < n:
        reds = tuple(sorted(rng.sample(range(1, 34), 6)))
        if reds in seen:
            continue
        seen.add(reds)
        out.append({"红球": list(reds), "蓝球": [rng.randint(1, 16)]})
    return out


def _overlap(a, b) -> int:
    return len(set(a["红球"]) & set(b["红球"]))


# ---------------------------------------------------------------- 概率口径
def test_single_ticket_matches_analytic():
    """K=1 时必须等于解析值 7873/17,721,088（一/二/三/四等之和）。

    ★ 这条能挡住最多的一类错：位掩码把 gate 写反（例如把「蓝球不中」也算进四等），
      那种错会让概率高一个量级，而其它测试（都是"比大小"）照样能过。
    """
    pool = _pool(4)
    ctx = G._Ctx(LOT, TGT4, None)
    p = G.exact_union_from_ctx(ctx, [pool[0]])
    assert p == pytest.approx(_P_GE4, rel=1e-12)


def test_meta_probability_matches_authoritative_impl():
    """meta 里的精确 P 必须与 `exact_union_probability` 完全相等（两条实现互证）。"""
    pool = _pool(16)
    tks, meta = G.select_exact_tier(LOT, pool, 4, TGT4, cap=3)
    ref = exact_union_probability(LOT, tks, target=TGT4)
    # 用 rel=1e-15 而不是 ==：两边浮点求和顺序不同（一边先平均再除内层，
    # 一边先乘总格数再除），末位可能差 1 ULP。差的量级必须是 ULP，不是"差不多"。
    assert meta["精确达标签概率"] == pytest.approx(ref, rel=1e-15)


def test_dlt_inner_combination_masking():
    """大乐透后区是 2 选 2、N_in=C(12,2)=66：掩码必须按内层**组合**索引。

    ★ 2026-11-03 实踩：原来按「单个内层号」索引（双色球恰好能对，因为蓝球就 1 个），
      大乐透上自报 0.0040 vs 权威 0.0075，差近 2 倍 —— 而所有"比大小"的测试照样全绿。
      只有与权威实现逐位对齐才抓得到。
    """
    pool = [{"红球": [1, 3, 7, 12, 25], "蓝球": [2, 9]},
            {"红球": [4, 8, 16, 22, 33], "蓝球": [3, 11]},
            {"红球": [2, 6, 14, 19, 28], "蓝球": [5, 12]}]
    tgt = {"kind": "grade", "grade": "四等"}
    ctx = G._Ctx("大乐透", tgt, None)
    for t in pool:
        p_self = G.exact_union_from_ctx(ctx, [t])
        p_ref = exact_union_probability("大乐透", [t], target=tgt)
        assert p_self == pytest.approx(p_ref, rel=1e-15), \
            f"单票概率与大乐透权威实现不一致：{p_self} vs {p_ref}"
        # 更硬的一条：被覆盖的 (ρ,B) **格数**必须是同一个整数
        tot = ctx.M_out * ctx.N_in
        assert round(p_self * tot) == round(p_ref * tot)
    tks, meta = G.select_exact_tier("大乐透", pool, 2, tgt, cap=3)
    assert meta["精确达标签概率"] == pytest.approx(
        exact_union_probability("大乐透", tks, target=tgt), rel=1e-15)


def test_probability_never_exceeds_kp_upper_bound():
    """P(并集) ≤ min(1, K·p)。越界就是口径错，不是"效果特别好"。"""
    pool = _pool(16)
    tks, meta = G.select_exact_tier(LOT, pool, 6, TGT4, cap=3)
    p1 = meta["单注达标概率"]
    assert meta["精确达标签概率"] <= min(1.0, p1 * len(tks)) + 1e-15


# ---------------------------------------------------------------- 约束
def test_all_from_pool_and_no_duplicate():
    pool = _pool(16)
    tks, _ = G.select_exact_tier(LOT, pool, 5, TGT4, cap=3)
    ids = {id(t) for t in tks}
    assert len(ids) == len(tks), "选中了重复对象"
    assert ids <= {id(t) for t in pool}, "选中了不在候选池里的票（现场造票）"


def test_cap_is_respected():
    """重叠上限用独立实现复核（`set` 交集），不信内部矩阵。"""
    pool = _pool(20, seed=11)
    tks, meta = G.select_exact_tier(LOT, pool, 6, TGT4, cap=3)
    worst = max((_overlap(a, b) for i, a in enumerate(tks) for b in tks[i + 1:]),
                default=0)
    assert worst <= 3, f"两两重叠 {worst} > cap 3"
    assert meta["重叠上限"] == worst, "meta 报告的重叠与独立复核不一致"


def test_budget_truncates_ticket_count():
    """预算先到就停：budget=6 元、每注 2 元 ⇒ 最多 3 注。"""
    pool = _pool(16)
    tks, meta = G.select_exact_tier(LOT, pool, 50, TGT4, cap=3, budget=6.0)
    assert len(tks) <= 3
    assert meta["总成本元"] <= 6.0
    assert meta["每注成本元"] == 2.0


# ---------------------------------------------------------------- 可复现与子模性
def test_deterministic_same_seed():
    pool = _pool(16)
    a, _ = G.select_exact_tier(LOT, pool, 4, TGT4, cap=3, seed=5)
    b, _ = G.select_exact_tier(LOT, pool, 4, TGT4, cap=3, seed=5)
    assert [id(x) for x in a] == [id(x) for x in b]


def test_trace_gains_are_non_increasing():
    """覆盖函数子模 ⇒ 边际增益必须单调不增。跌了说明进位/去重写错了。"""
    pool = _pool(16)
    _tks, meta = G.select_exact_tier(LOT, pool, 5, TGT4, cap=3, trace=True)
    gains = [step["新增格数"] for step in meta["贪心轨迹"]]
    assert gains == sorted(gains, reverse=True), f"边际增益非单调：{gains}"
    assert all(g > 0 for g in gains)


def test_secondary_does_not_change_primary_objective():
    """次级目标只允许在**并列**时起作用 ⇒ 主目标的精确 P 必须不变。"""
    pool = _pool(16)
    _a, m1 = G.select_exact_tier(LOT, pool, 4, TGT4, cap=3)
    _b, m2 = G.select_exact_tier(LOT, pool, 4, TGT4, cap=3, secondary=TGT5)
    assert m1["精确达标签概率"] == m2["精确达标签概率"]


# ---------------------------------------------------------------- 边界（不许静默降级）
def test_non_redblue_lottery_raises():
    """数字型不是按 (h,j) 分桶判级 ⇒ 直接拒绝，不做近似替代。"""
    with pytest.raises(ValueError, match="红蓝型"):
        G._Ctx("福彩3D", TGT4, None)


def test_enumeration_too_large_raises():
    """七乐彩 C(30,7)=2,035,800 超枚举上限 ⇒ 拒绝，不偷偷改成抽样。"""
    with pytest.raises(ValueError, match="超出上限"):
        G._Ctx("七乐彩", TGT4, None)


def test_caveat_is_present():
    """诚实边界必须随结果一起返回（不改变单注概率 / O((K·p)²) / 报收益要带 K）。"""
    pool = _pool(8)
    _tks, meta = G.select_exact_tier(LOT, pool, 3, TGT4, cap=3)
    caveat = meta["诚实边界"]
    assert "中奖概率" in caveat
    assert "K·p" in caveat or "Kp" in caveat
    assert "不保证盈利" in caveat
