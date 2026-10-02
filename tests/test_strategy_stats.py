# -*- coding: utf-8 -*-
"""tests/test_strategy_stats.py —— 策略历史实测统计（含随机基线）

锁住的行为：
  1. 实测=真预测（valid_prediction=True）的「总命中」均值；基线=compute_random_baseline
     的超几何期望（双色球 1.1534 / 大乐透 1.0476 / 排5 0.5 / 3D·排3 0.3 / 七星 0.6667）；
  2. ML 家族（ML(logistic)/统计训练(W50)）归一为 ML策略，不散成多行；
  3. 马后炮（valid_prediction=False）与无策略记录不计入；
  4. 样本 <30 → 标「样本不足」，数值照给不隐藏；
  5. 红蓝型给红/蓝分项与分项基线，数字型不给；
  6. batch_strategy_stats 只返回本批出现过的策略（策略名先归一再匹配）。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ev import strategy_stats
from ev.strategy_stats import batch_strategy_stats, strategy_hit_stats


def _rec(strategy, hits, grade="未中", valid=True, red=None, blue=None):
    r = {"策略": strategy, "总命中": hits, "中奖等级": grade, "valid_prediction": valid}
    if red is not None:
        r["红球命中"] = red
        r["蓝球命中"] = blue or 0
    return r


@pytest.fixture
def fake_history(monkeypatch):
    """替换 data.feedback.load_feedback_history，隔离真实数据。

    strategy_stats 内部是函数内 `from data.feedback import ...`，
    每次调用都从模块取属性 → patch 模块属性即可生效。
    """
    import data.feedback as fb

    def _set(records):
        monkeypatch.setattr(fb, "load_feedback_history", lambda lot, lookback=None: list(records))
    return _set


# ---------------- 1. 实测 / 基线 / 差值 ----------------
def test_ssq_observed_vs_baseline(fake_history):
    """双色球：3 注命中 1/2/0 → 实测 1.0；基线 1.1534（6²/33 + 1²/16）。"""
    fake_history([_rec("高频策略", 1, red=1, blue=0),
                  _rec("高频策略", 2, red=1, blue=1),
                  _rec("高频策略", 0, red=0, blue=0)])
    d = strategy_hit_stats("双色球")
    row = d["策略"]["高频策略"]
    assert row["样本数"] == 3
    assert row["平均总命中"] == pytest.approx(1.0, abs=1e-4)
    assert row["理论基线"] == pytest.approx(1.1534, abs=1e-3)
    assert row["差值"] == pytest.approx(1.0 - 1.1534, abs=1e-3)
    assert d["is_redblue"] is True
    assert d["整体实测"] == pytest.approx(1.0, abs=1e-4)


def test_digital_lottery_baseline_is_point_five(fake_history):
    """排列5：5 位 × 1/10 → 基线 0.5；数字型不给红/蓝分项。"""
    fake_history([_rec("遗漏值策略", 1), _rec("遗漏值策略", 0)])
    d = strategy_hit_stats("排列5")
    row = d["策略"]["遗漏值策略"]
    assert row["理论基线"] == pytest.approx(0.5, abs=1e-6)
    assert row["平均总命中"] == pytest.approx(0.5, abs=1e-4)
    assert "平均红球命中" not in row
    assert d["is_redblue"] is False


# ---------------- 2. 策略名归一 ----------------
def test_ml_family_is_merged(fake_history):
    fake_history([_rec("ML(logistic)", 2), _rec("统计训练(W1000)", 1), _rec("ML策略", 0)])
    d = strategy_hit_stats("双色球")
    assert list(d["策略"]) == ["ML策略"]
    assert d["策略"]["ML策略"]["样本数"] == 3
    assert d["策略"]["ML策略"]["平均总命中"] == pytest.approx(1.0, abs=1e-4)


# ---------------- 3. 过滤口径 ----------------
def test_invalid_and_strategyless_are_excluded(fake_history):
    fake_history([_rec("高频策略", 3),
                  _rec("高频策略", 9, valid=False),      # 马后炮/训练 → 不计
                  {"策略": "", "总命中": 9, "中奖等级": "未中", "valid_prediction": True},
                  {"总命中": 9, "中奖等级": "未中", "valid_prediction": True}])
    d = strategy_hit_stats("双色球")
    assert d["策略"]["高频策略"]["样本数"] == 1
    assert d["策略"]["高频策略"]["平均总命中"] == pytest.approx(3.0)
    assert d["样本合计"] == 1


def test_empty_history_returns_note(fake_history):
    fake_history([])
    d = strategy_hit_stats("双色球")
    assert d["策略"] == {} and d["样本合计"] == 0
    assert "note" in d


# ---------------- 4. 样本不足 ----------------
def test_small_sample_flagged(fake_history):
    fake_history([_rec("规则优选", 2)] * 5)
    d = strategy_hit_stats("双色球")
    row = d["策略"]["规则优选"]
    assert row["样本数"] == 5 and row["样本不足"] is True
    assert row["平均总命中"] == pytest.approx(2.0)      # 数值照给，不隐藏


def test_enough_sample_not_flagged(fake_history):
    fake_history([_rec("高频策略", 1)] * 30)
    assert strategy_hit_stats("双色球")["策略"]["高频策略"]["样本不足"] is False


# ---------------- 5. 中奖率 ----------------
def test_win_rate_and_grade_distribution(fake_history):
    fake_history([_rec("区间均衡策略", 3, grade="六等"),
                  _rec("区间均衡策略", 1, grade="未中"),
                  _rec("区间均衡策略", 2, grade="六等"),
                  _rec("区间均衡策略", 0, grade="未中")])
    row = strategy_hit_stats("双色球")["策略"]["区间均衡策略"]
    assert row["中奖次数"] == 2 and row["中奖率"] == pytest.approx(0.5)
    assert row["中奖等级分布"] == {"六等": 2, "未中": 2}


# ---------------- 6. 批次裁剪 ----------------
def test_batch_stats_only_returns_used_strategies(fake_history):
    """只返回本批出现过的策略；票面原始名先归一再匹配统计键。"""
    fake_history([_rec("高频策略", 1), _rec("遗漏值策略", 2), _rec("ML(logistic)", 3)])
    got = batch_strategy_stats("双色球", ["遗漏值策略", "ML(logistic)"])
    assert set(got) == {"遗漏值策略", "ML策略"}
    assert got["ML策略"]["平均总命中"] == pytest.approx(3.0)
    assert batch_strategy_stats("双色球", []) == {}
