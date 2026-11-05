# -*- coding: utf-8 -*-
"""tests/test_ev_basis_annotation.py —— EV 口径标注的牙齿护栏。

背景：`ev/profit/select.py::ev_proxy` 2026-11-04 修过一次「内层概率乘两遍」，
旧读数的绝对 EV 水平只有真实的约 1/5，且因为排序不变，比大小测试全绿。
若不把口径写进读数，新旧缓存里的「元/注」会被当成同一把尺子直接比较。

锁三件事：
  ① `annotate()` 是纯函数（不改入参），三个口径字段齐备且写清成因；
  ② 读数落盘的唯一出口 `refresh_strategy_robustness()` 必须带口径；
     `strategy_robustness()` 读新缓存透传、读老缓存显式写「未标注 + 原因」；
  ③ conftest 必须隔离 `ROBUSTNESS_PATH`（它是独立常量，不受 DIGEST_DIR 隔离影响），
     否则用例会覆盖当晚 23:40 的真身夜间留痕。
"""
from __future__ import annotations

import json
from pathlib import Path

import conftest
from ev.profit import ev_basis as EB
from web import health_digest as H


# ------------------------------------------------------------------ 纯函数
def test_annotate_is_pure_and_complete():
    src = {"生成时间": "2026-11-05T23:40:00", "彩种": {}}
    out = EB.annotate(src)
    assert out is not src
    assert "EV口径" not in src, "annotate 不许改入参"
    assert out["生成时间"] == src["生成时间"] and out["彩种"] == {}
    assert out["EV口径"] == EB.CURRENT_EV_BASIS
    assert "约 1/5" in out["EV口径说明"] and "19.2%" in out["EV口径说明"]
    assert "不可直接比较" in out["历史读数说明"]


def test_legacy_label_is_explicit():
    lab = EB.legacy_label()
    assert lab["EV口径"] == EB.LEGACY_EV_BASIS != EB.CURRENT_EV_BASIS
    assert "不可直接比较" in lab["历史读数说明"]


def test_unannotated_reason_carries_why():
    """缺席≠通过：老缓存缺字段要写原因，不许静默留空 / 当成通过。"""
    assert "未标注" in EB.UNANNOTATED_REASON and "原因" in EB.UNANNOTATED_REASON


# ------------------------------------------------------------------ 落盘 / 读取
def _stub_refresh(monkeypatch):
    """把昂贵的逐期压力测试换成固定读数，只验口径元数据。"""
    import ev.profit.stress as ST

    monkeypatch.setattr(H, "_robustness_skip_reason", lambda lot: None)
    monkeypatch.setattr(H, "_latest_target_issue", lambda lot: "26127")
    monkeypatch.setattr(ST, "stress_test",
                        lambda lot, issue, K=20, **k: {
                            "判决": "退化", "基准优势_元每注": 0.08,
                            "前视检查": True, "情景": []})


def test_refresh_writes_ev_basis_to_cache(monkeypatch):
    """★ 故意删掉 health_digest 里的 annotate() 调用 ⇒ 这条会红。"""
    _stub_refresh(monkeypatch)
    out = H.refresh_strategy_robustness(("双色球",))
    assert out["EV口径"] == EB.CURRENT_EV_BASIS
    doc = json.loads(Path(H.ROBUSTNESS_PATH).read_text(encoding="utf-8"))
    assert doc["EV口径"] == EB.CURRENT_EV_BASIS
    assert "约 1/5" in doc["EV口径说明"]
    assert doc["彩种"]["双色球"]["判决"] == "退化"


def test_read_new_cache_exposes_basis(monkeypatch):
    _stub_refresh(monkeypatch)
    H.refresh_strategy_robustness(("双色球",))
    r = H.strategy_robustness()
    assert r["可用"] is True
    assert r["EV口径"] == EB.CURRENT_EV_BASIS
    assert "不可直接比较" in r["历史读数说明"]


def test_read_old_cache_is_explicitly_unannotated(monkeypatch):
    """老缓存没有口径字段 ⇒ 显式写「未标注 + 原因」，不许伪装成当前口径。"""
    from datetime import datetime
    old = {"生成时间": datetime.now().isoformat(timespec="seconds"),
           "彩种": {"双色球": {"判决": "退化", "已测": True, "情景": []}}}
    Path(H.ROBUSTNESS_PATH).parent.mkdir(parents=True, exist_ok=True)
    Path(H.ROBUSTNESS_PATH).write_text(json.dumps(old, ensure_ascii=False),
                                      encoding="utf-8")
    r = H.strategy_robustness()
    assert r["可用"] is True
    assert r["EV口径"] == EB.UNANNOTATED_REASON
    assert r["历史读数说明"] == EB.UNANNOTATED_REASON


def test_health_digest_source_calls_annotate():
    """静态牙齿：落盘出口必须真的调用 annotate（不是只在注释里提一句）。"""
    src = Path(H.__file__).read_text(encoding="utf-8")
    assert "annotate(" in src
    assert "from ev.profit.ev_basis import annotate" in src


# ------------------------------------------------------------------ 隔离 / 禁词
def test_conftest_isolates_robustness_path():
    """★ 独立常量必须单独隔离；只隔离 DIGEST_DIR 会写脏真身夜间缓存。"""
    triples = [(m, a, f) for m, a, f in conftest._TARGETS]
    assert ("web.health_digest", "ROBUSTNESS_PATH", "strategy_robustness.json") in triples
    assert ("web.health_digest", "DIGEST_DIR", "automation") in triples


def test_no_forbidden_narrative_words():
    src = Path(EB.__file__).read_text(encoding="utf-8")
    for bad in ("证伪", "证否", "不可预测", "已证明公平", "公平随机", "写死"):
        assert bad not in src, f"EV 口径模块出现禁词：{bad}"


def test_ev_basis_module_has_no_calculation_or_io():
    """口径模块只提供元数据，不许碰计算、不许写盘。"""
    src = Path(EB.__file__).read_text(encoding="utf-8")
    for bad in ("import numpy", "ev_proxy(", "write_text(", "open(", "record_spend"):
        assert bad not in src, f"EV 口径模块越界：{bad}"
