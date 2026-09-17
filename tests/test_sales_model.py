"""
P1-2 销量/奖池/注数预测模型测试（ev/sales_model.py）

核心诚实性护栏：把目标列换成**纯随机**值后，模型在留出期不得出现
显著正 R² / 显著优于基线的 MAE 改进——否则就是特征里藏了泄漏。
"""
import numpy as np
import pandas as pd
import pytest

from ev import sales_model as sm


# ---------------------------------------------------------------- 零穿越
class TestFrameLag:
    def test_lags_are_strictly_past(self):
        """滞后特征必须等于上一期的实际值（不允许当期/未来信息进入特征）"""
        f = sm.build_frame("大乐透")
        for k in (5, 100, 500, len(f) - 3):
            assert f["pool_lag1"].iloc[k] == pytest.approx(f["pool_t"].iloc[k - 1])
            assert f["sales_lag1"].iloc[k] == pytest.approx(f["sales_t"].iloc[k - 1])
            assert f["n1_lag1"].iloc[k] == pytest.approx(f["n1_t"].iloc[k - 1])

    def test_frame_is_ascending(self):
        f = sm.build_frame("大乐透")
        assert f["date"].is_monotonic_increasing

    def test_ssq_sales_now_complete(self):
        """双色球 总投注额 2026-09-04 全史回填 100% → 升序帧中 sales_t 应几乎全有值"""
        f = sm.build_frame("双色球")
        assert f["sales_t"].notna().mean() > 0.99


# ---------------------------------------------------------------- 不自欺护栏（合成随机）
def _random_target_frame(rng: np.random.Generator) -> pd.DataFrame:
    """真实特征结构 + 随机目标（目标与特征脱钩）"""
    f = sm.build_frame("大乐透")
    f["sales_t"] = rng.lognormal(mean=19.0, sigma=0.3, size=len(f))
    f["pool_t"] = rng.lognormal(mean=20.0, sigma=0.4, size=len(f))
    f["n1_t"] = rng.poisson(lam=5.0, size=len(f)).astype(float)
    return f


class TestNoSelfDeception:
    def test_sales_model_no_edge_on_random_target(self):
        rng = np.random.default_rng(2026)
        f = _random_target_frame(rng)
        r = sm._fit_continuous(f, "sales_t", sm._SALES_FEATS, test_frac=0.25)
        # 真实大乐透 R²(log)≈0.997；随机目标必须远低于此（允许轻微过拟合噪声，设上限 0.3）
        assert r["R²(log尺度, vs均值)"] < 0.3, f"随机目标上出现虚假高 R²: {r}"

    def test_pool_model_no_edge_on_random_target(self):
        rng = np.random.default_rng(7)
        f = _random_target_frame(rng)
        r = sm._fit_continuous(f, "pool_t", sm._LAG_FEATS, test_frac=0.25)
        assert r["R²(log尺度, vs均值)"] < 0.3, f"随机奖池目标上出现虚假高 R²: {r}"

    def test_n1_no_edge_on_random_target(self):
        rng = np.random.default_rng(11)
        f = _random_target_frame(rng)
        glm = sm._fit_poisson_glm(f, sm._LAG_FEATS, test_frac=0.25)
        lgb = sm._fit_n1_lgb(f, sm._LAG_FEATS, test_frac=0.25)
        # 随机目标：校准应≈1、MAE 改进≈0（允许 ±0.15 抖动）
        assert abs(glm["校准比(λ̄/ȳ)"] - 1.0) < 0.2
        assert glm["MAE改进(相对齐性基线)"] < 0.15
        assert lgb["MAE改进(相对齐性基线)"] < 0.15


# ---------------------------------------------------------------- 真数据冒烟
class TestRealDataSmoke:
    def test_dlt_sales_has_signal(self):
        """验收①：销量模型留出期必须有真信号（远高于随机目标的表现）"""
        f = sm.build_frame("大乐透")
        r = sm._fit_continuous(f, "sales_t", sm._SALES_FEATS, test_frac=0.2)
        assert r["R²(log尺度, vs均值)"] > 0.9, f"大乐透销量 R² 过低: {r}"

    def test_forecast_shape_dlt(self):
        fc = sm.forecast_next("大乐透")
        assert fc["预测下一期"]["奖池(预测)"] > 0
        assert fc["预测下一期"]["销量(预测)"] > 0
        ev = fc["前瞻EV"]
        assert "参与信号(前瞻)" in ev and isinstance(ev["参与信号(前瞻)"], bool)
        assert "EV上限(头奖顶格)" in ev
        # 经济一致性：EV 必须在 [EV地板, EV上限] 之间
        assert ev["EV地板(池=0)"] <= ev["总EV(每注)"] <= ev["EV上限(头奖顶格)"]

    def test_forecast_shape_ssq(self):
        """双色球销量已补全 → 销量预测给数值（非降级占位）且 EV 在 [地板, 上限]"""
        fc = sm.forecast_next("双色球")
        assert fc["预测下一期"]["奖池(预测)"] > 0
        assert isinstance(fc["预测下一期"]["销量(预测)"], (int, float))
        assert fc["预测下一期"]["销量(预测)"] > 0
        ev = fc["前瞻EV"]
        assert ev["EV地板(池=0)"] <= ev["总EV(每注)"] <= ev["EV上限(头奖顶格)"]

    def test_signal_replay_dlt_runs(self):
        """验收②：前瞻回放可跑通且给出结构（真数据小留出）"""
        sc = sm.signal_consistency("大乐透", test_frac=0.1)
        assert sc["回放期数"] >= 50
        assert "①顶格判定一致率(预测EV̂ vs 实际)" in sc
        assert 0.0 <= sc["①顶格判定一致率(预测EV̂ vs 实际)"] <= 1.0

    def test_ssq_signal_replay_enabled(self):
        """双色球销量已补全 → EV 层回放启用：顶格一致率可算且在 [0,1]"""
        sc = sm.signal_consistency("双色球", test_frac=0.1)
        assert sc["回放期数"] >= 50
        assert "①顶格判定一致率(预测EV̂ vs 实际)" in sc
        assert 0.0 <= sc["①顶格判定一致率(预测EV̂ vs 实际)"] <= 1.0
        # 顶格时代 EV 无方差 → ②Spearman 允许 None（诚实）；不得是 nan
        ev_sp = sc.get("②预测EV̂ vs 实际EV Spearman")
        assert ev_sp is None or (isinstance(ev_sp, float) and not np.isnan(ev_sp))
