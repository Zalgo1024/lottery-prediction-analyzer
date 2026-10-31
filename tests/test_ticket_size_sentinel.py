# -*- coding: utf-8 -*-
"""出号配置健康度哨兵（2026-10-31 事故的牙齿）。

事故：`config/ticket_size.json` 被删除后，`effective_count()` 静默回落到
`default_count()`（数字型 5 / 乐透型 100），`sanitize()` 把 `mode` 兜底成
`fixed`、`锁定` 兜底成 `False` —— **「自动路径不加注」的锁随之失效**。
外部观察到的只是「注数变了」，与「有人改了配置」完全无法区分。

所以这组测试锁的不是「能不能读到配置」，而是「读到的是不是我们认可的那一份」：

  * 文件缺失 / mode=fixed / 锁定=false / limits 缺彩种 ⇒ 一律判**不安全**；
  * 不安全时 `predict(record_pending=True)` **停发**且不写 pending（不是回落默认值）；
  * `record_pending=False` 的验证出号不受影响（否则修一个故障会连带打断诊断链路）。
"""

from __future__ import annotations

import json

import pytest

from data import ticket_size as ts

LOTS = ("双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩")


def _payload(**over) -> dict:
    """一份「健康」的配置基线；用 over 覆盖单个字段来构造病态样本。"""
    doc = {
        "mode": "dynamic",
        "fixed": {lot: 5 for lot in LOTS},
        "dynamic": {lot: 50 for lot in LOTS},
        "current": {lot: 50 for lot in LOTS},
        "limits": {"双色球": 500, "大乐透": 400, "排列5": 100,
                   "福彩3D": 200, "排列3": 200, "七星彩": 300},
        "锁定": True,
    }
    doc.update(over)
    return doc


def _write(path, payload) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


# ------------------------------------------------------------------ 四类病态
def test_healthy_config_passes(tmp_path):
    p = tmp_path / "ticket_size.json"
    _write(p, _payload())
    hc = ts.health(path=p)
    assert hc["安全"] is True, hc["原因"]
    assert hc["存在"] is True and hc["模式"] == "dynamic" and hc["锁定"] is True


def test_missing_file_is_unsafe_and_reports_fallback_mode(tmp_path):
    """★ 事故原样重演：文件一没，模式和锁定都变成兜底值，必须判不安全。"""
    hc = ts.health(path=tmp_path / "nope.json")
    assert hc["安全"] is False
    assert hc["存在"] is False
    # 这两条正是「静默失真」的核心：报出来的 mode/锁定 已经不是用户设定
    assert hc["模式"] == ts.MODE_FIXED
    assert hc["锁定"] is False
    assert any("缺失" in r for r in hc["原因"])


def test_unlocked_is_unsafe(tmp_path):
    """`锁定=false` ⇒ `apply_dynamic_result` 又能改注数 ⇒ 必须判不安全。"""
    p = tmp_path / "ticket_size.json"
    _write(p, _payload(锁定=False))
    hc = ts.health(path=p)
    assert hc["安全"] is False
    assert any("锁定" in r for r in hc["原因"])


def test_fixed_mode_is_unsafe(tmp_path):
    p = tmp_path / "ticket_size.json"
    _write(p, _payload(mode="fixed"))
    hc = ts.health(path=p)
    assert hc["安全"] is False
    assert any("模式" in r for r in hc["原因"])


def test_limits_not_covering_a_lottery_is_unsafe(tmp_path):
    p = tmp_path / "ticket_size.json"
    limits = _payload()["limits"]
    limits.pop("七星彩")
    _write(p, _payload(limits=limits))
    hc = ts.health(path=p)
    assert hc["安全"] is False
    assert any("七星彩" in r for r in hc["原因"])


def test_corrupt_file_is_unsafe_not_crash(tmp_path):
    """坏 JSON 不能让哨兵自己抛异常 —— 否则调用方会当成「检查通过」。"""
    p = tmp_path / "ticket_size.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    hc = ts.health(path=p)
    assert hc["安全"] is False
    assert hc["存在"] is True
    assert hc["原因"]


def test_stop_reason_is_a_single_readable_sentence(tmp_path):
    hc = ts.health(path=tmp_path / "nope.json")
    reason = ts.stop_reason(hc)
    assert reason.startswith("出号配置不安全：")
    assert "缺失" in reason


# ------------------------------------------------------------------ 接线
def test_predict_stops_when_config_unsafe(tmp_path, monkeypatch):
    """★ 不安全 ⇒ 停发 + 不写 pending。**不是**回落默认值继续出号。"""
    from data.feedback import load_pending
    from prediction import engine

    # 只断言「本次没有新增 pending」，不断言目录为空 —— 测试环境的 pending
    # 目录可能已有其它用例/真实运行的记录，那种断言会把「没写入」误判成失败。
    before = len(load_pending("排列5"))

    monkeypatch.setattr(ts, "STORE_PATH", tmp_path / "gone.json")
    out = engine.predict("排列5", groups=5, record_pending=True)

    assert out["号码组数"] == 0
    assert out["预测号码"] == []
    assert out["停发"]["来源"] == "data.ticket_size.health"
    assert out["配置健康度"]["安全"] is False
    assert len(load_pending("排列5")) == before, "停发时不得写入 pending"


def test_predict_without_record_pending_is_not_blocked(tmp_path, monkeypatch):
    """★★ 反向守护：`record_pending=False` 是诊断出号，不该被配置哨兵拦住。

    判别「配置坏了」和「诊断链路还能不能跑」是两件事：如果条件写成交给哨兵
    一刀切，那么一旦配置出问题，连「查一查它到底怎么了」的试算都会失败。
    """
    from prediction import engine

    monkeypatch.setattr(ts, "STORE_PATH", tmp_path / "gone.json")
    out = engine.predict("排列5", groups=3, record_pending=False)

    assert out["号码组数"] == 3
    assert "停发" not in out
    assert len(out["预测号码"]) == 3


def test_startup_recovery_skips_when_config_unsafe(tmp_path, monkeypatch):
    """★ 配置不可信时「自愈」会按错误注数重生成 pending —— 必须整轮跳过。"""
    from web import startup_recovery as SR

    monkeypatch.setattr(ts, "STORE_PATH", tmp_path / "gone.json")
    fixed = SR.ensure_pending_groups()

    assert fixed, "配置不安全时应返回每彩种的停发说明"
    for lot, info in fixed.items():
        assert info["滑轨"] == 0
        assert "配置不安全" in info["动作"]
