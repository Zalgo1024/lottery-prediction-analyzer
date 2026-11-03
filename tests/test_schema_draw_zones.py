# -*- coding: utf-8 -*-
"""Zone 模型扩展：开奖数 ≠ 票面数（七乐彩/快乐8 接入的地基）。

★ 本文件锁死一件事：**既有 6 个彩种的配置、行为、序列化结果逐字节不变**。
  新增的 `Zone.draw` / `Zone.draw_only` 只在显式配置时才生效，`draw is None`
  时 draw_count 恒等于 choose，因此旧路径全部走原分支。
"""
from __future__ import annotations

import pytest

from config import LOTTERY_CONFIG
from data.schema import LotterySchema, Zone, schema_from_cfg

SIX = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]

# 七乐彩形态：7 个基本号（既开也选）+ 1 个特别号（只开不选）
QLC_CFG = {
    "zones": [
        {"name": "基本号", "min": 1, "max": 30, "choose": 7},
        {"name": "特别号", "min": 1, "max": 30, "choose": 0, "draw": 1, "draw_only": True},
    ],
    "payout": {"cost": 2},
}

# 快乐8 形态：开奖摇 20 个，票面按玩法选 1~10 个
KL8_CFG = {
    "zones": [{"name": "基本号", "min": 1, "max": 80, "choose": 10, "draw": 20}],
    "plays": {"选一": 1, "选五": 5, "选十": 10},
    "default_play": "选十",
    "payout": {"cost": 2},
}


# ---------------------------------------------------------------- 默认值语义
def test_draw_defaults_to_choose():
    z = Zone("红球", 1, 33, 6)
    assert z.draw is None
    assert z.draw_count == 6
    assert z.ticket_count == 6


def test_draw_only_zone_has_zero_ticket_count():
    z = Zone("特别号", 1, 30, 0, draw=1, draw_only=True)
    assert z.draw_count == 1
    assert z.ticket_count == 0


def test_draw_may_exceed_choose():
    """快乐8：开 20 / 票选 10 —— 两个数必须能分开。"""
    z = Zone("基本号", 1, 80, 10, draw=20)
    assert z.draw_count == 20
    assert z.ticket_count == 10


# ---------------------------------------------------------------- 6 彩种零改动
@pytest.mark.parametrize("lot", SIX)
def test_six_lotteries_to_cfg_is_unchanged(lot):
    """既有彩种的 to_cfg() 必须仍然只产出 red/blue 那套键（不多出 zones/plays）。"""
    s = schema_from_cfg(LOTTERY_CONFIG[lot], lot)
    got = s.to_cfg()
    exp = {}
    if s.red_zone:
        exp["red_range"] = s.red_zone.range_tuple()
        exp["red_count"] = s.red_zone.choose
    if s.blue_zone:
        exp["blue_range"] = s.blue_zone.range_tuple()
        exp["blue_count"] = s.blue_zone.choose
    assert got == exp
    assert "zones" not in got and "plays" not in got


@pytest.mark.parametrize("lot", SIX)
def test_six_lotteries_draw_equals_choose(lot):
    s = schema_from_cfg(LOTTERY_CONFIG[lot], lot)
    for z in s.zones:
        assert z.draw_count == z.choose
        assert z.ticket_count == z.choose


@pytest.mark.parametrize("lot", SIX)
def test_six_lotteries_total_choose_unchanged(lot):
    s = schema_from_cfg(LOTTERY_CONFIG[lot], lot)
    assert s.total_choose == sum(z.choose for z in s.zones)


# ---------------------------------------------------------------- 新形态
def test_qlc_total_choose_excludes_special_zone():
    """七乐彩票面只有 7 个号 —— 若把特别号算进 total_choose 会变成 8，
    而 8 会静默漂移 web/utils 的命中率口径。"""
    s = schema_from_cfg(QLC_CFG, "七乐彩")
    assert s.total_choose == 7
    assert s.total_draw == 8


def test_kl8_draw_and_pick_differ():
    s = schema_from_cfg(KL8_CFG, "快乐8")
    assert s.total_draw == 20
    assert s.total_choose == 10


def test_picks_for_play():
    s = schema_from_cfg(KL8_CFG, "快乐8")
    assert s.picks_for("选一") == {"基本号": 1}
    assert s.picks_for("选十") == {"基本号": 10}
    assert s.picks_for() == {"基本号": 10}      # 默认玩法
    assert s.pick_count("选五") == 5


def test_picks_for_rejects_unknown_play():
    s = schema_from_cfg(KL8_CFG, "快乐8")
    with pytest.raises(ValueError, match="无此玩法"):
        s.picks_for("选十一")


def test_picks_for_without_plays_is_choose():
    s = schema_from_cfg(LOTTERY_CONFIG["双色球"], "双色球")
    assert s.picks_for() == {"红球": 6, "蓝球": 1}


def test_multi_zone_play_raises_instead_of_guessing():
    """多区玩法没有定义票面分配规则 —— 与其猜一套，不如显式失败。"""
    s = LotterySchema("怪彩种", [Zone("甲", 1, 10, 3), Zone("乙", 1, 10, 2)],
                      plays={"选五": 5}, default_play="选五")
    with pytest.raises(NotImplementedError, match="多区玩法"):
        s.picks_for("选五")


# ---------------------------------------------------------------- 往返一致
def test_zone_to_cfg_roundtrip():
    z = Zone("特别号", 1, 30, 0, draw=1, draw_only=True)
    cfg = z.to_cfg()
    assert cfg == {"name": "特别号", "min": 1, "max": 30, "choose": 0,
                   "draw": 1, "draw_only": True}
    assert Zone(**cfg) == z


def test_zone_to_cfg_omits_defaults():
    """既有彩种的 zone 往返不该多出任何键。"""
    z = Zone("红球", 1, 33, 6)
    assert z.to_cfg() == {"name": "红球", "min": 1, "max": 33, "choose": 6}


def test_schema_roundtrip_preserves_plays():
    s = schema_from_cfg(KL8_CFG, "快乐8")
    back = s.to_cfg()
    assert back["plays"] == KL8_CFG["plays"]
    assert back["default_play"] == "选十"
    s2 = schema_from_cfg(back, "快乐8")
    assert s2.total_draw == s.total_draw
    assert s2.pick_count() == s.pick_count()


# ---------------------------------------------------------------- validate 用开奖数
def test_validate_expects_draw_count(monkeypatch):
    """开奖记录要 8 个号（7 基本 + 1 特别），不能按票面 7 个去判。"""
    from data.schema import DrawRecord

    monkeypatch.setitem(LOTTERY_CONFIG, "临时七乐彩", QLC_CFG)
    ok = DrawRecord(期号=1, zone_numbers={"基本号": list(range(1, 8)), "特别号": [9]})
    assert ok.validate("临时七乐彩") == []

    bad = DrawRecord(期号=2, zone_numbers={"基本号": list(range(1, 8))})
    errs = bad.validate("临时七乐彩")
    assert any("特别号" in e for e in errs)
