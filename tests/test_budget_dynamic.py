# -*- coding: utf-8 -*-
"""动态定额三机制 + 手动通道的测试（v2）。

锁四件事：
  ① 三个自动机制**只能收紧**，任何一个都不许把额度放宽；
  ② 手动是唯一上调通道，且必须留审计（无备注直接拒）；
  ③ 机制①（滚动分摊）不改变「不传期数」时的静态行为 —— 保证 v1 的 45 个
     用例逐位不变，避免这次扩展变成静默改口径；
  ④ 闸门的单向性在动态路径上依旧成立（只降不升）。
"""
from __future__ import annotations

import json

import pytest

from ev import budget_envelope as BE


# ---------------------------------------------------------------- 夹具
def _env(**kw) -> BE.Envelope:
    base = dict(monthly_budget=260.0, per_period_cap=200.0, cost_per_ticket=2.0,
                baseline_tickets=100, lottery="双色球", 标签="测试")
    base.update(kw)
    return BE.Envelope(**base)


def _prod_env() -> BE.Envelope:
    """生产口径：双色球 200 注 × 2 元 × 13 期 = 5,200 元/月。"""
    return BE.Envelope(monthly_budget=5200.0, per_period_cap=400.0,
                       cost_per_ticket=2.0, baseline_tickets=200,
                       lottery="双色球", 标签="测试")


# ---------------------------------------------------------------- 机制①
def test_dynamic_cap_never_exceeds_static():
    env = _env()
    for spent in (0.0, 50.0, 130.0, 260.0, 999.0):
        for done in range(0, 15):
            cap = BE.dynamic_cap(env, spent, periods_done=done)
            assert cap["单期上限元"] <= env.per_period_cap + 1e-9, (spent, done)
            assert cap["注数"] <= env.cap_tickets, (spent, done)


def test_dynamic_cap_tightens_as_periods_run_out():
    """月度定额小于「静态上限 × 期数」时，滚动分摊在月初就已经收紧。"""
    env = _env()                       # 260 元/月 ÷ 13 期 = 20 元/期
    cap0 = BE.dynamic_cap(env, 0.0, periods_done=0)
    assert cap0["单期上限元"] == pytest.approx(20.0)
    assert cap0["注数"] == 10
    assert cap0["是否被滚动收紧"] is True
    # 前 6 期一直没买 ⇒ 剩余额度摊到 7 期，每期 37.14 元，但仍受静态上限 200 约束
    cap_mid = BE.dynamic_cap(env, 0.0, periods_done=6)
    assert cap_mid["剩余期数"] == 7
    assert cap_mid["滚动每期元"] == pytest.approx(260.0 / 7, abs=1e-2)
    assert cap_mid["注数"] == 18          # 37.14 // 2 = 18
    assert cap_mid["注数"] > cap0["注数"]  # 前 6 期没买 ⇒ 每期能多买几注


def test_dynamic_cap_zero_when_exhausted():
    env = _prod_env()
    cap = BE.dynamic_cap(env, 5200.0, periods_done=5)
    assert cap["单期上限元"] == 0.0
    assert cap["注数"] == 0
    assert "耗尽" in cap["原因"][0]


def test_dynamic_cap_last_period_does_not_divide_by_zero():
    env = _prod_env()
    cap = BE.dynamic_cap(env, 0.0, periods_done=99)
    assert cap["剩余期数"] == 1
    assert cap["单期上限元"] == env.per_period_cap


def test_allocate_static_path_is_unchanged():
    """不传期数 ⇒ 与 v1 逐位相同（回归锁）。"""
    env = _prod_env()
    a = BE.allocate(env, 0.0)
    assert a["注数"] == 200
    assert "滚动分摊" not in a
    b = BE.allocate(env, 0.0, periods_done=0)
    assert "滚动分摊" in b               # 传了才有，且注数相同
    assert b["注数"] == 200


def test_allocate_dynamic_reports_rolling_reason():
    env = _env()
    out = BE.allocate(env, 100.0, want_tickets=100, periods_done=6)
    # 剩 160 元 ÷ 7 期 = 22.86 元/期 ⇒ 11 注（比静态 100 注紧得多）
    assert out["注数"] == 11
    assert out["滚动分摊"]["是否被滚动收紧"] is True


# ---------------------------------------------------------------- 只降不升
def test_tighten_rejects_any_increase():
    env = _prod_env()
    with pytest.raises(ValueError, match="不得放宽额度"):
        BE._tighten(env, monthly=6000.0, tag="x", reason="test")


def test_sync_ticket_count_shrinks_when_count_drops():
    env = _prod_env()                  # 200 注 ⇒ 5200 元/月
    out, tr = BE.sync_ticket_count(env, 100)
    assert out.monthly_budget == pytest.approx(100 * 2.0 * 13)
    assert out.per_period_cap <= out.monthly_budget
    assert tr["动作"] == "收紧"


def test_sync_ticket_count_never_grows():
    env = _prod_env()
    out, tr = BE.sync_ticket_count(env, 500)
    assert out.monthly_budget == env.monthly_budget
    assert out.baseline_tickets == env.baseline_tickets
    assert tr["动作"] == "不变"


def test_sync_ticket_count_baseline_never_grows():
    env = _prod_env()
    out, _ = BE.sync_ticket_count(env, 300)
    assert out.baseline_tickets == 200
    out2, _ = BE.sync_ticket_count(env, 120)
    assert out2.baseline_tickets == 120


def test_adapt_monthly_triggers_on_worse_than_expected():
    env = _prod_env()
    out, tr = BE.adapt_monthly(env, {"投入": 5200.0, "回款": 0.0})
    assert out.monthly_budget == pytest.approx(5200.0 * BE.DEFAULT_SHRINK_FACTOR)
    assert tr["动作"] == "收紧"
    assert tr["上月净亏率"] == pytest.approx(1.0)


def test_adapt_monthly_quiet_when_result_is_expected():
    env = _prod_env()
    back = 5200.0 * BE.DEFAULT_RETURN_RATE
    out, tr = BE.adapt_monthly(env, {"投入": 5200.0, "回款": back})
    assert out.monthly_budget == env.monthly_budget
    assert tr["动作"] == "不变"


def test_adapt_monthly_never_raises_even_after_a_profit():
    """中了大奖也不加额度 —— 加额度只能由人手动做。"""
    env = _prod_env()
    out, tr = BE.adapt_monthly(env, {"投入": 5200.0, "回款": 500000.0})
    assert out.monthly_budget == env.monthly_budget
    assert tr["动作"] == "不变"


def test_adapt_monthly_no_history_is_noop():
    env = _prod_env()
    for hist in (None, {}, {"投入": 0.0, "回款": 0.0}):
        out, tr = BE.adapt_monthly(env, hist)
        assert out.monthly_budget == env.monthly_budget
        assert tr["动作"] == "不变"


def test_adapt_monthly_respects_floor():
    env = _prod_env()
    out, _ = BE.adapt_monthly(env, {"投入": 5200.0, "回款": 0.0},
                              dyn=BE.Dynamics(hard_floor=5000.0))
    assert out.monthly_budget == pytest.approx(5000.0)


@pytest.mark.parametrize("dyn", [
    BE.Dynamics(),
    BE.Dynamics(ticket_count=100),
    BE.Dynamics(last_month={"投入": 5200.0, "回款": 0.0}),
    BE.Dynamics(last_month={"投入": 5200.0, "回款": 0.0}, ticket_count=50),
    BE.Dynamics(ticket_count=9999, last_month={"投入": 5200.0, "回款": 5200.0}),
])
def test_apply_dynamics_never_increases_envelope(dyn):
    env = _prod_env()
    out, rep = BE.apply_dynamics(env, dyn)
    assert out.monthly_budget <= env.monthly_budget + 1e-9
    assert out.per_period_cap <= env.per_period_cap + 1e-9
    assert rep["原定额"] == env.monthly_budget
    assert rep["是否收紧"] == (out.monthly_budget < env.monthly_budget - 1e-9)


def test_apply_dynamics_trace_names_each_mechanism():
    env = _prod_env()
    _, rep = BE.apply_dynamics(env, BE.Dynamics(
        ticket_count=100, last_month={"投入": 5200.0, "回款": 0.0}))
    tags = [t["机制"] for t in rep["轨迹"]]
    assert tags == ["②实绩收紧", "③注数联动"]


def test_dynamics_rejects_bad_params():
    with pytest.raises(ValueError, match="收紧步长"):
        BE.Dynamics(shrink_factor=1.5)
    with pytest.raises(ValueError, match="收紧步长"):
        BE.Dynamics(shrink_factor=0.0)
    with pytest.raises(ValueError, match="容忍带宽"):
        BE.Dynamics(tolerance=-0.1)
    with pytest.raises(ValueError, match="下限"):
        BE.Dynamics(hard_floor=-1.0)


def test_shrink_keeps_envelope_invariant():
    """收紧定额后单期上限必须一起夹住，否则 Envelope 构造直接抛错。"""
    env = _prod_env()
    out, _ = BE.sync_ticket_count(env, 1)          # 1 注 ⇒ 26 元/月
    assert out.monthly_budget == pytest.approx(26.0)
    assert out.per_period_cap <= out.monthly_budget
    BE.Envelope(**{**out.__dict__})               # 能被重新构造 ⇒ 不变式成立


# ---------------------------------------------------------------- 手动通道
def test_manual_path_is_redirected_by_conftest():
    assert "pytest" in str(BE.MANUAL_PATH) or "tmp" in str(BE.MANUAL_PATH).lower()


def test_set_manual_requires_note():
    with pytest.raises(ValueError, match="备注"):
        BE.set_manual("双色球", monthly_budget=200.0, per_period_cap=40.0, note="  ")


def test_set_manual_allows_raise_but_records_audit():
    BE.set_manual("双色球", monthly_budget=200.0, per_period_cap=40.0,
                  baseline_tickets=20, note="先定个小额度")
    up = BE.set_manual("双色球", monthly_budget=400.0, note="这个月想多看两期")
    assert up["是否上调"] is True
    doc = json.loads(BE.MANUAL_PATH.read_text(encoding="utf-8"))
    assert doc["最近审计"]["前定额"] == 200.0
    assert doc["最近审计"]["后定额"] == 400.0
    assert doc["最近审计"]["actor"] == "user"
    assert doc["最近审计"]["备注"] == "这个月想多看两期"


def test_set_manual_rejects_cap_above_monthly():
    with pytest.raises(ValueError, match="单期上限"):
        BE.set_manual("双色球", monthly_budget=100.0, per_period_cap=200.0, note="x")


def test_manual_override_drives_default_envelope():
    BE.set_manual("双色球", monthly_budget=200.0, per_period_cap=40.0,
                  baseline_tickets=20, note="200 元娱乐预算")
    env = BE.default_envelope("双色球")
    assert env.monthly_budget == 200.0
    assert env.per_period_cap == 40.0
    assert "手动定额" in env.标签


def test_clear_manual_restores_computed_envelope():
    BE.set_manual("双色球", monthly_budget=1.0, per_period_cap=1.0, note="临时")
    assert BE.default_envelope("双色球").monthly_budget == 1.0
    BE.clear_manual("双色球")
    assert BE.default_envelope("双色球").monthly_budget != 1.0
    assert BE.manual_overrides() == {}


def test_manual_takes_effect_immediately_mid_month():
    """「随时可调」的语义：额度一改，下一秒的判定就用新值。"""
    BE.set_manual("双色球", monthly_budget=200.0, per_period_cap=40.0,
                  baseline_tickets=20, note="200 元")
    env = BE.default_envelope("双色球")
    assert BE.decide(env, 200.0, net_ev_per_ticket=0.01)["判决"] == BE.VERDICT_STOP
    BE.set_manual("双色球", monthly_budget=300.0, note="月中追加 100")
    env2 = BE.default_envelope("双色球")
    assert BE.decide(env2, 200.0, net_ev_per_ticket=0.01)["判决"] == BE.VERDICT_JOIN


# ---------------------------------------------------------------- 闸门 + 动态
def test_gate_accepts_periods_and_only_tightens():
    env = _prod_env()
    loose = BE.gate_verdict("参与", env, 0.0)
    tight = BE.gate_verdict("参与", env, 4800.0, periods_done=11)
    assert loose["注数"] == 200
    assert tight["注数"] < loose["注数"]
    assert tight["包络"]["滚动分摊"]["是否被滚动收紧"] is True


@pytest.mark.parametrize("up", ["不参与", "本期不建议参与", "不出结论（数据不足）", "停（本月额度耗尽）"])
def test_dynamic_gate_still_never_upgrades(up):
    env = _prod_env()
    out = BE.gate_verdict(up, env, 0.0, periods_done=0)
    assert out["是否降级"] is False
    assert out["注数"] == 0
    assert "只降不升" in out["原因"]
