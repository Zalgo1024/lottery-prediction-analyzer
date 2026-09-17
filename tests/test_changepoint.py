"""
P2-2 变点检测测试（credibility/changepoint.py）

诚实性校准：
- 两段分布突变合成序列 → 单变点扫描必须检出（检测能力为正）。
- iid 均匀序列 → 用 Cramér V 效应量门控压制"扫描最优点"的乐观偏置，
  不得批量误报"实质变点"（允许 ≈α 抖动）。
- 真数据锚点：七星彩第 7 位 2020-10-11 官方规则变更(0-9→0-14)必须被检出。
- 写盘路径 discovery/changepoints.json 结构正确（无检出也须落 key）。
"""
import json

import numpy as np
import pytest

import credibility.changepoint as cp
from credibility.changepoint import (_best_split, _cramers_v,
                                     _split_p, change_point_scan)


def _iid_seq(n=3000, size=10, seed=0):
    return np.random.default_rng(seed).integers(0, size, n)


def _two_seg_seq(n=3000, cut=1500, size=10, seed=0):
    """前半取值 0..4、后半取值 5..9 → 分布突变在 cut 附近"""
    rng = np.random.default_rng(seed)
    a = rng.integers(0, size // 2, cut)
    b = rng.integers(size // 2, size, n - cut)
    return np.concatenate([a, b])


def _scan_sig(vals, size, B=150, seed=0, min_seg=200, v_gate=0.1):
    """内部函数拼装的单分区扫描：返回切点/χ²/p/V/实质结论"""
    c, chi2 = _best_split(vals, size, min_seg)
    p = _split_p(vals, c, size, B=B, seed=seed)
    V = _cramers_v(chi2, len(vals), size)
    return {"c": c, "chi2": chi2, "p": p, "V": V,
            "实质": bool(p < 0.05 and V >= v_gate)}


class TestCalibration:
    def test_two_segment_is_detected(self):
        """检测能力：分布突变的合成序列必须被检出，切点误差有界"""
        vals = _two_seg_seq()
        r = _scan_sig(vals, 10, seed=1)
        assert abs(r["c"] - 1500) <= 200, f"切点偏差过大: {r['c']}"
        assert r["V"] >= 0.2, f"效应量过低: {r['V']}"
        assert r["p"] < 0.05
        assert r["实质"]

    def test_iid_no_batch_false_positive(self):
        """诚实护栏：公平随机序列不得批量误报实质变点（V 门控压扫描乐观偏置）"""
        sigs = 0
        for s in range(10):
            r = _scan_sig(_iid_seq(seed=s), 10, B=100, seed=10 + s)
            if r["实质"]:
                sigs += 1
                assert r["V"] >= 0.1  # 即使误报也须先过门控（自洽）
        assert sigs <= 2, f"10 个 iid 序列 {sigs} 个假阳（应≈0-1）"

    def test_iid_large_chi2_alone_not_significant(self):
        """扫描最优点的 χ² 再大，若 Cramér V 不过门控也不算实质变点"""
        vals = _iid_seq(seed=5)
        c, chi2 = _best_split(vals, 10, 200)
        V = _cramers_v(chi2, len(vals), 10)
        assert V < 0.1  # 固定 seed 下 iid 的 max-χ² 效应量必在门控之下


class TestRealData:
    def test_qxc_7th_detects_2020_rule_change(self):
        """已知规则变更锚点：七星彩第 7 位 2020-10 官方 0-9→0-14"""
        rep = change_point_scan("七星彩", B=100, min_seg=200, write_json=False)
        z7 = next(r for r in rep["全部扫描"] if r.get("值域") == "0-14")
        assert z7["实质变点"], f"第7位官方规则变更未被检出: {z7}"
        assert z7["CramérV"] >= 0.3
        assert str(z7["变点日期"]).startswith("2020"), z7["变点日期"]

    def test_3d_rows_self_consistent(self):
        """排列3 无已知规则变更 → 任何检出都须自洽（V≥门控 且 q<0.05）"""
        rep = change_point_scan("排列3", B=100, min_seg=200, write_json=False)
        assert rep["彩种"] == "排列3"
        for r in rep["全部扫描"]:
            assert 0.0 <= r.get("p", 0.0) <= 1.0
            if r.get("实质变点"):
                assert r["CramérV"] >= 0.1 and r["q(BH)"] < 0.05

    def test_write_json_path(self, tmp_path, monkeypatch):
        """写盘路径：按彩种落 key，检出/无检出结构一致"""
        monkeypatch.setattr(cp, "OUT_JSON", tmp_path / "changepoints.json")
        change_point_scan("双色球", B=60, min_seg=200, write_json=True)
        p = tmp_path / "changepoints.json"
        assert p.exists()
        payload = json.loads(p.read_text(encoding="utf-8"))
        assert "双色球" in payload
        entry = payload["双色球"]
        assert "检出" in entry and "note" in entry and "updated_at" in entry
        assert isinstance(entry["检出"], list)
