# -*- coding: utf-8 -*-
"""tests/test_tier_review.py —— 分级复盘内核（`ev/tier_review.py`）的回归钉。

固化的两类事故（都发生在 2026-10-24 前后，且都不会报错、只会算错）：

1. **规则版本口径混用**：判级用最新规则、金额按奖级名查旧期 ⇒ 大乐透固定奖
   整体放大约 6 倍（返奖率 163.8%）。⇒ `current` / `periodic` 两条口径必须
   分开实现；双色球单版本 ⇒ 构造性等价（逐位相同），大乐透 5 版本 ⇒ 不相等。
2. **同名奖级跨版本合并**：大乐透「四等」3000 元(2007/09) → 200 元(2014) →
   300 元(2026)。⇒ `tier_breakdown` 分桶键必须带规则版本，双键分桶。

外加三条口径纪律的回归：
· 分级累加总额与 `money_series` 逐分相等（脚本内原有断言，这里固化）；
· 逐票兜底**只按「本期真的命中了金额未定的奖级」触发**，不按名单非空整期回退；
· 倍投向量对金额/命中票次是严格线性的（倍数不进命中结构）。
"""
from __future__ import annotations

import numpy as np
import pytest

from ev import tier_review
from ev.arena import payout as MONEY
from ev.coverage_backtest import build_pool
from ev.profit.rules import rules_for
from ev.tier_backtest import load_test_recs

SSQ = "双色球"
DLT = "大乐透"
T10 = {"kind": "payout", "min_payout": 10.0}
T4 = {"kind": "grade", "grade": "四等"}
POOL_SEED = 20261024

# ---------------------------------------------------------------- 夹具
_RECS: dict = {}


def _recs(lottery: str):
    """开奖记录缓存（只读；`load_test_recs` 每次解析 CSV，缓存后全套件更快）。"""
    if lottery not in _RECS:
        _RECS[lottery] = load_test_recs(lottery)
    return _RECS[lottery]


def _first_idx(lottery: str, version: str) -> int:
    for i, r in enumerate(_recs(lottery)):
        if rules_for(lottery, r.期号).rule_version == version:
            return i
    raise AssertionError(f"{lottery} 找不到规则版本 {version}")


def _boundary_window(lottery: str, version: str, half: int = 25):
    """跨版本边界窗口：`version` 首期前 half 期 + 其后 half 期（两版本各 half）。"""
    i = _first_idx(lottery, version)
    return _recs(lottery)[i - half:i + half]


def _tickets(lottery: str, k: int):
    return build_pool(lottery, max(k, 8), seed=POOL_SEED)[:k]


def _const_hj(k: int, n: int, h: int, j: int):
    """全票全期同一 (h, j) 的合成命中矩阵（确定性，不依赖摇号运气）。"""
    return (np.full((k, n), h, dtype=np.int16),
            np.full((k, n), j, dtype=np.int16))


# ================================================================ gate_ok
def test_gate_current_matches_manual_lookup():
    """current 口径 = 目标期号 gate 的一次查表（独立路径重算）。"""
    recs = _recs(SSQ)[-60:]
    ti = recs[-1].期号
    tks = _tickets(SSQ, 12)
    h, j = MONEY.hit_matrices(SSQ, tks, recs, ti)
    ok = tier_review.gate_ok(SSQ, T10, h, j, recs, "current", ti)
    gate, _w, _m, _n = tier_review.outcome_gate(SSQ, T10, ti)
    np.testing.assert_array_equal(ok, gate[h, j])
    assert ok.shape == h.shape and ok.dtype == np.bool_


def test_gate_periodic_matches_per_period_manual():
    """periodic 口径 = 逐期用当期 gate（独立路径重算，不用缓存）。"""
    recs = _boundary_window(DLT, "dlt-2019-02")
    ti = recs[-1].期号
    tks = _tickets(DLT, 12)
    h, j = MONEY.hit_matrices(DLT, tks, recs, ti)
    ok = tier_review.gate_ok(DLT, T4, h, j, recs, "periodic", ti)
    want = np.zeros(h.shape, dtype=bool)
    for p, rec in enumerate(recs):
        g, _w, _m, _n = tier_review.outcome_gate(DLT, T4, rec.期号)
        want[:, p] = g[h[:, p], j[:, p]]
    np.testing.assert_array_equal(ok, want)


def test_gate_current_equals_periodic_for_ssq():
    """双色球只有 1 个规则版本 ⇒ 两口径构造性等价（逐位相同）。"""
    recs = _recs(SSQ)[-60:]
    ti = recs[-1].期号
    tks = _tickets(SSQ, 12)
    h, j = MONEY.hit_matrices(SSQ, tks, recs, ti)
    for tgt in (T10, T4):
        a = tier_review.gate_ok(SSQ, tgt, h, j, recs, "current", ti)
        b = tier_review.gate_ok(SSQ, tgt, h, j, recs, "periodic", ti)
        np.testing.assert_array_equal(a, b)


def test_gate_two_modes_differ_for_dlt_tier4():
    """★ 大乐透 ≥四等奖：current 与 periodic **不相等**，且两个方向都能出现。

    条件集宽窄关系（`data/prize_versions.py` 实测）：
      dlt-2014-05 ⊃ dlt-2019-02（2014 多 (3,2)）；dlt-2026-02 ⊃ dlt-2019-02（2026 多 (4,1)）。
    ⇒ 目标期落在较新版本时，旧期段上 periodic 可能更宽也可能更窄，
      「用哪个口径」不是写法问题，是结论问题。
    """
    # 窗口 A：2014 | 2019（目标期 = 2019）。2014 段 (3,2)：periodic 宽、current 窄。
    recs = _boundary_window(DLT, "dlt-2019-02")
    n0 = sum(1 for r in recs if rules_for(DLT, r.期号).rule_version == "dlt-2014-05")
    assert 0 < n0 < len(recs)
    ti = recs[-1].期号
    h, j = _const_hj(2, len(recs), 3, 2)
    pa = tier_review.gate_ok(DLT, T4, h, j, recs, "periodic", ti)
    ca = tier_review.gate_ok(DLT, T4, h, j, recs, "current", ti)
    assert pa[:, :n0].all() and not ca[:, :n0].any(), "2014 段 (3,2)：应 periodic 真 / current 假"
    assert not (pa == ca).all()

    # 窗口 B：2019 | 2026（目标期 = 2026）。2019 段 (4,1)：periodic 窄、current 宽。
    recs = _boundary_window(DLT, "dlt-2026-02")
    n0 = sum(1 for r in recs if rules_for(DLT, r.期号).rule_version == "dlt-2019-02")
    assert 0 < n0 < len(recs)
    ti = recs[-1].期号
    h, j = _const_hj(2, len(recs), 4, 1)
    pb = tier_review.gate_ok(DLT, T4, h, j, recs, "periodic", ti)
    cb = tier_review.gate_ok(DLT, T4, h, j, recs, "current", ti)
    assert not pb[:, :n0].any() and cb[:, :n0].all(), "2019 段 (4,1)：应 periodic 假 / current 真"
    assert not (pb == cb).all()


def test_gate_payout10_identical_across_versions_for_dlt():
    """≥10元 条件集五版本逐位相同 ⇒ 两口径在大乐透上也必须逐位相等。

    与上一条合起来才是完整口径图：不是「永远不等」，而是「取决于目标的条件集
    是否跨版本稳定」——≥10元 稳定（可互换），≥四等奖 不稳定（不可互换）。
    """
    for version in ("dlt-2019-02", "dlt-2026-02"):
        recs = _boundary_window(DLT, version)
        ti = recs[-1].期号
        h, j = _const_hj(3, len(recs), 4, 1)
        a = tier_review.gate_ok(DLT, T10, h, j, recs, "current", ti)
        b = tier_review.gate_ok(DLT, T10, h, j, recs, "periodic", ti)
        np.testing.assert_array_equal(a, b)
        # (4,1) 在 2026 版是四等 300 元 ⇒ ≥10元 为真；两个窗口下都应为真
        assert a.all()


def test_gate_shape_assertion_current():
    """current：命中数越出 gate 范围必须炸（静默截断会伪造达标率）。"""
    recs = _recs(SSQ)[-20:]
    ti = recs[-1].期号
    h, j = _const_hj(2, len(recs), 34, 0)      # SSQ 外层 33 选 6 ⇒ gate 行数 34
    with pytest.raises(AssertionError, match="current gate 形状"):
        tier_review.gate_ok(SSQ, T10, h, j, recs, "current", ti)


def test_gate_shape_assertion_periodic():
    """periodic：逐期形状检查，越界期必须炸。"""
    recs = _recs(SSQ)[-20:]
    ti = recs[-1].期号
    h, j = _const_hj(2, len(recs), 34, 0)
    with pytest.raises(AssertionError, match="periodic gate"):
        tier_review.gate_ok(SSQ, T10, h, j, recs, "periodic", ti)


def test_gate_periodic_caches_gate_by_version(monkeypatch):
    """periodic 按规则版本缓存 gate：同版本多期只查一次表（性能，且行为不变）。"""
    recs = _boundary_window(DLT, "dlt-2019-02")
    ti = recs[-1].期号
    tks = _tickets(DLT, 8)
    h, j = MONEY.hit_matrices(DLT, tks, recs, ti)
    real = tier_review.outcome_gate
    seen: list = []

    def counting(*a, **k):
        seen.append(a[2] if len(a) > 2 else k.get("issue"))
        return real(*a, **k)

    monkeypatch.setattr(tier_review, "outcome_gate", counting)
    ok = tier_review.gate_ok(DLT, T4, h, j, recs, "periodic", ti)
    assert len(seen) == 2, f"两版本应只查 2 次表，实查 {len(seen)}"
    assert len(set(str(x) for x in seen)) == 2
    # 打桩后结果必须与真函数一致（缓存不能改变语义）
    monkeypatch.undo()
    ok2 = tier_review.gate_ok(DLT, T4, h, j, recs, "periodic", ti)
    np.testing.assert_array_equal(ok, ok2)
    # current 只查一次（目标期号）
    seen.clear()
    monkeypatch.setattr(tier_review, "outcome_gate", counting)
    tier_review.gate_ok(DLT, T4, h, j, recs, "current", ti)
    assert len(seen) == 1 and seen[0] == ti


# ================================================================ tier_breakdown
def _ssq_fixture(n=60, k=12):
    recs = _recs(SSQ)[-n:]
    ti = recs[-1].期号
    tks = _tickets(SSQ, k)
    h, j = MONEY.hit_matrices(SSQ, tks, recs, ti)
    return tks, recs, ti, h, j


@pytest.mark.parametrize("mode", ["current", "periodic"])
def test_breakdown_total_matches_money_series(mode):
    """★ 分级累加总额与 money_series 总实得逐分相等（脚本内断言的固化）。"""
    tks, recs, ti, h, j = _ssq_fixture()
    tb = tier_review.tier_breakdown(SSQ, tks, recs, h, j, mode, ti)
    ms = MONEY.money_series(SSQ, tks, recs, rule_mode=mode, target_issue=ti)
    assert abs(ms["总实得"] - tb["总额"]) < 0.01


def test_breakdown_bucket_sums_match_total_and_winning_count():
    """桶间累加自洽：金额和 == 总额；命中票次和 == money_series 的中奖注数。"""
    tks, recs, ti, h, j = _ssq_fixture()
    tb = tier_review.tier_breakdown(SSQ, tks, recs, h, j, "current", ti)
    ms = MONEY.money_series(SSQ, tks, recs, rule_mode="current", target_issue=ti)
    assert tb["buckets"], "60 期 × 12 注应有中奖桶"
    assert sum(b["金额"] for b in tb["buckets"].values()) == pytest.approx(tb["总额"], abs=0.02)
    assert sum(b["命中票次"] for b in tb["buckets"].values()) == ms["中奖注数"]
    # 键形态：`版本|奖级`，版本必须是该窗内真实存在的版本
    vers = {rules_for(SSQ, r.期号).rule_version for r in recs}
    for key in tb["buckets"]:
        ver, grade = key.split("|")
        assert ver in vers and grade


def test_breakdown_double_key_no_merge_real_data():
    """★ 真实数据上的双键分桶：同名「三等」在 2019/2026 两版本下必须是两个桶。

    2019 版三等 = 10000 元；2026 版三等 = 5000/6666 元（奖池 ≥8 亿上浮）。
    按名合并会把两个版本的金额混在一起 —— 正是 10-24 事故的形态。
    """
    i26 = _first_idx(DLT, "dlt-2026-02")
    recs = _recs(DLT)[i26 - 31:i26 + 29]          # 31 期 2019 + 29 期 2026
    ti = recs[-1].期号
    tks = _tickets(DLT, 4)
    h, j = _const_hj(4, len(recs), 5, 0)          # (5,0) 两版本都判三等
    tb = tier_review.tier_breakdown(DLT, tks, recs, h, j, "periodic", ti)
    b = tb["buckets"]
    assert set(b) == {"dlt-2019-02|三等", "dlt-2026-02|三等"}, f"实际桶: {sorted(b)}"
    n19 = sum(1 for r in recs if rules_for(DLT, r.期号).rule_version == "dlt-2019-02")
    n26 = len(recs) - n19
    assert n19 == 31 and n26 == 29
    assert b["dlt-2019-02|三等"]["命中票次"] == 4 * n19
    assert b["dlt-2019-02|三等"]["涉及期数"] == n19
    assert b["dlt-2019-02|三等"]["金额"] == pytest.approx(4 * n19 * 10000.0)
    assert b["dlt-2026-02|三等"]["命中票次"] == 4 * n26
    assert b["dlt-2026-02|三等"]["涉及期数"] == n26
    # 2026 三期额 5000（奖池 <8 亿）或 6666（≥8 亿上浮）⇒ 只夹紧上下界
    assert 4 * n26 * 5000.0 <= b["dlt-2026-02|三等"]["金额"] <= 4 * n26 * 6666.0
    assert tb["总额"] == pytest.approx(
        b["dlt-2019-02|三等"]["金额"] + b["dlt-2026-02|三等"]["金额"], abs=0.02)


def test_breakdown_no_merge_hermetic(monkeypatch):
    """★  hermetic 双键钉：同名同格、跨版本不同额 ⇒ 必须分桶且各按各的版本发。

    直接复刻 10-24 事故的形态：判级同一格 (4,2)，2009 版四等 3000 元、
    2014 版四等 200 元。按名合并或按目标版本发钱都会在这里现形。
    """
    recs = _boundary_window(DLT, "dlt-2014-05")
    ti = recs[-1].期号
    tks = _tickets(DLT, 3)
    h, j = _const_hj(3, len(recs), 4, 2)

    def fake_amount_grid(lottery, issue, *, rule_issue=None):
        r = rules_for(lottery, rule_issue if rule_issue is not None else issue)
        w, m = int(r.outer_width), max(jj for (_h, jj) in r.grade_table)
        old = int(str(issue)) < 14052                # dlt-2014-05 首期
        ver, amt = ("verOld", 3000.0) if old else ("verNew", 200.0)
        grid = np.zeros((w + 1, m + 1))
        grid[4, 2] = amt
        names = [["" for _ in range(m + 1)] for _ in range(w + 1)]
        names[4][2] = "四等"
        return grid, {"规则版本": ver, "期号基准": "fake", "奖级金额": {"四等": amt},
                      "口径": {"四等": "fake"}, "需逐票兜底": [],
                      "待定格": np.zeros((w + 1, m + 1), dtype=bool),
                      "奖级网格": names}

    monkeypatch.setattr(MONEY, "amount_grid", fake_amount_grid)
    tb = tier_review.tier_breakdown(DLT, tks, recs, h, j, "periodic", ti)
    n_old = sum(1 for r in recs if int(str(r.期号)) < 14052)
    n_new = len(recs) - n_old
    assert 0 < n_old < len(recs)
    assert set(tb["buckets"]) == {"verOld|四等", "verNew|四等"}
    assert tb["buckets"]["verOld|四等"]["金额"] == pytest.approx(3 * n_old * 3000.0)
    assert tb["buckets"]["verNew|四等"]["金额"] == pytest.approx(3 * n_new * 200.0)
    assert tb["buckets"]["verOld|四等"]["命中票次"] == 3 * n_old
    assert tb["逐票兜底期数"] == 0
    assert tb["总额"] == pytest.approx(3 * (n_old * 3000.0 + n_new * 200.0))


def test_breakdown_fallback_triggered_only_when_pending_cell_hit(monkeypatch):
    """逐票兜底只按「本期真的命中了金额未定的奖级」触发 —— 两个分支都钉住。"""
    recs = _recs(SSQ)[-12:]
    ti = recs[-1].期号
    tks = _tickets(SSQ, 3)
    gate = tier_review.outcome_gate(SSQ, T10, ti)[0]
    w, m = gate.shape[0] - 1, gate.shape[1] - 1

    def fake_amount_grid(lottery, issue, *, rule_issue=None):
        grid = np.zeros((w + 1, m + 1))
        grid[4, 0] = 10.0                       # 五等（正常路径可走网格）
        names = [["" for _ in range(m + 1)] for _ in range(w + 1)]
        names[4][0] = "五等"
        pending = np.zeros((w + 1, m + 1), dtype=bool)
        pending[6, 1] = True                    # 一等金额未定（SSQ 浮动奖）
        return grid, {"规则版本": "ssq-fake", "期号基准": "fake",
                      "奖级金额": {}, "口径": {}, "需逐票兜底": ["一等"],
                      "待定格": pending, "奖级网格": names}

    def fake_grade_ticket(lottery, ticket, rec, issue, cache, *, rule_issue=None):
        # 只有第一张票中一等奖；其余未中
        if ticket is tks[0]:
            return ("一等", 5_000_000.0, "fake")
        return ("未中", 0.0, "fake")

    monkeypatch.setattr(MONEY, "amount_grid", fake_amount_grid)
    monkeypatch.setattr(tier_review, "grade_ticket", fake_grade_ticket)

    # 分支 A：命中 (6,1)（金额未定格）⇒ 该期必须走逐票兜底
    h, j = _const_hj(3, len(recs), 6, 1)
    tb = tier_review.tier_breakdown(SSQ, tks, recs, h, j, "current", ti)
    assert tb["逐票兜底期数"] == len(recs)
    assert set(tb["buckets"]) == {"ssq-fake|一等"}
    assert tb["buckets"]["ssq-fake|一等"]["命中票次"] == len(recs)   # 每期仅 1 注中
    assert tb["buckets"]["ssq-fake|一等"]["金额"] == pytest.approx(len(recs) * 5_000_000.0)

    # 分支 B：只命中 (4,0)（金额已定）⇒ 即使兜底名单非空也不整期回退
    h, j = _const_hj(3, len(recs), 4, 0)
    tb = tier_review.tier_breakdown(SSQ, tks, recs, h, j, "current", ti)
    assert tb["逐票兜底期数"] == 0, "未命中金额未定格时不得整期回退（52%→0~1 期的优化）"
    assert set(tb["buckets"]) == {"ssq-fake|五等"}
    assert tb["buckets"]["ssq-fake|五等"]["命中票次"] == 3 * len(recs)
    assert tb["buckets"]["ssq-fake|五等"]["金额"] == pytest.approx(3 * len(recs) * 10.0)


def test_breakdown_multiplier_is_linear():
    """倍投向量对金额与命中票次严格线性（倍数不进命中结构的直接读数）。"""
    recs = _recs(SSQ)[-40:]
    ti = recs[-1].期号
    tks = _tickets(SSQ, 3)
    h, j = _const_hj(3, len(recs), 4, 0)          # 五等 10 元/注
    base = tier_review.tier_breakdown(SSQ, tks, recs, h, j, "current", ti)
    assert base["逐票兜底期数"] == 0               # (6,1) 未被命中 ⇒ 不走兜底
    assert base["总额"] == pytest.approx(3 * len(recs) * 10.0)
    assert base["buckets"]["ssq-2026-02|五等"]["涉及期数"] == len(recs)  # 期数去重

    # mult=None 与全 1 等价
    ones = tier_review.tier_breakdown(SSQ, tks, recs, h, j, "current", ti,
                                      mult=np.ones(3, dtype=np.int64))
    assert ones == base

    # 混合倍数：总额 = Σ_k m_k × 单注累计；命中票次同比例
    m = np.array([1, 2, 3], dtype=np.int64)
    tb = tier_review.tier_breakdown(SSQ, tks, recs, h, j, "current", ti, mult=m)
    assert tb["总额"] == pytest.approx(len(recs) * 10.0 * m.sum())
    assert tb["buckets"]["ssq-2026-02|五等"]["命中票次"] == len(recs) * int(m.sum())
    assert tb["buckets"]["ssq-2026-02|五等"]["金额"] == pytest.approx(tb["总额"])

    # 单票翻倍 = 只改那一张票的贡献（定位到具体票）
    t2 = tier_review.tier_breakdown(SSQ, tks[:1], recs, h[:1], j[:1], "current", ti)
    m2 = tier_review.tier_breakdown(SSQ, tks, recs, h, j, "current", ti,
                                    mult=np.array([1, 5, 1], dtype=np.int64))
    assert m2["总额"] == pytest.approx(base["总额"] + 4 * t2["总额"])


def test_breakdown_empty_when_no_win():
    """零命中 ⇒ 空桶、零总额、零兜底（不得造假桶）。"""
    recs = _recs(SSQ)[-20:]
    ti = recs[-1].期号
    tks = _tickets(SSQ, 3)
    h, j = _const_hj(3, len(recs), 0, 0)          # (0,0) 未中
    tb = tier_review.tier_breakdown(SSQ, tks, recs, h, j, "current", ti)
    assert tb["buckets"] == {} and tb["总额"] == 0.0 and tb["逐票兜底期数"] == 0


# ================================================================ attribute_delta
def test_attribute_delta_single_ticket_double():
    """单张票加倍：Δ 只来自那一张票，按奖级归因键带版本。"""
    recs = _recs(SSQ)[-40:]
    ti = recs[-1].期号
    tks = _tickets(SSQ, 3)
    h = np.zeros((3, len(recs)), dtype=np.int16)
    j = np.zeros((3, len(recs)), dtype=np.int16)
    h[0, :], j[0, :] = 4, 0                        # 仅票 0 每期中五等 10 元
    ad = tier_review.attribute_delta(SSQ, tks, recs, h, j,
                                     np.ones(3, dtype=np.int64),
                                     np.array([2, 1, 1], dtype=np.int64),
                                     "current", ti)
    assert ad["总额"] == pytest.approx(len(recs) * 10.0)
    assert ad["按奖级"] == {"ssq-2026-02|五等": pytest.approx(len(recs) * 10.0)}
    assert len(ad["最大5期"]) == 5
    assert ad["最大单期占总额比"] == pytest.approx(1.0 / len(recs))


def test_attribute_delta_zero_and_negative():
    """无差 ⇒ 全零（占占比为 None 而非除零）；反向倍投 ⇒ Δ 为负。"""
    recs = _recs(SSQ)[-40:]
    ti = recs[-1].期号
    tks = _tickets(SSQ, 3)
    h, j = _const_hj(3, len(recs), 4, 0)
    z = tier_review.attribute_delta(SSQ, tks, recs, h, j,
                                    np.ones(3, dtype=np.int64),
                                    np.ones(3, dtype=np.int64), "current", ti)
    assert z["总额"] == 0.0 and z["按奖级"] == {} and z["最大5期"] == []
    assert z["最大单期占总额比"] is None
    neg = tier_review.attribute_delta(SSQ, tks, recs, h, j,
                                      np.array([2, 1, 1], dtype=np.int64),
                                      np.ones(3, dtype=np.int64), "current", ti)
    assert neg["总额"] == pytest.approx(-len(recs) * 10.0)
    assert neg["按奖级"]["ssq-2026-02|五等"] == pytest.approx(-len(recs) * 10.0)
    assert neg["最大单期占总额比"] > 0              # 占比取绝对值
