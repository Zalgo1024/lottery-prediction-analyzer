"""
P2-4 传递熵/信息量化测试（discovery/info_theory.py）

诚实性校准：
- iid 均匀随机序列 → 任何 MI/CMI 都不应通过 BH 显著（固定 seed 可复现）。
- 一阶 AR 依赖合成序列 → MI1 必须被检出（检测能力为正）。
- 真数据冒烟：lottery_report 结构完整、七星彩第 7 位用现行段口径。
"""
import numpy as np
import pytest

from discovery.info_theory import analyze_seq, lottery_report, _QXC_CUTOFF


def _iid_seq(n=4000, size=10, seed=0):
    return np.random.default_rng(seed).integers(0, size, n)


def _ar1_seq(n=4000, size=10, p=0.6, seed=1):
    rng = np.random.default_rng(seed)
    out = np.zeros(n, dtype=int)
    for t in range(1, n):
        out[t] = (out[t - 1] + 1) % size if rng.random() < p else int(rng.integers(0, size))
    return out


class TestCalibration:
    def test_iid_no_false_positive(self):
        """诚实护栏：公平随机序列不得检出时序依赖"""
        r = analyze_seq(_iid_seq(), 10, kmax=5, B=150, seed=3)
        sig = [t for t in r["检验"] if t["显著"]]
        assert sig == [], f"iid 序列被误判显著: {sig}"

    def test_ar1_is_detected(self):
        """检测能力：一阶自相关序列必须在 MI1 上显著"""
        r = analyze_seq(_ar1_seq(), 10, kmax=5, B=150, seed=4)
        mi1 = r["检验"][0]
        assert mi1["显著"], f"AR(1) 的 MI1 未被检出: {mi1}"
        assert mi1["q(BH)"] < 0.05

    def test_two_iid_batches_mostly_clean(self):
        """多重保险：多个独立 iid 序列中，显著率应≈α（允许 1/10 抖动）"""
        sigs = 0
        for s in range(10):
            r = analyze_seq(_iid_seq(seed=s), 10, kmax=3, B=100, seed=10 + s)
            sigs += int(any(t["显著"] for t in r["检验"]))
        assert sigs <= 2, f"10 个 iid 序列中 {sigs} 个假阳（应≈1）"


class TestRealData:
    def test_lottery_report_structure(self):
        rep = lottery_report("排列3", kmax=3, B=100)
        assert rep["彩种"] == "排列3"
        assert len(rep["分区"]) == 3
        for z in rep["分区"]:
            assert len(z["检验"]) == 3  # MI1 + CMI lag2,3
            for t in z["检验"]:
                assert 0.0 <= t["q(BH)"] <= 1.0
        assert "结论" in rep

    def test_qxc_7th_uses_modern_cutoff(self):
        """七星彩第 7 位（0-14）必须只用 2020-10-11 起现行段，避免新旧规则混合"""
        rep = lottery_report("七星彩", kmax=3, B=100)
        z7 = [z for z in rep["分区"] if z["值域"] == "0-14"]
        assert len(z7) == 1
        # 现代段约 2020-10 起 ~每年 150 期 → 约 800-1000 期，不应等于全量 3385
        assert z7[0]["期数(用)"] < 2000
        assert _QXC_CUTOFF.year == 2020

    def test_ssq_blue_report_runs(self):
        rep = lottery_report("双色球", kmax=3, B=100)
        assert len(rep["分区"]) == 2
