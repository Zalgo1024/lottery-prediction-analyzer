"""当期建议的**票面形状**回归（2026-11-04）。

★ 这条测试锁的是一个真实踩过的坑：
  `pipeline/step6_ev._advice_via_profit` 原先把所有彩种的票面都包成
  `{"号码": {区名: [...]}}`，而 `ev.payout.normalize_ticket` 对**数字型**
  （排列3 / 福彩3D / 排列5 / 七星彩）要求**直接**给 `{区名: [...]}`。
  多包一层 "号码" 后，那个 dict 被当成「一个取值」去做 `int()`，于是抛
  `TypeError: int() argument must be ... not 'dict'`，函数返回 None，
  看板的「当期建议」对四个数字彩种退化成一句错误 —— 而**日志里只是一行 warning**，
  接口照样返回 200，界面看着"有内容"。

  七乐彩 / 快乐8 反而是 `{"号码": {...}}` 才保留号码，所以不能"统一成一种形状"。

为什么放在这里而不是塞进 step6 的测试：本文件是纯函数级、不依赖真实开奖数据，
`_advice_via_profit(..., data=None)` 的 `data` 只用于 `getattr(data, "records", None)`，
传 None 安全。
"""

from __future__ import annotations

import pytest

from pipeline.step6_ev import _advice_via_profit

DIGITAL = ("排列3", "福彩3D", "排列5", "七星彩")
SHAPED = ("七乐彩", "快乐8")


@pytest.mark.parametrize("lottery", DIGITAL + SHAPED)
def test_profit_advice_returns_real_numbers_not_error(lottery):
    """六个彩种都必须给出真实读数，而不是「因为票面解析失败退回错误」。"""
    adv = _advice_via_profit(lottery, None)
    assert adv is not None, f"{lottery} 的 payout_eff 票面解析失败（返回 None）"
    # 真实读数必须有这几项，且期望是数字（不是 None、不是错误字符串）
    for k in ("单注期望", "固定奖期望", "浮动奖期望"):
        assert k in adv, f"{lottery} 缺字段 {k}"
        assert isinstance(adv[k], (int, float)), f"{lottery} 的 {k} 不是数字：{adv[k]!r}"


@pytest.mark.parametrize("lottery", DIGITAL)
def test_digital_ticket_shape_is_flat_zones(lottery):
    """数字型：直接 `{区名: [...]}` 可用，包一层 `号码` 必须失败。

    ★ 反过来钉：如果有人"顺手统一"成 `{"号码": zones}`，这条会立刻红。
    """
    from ev.payout import payout_eff
    from data.schema import get_schema

    zones = {}
    for z in get_schema(lottery).zones:
        n = int(getattr(z, "choose", 0) or 0)
        if n <= 0:
            continue
        zones[z.name] = list(range(int(z.min), int(z.min) + n))

    flat = payout_eff(lottery, zones)
    assert "error" not in flat, f"{lottery} 直接给区名应当可用：{flat.get('error')}"
    assert "digits" not in flat.get("error", "")

    wrapped = payout_eff(lottery, {"号码": zones})
    assert "error" in wrapped, "包一层 `号码` 在数字型上本该失败，行为变了要重新确认下游"


@pytest.mark.parametrize("lottery", SHAPED)
def test_shaped_ticket_keeps_numbers(lottery):
    """七乐彩 / 快乐8 走 `{"号码": zones}` —— 两种写法都能算，但只有这种能保住号码。"""
    from ev.payout import payout_eff
    from data.schema import get_schema

    zones = {}
    for z in get_schema(lottery).zones:
        n = int(getattr(z, "choose", 0) or 0)
        if n <= 0:
            continue
        zones[z.name] = list(range(int(z.min), int(z.min) + n))

    r = payout_eff(lottery, {"号码": zones})
    assert "error" not in r
    assert r.get("号码"), f"{lottery} 的号码被丢掉了（下游无法追溯票面）"
