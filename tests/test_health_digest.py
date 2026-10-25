# -*- coding: utf-8 -*-
"""离线体检周报（web/health_digest.py）的契约测试。

背景：`scripts/health_check.py` 写得很完整，却**没有任何调用方** ——
它只能被人手动叫醒，等于把"维护"绑死在"人得在线"上。这一层补的就是
"谁来叫醒它"和"叫醒后要不要吵人"。

这里锁三件事：
  1. 口径：窗口外的记录不能算进日报（否则永远一片绿）；
  2. 静默：无问题时**不推送**（每天说一句"正常"只会被静音）；
  3. 时点：日报与冷启补报各占一个槽，一天各一次（重启风暴不刷屏）。
"""

import json
from datetime import datetime, timedelta

import pytest

import web.health_digest as H


# ★ 2026-10-25 修：本文件原先把 `datetime.now()` 当基准 ⇒ 运行日恰逢 **周日**
#   （`FULL_HEALTH_WEEKDAY = 6`）时，周检槽位会与日报槽位同时触发，
#   `== ["daily"]` 必然挂掉；更糟的是测试会**真跑一遍六项全扫描**并把报告写进真实 logs/。
#   ⇒ 钉死在固定的周三（weekday=2 ≠ 6），与运行日期彻底解耦。
FIXED_DAY = datetime(2026, 10, 21)          # 2026-10-21 是周三


def _at(hour: int, minute: int = 0) -> datetime:
    return FIXED_DAY.replace(hour=hour, minute=minute, second=0, microsecond=0)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """把 digest 的所有落盘路径钉到 tmp，并把重活（读 CSV/注数体检）替换成假数据。"""
    status = tmp_path / "status.json"
    monkeypatch.setattr(H, "STATUS_FILE", status)
    monkeypatch.setattr(H, "DIGEST_DIR", tmp_path / "automation")
    monkeypatch.setattr(H, "HEALTH_DIR", tmp_path / "health")
    monkeypatch.setattr(H, "STATE_PATH", tmp_path / "digest_state.json")
    monkeypatch.setattr(H, "HEALTH_DIR", tmp_path / "health")
    monkeypatch.setattr(H, "DIGEST_DIR", tmp_path / "automation")
    # 兜底：任何用例若不小心落到周检槽位，也绝不允许真跑六项全扫描 / 写真实报告
    monkeypatch.setattr(H, "run_full_health",
                        lambda **k: {"问题": [], "_stub": True})
    H._CACHE.clear()   # 接口走 cached_digest，用例之间必须清缓存
    (tmp_path / "health").mkdir()

    def _fake_status(history):
        status.write_text(json.dumps({"history": history}, ensure_ascii=False), encoding="utf-8")

    # 默认：注数全一致、无滞后
    import web.utils as W
    monkeypatch.setattr(W, "groups_invariant", lambda *a, **k: {
        "全部一致": True, "不一致": [], "彩种": [
            {"彩种": n, "滑轨": 5, "组数": 5, "状态": "一致", "一致": True}
            for n in ("双色球", "大乐透")
        ]})
    monkeypatch.setattr(W, "get_data_status", lambda lot, now=None: {
        "lottery": lot, "latest_draw": "26100", "latest_date": "2026-10-20",
        "data_lag_days": 0})

    return {"root": tmp_path, "set_status": _fake_status, "status": status}


def _entry(days_ago: float, ok=True, lottery="双色球", msg="ok"):
    ts = datetime.now() - timedelta(days=days_ago)
    return {"time": ts.isoformat(timespec="seconds"), "lottery": lottery,
            "success": ok, "message": msg}


# ------------------------------------------------------------ 口径
def test_digest_counts_only_window(env):
    env["set_status"]([_entry(0.1), _entry(1), _entry(30)])
    d = H.build_digest(days=7)
    assert d["流水线"]["总次数"] == 2
    assert d["流水线"]["窗口内无记录"] is False


def test_digest_flags_pipeline_failure(env):
    env["set_status"]([_entry(0.2, ok=False, lottery="大乐透", msg="抓取失败")])
    d = H.build_digest(days=7)
    assert d["流水线"]["失败"] == 1
    assert any(p["项"] == "流水线失败" for p in d["问题"])
    assert d["结论"].startswith("有 1 项")


def test_digest_flags_groups_mismatch(env, monkeypatch):
    import web.utils as W
    monkeypatch.setattr(W, "groups_invariant", lambda *a, **k: {
        "全部一致": False, "不一致": ["双色球"],
        "彩种": [{"彩种": "双色球", "滑轨": 200, "组数": 5, "状态": "不一致", "一致": False}]})
    env["set_status"]([])
    d = H.build_digest(days=7)
    assert d["注数一致性"]["全部一致"] is False
    assert any(p["项"] == "注数不一致" and "200" in p["详情"] for p in d["问题"])


def test_digest_flags_data_lag(env, monkeypatch):
    import web.utils as W
    monkeypatch.setattr(W, "get_data_status", lambda lot, now=None: {
        "lottery": lot, "latest_draw": "26100", "data_lag_days": 3})
    env["set_status"]([])
    d = H.build_digest(days=7)
    assert any(p["项"] == "数据滞后" for p in d["问题"])


def test_digest_clean_means_no_problems(env):
    env["set_status"]([_entry(0.5)])
    d = H.build_digest(days=7)
    assert d["问题"] == []
    assert d["结论"] == "正常"


def test_digest_survives_missing_status_file(env):
    """status.json 不存在（新机器/被清）不能抛，只能如实说没有记录。"""
    env["status"].unlink(missing_ok=True)
    d = H.build_digest(days=7)
    assert d["流水线"]["窗口内无记录"] is True
    assert "说明" in d["流水线"]


# ------------------------------------------------------------ 静默
def _patch_push(monkeypatch, sink):
    import data.push_notify as P
    monkeypatch.setattr(P, "effective_ready", lambda cfg: True)
    monkeypatch.setattr(P, "_mark_pushed", lambda *a, **k: None)

    def _send(title, content, cfg=None):
        sink.append((title, content))
        return {"ok": True, "detail": "test"}

    monkeypatch.setattr(P, "send", _send)


def test_push_digest_is_silent_when_clean(monkeypatch):
    import data.push_notify as P
    sent = []
    _patch_push(monkeypatch, sent)
    res = P.push_digest({"窗口天数": 7, "结论": "正常", "问题": [], "流水线": {},
                         "注数一致性": {"全部一致": True}, "数据滞后": []})
    assert res["skipped"] is True
    assert sent == [], "无问题时不该消耗推送配额"


def test_push_digest_sends_when_problem(monkeypatch):
    import data.push_notify as P
    sent = []
    _patch_push(monkeypatch, sent)
    P.push_digest({"窗口天数": 7, "结论": "有 1 项需要处理",
                   "问题": [{"彩种": "双色球", "项": "注数不一致", "详情": "滑轨 200 ≠ 实际出号 5"}],
                   "流水线": {"总次数": 3, "成功": 3, "失败": 0},
                   "注数一致性": {"全部一致": False, "不一致": ["双色球"]},
                   "数据滞后": []})
    assert len(sent) == 1
    assert "注数不一致" in sent[0][1]


# ------------------------------------------------------------ 时点
def test_run_digest_writes_file(env, monkeypatch):
    env["set_status"]([_entry(0.5)])
    import data.push_notify as P
    monkeypatch.setattr(P, "push_digest", lambda d, **k: {"ok": True, "skipped": True})
    d = H.run_digest(notify=True)
    assert d["本地文件"].endswith(".json")
    assert (env["root"] / "automation").exists()


def test_maybe_run_before_due_hour_does_nothing(env):
    env["set_status"]([])
    early = _at(9, 0)
    assert H.maybe_run(early) is None
    assert not H.STATE_PATH.exists()


def test_maybe_run_daily_once_per_day(env, monkeypatch):
    env["set_status"]([])
    monkeypatch.setattr(H, "run_digest", lambda **k: {"问题": []})
    late = _at(H.DIGEST_HOUR, H.DIGEST_MINUTE + 1)
    first = H.maybe_run(late)
    assert first and "daily" in first["已执行"]
    second = H.maybe_run(late)
    assert second is None, "同一天第二次必须跳过（tick 每 60s 一轮，不跳过会刷屏）"


def test_maybe_run_cold_start_has_own_slot(env, monkeypatch):
    """冷启补报与当日总结是两个槽：早上开机报了，晚上 23:40 还要再报一次。"""
    env["set_status"]([])
    monkeypatch.setattr(H, "run_digest", lambda **k: {"问题": []})
    morning = _at(8, 0)
    assert H.maybe_run(morning, cold_start=True)["已执行"] == ["boot"]
    late = morning.replace(hour=H.DIGEST_HOUR, minute=H.DIGEST_MINUTE + 1)
    assert H.maybe_run(late)["已执行"] == ["daily"]


def test_maybe_run_cold_start_once_per_day(env, monkeypatch):
    """看门狗反复重启不该反复补报。"""
    env["set_status"]([])
    monkeypatch.setattr(H, "run_digest", lambda **k: {"问题": []})
    morning = _at(8, 0)
    assert H.maybe_run(morning, cold_start=True) is not None
    assert H.maybe_run(morning, cold_start=True) is None


# ------------------------------------------------------------ 接口
def test_api_digest_returns_payload(env):
    env["set_status"]([_entry(0.5)])
    from flask import Flask
    from web.routes.system import bp
    app = Flask(__name__)
    app.register_blueprint(bp)
    r = app.test_client().get("/api/system/digest?days=7")
    assert r.status_code == 200
    body = r.get_json()
    assert body["ok"] is True
    assert body["窗口天数"] == 7
    assert "流水线" in body and "注数一致性" in body


def test_api_digest_clamps_days(env):
    env["set_status"]([])
    from flask import Flask
    from web.routes.system import bp
    app = Flask(__name__)
    app.register_blueprint(bp)
    body = app.test_client().get("/api/system/digest?days=999").get_json()
    assert body["窗口天数"] == 30, "上界必须收口，否则 status.json 之外还要扫 90 天日志"


def test_digest_marks_rotated_window_as_undercounted(env):
    """history 写满且最早一条仍在窗口内时要标「计数可能偏少」。

    诚实边界：旋转窗口里的计数只能是"至少这么多次"，不能当成全量。
    不标的话，长期高负荷运行时日报会一直显示一个偏小的成功次数且无人察觉。
    """
    from data.automation_status import MAX_HISTORY
    env["set_status"]([_entry(i * 0.1) for i in range(MAX_HISTORY)])
    d = H.build_digest(days=7)
    assert d["流水线"]["计数可能偏少"] is True
    assert "旋转淘汰" in d["流水线"]["说明"] or "更早记录" in d["流水线"]["说明"]


def test_digest_no_truncation_flag_when_window_short(env):
    env["set_status"]([_entry(0.1), _entry(0.2)])
    d = H.build_digest(days=7)
    assert "计数可能偏少" not in d["流水线"]


# ------------------------------------------------------------ is_due（调度器每 60s 调一次，必须毫秒级）
def test_is_due_false_before_hour(env):
    early = _at(9, 0)
    assert H.is_due(early) is False


def test_is_due_true_after_hour(env):
    late = _at(H.DIGEST_HOUR, H.DIGEST_MINUTE + 1)
    assert H.is_due(late) is True


def test_is_due_false_once_daily_slot_used(env, monkeypatch):
    """跑过之后必须立刻变 False，否则调度器会每 60s 起一个线程重跑一次。"""
    env["set_status"]([])
    monkeypatch.setattr(H, "run_digest", lambda **k: {"问题": []})
    late = _at(H.DIGEST_HOUR, H.DIGEST_MINUTE + 1)
    assert H.is_due(late) is True
    H.maybe_run(late)
    assert H.is_due(late) is False


def test_cached_digest_reuses_within_ttl(env, monkeypatch):
    """接口必须走缓存：每次看板加载读六个 CSV 要 ~3s，会把页面拖到肉眼可感。"""
    calls = []
    monkeypatch.setattr(H, "build_digest",
                        lambda d=7: (calls.append(d), {"问题": [], "窗口天数": d})[1])
    H._CACHE.clear()
    H.cached_digest(7)
    H.cached_digest(7)
    assert len(calls) == 1
    H.cached_digest(3)
    assert len(calls) == 2, "不同窗口天数应各自缓存"


def test_api_digest_does_not_pollute_cache(env):
    """接口塞的 ok 字段不能写回缓存对象。"""
    env["set_status"]([])
    from flask import Flask
    from web.routes.system import bp
    app = Flask(__name__)
    app.register_blueprint(bp)
    app.test_client().get("/api/system/digest?days=7")
    assert "ok" not in (H._CACHE.get("7") or (0, {}))[1]
