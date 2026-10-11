# -*- coding: utf-8 -*-
"""tests/test_mechanism_feedback.py —— P5 反馈闭环（pending 快照 → 反馈 → 看板 → 推送）

锁住的契约：
  1. 快照**只读缓存**：出号路径绝不能顺手跑一次完整测量（双色球约 17s）；
  2. 三个字段挂在 pending **顶层**，不进「预测号码」
     ⇒ 保住「号码组数 == len(预测号码)」契约（09-20 事故契约）；
  3. 缓存缺失 ⇒ 快照 None + 显式写「无测量缓存」，**不**粉饰成"无缝隙"；
  4. 反馈记录带过字段，且**不得改变去重键**（放在 `_fb_key` 之后）；
  5. 推送 footer 是条件式的；机制倾斜**只在真激活时**才出现（γ=0 不制造噪音）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def fb_dir(monkeypatch, tmp_path):
    """FEEDBACK_DIR 重定向到 tmp，测试绝不触碰真身 training/feedback。"""
    from data import feedback as fb
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path)
    return tmp_path


def _ticket(zone="第1位", n=1):
    return {"号码": {zone: [n]}, "策略": "高频策略", "置信度": 0.35, "类型": "single"}


_FAKE_SNAP = {
    "彩种": "排列3", "期数": 7000, "裁决": "no-track", "判定轨": [],
    "应有判定轨": ["A′"], "R": None, "ū": None, "γ": None, "γ显著": False,
    "模型Go": None, "测量时间": "2026-10-03T00:00:00", "指纹": "v3|x",
    "来源": "只读缓存",
}


_FAKE_CACHE = {
    "applicable": True, "n": 3503, "sig": "v3|双色球",
    "裁决": {"status": "none", "判定轨": ["A", "B"], "应有判定轨": ["A", "B"]},
    "track结果": {"A": {"主口径": {"R": 0.9939}},
                  "B": {"mean_u": 0.50135}},
    "gamma": {"gamma_合并": {"gamma": -0.0185, "显著": False}},
    "B过程": {"模型Go": True},
    "_cached_at": "2026-10-03T22:57:55",
}


@pytest.fixture
def fake_cache(monkeypatch):
    """注入一份假缓存。

    ⚠️ 必须注入：根 conftest 把 `gap_cache.CACHE_DIR` 隔离到 tmp（防测试写脏真身），
    所以**测试环境里永远读不到真实缓存**。不注入的话「快照没触发测量」会是假绿
    —— 它压根没读到缓存，自然也不会去测量。
    """
    monkeypatch.setattr("ev.mechanism.gap_cache.load",
                        lambda *a, **k: dict(_FAKE_CACHE))
    return _FAKE_CACHE


# ------------------------------------------------------------------ ① 快照只读缓存
def test_snapshot_never_triggers_a_measurement(monkeypatch, fake_cache):
    """★ 快照必须只读缓存——出号时不该付 17s 的测量代价。"""
    from ev.mechanism import snapshot as SN

    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise AssertionError("快照触发了测量！")

    for tgt in ("ev.gap.measure", "ev.gap.measure_gap", "ev.gap.measure_gap_global",
                "ev.mechanism.gap_verdict.measure_gap"):
        monkeypatch.setattr(tgt, boom)

    s = SN.mechanism_snapshot("双色球")
    assert s is not None, "有缓存却读出 None —— 上一轮的假绿就是这样来的"
    assert s["R"] == pytest.approx(0.9939)
    assert s["ū"] == pytest.approx(0.50135)
    assert calls == [], "快照触发了测量 —— 出号路径会被拖垮"


def test_snapshot_none_when_cache_missing(monkeypatch):
    """缓存缺失 ⇒ None（不是 0、不是空 dict）——不许把「没测」伪装成「测过」。"""
    from ev.mechanism import snapshot as SN
    monkeypatch.setattr("ev.mechanism.gap_cache.load", lambda *a, **k: None)
    assert SN.mechanism_snapshot("双色球") is None


def test_snapshot_is_pure_scalars(fake_cache):
    """快照必须全是标量/小列表——它要写进 pending JSON 与推送 footer。"""
    from ev.mechanism import snapshot as SN
    s = SN.mechanism_snapshot("双色球")
    assert s is not None
    json.dumps(s, ensure_ascii=False, allow_nan=False)   # 可序列化且无 NaN
    assert isinstance(s["判定轨"], list)
    assert "来源" in s and "测量时间" in s and "指纹" in s


def test_snapshot_all_separates_measured_from_missing(monkeypatch):
    """未测量的彩种必须单列，不混进「已测量」——否则会被读成"测过、没检出"。 """
    from ev.mechanism import snapshot as SN
    monkeypatch.setattr(SN, "mechanism_snapshot",
                        lambda lot: None if lot == "福彩3D" else dict(_FAKE_SNAP, 彩种=lot))
    g = SN.mechanism_snapshot_all()
    assert "福彩3D" in g["未测量彩种"]
    assert "福彩3D" not in g["已测量"]
    assert g["已测量数"] == len(SN.LOTTERIES_FOR_SNAPSHOT) - 1


# ------------------------------------------------------------------ ② pending 挂字段
def test_pending_carries_three_mechanism_fields(monkeypatch, fb_dir):
    from data import feedback as fb
    monkeypatch.setattr(fb, "_mechanism_snapshot", lambda lot: dict(_FAKE_SNAP))

    pred = {"预测日期": "2099-01-01", "目标期号": 99901, "来源": "predict",
            "预测号码": [_ticket(n=i) for i in range(4)]}
    fb.record_pending_prediction("排列3", pred)

    for k in fb.MECHANISM_SNAPSHOT_KEYS:
        assert k in pred, f"pending 缺字段 {k}"
    assert pred["缝隙快照"]["期数"] == 7000
    assert pred["机制倾斜"]["倾斜"] is False


def test_mechanism_fields_do_not_break_group_count_contract(monkeypatch, fb_dir):
    """★ 三个字段不得进「预测号码」⇒ 号码组数 == len(预测号码) 必须仍然成立。"""
    from data import feedback as fb

    def fake_compress(tickets, lottery, **kwargs):
        return [0, 1], {"压缩前注数": len(tickets), "压缩后注数": 2,
                        "质量淘汰": 0, "重复淘汰": 0, "重叠淘汰": 0,
                        "启用": True, "说明": "test"}

    monkeypatch.setattr("ev.coverage.compress_tickets", fake_compress)
    monkeypatch.setattr(fb, "_mechanism_snapshot", lambda lot: dict(_FAKE_SNAP))

    pred = {"预测日期": "2099-01-01", "目标期号": 99902, "来源": "predict",
            "号码组数": 10,
            "预测号码": [_ticket(n=i) for i in range(10)]}
    fb.record_pending_prediction("排列3", pred)

    saved = json.loads((fb_dir / "排列3_pending.json").read_text(encoding="utf-8"))
    r = (saved if isinstance(saved, list) else saved.get("records", []))[-1]
    assert len(r["预测号码"]) == 2
    assert r["号码组数"] == 2, "机制字段改动破坏了号码组数契约"
    assert r["缝隙快照"]["期数"] == 7000


def test_attach_is_idempotent(monkeypatch, fb_dir):
    """调用方已显式传过快照 ⇒ 不覆盖（避免回放/测试场景被覆盖）。"""
    from data import feedback as fb
    pred = {"预测日期": "2099-01-01", "目标期号": 99903, "来源": "predict",
            "缝隙快照": {"期数": 1}, "预测号码": [_ticket(n=1)]}
    fb.record_pending_prediction("排列3", pred)
    assert pred["缝隙快照"] == {"期数": 1}


def test_no_cache_is_reported_honestly(monkeypatch, fb_dir):
    """★ 无缓存 ⇒ 机制倾斜的原因必须写「无测量缓存」，不许说成"γ=0 已测过"。"""
    from data import feedback as fb
    monkeypatch.setattr(fb, "_mechanism_snapshot", lambda lot: None)
    pred = {"预测日期": "2099-01-01", "目标期号": 99904, "来源": "predict",
            "预测号码": [_ticket(n=1)]}
    fb.record_pending_prediction("排列3", pred)
    assert pred["缝隙快照"] is None
    assert "无测量缓存" in pred["机制倾斜"]["原因"]
    assert "已证明" not in pred["机制倾斜"]["说明"]


# ------------------------------------------------------------------ ③ 反馈记录带过
def test_carry_mechanism_fields_copies_all_three():
    from data.feedback import MECHANISM_SNAPSHOT_KEYS, _carry_mechanism_fields
    pred = {"缝隙快照": {"a": 1}, "机制γ": 0.0, "机制倾斜": {"倾斜": False}}
    fb = {}
    _carry_mechanism_fields(pred, fb)
    for k in MECHANISM_SNAPSHOT_KEYS:
        assert k in fb


def test_carry_skips_none_values():
    """字段为 None 不写进反馈记录——避免把一堆 null 塞进历史台账。"""
    from data.feedback import _carry_mechanism_fields
    fb = {}
    _carry_mechanism_fields({"缝隙快照": None, "机制γ": 0.0}, fb)
    assert "缝隙快照" not in fb
    assert fb["机制γ"] == 0.0


def test_carry_happens_after_dedup_key():
    """★ 带过必须在 `_fb_key` **之后**：新增字段不得改变去重键。"""
    import inspect
    from data import feedback as fb
    src = inspect.getsource(fb.evaluate_pending_predictions)
    i_key = src.find("_k = _fb_key(feedback)")
    i_carry = src.find("_carry_mechanism_fields(pred, feedback)")
    assert i_key > 0 and i_carry > 0, "找不到关键行"
    assert i_carry > i_key, "带过跑到了去重键之前 ⇒ 同批会重复入账"


# ------------------------------------------------------------------ ④ API
def test_gap_api_returns_snapshot_or_honest_missing(monkeypatch):
    from web.app import app
    from ev.mechanism import snapshot as SN

    monkeypatch.setattr(SN, "mechanism_snapshot", lambda lot: None)
    c = app.test_client()
    r = c.get("/api/audit/gap/双色球")
    assert r.status_code == 200
    d = r.get_json()
    assert d["未测量"] is True
    assert "尚未测量" in d["提示"] or "尚无测量缓存" in d["提示"]
    assert "≠" in d["诚实说明"], "未测量时必须写明「不等于无缝隙」"


def test_gap_api_returns_snapshot_when_cached(monkeypatch):
    from web.app import app
    from ev.mechanism import snapshot as SN

    monkeypatch.setattr(SN, "mechanism_snapshot",
                        lambda lot: dict(_FAKE_SNAP, 彩种=lot, 裁决="none"))
    c = app.test_client()
    d = c.get("/api/audit/gap/双色球").get_json()
    assert d["未测量"] is False
    assert d["快照"]["裁决"] == "none"
    assert d["机制倾斜"]["倾斜"] is False


def test_gap_global_api_lists_missing(monkeypatch):
    from web.app import app
    from ev.mechanism import snapshot as SN

    monkeypatch.setattr(SN, "mechanism_snapshot",
                        lambda lot: None if lot == "福彩3D" else dict(_FAKE_SNAP, 彩种=lot))
    d = app.test_client().get("/api/audit/gap_global").get_json()
    assert "福彩3D" in d["未测量彩种"]
    assert d["已测量数"] >= 1


def test_gap_api_rejects_unknown_lottery():
    from web.app import app
    r = app.test_client().get("/api/audit/gap/不存在的彩种")
    assert r.status_code == 400


# ------------------------------------------------------------------ ⑤ 推送 footer
def test_footer_is_conditional_now():
    """★ footer 的「概率恒定」必须带上前提，不许再写成无条件断言。"""
    import inspect
    from data import push_notify as pn
    src = inspect.getsource(pn)
    # 源码里是跨行拼接的字符串，按片段断言（整句断言会因换行而假失败）
    assert "在「开奖对全部组合等可能」的前提下单注中奖概率恒定、" in src
    assert "请勿用于投注决策" in src
    assert "未检出偏离 ≠ 证明开奖公平" in src
    assert "该前提由四轨测量独立检验" in src


def test_footer_has_no_tilt_line_when_gamma_zero(monkeypatch):
    """γ=0 ⇒ 不额外加行（推送是高频通知，天天挂「未激活」是噪音）。"""
    from data import push_notify as pn
    monkeypatch.setattr("ev.mechanism.snapshot.mechanism_snapshot",
                        lambda lot: dict(_FAKE_SNAP, γ=0.0, γ显著=False))
    assert pn._mechanism_footer_line("双色球") == ""


def test_footer_announces_tilt_when_active(monkeypatch):
    """★ 倾斜真激活时必须显式告知——出号权重被动了，不能悄悄改。"""
    from data import push_notify as pn
    monkeypatch.setattr("ev.mechanism.snapshot.mechanism_snapshot",
                        lambda lot: dict(_FAKE_SNAP, γ=0.8, γ显著=True))
    line = pn._mechanism_footer_line("双色球")
    assert "已激活" in line
    assert "0.8" in line
    assert "偏向冷门" in line


def test_footer_silent_when_no_cache(monkeypatch):
    """无缓存 ⇒ 不激活也不报错（推送不能因元信息失败而阻塞）。"""
    from data import push_notify as pn
    monkeypatch.setattr("ev.mechanism.snapshot.mechanism_snapshot", lambda lot: None)
    assert pn._mechanism_footer_line("双色球") == ""


def test_footer_never_raises(monkeypatch):
    """任何异常都吞掉返回空串——推送是通知渠道，不该因为读快照失败而挂掉。"""
    from data import push_notify as pn

    def boom(lot):
        raise RuntimeError("boom")

    monkeypatch.setattr("ev.mechanism.snapshot.mechanism_snapshot", boom)
    assert pn._mechanism_footer_line("双色球") == ""
