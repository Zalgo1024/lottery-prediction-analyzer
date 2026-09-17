"""
P2-1 随机性审计测试（credibility/randomness_audit.py）

诚实性校准（必须先在合成数据上证明不自欺，才可信赖真数据结论）：
- iid 均匀流：审计不得误报（多流独立验证，效应门 + p 双条件）。
- 强边际偏差（单类 p=0.25）：T1 频率必须检出。
- 粘滞序列（相邻同号率 0.3）：T3/T4 等时序检验必须检出。

真数据锚点 + 修复回归：
- 6 彩种全部通过审计（『数据随机性良好』权威背书，B=150 固定 seed 确定）。
- P2-1 定位用例：data_quality 曾对七星彩第 7 位报"相邻自相关 p≈1e-10 实质异常"
  ——实为规则段混用伪影（前段 0-9 基准率 1/10 被拿去比后段期望 1/15）。
  本测试锁定：现行段（2020-10-11 起，0-14）真实同号率 0.074≈1/15 干净；
  全史混用才会造出 z≈6.4 的假告警（已修 data_quality.py 统一按现行段过滤）。
- data_quality 游程检验此前被 scipy 缺失静默吞掉（从未生效）→ 现手写
  Wald-Wolfowitz，七星彩应出现 7 行游程、且 pos7 无实质异常。
"""
import math

import numpy as np
import pytest
from scipy import stats

from credibility.data_quality import data_quality_report
from credibility.randomness_audit import (
    _base_records, audit_lottery, audit_zone, run_all,
)
from data.loader import load_lottery
from data.schema import get_schema, record_zone_numbers


# ---------------- 合成夹具（数字型位 0-9） ----------------
class _FakeRec:
    def __init__(self, vals, num):
        self._z = list(vals)
        self.期号 = num

    @property
    def zone_numbers(self):
        return {"第1位": list(self._z)}

    @property
    def 开奖日期(self):
        return None


_Z1 = get_schema("排列3").zones[0]   # 第1位 0-9 ordered


def _mk(vals):
    return [_FakeRec([int(v)], i) for i, v in enumerate(vals)]


def _zone_hits(vals, B=100):
    """单分区审计 → (p, 效应门) 行列表。"""
    out = audit_zone(_Z1, _mk(vals), B=B, seed=0)
    return [(t["p值"], t.get("效应门", False)) for t in out["检验"]]


def _bh_q(pvals):
    """与生产同构的 BH 校正（复刻，避免跨包 import 测试细节）。"""
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(1, n + 1))
    qs = np.empty(n)
    qs[order] = np.minimum.accumulate(ranked[::-1])[::-1]
    return np.minimum(qs, 1.0)


class TestCalibration:
    def test_iid_never_flags(self):
        """诚实护栏：iid 均匀流不误报——与生产同构：12 流整族 BH 后零实质显著。

        （单行 p 偶然 <0.05 属名义水平内波动，生产靠 BH 跨 ~100 行校正，
        此处必须模拟同一多重比较口径，否则会拿"单测水平"误判校准。）
        """
        rows = []
        for sd in range(12):
            rng = np.random.default_rng(sd)
            vals = rng.integers(0, 10, 3000)
            rows.extend(_zone_hits(vals))
        ps = np.array([r[0] for r in rows])
        gates = np.array([r[1] for r in rows])
        qs = _bh_q(ps)
        flagged = [(i, ps[i], qs[i]) for i in range(len(rows))
                   if gates[i] and qs[i] <= 0.05]
        assert flagged == [], f"iid 族整族 BH 后仍有实质显著: {flagged}"

    def test_strong_marginal_bias_detected(self):
        """检测能力：单类概率 0.25（vs 0.1）→ T1 频率必须检出"""
        rng = np.random.default_rng(1)
        vals = np.where(rng.random(3000) < 0.25, 5, rng.integers(0, 10, 3000))
        out = audit_zone(_Z1, _mk(vals), B=100, seed=0)
        hits = {t["检验"] for t in out["检验"]
                if t["p值"] < 0.05 and t.get("效应门")}
        assert any(h.startswith("T1频率") for h in hits), f"强偏差未检出: {hits}"

    def test_stickiness_detected(self):
        """检测能力：相邻同号率 0.3 → 时序类（T2/T3/T4）应检出"""
        rng = np.random.default_rng(2)
        vals = [int(rng.integers(0, 10))]
        for _ in range(2999):
            vals.append(vals[-1] if rng.random() < 0.3
                        else int(rng.integers(0, 10)))
        out = audit_zone(_Z1, _mk(vals), B=100, seed=0)
        hits = {t["检验"] for t in out["检验"]
                if t["p值"] < 0.05 and t.get("效应门")}
        assert any(h.startswith(("T2", "T3", "T4")) for h in hits), hits


class TestRealData:
    def test_six_lotteries_all_clean(self):
        """真数据权威背书：6 彩种全部分区通过审计（B=150 固定 seed 确定）"""
        reps = run_all(B=150)
        for rep in reps:
            flagged = [t for z in rep["分区"] for t in z.get("检验", [])
                       if t.get("实质显著")]
            assert flagged == [], f"{rep['彩种']} 检出实质偏离: {flagged}"
            assert "通过" in rep["结论"], rep["结论"]

    def test_qxc_pos7_modern_segment_clean(self):
        """定位用例：七星彩第 7 位现行段（2020-10-11 起）审计干净"""
        rep = audit_lottery("七星彩", B=150)
        z7 = next(z for z in rep["分区"] if z["值域"] == "0-14")
        assert 700 <= z7["期数(用)"] <= 1500
        for t in z7["检验"]:
            assert not t.get("实质显著"), t

    def test_mixed_segment_would_false_flag(self):
        """定位用例：全史混用才会造出假异常（z≈6.4），现行段干净（z≈0.9）"""
        data = load_lottery("七星彩")
        chron = sorted(data.records, key=lambda r: r.期号 or 0)
        z7 = [z for z in get_schema("七星彩").zones if z.max == 14][0]
        full = [list(record_zone_numbers(r, z7))[0] for r in chron]
        modern = [list(record_zone_numbers(r, z7))[0]
                  for r in _base_records(chron, z7)]

        def z_same(vals, expect):
            n = len(vals) - 1
            same = sum(1 for a, b in zip(vals, vals[1:]) if a == b)
            se = math.sqrt(expect * (1 - expect) / n)
            return (same / n - expect) / se

        assert z_same(full, 1 / 15) > 3.0    # 混用 → 假告警（旧口径 bug）
        assert abs(z_same(modern, 1 / 15)) < 3.0  # 现行段干净
        assert len(modern) < 2000

    def test_data_quality_fix_and_runs_rows(self):
        """修复回归：data_quality 的七星彩 pos7 无假异常 + 游程行已生效"""
        r = data_quality_report("七星彩")
        runs_rows = [t for t in r["tests"] if t["检验"] == "游程检验"]
        assert len(runs_rows) == 7, "游程检验行缺失（scipy 静默吞掉的老问题）"
        p7 = [t for t in r["tests"] if t["分区"] == "第7位"]
        auto = next(t for t in p7 if t["检验"] == "相邻自相关")
        assert auto["p值"] > 0.05 and not auto["实质显著"], auto
        assert r["n_rejected"] == 0, r["tests"]
