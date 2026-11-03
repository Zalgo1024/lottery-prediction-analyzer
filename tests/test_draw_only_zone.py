# -*- coding: utf-8 -*-
"""draw_only / 非对称开奖分区的票面契约。

背景（2026-11-02 取证）：
  两类新彩种的「票面宽度 ≠ 开奖宽度」，处理方式不同，但都必须保证
  **票面只含该选的那些号**：

  1. 七乐彩：基本号 choose=7 / draw=7，特别号是 **draw_only**（choose=0 / draw=1）
     —— 特别号只在开奖里出现，票面不选。
  2. 快乐8：基本号 choose=10 / draw=20 —— 同一分区，票面 10 个、开奖 20 个
     （10 个玩法由票面宽度反向决定，见 `ev.profit.rules.kl8_play_of_width`）。

★为什么必须锁：`_pad_to_groups` 的空池兜底旧实现写 `n = max(1, choose or 1)`，
  把 choose=0 **强行提成 1** ⇒ 七乐彩补票时产出「基本号 7 + 特别号 1」的**八码票面**。
  七乐彩只有 7 个号，这既是无效票，结算还会拿 8 个号去比开奖号。
  正常出号时池子够用、触发不到这条路径，所以必须直接压空池才能测到。
"""
from __future__ import annotations

import pytest

from data.schema import get_schema
from prediction.engine import PredictSet, _pad_to_groups

# 票面宽度表：彩种 -> (票面分区名, 票面该选几个, 该区开奖开几个)
TICKET_WIDTH = {
    "七乐彩": ("基本号", 7, 7),
    "快乐8": ("基本号", 10, 20),
}


def _draw_only_names(lottery: str) -> list[str]:
    return [z.name for z in get_schema(lottery).zones if getattr(z, "draw_only", False)]


# ============================================================
# 一、schema 前提
# ============================================================

def test_qlc_special_zone_is_draw_only():
    """七乐彩特别号：票面不选号（choose=0），但开奖真的开 1 个。"""
    draws = {z.name: z for z in get_schema("七乐彩").zones}
    assert "特别号" in draws, f"七乐彩应有特别号分区，实得 {list(draws)}"
    z = draws["特别号"]
    assert z.choose == 0, "票面不选号 ⇒ choose 必须为 0"
    assert z.draw_count == 1, "特别号必须真的出现在开奖里"
    assert getattr(z, "draw_only", False), "必须标记 draw_only，否则会被当成票面分区"


def test_kl8_zone_draws_more_than_ticket():
    """快乐8：同一分区票面选 10、开奖开 20（不是 draw_only）。"""
    z = get_schema("快乐8").zones[0]
    assert z.choose == 10 and z.draw_count == 20, (z.choose, z.draw_count)
    assert not getattr(z, "draw_only", False)


# ============================================================
# 二、★核心回归：补票路径不许凭空造号
# ============================================================

@pytest.mark.parametrize("lottery", sorted(TICKET_WIDTH))
def test_pad_to_groups_never_fills_draw_only_zone(lottery):
    """空池兜底补票：票面宽度必须精确等于 choose，draw_only 分区一个号都不能有。"""
    base_name, base_n, _draw_n = TICKET_WIDTH[lottery]
    out, stats = _pad_to_groups([], 5, get_schema(lottery), lottery_name=lottery)
    assert len(out) == 5
    assert stats["随机补齐"] == 5
    for ps in out:
        zones = ps.to_dict()["号码"]
        assert len(zones[base_name]) == base_n, zones
        for nm in _draw_only_names(lottery):
            assert not zones.get(nm), (
                f"{lottery} 补票给 draw_only 分区「{nm}」塞了号：{zones.get(nm)}"
                f" ⇒ 票面变成 {base_name}{len(zones.get(base_name, []))}+{nm}{len(zones.get(nm, []))}"
            )
        # 票面总注数也必须是 1 注该彩种的正常宽度（不许出现 8 码这种超宽票面）
        assert sum(len(v) for v in zones.values()) == base_n, zones


def test_pad_to_groups_keeps_red_blue_path_untouched():
    """回归护栏：红/蓝彩种本来就没有 draw_only 分区，补票不许被改窄。"""
    out, _ = _pad_to_groups([], 3, get_schema("双色球"), lottery_name="双色球")
    assert len(out) == 3
    for ps in out:
        d = ps.to_dict()
        assert len(d["红球"]) == 6 and len(d["蓝球"]) == 1, d["号码"]


# ============================================================
# 三、生产出号：`号码` 里不出现 draw_only 键
# ============================================================

@pytest.mark.parametrize("lottery", sorted(TICKET_WIDTH))
def test_prediction_has_no_draw_only_key(lottery):
    """空键会被前端/报告渲染成一个永远空的分区列，票面里不该有它。"""
    from prediction.engine import predict

    base_name, base_n, _ = TICKET_WIDTH[lottery]
    res = predict(lottery, groups=3, record_pending=False)
    assert res["预测号码"], "出号为空"
    for ps in res["预测号码"]:
        zones = ps["号码"]
        assert len(zones[base_name]) == base_n, zones
        for nm in _draw_only_names(lottery):
            assert nm not in zones, f"{lottery} 票面不该带「{nm}」键，实得 {zones}"


# ============================================================
# 四、PredictSet 层：空分区即非票面
# ============================================================

def test_predict_set_drops_empty_zones():
    ps = PredictSet(zones={"基本号": [1, 2, 3], "特别号": []})
    assert ps.to_dict()["号码"] == {"基本号": [1, 2, 3]}
    assert ps.zones == {"基本号": [1, 2, 3]}


def test_predict_set_keeps_red_blue_view_consistent():
    """红/蓝视图必须跟着 self.zones 走，不能出现「视图有、分区没有」的自相矛盾。"""
    ps = PredictSet(reds=[1, 2], blues=[])
    d = ps.to_dict()
    assert d["红球"] == [1, 2]
    assert "蓝球" not in d
    assert d["号码"] == {"红球": [1, 2]}
