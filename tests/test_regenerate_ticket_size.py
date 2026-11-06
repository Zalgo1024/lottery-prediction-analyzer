# -*- coding: utf-8 -*-
"""「改到 1w 注 → 重新出号 → 又变回 1000 注」的回归锁（2026-11-07）。

事故链条（两段叠加，缺一段都不会被用户看见）：
  ① 卡片上「重新出号」只发彩种名。用户把滑块拖到 10000 但没点「保存」就点重出，
     后端 `resolve_groups` 读到的还是磁盘里的 1000 ⇒ 按 1000 出号；成功后
     `tsLoad()` 重绘 ⇒ 界面又变回 1000，看起来像系统把注数改回去了。
  ② 即使先保存，`prediction.engine.predict` 里 `PREDICT_DEFAULTS["max_groups"]=2000`
     会把 10000 静默截成 2000（11-06 22:50 流水线日志：「出号数量10000」而
     「预测 2000组」）⇒ 「能设 1w」与「真出 1w」长期不一致且不报错。

本文件锁四件事：卡片值先落盘且只写当前模式字段、不带值时不写盘、
`groups` 仍为 None（不绕过出号数量真源）、引擎封顶不得小于 HARD_CAP。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
MAIN_JS = ROOT / "web" / "static" / "js" / "main.js"
LOT = "双色球"


@pytest.fixture
def stub(monkeypatch):
    """隔离反馈台账 + 拦住真出号线程，只观察路由做了什么。"""
    import data.feedback as fb
    import web.routes.predict as P
    from web.app import app

    monkeypatch.setattr(fb, "load_pending", lambda lot: [])
    monkeypatch.setattr(fb, "_next_issue", lambda lot: "TEST-ISSUE")
    monkeypatch.setattr(fb, "next_renumber", lambda lot, issue: 1)
    monkeypatch.setattr(fb, "latest_production_pending", lambda *a, **kw: None)
    calls = {}
    monkeypatch.setattr(
        P, "start_predict",
        lambda lot, params: calls.update(lot=lot, params=params) or "task-x")
    return app.test_client(), calls


def _cfg() -> dict:
    from data import ticket_size as ts
    return json.loads(ts.STORE_PATH.read_text(encoding="utf-8"))


def _post(c, payload):
    return c.post("/api/predict/regenerate", json={"lottery": LOT, **payload})


# ---------------------------------------------------------------- ① 卡片值落盘
def test_regen_persists_card_value_in_dynamic_mode(stub):
    """动态模式：写 dynamic，fixed 意图锚点不许被动，锁定/模式保持。"""
    c, calls = stub
    before = _cfg()
    r = _post(c, {"出号数量": 10000})
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["已保存出号数量"] == 10000
    assert d["出号数量来源"].startswith("卡片当前值")

    after = _cfg()
    assert after["dynamic"][LOT] == 10000
    assert after["fixed"][LOT] == before["fixed"][LOT], "fixed 锚点被顺手覆盖了"
    assert after["mode"] == "dynamic"
    assert after["锁定"] is True
    # ★ 仍然走出号数量真源，不能用显式 groups 绕过配置与预算闸门
    assert calls["params"]["groups"] is None
    assert calls["params"]["record_pending"] is True
    assert calls["params"]["renumber"] == 1


def test_regen_persists_card_value_in_fixed_mode(stub):
    """固定模式：只写 fixed，dynamic 不许被顺手覆盖。"""
    from data import ticket_size as ts
    cfg = ts.sanitize(ts.load_config())
    cfg["mode"] = "fixed"
    ts.save_config(cfg)

    c, _ = stub
    before = _cfg()
    r = _post(c, {"出号数量": 800})
    assert r.status_code == 200
    assert r.get_json()["已保存出号数量"] == 800
    after = _cfg()
    assert after["fixed"][LOT] == 800
    assert after["dynamic"][LOT] == before["dynamic"][LOT]


def test_saved_value_is_what_resolve_groups_reads(stub, monkeypatch):
    """落盘后 `resolve_groups` 必须读到新值 —— 这才是「不再回滚」的端到端判据。"""
    import config
    import ev.budget_gate as bg
    c, _ = stub
    # 预算台账是另一套真源，这里打桩成直通，专测注数配置这一环
    monkeypatch.setattr(bg, "clamp",
                        lambda lot, want, **kw: {"注数": int(want)})
    assert config.resolve_groups(LOT) == 100
    assert _post(c, {"出号数量": 10000}).status_code == 200
    assert config.resolve_groups(LOT) == 10000


# ---------------------------------------------------------------- ② 写盘纪律
def test_regen_without_card_value_does_not_write(stub, monkeypatch):
    """不带参数 = 修复前的纯读行为（老客户端/脚本仍按配置生效值走）。"""
    from data import ticket_size as ts
    c, calls = stub
    orig, hits = ts.save_config, []
    monkeypatch.setattr(ts, "save_config",
                        lambda cfg: hits.append(cfg) or orig(cfg))
    r = _post(c, {})
    assert r.status_code == 200
    assert r.get_json()["已保存出号数量"] is None
    assert "配置生效值" in r.get_json()["出号数量来源"]
    assert hits == [], "没提交卡片值却写了盘"
    assert calls["params"]["groups"] is None


def test_regen_same_value_does_not_write(stub, monkeypatch):
    """值没变就不写盘（避免 8 个彩种统一重出时反复重写同一份配置）。"""
    from data import ticket_size as ts
    c, _ = stub
    cur = _cfg()["dynamic"][LOT]
    orig, hits = ts.save_config, []
    monkeypatch.setattr(ts, "save_config",
                        lambda cfg: hits.append(cfg) or orig(cfg))
    r = _post(c, {"出号数量": cur})
    assert r.get_json()["已保存出号数量"] == cur
    assert hits == []


def test_regen_clamps_card_value_to_limit(stub):
    """超过上限按 limits 夹取，并把**实际值**回传，不假装用户值生效了。"""
    c, _ = stub
    r = _post(c, {"出号数量": 999999})
    limit = _cfg()["limits"][LOT]
    assert r.get_json()["已保存出号数量"] == limit
    assert _cfg()["dynamic"][LOT] == limit


def test_regen_rejects_non_integer_card_value(stub):
    """非法值必须 400 且不启动出号 —— 拒绝分支要能真的走到。"""
    c, calls = stub
    r = _post(c, {"出号数量": "abc"})
    assert r.status_code == 400
    assert "出号数量" in (r.get_json().get("error") or "")
    assert "params" not in calls, "参数非法却已经启动了出号任务"


# ---------------------------------------------------------------- ③ 引擎封顶
def test_engine_cap_is_not_below_ticket_size_hard_cap():
    """`max_groups` 比 HARD_CAP 小 = 界面能设 1w、引擎却只出 2000 的静默失真。"""
    from config import PREDICT_DEFAULTS
    from data.ticket_size import HARD_CAP
    assert PREDICT_DEFAULTS["max_groups"] >= HARD_CAP


def test_predict_does_not_silently_cap_10000(monkeypatch, caplog):
    """真跑封顶分支：请求 10000 注时日志里的 groups 必须还是 10000。

    打桩 `load_lottery` 让函数在打完 groups 日志后立刻停下，避免真的算 1w 注；
    旧实现会在这里打出 `groups=2000`，测试即失败。
    """
    from prediction import engine

    def _stop(*a, **kw):
        raise RuntimeError("stop-after-cap")

    monkeypatch.setattr(engine, "load_lottery", _stop)
    with caplog.at_level("INFO"):
        with pytest.raises(RuntimeError, match="stop-after-cap"):
            engine.predict(LOT, groups=10000, record_pending=False)
    texts = [r.getMessage() for r in caplog.records]
    assert any("groups=10000" in t for t in texts), texts


def _distinct_tickets(n):
    import random
    from prediction.engine import PredictSet
    rng, seen, out = random.Random(11), set(), []
    while len(out) < n:
        reds = tuple(sorted(rng.sample(range(1, 34), 6)))
        blue = (rng.randint(1, 16),)
        if (reds, blue) in seen:
            continue
        seen.add((reds, blue))
        out.append(PredictSet(reds=list(reds), blues=list(blue), strategy="高频策略"))
    return out


def test_pad_success_never_logs_false_shortfall(caplog):
    """补齐成功时不能打「出号未达目标」——1w 注场景下的假告警会误导排障。"""
    from data.schema import get_schema
    from prediction.engine import _pad_to_groups

    with caplog.at_level("WARNING"):
        out, kpi = _pad_to_groups(_distinct_tickets(3), 30,
                                  get_schema("双色球"), lottery_name="双色球")
    assert len(out) == 30 and kpi["随机补齐"] == 27
    assert not any("出号未达目标" in r.getMessage() for r in caplog.records), \
        "补齐明明达标，却报「未达目标」"
    assert any("最终 30/30 组" in r.getMessage() for r in caplog.records), \
        "补齐来源与最终注数必须如实披露"


# ---------------------------------------------------------------- ④ 前端契约
def test_frontend_sends_card_value_on_regenerate():
    """静态契约：单彩种与批量共用的核心必须把卡片值带上，确认框必须说明会保存。"""
    js = MAIN_JS.read_text(encoding="utf-8")
    assert "function _tsCardValue(" in js, "没有从卡片 DOM 读当前值的函数"
    core = re.search(r"function\s+_tsRegenerateOne\s*\([^)]*\)\s*\{(.*?)\n\}",
                     js, re.S)
    assert core, "找不到 _tsRegenerateOne"
    assert "_tsCardValue(" in core.group(1), "重出请求没读卡片值"
    assert "出号数量" in core.group(1), "重出请求没带出号数量"
    single = re.search(r"function\s+tsRegenerate\s*\([^)]*\)\s*\{(.*?)\n\}",
                       js, re.S)
    assert single and "保存为当前生效值" in single.group(1), \
        "确认框没有说明本次会保存卡片值（用户不该被静默改写配置）"
