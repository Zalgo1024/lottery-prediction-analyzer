# -*- coding: utf-8 -*-
"""影子三臂「可判性判决」的牙齿（2026-11-01）。

这一层的价值全在**拒绝下结论**上。读数（`data.shadow.summary()`）永远会给出
三行漂亮的净收益率；判决层必须回答「这点差异够不够算」。最容易犯的错不是算错，
而是**算得太顺**：

1. 一期都没结算就报 −100%（奖金还没产生）。
2. 样本 2~3 期就报「模型更好」。
3. Δ 由单独一期撑起，却报 z 显著。
4. 6 个检验不过多重比较校正，裸 P95 当判据。
5. 三臂窗口不一致时悄悄取交集，比较「跑了不同的期」。
6. 判决层偷偷写台账（它是只读的）。

每条一个测试。
"""

from __future__ import annotations

import pytest

from data import shadow as SH
from data import shadow_verdict as V

LOT = "双色球"
K = 5
UNIT = 2.0          # 单价：5 注 ⇒ 成本 10 元/期


def _tickets(n: int = K):
    return [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]} for _ in range(n)]


def _period(tmp_path, issue, gross, *, settled=True, k=K):
    """给三臂各写一期（同注数、同奖金），保证窗口一致。"""
    for arm in SH.ARMS:
        SH.record(LOT, issue, arm, tickets=_tickets(k), price=UNIT, base=tmp_path)
        if settled:
            SH.settle_row(LOT, issue, arm, base=tmp_path,
                          result={"中奖注数": 0, "税前奖金": float(gross)})


def _period_one_arm(tmp_path, issue, arm, gross, *, settled=True, k=K):
    SH.record(LOT, issue, arm, tickets=_tickets(k), price=UNIT, base=tmp_path)
    if settled:
        SH.settle_row(LOT, issue, arm, base=tmp_path,
                      result={"中奖注数": 0, "税前奖金": float(gross)})


def _rows_of(res, metric):
    return [r for r in res["对照"] if r["口径"] == metric]


def test_not_settled_is_not_a_reading(tmp_path):
    """★ 一期都没结算时的 −100% 不是读数（奖金尚未产生），判决必须拒绝。"""
    _period(tmp_path, "10001", 0.0, settled=False)
    res = V.compare(LOT, base=tmp_path)
    assert res["可用"] is False
    assert "尚未产生" in res["原因"]
    assert res["对照"] == []
    assert res["期数"] == {"记录": 1, "已结算": 0, "待结算": 1}


def test_sample_below_minimum_refuses_to_judge(tmp_path):
    """2 期就下结论 = 把运气当策略；这一档连配对差方差都估不出来。"""
    _period(tmp_path, "10001", 100.0)
    _period(tmp_path, "10002", 0.0)
    res = V.compare(LOT, base=tmp_path)
    assert res["可用"] is False
    assert "方差无法估计" in res["原因"]
    assert res["对照"] == []
    assert res["期数"]["已结算"] == 2


def test_identical_arms_are_no_difference(tmp_path):
    """三臂逐期一样多 ⇒ Δ=0，只能说「这一批没差异」，不能报显著。"""
    for i, g in enumerate([0.0, 20.0, 0.0, 5.0], start=1):
        _period(tmp_path, f"1000{i}", g)
    res = V.compare(LOT, base=tmp_path)
    assert res["可用"] is True
    money = _rows_of(res, "money")
    assert money and all(r["判决"] == "未检出差异" for r in money)
    assert all(abs(r["实测Δ"]) < 1e-9 for r in money)
    assert res["总结"].startswith("各组均未检出差异")


def test_single_period_dominates_blocks_conclusion(tmp_path):
    """★★ Δ 被单独一期撑起时必须拒绝。

    数学上很干脆：n 期里只有一期 Δ=S、其余 0 时，z 恒等于 1（与 S 和 n 无关）。
    所以「z 小」并不足以说明没问题 —— 必须看集中度，否则换个期次就反号。
    """
    _period_one_arm(tmp_path, "10001", SH.ARM_MODEL, 2000.0)
    for i in range(2, 5):
        _period_one_arm(tmp_path, f"1000{i}", SH.ARM_MODEL, 0.0)
    for arm in (SH.ARM_RANDOM, SH.ARM_FIXED):
        for i in range(1, 5):
            _period_one_arm(tmp_path, f"1000{i}", arm, 0.0)

    res = V.compare(LOT, base=tmp_path)
    money = _rows_of(res, "money")
    assert money, res
    model_vs_random = [r for r in money if r["对照"] == f"{SH.ARM_MODEL} vs {SH.ARM_RANDOM}"]
    assert model_vs_random
    row = model_vs_random[0]
    assert row["判决"] == "不可判"
    assert "单独一期" in row["判决说明"]
    assert abs(row["单期占Δ比"] - 1.0) < 1e-9


def test_window_mismatch_is_refused_not_intersected(tmp_path):
    """三臂跑了不同的期时不许悄悄取交集 —— 那样比较的是「不同的期」。"""
    _period(tmp_path, "10001", 0.0)
    _period(tmp_path, "10002", 0.0)
    _period(tmp_path, "10003", 0.0)
    _period_one_arm(tmp_path, "10004", SH.ARM_MODEL, 50.0)

    res = V.compare(LOT, base=tmp_path)
    assert res["可用"] is False
    assert "期号集合不一致" in res["原因"]
    assert res["缺失期"][SH.ARM_MODEL] == ["10004"]
    assert res["对照"] == []


def test_constant_difference_is_decidable_but_flags_implementation(tmp_path):
    """Δ 恒定且非零 ⇒ 零方差、n*=1，可判；但必须提醒「先查实现缺陷」。

    ★ 固定奖层面三臂的期望回款相同（E[中奖注数]=K·p）。恒定差不是"策略变强"，
      而是实现或口径出了问题的典型形状 —— 判决文案必须把这句话带上。
    """
    for i in range(1, 5):
        _period_one_arm(tmp_path, f"1000{i}", SH.ARM_MODEL, 110.0)
        for arm in (SH.ARM_RANDOM, SH.ARM_FIXED):
            _period_one_arm(tmp_path, f"1000{i}", arm, 10.0)

    res = V.compare(LOT, base=tmp_path)
    money = [r for r in _rows_of(res, "money")
             if r["对照"] == f"{SH.ARM_MODEL} vs {SH.ARM_RANDOM}"][0]
    assert money["判决"] == "可判", money["判决说明"]
    assert "实现缺陷" in money["判决说明"]
    # 恒定差 Δ=100 元/期：成本 10，故净差额就是回款差额
    assert abs(money["实测Δ"] - 100.0) < 1e-9


def test_six_tests_are_bh_adjusted(tmp_path):
    """3 对 × 2 口径 = 6 个检验；裸 P95 在 6 个检验下假阳率 26.5%。"""
    for i, g in enumerate([0.0, 30.0, 0.0, 12.0, 0.0], start=1):
        _period(tmp_path, f"1000{i}", g)
    res = V.compare(LOT, base=tmp_path)
    assert len(res["对照"]) == 6
    assert res["多重比较"]["检验数"] == 6
    assert res["多重比较"]["方法"] == "bh"
    for r in res["对照"]:
        assert r["p_BH"] >= r["p_原始"] - 1e-12
        assert "判决" in r and "判决说明" in r


def test_partial_settlement_is_excluded_and_named(tmp_path):
    """某臂结算写入失败时该期三臂不成对 ⇒ 剔除并点名。

    ★ 若不取「三臂都已结算」的期，取数时会 KeyError；若静默取交集不说，
      就变成「悄悄换了窗口」。两条都不能要：要取交集，同时把剔除了哪期写出来。
    """
    for i in range(1, 5):
        _period(tmp_path, f"1000{i}", 0.0)
    # 第 5 期：三臂都有记录，但只有当前模型臂结算成功
    _period_one_arm(tmp_path, "10005", SH.ARM_MODEL, 99.0)
    for arm in (SH.ARM_RANDOM, SH.ARM_FIXED):
        _period_one_arm(tmp_path, "10005", arm, 0.0, settled=False)

    res = V.compare(LOT, base=tmp_path)
    assert res["可用"] is True
    assert res["期数"]["已结算"] == 4
    assert res["剔除期"]["数量"] == 1
    assert res["剔除期"]["期号"] == ["10005"]
    assert all(r["期数"] == 4 for r in res["对照"])


def test_stopped_period_stays_in_denominator(tmp_path):
    """停发期（0 注）也进样本：只留有号的期 = 把不利样本删掉。"""
    for i in range(1, 5):
        _period(tmp_path, f"1000{i}", 0.0)
    _period(tmp_path, "10005", 0.0, k=0)          # 停发期：0 注、0 成本、0 奖金

    res = V.compare(LOT, base=tmp_path)
    assert res["期数"]["已结算"] == 5
    assert res["期数"]["待结算"] == 0


def test_compare_is_read_only(tmp_path):
    """判决层只读：跑完台账字节不变、行数不变、没有任何「记账」行。"""
    for i, g in enumerate([0.0, 10.0, 0.0, 40.0], start=1):
        _period(tmp_path, f"1000{i}", g)

    before = {p.name: (p.stat().st_size, p.read_bytes()) for p in tmp_path.glob("*.jsonl")}
    V.compare(LOT, base=tmp_path)
    after = {p.name: (p.stat().st_size, p.read_bytes()) for p in tmp_path.glob("*.jsonl")}
    assert before == after
    assert all(not r.get("记账") for r in SH.load(LOT, base=tmp_path))


def test_compare_all_covers_every_lottery():
    """六彩种一轮判决不得漏项（单彩种失败也要占位说明）。"""
    from config import LOTTERY_CONFIG

    res = V.compare_all()
    assert set(res) == set(LOTTERY_CONFIG)
    for lot, r in res.items():
        assert r["彩种"] == lot
        if not r["可用"]:
            assert r["原因"], f"{lot} 不可用却没给原因"
