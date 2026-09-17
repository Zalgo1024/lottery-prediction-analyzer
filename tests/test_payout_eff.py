# -*- coding: utf-8 -*-
"""ev/payout.py —— 实得奖金统一口径（NetPayout WP0）单元测试

设计原则（与项目证伪定位一致）：
- 概率断言用**组合精确值**（与号码无关），不依赖任何数据文件；
- EV 断言只锁「固定赔率彩种」（排列5/福彩3D，不读数据即可确定）；
- 乐透型涉及当期实际奖金，只断言结构性关系（实得 ≤ 名义、净EV < 0），
  不锁具体数值（会随奖池变化而变）。
"""
import math

import pytest

from ev.payout import (
    COST,
    grade_probabilities,
    net_ev_payout,
    normalize_ticket,
    payout_eff,
    share_multiplier,
    _poisson_share,
)


# ---------------- 概率：组合精确值 ----------------
def test_ssq_first_prize_prob():
    p = grade_probabilities("双色球")["一等"]
    assert abs(p - 1 / 17_721_088) < 1e-15


def test_ssq_all_grades_match_official_odds():
    """双色球官方赔率倒数：一 17,721,088 / 二 1,181,406 / 三 109,389 / 六 ≈17"""
    pr = grade_probabilities("双色球")
    assert round(1 / pr["二等"]) == 1_181_406
    assert round(1 / pr["三等"]) == 109_389
    assert round(1 / pr["四等"]) == 2_303
    assert round(1 / pr["六等"]) == 17
    # 全部奖级概率和 = 官方总中奖率 ≈ 6.71%
    assert 0.066 < sum(pr.values()) < 0.068


def test_dlt_first_prize_prob():
    p = grade_probabilities("大乐透")["一等"]
    assert abs(p - 1 / 21_425_712) < 1e-15
    # 一等 = 1/21425712 与 rollover.py 的 _P1 口径一致
    assert math.isclose(p, 1.0 / (math.comb(35, 5) * math.comb(12, 2)), rel_tol=1e-12)


def test_qxc_prob_sum_matches_rulebook():
    pr = grade_probabilities("七星彩")
    assert abs(sum(pr.values()) - (1 + 14 + 54 + 1971 + 31590 + 1188270) / 15_000_000) < 1e-12


def test_prob_independent_of_ticket():
    """概率与具体号码无关（本轨道诚实性的数学保证）"""
    a = grade_probabilities("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]})
    b = grade_probabilities("双色球", {"红球": [11, 22, 23, 24, 25, 26], "蓝球": [16]})
    assert a == b


# ---------------- 号码归一化 ----------------
def test_normalize_ticket_digital_str_and_list():
    assert normalize_ticket("排列5", "12345")["号码"] == [1, 2, 3, 4, 5]
    assert normalize_ticket("排列5", [1, 2, 3, 4, 5])["号码"] == [1, 2, 3, 4, 5]
    assert normalize_ticket("排列5", "1,2,3,4,5")["号码"] == [1, 2, 3, 4, 5]


def test_normalize_ticket_zone_dict_ordered():
    tk = normalize_ticket("排列5", {"第5位": 5, "第1位": 1, "第3位": 3, "第2位": 2, "第4位": 4})
    assert tk["号码"] == [1, 2, 3, 4, 5]


def test_normalize_ticket_redblue():
    tk = normalize_ticket("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]})
    assert tk["类型"] == "redblue" and tk["红球"] == [1, 2, 3, 4, 5, 6] and tk["蓝球"] == [7]
    tk2 = normalize_ticket("大乐透", ([1, 2, 3, 4, 5], [6, 7]))
    assert tk2["蓝球"] == [6, 7]


def test_normalize_ticket_bad_input():
    with pytest.raises(ValueError):
        normalize_ticket("双色球", "1234567")


# ---------------- 固定赔率彩种：EV 可精确锁定 ----------------
def test_pl5_ev_exactly_minus_one():
    """排列5：100000 × 1/1e5 = 1.0 → 净EV = -1.0（不依赖任何数据）"""
    r = payout_eff("排列5", "12345")
    assert r["名义期望"] == pytest.approx(1.0)
    assert r["净EV"] == pytest.approx(-1.0)


def test_3d_ev_exactly_minus_096():
    """福彩3D 直选：1040 × 1/1000 = 1.04 → 净EV = -0.96"""
    r = payout_eff("福彩3D", "123")
    assert r["名义期望"] == pytest.approx(1.04)
    assert r["净EV"] == pytest.approx(-0.96)


def test_fixed_odds_no_split():
    """固定赔率撞号不分薄 → 分薄乘数恒 1.0"""
    m, src = share_multiplier("排列5", "12345")
    assert m == 1.0
    assert "固定赔率" in src


# ---------------- 分薄乘数 ----------------
def test_poisson_share_bounds():
    assert _poisson_share(0.0) == 1.0          # λ=0 → 独享
    assert _poisson_share(1e-12) == 1.0
    assert _poisson_share(1.0) == pytest.approx(1 - math.exp(-1))
    assert _poisson_share(100.0) < 0.02        # λ 大 → 被严重分摊
    assert 0.0 < _poisson_share(5.0) < 1.0


def test_redblue_share_is_one_before_wp1():
    m, src = share_multiplier("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]})
    assert m == 1.0
    assert "WP1" in src


# ---------------- 结构性关系（不锁具体数值） ----------------
def test_effective_never_exceeds_nominal():
    """分薄路径（大乐透 No-Go 走当期实际基准、七星彩）实得 ≤ 名义恒成立。"""
    for lot, tk in (("大乐透", {"红球": [1, 2, 3, 4, 5], "蓝球": [6, 7]}),
                    ("七星彩", "1234567")):
        r = payout_eff(lot, tk)
        assert r["实得期望"] <= r["名义期望"] + 1e-9, lot
        assert r["净EV"] == pytest.approx(r["实得期望"] - COST)


def test_ssq_pool_path_activates():
    """双色球 WP1 GO → 一/二等奖走「奖池×E[1/(1+X)]」号码相关路径。"""
    r = payout_eff("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]})
    assert "流行度模型" in r["分薄"]["来源"]
    jz = next(x for x in r["奖级明细"] if x["奖级"] == "一等")
    assert "流行度模型" in jz["来源"] and jz["实得单注"] > 0


def test_ssq_cold_combo_beats_hot():
    """冷门组合的一等奖实得应高于热门组合（WP1 模型的方向性检验）。

    两组号码取自 2026-09-12 拟合的组合流行度分位：
    冷门 = 10% 分位 {11,16,23,27,28,32}；热门 = 90% 分位 {01,03,11,12,24,28}。
    """
    cold = payout_eff("双色球", {"红球": [11, 16, 23, 27, 28, 32], "蓝球": [7]})
    hot = payout_eff("双色球", {"红球": [1, 3, 11, 12, 24, 28], "蓝球": [7]})
    jz_cold = next(x for x in cold["奖级明细"] if x["奖级"] == "一等")["实得单注"]
    jz_hot = next(x for x in hot["奖级明细"] if x["奖级"] == "一等")["实得单注"]
    assert jz_cold > jz_hot


def test_net_ev_negative_for_all_lotteries():
    """所有彩种长期必为负期望（返奖率 < 100%）——框架的诚实底线"""
    for lot, tk in (("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}),
                    ("大乐透", {"红球": [1, 2, 3, 4, 5], "蓝球": [6, 7]}),
                    ("排列5", "12345"), ("福彩3D", "123"), ("七星彩", "1234567")):
        assert net_ev_payout(lot, tk) < 0, lot


def test_grade_rows_complete():
    r = payout_eff("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]})
    grades = [x["奖级"] for x in r["奖级明细"]]
    assert grades == ["一等", "二等", "三等", "四等", "五等", "六等"]
    for row in r["奖级明细"]:
        assert row["概率"] > 0
        assert row["来源"]


# ---------------- 错误处理 ----------------
def test_unknown_lottery_returns_error():
    assert "error" in payout_eff("快乐8", "12345")


def test_bad_ticket_returns_error():
    assert "error" in payout_eff("双色球", "1234567")


# ---------------- selection（WP2） ----------------
def test_select_fixed_odds_refuses():
    """固定赔率彩种选号不改变期望 → 明确拒绝而非假装优化"""
    from ev.selection import select_tickets
    r = select_tickets("排列5", n=3)
    assert r.get("拒绝") and "不改变" in r["原因"]


def test_select_ssq_returns_valid_tickets():
    from ev.selection import select_tickets
    r = select_tickets("双色球", n=3, pool_size=200, seed=42)
    assert r["选中数"] == 3 and r["机制"]
    for t in r["选中"]:
        reds = t["号码"]["红球"]
        assert len(reds) == 6 and len(set(reds)) == 6
        assert all(1 <= d <= 33 for d in reds)
        assert len(t["号码"]["蓝球"]) == 1 and 1 <= t["号码"]["蓝球"][0] <= 16
        assert t["净EV"] < 0  # 诚实底线：任何选号都为负期望
    # 重叠约束：两两红球交集 ≤ 4
    picked = [t["号码"]["红球"] for t in r["选中"]]
    for i in range(len(picked)):
        for j in range(i + 1, len(picked)):
            assert len(set(picked[i]) & set(picked[j])) <= 4


def test_select_dlt_honest_mechanism_note():
    """大乐透 WP1 No-Go → 机制字段必须诚实标注 EV 恒定"""
    from ev.selection import select_tickets
    r = select_tickets("大乐透", n=2, pool_size=100, seed=1)
    assert "否决" in r["机制"]


# ---------------- 向量化批量口径与单注口径对齐（优化回归） ----------------
def test_pool_share_batch_matches_single():
    """pool_share_expected_batch 必须与逐注 pool_share_expected 数值一致（网格差异容忍）。"""
    import numpy as np
    from ev.payout import pool_share_expected, pool_share_expected_batch
    mus = np.array([0.0, 0.3, 1.7, 5.2, 23.0])
    pool = 120_000_000.0
    batch = pool_share_expected_batch(pool, mus)
    for mu, b in zip(mus, batch):
        s = pool_share_expected(pool, float(mu))
        assert abs(b - s) <= max(1e-6, abs(s) * 1e-9), f"mu={mu} batch={b} single={s}"


def test_select_ssq_vectorized_matches_payout_eff():
    """select 向量化打分的「一等奖实得」必须与逐注 payout_eff 一致（同公式不同实现）。"""
    from ev.selection import select_tickets
    r = select_tickets("双色球", n=2, pool_size=400, seed=11)
    assert r["选中数"] >= 1
    for row in r["选中"]:
        t = row["号码"]
        single = payout_eff("双色球", t, issue=r["期号"])
        jz = next(x for x in single["奖级明细"] if x["奖级"] == "一等")
        if jz["实得单注"] is not None and row["一等奖实得"] is not None:
            assert abs(row["一等奖实得"] - jz["实得单注"]) <= max(1.0, jz["实得单注"] * 1e-6)


def test_select_dlt_no_go_is_honest_random():
    """大乐透 NO-GO：不做伪排序，选中项无『一等奖实得』数值，机制说明含否决语义。"""
    from ev.selection import select_tickets
    r = select_tickets("大乐透", n=2, pool_size=50, seed=3)
    assert "否决" in r["机制"]
    for row in r["选中"]:
        assert row["一等奖实得"] is None
        assert set(row["号码"]["红球"]) <= set(range(1, 36))
        assert len(row["号码"]["蓝球"]) == 2


# ---------------- 源滞后：期号解析必须跳过「未结算」记录 ----------------
def test_record_settled_rejects_lag_record():
    """最新一期只有开奖号码、奖金/销售额全为 0（源回填滞后）→ 不算已结算。

    同时也是反向保护：**无人中头奖**（一等奖金/注数=0）是正常已结算情形。
    """
    from ev.payout import _record_settled

    class R:
        pass

    lag = R()
    lag.一等奖奖金, lag.一等奖注数 = 0, 0
    lag.二等奖奖金, lag.总投注额 = 0, 0
    ok = R()
    ok.一等奖奖金, ok.一等奖注数 = 10_000_000, 1
    ok.二等奖奖金, ok.总投注额 = 148_168, 333_874_688
    no_jackpot = R()
    no_jackpot.一等奖奖金, no_jackpot.一等奖注数 = 0, 0
    no_jackpot.二等奖奖金, no_jackpot.总投注额 = 128_382, 333_116_798

    assert _record_settled(lag) is False
    assert _record_settled(ok) is True
    assert _record_settled(no_jackpot) is True


def test_resolve_issue_skips_unsettled_latest():
    """`_resolve_issue` 返回的期号必须是「已结算」的，否则浮动奖级读不到当期奖金。

    这是数据源滞后（开奖当晚只有号码、奖金列次日才回填）下的防线：
    若不跳过，一/二等会一路降级成「未知」→ EV 系统性低估。
    """
    from data.loader import load_lottery
    from ev.payout import _record_settled, _resolve_issue

    iss = _resolve_issue("双色球", None)
    assert iss is not None
    rec = next((r for r in load_lottery("双色球").records if str(r.期号) == iss), None)
    assert rec is not None, f"解析出的期号 {iss} 不在数据里"
    assert _record_settled(rec), f"解析出的期号 {iss} 仍未结算"


def test_select_ssq_fallback_path_fills_detail(monkeypatch):
    """向量化路径不可用时，逐注回退路径必须能回填明细。

    历史 bug：`_greedy_pick` 返回 {"号码","排序键"} 字典，而回退路径按 (key, t)
    解包 → 拿到的是字典的**键名** → round("号码", 2) TypeError。
    只在数据缺失（向量化路径返回 None）时才暴露，属长期潜伏缺陷。
    """
    from ev import selection

    monkeypatch.setattr(selection, "_select_redblue_vectorized", lambda *a, **k: None)
    r = selection.select_tickets("双色球", n=3, pool_size=120, seed=42)
    assert r["选中数"] == 3
    assert r["期号"]
    for t in r["选中"]:
        assert isinstance(t["净EV"], float) and t["净EV"] < 0   # 诚实底线
        assert isinstance(t["一等奖实得"], float)
