# -*- coding: utf-8 -*-
"""tests/test_budget_envelope.py —— 预算包络（v1.8 用户指令一）。

这里锁的不是「算得对不对」，而是三条**纪律**：
  ① 不追损、不因看好而加注；② 定额 + 单期上限，亏完即停；③ 可以不参与（且默认为不参与）。
另锁一条安全属性：闸门**单向**——只降级、永不升级。
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from ev import budget_envelope as BE


def _env(**kw):
    base = dict(monthly_budget=1000.0, per_period_cap=400.0,
                cost_per_ticket=2.0, baseline_tickets=200, lottery="双色球")
    base.update(kw)
    return BE.Envelope(**base)


# ---------------------------------------------------------------- ① 包络构造
def test_envelope_rejects_negative_budget():
    with pytest.raises(ValueError, match="月度定额不能为负"):
        BE.Envelope(monthly_budget=-1.0, per_period_cap=10.0)


def test_envelope_rejects_cap_above_monthly():
    """单期上限吃掉整月额度 ⇒ 包络失去意义，必须在构造时炸掉。"""
    with pytest.raises(ValueError, match="大于月度定额"):
        BE.Envelope(monthly_budget=100.0, per_period_cap=200.0)


def test_envelope_rejects_bad_cost():
    with pytest.raises(ValueError, match="单注成本必须为正"):
        BE.Envelope(monthly_budget=1000.0, per_period_cap=100.0, cost_per_ticket=0.0)


def test_cap_tickets_floors():
    assert _env(monthly_budget=1000.0, per_period_cap=39.0).cap_tickets == 19
    assert _env(monthly_budget=1000.0, per_period_cap=40.0).cap_tickets == 20


# ---------------------------------------------------------------- ② 折算
def test_allocate_uses_baseline_and_cap():
    a = BE.allocate(_env(), 0.0)
    assert a["注数"] == 200            # 基线 200 注 = 400 元 = 单期上限
    assert a["本期成本元"] == 400.0
    assert a["剩余额度"] == 1000.0


def test_allocate_limited_by_remaining_budget():
    a = BE.allocate(_env(), 900.0)
    assert a["注数"] == 50             # 剩 100 元 ⇒ 50 注
    assert any("剩余额度" in r for r in a["原因"])


def test_allocate_zero_when_exhausted():
    a = BE.allocate(_env(), 1000.0)
    assert a["注数"] == 0 and a["剩余额度"] == 0.0
    assert any("额度已耗尽" in r for r in a["原因"])


def test_allocate_overdraft_is_clamped_not_negative():
    """已投超定额（跨月前的历史）不得算出负注数。"""
    a = BE.allocate(_env(), 5000.0)
    assert a["注数"] == 0 and a["剩余额度"] == 0.0


def test_allocate_no_parity_forcing():
    """★ 不取偶：数字型生产注数本来就有奇数（3D 5 注），取偶会凭空删一注。"""
    env = _env(monthly_budget=300.0, per_period_cap=10.0, baseline_tickets=5,
               lottery="福彩3D")
    assert BE.allocate(env, 0.0)["注数"] == 5


def test_allocate_rejects_more_than_baseline():
    a = BE.allocate(_env(), 0.0, want_tickets=300)
    assert a["注数"] == 200
    assert any("超过基线" in r for r in a["原因"])


def test_allocate_default_want_is_baseline():
    env = _env(monthly_budget=10000.0, per_period_cap=10000.0, baseline_tickets=38,
               lottery="大乐透")
    assert BE.allocate(env, 0.0, want_tickets=None)["注数"] == 38


# ---------------------------------------------------------------- ③ 追损/加注
def test_chase_guard_refuses_after_loss():
    g = BE.chase_guard(_env(), 400, recent_pnl=-880.0)
    assert g["是否拒绝加注"] is True and g["允许注数"] == 200
    assert any("追损" in s for s in g["说明"])


def test_chase_guard_refuses_even_when_up():
    """「这期更看好」也不是加注理由：加注不改变单注概率。"""
    g = BE.chase_guard(_env(), 250, recent_pnl=+300.0)
    assert g["是否拒绝加注"] is True and g["允许注数"] == 200
    assert any("E[中奖注数]" in s for s in g["说明"])


def test_chase_guard_passes_at_baseline():
    g = BE.chase_guard(_env(), 200)
    assert g["是否拒绝加注"] is False and g["允许注数"] == 200


def test_chase_guard_clamps_negative_request():
    assert BE.chase_guard(_env(), -5)["允许注数"] == 0


# ---------------------------------------------------------------- ④ 判决
def test_decide_unknown_ev_defaults_to_skip():
    """净 EV 不可得必须落在「不亏钱」的一侧。"""
    d = BE.decide(_env(), 0.0, None)
    assert d["判决"] == BE.VERDICT_SKIP and d["注数"] == 0
    assert "不可得" in d["原因"]


def test_decide_negative_ev_is_skip():
    d = BE.decide(_env(), 0.0, -0.9510)
    assert d["判决"] == BE.VERDICT_SKIP and d["注数"] == 0
    assert "≤ 0" in d["原因"]


def test_decide_positive_ev_within_envelope_joins():
    d = BE.decide(_env(), 0.0, 0.05)
    assert d["判决"] == BE.VERDICT_JOIN and d["注数"] == 200
    assert d["包络"]["本期成本元"] == 400.0


def test_decide_stops_when_month_exhausted_even_if_ev_positive():
    d = BE.decide(_env(), 1000.0, 5.0)
    assert d["判决"] == BE.VERDICT_STOP and d["注数"] == 0


def test_decide_is_deterministic():
    a, b = BE.decide(_env(), 300.0, 0.02), BE.decide(_env(), 300.0, 0.02)
    assert a == b


# ---------------------------------------------------------------- ⑤ 单向闸门
@pytest.mark.parametrize("up", ["不参与", "本期不建议参与", "不出结论（数据不足）",
                                "停（本月额度耗尽）", None, ""])
def test_gate_never_upgrades(up):
    """★★ 安全属性：上游说不参与，闸门绝不能改成参与（哪怕 EV 为正）。"""
    g = BE.gate_verdict(up, _env(), 0.0, net_ev_per_ticket=9.99)
    assert g["是否降级"] is False and g["注数"] == 0
    assert "只降不升" in g["原因"]
    if up:
        assert g["最终判决"] == str(up)


def test_gate_downgrades_on_negative_ev():
    g = BE.gate_verdict("条件性可参与", _env(), 0.0, net_ev_per_ticket=-0.95)
    assert g["最终判决"] == BE.VERDICT_SKIP
    assert g["是否降级"] is True and g["注数"] == 0
    assert "包络闸门降级" in g["原因"]


def test_gate_downgrades_when_month_exhausted():
    g = BE.gate_verdict("条件性可参与", _env(), 1000.0, net_ev_per_ticket=0.5)
    assert g["最终判决"] == BE.VERDICT_STOP and g["是否降级"] is True


def test_gate_keeps_upstream_wording_when_passing():
    """通过时保留上游措辞（「条件性可参与」不能被改写成「参与」）。"""
    g = BE.gate_verdict("条件性可参与", _env(), 0.0, net_ev_per_ticket=0.5)
    assert g["最终判决"] == "条件性可参与" and g["注数"] == 200
    assert g["是否降级"] is False


def test_gate_caps_tickets_by_envelope():
    env = _env(monthly_budget=1000.0, per_period_cap=76.0, baseline_tickets=38,
               lottery="大乐透")
    g = BE.gate_verdict("参与", env, 0.0, net_ev_per_ticket=0.5, want_tickets=200)
    assert g["注数"] == 38


# ---------------------------------------------------------------- ⑥ 诚实边界
@pytest.mark.parametrize("kwargs", [
    {"spent": 0.0, "ev": -1.0}, {"spent": 0.0, "ev": 1.0}, {"spent": 9999.0, "ev": 1.0},
])
def test_honest_note_on_every_path(kwargs):
    d = BE.decide(_env(), kwargs["spent"], kwargs["ev"])
    for out in (d, d["包络"], BE.gate_verdict("参与", _env(), kwargs["spent"],
                                              net_ev_per_ticket=kwargs["ev"])):
        assert "封顶" in out["诚实边界"] and "公益金" in out["诚实边界"]


def test_honest_note_states_no_probability_change():
    assert "不改变任何一注的中奖概率" in BE.HONEST_NOTE


# ---------------------------------------------------------------- ⑦ 月度换算
def test_periods_per_month_by_type():
    assert BE.periods_per_month("双色球") == 13
    assert BE.periods_per_month("福彩3D") == 30


def test_monthly_report_is_negative_at_market_return_rate():
    r = BE.monthly_report(_env(monthly_budget=5200.0), return_rate=0.5245)
    assert r["每月可执行期数"] == 13
    assert r["月计划投入元"] == 5200.0
    assert r["期望净亏元"] < 0
    assert r["期望净亏元"] == pytest.approx(5200.0 * (0.5245 - 1.0), abs=0.01)


def test_monthly_report_capped_by_budget_not_by_issue_count():
    """定额买不满整月期数时，报告的期数必须跟着定额走（不许假装投满）。"""
    r = BE.monthly_report(_env(monthly_budget=800.0), return_rate=0.5)
    assert r["每月可执行期数"] == 2 and r["月计划投入元"] == 800.0


# ---------------------------------------------------------------- ⑧ 默认包络
def test_default_envelope_uses_production_count():
    env = BE.default_envelope("双色球")
    assert env.baseline_tickets >= 1
    assert env.per_period_cap == pytest.approx(env.baseline_tickets * 2.0)
    assert env.monthly_budget == pytest.approx(env.per_period_cap * 13)
    assert "非建议投入" in env.标签


def test_default_envelopes_cover_all_lotteries():
    envs = BE.default_envelopes()
    assert set(envs) == set(BE.PERIODS_PER_MONTH)
    for env in envs.values():
        assert env.baseline_tickets >= 1 and env.monthly_budget >= env.per_period_cap


# ---------------------------------------------------------------- ⑨ 账本
def test_ledger_accumulates(tmp_path):
    led = BE.Ledger(tmp_path / "ledger.json")
    t = datetime(2026, 10, 26, 21, 0)
    led.add(400.0, now=t, note="双色球")
    led.add(76.0, now=t, note="大乐透")
    assert led.spent(t) == 476.0
    assert len(led.read(t)["记录"]) == 2


def test_ledger_resets_on_new_month(tmp_path):
    led = BE.Ledger(tmp_path / "ledger.json")
    led.add(400.0, now=datetime(2026, 10, 26, 21, 0))
    r = led.read(datetime(2026, 11, 2, 9, 0))
    assert r["已投"] == 0.0 and r["月份"] == "2026-11"
    assert r["_上月"]["已投"] == 400.0          # 历史保留，只账面归零


def test_ledger_write_is_atomic_no_tmp_left(tmp_path):
    p = tmp_path / "ledger.json"
    led = BE.Ledger(p)
    led.add(10.0, now=datetime(2026, 10, 1, 8, 0))
    assert p.exists() and not p.with_suffix(p.suffix + ".tmp").exists()
    assert json.loads(p.read_text(encoding="utf-8"))["已投"] == 10.0


def test_ledger_corrupt_file_raises_not_silently_zero(tmp_path):
    """★ 坏账本不许当 0 —— 那会把「已投多少」抹掉，等于放开额度。"""
    p = tmp_path / "ledger.json"
    p.write_text("{bad json", encoding="utf-8")
    with pytest.raises(Exception):
        BE.Ledger(p).spent(datetime(2026, 10, 26))


def test_ledger_reset(tmp_path):
    led = BE.Ledger(tmp_path / "ledger.json")
    t = datetime(2026, 10, 26, 21, 0)
    led.add(400.0, now=t)
    led.reset(now=t)
    assert led.spent(t) == 0.0


def test_ledger_path_redirected_by_conftest():
    """★ 根 conftest 的 autouse 夹具必须把 LEDGER_PATH 指到 tmp（防写真实账本）。"""
    assert "pytest" in str(BE.LEDGER_PATH).lower() or "tmp" in str(BE.LEDGER_PATH).lower()
