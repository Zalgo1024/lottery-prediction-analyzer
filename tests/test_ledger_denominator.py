# -*- coding: utf-8 -*-
"""逐期账本的分母与金额口径（2026-10-31 任务 1 的牙齿）。

这组测试只锁四件事，每一件都对应一种「数字看起来正常但结论是错的」：

1. **空期必须进分母**。只数「有号码的期」，等价于把没出号的期从样本里删掉 ——
   而「没出号」恰恰经常发生在预算耗尽/停发/数据缺失的时候，正是最该计入的期。
2. **中奖期占比的分母是参与期数**，不是开奖期数、也不是中奖注数。
   三个数含义不同，混用会同时制造乐观与悲观两种假象。
3. **金额未知不参与求和**。`prize_payout` 拿不到浮动奖当期派奖时返回 `金额=None`；
   当 0 计入会让合计系统性偏低，按上限计入会虚报。唯一正确是排除 + 单独计数。
4. **回撤按累计净盈亏算**，且幂等重算不会越跑越大。
"""

from __future__ import annotations

import json

import pytest

from data import ledger as LG


def _row(issue, *, n=10, cost=20.0, gross=0.0, hits=0, unknown=0):
    return {"期号": issue, "参与": True, "参与原因": "已出号",
            "实际注数": n, "成本": cost, "税前奖金": gross,
            "中奖注数": hits, "中奖期": hits > 0, "金额未知注数": unknown}


# ------------------------------------------------------------------ 分母
def test_empty_periods_enter_denominator():
    """★★ 3 期开奖里只有 2 期出号 ⇒ 参与 2 / 未参与 1 / 开奖 3。"""
    rows = [_row("100"), _row("101")]
    full = LG.fill_empty(rows, "排列5", issues=["100", "101", "102"])
    s = LG.summary_of(LG.recompute(full))

    assert s["开奖期数"] == 3
    assert s["参与期数"] == 2
    assert s["未参与期数"] == 1
    # 空期的成本必须是 0（没买就是没花钱），不能拿配置注数去"估算"
    empty = [r for r in full if not r["参与"]]
    assert len(empty) == 1 and empty[0]["成本"] == 0.0
    assert "未出号" in empty[0]["参与原因"]


def test_hit_ratio_uses_participated_not_total():
    """★ 1 期中奖 / 2 期参与 = 0.5；不能因为总开奖 3 期就写成 0.333。"""
    rows = [_row("100", gross=10.0, hits=1), _row("101")]
    full = LG.recompute(LG.fill_empty(rows, "排列5", issues=["100", "101", "102"]))
    s = LG.summary_of(full)

    assert s["参与期数"] == 2
    assert s["中奖期数"] == 1
    assert s["中奖期占比"] == pytest.approx(0.5)


def test_no_participation_gives_none_not_zero():
    """一期都没参与时占比是 `None`（不知道），不是 0%（知道没中）——两者结论相反。"""
    full = LG.recompute(LG.fill_empty([], "排列5", issues=["1", "2"]))
    s = LG.summary_of(full)
    assert s["参与期数"] == 0
    assert s["中奖期占比"] is None
    assert s["净收益率"] is None


# ------------------------------------------------------------------ 金额口径
def test_amount_unknown_excluded_from_sum():
    """★★ 一注拿到金额、一注金额未知 ⇒ 合计只算前者，另记「未知 1 注」。"""
    recs = [
        {"期号": "1", "批次号": "b", "批次规模": 2, "中奖等级": "六等", "税前奖金": 5.0},
        {"期号": "1", "批次号": "b", "批次规模": 2, "中奖等级": "二等", "金额未知": True},
    ]
    rows = LG.build_rows("排列5", records=recs)
    assert len(rows) == 1
    r = rows[0]
    assert r["税前奖金"] == 5.0, "金额未知的那注不得按 0 计入（会偏低）也不得猜（会虚报）"
    assert r["金额未知注数"] == 1
    assert r["成本"] == 4.0            # 2 注 × 2 元：成本照算，未知的是**奖金**
    assert r["中奖注数"] == 2


def test_missing_amount_field_counts_as_unknown():
    """没填过金额（历史回填前）也要记成「未知」，不能静默当 0 —— 否则账本会显示
    「中奖 181 注、奖金 0 元」这种自相矛盾的读数。"""
    recs = [{"期号": "1", "批次号": "b", "批次规模": 1, "中奖等级": "六等"}]
    r = LG.build_rows("排列5", records=recs)[0]
    assert r["金额未知注数"] == 1
    assert r["税前奖金"] == 0.0


def test_net_is_gross_minus_tax_minus_cost():
    rows = [_row("1", n=10, cost=20.0, gross=100.0, hits=1)]
    r = LG.recompute(rows)[0]
    assert r["税"] == pytest.approx(0.0)     # 100 元未达起征点（10000）
    assert r["实得"] == pytest.approx(100.0)
    assert r["净盈亏"] == pytest.approx(80.0)


def test_tax_kicks_in_above_threshold():
    """偶然所得税是**全额 20%**（超过起征点即全额计税），不是超额累进。"""
    rows = [_row("1", n=10, cost=20.0, gross=20000.0, hits=1)]
    r = LG.recompute(rows)[0]
    assert r["税"] == pytest.approx(20000.0 * 0.20)
    assert r["实得"] == pytest.approx(20000.0 - 4000.0)


# ------------------------------------------------------------------ 回撤
def test_drawdown_from_cumulative_net():
    rows = [_row("1", gross=30.0, cost=10.0),    # 净 +20 ⇒ 累计 20
            _row("2", gross=0.0, cost=10.0),     # 净 −10 ⇒ 累计 10
            _row("3", gross=0.0, cost=10.0)]     # 净 −10 ⇒ 累计 0
    out = LG.recompute(rows)
    assert [r["累计净盈亏"] for r in out] == [20.0, 10.0, 0.0]
    assert out[-1]["最大回撤"] == pytest.approx(20.0)
    assert out[-1]["最大回撤持续期数"] == 2


def test_recompute_is_idempotent():
    rows = [_row("1", gross=30.0, cost=10.0), _row("2", cost=10.0)]
    a = LG.recompute(json.loads(json.dumps(rows)))
    b = LG.recompute(a)
    for x, y in zip(a, b):
        assert (x["净盈亏"], x["累计净盈亏"], x["最大回撤"]) == \
               (y["净盈亏"], y["累计净盈亏"], y["最大回撤"])


# ------------------------------------------------------------------ 落盘
def test_rebuild_writes_one_line_per_period(tmp_path):
    """落盘必须一期一行（含空期）—— 账本按行读，缺行就等于缺期。"""
    rows = LG.recompute(LG.fill_empty([_row("100")], "排列5", issues=["100", "101"]))
    LG._write("排列5", rows)

    p = LG.ledger_path("排列5")
    lines = [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) == 2
    assert all("期号" in json.loads(ln) for ln in lines)
    assert LG.load("排列5")[1]["参与"] is False


def test_ledger_dir_is_isolated_by_conftest():
    """★ 账本目录必须被根 conftest 重定向（`_TARGETS` 里的 LEDGER_DIR）。

    否则任何一次 rebuild 都会把真实 `training/ledger/*.jsonl` 覆盖成测试残本，
    而那正是复盘时唯一的成本/净盈亏证据。
    """
    assert ".pytest_tmp" in str(LG.LEDGER_DIR) or "pytest" in str(LG.LEDGER_DIR)
