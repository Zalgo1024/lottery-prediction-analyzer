# -*- coding: utf-8 -*-
"""tests/test_shadow_status_live.py —— 影子验证「可运行状态」的接线与牙齿测试。

锁的东西：
  ① 状态快照是**只读**的，读不到就写原因，绝不记为通过；
  ② 门槛可行性把「还需 N 期」翻译成年限，且不读系统时间、不做判决；
  ③ 报告 / CLI / Web 路由 / 静态看板 / 调度器日扫都能拿到同一份状态；
  ④ 新增接线没有放宽 import 护栏，也没有把研究模块塞进生产四包。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ev import prefix_shadow as PS
from ev import shadow_bridge as SB

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("证伪", "证否", "不可预测", "已证明公平", "公平随机", "写死")


def _gate(need=1000):
    return {"可用": True, "原因": [], "门槛期数": need, "有限格数": 1,
            "不可判格数": 0, "逐格": [], "主口径n": 3}


# ---------------------------------------------------------------- ① 可行性
def test_annual_periods_is_data_driven_and_time_free():
    """同一份数据连算两次必须完全相同（不读系统时间）。"""
    a = PS.annual_periods(lotteries=["双色球"])
    b = PS.annual_periods(lotteries=["双色球"])
    assert a == b
    assert "双色球" in a
    assert a["双色球"]["每年期数"] is None or a["双色球"]["每年期数"] > 0


def test_annual_periods_missing_lottery_writes_reason():
    r = PS.annual_periods(lotteries=["不存在彩种"])
    assert r["不存在彩种"]["每年期数"] is None
    assert r["不存在彩种"]["原因"]


def test_feasibility_translates_periods_into_years():
    annual = {"双色球": {"每年期数": 150}, "排列3": {"每年期数": 350}}
    f = PS.feasibility(_gate(17141), annual=annual)
    assert f["可用"] is True and f["门槛期数"] == 17141
    assert f["门槛彩种"] == "双色球"
    assert f["主口径每年期数"] == 150
    assert f["主口径预计年数"] == pytest.approx(114.3, abs=0.2)
    assert f["最快预计年数"] == pytest.approx(49.0, abs=0.2)
    assert f["最慢预计年数"] == pytest.approx(114.3, abs=0.2)
    assert any("不能叠加" in n for n in f["披露"])
    assert any("不构成证明" in n for n in f["披露"])
    assert any("不代判" in n for n in f["披露"])


def test_feasibility_unavailable_gate_is_not_a_pass():
    gate = {"可用": False, "原因": ["未提供判决层读数"], "门槛期数": None}
    f = PS.feasibility(gate, annual={"双色球": {"每年期数": 150}})
    assert f["可用"] is False and f["原因"]
    assert f["披露"]


def test_feasibility_handles_missing_annual_data():
    f = PS.feasibility(_gate(1000),
                       annual={"双色球": {"每年期数": None, "原因": "数据不可读"}})
    assert f["主口径预计年数"] is None
    assert f["最快预计年数"] is None
    assert f["逐彩种"][0]["原因"] == "数据不可读"


# ---------------------------------------------------------------- ② 状态快照
def test_status_snapshot_structure_and_honest_defaults(tmp_path):
    d = SB.status_snapshot(as_of="2026-11-07T02:30:00")
    assert d["生成时刻"] == "2026-11-07T02:30:00"
    assert d["编排版本"] == SB.BRIDGE_VERSION
    for k in ("开关", "批次性质预注册", "影子状态", "快照体检",
              "方案B证据", "门槛可行性", "最近登记", "纪律"):
        assert k in d, k
    assert d["影子状态"]["可开判"] is False
    assert d["影子状态"]["状态"] == "未开判"
    assert d["快照体检"]["文件总数"] >= d["快照体检"]["合格数"]
    assert d["纪律"] and any("缺席≠通过" in n for n in d["纪律"])


def test_status_snapshot_without_as_of_does_not_invent_now():
    d = SB.status_snapshot()
    assert "未提供" in d["生成时刻"]


def test_status_snapshot_switch_off_writes_reason(tmp_path):
    d = SB.status_snapshot(as_of="x", switch_path=tmp_path / "no_switch.json")
    assert d["开关"]["启用"] is False
    assert d["开关"]["原因"] and "不 fail-open" in d["开关"]["原因"]
    assert d["影子状态"]["可开判"] is False


def test_status_snapshot_missing_verdict_is_not_a_pass(tmp_path):
    d = SB.status_snapshot(as_of="x", verdict_path=tmp_path / "no_verdict.json")
    g = d["影子状态"]["门槛"]
    assert g["可用"] is False
    assert d["影子状态"]["可开判"] is False
    assert any("缺席≠通过" in r for r in d["影子状态"]["未开判原因"]
               ) or g["原因"]


def test_status_snapshot_evidence_denominator_is_explicit():
    d = SB.status_snapshot(as_of="x")
    e = d["方案B证据"]
    assert "行数" in e and "坏行数" in e and "逐彩种条数" in e
    assert "未参与" in e["交叉校验"]
    assert "不构成" in e["披露"]


def test_external_evidence_source_missing_is_conservative(tmp_path):
    """开关声明了外部源却读不到 ⇒ 不登记并写原因，绝不静默当「没有矛盾」。"""
    from ev import draw_schedule as DS

    sched = tmp_path / "sched.json"
    doc = DS.input_template()
    for row in doc["彩种"].values():
        row["开奖时刻"] = "21:15"
    sched.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    sw = tmp_path / "switch.json"
    sw.write_text(json.dumps({"启用": True,
                              "外部源": str(tmp_path / "no_evidence.jsonl")},
                             ensure_ascii=False), encoding="utf-8")
    r = SB.freeze_after_predict(
        lottery="双色球", issue="26128",
        tickets=[{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}] * 3,
        K=3, target_date="2026-11-08", batch_time="2026-11-07T10:00:00",
        register_time="2026-11-07T10:00:05",
        switch_path=sw, schedule_path=sched,
        rules_path=tmp_path / "no_rules.json",
        snapshot_dir=tmp_path / "snap", shadow_path=tmp_path / "shadow.jsonl")
    assert r["状态"] == SB.STATUS_NA
    assert "外部证据源不存在" in r["原因"]
    assert any("缺席≠通过" in n for n in r["披露"])


# ---------------------------------------------------------------- ③ 接线
def test_report_has_feasibility_section_and_build_exposes_it(tmp_path):
    import scripts.prefix_shadow_report as R
    audit = PS.audit_freeze_dir(tmp_path / "no_dir")
    status = PS.shadow_status(verdict={"主口径n": 3, "格": [
        {"n": 3, "门槛": "≥5元", "臂": "a", "n*_保守": 1000.0}]},
        path=tmp_path / "no_shadow.jsonl")
    feas = PS.feasibility(status["门槛"],
                          annual={"双色球": {"每年期数": 150}})
    text = R.render(audit, status, verdict_path=tmp_path / "v.json",
                    verdict={}, feasibility=feas)
    assert "4b. 门槛可行性" in text
    assert "不能相加" in text
    assert "6.7" in text           # 1000 / 150
    # 不传 feasibility 也要能渲染（旧调用点零改动）
    assert R.render(audit, status, verdict_path=tmp_path / "v.json",
                    verdict={})


def test_web_utils_wrappers_are_read_only(monkeypatch):
    import web.utils as U
    calls = []
    monkeypatch.setattr(SB, "settle_after_draw",
                        lambda **kw: calls.append(kw) or {"启用": True, "已结算": 0,
                                                         "跳过": 0, "失败": 0})
    d = U.shadow_status_snapshot(as_of="2026-11-07T02:30:00")
    assert "影子状态" in d and d["生成时刻"] == "2026-11-07T02:30:00"
    r = U.shadow_settle_tick(settle_time="2026-11-07T22:30:00")
    assert r["启用"] is True
    assert calls == [{"settle_time": "2026-11-07T22:30:00"}]


def test_shadow_refresh_static_writes_only_display_artifact(tmp_path):
    import web.utils as U
    out = tmp_path / "data" / "shadow_status.json"
    r = U.shadow_refresh_static(out=out, as_of="2026-11-07T02:30:00")
    assert r["ok"] is True and out.is_file()
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["生成时刻"] == "2026-11-07T02:30:00"
    assert d["页面"] == "/static/shadow.html"
    assert "影子状态" in d


def test_web_utils_wrapper_failure_is_not_a_pass(monkeypatch):
    import web.utils as U

    def _boom(**kw):
        raise RuntimeError("gone")
    monkeypatch.setattr(SB, "settle_after_draw", _boom)
    r = U.shadow_settle_tick(settle_time="x")
    assert r["启用"] is False and "不可用" in r["原因"]
    assert r["已结算"] == 0


def test_api_shadow_status_route_returns_snapshot():
    from web.app import app
    c = app.test_client()
    r = c.get("/api/shadow/status")
    assert r.status_code == 200
    d = r.get_json()
    assert "影子状态" in d and "门槛可行性" in d
    assert "适用" not in d or d.get("适用") is not False


def test_scheduler_daily_settle_hook_is_throttled(monkeypatch):
    from datetime import datetime
    from web import scheduler as S

    calls = []
    monkeypatch.setattr("web.utils.shadow_settle_tick",
                        lambda **kw: calls.append(kw) or {"启用": True,
                                                         "已结算": 0, "跳过": 0,
                                                         "失败": 0})
    monkeypatch.setattr("web.utils.shadow_refresh_static",
                        lambda **kw: {"ok": True, "路径": "stub"})
    S.AutoScheduler._shadow_settle_day = None
    sch = S.AutoScheduler()
    assert sch._maybe_settle_shadow(datetime(2026, 11, 7, 21, 0)) is False
    assert calls == []
    assert sch._maybe_settle_shadow(datetime(2026, 11, 7, 22, 31)) is True
    assert sch._maybe_settle_shadow(datetime(2026, 11, 7, 23, 0)) is False
    assert len(calls) == 1
    assert sch._maybe_settle_shadow(datetime(2026, 11, 8, 22, 31)) is True
    assert len(calls) == 2


def test_cli_shadow_subcommand_is_wired():
    src = (ROOT / "cli.py").read_text(encoding="utf-8")
    assert 'subparsers.add_parser(\n        "shadow"' in src
    assert 'args.command == "shadow"' in src
    assert "_cmd_shadow" in src


def test_static_page_and_script_exist_and_use_same_snapshot():
    html = (ROOT / "web" / "static" / "shadow.html").read_text(encoding="utf-8")
    assert "/static/data/shadow_status.json" in html
    assert "不适用" in html or "不记为通过" in html
    assert 'd.适用 === false' in html           # 后端说「不适用」时不能显示成 0
    src = (ROOT / "scripts" / "shadow_status.py").read_text(encoding="utf-8")
    assert "status_snapshot" in src and "shadow_status.json" in src
    assert "--settle" in src


def test_import_guard_still_clean_after_wiring():
    from tests._import_guard import violations
    assert violations() == []


def test_new_texts_have_no_forbidden_words():
    for rel in ("ev/shadow_bridge.py", "ev/prefix_shadow.py",
                "scripts/prefix_shadow_report.py", "scripts/shadow_status.py",
                "web/static/shadow.html", "web/routes/ev.py", "cli.py",
                "web/utils.py", "web/scheduler.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{rel} 出现禁词 {bad}"
