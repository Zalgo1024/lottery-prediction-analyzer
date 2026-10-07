# -*- coding: utf-8 -*-
"""D 轨（派奖期 vs 平常期）测试。

覆盖：
  ① 派奖期识别（真数据 21 期；无派奖列必须显式不适用并给出原因）
  ② 合成对照组：派奖期真偏低 → 检出；两组相同 → 未检出且**必须**报「功效低」
  ③ 派奖期太少 → 显式不适用（不许硬凑）
  ④ `gap` 语义：只表示"两组有差异"，不是"存在机制缝隙"
"""
from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest

from ev.mechanism import sales_grade as SG
from ev.mechanism.gap_d_special_period import special_period_report


_P = {"一等": 1 / 15_000_000, "二等": 14 / 15_000_000, "三等": 54 / 15_000_000,
      "四等": 1971 / 15_000_000, "五等": 31590 / 15_000_000,
      "六等": 1188270 / 15_000_000}


def _frame(*, n_aw: int = 60, n_nm: int = 400, ratio_aw: float = 1.0,
           ratio_nm: float = 1.0, sales: float = 30_000_000.0,
           has_award: bool = True, noise: bool = True, seed: int = 3):
    """
    合成 七星彩 帧（现行段内），六个奖级都造，ratio_* 乘在期望注数上。
    noise=True 时用泊松抽样（真实数据必然有噪声；无噪声会让 bootstrap se=0，
    那是退化情形，另有专门用例覆盖）。
    """
    rng = np.random.default_rng(seed)
    start = SG.QXC_RULE_CUTOFF + timedelta(days=1)
    n = n_aw + n_nm
    issues, dates, sales_l, award = [], [], [], []
    y = {c: [] for c in _P}
    for i in range(n):
        issues.append(f"{21000 + i}")
        dates.append(start + timedelta(days=2 * i))
        sales_l.append(float(sales))
        aw = i < n_aw
        award.append(bool(aw))
        r = ratio_aw if aw else ratio_nm
        for c, p in _P.items():
            e = sales / 2.0 * p * r
            y[c].append(float(rng.poisson(max(e, 1e-9))) if noise else e)
    return SG.DigitalFrame(
        lottery="七星彩", issues=issues, dates=dates,
        sales=np.asarray(sales_l),
        notes={c: np.asarray(v, dtype=np.float64) for c, v in y.items()},
        prizes={c: np.full(n, 5.0) for c in _P},
        award=np.asarray(award, dtype=bool),
        has_award_columns=has_award, source="<synthetic>",
    )


def _level(r, cat):
    return [x for x in r["各奖级"] if x["奖级"] == cat][0]


# ------------------------------------------------------------------ ① 识别
def test_real_qxc_award_periods_found():
    r = special_period_report("七星彩", B=1000)
    assert r["applicable"] is True
    assert r["n_派奖期"] == 21
    dts = r["派奖期日期"]
    assert dts[0] >= "2021-01-01"
    assert all(d >= SG.QXC_RULE_CUTOFF.isoformat() for d in dts)


@pytest.mark.parametrize("lot", ["排列3", "排列5", "福彩3D"])
def test_no_award_columns_means_not_applicable(lot):
    r = special_period_report(lot)
    assert r["applicable"] is False
    assert "无「派奖」列" in r["note"]
    assert r["未覆盖的特别时期形态"]


def test_lotto_lottery_not_applicable():
    r = special_period_report("双色球")
    assert r["applicable"] is False
    assert "不硬凑代理" in r["note"]


# ------------------------------------------------------------------ ② 对照组
def test_synthetic_award_period_lower_is_detected(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame",
                        lambda lot: _frame(n_aw=60, ratio_aw=0.85, ratio_nm=1.0))
    r = special_period_report("七星彩", B=800)
    assert r["applicable"] is True
    lv = _level(r, "六等")
    assert lv["判_派奖更低"] is True, lv
    assert lv["差_z"] < -1.96
    assert r["gap"] is True
    assert "支持" in r["结论"]


def test_synthetic_equal_groups_not_detected_and_power_is_disclosed(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame",
                        lambda lot: _frame(n_aw=60, ratio_aw=1.0, ratio_nm=1.0,
                                           seed=17))
    r = special_period_report("七星彩", B=800)
    assert r["gap"] is False, [(x["奖级"], x.get("差_z")) for x in r["各奖级"]]
    assert "未检出差异" in r["结论"]
    assert "功效极低" in r["结论"], "未检出时必须披露功效不足"
    assert r["功效"]["检出下限"]["可检出|ΔR|下限"] > 0


def test_synthetic_higher_award_period_is_not_flagged(monkeypatch):
    """派奖期反而更高 → 不能判成"更冷"，方向必须写对。"""
    monkeypatch.setattr(SG, "load_digital_frame",
                        lambda lot: _frame(n_aw=60, ratio_aw=1.20, ratio_nm=1.0))
    r = special_period_report("七星彩", B=800)
    assert _level(r, "六等")["判_派奖更低"] is False
    assert r["gap"] is False


def test_zero_variance_synthetic_cannot_produce_a_z(monkeypatch):
    """退化情形：组内方差为 0（无噪声合成）→ 必须显式给不出 z，不许伪造显著性。"""
    monkeypatch.setattr(SG, "load_digital_frame",
                        lambda lot: _frame(n_aw=60, ratio_aw=0.85,
                                           ratio_nm=1.0, noise=False))
    r = special_period_report("七星彩", B=200)
    lv = _level(r, "六等")
    assert lv["差_派奖减平常"] == pytest.approx(-0.15, abs=1e-9)
    assert lv["差_se"] == 0.0
    assert lv["差_z"] is None
    assert r["gap"] is False


# ------------------------------------------------------------------ ③ 太少
def test_too_few_award_periods_degrades(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame",
                        lambda lot: _frame(n_aw=3, ratio_aw=0.5, ratio_nm=1.0))
    r = special_period_report("七星彩", B=300)
    assert r["applicable"] is False
    assert "只有 3 期" in r["note"]


def test_award_frame_without_columns_degrades(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame",
                        lambda lot: _frame(n_aw=30, has_award=False))
    r = special_period_report("七星彩", B=300)
    assert r["applicable"] is False
    assert "无「派奖」列" in r["note"]


# ------------------------------------------------------------------ ④ 语义
def test_gap_semantics_is_difference_not_mechanism(monkeypatch):
    monkeypatch.setattr(SG, "load_digital_frame",
                        lambda lot: _frame(n_aw=60, ratio_aw=0.80, ratio_nm=1.0))
    r = special_period_report("七星彩", B=500)
    assert r["gap"] is True
    assert "不是" in r["gap说明"] and "机制缝隙" in r["gap说明"]


def test_real_qxc_report_is_honest_about_underpower():
    r = special_period_report("七星彩", B=1000)
    assert r["n_派奖期"] == 21 and r["n_平常期"] > 800
    assert r["功效"]["检出下限"]["可检出|ΔR|下限"] > 0.2
    assert "功效" in r["结论"] or "未检出差异" in r["结论"]
