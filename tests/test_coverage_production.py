# -*- coding: utf-8 -*-
"""覆盖选择接生产（v1.9）的测试。

锁五条：
  ① 注数守恒（选多少就是多少，绝不增注）；
  ② 无重复注 / 号码在范围内 / 分区个数正确；
  ③ 同期跨进程可复现（crc32 seed，不用 `hash()`）；
  ④ 失败即降级：探针抛错时沿用原票，且 KPI 里必须留下原因；
  ⑤ 未启用彩种**逐位不变**（不新增字段、不改号码）。

★ 全部用极小的 MC 采样与单一臂，避免测试跑成生产那么慢（生产 MC=100000）。
"""
from __future__ import annotations

import json

import pytest

from prediction import coverage_production as CP


FAST = {
    "启用彩种": ["双色球"],
    "目标": {"kind": "payout", "min_payout": 10.0},
    "候选臂": ["union_greedy"],
    "候选池下限": 400,
    "候选池倍数": 40,
    "MC评估采样数": 400,
    "MC拟合采样数": 400,
    "重叠上限": 3,
    "超时秒": 120,
    # 测试用极小的 K ⇒ 可争取空间天然很小。把闸门关掉，专测选号本体；
    # 空间闸本身由 `test_select_skips_when_headroom_tiny` 单独锁。
    "最小可争取空间": 0.0,
}


@pytest.fixture()
def fast_cfg():
    return dict(FAST)


# ---------------------------------------------------------------- 选号本体
def test_select_conserves_ticket_count(fast_cfg):
    rows, kpi = CP.select("双色球", 8, issue=2026120, cfg=fast_cfg)
    assert len(rows) == 8
    assert kpi["注数"] == 8
    assert kpi["是否替换"] is True


def test_select_has_no_duplicates_and_valid_numbers(fast_cfg):
    rows, _ = CP.select("双色球", 12, issue=2026121, cfg=fast_cfg)
    keys = [tuple(sorted(r["红球"])) + tuple(r["蓝球"]) for r in rows]
    assert len(set(keys)) == len(keys)
    for r in rows:
        assert len(r["红球"]) == 6 and len(set(r["红球"])) == 6
        assert all(1 <= n <= 33 for n in r["红球"])
        assert len(r["蓝球"]) == 1 and 1 <= r["蓝球"][0] <= 16


def test_select_is_deterministic_across_calls(fast_cfg):
    a, _ = CP.select("双色球", 8, issue=2026122, cfg=fast_cfg)
    b, _ = CP.select("双色球", 8, issue=2026122, cfg=fast_cfg)
    assert a == b


def test_select_differs_across_issues(fast_cfg):
    a, _ = CP.select("双色球", 8, issue=2026122, cfg=fast_cfg)
    b, _ = CP.select("双色球", 8, issue=2026123, cfg=fast_cfg)
    assert a != b


def test_kpi_reports_both_sides(fast_cfg):
    """KPI 必须同时给出「比随机好多少」与「回款不变」的声明。"""
    _, kpi = CP.select("双色球", 8, issue=2026124, cfg=fast_cfg)
    assert kpi["对比随机基线"] is not None
    assert kpi["理论上限Kp"] > 0
    assert "期望回款不变" in kpi["诚实声明"]
    assert "不预测开奖号码" in kpi["诚实声明"]


def test_exact_p_never_exceeds_cap(fast_cfg):
    _, kpi = CP.select("双色球", 8, issue=2026125, cfg=fast_cfg)
    assert kpi["选中精确P"] <= kpi["理论上限Kp"] + 1e-12


def test_select_skips_when_headroom_tiny(fast_cfg):
    """K·p 远大于 1 时「至少中一次」已接近必然，覆盖优化没有空间 ⇒ 明确跳过。"""
    cfg = dict(fast_cfg, **{"最小可争取空间": 0.99})
    rows, kpi = CP.select("双色球", 6, issue=2026127, cfg=cfg)
    assert rows == []
    assert kpi["是否替换"] is False
    assert kpi["跳过"] is True
    assert "可争取空间" in kpi["跳过原因"]


def test_headroom_peaks_near_kp_one(fast_cfg):
    """可争取空间在 K·p≈1 处最大，两头都小。

    ★ 这是「接生产后收益变小」的根本原因，也是本次唯一需要记住的形状：
      K·p=0.16 → 1.2pp；K·p≈1.0 → 36.8pp；K·p=4.1 → 1.6pp。
      注数太少没覆盖可谈，太多则「至少中一次」已接近必然，覆盖也无从改善。
    """
    low, k_low = CP.select("双色球", 20, issue=2026128, cfg=fast_cfg)      # K·p≈0.16
    peak, k_peak = CP.select("双色球", 122, issue=2026128, cfg=fast_cfg)   # K·p≈1.00
    high, k_high = CP.select("双色球", 500, issue=2026128, cfg=fast_cfg)   # K·p≈4.10
    assert k_peak["可争取空间"] > k_low["可争取空间"]
    assert k_peak["可争取空间"] > k_high["可争取空间"]
    assert k_high["随机独立基线"] > k_peak["随机独立基线"]
    assert len(low) == 20 and len(peak) == 122 and len(high) == 500


def test_zero_tickets_rejected(fast_cfg):
    with pytest.raises(ValueError, match="注数必须为正"):
        CP.select("双色球", 0, issue=2026120, cfg=fast_cfg)


def test_all_arms_failing_raises(fast_cfg, monkeypatch):
    from ev.arena import arms as ARMS

    def _boom(*a, **kw):
        raise RuntimeError("模拟选号失败")

    monkeypatch.setitem(ARMS.ARMS, "union_greedy", _boom)
    with pytest.raises(RuntimeError, match="所有候选臂"):
        CP.select("双色球", 6, issue=2026126, cfg=fast_cfg)


def test_validate_rejects_count_mismatch():
    rows = [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}]
    with pytest.raises(ValueError, match="注数不符"):
        CP._validate("双色球", rows, 2)


def test_validate_rejects_duplicates():
    row = {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}
    with pytest.raises(ValueError, match="重复注"):
        CP._validate("双色球", [row, dict(row)], 2)


def test_validate_rejects_out_of_range():
    rows = [{"红球": [1, 2, 3, 4, 5, 34], "蓝球": [1]}]
    with pytest.raises(ValueError, match="越界"):
        CP._validate("双色球", rows, 1)


# ---------------------------------------------------------------- 开关
def test_enabled_lotteries_default():
    cfg = CP._load_config()
    assert CP.is_enabled("双色球", cfg) is True
    assert CP.is_enabled("大乐透", cfg) is False


def test_config_path_is_redirected_by_conftest():
    assert "pytest" in str(CP.CONFIG_PATH) or "tmp" in str(CP.CONFIG_PATH).lower()


def test_broken_config_falls_back_to_default(monkeypatch, tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(CP, "CONFIG_PATH", bad)
    cfg = CP._load_config()
    assert cfg["启用彩种"] == CP.DEFAULT_CONFIG["启用彩种"]


def test_config_override_can_disable(monkeypatch, tmp_path):
    p = tmp_path / "cov.json"
    p.write_text(json.dumps({"启用彩种": []}, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(CP, "CONFIG_PATH", p)
    assert CP.is_enabled("双色球") is False


# ---------------------------------------------------------------- 引擎接线
@pytest.fixture()
def fast_engine(monkeypatch):
    monkeypatch.setattr(CP, "DEFAULT_CONFIG", dict(FAST))
    monkeypatch.setattr(CP, "CONFIG_PATH", CP.CONFIG_PATH)   # 保持被 conftest 隔离的路径
    return CP


def test_engine_applies_coverage_and_keeps_count(fast_engine):
    from prediction import engine
    r = engine.predict("双色球", groups=6, record_pending=False)
    assert r["号码组数"] == 6
    assert r["目标注数"] == 6
    assert len(r["预测号码"]) == 6
    cov = r["覆盖优化"]
    assert cov["是否替换"] is True
    assert cov["注数"] == 6
    assert {t["策略"] for t in r["预测号码"]} == {"覆盖选择"}


def test_engine_degrades_to_original_when_selection_raises(fast_engine, monkeypatch):
    from prediction import engine

    def _boom(*a, **kw):
        raise RuntimeError("模拟覆盖失败")

    monkeypatch.setattr(fast_engine, "select", _boom)
    r = engine.predict("双色球", groups=6, record_pending=False)
    assert r["号码组数"] == 6
    cov = r["覆盖优化"]
    assert cov["是否替换"] is False
    assert "模拟覆盖失败" in cov["降级原因"]
    # 原策略票被保留 ⇒ 策略不再全是「覆盖选择」
    assert "覆盖选择" not in {t["策略"] for t in r["预测号码"]}


def test_engine_skips_unknown_lottery(fast_engine, monkeypatch):
    """未启用彩种不许出现 `覆盖优化` 字段（逐位不变）。"""
    from prediction import engine
    assert "大乐透" not in (fast_engine._load_config()["启用彩种"])
    seen = {}

    def _spy(lottery):
        seen["called"] = seen.get("called", 0) + 1
        return False

    monkeypatch.setattr(fast_engine, "is_enabled", _spy)
    r = engine.predict("双色球", groups=6, record_pending=False)
    assert "覆盖优化" not in r
    assert seen["called"] >= 1
