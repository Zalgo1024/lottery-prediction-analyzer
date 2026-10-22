"""
market.db 自动更新编排的锁与编排约束

★ 为什么值得单独测：这条链路会**真写数据库**，而且是被定时器在深夜自动拉起的
  —— 出错的时候没人在看屏幕。所以至少要锁死四件事：
    1. 单源失败不能让其它源的成果白跑（网络抓取随时会挂）
    2. 两路并发（23:10 定时 + 页面按钮）不能同时写同一个库
    3. 上次进程被杀留下的陈旧锁必须能被接管，否则会把自己锁死 40 分钟
    4. 调度判定要"错过就补"，不能因为 23:10 那一刻没开机就顺延到明天

全部用临时库 + monkeypatch，绝不碰真实的 market/market.db。
"""

import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta

import pytest

import market.build as B
import market.update as U


# ---------------------------------------------------------------- 环境

def _make_db(path):
    con = sqlite3.connect(str(path))
    con.executescript("""
        CREATE TABLE IF NOT EXISTS sync_log(
            id INTEGER PRIMARY KEY AUTOINCREMENT, task TEXT, started_at TEXT,
            finished_at TEXT, rows INTEGER, status TEXT, note TEXT);
        CREATE TABLE IF NOT EXISTS draw(
            lottery TEXT, issue TEXT, source TEXT, issue_year INT, issue_seq INT,
            draw_date TEXT, front TEXT, back TEXT, seq_front TEXT, seq_back TEXT,
            sales REAL, pool REAL, first_winners INT, first_region_raw TEXT,
            fetched_at TEXT);
    """)
    con.commit()
    return con


@pytest.fixture
def fake_env(tmp_path, monkeypatch):
    """把 update/build 的落盘路径和连接全部指向临时目录。"""
    db = tmp_path / "market.db"
    _make_db(db).close()
    lock = tmp_path / ".update.lock"

    monkeypatch.setattr(U, "DB_PATH", str(db))
    monkeypatch.setattr(U, "LOCK_PATH", str(lock))
    monkeypatch.setattr(U, "_RUNNING", False)
    # build.connect 默认打开真库，这里整体换成临时库连接
    monkeypatch.setattr(B, "connect", lambda path=None: _make_db(db))
    monkeypatch.setattr(B, "init_db", lambda path=None: _make_db(db))
    return {"db": db, "lock": lock, "tmp": tmp_path}


def _stub(monkeypatch, name, recorder, ok=True, rows=10, kw_sink=None):
    if name == "mof":
        # ★mof 的返回是 {months,rows,failed,...}，跟「彩种→统计」完全不同，
        #   run_update 里走的是专属分支，所以桩也要单独一份。
        def fn_mof(con, verbose=True, **kw):
            recorder.append(name)
            if kw_sink is not None:
                kw_sink[name] = kw
            if not ok:
                raise RuntimeError("%s 挂了" % name)
            return {"months": 1, "rows": 62, "failed": {}, "no_data": [],
                    "incomplete": {}, "skipped": []}
        monkeypatch.setattr(B, "build_mof", fn_mof)
        return

    def fn(con, codes=None, verbose=True, **kw):
        recorder.append(name)
        if kw_sink is not None:
            kw_sink[name] = kw
        if not ok:
            raise RuntimeError("%s 挂了" % name)
        return {"ssq": {"draw": rows, "prize": rows * 2, "region": 0}}
    monkeypatch.setattr(B, {"csv": "build_csv", "cwl": "build_cwl",
                            "sporttery": "build_sporttery", "zhcw": "build_zhcw"}[name], fn)


def _no_viewer(monkeypatch):
    monkeypatch.setattr(U, "notify_viewer", lambda timeout=180.0: None)


# ---------------------------------------------------------------- 编排

def test_runs_all_sources_in_order(fake_env, monkeypatch):
    calls = []
    for s in U.DEFAULT_SOURCES:
        _stub(monkeypatch, s, calls)
    _no_viewer(monkeypatch)

    r = U.run_update(log=lambda m: None)

    assert calls == ["csv", "cwl", "sporttery", "zhcw", "mof"], "顺序不对：%s" % calls
    assert r["ok"] is True and r["status"] == "ok"
    assert r["rows"] == 40 + 62   # 4 源 × 10 期 + mof 62 行
    assert r["failed"] == []
    assert r["db"]["draw"] == 0  # 临时库里没有 draw 行（stub 没写）


def test_one_source_failure_does_not_stop_others(fake_env, monkeypatch):
    calls = []
    _stub(monkeypatch, "csv", calls)
    _stub(monkeypatch, "cwl", calls, ok=False)      # 模拟网络抓取失败
    _stub(monkeypatch, "sporttery", calls)
    _stub(monkeypatch, "zhcw", calls)
    _stub(monkeypatch, "mof", calls)                # ★不 stub 就会真去抓财政部
    _no_viewer(monkeypatch)

    r = U.run_update(log=lambda m: None)

    assert calls == ["csv", "cwl", "sporttery", "zhcw", "mof"], "失败源后面的源被跳过了"
    assert r["ok"] is False and r["status"] == "partial"
    assert r["failed"] == ["cwl"]
    assert "挂了" in (r["sources"]["cwl"]["error"] or "")


def test_all_sources_failed_marks_fail(fake_env, monkeypatch):
    calls = []
    for s in U.DEFAULT_SOURCES:
        _stub(monkeypatch, s, calls, ok=False)
    _no_viewer(monkeypatch)

    r = U.run_update(log=lambda m: None)
    assert r["status"] == "fail"
    assert len(r["failed"]) == 5


def test_empty_result_counts_as_failure(fake_env, monkeypatch):
    """不抛异常但一个彩种都没拿到（源改版常见）不能算成功。"""
    calls = []
    monkeypatch.setattr(B, "build_csv", lambda con, codes=None, verbose=True: {})
    for s in ("cwl", "sporttery", "zhcw"):
        _stub(monkeypatch, s, calls)
    _no_viewer(monkeypatch)

    r = U.run_update(sources=["csv"], log=lambda m: None)
    assert r["ok"] is False and r["failed"] == ["csv"]


def test_unknown_source_rejected(fake_env, monkeypatch):
    _no_viewer(monkeypatch)
    r = U.run_update(sources=["csv", "weibo"], log=lambda m: None)
    assert r["ok"] is False and "未知数据源" in r["error"]


def test_only_csv_is_fast_path(fake_env, monkeypatch):
    calls = []
    _stub(monkeypatch, "csv", calls)
    _no_viewer(monkeypatch)
    r = U.run_update(sources=["csv"], log=lambda m: None)
    assert calls == ["csv"] and r["ok"] is True


def test_writes_summary_to_sync_log(fake_env, monkeypatch):
    calls = []
    _stub(monkeypatch, "csv", calls)
    _no_viewer(monkeypatch)
    U.run_update(sources=["csv"], log=lambda m: None)

    con = sqlite3.connect(str(fake_env["db"]))
    try:
        row = con.execute("SELECT task, status, rows FROM sync_log WHERE task='update:all'").fetchone()
    finally:
        con.close()
    assert row is not None, "没有写 update:all 汇总记录"
    assert row[1] == "ok" and row[2] == 10


def test_lock_released_after_success(fake_env, monkeypatch):
    _stub(monkeypatch, "csv", [])
    _no_viewer(monkeypatch)
    U.run_update(sources=["csv"], log=lambda m: None)
    assert not os.path.exists(fake_env["lock"]), "锁没释放，下次更新会被挡住"


def test_lock_released_after_exception(fake_env, monkeypatch):
    _no_viewer(monkeypatch)
    monkeypatch.setattr(B, "connect", lambda path=None: (_ for _ in ()).throw(OSError("磁盘满")))
    r = U.run_update(sources=["csv"], log=lambda m: None)
    assert r["ok"] is False and "磁盘满" in r.get("error", "")
    assert not os.path.exists(fake_env["lock"]), "异常路径也必须释放锁"


def test_incremental_mode_limits_pages(fake_env, monkeypatch):
    """日常增量只抓最近一页 —— 全量翻页在晚间能跑到 20 分钟以上。"""
    calls, kw = [], {}
    for src in U.DEFAULT_SOURCES:
        _stub(monkeypatch, src, calls, kw_sink=kw)
    _no_viewer(monkeypatch)

    r = U.run_update(log=lambda m: None)
    assert r["mode"] == "incremental"
    assert kw["cwl"] == {"max_pages": 1}, kw["cwl"]
    assert kw["sporttery"] == {"max_pages": 1}, kw["sporttery"]
    assert kw["zhcw"] == {"count": 200}, kw["zhcw"]
    assert kw["csv"] == {}, "CSV 本来就是全量，不该传翻页参数"
    assert kw["mof"] == {"max_pages": 1}, "财政部日常只需看最新一页公告"


def test_full_mode_uses_default_depth(fake_env, monkeypatch):
    calls, kw = [], {}
    for src in U.DEFAULT_SOURCES:
        _stub(monkeypatch, src, calls, kw_sink=kw)
    _no_viewer(monkeypatch)

    r = U.run_update(mode="full", log=lambda m: None)
    assert r["mode"] == "full"
    assert all(v == {} for v in kw.values()), "全量模式不该覆盖默认翻页深度：%s" % kw


def test_mode_recorded_in_sync_log(fake_env, monkeypatch):
    calls = []
    _stub(monkeypatch, "csv", calls)
    _no_viewer(monkeypatch)
    U.run_update(sources=["csv"], mode="full", log=lambda m: None)

    import json as _json
    con = sqlite3.connect(str(fake_env["db"]))
    try:
        note = con.execute("SELECT note FROM sync_log WHERE task='update:all'").fetchone()[0]
    finally:
        con.close()
    assert _json.loads(note)["mode"] == "full"


# ---------------------------------------------------------------- 并发

def test_fresh_lock_blocks_second_run(fake_env, monkeypatch):
    """23:10 定时任务在跑时，页面按钮再点一次应该被挡回去。"""
    fake_env["lock"].write_text("99999", encoding="utf-8")  # 新鲜锁
    calls = []
    _stub(monkeypatch, "csv", calls)
    _no_viewer(monkeypatch)

    r = U.run_update(sources=["csv"], log=lambda m: None)
    assert r["ok"] is False and r["busy"] is True
    assert calls == [], "拿不到锁还是跑了"


def test_stale_lock_can_be_taken_over(fake_env, monkeypatch):
    """上次进程被杀留下的旧锁必须能接管，否则会把自己锁死 40 分钟。"""
    fake_env["lock"].write_text("99999", encoding="utf-8")
    old = time.time() - (U.LOCK_STALE_SEC + 60)
    os.utime(str(fake_env["lock"]), (old, old))

    calls = []
    _stub(monkeypatch, "csv", calls)
    _no_viewer(monkeypatch)

    r = U.run_update(sources=["csv"], log=lambda m: None)
    assert r["ok"] is True, "陈旧锁没被接管：%s" % r.get("error")
    assert calls == ["csv"]


def test_is_running_reflects_lock(fake_env):
    assert U.is_running() is False
    fake_env["lock"].write_text("1", encoding="utf-8")
    assert U.is_running() is True


# ---------------------------------------------------------------- 读侧

def test_last_update_without_records(fake_env):
    info = U.last_update()
    assert info["available"] is False
    assert "从未" in (info.get("reason") or "")


def test_last_update_parses_summary(fake_env, monkeypatch):
    calls = []
    _stub(monkeypatch, "cwl", calls, ok=False)
    monkeypatch.setattr(B, "build_csv", lambda con, codes=None, verbose=True: {"ssq": {"draw": 7}})
    _no_viewer(monkeypatch)
    U.run_update(sources=["csv", "cwl"], log=lambda m: None)

    info = U.last_update()
    assert info["available"] is True
    assert info["status"] == "partial"
    assert info["failed"] == ["cwl"]
    assert info["rows"] == 7
    assert isinstance(info["age_hours"], float)


def test_deadline_skips_remaining_sources(fake_env, monkeypatch):
    """官方接口在晚间高峰能慢到早上的 10 倍；总时限到了就不该再启动新源。"""
    calls = []
    for s in U.DEFAULT_SOURCES:
        _stub(monkeypatch, s, calls)
    _no_viewer(monkeypatch)
    monkeypatch.setattr(U, "DEADLINE_SEC", 1e-9)   # 一进循环就判超时

    r = U.run_update(log=lambda m: None)

    assert calls == [], "超时了还去抓源：%s" % calls
    assert r["skipped"] == ["csv", "cwl", "sporttery", "zhcw", "mof"]
    assert r["failed"] == [], "跳过不该算失败"
    assert r["status"] == "partial", "有源没跑不能报 ok"


def test_deadline_never_marks_failure(fake_env, monkeypatch):
    """跳过（超时没轮到）不能记成失败 —— 否则日报会天天报「补库失败」。"""
    calls = []
    _stub(monkeypatch, "csv", calls)
    _no_viewer(monkeypatch)
    monkeypatch.setattr(U, "DEADLINE_SEC", 0.001)

    r = U.run_update(sources=["csv"], log=lambda m: None)
    assert r["failed"] == [], "跳过被错记成失败"
    assert r["status"] in ("ok", "partial")


# ---------------------------------------------------------------- 调度判定

def _bare_scheduler():
    """不调 __init__，避免读写真实的 config/auto_scheduler_state.json。"""
    from web.scheduler import AutoScheduler
    s = AutoScheduler.__new__(AutoScheduler)
    s._lock = threading.RLock()
    s.state = {}
    s.enabled = True
    s._market_thread = None      # market_status() 要读它
    s._digest_running = False
    return s


def test_market_due_before_time():
    s = _bare_scheduler()
    now = datetime(2026, 10, 21, 22, 0)
    assert s._market_due(now=now) is False, "23:10 之前不该跑"


def test_market_due_after_time_never_ran():
    s = _bare_scheduler()
    assert s._market_due(now=datetime(2026, 10, 21, 23, 20)) is True


def test_market_due_skips_when_already_ran_today():
    from web.scheduler import MARKET_STATE_KEY
    s = _bare_scheduler()
    s.state[MARKET_STATE_KEY] = {"last_run": "2026-10-21T23:11:02"}
    assert s._market_due(now=datetime(2026, 10, 21, 23, 40)) is False


def test_market_due_runs_again_next_day():
    from web.scheduler import MARKET_STATE_KEY
    s = _bare_scheduler()
    s.state[MARKET_STATE_KEY] = {"last_run": "2026-10-21T23:11:02"}
    assert s._market_due(now=datetime(2026, 10, 22, 23, 20)) is True


def test_market_due_catches_up_after_boot_late():
    """23:10 那会儿没开机：当晚稍后（23:30）开机要补，不该顺延到明天。"""
    from web.scheduler import MARKET_STATE_KEY
    s = _bare_scheduler()
    s.state[MARKET_STATE_KEY] = {"last_run": "2026-10-20T23:11:00"}
    assert s._market_due(now=datetime(2026, 10, 21, 23, 30)) is True
    # 而白天 9:00 不该补 —— 今天的 23:10 还没到，昨晚那一档确实跑过了
    assert s._market_due(now=datetime(2026, 10, 21, 9, 0)) is False


def test_market_status_shape():
    s = _bare_scheduler()
    st = s.market_status()
    for key in ("running", "last_run", "schedule", "due_today", "last_failed"):
        assert key in st
    assert st["schedule"] == "23:10"


# ---------------------------------------------------------------- 日报

def test_digest_includes_market_field(monkeypatch):
    from web import health_digest as H
    monkeypatch.setattr("market.update.is_running", lambda: False)
    monkeypatch.setattr("market.update.last_update", lambda path=None: {
        "available": True, "finished_at": "2026-10-21 23:12:00", "age_hours": 1.0,
        "status": "ok", "failed": [], "rows": 33150,
        "latest_draw_date": "2026-10-21", "latest_fetch": "2026-10-21 23:12:00",
    })
    d = H.build_digest(7)
    assert "开奖数据库" in d
    assert d["开奖数据库"]["状态"] == "ok"
    assert not [p for p in d["问题"] if p["彩种"] == "开奖数据库"]


def test_digest_reports_partial_failure(monkeypatch):
    from web import health_digest as H
    monkeypatch.setattr("market.update.is_running", lambda: False)
    monkeypatch.setattr("market.update.last_update", lambda path=None: {
        "available": True, "finished_at": "2026-10-21 23:12:00", "age_hours": 1.0,
        "status": "partial", "failed": ["cwl"], "rows": 33150, "latest_draw_date": "2026-10-21",
    })
    d = H.build_digest(7)
    hits = [p for p in d["问题"] if p["彩种"] == "开奖数据库" and p["项"] == "补库失败"]
    assert hits, "补库失败没进日报"
    assert "cwl" in hits[0]["详情"]


def test_digest_reports_stale_market(monkeypatch):
    from web import health_digest as H
    monkeypatch.setattr("market.update.is_running", lambda: False)
    monkeypatch.setattr("market.update.last_update", lambda path=None: {
        "available": True, "finished_at": "2026-10-19 23:12:00", "age_hours": 48.0,
        "status": "ok", "failed": [], "rows": 1, "latest_draw_date": "2026-10-19",
    })
    d = H.build_digest(7)
    hits = [p for p in d["问题"] if p["彩种"] == "开奖数据库" and p["项"] == "补库停滞"]
    assert hits, "48 小时没更新却没报停滞"


def test_digest_ignores_never_used_market(monkeypatch):
    """旁路库可删可重建：从未启用不该算故障。"""
    from web import health_digest as H
    monkeypatch.setattr("market.update.is_running", lambda: False)
    monkeypatch.setattr("market.update.last_update",
                        lambda path=None: {"available": False, "reason": "market.db 不存在"})
    d = H.build_digest(7)
    assert d["开奖数据库"]["可用"] is False
    assert not [p for p in d["问题"] if p["彩种"] == "开奖数据库"]


# ---------------------------------------------------------------- 接口

@pytest.fixture(scope="module")
def client():
    from web.app import app
    return app.test_client()


def test_update_api_returns_409_when_running(client, monkeypatch):
    from web.scheduler import scheduler
    monkeypatch.setattr(scheduler, "trigger_market_update", lambda reason="manual", mode=None: False)
    r = client.post("/api/market/update")
    assert r.status_code == 409
    assert r.get_json()["already_running"] is True


def test_update_api_starts(client, monkeypatch):
    from web.scheduler import scheduler
    monkeypatch.setattr(scheduler, "trigger_market_update", lambda reason="manual", mode=None: True)
    r = client.post("/api/market/update")
    assert r.status_code == 200
    assert r.get_json()["ok"] is True


def test_status_api_exposes_schedule(client):
    d = client.get("/api/market/status").get_json()
    assert "update" in d, "状态接口缺 update 段"
    assert d["update"]["schedule"] == "23:10"
