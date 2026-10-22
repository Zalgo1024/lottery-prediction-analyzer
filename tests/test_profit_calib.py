# -*- coding: utf-8 -*-
"""tests/test_profit_calib.py —— Tobit 池模型校准的回归锁。

锁定的关键不变量：
1. 双色球稳健 a1 = median(W·Y/S) ∈ [0.12, 0.18]（验收区间）
2. 双色球触顶期占比 ∈ [0.15, 0.25]（验收区间）
3. build_pool_dataset 返回 P/S/W/Y 四数组且 n≥200
4. estimate_a1_empirical 中位口径与手算一致
5. estimate_cap_binding 触顶计数正确
6. 对数线性 Tobit 在合成数据上能恢复已知 a1（含右删失）
7. 校准的「数据坑」说明存在且点名 P/a1 口径
"""
import numpy as np

from ev.profit import calib

LOT = "双色球"
ISSUE = "2026280"


def test_acceptance_interval_a1_and_capped():
    res = calib.calibrate(LOT, ISSUE)
    assert res["ok"] is True
    assert 0.12 <= res["a1_稳健"] <= 0.18, res["a1_稳健"]
    assert 0.15 <= res["capped触顶占比"] <= 0.25, res["capped触顶占比"]
    # 稳健 a1 与均值口径（未删失干净量）应接近，均值口径≤0.18 上界约束
    assert res["a1_稳健"] <= res["a1_均值口径"] + 1e-9  # 中位≤均值（非负偏）
    # 数据坑存在
    assert "奖池奖金" in res["数据坑"] and "高估" in res["数据坑"]


def test_build_pool_dataset_shape_and_size():
    ds = calib.build_pool_dataset(LOT, ISSUE)
    assert ds is not None
    for k in ("P", "S", "W", "Y"):
        assert k in ds and isinstance(ds[k], np.ndarray)
    assert len(ds["P"]) >= 200
    assert len(ds["P"]) == len(ds["S"]) == len(ds["W"]) == len(ds["Y"])
    # 全部为正且 W>0
    assert (ds["W"] > 0).all() and (ds["S"] > 0).all() and (ds["Y"] > 0).all()


def test_estimate_a1_empirical_median():
    P = np.array([1e8, 2e8, 3e8])
    S = np.array([1e8, 2e8, 3e8])
    W = np.array([2.0, 3.0, 5.0])
    Y = np.array([0.15 * 1e8 / 2, 0.15 * 2e8 / 3, 0.15 * 3e8 / 5])
    out = calib.estimate_a1_empirical(P, S, W, Y, n_boot=50, seed=1)
    # ratio = W*Y/S = [0.15, 0.15, 0.15] → 中位=0.15
    assert abs(out["a1"] - 0.15) < 1e-9
    assert out["ci90_lo"] <= 0.15 <= out["ci90_hi"]


def test_estimate_cap_binding_count():
    Y = np.array([1e7, 1e7, 1e7, 5e6, 2e6])  # 3 个达到 cap
    out = calib.estimate_cap_binding(Y, cap=1e7)
    assert out["capped"] == 3 / 5
    assert out["n_hit"] == 3 and out["n"] == 5


def test_tobit_recovers_known_a1_with_censoring():
    rng = np.random.default_rng(7)
    n = 3000
    a1_true, sigma_true, cap = 0.15, 0.30, 1e7
    # S 在 [1e8,4e8]，W 在 [1,20] → 部分触顶、部分未触顶
    S = rng.uniform(1e8, 4e8, size=n)
    W = rng.integers(1, 21, size=n).astype(float)
    eps = rng.normal(0.0, sigma_true, size=n)
    log_y_star = np.log(a1_true) + np.log(S) - np.log(W) + eps
    y_star = np.exp(log_y_star)
    Y = np.minimum(y_star, cap)  # 右删失于 cap
    out = calib.fit_tobit_a1(S, W, Y, cap, seed=3)
    assert out["converged"]
    # 对数线性 Tobit 应恢复 a1≈0.15，误差<15%
    assert abs(out["a1"] - a1_true) / a1_true < 0.15, out["a1"]
    # log 尺度 sigma 应接近 0.3
    assert abs(out["sigma"] - sigma_true) / sigma_true < 0.25, out["sigma"]


def test_calibrate_returns_full_schema():
    res = calib.calibrate(LOT, ISSUE)
    for key in ("ok", "a1_稳健", "a1_ci90", "a1_tobit", "capped触顶占比",
                "capped_n", "n", "a1_在验收区间", "capped_在验收区间",
                "验收区间", "数据坑"):
        assert key in res
    assert len(res["a1_ci90"]) == 2
    assert res["验收区间"]["a1"] == [0.12, 0.18]
    assert res["验收区间"]["capped"] == [0.15, 0.25]
