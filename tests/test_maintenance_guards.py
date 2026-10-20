# -*- coding: utf-8 -*-
"""低维护护栏（2026-10-21）：注数一致性体检 + 启动自愈单飞。

背景（用户诉求："国庆之后没有天天开，能不能别那么频繁地维护"）：
  无人值守时的真实故障不是"算错了"，而是**静默失真** ——
  机器在跑、日志全绿、报表照长，但把用户设的 200/38/50/44 悄悄换成 5 注，
  页面上没有一个字提示。所以：
    1) 把「滑轨 == 下一期 pending 组数」做成常驻体检（groups_invariant）；
    2) 冷启时自动修复，而不是等用户自己发现（ensure_pending_groups）；
    3) 同一轮恢复里每个彩种只触发一次流水线（_claim_trigger 单飞）。
"""

import pytest

from web import startup_recovery as SR
from web.utils import groups_invariant


@pytest.fixture
def patched(monkeypatch):
    """把 pending / 期号 / 滑轨全部替身化，绝不读真实生产数据。"""
    from config import LOTTERY_CONFIG
    import config

    slide = {lot: 10 for lot in LOTTERY_CONFIG}
    state = {"pending": {}, "next": {lot: 26121 for lot in LOTTERY_CONFIG},
             "triggered": []}

    monkeypatch.setattr(config, "resolve_groups",
                        lambda lot, explicit=None: slide[lot])
    monkeypatch.setattr("data.feedback.load_pending",
                        lambda lot: list(state["pending"].get(lot) or []))
    monkeypatch.setattr("data.feedback._next_issue",
                        lambda lot: state["next"][lot])

    def _fake_start(lottery, params):
        state["triggered"].append((lottery, dict(params)))
        return f"task-{lottery}"

    monkeypatch.setattr("web.utils.start_auto_pipeline", _fake_start)
    SR._reset_trigger_registry()
    yield {"slide": slide, "state": state, "lots": list(LOTTERY_CONFIG)}
    SR._reset_trigger_registry()


def _pend(issue, n, groups_declared=None):
    return {"目标期号": issue,
            "预测号码": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]} for _ in range(n)],
            "号码组数": groups_declared if groups_declared is not None else n,
            "生成时间": "2026-10-21T10:00:00"}


# ------------------------------------------------------------
# groups_invariant：三态语义
# ------------------------------------------------------------

def test_invariant_consistent(patched):
    st = patched["state"]
    for lot in patched["lots"]:
        st["pending"][lot] = [_pend(st["next"][lot], 10)]
    rep = groups_invariant()
    assert rep["全部一致"] is True
    assert rep["不一致"] == []
    assert {r["状态"] for r in rep["彩种"]} == {"一致"}


def test_invariant_flags_overwritten_count(patched):
    """5 注覆盖事故的形态：滑轨 10，pending 只有 5 注 ⇒ 必须报不一致。"""
    st = patched["state"]
    for lot in patched["lots"]:
        st["pending"][lot] = [_pend(st["next"][lot], 10)]
    st["pending"]["大乐透"] = [_pend(st["next"]["大乐透"], 5)]
    rep = groups_invariant()
    assert rep["全部一致"] is False
    assert rep["不一致"] == ["大乐透"]
    row = next(r for r in rep["彩种"] if r["彩种"] == "大乐透")
    assert row["滑轨"] == 10 and row["组数"] == 5
    assert "≠" in row["说明"]


def test_invariant_no_pending_is_neutral_not_error(patched):
    """开奖后到下一次出号之间的空窗不是错误，不能把徽章一直挂红。"""
    rep = groups_invariant()
    assert rep["全部一致"] is True
    assert {r["状态"] for r in rep["彩种"]} == {"无预测"}


def test_invariant_ignores_stale_pending_for_other_issue(patched):
    """上一期遗留的 pending 不参与本期一致性判定。"""
    st = patched["state"]
    st["pending"]["双色球"] = [_pend(st["next"]["双色球"] - 1, 3)]
    rep = groups_invariant(["双色球"])
    assert rep["彩种"][0]["状态"] == "无预测"


def test_invariant_survives_per_lottery_exception(patched, monkeypatch):
    """单个彩种检查抛异常不能让整个体检 500。"""
    def _boom(lot):
        raise RuntimeError("pending 文件损坏")
    monkeypatch.setattr("data.feedback.load_pending", _boom)
    rep = groups_invariant()
    assert all(r["状态"] == "检查失败" for r in rep["彩种"])
    assert rep["全部一致"] is True   # 不把"查不出来"当成"不一致"


# ------------------------------------------------------------
# ensure_pending_groups：启动自愈
# ------------------------------------------------------------

def test_ensure_pending_groups_repairs_overwritten(patched):
    st = {lot: 10 for lot in patched["lots"]}
    st["排列3"] = 50
    for lot, n in st.items():
        patched["slide"][lot] = n
        patched["state"]["pending"][lot] = [_pend(26121, 5)]   # 全被覆盖成 5 注
    fixed = SR.ensure_pending_groups()
    assert set(fixed) == set(patched["lots"])
    assert all(v["原组数"] == 5 for v in fixed.values())
    assert len(patched["state"]["triggered"]) == len(patched["lots"])
    # 触发参数必须带正确滑轨值，且是 fresh 模式
    for lot, params in patched["state"]["triggered"]:
        assert params["groups"] == patched["slide"][lot]
        assert params["mode"] == "fresh"


def test_ensure_pending_groups_noop_when_consistent(patched):
    for lot in patched["lots"]:
        patched["state"]["pending"][lot] = [_pend(26121, 10)]
    assert SR.ensure_pending_groups() == {}
    assert patched["state"]["triggered"] == []


def test_ensure_pending_groups_claims_single_flight(patched):
    """本轮已被别的路径触发过 ⇒ 自愈不再重复开一条流水线。"""
    for lot in patched["lots"]:
        patched["state"]["pending"][lot] = [_pend(26121, 5)]
    SR._reset_trigger_registry()
    for lot in patched["lots"]:
        assert SR._claim_trigger(lot) is True
    fixed = SR.ensure_pending_groups()
    assert fixed == {}
    assert patched["state"]["triggered"] == []


# ------------------------------------------------------------
# 单飞语义
# ------------------------------------------------------------

def test_claim_trigger_is_idempotent(patched):
    assert SR._claim_trigger("双色球") is True
    assert SR._claim_trigger("双色球") is False
    SR._reset_trigger_registry()
    assert SR._claim_trigger("双色球") is True


def test_startup_recovery_runs_only_once(monkeypatch):
    """冷启只跑一次（此前 recover/ensure_fresh/worker 三方同时开火）。"""
    monkeypatch.setattr(SR, "_started", True)
    assert SR.run_startup_recovery() is None


# ------------------------------------------------------------
# 预测页：曾经的「5 注覆盖」活温床
# ------------------------------------------------------------

def test_predict_page_defaults_to_slide_not_five():
    """组数下拉不能再默认 5 组（那正是 5 注事故的入口）。"""
    from pathlib import Path
    html = (Path(__file__).resolve().parents[1] / "web" / "templates" / "predict.html").read_text(encoding="utf-8")
    assert '<option value="" selected>按出号数量' in html
    assert "5 组（推荐）" not in html
    assert 'id="pred-record"' in html


def test_predict_js_enforces_record_pending():
    """前端必须显式发 record_pending，且显式注数≠滑轨时强制 false。"""
    from pathlib import Path
    js = (Path(__file__).resolve().parents[1] / "web" / "static" / "js" / "main.js").read_text(encoding="utf-8")
    assert "record_pending: record" in js
    assert "本次仅试算，不写生产 pending" in js


def test_validator_allows_null_groups():
    """groups: null 必须放行（留空 → 按滑轨解析），否则滑轨口径被挡住。"""
    from flask import Flask
    from web.validators import validate_predict_body

    app = Flask(__name__)

    @app.route("/t", methods=["POST"])
    @validate_predict_body
    def _t():
        return {"ok": True}

    c = app.test_client()
    assert c.post("/t", json={"groups": None}).status_code == 200
    assert c.post("/t", json={}).status_code == 200
    assert c.post("/t", json={"groups": 200}).status_code == 200
    assert c.post("/t", json={"groups": 0}).status_code == 400
    assert c.post("/t", json={"groups": 99999}).status_code == 400
