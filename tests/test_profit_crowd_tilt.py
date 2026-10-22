# -*- coding: utf-8 -*-
"""tests/test_profit_crowd_tilt.py —— B2/B3 代理 π 倾斜来源 + 边际频率上界。

锁住：
  · π 模型两级缓存（进程内 + 磁盘）往返
  · Z 因子分解 < 1e-9（offset 口径正确、打分可加）
  · κ=0 严格均匀（weights(tl,0) 均匀）
  · 中点秩分位均值 ≡ 0.5（与 popularity 同口径）
  · 七星彩第 7 位不参与打分（与第 7 位无关）
  · 福彩3D 因 2022 起直选注数全 0 ⇒ None 且原因含「2022」
  · 合成数据验 kappa_upper_marginal 已知答案（κ̂≈κ_true）
  · offset 口径：exp(scores+offset) 在宇宙上和 ≈ 1（相对购买概率）
"""
import numpy as np
import pytest

from ev.profit import crowd_tilt as CT
from ev.profit import tilt as T
from ev.profit.outcomes import outer_universe


# ---------------------------------------------------------------- π 缓存

def test_pi_model_cache_roundtrip():
    """同一进程内第二次调用应命中缓存（同一对象/等价结果）。"""
    pm1 = CT.pi_model("排列3")
    assert pm1 is not None
    pm2 = CT.pi_model("排列3")
    assert pm2 is not None
    assert pm1["n_obs"] == pm2["n_obs"]
    assert pm1["source"] == "crowd_pi_v2"
    # 进程内命中：身份或等价
    assert pm1 is pm2 or pm1["n_obs"] == pm2["n_obs"]


def test_pi_model_disk_cache_roundtrip(tmp_path, monkeypatch):
    """磁盘缓存落盘后，二次加载应命中（n_obs 一致即视为往返成功）。"""
    # 指向临时 cache 目录
    import os
    monkeypatch.setattr(CT, "_pi_cache_path",
                        lambda lot: str(tmp_path / f"crowd_pi_{lot}.json"))
    CT._PI_CACHE.clear()          # 清空进程内缓存，强制走磁盘写+读
    pm = CT.pi_model("排列5")
    assert pm is not None
    assert (tmp_path / "crowd_pi_排列5.json").exists()
    # 清空进程内缓存，强制走磁盘
    CT._PI_CACHE.clear()
    pm2 = CT.pi_model("排列5")
    assert pm2 is not None
    assert pm2["n_obs"] == pm["n_obs"]


# ---------------------------------------------------------------- Z 因子分解

@pytest.mark.parametrize("lot", ["排列3", "排列5", "七星彩"])
def test_z_factorization_below_tol(lot):
    """Z(κ) 直接求和 vs 因子分解，相对误差 < 1e-9（κ∈{0.5,1,3}）。"""
    zc = CT.z_factorization_check(lot)
    assert zc["可用"], zc
    assert zc["通过"], zc["明细"]
    for r in zc["明细"]:
        assert r["相对误差"] < 1e-9, r


# ---------------------------------------------------------------- κ=0 / 分位

def test_kappa0_is_uniform():
    """κ=0 下 P(ω|0) 严格均匀（倾斜机制的定义）。"""
    tl = CT.crowd_tilt("排列3")
    assert tl is not None
    w = T.weights(tl, 0.0)
    assert np.allclose(w, 1.0 / len(w))


def test_quantile_mean_is_half():
    """中点秩分位均值 ≡ 0.5（公平开奖无偏性，与 popularity 同口径）。"""
    tl = CT.crowd_tilt("排列5")
    assert tl is not None
    assert abs(float(tl.quantiles.mean()) - 0.5) < 1e-9


def test_offset_restores_relative_purchasing_probability():
    """offset = raw.mean() ⇒ exp(scores+offset) 在宇宙上和 ≈ 1（相对购买概率）。"""
    tl = CT.crowd_tilt("排列3")
    assert tl is not None
    total = float(np.exp(tl.scores + tl.offset).sum())
    assert total == pytest.approx(1.0, abs=1e-6)


# ---------------------------------------------------------------- 七星彩第 7 位无关

def test_qxc_score_ignores_seventh_digit():
    """七星彩打分只用前 6 位；第 7 位不同应给出相同 combo_score。"""
    base = [1, 2, 3, 4, 5, 6]
    s_a = CT.combo_score("七星彩", base + [0])
    s_b = CT.combo_score("七星彩", base + [9])
    assert s_a is not None and s_b is not None
    assert s_a == pytest.approx(s_b)


def test_qxc_tilt_only_uses_six_positions():
    """crowd_scores 对七星彩只吃前 6 位（outer_width=6）。"""
    tl = CT.crowd_tilt("七星彩")
    assert tl is not None
    assert tl.scores.shape[0] == 10 ** 6     # 前 6 位 10^6 个组合


# ---------------------------------------------------------------- 福彩3D 数据门

def test_fd3d_pi_unavailable_with_2022_reason():
    """福彩3D 直选注数自 2022 起全 0 ⇒ π 不可测、outer_tilt 返回 None。"""
    assert CT.pi_model("福彩3D") is None
    assert CT.unavailable_reason("福彩3D") is not None
    assert "2022" in CT.unavailable_reason("福彩3D")
    assert T.outer_tilt("福彩3D") is None


# ---------------------------------------------------------------- 合成数据验 κ_upper_marginal

def _synthetic_pm_and_counts():
    """构造：π 让第 0 位「热」（概率 0.5），其余均匀；
    观测计数按 κ=1 的边际 p_i(d;1) 生成 ⇒ κ̂ 应 ≈ 1。
    """
    w = 3
    hot = 0.5
    cold = (1.0 - hot) / 9.0
    pi = [[hot] + [cold] * 9 for _ in range(w)]
    pm = {"source": "crowd_pi_v2", "pi": pi, "n_obs": 1000,
          "zones": list(range(w)), "converged": True,
          "n_iter": 1, "nll": 0.0,
          "window": {"n": 1000, "first": "", "last": ""},
          "lottery": "排列3"}

    # κ=1 下 p_i(0;1) = hot^(-1)/Z，p_i(other;1)=cold^(-1)/Z
    z = hot ** (-1.0) + 9 * (cold ** (-1.0))
    p0 = hot ** (-1.0) / z
    pother = cold ** (-1.0) / z
    n_total = 10004
    n0 = int(round(p0 * n_total))
    nother = int(round(pother * n_total))
    # 修正最后一位使列和一致
    nother = (n_total - n0) // 9
    counts = np.zeros((w, 10), dtype=np.int64)
    for i in range(w):
        counts[i, 0] = n0
        counts[i, 1:] = nother
    return pm, counts, p0


def test_kappa_upper_marginal_recovers_known_kappa(monkeypatch):
    """合成数据（κ_true=1）下 κ̂≈1 且上界 > κ̂。"""
    pm, counts, _ = _synthetic_pm_and_counts()
    monkeypatch.setattr(CT, "pi_model", lambda lottery, refresh=False: pm)
    monkeypatch.setattr(CT, "draw_counts", lambda lottery: counts)

    res = T.kappa_upper_marginal("排列3", None, alpha=0.05)
    assert res["可用"], res
    # κ̂ 应接近 1（网格步长 0.01 + 有限样本，放宽到 0.2）
    assert abs(res["kappa_hat"] - 1.0) < 0.2, res
    assert res["kappa_upper"] > res["kappa_hat"] - 1e-9, res
    # 口径与免责必须随结果返回
    assert "代理" in res["口径"]
    assert "不约束位间联合分布" in res["免责"]


def test_kappa_upper_marginal_unavailable_for_fd3d():
    """福彩3D π 不可用 ⇒ kappa_upper_marginal 也不可用。"""
    res = T.kappa_upper_marginal("福彩3D")
    assert not res["可用"]
