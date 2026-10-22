# -*- coding: utf-8 -*-
"""tests/test_profit_ordered_tickets.py —— B1：排列型（七星彩/数字型）票面契约。

曾有的三个 bug（都由本文件锁住）：
1. `outer_universe` 排列型分支把数组建成 1 维却按 2 维写 ⇒ IndexError
   （七星彩/数字型在 hit_histogram 直接崩，等于没接入）；
2. `tickets_to_outer` 对排列型也 `sorted` ⇒ 「123…」与「321…」混同，
   命中数虚高（奖级按**同位**命中数判，位序即信息）；
3. `_overlap_ok` 对排列型用集合交集 ⇒ 位序不同的票被判「重叠 3」。

另锁：七星彩票面 7 位必须切前 6 位作外层（第 7 位是内层）。
"""
import math

import numpy as np
import pytest

from ev.profit import evaluate as EV
from ev.profit import outcomes as O
from ev.profit import payouts as P
from ev.profit import rules as R
from ev.profit import select as SEL

# 各彩种真实历史期号（pit.as_of 需要可解析期号；用「最新前一期」保证有历史）
ISSUES = {"排列3": "9357", "排列5": "9357", "福彩3D": "2026279", "七星彩": "9152"}


# ---------------------------------------------------------------- 枚举内核

@pytest.mark.parametrize("name,width,base", [("排列3", 3, 10), ("排列5", 5, 10),
                                             ("福彩3D", 3, 10), ("七星彩", 6, 10)])
def test_outer_universe_ordered_shape(name, width, base):
    """排列型枚举必须是 (n^w, w) 的 2 维矩阵（1 维是曾崩过的 bug）。"""
    r = R.rules_for(name)
    assert r.outer_choose is False
    U = O.outer_universe(name)
    assert U.shape == (base ** width, width)
    assert U.dtype == np.int16
    assert int(U.min()) == 0 and int(U.max()) == 9


def test_outer_universe_lexicographic_order():
    """位串按字典序展开（最低位变化最快）—— 顺序即索引语义，不许变。"""
    U = O.outer_universe("排列3")
    assert U[0].tolist() == [0, 0, 0]
    assert U[1].tolist() == [0, 0, 1]
    assert U[9].tolist() == [0, 0, 9]
    assert U[10].tolist() == [0, 1, 0]
    assert U[-1].tolist() == [9, 9, 9]


# ---------------------------------------------------------------- 保序

def test_tickets_to_outer_preserves_order_for_ordered_types():
    """★ 排列型保序：位序不同的两张票不许被混同。"""
    a = [{"号码": [1, 2, 3, 4, 5, 6, 7]}]
    b = [{"号码": [6, 5, 4, 3, 2, 1, 7]}]
    ra, rb = O.tickets_to_outer("七星彩", a), O.tickets_to_outer("七星彩", b)
    assert ra.shape == (1, 6) and rb.shape == (1, 6)     # 只取前 6 位
    assert not np.array_equal(ra, rb), "排序把位序抹掉了"


def test_tickets_to_outer_sorts_combination_types():
    """组合型仍规范化排序（双色球乱序输入 → 升序输出）。"""
    rows = O.tickets_to_outer("双色球", [{"红球": [6, 5, 4, 3, 2, 1], "蓝球": [1]}])
    assert rows.tolist() == [[1, 2, 3, 4, 5, 6]]


def test_qxc_ticket_slices_first_six_digits():
    """七星彩票面 7 位：外层只取前 6 位（第 7 位是内层）。"""
    rows = O.tickets_to_outer("七星彩", [{"号码": [1, 2, 3, 4, 5, 6, 7]}])
    assert rows.shape == (1, 6) and rows[0].tolist() == [1, 2, 3, 4, 5, 6]


# ---------------------------------------------------------------- 命中直方图

@pytest.mark.parametrize("name,issue", [("排列3", "9357"), ("福彩3D", "2026279")])
def test_digit_histogram_matches_positional_probabilities(name, issue):
    """★ κ=0 直方图必须退化到**同位命中**组合概率（不是多重集合概率）。"""
    tk = [{"号码": [1, 2, 3]}] if name == "排列3" else [{"号码": [1, 2, 3]}]
    H = O.hit_histogram(name, tk)
    w = 3
    assert H.shape == (1000, w + 1)
    ph = H.sum(axis=0) / 1000.0
    for h in range(w + 1):
        want = math.comb(w, h) * 9 ** (w - h) / 1000
        assert ph[h] == pytest.approx(want, abs=1e-12), f"{name} h={h}"


def test_qxc_histogram_matches_positional_probabilities():
    """七星彩外层 10^6 的同位命中分布。"""
    H = O.hit_histogram("七星彩", [{"号码": [1, 2, 3, 4, 5, 6, 7]}])
    assert H.shape == (10 ** 6, 7)
    ph = H.sum(axis=0) / (10 ** 6)
    for h in range(7):
        want = math.comb(6, h) * 9 ** (6 - h) / 10 ** 6
        assert ph[h] == pytest.approx(want, abs=1e-15), f"h={h}"


def test_order_actually_changes_hits():
    """保序的行为后果：票 321 对开奖 123 只有 1 位相同（排序 bug 下会是 3 位）。"""
    H = O.hit_histogram("排列3", [{"号码": [3, 2, 1]}])
    row123 = int("123")                      # 字典序索引：123 → 第 123 行
    assert H[row123].tolist() == [0, 1, 0, 0]
    row321 = int("321")
    assert H[row321, 3] == 1 and H[row321].sum() == 1


# ---------------------------------------------------------------- 重叠判据

def test_overlap_ordered_uses_position_matches():
    """排列型重叠 = 同位相同数（集合交集会把位序不同的票误判为重叠 3）。"""
    assert SEL._overlap_ok([1, 2, 3], [[3, 2, 1]], 1, ordered=True)
    assert not SEL._overlap_ok([1, 2, 3], [[1, 2, 3]], 2, ordered=True)


def test_overlap_combination_uses_set_intersection():
    s = SEL._overlap_ok([1, 2, 3, 4, 5, 6], [[6, 5, 4, 3, 2, 1]], 6)
    assert s                                  # 全同号，交集 6 ≤ 6
    assert not SEL._overlap_ok([1, 2, 3], [[1, 2, 9]], 1)   # 交集 2 > 1


# ---------------------------------------------------------------- 端到端

@pytest.mark.parametrize("name", list(ISSUES))
def test_select_end_to_end_ordered(name):
    """数字型/七星彩选号端到端：出数正确、票面位数正确。"""
    ctx = P.build_ctx(name, ISSUES[name])
    sel = SEL.select_profit_tickets(name, 5, ctx, kappa=0.0, seed=0,
                                    pool_size=200, overlap_cap=3)
    tks = sel["票"]
    assert len(tks) == 5
    wdt = R.rules_for(name).outer_width
    for t in tks:
        assert len(t["号码"]) == (wdt + 1 if name == "七星彩" else wdt)
        assert all(0 <= d <= 9 for d in t["号码"])


def test_qxc_exact_metrics_kappa0_matches_analytic_p_any():
    """七星彩 κ=0 的 P(≥1) == 解析值（六等面很宽，不是 1/15e6）。"""
    ctx = P.build_ctx("七星彩", ISSUES["七星彩"])
    m = EV.exact_metrics("七星彩", [{"号码": [1, 2, 3, 4, 5, 6, 7]}], ctx, kappa=0.0)
    # y=1（第7位中，1/15）恒中；或 y=0 且前6位命中 x>=3
    tail = (math.comb(6, 3) * .1 ** 3 * .9 ** 3 + math.comb(6, 4) * .1 ** 4 * .9 ** 2
            + math.comb(6, 5) * .1 ** 5 * .9 + .1 ** 6)
    expect = 1 / 15 + (14 / 15) * tail
    assert m["P至少中一注"] == pytest.approx(expect, abs=1e-12), expect
    assert m["净EV单注"] < 0, "κ=0 净EV 必须为负（固定奖彩种返奖率 < 100%）"
