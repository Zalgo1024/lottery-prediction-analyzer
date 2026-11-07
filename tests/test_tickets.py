# -*- coding: utf-8 -*-
"""票面注数与合法性（`prediction/tickets.py`）的契约锁。

为什么值得单独锁
----------------
「注数」这件事在本项目里有四个各自写入的数字：声明组数、目标注数、实际注数、
`len(预测号码)`。它们没有任何一处被校验过相等，而事故形态都是**数字对不上但不报错**：
  · 2026-11-07「改成 1w 注、重出后变回 1000 注」；
  · 七乐彩特别号 `choose=0` 曾被 `max(1, choose or 1)` 补成 1 ⇒ 八码非法票面。

本文件钉两件事：
  ① 注数公式的唯一写法（含位置型**不是** `C(池,k)` 这个反例）；
  ② 单组票面的合法性判据（区名/数量/范围/可重复性）。
"""
import pytest

from prediction.tickets import (MAX_GROUPS, batch_check, ticket_count,
                                ticket_shape, validate_group)


# ---------------------------------------------------------------- ① 注数公式
def test_single_ticket_is_one_group():
    for lot in ("双色球", "大乐透", "福彩3D", "七星彩", "七乐彩", "快乐8"):
        assert ticket_count(lot)["注数"] == 1, lot


def test_compound_lottery_is_combination_product():
    """双色球 10+3 复式：C(10,6) × C(3,1) = 210 × 3 = 630。"""
    r = ticket_count("双色球",
                     pools={"红球": list(range(1, 11)), "蓝球": [1, 2, 3]})
    assert r["注数"] == 630
    assert r["成本"] == pytest.approx(630 * 2.0)
    assert r["合法"] is True


def test_dlt_compound_uses_both_zones():
    """大乐透 7+3：C(7,5) × C(3,2) = 21 × 3 = 63。"""
    assert ticket_count("大乐透",
                        pools={"红球": list(range(1, 8)), "蓝球": [1, 2, 3]})["注数"] == 63


def test_dantuo_is_combination_of_tuo():
    """双色球 3 胆 5 拖 + 1 蓝：C(5, 6-3) = 10。"""
    r = ticket_count("双色球", dan={"红球": [1, 2, 3]},
                     tuo={"红球": [4, 5, 6, 7, 8]}, pools={"蓝球": [7]})
    assert r["注数"] == 10
    assert r["合法"] is True


def test_dantuo_needs_enough_tuo():
    """胆码占满应选数时拖码无处安放 ⇒ 显式不合法，不是「算出 0 注」了事。"""
    r = ticket_count("双色球", dan={"红球": [1, 2, 3, 4, 5, 6]},
                     tuo={"红球": [7]}, pools={"蓝球": [1]})
    assert r["注数"] == 0
    assert r["合法"] is False
    assert "胆码" in r["原因"]


def test_positional_is_product_not_combination():
    """★ 位置型：每位选 2 个 ⇒ 2^6 × 3 = 192，**不是** C(12,6)=924。

    外部参考程序在这里写成 `C(redCount,6)×蓝`，会把这张票算成 924 注。
    两者差 5 倍，而成本/额度/单期上限全部按注数算 ⇒ 一个不会报错的错数。
    """
    pools = {f"第{i}位": [1, 2] for i in range(1, 7)}
    pools["第7位"] = [0, 1, 2]
    r = ticket_count("七星彩", pools=pools)
    assert r["注数"] == 2 ** 6 * 3 == 192
    assert r["注数"] != 924
    assert r["明细"][0]["形态"] == "复式"


def test_kl8_play_changes_expected_count():
    """快乐8 的票面数由玩法决定：选八从 12 码池里取 ⇒ C(12,8)=495。"""
    assert ticket_count("快乐8", pools={"基本号": list(range(1, 13))},
                        play="选八")["注数"] == 495
    assert ticket_count("快乐8", pools={"基本号": list(range(1, 13))},
                        play="选十")["注数"] == 66


def test_over_cap_is_explicit():
    """超单张上限时仍给注数（方便看超多少），但必须标不合法并写原因。"""
    r = ticket_count("大乐透", pools={"红球": list(range(1, 36)),
                                      "蓝球": list(range(1, 13))})
    assert r["注数"] > MAX_GROUPS
    assert r["合法"] is False
    assert "上限" in r["原因"]


# ---------------------------------------------------------------- ② 票面合法性
def test_valid_group_passes():
    g = {"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}
    assert validate_group("双色球", g) == []


def test_wrong_count_is_reported():
    g = {"号码": {"红球": [1, 2, 3, 4, 5], "蓝球": [7]}}
    assert any("应选 6" in p for p in validate_group("双色球", g))


def test_out_of_range_is_reported():
    g = {"号码": {"红球": [1, 2, 3, 4, 5, 34], "蓝球": [7]}}
    assert any("越界" in p for p in validate_group("双色球", g))


def test_duplicate_in_non_repeatable_zone_is_reported():
    g = {"号码": {"红球": [1, 2, 3, 4, 5, 5], "蓝球": [7]}}
    assert any("重复" in p for p in validate_group("双色球", g))


def test_duplicate_allowed_in_positional_zone():
    """数字型每位可重复（如 555），不能按「不可重复区」判非法。"""
    g = {"号码": {f"第{i}位": [5] for i in range(1, 4)}}
    assert validate_group("福彩3D", g) == []


def test_missing_zone_is_reported():
    g = {"号码": {"红球": [1, 2, 3, 4, 5, 6]}}
    assert any("缺分区" in p for p in validate_group("双色球", g))


def test_draw_only_zone_must_not_appear_on_ticket():
    """★ 七乐彩特别号是 draw_only：只开不选。它出现在票面里就是八码非法票。"""
    bad = {"号码": {"基本号": list(range(1, 8)), "特别号": [9]}}
    problems = validate_group("七乐彩", bad)
    assert any("非票面分区" in p for p in problems)
    good = {"号码": {"基本号": list(range(1, 8))}}
    assert validate_group("七乐彩", good) == []


# ---------------------------------------------------------------- ③ 整批
def test_batch_check_counts_all_illegal_groups():
    """非法组数必须是**全量**计数，不能被「示例只留 3 条」截断成 3。"""
    ok = {"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}
    bad = {"号码": {"红球": [1, 2, 3, 4, 5], "蓝球": [7]}}
    r = batch_check("双色球", [ok, bad, bad, bad, bad], declared=5)
    assert r["非法组数"] == 4
    assert r["合法"] is False


def test_batch_check_declared_mismatch():
    """声明组数与实际组数不等 ⇒ 必须报出来（这就是「1w 变回 1000」的判据）。"""
    ok = {"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}
    r = batch_check("双色球", [ok], declared=10000)
    assert r["合法"] is False
    assert any("声明组数" in p for p in r["问题"])


def test_ticket_shape_excludes_draw_only_zone():
    names = [z["区"] for z in ticket_shape("七乐彩")]
    assert names == ["基本号"]
    assert [z["区"] for z in ticket_shape("快乐8")] == ["基本号"]
