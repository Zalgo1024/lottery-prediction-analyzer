# -*- coding: utf-8 -*-
"""tests/test_tier_target.py —— 奖级达标目标的反查 / 通道键契约。

这些数不是拍出来的：`single_ticket_probability` 与解析手算逐位对齐
（双色球 ≥四等 4.442729e-4、大乐透 2026-02 ≥四等 1.501e-4），
而解析手算又与大乐透 13 个条件概率和 0.066704 的既有锁一致。
"""
import math

import pytest

from ev import tier_target as TT

SSQ_TOTAL = 17_721_088
DLT_TOTAL = 21_425_712


# ---------------------------------------------------------------- 目标解析
def test_normalize_target_forms():
    assert TT.normalize_target(None) == {"kind": "grade", "grade": "四等"}
    assert TT.normalize_target("四等") == {"kind": "grade", "grade": "四等"}
    assert TT.normalize_target("四等奖") == {"kind": "grade", "grade": "四等"}
    assert TT.normalize_target(10) == {"kind": "payout", "min_payout": 10.0}
    assert TT.normalize_target("10元") == {"kind": "payout", "min_payout": 10.0}
    assert TT.normalize_target({"kind": "grade", "grade": "五等"})["grade"] == "五等"
    assert TT.label_of(None) == "≥四等"
    assert TT.label_of(10) == "单注≥10元"


# ---------------------------------------------------------------- 条件集 / 解析概率
def test_ssq_conditions_match_grade_table():
    r = TT.resolve_conditions("双色球", "四等", "26120")
    assert r["conditions"] == [(4, 1), (5, 0), (5, 1), (6, 0), (6, 1)]
    assert r["grades"] == frozenset({"一等", "二等", "三等", "四等"})
    assert r["rule_version"] == "ssq-2026-02"


def test_ssq_single_ticket_probability():
    """与手算一致：7873/17,721,088、1,188,988/17,721,088。"""
    p4 = TT.single_ticket_probability(
        "双色球", TT.grade_conditions_at_or_above("双色球", "四等", "26120"), "26120")
    assert p4 == pytest.approx(7873 / SSQ_TOTAL, rel=1e-12)
    assert p4 == pytest.approx(4.442729e-4, rel=1e-6)

    p5 = TT.single_ticket_probability(
        "双色球", TT.grade_conditions_at_or_above("双色球", "五等", "26120"), "26120")
    assert p5 == pytest.approx(8.201979e-3, rel=1e-6)

    p6 = TT.single_ticket_probability(
        "双色球", TT.grade_conditions_at_or_above("双色球", "六等", "26120"), "26120")
    assert p6 == pytest.approx(1_188_988 / SSQ_TOTAL, rel=1e-12)
    assert p6 == pytest.approx(0.067094, abs=1e-6)


def test_dlt_single_ticket_probability():
    p4 = TT.single_ticket_probability("大乐透", TT.grade_conditions_at_or_above("大乐透", "四等", "26120"), "26120")
    assert p4 == pytest.approx(3216 / DLT_TOTAL, rel=1e-12)
    assert p4 == pytest.approx(1.501e-4, rel=1e-3)


def test_dlt_all_winning_conditions_conserved():
    """13 个中奖条件的总概率 = 0.066704（与 test_display_single_source 同一条锁）。"""
    p = TT.single_ticket_probability(
        "大乐透", TT.grade_conditions_at_or_above("大乐透", "七等", "26120"), "26120")
    assert p == pytest.approx(0.066704, abs=1e-6)


def test_dlt_conditions_are_issue_version_aware():
    """同一问法在 2026-02 与 2019 段答案不同：(4,1) 在 2026 是四等，在 2019 不是四等。"""
    c26 = TT.grade_conditions_at_or_above("大乐透", "四等", "26120")
    c19 = TT.grade_conditions_at_or_above("大乐透", "四等", "23001")
    assert c26 == [(4, 1), (4, 2), (5, 0), (5, 1), (5, 2)]
    assert c19 == [(4, 2), (5, 0), (5, 1), (5, 2)]
    assert (4, 1) in c26 and (4, 1) not in c19
    # 2026 的七等 == 2019 的九等（同一批格，只是级数改了）
    assert TT.grade_conditions_at_or_above("大乐透", "七等", "26120") == \
        TT.grade_conditions_at_or_above("大乐透", "九等", "23001")
    # 2019 段七等是 4+0（比 2026 的七等窄）
    assert (4, 0) in TT.grade_conditions_at_or_above("大乐透", "七等", "23001")


def test_conditions_are_upward_closed_in_h():
    """条件集必须在 h 上向上封闭（否则通道分解会漏结局）。"""
    for lot, issue in (("双色球", "26120"), ("大乐透", "26120"), ("大乐透", "23001")):
        from ev.profit.rules import rules_for
        gt = rules_for(lot, issue).grade_table
        for grade in ("二等", "三等", "四等", "五等", "六等", "七等"):
            try:
                conds = TT.grade_conditions_at_or_above(lot, grade, issue)
            except ValueError:
                continue
            cs = set(conds)
            for (h, j) in conds:
                for h2 in range(h + 1, 6):
                    g2 = gt.get((h2, j))
                    if g2 is not None:
                        assert (h2, j) in cs, f"{lot}@{issue} {grade}: ({h},{j}) 达标但 ({h2},{j}) 不达标"


def test_unknown_grade_raises():
    with pytest.raises(ValueError):
        TT.resolve_conditions("双色球", "九等", "26120")
    with pytest.raises(ValueError):
        TT.resolve_conditions("福彩3D", "组选3", None)


# ---------------------------------------------------------------- 金额线 → 奖级
def test_payout_target_maps_via_rule_table():
    """≥10 元在双色球 = 一等..五等；在大乐透 2026-02 = 一等..六等（六等 15 元）。"""
    s = TT.resolve_conditions("双色球", 10, "26120")
    assert s["grades"] == frozenset({"一等", "二等", "三等", "四等", "五等"})
    assert s["conditions"] == TT.grade_conditions_at_or_above("双色球", "五等", "26120")

    d = TT.resolve_conditions("大乐透", 10, "26120")
    assert "六等" in d["grades"] and "七等" not in d["grades"]
    assert len(d["conditions"]) == 9

    # ≥5 元：双色球 = 全部六大奖级；大乐透 = 全部七级（13 个格）
    assert len(TT.resolve_conditions("双色球", 5, "26120")["conditions"]) == 10
    assert len(TT.resolve_conditions("大乐透", 5, "26120")["conditions"]) == 13


def test_payout_target_has_no_forward_look(monkeypatch):
    """金额映射只读**规则表**：把「读当期实际派奖」的入口打爆后仍能算出条件集。"""
    import data.prize_amounts as PA
    import data.prize_table as PT

    def boom(*a, **k):
        raise AssertionError("resolve_conditions 不得读当期实际派奖（前视泄漏）")

    monkeypatch.setattr(PA, "actual_per_bet", boom, raising=False)
    monkeypatch.setattr(PT, "prize_payout", boom, raising=False)

    d = TT.resolve_conditions("大乐透", 10, "26120")
    assert len(d["conditions"]) == 9
    s = TT.resolve_conditions("双色球", 5, "26120")
    assert len(s["conditions"]) == 10
    # 同一规则版本内，期号不影响条件集
    assert TT.resolve_conditions("大乐透", 10, "26119")["conditions"] == d["conditions"]


# ---------------------------------------------------------------- 判级一致
@pytest.mark.parametrize("lottery,issue", [("双色球", "26120"), ("大乐透", "26120")])
def test_grade_of_ticket_matches_backtest_grader(lottery, issue):
    """`grade_of_ticket` 必须与回测用的 `grade_ticket` 判级完全一致（同源单一真源）。"""
    import numpy as np

    from data.loader import load_lottery
    from ev.coverage_backtest import grade_ticket
    from ev.selection import _rand_tickets

    data = load_lottery(lottery)
    rec = next(r for r in data.records if str(r.期号) == issue)
    tickets = _rand_tickets(lottery, 40, np.random.default_rng(7))
    cache: dict = {}
    for t in tickets:
        want = grade_ticket(lottery, t, rec, issue, cache)[0]
        got = TT.grade_of_ticket(lottery, t, rec.zone_numbers or {}, issue)
        assert got == want, f"{lottery} {t} → mine={got} backtest={want}"


@pytest.mark.parametrize("lottery,issue", [("双色球", "26120"), ("大乐透", "26120")])
def test_target_is_hit_agrees_with_grade(lottery, issue):
    import numpy as np

    from data.loader import load_lottery
    from ev.selection import _rand_tickets

    data = load_lottery(lottery)
    rec = next(r for r in data.records if str(r.期号) == issue)
    tickets = _rand_tickets(lottery, 30, np.random.default_rng(11))
    for tgt in ({"kind": "grade", "grade": "四等"},
                {"kind": "grade", "grade": "六等"},
                {"kind": "payout", "min_payout": 10.0}):
        res = TT.resolve_conditions(lottery, tgt, issue)
        for t in tickets:
            g = TT.grade_of_ticket(lottery, t, rec.zone_numbers or {}, issue)
            want = g != "未中" and g in res["grades"]
            assert TT.target_is_hit(lottery, t, rec.zone_numbers or {}, issue, tgt) is want


def test_target_is_hit_accepts_preresolved():
    """回测里逐注复用已解析结果，行为不变（只是省重算）。"""
    from data.loader import load_lottery

    data = load_lottery("双色球")
    rec = next(r for r in data.records if str(r.期号) == "26120")
    res = TT.resolve_conditions("双色球", "四等", "26120")
    ticket = {"红球": rec.zone_numbers["红球"], "蓝球": rec.zone_numbers["蓝球"]}
    assert TT.target_is_hit("双色球", ticket, rec.zone_numbers, "26120", res) is True
    assert TT.target_is_hit("双色球", ticket, rec.zone_numbers, "26120", "四等") is True


# ---------------------------------------------------------------- 通道键
@pytest.mark.parametrize("grade,n", [("四等", 22), ("五等", 42), ("六等", 64)])
def test_ssq_channel_counts(grade, n):
    """AGG 折叠后的每票通道数（手算：见模块文档的 h 分档表）。"""
    t = {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}
    conds = TT.grade_conditions_at_or_above("双色球", grade, "26120")
    keys = TT.coverage_keys_of(t, "双色球", conds, "26120")
    assert len(keys) == len(set(keys)) == n


def test_dlt_channel_count():
    """大乐透 ≥四等：h=4 需按内层坐标展开（j∈{1,2}，21 个后区坐标 × 5 个 4-子集），
    h=5 全内层类达标 ⇒ 折叠成 1 个 AGG 键。合计 106。"""
    t = {"红球": [1, 2, 3, 4, 5], "蓝球": [1, 2]}
    conds = TT.grade_conditions_at_or_above("大乐透", "四等", "26120")
    keys = TT.coverage_keys_of(t, "大乐透", conds, "26120")
    assert len(keys) == len(set(keys)) == 106


def test_agg_keys_are_inner_independent():
    """≥四等里 h=5/h=6 是 AGG ⇒ 换蓝球后这 7 个键完全不变；h=4 是 SPEC ⇒ 全变。"""
    conds = TT.grade_conditions_at_or_above("双色球", "四等", "26120")
    a = set(TT.coverage_keys_of({"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}, "双色球", conds, "26120"))
    b = set(TT.coverage_keys_of({"红球": [1, 2, 3, 4, 5, 6], "蓝球": [8]}, "双色球", conds, "26120"))
    assert len(a & b) == 7
    assert len(a - b) == 15 and len(b - a) == 15


def test_no_channel_key_collision_across_h():
    """不同 h 档的键不得相撞（编码偏移要够宽）。"""
    from itertools import combinations
    import numpy as np

    rng = np.random.default_rng(3)
    for lot, k, hi in (("双色球", 6, 33), ("大乐透", 5, 35)):
        conds_all = []
        from ev.profit.rules import rules_for
        conds_all = sorted(rules_for(lot).grade_table)
        for _ in range(20):
            reds = sorted(rng.choice(np.arange(1, hi + 1), size=k, replace=False).tolist())
            t = {"红球": reds, "蓝球": [1] if lot == "双色球" else [1, 2]}
            keys = TT.coverage_keys_of(t, lot, conds_all, "26120")
            assert len(keys) == len(set(keys))


def test_channel_weights_are_an_upper_bound():
    """Σ单票通道权重 ≥ 单注解析概率（通道互相重叠 ⇒ 上界，不是等式）。"""
    for lot, grade, issue in (("双色球", "四等", "26120"), ("大乐透", "四等", "26120")):
        from ev.profit.rules import rules_for
        rules = rules_for(lot, issue)
        conds = TT.grade_conditions_at_or_above(lot, grade, issue)
        t = ({"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]} if lot == "双色球"
             else {"红球": [1, 2, 3, 4, 5], "蓝球": [1, 2]})
        nkeys = len(TT.coverage_keys_of(t, lot, conds, issue))
        # 用平均权重粗算上界（每档权重不同，故上界一定 ≥ 解析概率）
        p = TT.single_ticket_probability(lot, conds, issue)
        # 逐一档把「键数 × 该档权重」加起来
        by_h = {}
        for h, j in conds:
            by_h.setdefault(h, set()).add(j)
        all_j = set(rules.inner_multiplicity)
        total = 0.0
        for h, js in by_h.items():
            nsub = math.comb(rules.outer_width, h)
            if js >= all_j:
                total += nsub * TT.channel_weight(lot, h, False, issue)
            else:
                ncoord = sum(1 for c in TT._draw_coords(TT._space(lot, issue), lot, issue)
                             if TT._inner_class(lot, TT._inner_of(t, lot), c) in js)
                total += nsub * ncoord * TT.channel_weight(lot, h, True, issue)
        assert nkeys > 0
        assert total >= p * (1 - 1e-9), f"{lot}: 上界 {total} 小于解析概率 {p}"


def test_digital_lottery_single_channel():
    """福彩3D 直选：只有 (3,0) 一格 ⇒ 1 个 AGG 通道。"""
    conds = TT.resolve_conditions("福彩3D", "直选")["conditions"]
    assert conds == [(3, 0)]
    keys = TT.coverage_keys_of({"号码": [1, 2, 3]}, "福彩3D", conds)
    assert len(keys) == 1


def test_upper_bound_note_is_registered():
    """诚实边界常量必须存在且写明「上界」二字（供文档/CLI 直接引用）。"""
    assert "上界" in TT.UPPER_BOUND_NOTE
