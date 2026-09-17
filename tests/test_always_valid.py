"""
P2-3 e-value 序贯检验测试（discovery/always_valid.py）

诚实性校准（e-process 理论性质）：
- 零假设下财富过程是非负鞅：E[W_n]=1（均值 ≈1、全程非负、标量=矩阵一致）。
- iid 公平数据按真实 22 分区结构模拟：族级任意停时误报率应 ≤ 2α（实测 0）。
- 有偏数据检测能力诚实口径：δ=0.06 在 3000 期可靠检出（≥90%）；
  δ=0.03 检不出（<50%）——任意停时检验对小偏差需要大样本，不许吹牛。

真数据锚点（回归护栏，数据固定故结果确定）：
- 6 彩种 22 分区族级任意停时均无告警（e-max < T=440）。
- 大乐透红球诊断 e-max ≈112（高于其他分区 ≈1-3、低于告警线）——
  与 P2-2 该区 2013 变点遥相呼应但未误报（e-value 自首期累计，段内漂移部分抵消）。
- 七星彩第 7 位只用 2020-10-11 起现行段（0-14，~891 期），不与全史混用。
"""
import numpy as np
import pytest

from discovery.always_valid import (
    _QXC_CUTOFF, _REAL_ZONES, _combine_wealth, _first_cross, _wealth_matrix,
    calibrate_bias, calibrate_null, e_process, monitor_lottery, run_all,
)

THRESHOLD = len(_REAL_ZONES) / 0.05  # 440


def _fair_zone(n=3000, K=10, seed=0):
    rng = np.random.default_rng(seed)
    return (rng.random((n, K)) < 1.0 / K).astype(float)


class TestEValueTheory:
    def test_nonnegative_and_scalar_equals_matrix(self):
        x = _fair_zone(2000, 10, seed=1)
        Wk = _wealth_matrix(x, 0.1)
        assert (Wk >= 0).all()
        col = e_process(x[:, 3], 0.1)
        assert np.allclose(col, Wk[:, 3])

    def test_martingale_mean_is_one_under_null(self):
        """零假设下 E[W_n] = 1：跨多 seed 平均应在 1 附近（非负鞅性质）"""
        means = []
        for s in range(30):
            x = _fair_zone(2000, 10, seed=s)
            Wk = _wealth_matrix(x, 0.1)
            means.append(float(Wk[-1].mean()))   # 该 seed 的 10 类别均值
        avg = float(np.mean(means))
        assert 0.4 <= avg <= 2.5, f"零假设财富均值应≈1，实测 {avg}"

    def test_first_cross_none_on_fair_data(self):
        x = _fair_zone(1500, 10, seed=2)
        zone = _wealth_matrix(x, 0.1).mean(axis=1)
        assert _first_cross(zone, THRESHOLD) is None

    def test_combine_handles_unequal_length(self):
        """七星彩第 7 位段较短 → 短曲线按末值平走合并"""
        a = np.array([1.0, 2.0, 3.0])
        b = np.array([1.0, 1.5])
        c = _combine_wealth([a, b])
        assert len(c) == 3
        assert np.allclose(c, [1.0, 1.75, 2.25])


class TestCalibration:
    def test_null_fleet_no_false_alarm(self):
        """iid 公平：真实 22 分区结构族级任意停时误报率 ≤ 2α（理论 ≤ α）"""
        r = calibrate_null(n_draws=1500, seeds=20)
        assert r["族级误报"] <= max(1, int(20 * 0.05 * 2)), r["判定"]

    def test_bias_006_reliably_detected(self):
        """检测能力：δ=0.06（单类别，K=10）3000 期内检出率 ≥ 90%"""
        r = calibrate_bias(delta=0.06, n_draws=3000, seeds=20)
        assert r["检出率"] >= 0.9, r["判定"]

    def test_bias_003_honest_undetectable_at_3000(self):
        """诚实护栏：δ=0.03 在 3000 期检不出（不许夸大任意停时检验的能力）"""
        r = calibrate_bias(delta=0.03, n_draws=3000, seeds=20)
        assert r["检出率"] <= 0.5, r["判定"]


class TestRealData:
    def test_all_zones_clean_no_alarm(self):
        """真数据回归锚点：6 彩种 22 分区族级任意停时无告警"""
        rep = run_all()
        assert rep["越阈分区"] == [], rep["族结论"]
        assert rep["监控分区数M"] == 22

    def test_dlt_red_elevated_diagnostic_below_alarm(self):
        """大乐透红球诊断 e-max ≈112：高于正常区(≈1-3)但低于告警线 440"""
        dlt = next(r for r in run_all()["彩种"] if r["彩种"] == "大乐透")
        red = next(z for z in dlt["分区"] if z["分区"] == "红球")
        assert 10.0 < red["组合e-max"] < THRESHOLD

    def test_qxc_7th_uses_modern_cutoff(self):
        """七星彩第 7 位（0-14）只用 2020-10-11 起现行段"""
        qxc = monitor_lottery("七星彩")
        z7 = next(z for z in qxc["分区"] if z["值域"] == "0-14")
        assert 700 <= z7["期数(用)"] <= 1500, z7["期数(用)"]
        assert z7["首次越阈(期索引)"] is None
        assert _QXC_CUTOFF.year == 2020

    def test_deterministic_repeat(self):
        """固定数据下监控结果确定（调度器重复运行可复现）"""
        a = monitor_lottery("双色球")
        b = monitor_lottery("双色球")
        za = next(z for z in a["分区"] if z["分区"] == "红球")
        zb = next(z for z in b["分区"] if z["分区"] == "红球")
        assert za["组合e-max"] == zb["组合e-max"] == 2.0701

    def test_hypothesis_coverage_25(self):
        """25 条登记假设全部有 e-value 覆盖说明"""
        names = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]
        total = sum(len(monitor_lottery(nm)["覆盖假设"]) for nm in names)
        assert total == 25
