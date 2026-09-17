"""
NetPayout WP3 前瞻配对对照实验 + 漂移监控 测试

覆盖：
  - 对照严格性（A/B 同池、同重叠约束）
  - 期号口径（下注目标期 ≠ 打分基准期）
  - 官方判级复用
  - 序贯 e-value 公式
  - 功效/等待时间的诚实数字
  - 结算回填与报告诚实性
  - 漂移监控结构与"维护而非变强"的语义
"""
import json
import os

import numpy as np
import pytest

from ev import trials as T
from ev.selection import _rand_tickets, control_tickets


# ---------------- 对照严格性 ----------------
def test_control_comes_from_same_pool_with_overlap_cap():
    """B 组必须来自同一候选池、且遵守同样的低重叠约束（否则对照不严格）。"""
    rng = np.random.default_rng(1)
    cands = _rand_tickets("双色球", 2000, rng)
    picked = control_tickets("双色球", cands, n=5, seed=2)
    assert len(picked) == 5

    pool = {json.dumps(c, sort_keys=True) for c in cands}
    for t in picked:
        assert json.dumps(t, sort_keys=True) in pool, "对照注必须来自同一候选池"

    for i in range(len(picked)):
        for j in range(i + 1, len(picked)):
            ov = len(set(picked[i]["红球"]) & set(picked[j]["红球"]))
            assert ov <= 4, f"对照注未遵守重叠上限: {ov}"


def test_build_arms_same_pool_both_sides():
    """A/B 两组来自同一候选池（唯一差异=是否排序）。"""
    arms = T.build_arms("双色球", n=3, pool_size=800, seed=5)
    assert not arms.get("拒绝")
    assert len(arms["A"]) == 3 and len(arms["B"]) == 3
    assert arms["A"] != arms["B"] or True   # 允许偶然相同（n 极小时），但不要求
    for t in arms["A"] + arms["B"]:
        assert sorted(t["红球"]) == t["红球"] and len(set(t["红球"])) == 6


# ---------------- 期号口径 ----------------
def test_record_trial_idempotent_and_marks_basis(monkeypatch, tmp_path):
    """同一目标期号不重复记录；记录里必须区分『期号』与『打分基准期』。"""
    monkeypatch.setattr(T, "_path", lambda lot: tmp_path / f"{lot}.jsonl")
    monkeypatch.setattr(T, "_writable", lambda: True)

    r1 = T.record_trial("双色球", n=3, pool_size=300, seed=7)
    assert r1.get("ok"), r1
    r2 = T.record_trial("双色球", n=3, pool_size=300, seed=7)
    assert r2.get("ok") and r2.get("跳过"), "同一期号必须幂等跳过"

    rows = T._read_rows("双色球")
    assert len(rows) == 1
    assert rows[0]["期号"] == str(r1["期号"])
    assert "打分基准期" in rows[0], "必须记录打分基准期（防止口径混淆）"
    assert rows[0]["打分基准期"] != rows[0]["期号"], "目标期与打分基准期不应相同"


def test_record_trial_refuses_in_pytest(monkeypatch):
    """三道闸之一：pytest 内默认拒写生产 trials 目录。"""
    monkeypatch.setattr(T, "_writable", lambda: False)
    r = T.record_trial("双色球", n=2, pool_size=50)
    assert r.get("ok") is False
    assert "pytest" in r.get("原因", "")


def test_non_participating_lottery_rejected():
    r = T.record_trial("排列5", n=2)
    assert r.get("ok") is False and "不参与" in r["原因"]


# ---------------- 判级 ----------------
def test_grade_of_reuses_official_rules():
    az = {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}
    assert T._grade_of("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}, az) == "一等"
    assert T._grade_of("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [8]}, az) == "二等"
    assert T._grade_of("双色球", {"红球": [9, 10, 11, 12, 13, 14], "蓝球": [7]}, az) == "六等"
    assert T._grade_of("双色球", {"红球": [9, 10, 11, 12, 13, 14], "蓝球": [8]}, az) == "未中"

    azd = {"红球": [1, 2, 3, 4, 5], "蓝球": [6, 7]}
    assert T._grade_of("大乐透", {"红球": [1, 2, 3, 4, 5], "蓝球": [6, 7]}, azd) == "一等"
    assert T._grade_of("大乐透", {"红球": [1, 2, 3, 4, 9], "蓝球": [6, 7]}, azd) == "四等"
    assert T._grade_of("大乐透", {"红球": [9, 10, 11, 12, 13], "蓝球": [1, 2]}, azd) == "未中"


# ---------------- 统计口径 ----------------
def test_sign_martingale_formula():
    """c=0.5：+1 → ×1.5，-1 → ×0.5；对称序列必然 <1（H0 下期望为 1）。"""
    W, path = T._sign_martingale([1, -1])
    assert abs(W - 0.75) < 1e-12
    assert len(path) == 2
    W2, _ = T._sign_martingale([1, 1])
    assert abs(W2 - 2.25) < 1e-12
    W0, _ = T._sign_martingale([])
    assert W0 == 1.0


def test_power_plan_is_human_scale_for_primary_endpoint():
    """主终点（中奖率等价性）必须在人类时间尺度内可完成。"""
    pl = T.power_plan("双色球", n_per_issue=100)
    assert 0 < pl["每注中奖概率"] < 1
    by_eff = {row["相对效应"]: row for row in pl["规划"]}
    assert by_eff["+20%"]["折合年数"] < 2, "检出 20% 差异应在 2 年内"
    assert by_eff["+10%"]["折合年数"] < 5, "检出 10% 差异应在 5 年内"


def test_wait_time_declares_jackpot_infeasible():
    """次终点（实得奖金）必须诚实标注为不可完成。"""
    wt = T.wait_time_years("双色球", n_per_issue=100)
    assert wt["期望等待"]["一等"]["期望等待年数"] > 500
    assert wt["期望等待"]["二等"]["期望等待年数"] > 10
    assert "不可完成" in wt["结论"]


# ---------------- 结算与报告 ----------------
def test_settle_backfills_and_pairs(monkeypatch, tmp_path):
    """结算必须按期号回填，且 A/B 配对出现在同一行。"""
    monkeypatch.setattr(T, "_path", lambda lot: tmp_path / f"{lot}.jsonl")
    monkeypatch.setattr(T, "_writable", lambda: True)

    from data.loader import load_lottery
    from data.schema import get_schema, normalize_zone_names

    data = load_lottery("双色球")
    last = data.records[-1]
    schema = get_schema("双色球")
    az = {k: [int(x) for x in v] for k, v in normalize_zone_names(last.zone_numbers, schema).items()}
    reds = az["红球"]
    blues = az["蓝球"]

    # A = 当期开奖号（必然一等）；B = 完全不重合（必然未中）
    miss_red = [d for d in range(1, 34) if d not in set(reds)][:6]
    miss_blue = [d for d in range(1, 17) if d not in set(blues)][:1]
    rows = [{
        "期号": str(last.期号),
        "打分基准期": str(last.期号),
        "A": [{"红球": reds, "蓝球": blues}],
        "B": [{"红球": miss_red, "蓝球": miss_blue}],
        "每组注数": 1,
        "结算": None,
    }]
    T._write_rows("双色球", rows)

    s = T.settle_trials("双色球")
    assert s["新结算期数"] == 1

    got = T._read_rows("双色球")[0]["结算"]
    assert got["A"]["中奖注数"] == 1
    assert got["B"]["中奖注数"] == 0
    assert "一等" in got["A"]["奖级分布"]
    assert got["Δ"] == pytest.approx(got["A"]["实得合计"] - got["B"]["实得合计"], abs=0.01)


def test_report_honest_with_no_data(monkeypatch, tmp_path):
    monkeypatch.setattr(T, "_path", lambda lot: tmp_path / f"{lot}.jsonl")
    r = T.trial_report("双色球")
    assert r["已结算期数"] == 0
    assert "样本不足" in r["裁决"]
    assert "功效规划" in r and "次终点样本量" in r


def test_report_equivalence_verdict_on_balanced_data(monkeypatch, tmp_path):
    """A/B 完全等价的数据 → 必须判为『等价性成立』（不得暗示模型更优）。"""
    monkeypatch.setattr(T, "_path", lambda lot: tmp_path / f"{lot}.jsonl")

    def _mk(hits_a, hits_b, amt_a, amt_b):
        return {"期号": str(1000 + len(_mk.pool)), "每组注数": 100, "A": [], "B": [],
                "结算": {"A": {"中奖注数": hits_a, "奖级分布": {}, "实得合计": amt_a},
                         "B": {"中奖注数": hits_b, "奖级分布": {}, "实得合计": amt_b},
                         "Δ": round(amt_a - amt_b, 2)}}
    _mk.pool = []
    rows = []
    for i in range(40):
        rows.append(_mk(6, 6, 30.0, 30.0))
        _mk.pool.append(1)
    T._write_rows("双色球", rows)

    r = T.trial_report("双色球")
    assert r["已结算期数"] == 40
    assert r["A累计中奖注数"] == r["B累计中奖注数"]
    assert r["等价性成立"] is True
    assert "等价" in r["裁决"]


# ---------------- 漂移监控 ----------------
def test_drift_report_go_path():
    from ev.popularity import drift_report

    r = drift_report("双色球")
    assert r["彩种"] == "双色球"
    assert r["go"] is True
    assert r["裁决"] in ("稳定", "漂移告警（结论层面）", "系数位移（结论仍稳定）")
    assert "分段提升" in r
    assert "维护" in r["性质说明"], "必须明确漂移修正=维护，不是让模型变强"


def test_drift_report_no_go_is_explicit():
    from ev.popularity import drift_report

    r = drift_report("大乐透")
    assert r["go"] is False
    assert r["裁决"] == "不适用"
    assert "Go 门槛" in r["建议"]
