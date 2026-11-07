# -*- coding: utf-8 -*-
"""期次归属（`settle_anchor` / `resolve_settlement_draw`）的契约锁。

为什么锁它
----------
「这张票算哪一期」一旦错，后面全错：结算拿错期的开奖号、命中率记到别人头上、
甚至开奖后生成的票被当成真预测。三条判据必须同时成立，缺一条就会静默出错：

  ① 截止时刻（开奖日 21:00）之后出的号，不能算进当期；
  ② 顺延只能沿开奖日历（含休市），不能手写「明天」；
  ③ 找不到目标期就返回 None（待开奖），**绝不回落到最新一期**。

★ 测试不拿 `datetime.now()` 当基准：一律显式传 `when`，期望值由 `predict_target()`
  当场推出，不写死日期（写死就会在下一次开奖后变红，且红得没有意义）。
"""
from datetime import date, datetime, time, timedelta

import pytest

from data.feedback import (DRAW_CUTOFF, _find_draw_by_date, predict_target,
                           resolve_settlement_draw, settle_anchor)
from data.holiday import is_draw_day
from data.loader import load_lottery


def _latest(lottery):
    recs = load_lottery(lottery).records
    return max(recs, key=lambda r: r.期号 or 0)


# ---------------------------------------------------------------- ① 截止时刻
@pytest.mark.parametrize("lottery", ["快乐8", "双色球", "福彩3D"])
def test_before_cutoff_stays_on_target_issue(lottery):
    issue, day = predict_target(lottery)
    if issue is None or day is None:
        pytest.skip(f"{lottery} 无本地数据")
    cutoff = time.fromisoformat(DRAW_CUTOFF)
    when = datetime.combine(day, cutoff) - timedelta(minutes=1)
    a = settle_anchor(lottery, when)
    assert a["目标期号"] == issue
    assert a["目标开奖日"] == day
    assert a["顺延期数"] == 0


@pytest.mark.parametrize("lottery", ["快乐8", "福彩3D"])
def test_after_cutoff_advances_one_issue(lottery):
    """★ 21:00 之后出的号不能算进当期 —— 否则就是「开奖后生成的票」。"""
    issue, day = predict_target(lottery)
    if issue is None or day is None:
        pytest.skip(f"{lottery} 无本地数据")
    cutoff = time.fromisoformat(DRAW_CUTOFF)
    when = datetime.combine(day, cutoff) + timedelta(minutes=30)
    a = settle_anchor(lottery, when)
    assert a["顺延期数"] == 1
    assert a["目标期号"] == issue + 1
    assert a["目标开奖日"] > day
    assert "已过" in a["依据"][-1]


# ---------------------------------------------------------------- ② 日历
def test_advance_lands_on_a_draw_day():
    """顺延必须落在开奖日上（含休市跳过），不能简单 +1 天。"""
    issue, day = predict_target("快乐8")
    if issue is None:
        pytest.skip("无本地数据")
    a = settle_anchor("快乐8", datetime.combine(day, time(23, 0)))
    assert is_draw_day("快乐8", a["目标开奖日"])


def test_anchor_reports_its_own_rule():
    """依据必须自带口径，否则事后无法复核这张票凭什么算到那一期。"""
    issue, day = predict_target("双色球")
    if issue is None:
        pytest.skip("无本地数据")
    a = settle_anchor("双色球", datetime.combine(day, time(10, 0)))
    assert a["依据"] and a["口径"]
    assert "不回落" in a["口径"]


# ---------------------------------------------------------------- ③ 不回落
def test_unknown_issue_and_date_returns_none():
    """期号查不到、日期也查不到 ⇒ 待开奖；必须返回 None，不许拿最近一期顶上。"""
    rec, basis = resolve_settlement_draw(
        "双色球", {"目标期号": 999999, "预测日期": "1999-01-01"})
    assert rec is None
    assert "不回落" in basis


def test_date_fallback_only_on_exact_match():
    """老记录没有期号时按日期精确匹配；差一天就必须落空。"""
    last = _latest("双色球")
    rec, basis = resolve_settlement_draw(
        "双色球", {"目标期号": 999999, "预测日期": str(last.开奖日期)})
    assert rec is not None and basis == "日期精确匹配"

    day = date.fromisoformat(str(last.开奖日期)[:10]) - timedelta(days=1)
    rec2, _ = resolve_settlement_draw(
        "双色球", {"目标期号": 999999, "预测日期": str(day)})
    assert rec2 is None, "差一天被匹配上了 ⇒ 会拿上一期开奖结算"


def test_issue_lookup_wins_over_date():
    """期号是稳定主键：能按期号找到时，不再走日期（避免两处不一致时选错）。"""
    last = _latest("双色球")
    rec, basis = resolve_settlement_draw(
        "双色球", {"目标期号": last.期号, "预测日期": "1999-01-01"})
    assert rec is not None and basis == "期号"


def test_find_draw_by_date_is_exact_only():
    """底层按日期查找只做等值比较 —— 这是「不回落」的实现基础。"""
    last = _latest("双色球")
    assert _find_draw_by_date("双色球", str(last.开奖日期)) is not None
    assert _find_draw_by_date("双色球", "1999-01-01") is None
