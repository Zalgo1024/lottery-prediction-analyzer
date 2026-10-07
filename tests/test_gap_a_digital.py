# -*- coding: utf-8 -*-
"""A′ 轨（数字型 / 七星彩注数比）测试。

覆盖：
  ① 数据层：原始销售奖级 CSV 的「缺失 vs 0」区分、奖级名推导、规则切段
  ② 口径：七星彩规则变更日与项目内四处既有常量**同源**（锁死，防漂移）
  ③ 判定：合成零假设 → R=1 不判缝隙；合成偏置 → 判缝隙；泊松噪声不误报
  ④ 降级：排列3 / 福彩3D 必须显式不适用并给出量化理由（不得静默返回 nothing）
  ⑤ 单奖级彩种的免票量口径必须显式不可用
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from ev.mechanism import sales_grade as SG
from ev.mechanism.gap_a_ticket_ratio import digital_ticket_ratio_test


# ------------------------------------------------------------------ 合成工具
def _pl5_frame(n: int = 400, *, ratio: float = 1.0, sales: float = 20_000_000.0,
               start: date = date(2017, 1, 3), noise: float = 0.0,
               seed: int = 7):
    """
    排列5 合成帧：p=1e-5，票量 = sales/2 ⇒ E[一等奖注数] = sales/2 × 1e-5 = 100。
    ratio 直接乘在期望注数上（ratio=1 ⇒ R=1；ratio=0.9 ⇒ R=0.9）。
    """
    rng = np.random.default_rng(seed)
    exp = sales / 2.0 * 1e-5
    y = np.full(n, exp * ratio, dtype=np.float64)
    if noise > 0:
        y = rng.poisson(np.clip(y, 1e-6, None)).astype(np.float64)
    return SG.DigitalFrame(
        lottery="排列5",
        issues=[f"{17000 + i}" for i in range(n)],
        dates=[start + timedelta(days=2 * i) for i in range(n)],
        sales=np.full(n, float(sales)),
        notes={"一等": y},
        prizes={"一等": np.full(n, 100000.0)},
        award=np.zeros(n, dtype=bool),
        has_award_columns=False,
        source="<synthetic>",
    )


def _qxc_frame(n_old: int = 100, n_new: int = 200, *, ratio_new: float = 1.0,
               sales: float = 30_000_000.0, seed: int = 11):
    """
    七星彩合成帧：cutoff 前 n_old 期（注数故意巨大，用来验证"必须切段"），
    cutoff 后 n_new 期（ratio_new 控制偏置）。
    """
    old_start = SG.QXC_RULE_CUTOFF - timedelta(days=3 * n_old)
    issues, dates, sales_l, y1, award = [], [], [], [], []
    for i in range(n_old):
        issues.append(f"old{i}")
        dates.append(old_start + timedelta(days=3 * i))
        sales_l.append(float(sales))
        y1.append(1e9)                      # 旧规则段的巨额注数
        award.append(False)
    p1 = 1.0 / 15_000_000.0
    exp_new = sales / 2.0 * p1
    for i in range(n_new):
        issues.append(f"new{i}")
        dates.append(SG.QXC_RULE_CUTOFF + timedelta(days=3 * i))
        sales_l.append(float(sales))
        y1.append(exp_new * ratio_new)
        award.append(False)
    n = n_old + n_new
    return SG.DigitalFrame(
        lottery="七星彩", issues=issues, dates=dates,
        sales=np.asarray(sales_l),
        notes={"一等": np.asarray(y1, dtype=np.float64)},
        prizes={"一等": np.full(n, 5_000_000.0)},
        award=np.asarray(award, dtype=bool),
        has_award_columns=True, source="<synthetic>",
    )


# ------------------------------------------------------------------ ② 口径同源
def test_qxc_rule_cutoff_is_same_as_all_existing_sources():
    """
    七星彩规则变更日在本项目里出现了 4 次（data_quality / randomness_audit /
    always_valid / info_theory）。它们必须与 `sales_grade.QXC_RULE_CUTOFF` 一致 ——
    这里锁死，任何一处被改都会红。
    """
    import importlib
    mods = ["credibility.data_quality", "credibility.randomness_audit",
            "discovery.always_valid", "discovery.info_theory"]
    for m in mods:
        mod = importlib.import_module(m)
        assert getattr(mod, "_QXC_CUTOFF") == SG.QXC_RULE_CUTOFF, \
            f"{m}._QXC_CUTOFF 与 sales_grade.QXC_RULE_CUTOFF 不一致"
    assert SG.QXC_RULE_CUTOFF == date(2020, 10, 11)


# ------------------------------------------------------------------ ① 数据层
def test_load_all_four_digital_frames():
    for lot in ("排列3", "排列5", "福彩3D", "七星彩"):
        fr = SG.load_digital_frame(lot)
        assert fr is not None, f"{lot} 装载失败"
        assert fr.n > 1000
        assert fr.sales.shape == (fr.n,)
        assert all(fr.notes[k].shape == (fr.n,) for k in fr.notes)
        # 升序？
        assert fr.dates == sorted(fr.dates), f"{lot} 未按日期升序"


def test_missing_sales_is_zero_but_notes_stay_nan():
    """「缺失」与「0」必须分开：销售额缺失 → 0（好过滤）；注数缺失 → NaN（不能当 0 用）。"""
    fr = SG.load_digital_frame("排列5")
    assert (fr.sales >= 0).all()
    assert (fr.sales == 0).sum() > 0, "排列5 应有大量早期无销售额的期"
    y1 = fr.notes["一等"]
    # 排列5 一等奖注数在 CSV 里几乎全覆盖（仅极个别空行）
    assert int(np.isnan(y1).sum()) <= 2
    # 福彩3D 的组选3注数只在组选3型开奖期才有值
    d3 = SG.load_digital_frame("福彩3D")
    assert int(np.isnan(d3.notes["组选3"]).sum()) > 0
    assert float(np.nanmax(d3.notes["组选3"])) > 0


def test_category_names_derived_from_columns():
    assert set(SG.load_digital_frame("排列3").notes) == {"直选", "组选3", "组选6"}
    assert set(SG.load_digital_frame("七星彩").notes) == \
        {"一等", "二等", "三等", "四等", "五等", "六等"}


def test_prob_key_maps_pl5_to_direct():
    """排列5 的 CSV 列叫「一等奖注数」，但规则上只有一个奖级「直选」(p=1e-5)。"""
    assert SG.prob_key("排列5", "一等") == "直选"
    assert SG.prob_key("七星彩", "三等") == "三等"
    assert SG.prob_key("未知", "X") == "X"


def test_award_periods_real_qxc_is_21():
    """真实的七星彩派奖期必须能被识别出来（2021 年 9 期 + 2023 年 12 期 = 21）。"""
    fr = SG.load_digital_frame("七星彩")
    ap = SG.award_periods(fr)
    assert ap["applicable"] is True
    assert ap["n"] == 21
    assert all(d >= "2021-01-01" for d in ap["日期"])


# ------------------------------------------------------------------ ③ 判定
def test_synthetic_fair_gives_r_one_and_no_gap(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame", lambda lot: _pl5_frame(400, ratio=1.0))
    r = digital_ticket_ratio_test("排列5", B=500)
    assert r["applicable"] is True
    assert r["主口径"]["R"] == pytest.approx(1.0, abs=1e-9)
    assert r["判定"]["gap"] is False


def test_synthetic_tilt_flags_gap(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame", lambda lot: _pl5_frame(400, ratio=0.90))
    r = digital_ticket_ratio_test("排列5", B=500)
    assert r["判定"]["gap"] is True, r["判定"]
    assert r["主口径"]["R"] == pytest.approx(0.90, rel=1e-9)
    assert r["判定"]["ci95"][1] < 1.0


def test_small_tilt_below_min_effect_is_not_flagged(monkeypatch):
    """R 低 1%（< min_effect=3%）→ 即使 CI 全在 1 以下也不判缝隙（效应量门控）。"""
    monkeypatch.setattr(SG, "load_digital_frame", lambda lot: _pl5_frame(400, ratio=0.99))
    r = digital_ticket_ratio_test("排列5", B=500, min_effect=0.03)
    assert r["判定"]["gap"] is False
    assert "最小效应量" in r["判定"]["note"]


def test_poisson_noise_around_fair_is_not_flagged(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame",
                        lambda lot: _pl5_frame(600, ratio=1.0, noise=1.0, seed=2026))
    r = digital_ticket_ratio_test("排列5", B=500)
    assert 0.9 < r["主口径"]["R"] < 1.1
    assert r["判定"]["gap"] is False, r["判定"]


# ------------------------------------------------------------------ ④ 降级
def test_pl3_and_3d_are_excluded_with_quantified_reason():
    for lot in ("排列3", "福彩3D"):
        r = digital_ticket_ratio_test(lot)
        assert r["applicable"] is False
        note = r["note"]
        assert "全部玩法" in note
        assert "0.6766" in note or "2022-01" in note, note


def test_lotto_lottery_degrades_in_a_prime_track():
    r = digital_ticket_ratio_test("双色球")
    assert r["applicable"] is False
    assert "适用清单" in r["note"]


# ------------------------------------------------------------------ 规则切段
def test_qxc_uses_rule_segment_only(monkeypatch):
    """
    旧规则段（cutoff 前）的注数被灌成天文数字：只要切段正确，R 就必须仍是 1。
    这正是 A′ 轨必须**整段切**（而 C 轨只切第 7 位）的理由。
    """
    monkeypatch.setattr(SG, "load_digital_frame", lambda lot: _qxc_frame(100, 200, ratio_new=1.0))
    r = digital_ticket_ratio_test("七星彩", B=500)
    assert r["applicable"] is True
    assert r["规则段"] == "2020-10-11"
    assert r["n"] == 200, "旧规则段没有被剔除"
    assert r["主口径"]["R"] == pytest.approx(1.0, abs=1e-9)


def test_qxc_segment_tilt_still_detected(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame", lambda lot: _qxc_frame(50, 300, ratio_new=0.90))
    r = digital_ticket_ratio_test("七星彩", B=500)
    assert r["n"] == 300
    assert r["主口径"]["R"] == pytest.approx(0.90, rel=1e-9)
    assert r["判定"]["gap"] is True


def test_rule_mask_is_all_true_without_rule_change():
    fr = SG.load_digital_frame("排列5")
    assert fr.rule_mask().all()
    assert (fr.usable_mask() == (fr.sales > 0)).all()


def test_usable_mask_for_qxc_is_intersection_of_sales_and_segment():
    fr = SG.load_digital_frame("七星彩")
    assert fr.usable_mask().sum() < (fr.sales > 0).sum()
    assert fr.usable_mask().sum() == int(((fr.sales > 0) & fr.rule_mask()).sum())


# ------------------------------------------------------------------ ⑤ 免票量口径
def test_calib_unavailable_for_single_level_lottery(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame", lambda lot: _pl5_frame(200))
    r = digital_ticket_ratio_test("排列5", B=200)
    assert r["免票量校验"]["applicable"] is False
    assert "两个奖级" in r["免票量校验"]["note"]


def test_real_pl5_and_qxc_smoke():
    """真数据冒烟：两彩种的 R 都必须在 1 附近（本项目 P2 的实测结论）。"""
    r5 = digital_ticket_ratio_test("排列5", B=400)
    assert r5["applicable"] and r5["n"] > 3000
    assert 0.9 < r5["主口径"]["R"] < 1.15
    assert r5["判定"]["gap"] is False

    rq = digital_ticket_ratio_test("七星彩", B=400)
    assert rq["applicable"] and rq["n"] > 800
    for cat, lv in rq["各奖级"].items():
        if lv.get("usable"):
            assert 0.9 < lv["R"] < 1.15, (cat, lv["R"])
    # 六奖级观测同一张票：R 应彼此接近（实测极差 0.057）
    assert rq["各奖级R极差"] is not None and rq["各奖级R极差"] < 0.10
    # 一/二等 R 实测略高于 1 → 「全部偏低」不成立，不得粉饰
    assert rq["全奖级低于1"] is False
