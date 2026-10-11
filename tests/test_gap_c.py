# -*- coding: utf-8 -*-
"""C 轨（位级 / 分区结构偏差）测试。

覆盖：
  ① MC 零分布的正确性与校准（拒绝率 ≈ α；multinomial 与逐观测计数同分布）
  ② 位级 χ²：植入倾斜必须检出；均匀数据不得检出
  ③ 红球形态：形态特征与暴力实现一致；均匀 MC 组合的 null 均值 = 解析期望
  ④ **分段同质闸门**：人造分段断裂必须判「分段不一致」而**不是**「实质偏离」
  ⑤ 七星彩第 7 位只用现行段（与 A′ 轨整段切的口径**不同**，不能混）
  ⑥ `gap` 恒为 False（C 轨绝不单独判缝隙）
  ⑦ 位级 MC p 的分辨率地板：披露 `1/(B+1)` 并保证低于 BH 最严临界值
  ⑧ 红球形态 z 的 MC 精度闸门：MC 组合数随 n 缩放（≥400×n），噪声 ≤0.05
"""
from __future__ import annotations

import types
from datetime import date, timedelta

import numpy as np
import pytest

from ev.mechanism import gap_c_structural as GC
from ev.mechanism import sales_grade as SG
from ev.mechanism.common import DrawFrame


# ------------------------------------------------------------------ 合成工具
def _fake_records(lottery: str, n: int, *, digit: int = 0, tilt: float = 0.0,
                  start: date = date(2004, 11, 14), spacing_days: int = 2,
                  zone7_break: bool = False, seed: int = 5):
    """
    造 n 条假开奖记录。`tilt` 让 `digit` 位偏向大数字（0 = 完全均匀）。
    zone7_break=True：只能配 七星彩，cutoff 前该位取 0-9（旧规则），之后取 0-14。
    """
    rng = np.random.default_rng(seed)
    zones = (["第1位", "第2位", "第3位"] if lottery in ("排列3", "福彩3D")
             else ["第1位", "第2位", "第3位", "第4位", "第5位"]
             if lottery == "排列5"
             else ["第1位", "第2位", "第3位", "第4位", "第5位", "第6位", "第7位"])
    recs = []
    for i in range(n):
        d = start + timedelta(days=spacing_days * i)
        zs = {}
        for j, zn in enumerate(zones):
            if zn == "第7位":
                if zone7_break:
                    # 旧规则段：只有 0-9；现行段：0-14
                    hi = 9 if d < SG.QXC_RULE_CUTOFF else 14
                elif lottery == "七星彩":
                    hi = 14 if d >= SG.QXC_RULE_CUTOFF else 9
                else:
                    hi = 9
                zs[zn] = [int(rng.integers(0, hi + 1))]
            else:
                if j == digit and tilt > 0:
                    # 偏离均匀：以 (1-tilt) 概率均匀、tilt 概率偏向 6-9
                    if rng.random() < tilt:
                        zs[zn] = [int(rng.integers(6, 10))]
                    else:
                        zs[zn] = [int(rng.integers(0, 10))]
                else:
                    zs[zn] = [int(rng.integers(0, 10))]
        recs.append(types.SimpleNamespace(期号=i + 1, 开奖日期=d, zone_numbers=zs))
    return recs


def _patch_loader(monkeypatch, recs):
    monkeypatch.setattr("data.loader.load_lottery",
                        lambda lot: types.SimpleNamespace(records=recs))


def _reds_from_support(lo, hi, k, n, *, seed, weights=None):
    """从 [lo, hi] 里均匀（或按 weights）抽 n 个有序 k 元组合（已升序）。"""
    rng = np.random.default_rng(seed)
    span = hi - lo + 1
    p = None if weights is None else np.asarray(weights, dtype=float) / np.sum(weights)
    out = np.empty((n, k), dtype=np.int16)
    for i in range(n):
        out[i] = np.sort(rng.choice(np.arange(lo, hi + 1), size=k,
                                    replace=False, p=p))
    return out


def _frame(reds, lottery="双色球"):
    n = len(reds)
    return DrawFrame(lottery=lottery, issues=[str(i) for i in range(n)],
                     dates=[date(2010, 1, 1) + timedelta(days=i) for i in range(n)],
                     reds=np.asarray(reds, dtype=np.int16),
                     prev=np.asarray(reds, dtype=np.int16),
                     y1=np.zeros(n), y2=np.zeros(n), sales=np.ones(n) * 1e8,
                     dropped=0)


# ------------------------------------------------------------------ ① MC 零分布
def test_mc_chi2_rejection_rate_is_near_alpha():
    """
    MC 校准的意义：在**真均匀**数据上，p 必须近似 U(0,1) ⇒ α=0.05 的拒绝率 ≈ 5%。
    若零分布写错（例如忘了归一化到 0..size-1、或用渐近 χ²），这里会立刻爆。
    """
    size, total, B, reps = 10, 7000, 999, 200
    rng = np.random.default_rng(20261003)
    pvals = []
    for _ in range(reps):
        counts = rng.multinomial(total, np.full(size, 1 / size))
        exp = total / size
        obs = float(((counts - exp) ** 2 / exp).sum())
        p, _ = GC._mc_chi2_p(obs, total, size, B=B, seed=int(rng.integers(1, 10 ** 6)))
        pvals.append(p)
    pv = np.asarray(pvals)
    assert abs(float((pv <= 0.05).mean()) - 0.05) < 0.04, pv[pv <= 0.05].size
    assert 0.35 < float(pv.mean()) < 0.65


def test_mc_chi2_is_monotone_and_bounded():
    """观测 χ² 越大 → p 越小；p ∈ (0,1)。"""
    ps = [GC._mc_chi2_p(v, 5000, 10, B=499, seed=1)[0]
          for v in (5.0, 20.0, 50.0, 200.0)]
    assert all(0 < p < 1 for p in ps)
    assert ps == sorted(ps, reverse=True)


def test_zone_counts_normalizes_by_zone_min():
    """不归一化会让最大号被截断、并造出空 bin 0 的大 χ²（data_quality.py 已踩过）。"""
    vals = np.array([1, 2, 3, 1, 3, 3])
    c = GC._zone_counts(vals, size=3, lo=1)
    assert c.tolist() == [2, 1, 3]


# ------------------------------------------------------------------ ② 位级
def test_planted_digit_tilt_is_detected(monkeypatch):
    recs = _fake_records("排列3", 6000, digit=0, tilt=0.45)
    _patch_loader(monkeypatch, recs)
    r = GC.structural_bias_report("排列3", B=800, mc=1000)
    t0 = [t for t in r["检验"] if t["特征"] == "第1位"][0]
    assert t0["统计显著"] is True
    assert t0["实质显著"] is True, t0
    assert t0["效应量"] > GC.V_MIN
    assert "实质结构偏离" in r["结论"]
    assert r["gap"] is False


def test_uniform_digits_are_not_flagged(monkeypatch):
    recs = _fake_records("排列3", 6000, tilt=0.0, seed=99)
    _patch_loader(monkeypatch, recs)
    r = GC.structural_bias_report("排列3", B=800, mc=1000)
    assert r["实质偏离"] == []
    assert all(not t["实质显著"] for t in r["检验"])


def test_lotto_red_zone_is_skipped_in_favor_of_shape_family(monkeypatch):
    """红球是无序多选 → 边际频率意义弱，必须跳过并说明走形态族。"""
    recs = _fake_records("排列3", 500)
    _patch_loader(monkeypatch, recs)
    r = GC.structural_bias_report("排列3", B=400, mc=500)
    assert all(t["族"] == "排列3|位级" for t in r["检验"])
    assert r["族"]["排列3|位级"]["m"] == 3


# ------------------------------------------------------------------ ③ 形态
def test_red_features_match_brute_force():
    """特征必须与逐行手算一致（列序见 RED_FEATURES）。"""
    combos = np.array([[1, 2, 3, 4, 5, 6],
                       [1, 2, 3, 4, 5, 33],
                       [10, 11, 12, 13, 14, 20]])
    f = GC._red_features(combos, 1, 33, 6)
    cols = {name: f[:, j].tolist() for j, name in enumerate(GC.RED_FEATURES)}
    assert cols["和值"] == [21, 48, 80]
    assert cols["跨度"] == [5, 32, 10]
    # 连号对数：[1..6]=5、[1..5,33]=4、[10..14,20]=4
    assert cols["连号对数"] == [5, 4, 4]
    # 奇数个数：{1,3,5}=3、{1,3,5,33}=4、{11,13}=2
    assert cols["奇数个数"] == [3, 4, 2]
    # 大号个数（>17）：0、{33}=1、{20}=1
    assert cols["大号个数"] == [0, 1, 1]
    # 尾数重复：无、{3}重=1、{0}重=1
    assert cols["尾数重复"] == [0, 1, 1]
    # 空三分区数（1-11 / 12-22 / 23-33）：只占 0 区=2、占 0+2 区=1、占 0+1 区=1
    assert cols["空三分区数"] == [2, 1, 1]
    # 暴力交叉核对
    for row, vals in zip(combos, f):
        s = sorted(int(v) for v in row)
        assert vals[0] == sum(s)
        assert vals[1] == s[-1] - s[0]
        assert vals[2] == sum(1 for a, b in zip(s, s[1:]) if b - a == 1)
        assert vals[3] == sum(1 for v in s if v % 2 == 1)
        assert vals[4] == sum(1 for v in s if v > 17)
        assert vals[5] == 6 - len({v % 10 for v in s})
        th = {min(2, (v - 1) // 11) for v in s}
        assert vals[6] == 3 - len(th)


def test_mc_uniform_combos_mean_matches_analytic():
    """MC 组合的 null 均值必须等于解析期望（否则所有 z 都偏）。"""
    c = GC._mc_uniform_combos(1, 35, 5, mc=60_000, seed=3)
    f = GC._red_features(c, 1, 35, 5)
    assert float(f[:, 0].mean()) == pytest.approx(5 * 18.0, abs=0.3)
    assert float(f[:, 4].mean()) == pytest.approx(5 * 17 / 35, abs=0.03)
    assert c.min() == 1 and c.max() == 35
    assert all(len(set(row.tolist())) == 5 for row in c[:200])


def test_uniform_reds_have_no_practical_deviation(monkeypatch):
    reds = _reds_from_support(1, 33, 6, 1500, seed=42)
    monkeypatch.setattr(GC, "load_draw_frame", lambda lot, **k: _frame(reds))
    r = GC.structural_bias_report("双色球", B=300, mc=40_000)
    assert r["实质偏离"] == []
    assert r["分段不一致"] == []


# ------------------------------------------------------------------ ④ 分段闸门
def test_segment_break_is_classified_as_inhomogeneous_not_practical(monkeypatch):
    """
    前 800 期从 1..33 均匀抽；后 800 期只从 24..33 抽（和值大幅上移）。
    全样本 z 会很大，但**分段同质闸门**必须把它判成「数据/规则分段问题」，
    绝不能报成「实质结构偏离」—— 这正是大乐透 2007–2013 段实测踩到的坑。
    """
    a = _reds_from_support(1, 33, 6, 800, seed=1)
    b = _reds_from_support(24, 33, 6, 800, seed=2)
    monkeypatch.setattr(GC, "load_draw_frame",
                        lambda lot, **k: _frame(np.vstack([a, b])))
    r = GC.structural_bias_report("双色球", B=300, mc=40_000)
    feats = {t["特征"]: t for t in r["检验"]}
    assert feats["和值"]["统计显著"] is True
    assert feats["和值"]["分段"]["同质"] is False
    assert abs(feats["和值"]["分段"]["分段差_z"]) > 3
    assert feats["和值"]["实质显著"] is False
    assert feats["和值"]["分段不一致"] is True
    assert r["实质偏离"] == []
    assert any(x["特征"] == "和值" for x in r["分段不一致"])
    assert "分段" in r["结论"]


def test_real_daloto_segment_break_is_caught():
    """真数据回归：大乐透 2007–2013 段的和值断裂必须被判成「分段不一致」。"""
    r = GC.structural_bias_report("大乐透", B=1000, mc=60_000)
    names = {x["特征"] for x in r["分段不一致"]}
    assert "和值" in names, r["分段不一致"]
    assert r["实质偏离"] == [], r["实质偏离"]


def test_real_lotto_smoke_all_six():
    """6 彩种全跑一遍不报错，且 `gap` 恒 False。"""
    for lot in ("双色球", "大乐透", "排列3", "排列5", "福彩3D", "七星彩"):
        r = GC.structural_bias_report(lot, B=400, mc=20_000)
        assert r["applicable"] is True, lot
        assert r["gap"] is False
        assert r["n检验"] >= 3
        assert all(t["族"].startswith(lot + "|") for t in r["检验"])
        assert "q(FDR)" in r["检验"][0]


# ------------------------------------------------------------------ ⑤ 七星彩第 7 位
def test_qxc_zone7_uses_modern_segment_only(monkeypatch):
    """
    cutoff 前把第 7 位做成极度倾斜（恒为 0），cutoff 后完全均匀。
    若 C 轨没有切段，第 7 位会被判成巨大偏离；切段正确则统计量很小。
    """
    n = 2400
    rng = np.random.default_rng(8)
    recs = []
    start = SG.QXC_RULE_CUTOFF - timedelta(days=2 * 1200)
    for i in range(n):
        d = start + timedelta(days=2 * i)
        old = d < SG.QXC_RULE_CUTOFF
        z7 = [0] if old else [int(rng.integers(0, 15))]
        zs = {f"第{j}位": [int(rng.integers(0, 10))] for j in range(1, 7)}
        zs["第7位"] = z7
        recs.append(types.SimpleNamespace(期号=i + 1, 开奖日期=d, zone_numbers=zs))
    _patch_loader(monkeypatch, recs)
    r = GC.structural_bias_report("七星彩", B=600, mc=1000)
    t7 = [t for t in r["检验"] if t["特征"] == "第7位"][0]
    assert t7["段"].startswith("现行段")
    assert t7["n"] < 1300, f"第7位用了 {t7['n']} 个观测，说明没切段"
    assert t7["实质显著"] is False
    # 第 1..6 位不该被切段（口径不同：A′ 轨整段切，C 轨只切第 7 位）
    t1 = [t for t in r["检验"] if t["特征"] == "第1位"][0]
    assert t1["段"] == "全段"
    assert t1["n"] > 2300


# ------------------------------------------------------------------ ⑥ 语义
def test_c_track_never_judges_gap(monkeypatch):
    recs = _fake_records("排列3", 4000, digit=1, tilt=0.5)
    _patch_loader(monkeypatch, recs)
    r = GC.structural_bias_report("排列3", B=500, mc=500)
    assert r["实质偏离"], "植入倾斜却没检出"
    assert r["gap"] is False
    assert "不单独判缝隙" in r["gap说明"]


def test_family_summary_counts_match_tests():
    r = GC.structural_bias_report("七星彩", B=400, mc=20_000)
    assert sum(v["m"] for v in r["族"].values()) == r["n检验"]
    assert set(r["族"]) == {"七星彩|位级"}


# ------------------------------------------------- ⑦ MC p 的分辨率地板披露
#
# `p = (1+ge)/(B+1)` 有下限 1/(B+1)。这不是 bug，但必须**披露**：一旦数据更新
# 导致族内 m 变大、或 B 被调小，地板就可能高过 BH 临界值而吃掉真信号。
# 本轮明确**不加大 B**（会拖慢 5 倍）—— 前提是地板确实远低于临界值，那就把它
# 变成可自检的字段而不是口头结论。

def test_mc_p_floor_is_reported_and_below_bh_critical_value():
    r = GC.structural_bias_report("七星彩", B=2000, mc=20_000)
    k = r["口径"]
    assert k["p分辨率地板"] == pytest.approx(1.0 / 2001.0, rel=1e-6)
    assert k["族内最大检验数"] == r["n检验"]
    assert k["BH最严临界值"] == pytest.approx(0.05 / r["n检验"], rel=1e-6)
    assert k["地板足够"] is True
    assert k["p分辨率地板"] < k["BH最严临界值"], \
        "MC 地板高过 BH 临界值 ⇒ 必须加大 B，否则本族检验失去区分力"


def test_p_floor_scales_with_B():
    """披露的必须是真实的 1/(B+1)，不是写死的常数。"""
    floors = []
    for B in (500, 2000, 8000):
        r = GC.structural_bias_report("七星彩", B=B, mc=20_000)
        floors.append(r["口径"]["p分辨率地板"])
        assert r["口径"]["p分辨率地板"] == pytest.approx(1.0 / (B + 1), rel=1e-6)
    assert floors[0] > floors[1] > floors[2], "B 越大地板应越低"


# ------------------------------------------------- ⑧ 红球形态 z 的 MC 精度闸门
#
# 与位级分支的「p 分辨率地板」对称：红球形态的
#   z = (观测均值 − MC零分布均值) / (null_sd/√n)
# 中，MC 零分布均值**自身**有抽样误差 null_sd/√mc
#   ⇒ **z 的种子噪声 = √(n/mc)**，随数据增长而固定 mc 时噪声上升。
# 2026-10-11 实测：大乐透 n≈2928、mc=6e4 ⇒ 噪声 0.221，
# 把真值 z≈2.88（解析期望口径）顶到 3.008，误报「连号对数」为实质偏离。
# 修法：mc 随 n 缩放（≥400×n），并把噪声披露出来自检。

def test_red_shape_mc_noise_is_bounded_and_scales_with_n(monkeypatch):
    """MC 组合数必须 ≥400×n，且披露的 z 噪声 ≤0.05（即便调用方传了更小的 mc）。"""
    reds = _reds_from_support(1, 33, 6, 1200, seed=7)
    monkeypatch.setattr(GC, "load_draw_frame", lambda lot, **k: _frame(reds))
    r = GC.structural_bias_report("双色球", B=200, mc=20_000)
    k = r["口径"]
    shape = [t for t in r["检验"] if t["族"].endswith("红球形态")]
    assert shape, "应产生红球形态检验"
    assert shape[0]["MC组合数"] >= GC.RED_NULL_MC_PER_DRAW * shape[0]["n"], \
        "MC 组合数必须随 n 缩放，否则大样本下 z 被 MC 噪声推过门槛"
    assert k["形态z的MC噪声"] is not None and k["形态z的MC噪声"] <= 0.05 + 1e-9
    assert k["形态z地板足够"] is True


def test_real_daloto_consecutive_pairs_is_not_flagged():
    """真数据回归：大乐透「连号对数」z≈2.95 是临界噪声，**不是**实质偏离。"""
    r = GC.structural_bias_report("大乐透", B=1000, mc=60_000)
    assert "连号对数" not in {x["特征"] for x in r["实质偏离"]}, r["实质偏离"]
