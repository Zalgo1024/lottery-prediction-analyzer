# -*- coding: utf-8 -*-
"""rollover 冷门口径（策略升级 A）测试。

契约：
  ① 既有键（期望一等奖注数λ/头奖单注奖金估计）保持 λ 口径——语义零漂移
    （payout.py 兜底与前端 audit.html 依赖既有键名）；
  ② 新键（μ_冷门/头奖单注估计(冷门口径)/split_margin(冷门)/参与信号(冷门口径)）
    只追加不覆盖；
  ③ μ_冷门 < λ ⇒ 冷门口径单注 ≥ λ 口径单注（参与门槛更低的机制方向）；
  ④ NO-Go → 显式回退（λ均摊口径 + 备注含「回退」），不许静默；
  ⑤ 数字型 → error dict（不再裸崩）。

fixture 用 monkeypatch 伪造流行度模型：features_matrix 恒返回 -1 向量、
β = -ln2/9×9 → 形态因子 = exp(-ln2) = 0.5（μ_冷门 = λ×0.5，确定性）。
数据取 head_pool/λ < cap 的组合（1.86e6 < 1e7），冷门口径同不饱和，可精确断言。
"""
import numpy as np
import pandas as pd
import pytest

import ev.popularity as pop
import ev.rollover as ro

POOL = 600_000_000.0
SALES = 8_000_000_000.0
CAP = 10_000_000
_P1 = 1.0 / 17_721_088


def _df():
    return pd.DataFrame({"期号": ["26111"], "奖池奖金": [POOL], "总投注额": [SALES]})


@pytest.fixture
def fake_nogo(monkeypatch):
    monkeypatch.setattr(ro, "_load_history", lambda name: _df())
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": False, "report": {}})


@pytest.fixture
def fake_go(monkeypatch):
    """GO 假模型：形态因子恒 = exp(β·(-1)) = 0.5。"""
    monkeypatch.setattr(ro, "_load_history", lambda name: _df())
    beta = np.full(9, np.log(2.0) / 9.0)   # z=-1（features 恒 -1）→ β·z = -ln2 → 因子 0.5
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": True, "beta": beta,
                                     "m_": np.zeros(9), "s_": np.ones(9),
                                     "report": {}})
    monkeypatch.setattr(pop, "cold_hot_combo",
                        lambda lot, q_cold=0.1, q_hot=0.9:
                        ([1, 2, 3, 4, 5, 6], [1, 9, 17, 23, 29, 33]))
    monkeypatch.setattr(pop, "features_matrix",
                        lambda R: np.tile(np.array([-1.0] * 9), (len(R), 1)))


def test_mu_cold_state_go(fake_go):
    factor, basis = ro._mu_cold_state("双色球")
    assert factor == pytest.approx(0.5)
    assert "分位组合" in basis


def test_mu_cold_state_nogo(fake_nogo):
    factor, basis = ro._mu_cold_state("双色球")
    assert factor is None
    assert "回退" in basis and "Go" in basis


def test_existing_keys_semantics_unmoved(fake_go):
    """① 既有键保持 λ 口径（payout.py 兜底/前端依赖，语义零漂移）。"""
    r = ro.rollover_ev("双色球")
    lam = SALES / 2.0 * _P1
    jpw = min(CAP, POOL * 0.7 / lam)
    assert r["期望一等奖注数λ"] == pytest.approx(lam, abs=1e-3)
    assert r["头奖单注奖金估计"] == pytest.approx(jpw, abs=0.01)
    assert isinstance(r["参与信号"], bool)


def test_new_cold_keys_present(fake_go):
    """② 新键齐全且数值 = λ×0.5 口径手算值。"""
    r = ro.rollover_ev("双色球")
    lam = SALES / 2.0 * _P1
    mu_cold = lam * 0.5
    assert r["μ_冷门"] == pytest.approx(mu_cold, abs=1e-3)
    assert r["头奖单注估计(冷门口径)"] == pytest.approx(
        min(CAP, POOL * 0.7 / mu_cold), abs=0.01)
    assert r["split_margin(冷门)"] == pytest.approx(
        (POOL * 0.7 / CAP) / mu_cold, abs=1e-3)
    assert r["参与信号(冷门口径)"] is False      # 数据构造为不饱和
    assert "冷门口径可用" in r["备注"]
    assert "冷门口径" in r["分摊口径"]


def test_cold_threshold_lower(fake_go):
    """③ μ_冷门 < λ ⇒ 冷门口径单注 ≥ λ 口径单注（A 项的机制方向）。"""
    r = ro.rollover_ev("双色球")
    assert r["μ_冷门"] < r["期望一等奖注数λ"]
    assert r["头奖单注估计(冷门口径)"] >= r["头奖单注奖金估计"]
    assert r["split_margin(冷门)"] >= (POOL * 0.7 / CAP) / r["期望一等奖注数λ"]


def test_nogo_degrades_explicitly(fake_nogo):
    """④ NO-Go → λ 均摊回退，冷门键 None，备注显式含「回退」。"""
    r = ro.rollover_ev("双色球")
    assert "λ均摊" in r["分摊口径"]
    assert r["μ_冷门"] is None
    assert r["头奖单注估计(冷门口径)"] is None
    assert r["split_margin(冷门)"] is None
    assert r["参与信号(冷门口径)"] is None
    assert "回退" in r["备注"]
    assert r["头奖单注奖金估计"] > 0            # 既有键不受影响


def test_history_rows_have_cold_keys(fake_go):
    """④' rollover_history 行含冷门新键，既有键不动。"""
    rows = ro.rollover_history("双色球", n=3)
    assert rows
    for row in rows:
        assert "头奖单注估计" in row and "参与信号" in row      # 既有键
        assert "μ_冷门" in row and "头奖单注估计(冷门口径)" in row
        assert row["μ_冷门"] is not None


def test_digital_lottery_rejected():
    """⑤ 数字型 → error dict（修复原先 KeyError/文件缺失裸崩）。"""
    r = ro.rollover_ev("排列5")
    assert "error" in r
