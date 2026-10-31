# -*- coding: utf-8 -*-
"""影子三臂接入调度的牙齿（2026-11-01）。

这一层比 `data/shadow.py` 更容易出**看不出来**的错：号码生成、注数、结算口径
任何一处偏了，台账照样有行、汇总照样出数、页面照样好看。

四条最贵的错，各一条测试：

1. **三臂注数不等** ⇒ 比较的是「买得更多」，不是「选得更好」。
   实测池 m=2000 时双色球 K=500 只挑出 435 注（cap=3 凑不满）。
2. **浮动奖按估算入账** ⇒ 冒烟时 50 注一等奖算出 2.5 亿元，净收益率当场失真。
3. **结算写回导致台账可变** ⇒ 事后无法自证「当时买的什么号」。
4. **拿已开奖的期补录** ⇒ 事后生成，无法自证提前出号。
"""

from __future__ import annotations

import json

import pytest

import ev.budget_gate as BG
from data import shadow as SH
from data import shadow_run as SR
from data.feedback import load_pending
from data.loader import load_lottery


@pytest.fixture()
def last_draw():
    """最近一期真实开奖（用于结算路径）。"""
    recs = load_lottery("双色球").records
    assert recs, "需要真实开奖数据"
    return recs[-1]


def _model_tickets(k: int, *, from_draw=None):
    if from_draw is not None:
        t = {"红球": list(from_draw.zone_numbers["红球"]),
             "蓝球": list(from_draw.zone_numbers["蓝球"])}
    else:
        t = {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}
    return [dict(t) for _ in range(k)]


def test_three_arms_have_identical_ticket_counts(tmp_path):
    """★★ 三臂必须同注数 —— 这是「同预算」的全部内容。"""
    r = SR.record_batch("双色球", "TEST-1", _model_tickets(20), base=tmp_path)
    assert r["状态"] == "已记录"
    rows = SH.load("双色球", base=tmp_path)
    assert [x["注数"] for x in rows] == [20, 20, 20]
    sb = SH.summary("双色球", base=tmp_path)["同预算"]
    assert sb["可比较"] is True and sb["同注数"] is True


def test_arms_are_not_identical_numbers(tmp_path):
    """同注数不等于同号码：三个臂必须是三套不同的票，否则对照没有意义。"""
    SR.record_batch("双色球", "TEST-2", _model_tickets(15), base=tmp_path)
    rand = SH.tickets_of("双色球", "TEST-2", SH.ARM_RANDOM, base=tmp_path)
    fixed = SH.tickets_of("双色球", "TEST-2", SH.ARM_FIXED, base=tmp_path)
    model = SH.tickets_of("双色球", "TEST-2", SH.ARM_MODEL, base=tmp_path)
    assert len(rand) == len(fixed) == len(model) == 15
    assert rand != fixed, "随机臂与固定规则臂不该是同一批号码"
    assert rand != model and fixed != model


def test_short_pool_is_flagged_not_silently_shorter(tmp_path, monkeypatch):
    """★★ 池不够大时，随机臂凑不满 K —— 必须**告警并留行**，不能悄悄少买。

    这是真实存在的现象，不是理论担忧：基准测量里 m=2000、seed=1 时双色球
    K=500 在 cap=3 下只挑出 **435** 注（换 seed 可能刚好凑满，故它同时是
    「池必须远大于 K」与「必须逐次校验 len==K」的理由）。
    测试为了确定性，把池压到 10 张：任何 seed 都凑不出 50 注。
    """
    monkeypatch.setattr(SR, "POOL_SIZE", 10)
    r = SR.record_batch("双色球", "TEST-3", _model_tickets(50), base=tmp_path)
    rand = [x for x in SH.load("双色球", base=tmp_path) if x["臂"] == SH.ARM_RANDOM][0]
    assert rand["注数"] < 50, "池 10 张不可能凑出 50 注（前提成立才算是真测试）"
    assert rand["结算失败"] is True
    assert "注数不足" in (rand.get("口径") or "")
    assert "告警" in r["臂"][SH.ARM_RANDOM]


def test_no_budget_and_no_pending(tmp_path, monkeypatch):
    """跑三臂：本月已投严格不变、生产 pending 不新增。"""
    monkeypatch.setattr(SH, "SHADOW_DIR", tmp_path)
    before_spent = BG.spent_this_month()
    before_pending = len(load_pending("双色球"))
    SR.record_batch("双色球", "TEST-4", _model_tickets(10), base=tmp_path)
    assert BG.spent_this_month() == before_spent
    assert len(load_pending("双色球")) == before_pending
    SH.assert_no_booking("双色球", base=tmp_path)


def test_record_is_idempotent(tmp_path):
    """重复记同一期必须跳过 —— 否则自愈重跑会把期数放大、净收益率失真。"""
    SR.record_batch("双色球", "TEST-5", _model_tickets(5), base=tmp_path)
    again = SR.record_batch("双色球", "TEST-5", _model_tickets(5), base=tmp_path)
    assert again["状态"] == "跳过"
    assert len(SH.load("双色球", base=tmp_path)) == 3      # 三臂各一行，没有翻倍


def test_stopped_period_still_gets_rows(tmp_path):
    """停发期（0 注）也要留行 —— 分母纪律同样适用于影子。"""
    r = SR.record_batch("双色球", "TEST-6", [], base=tmp_path)
    assert r["注数"] == 0
    rows = SH.load("双色球", base=tmp_path)
    assert len(rows) == 3
    assert all(x["注数"] == 0 for x in rows)
    assert all(x["成本"] == 0 for x in rows)


def test_floating_prize_is_amount_unknown(tmp_path, last_draw):
    """★★ 浮动奖不许按 `base_payout` 估算入账。

    冒烟实录：3 注全中一等奖时 `_grade` 返回单注约 8,400 万元（假设只有自己中），
    汇总净收益率直接失真。正确做法与账本一致：**不参与求和 + 单独计数**。
    """
    model = _model_tickets(3, from_draw=last_draw)
    SR.record_batch("双色球", last_draw.期号, model, base=tmp_path)
    SR.settle("双色球", base=tmp_path)
    row = [x for x in SH.load("双色球", base=tmp_path)
           if x["臂"] == SH.ARM_MODEL and str(x["期号"]) == str(last_draw.期号)][0]
    assert row["中奖注数"] == 3
    assert row["金额未知注数"] == 3
    assert row["税前奖金"] == 0.0, "浮动奖金额必须为 0 而不是估算值"


def test_fixed_prize_amounts_from_rule_table(tmp_path, last_draw):
    """固定奖按规则表单注金额结算（蓝球命中 = 六等 5 元）。"""
    red = [n for n in range(1, 34) if n not in last_draw.zone_numbers["红球"]][:6]
    model = [{"红球": red, "蓝球": list(last_draw.zone_numbers["蓝球"])} for _ in range(2)]
    SR.record_batch("双色球", last_draw.期号, model, base=tmp_path)
    SR.settle("双色球", base=tmp_path)
    row = [x for x in SH.load("双色球", base=tmp_path)
           if x["臂"] == SH.ARM_MODEL and str(x["期号"]) == str(last_draw.期号)][0]
    assert row["中奖注数"] == 2
    assert row["税前奖金"] == 10.0          # 2 注 × 六等 5 元
    assert row["金额未知注数"] == 0


def test_settle_never_backfills_unknown_issue(tmp_path):
    """★ 结算只处理**已存在的记录行**：没记过的期不许凭空生成。"""
    SR.record_batch("双色球", "TEST-7", _model_tickets(2), base=tmp_path)
    res = SR.settle("双色球", base=tmp_path, issues=["999999"])
    assert res["结算行数"] == 0
    assert len(SH.load("双色球", base=tmp_path)) == 3      # 没有新增行


def test_settle_is_idempotent(tmp_path, last_draw):
    """同一期结算两次，只应新增一次结算行（第二次已 `结算=True`）。"""
    SR.record_batch("双色球", last_draw.期号, _model_tickets(2), base=tmp_path)
    first = SR.settle("双色球", base=tmp_path)
    assert first["结算行数"] == 3
    second = SR.settle("双色球", base=tmp_path)
    assert second["结算行数"] == 0


def test_generation_is_deterministic(tmp_path):
    """同期同臂重跑必须得到同一批号码（否则台账里同一期会有两套票）。"""
    a = SR.build_arm_tickets("双色球", SH.ARM_RANDOM, 12, issue="DET-1")
    b = SR.build_arm_tickets("双色球", SH.ARM_RANDOM, 12, issue="DET-1")
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    c = SR.build_arm_tickets("双色球", SH.ARM_RANDOM, 12, issue="DET-2")
    assert json.dumps(a, sort_keys=True) != json.dumps(c, sort_keys=True)


def test_three_arms_share_one_pool(tmp_path):
    """同一期内三臂必须共用同一个候选池（池 seed 由彩种+期号派生）。"""
    pool = SR.build_arm_tickets.__globals__["build_pool"]
    from ev.coverage_backtest import build_pool as bp1
    from ev.coverage_backtest import build_pool as bp2
    assert bp1 is bp2 is pool
    seed = SR._seed_of("双色球", "POOL-1", "pool")
    first = bp1("双色球", 500, seed=seed)
    second = bp2("双色球", 500, seed=seed)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_pending_issues_shrinks_after_settle(tmp_path, last_draw):
    """结算之后该期不再挂在「待结算」里（否则分母会永远少这一期）。"""
    SR.record_batch("双色球", last_draw.期号, _model_tickets(2), base=tmp_path)
    assert str(last_draw.期号) in SH.pending_issues("双色球", base=tmp_path)
    SR.settle("双色球", base=tmp_path)
    assert str(last_draw.期号) not in SH.pending_issues("双色球", base=tmp_path)


def test_tickets_roundtrip_via_compression(tmp_path):
    """号码压缩必须无损往返 —— 结算全靠它。"""
    tks = _model_tickets(30)
    SR.record_batch("双色球", "RT-1", tks, base=tmp_path)
    back = SH.tickets_of("双色球", "RT-1", SH.ARM_MODEL, base=tmp_path)
    assert json.dumps(tks, sort_keys=True) == json.dumps(back, sort_keys=True)

def test_concurrent_record_writes_only_once(tmp_path):
    """★ 并发触发只许记一次（run_auto_pipeline 的三条路径可能几乎同时到达）。

    没有锁时「查重」与「写入」之间有窗口：两个进程都通过查重、各写一遍三臂，
    于是同一期出现 6 行，`同窗口` 判成 False，而台账里看不出是谁写重的。
    影子单行压缩后 > 4KB（500 注约 9KB），交错写还会产生半行 —— `load()` 遇到
    残行只跳过，那一期就少一两条臂。
    """
    import threading

    results = []

    def work():
        r = SR.record_batch("双色球", "CONC-1", _model_tickets(5), base=tmp_path)
        results.append(r.get("状态"))

    ts = [threading.Thread(target=work) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(60)

    assert results.count("已记录") == 1, results
    assert results.count("跳过") == 3, results
    assert len(SH.load("双色球", base=tmp_path)) == 3

def test_summary_separates_settled_from_pending(tmp_path, last_draw):
    """★ 未开奖的期必须与「开了且没中」分开报。

    只给一个「净收益率 −100%」时，两种完全不同的情况长得一模一样：
      (a) 一期都没开奖 —— 成本已记、奖金尚未产生，这个 −100% 不构成任何结论；
      (b) 开了 30 期全没中 —— 这才是读数。
    只看百分比会把它读成 (b)。
    """
    SR.record_batch("双色球", last_draw.期号, _model_tickets(2), base=tmp_path)
    SR.record_batch("双色球", "FUTURE-1", _model_tickets(2), base=tmp_path)

    s = SH.summary("双色球", base=tmp_path)
    arm = s["臂"][SH.ARM_MODEL]
    assert arm["期数"] == 2
    assert arm["已结算期数"] == 0 and arm["待结算期数"] == 2

    SR.settle("双色球", base=tmp_path, issues=[last_draw.期号])
    arm = SH.summary("双色球", base=tmp_path)["臂"][SH.ARM_MODEL]
    assert arm["已结算期数"] == 1 and arm["待结算期数"] == 1
