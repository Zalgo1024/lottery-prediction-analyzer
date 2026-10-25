# -*- coding: utf-8 -*-
"""审计脚本自身的「牙齿」测试。

★ 一个永远输出「通过」的哨兵比没有哨兵更危险 —— 它会把真实故障洗白。
   所以这里不测「正常数据是否通过」，而是**故意造故障**，验证：
     · NUL 空洞 / 非法 JSON → 必须报「损坏」
     · 开奖后出号 → J1 必须计数
     · 结算早于出号 → J2 必须计数
     · 同批次两个出号时刻 → J3 必须计数
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load_audit():
    spec = importlib.util.spec_from_file_location(
        "_audit_mod", ROOT / "scripts" / "audit_data_integrity.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def audit(tmp_path, monkeypatch):
    mod = _load_audit()
    monkeypatch.setattr(mod, "FEEDBACK_DIR", tmp_path, raising=False)
    return mod


def _hist(tmp_path, lot, rows):
    (tmp_path / f"{lot}_feedback_history.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _row(**kw):
    base = {"期号": 26121, "开奖日期": "2026-10-20", "预测日期": "2026-10-20",
            "批次号": "B1", "批次时间": "2026-10-19T22:05:00",
            "评估时间": "2026-10-20T22:05:00", "批次规模": 1,
            "valid_prediction": True, "记录类型": "预测", "中奖等级": "未中"}
    base.update(kw)
    return base


# ---------------------------------------------------------------- A
def test_clean_file_passes(audit, tmp_path):
    _hist(tmp_path, "双色球", [_row()])
    r = audit.check_files()
    assert r["通过"]
    assert r["损坏文件"] == []


def test_nul_hole_is_detected(audit, tmp_path):
    """★ 复刻 2026-10-24 排列3 的真实损坏形态。"""
    p = tmp_path / "排列3_feedback_history.json"
    p.write_bytes(b"[]" + b"\x00" * 4096 +
                  json.dumps([_row()], ensure_ascii=False).encode("utf-8"))
    r = audit.check_files()
    assert not r["通过"]
    assert "排列3_feedback_history.json" in r["损坏文件"]
    assert any("NUL" in x["状态"] for x in r["明细"])


def test_truncated_json_is_detected(audit, tmp_path):
    (tmp_path / "双色球_feedback_history.json").write_text(
        '[{"期号": 26121, "开奖', encoding="utf-8")
    r = audit.check_files()
    assert not r["通过"]


# ---------------------------------------------------------------- B
def test_clean_lookahead_passes(audit, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOTTERY_CONFIG", ["双色球"], raising=False)
    _hist(tmp_path, "双色球", [_row()])
    r = audit.check_lookahead(3650)
    assert r["通过"], r["明细"]


def test_prediction_after_draw_is_caught(audit, tmp_path, monkeypatch):
    """★ J1：出号时刻晚于开奖日 21:00 ⇒ 必须计数。"""
    monkeypatch.setattr(audit, "LOTTERY_CONFIG", ["双色球"], raising=False)
    _hist(tmp_path, "双色球", [_row(批次时间="2026-10-20T23:30:00")])
    r = audit.check_lookahead(3650)
    assert r["明细"]["双色球"]["J1_出号晚于开奖"] == 1
    assert not r["通过"]


def test_evaluate_before_generate_is_caught(audit, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOTTERY_CONFIG", ["双色球"], raising=False)
    _hist(tmp_path, "双色球", [_row(批次时间="2026-10-20T20:00:00",
                                   评估时间="2026-10-19T20:00:00")])
    r = audit.check_lookahead(3650)
    assert r["明细"]["双色球"]["J2_结算早于出号"] == 1


def test_same_batch_two_times_is_caught(audit, tmp_path, monkeypatch):
    """J3：同一批次出现两个出号时刻 ⇒ 说明是拼接或重算。"""
    monkeypatch.setattr(audit, "LOTTERY_CONFIG", ["双色球"], raising=False)
    _hist(tmp_path, "双色球", [
        _row(期号=26121, 批次时间="2026-10-19T22:05:00"),
        _row(期号=26121, 批次时间="2026-10-19T23:10:00"),
    ])
    r = audit.check_lookahead(3650)
    assert r["明细"]["双色球"]["J3_批内出号时刻不一致"] == 1


def test_batch_scale_mismatch_is_caught(audit, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOTTERY_CONFIG", ["双色球"], raising=False)
    _hist(tmp_path, "双色球", [_row(批次规模=200), _row(批次规模=200)])
    r = audit.check_lookahead(3650)
    assert r["明细"]["双色球"]["批次规模不符"] == 1


# ---------------------------------------------------------------- C
def test_hit_rate_fingerprint_catches_impossible_rate(audit, tmp_path, monkeypatch):
    """★ 若每条都中奖（命中率 100%），z 必然是天文数字 ⇒ 必须报超阈值。"""
    monkeypatch.setattr(audit, "LOTTERY_CONFIG", ["双色球"], raising=False)
    rows = [_row(期号=26100 + i, 批次号=f"B{i}", 中奖等级="六等奖")
            for i in range(200)]
    _hist(tmp_path, "双色球", rows)
    r = audit.check_hitrate(3650)
    assert not r["通过"]
    assert "双色球" in r["超阈值彩种"]


def test_hit_rate_fingerprint_passes_for_all_miss(audit, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOTTERY_CONFIG", ["双色球"], raising=False)
    rows = [_row(期号=26100 + i, 批次号=f"B{i}", 中奖等级="未中")
            for i in range(200)]
    _hist(tmp_path, "双色球", rows)
    r = audit.check_hitrate(3650)
    # 全不中在 6.7% 的理论率下 z 远低于 -4，会报「偏低」但**不属于前视**，
    # 因此不影响通过（前视只会让命中率偏高）。
    assert r["明细"]["双色球"]["z"] < 0


# ---------------------------------------------------------------- 渲染
def test_render_contains_all_sections(audit, tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "LOTTERY_CONFIG", ["双色球"], raising=False)
    _hist(tmp_path, "双色球", [_row()])
    rep = {"文件完整性": audit.check_files(),
           "前视判据": audit.check_lookahead(3650),
           "命中率指纹": audit.check_hitrate(3650)}
    md = audit.render(rep, 9)
    for kw in ("文件完整性", "前视判据", "命中率指纹", "总判定"):
        assert kw in md
