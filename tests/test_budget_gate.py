# -*- coding: utf-8 -*-
"""tests/test_budget_gate.py —— 生产预算闸门（任务 2：把注数增长纳入风险门）。

锁的是**纪律**，不是算术：
① 未启用时行为与接入前完全一致（默认状态不得改变现状）；
② 启用但额度缺失 ⇒ **抛**，绝不 fail-open；
③ 只降不升：返回值永远 ≤ 请求值，`auto` 来源还 ≤ 基线（自动路径永不加注）；
④ 幂等记账：同一 (期号, 档位, 域, 彩种) 连写 3 次，已投只等于一次金额；
⑤ 重生成不递缩：同期重算不会因为「已经记过一笔」而把注数越调越小；
⑥ 跨月归零、台账损坏不阻断出号。

★ 为什么第 ④ 条是硬要求：`record_pending_prediction` 每次流水线都会调一次，
  而启动自愈、手动重生成、多档管线都会重复调用同一期。按写盘次数累加，
  月度已投会在半天内虚增成真实投入的数倍 ⇒ 闸门提前把注数压成 1 甚至 0。
"""
from __future__ import annotations

import json

import pytest

import config
import data.feedback as fb
import ev.budget_gate as BG


@pytest.fixture
def tmp_fb(tmp_path, monkeypatch):
    """pending 落盘重定向到 tmp（与 `test_hindsight_guard` 同一套做法）。"""
    monkeypatch.setattr(fb, "_pending_path",
                        lambda name: tmp_path / f"{name}_pending.json")
    monkeypatch.setattr(fb, "_history_path",
                        lambda name: tmp_path / f"{name}_history.json")
    monkeypatch.setattr(fb, "_weights_path",
                        lambda name: tmp_path / f"{name}_weights.json")
    return tmp_path


def _write_limit(**kw):
    doc = {"版本": 1, "启用": True, "月度总额": 3000, "单期上限": 200,
           "锁定": True, "更新于": "2026-10-30T00:00:00", "备注": "测试"}
    doc.update(kw)
    BG.LIMIT_PATH.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return doc


@pytest.fixture(autouse=True)
def _clear_cache():
    BG._LIMIT_CACHE.update({"key": None, "doc": None})
    yield


# ---------------------------------------------------------------- ①② 开关
def test_unenabled_passes_through_unchanged():
    """缺文件 = 未启用 ⇒ 请求多少给多少（与接入前一致），并显式说「闸门未启用」。"""
    r = BG.clamp("双色球", 2000, baseline=50, source="manual")
    assert r["注数"] == 2000 and r["受限"] is False
    assert "闸门未启用" in r["原因"]
    assert r["月度总额"] is None


def test_auto_without_gate_still_capped_by_baseline():
    r = BG.clamp("双色球", 2000, baseline=50, source="auto")
    assert r["注数"] == 50 and r["受限"] is True
    assert "基线" in r["原因"]


def test_enabled_without_monthly_budget_raises():
    """唯一允许的失败方式：显式抛。静默 fail-open 会让配置手误变成「没有上限」。"""
    _write_limit(月度总额=None)
    with pytest.raises(BG.GateConfigError, match="月度总额"):
        BG.clamp("双色球", 50, baseline=50)


def test_enabled_with_garbage_monthly_budget_raises():
    _write_limit(月度总额="三千元")
    with pytest.raises(BG.GateConfigError):
        BG.clamp("双色球", 50)


def test_broken_json_raises_not_silently_disabled():
    BG.LIMIT_PATH.write_text("{坏掉的 json", encoding="utf-8")
    with pytest.raises(BG.GateConfigError, match="解析失败"):
        BG.clamp("双色球", 50)


# ---------------------------------------------------------------- ③ 夹取
def test_single_period_cap():
    _write_limit(月度总额=10000, 单期上限=200)      # 200 元 ÷ 2 元 = 100 注
    r = BG.clamp("双色球", 300, baseline=50, source="manual")
    assert r["注数"] == 100 and r["受限"] is True
    assert "单期上限" in r["原因"]


def test_monthly_remaining_cap():
    """已投要从月度总额里扣掉（记的是**上一期**，不是本次要出的那期）。"""
    _write_limit(月度总额=1000, 单期上限=1000)
    BG.record_spend("双色球", 目标期号="26100", 注数=400)      # 800 元（历史期）
    r = BG.clamp("双色球", 500, baseline=50, source="manual", 目标期号="26125")
    assert r["已投元"] == 800.0
    assert r["注数"] == 100                                   # 剩 200 元 ÷ 2 = 100 注


def test_auto_never_exceeds_baseline():
    """自动路径永不加注：即使额度充裕，`auto` 也不会给出比基线更多的注数。"""
    _write_limit(月度总额=100000, 单期上限=100000)
    r = BG.clamp("双色球", 500, baseline=50, source="auto")
    assert r["注数"] == 50
    assert "不超过基线" in r["原因"]


def test_manual_may_exceed_baseline_but_not_budget():
    _write_limit(月度总额=100000, 单期上限=100000)
    r = BG.clamp("双色球", 300, baseline=50, source="manual")
    assert r["注数"] == 300 and r["受限"] is False


def test_research_is_exempt():
    _write_limit(月度总额=100, 单期上限=100)
    r = BG.clamp("双色球", 5000, baseline=50, source="research")
    assert r["注数"] == 5000 and r["豁免"] is True


def test_exhausted_budget_stops_issuing():
    _write_limit(月度总额=200, 单期上限=200)
    BG.record_spend("双色球", 目标期号="26100", 注数=100)      # 200 元 = 满额（历史期）
    r = BG.clamp("双色球", 50, baseline=50, source="auto", 目标期号="26125")
    assert r["注数"] == 0 and r["停发"] is True


def test_unknown_source_raises():
    with pytest.raises(ValueError, match="未知来源"):
        BG.clamp("双色球", 10, source="boss")


# ---------------------------------------------------------------- ④ 幂等
def test_record_spend_is_idempotent():
    for _ in range(3):
        BG.record_spend("双色球", 目标期号="26125", 注数=50)
    doc = BG.load_spend()
    assert doc["已投"] == 100.0                # 50 注 × 2 元，只算一次
    assert len(doc["记录"]) == 1


def test_record_spend_overwrites_with_latest_count():
    BG.record_spend("双色球", 目标期号="26125", 注数=50)
    BG.record_spend("双色球", 目标期号="26125", 注数=80)
    assert BG.spent_this_month() == 160.0


def test_distinct_keys_accumulate():
    BG.record_spend("双色球", 目标期号="26125", 注数=50)
    BG.record_spend("大乐透", 目标期号="26125", 注数=50)
    BG.record_spend("双色球", 目标期号="26125", 注数=50, 档位="稳健")
    BG.record_spend("双色球", 目标期号="26125", 注数=50, 台账域="研究")
    assert BG.spent_this_month() == 400.0       # 4 条互不覆盖


def test_amount_uses_ticket_price_source():
    BG.record_spend("双色球", 目标期号="26125", 注数=37)
    assert BG.spent_this_month() == 37 * config.ticket_price("双色球")


# ---------------------------------------------------------------- ⑤ 重生成
def test_regeneration_does_not_shrink_monotonically():
    """同一期反复重算，注数不得一路塌下去（否则自愈每跑一次就少一截）。"""
    _write_limit(月度总额=300, 单期上限=300)
    BG.record_spend("双色球", 目标期号="26125", 注数=100)     # 200 元
    first = BG.clamp("双色球", 100, baseline=50, source="manual", 目标期号="26125")["注数"]
    BG.record_spend("双色球", 目标期号="26125", 注数=first)
    second = BG.clamp("双色球", 100, baseline=50, source="manual", 目标期号="26125")["注数"]
    BG.record_spend("双色球", 目标期号="26125", 注数=second)
    third = BG.clamp("双色球", 100, baseline=50, source="manual", 目标期号="26125")["注数"]
    assert first == second == third == 100, (first, second, third)


# ------------------------------------------------- ⑤' 重出批：不能退别批的钱
def test_renumber_batch_does_not_reclaim_previous_batch_credit():
    """★★ 同期第 2 批**不会覆盖**第 1 批 ⇒ 不许把第 1 批的钱退回来。

    早期实现按「本彩种本月最近一条 + 期号相同」退额度，第 2 批（重出批号=1）
    会把第 1 批（=0）的 200 元加回可用额度 —— 而那 200 元是**已经花掉的**。
    后果：可用额度凭空多一批，且每重出一次再放大一次，闸门比配置更松。
    """
    _write_limit(月度总额=1000, 单期上限=1000)
    BG.record_spend("双色球", 目标期号="26125", 注数=100)          # 第 1 批 200 元
    r = BG.clamp("双色球", 400, baseline=50, source="manual",
                 目标期号="26125", renumber=1)
    assert r["本彩种本月已记元"] == 0.0, "第 2 批不该退回第 1 批的钱"
    assert r["已投元"] == 200.0
    assert r["注数"] == 400            # 剩 800 元 ÷ 2 = 400 注（不因重出而放大到 500）
    # 第 2 批自己重生成（同批号）仍然不递缩
    BG.record_spend("双色球", 目标期号="26125", 注数=400, 重出批号=1)
    r2 = BG.clamp("双色球", 400, baseline=50, source="manual",
                  目标期号="26125", renumber=1)
    assert r2["本彩种本月已记元"] == 800.0
    assert r2["注数"] == 400


def test_renumber_batch_is_never_reclaimed_repeatedly():
    """每重出一批，可用额度只能减少，不能被「退回」循环放大。"""
    _write_limit(月度总额=1000, 单期上限=1000)
    BG.record_spend("双色球", 目标期号="26125", 注数=100)      # 首出批 200 元
    got = []
    for n in (1, 2, 3):
        r = BG.clamp("双色球", 500, baseline=50, source="manual",
                     目标期号="26125", renumber=n)
        assert r["本彩种本月已记元"] == 0.0, f"第 {n} 批退回了别人的钱"
        got.append(r["注数"])
        BG.record_spend("双色球", 目标期号="26125", 注数=r["注数"], 重出批号=n)
    assert got == [400, 0, 0]                    # 800 元 → 400 注；之后额度已满
    assert BG.spent_this_month() == 1000.0       # ★ 永不越过月度总额
    assert BG.clamp("双色球", 500, baseline=50, source="manual",
                    目标期号="26125", renumber=9)["注数"] == 0


def test_clamp_reads_renumber_scope_like_record_spend():
    """`clamp()` 不传 renumber 时必须读 `_RENUMBER_VAR`（与记账同源）。

    否则「重新出号」走的正是这条路径：`renumber_scope(1)` 里跑流水线，
    clamp 看不到批号 ⇒ 按首出批退额度 ⇒ 又把上一个月的钱退回来了。
    """
    _write_limit(月度总额=1000, 单期上限=1000)
    BG.record_spend("双色球", 目标期号="26125", 注数=100)      # 首出批 200 元
    with fb.renumber_scope(1):
        r = BG.clamp("双色球", 400, baseline=50, source="manual", 目标期号="26125")
        assert r["重出批号"] == 1
        assert r["本彩种本月已记元"] == 0.0
    # 作用域外仍是首出批 ⇒ 该退（重生成不递缩的原有语义）
    r0 = BG.clamp("双色球", 400, baseline=50, source="manual", 目标期号="26125")
    assert r0["重出批号"] == 0
    assert r0["本彩种本月已记元"] == 200.0


def test_research_batch_is_not_rebated():
    """研究批不占生产账，也就不该成为生产批的「可退回额度」。"""
    _write_limit(月度总额=1000, 单期上限=1000)
    BG.record_spend("双色球", 目标期号="26125", 注数=100, 台账域="研究")
    r = BG.clamp("双色球", 400, baseline=50, source="manual", 目标期号="26125")
    assert r["本彩种本月已记元"] == 0.0


# ---------------------------------------------------------------- ⑥ 跨月/损坏
def test_month_rollover_resets(monkeypatch):
    from datetime import datetime
    BG.record_spend("双色球", 目标期号="26125", 注数=100,
                    now=datetime(2026, 10, 5, 12, 0))
    assert BG.spent_this_month(now=datetime(2026, 10, 20)) == 200.0
    assert BG.spent_this_month(now=datetime(2026, 11, 1)) == 0.0


def test_corrupt_spend_file_does_not_block_issuing():
    _write_limit(月度总额=10000, 单期上限=1000)
    BG.SPEND_PATH.write_text("{不是 json", encoding="utf-8")
    doc = BG.load_spend()
    assert doc["已投"] == 0.0
    assert "★台账损坏" in doc
    r = BG.clamp("双色球", 50, baseline=50, source="auto")
    assert r["注数"] == 50                     # 台账坏了也不阻断出号


# ---------------------------------------------------------------- 与 resolve_groups 的接线
def test_resolve_groups_bypass_is_closed():
    """`--groups 10000` 不再是纯旁路：必须被单期上限夹住。"""
    _write_limit(月度总额=100000, 单期上限=200)
    assert config.resolve_groups("双色球", 10000, source="manual") == 100
    assert config.resolve_groups("双色球", 10000, source="research") == 10000


def test_resolve_groups_raises_on_bad_config():
    """配置错误必须**向上抛**（不许被 fail-closed 吞成基线）。"""
    _write_limit(月度总额=None)
    with pytest.raises(BG.GateConfigError):
        config.resolve_groups("双色球", 50)


def test_resolve_groups_disabled_keeps_auto_semantics():
    """闸门未启用时的既有语义：`auto` 不超过生效注数，少于则照请求。

    ★ 测试环境里 `data.ticket_size` 的配置被重定向到临时目录 ⇒ 生效注数走
      历史口径（乐透型 100），不是本机真实的 50。这里断言的是**关系**，
      不是某个固定数字。
    """
    base = config.resolve_groups("双色球")
    assert config.resolve_groups("双色球", base + 500) == base      # auto 不超过基线
    assert config.resolve_groups("双色球", 5) == 5                  # 少于基线照请求


# ---------------------------------------------------------------- 计划
def test_month_plan_covers_all_lotteries_and_sums():
    plan = BG.month_plan()
    assert set(plan["彩种"]) == set(config.LOTTERY_CONFIG)
    total = sum(v["月度元"] for v in plan["彩种"].values())
    assert plan["月度合计元"] == pytest.approx(total, abs=0.01)
    for lot, v in plan["彩种"].items():
        assert v["单价"] == config.ticket_price(lot)
        assert v["月度元"] == pytest.approx(v["注数"] * v["单价"] * v["期数每月"], abs=0.01)


def test_mark_stopped_is_idempotent():
    BG.mark_stopped("双色球", "额度耗尽", 目标期号="26125")
    BG.mark_stopped("双色球", "额度耗尽", 目标期号="26125")
    doc = BG.load_spend()
    assert len(doc["停发"]) == 1


# ---------------------------------------------------------------- 停发：写不写 pending
def test_stopped_record_is_not_written_to_pending(tmp_fb):
    """停发的批**不许**进 pending：pending 是「待开奖核对的一批票」。

    写一条 0 注记录会让「滑轨 == pending 组数」的一致性判据退化成 0==0 的
    假通过，结算时还会凭空多出一期「全空」战绩。
    """
    from data.feedback import load_pending, record_pending_prediction

    rec = {"目标期号": 999101, "预测号码": [], "号码组数": 0,
           "预测日期": "2099-01-01",
           "停发": {"原因": "月度额度耗尽"}}
    assert record_pending_prediction("排列5", rec) is None
    assert load_pending("排列5") == []
    # 停发原因必须落进预算台账，否则「为什么这期没出号」无从查起
    doc = BG.load_spend()
    assert [x.get("原因") for x in doc["停发"]] == ["月度额度耗尽"]


def test_empty_tickets_without_marker_still_record(tmp_fb):
    """★★ 反向守护：**没有**停发标记的空票集照常记录。

    判据一旦写成「号码为空 ⇒ 停发」，就会在 `test_hindsight_guard` /
    `test_feedback_tiers` 里那些用空票集构造记录的测试**之前**短路 ——
    那 6 个测试是验纪律的，被抢跑就等于纪律没验。
    """
    from data.feedback import load_pending, record_pending_prediction

    rec = {"目标期号": 999102, "预测号码": [], "号码组数": 0,
           "预测日期": "2099-01-01"}
    # 注意 `record_pending_prediction` 正常路径也返回 None ⇒ 只能断副作用
    record_pending_prediction("排列5", rec)
    assert len(load_pending("排列5")) == 1
    assert BG.load_spend()["停发"] == []
