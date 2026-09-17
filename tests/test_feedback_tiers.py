"""
反馈闭环档位化（data/feedback.py）测试

覆盖：期号+档位去重 / 各档独立结算 / 权重学习仅一般档 / 旧记录向后兼容 / 三档对比。
全部使用 tmp_path 隔离，不碰真实反馈数据。
"""

import json

import pytest

import data.feedback as fb
from data.feedback import (
    TIER_GENERAL,
    _tier_of,
    append_feedback,
    dedupe_pending,
    evaluate_pending_predictions,
    get_feedback_summary,
    load_pending,
    record_pending_prediction,
    save_pending,
    tier_comparison,
    update_strategy_weights,
)


@pytest.fixture
def tmp_fb(tmp_path, monkeypatch):
    """把 pending/history/weights 三个路径全部指到 tmp 目录。"""
    monkeypatch.setattr(fb, "_pending_path", lambda name: tmp_path / f"{name}_pending.json")
    monkeypatch.setattr(fb, "_history_path", lambda name: tmp_path / f"{name}_history.json")
    monkeypatch.setattr(fb, "_weights_path", lambda name: tmp_path / f"{name}_weights.json")
    return tmp_path


# ------------------------------------------------------------
# _tier_of 兼容
# ------------------------------------------------------------

def test_tier_of_defaults():
    """无档位/非法档位 → 一般（旧记录向后兼容）。"""
    assert _tier_of({}) == TIER_GENERAL
    assert _tier_of({"档位": "稳健"}) == "稳健"
    assert _tier_of({"档位": "高鲁棒"}) == "高鲁棒"
    assert _tier_of({"档位": "乱写的"}) == TIER_GENERAL
    assert _tier_of(None) == TIER_GENERAL


# ------------------------------------------------------------
# pending 期号+档位 去重
# ------------------------------------------------------------

def test_record_pending_tiers_coexist(tmp_fb):
    """同目标期号三档共存；同档重写替换旧记录。"""
    base = {"预测日期": "2099-01-01", "预测号码": [], "号码组数": 0}
    for tier in ("一般", "稳健", "高鲁棒"):
        rec = dict(base, 目标期号=999001, 档位=tier, 标记=f"{tier}-v1")
        record_pending_prediction("排列5", rec)
    pending = load_pending("排列5")
    assert len(pending) == 3
    assert {r["档位"] for r in pending} == {"一般", "稳健", "高鲁棒"}

    # 同档重写：只替换该档，其他两档保留
    rec = dict(base, 目标期号=999001, 档位="稳健", 标记="稳健-v2")
    record_pending_prediction("排列5", rec)
    pending = load_pending("排列5")
    assert len(pending) == 3
    rob = [r for r in pending if r["档位"] == "稳健"][0]
    assert rob["标记"] == "稳健-v2"


def test_record_pending_no_tier_replaces_general(tmp_fb):
    """无档位记录（旧引擎路径）与一般档互相同步替换。"""
    base = {"预测日期": "2099-01-01", "预测号码": [], "号码组数": 0}
    record_pending_prediction("排列5", dict(base, 目标期号=999002, 标记="旧引擎"))
    pending = load_pending("排列5")
    assert len(pending) == 1 and pending[0]["标记"] == "旧引擎"
    # 再写一般档 → 同 (期号, 一般) 键被替换
    record_pending_prediction("排列5", dict(base, 目标期号=999002, 档位="一般", 标记="三档"))
    pending = load_pending("排列5")
    assert len(pending) == 1 and pending[0]["标记"] == "三档"


def test_dedupe_pending_by_issue_and_tier(tmp_fb):
    """dedupe_pending 按 (期号, 档位) 保留最新。"""
    base = {"预测日期": "2099-01-01", "预测号码": [], "号码组数": 0, "状态": "pending"}
    pending = [
        dict(base, 目标期号=999003, 档位="一般", 记录时间="2026-01-01T00:00:00", 标记="g-old"),
        dict(base, 目标期号=999003, 档位="一般", 记录时间="2026-01-02T00:00:00", 标记="g-new"),
        dict(base, 目标期号=999003, 档位="稳健", 记录时间="2026-01-01T00:00:00", 标记="r-old"),
        dict(base, 目标期号=999004, 档位="一般", 记录时间="2026-01-01T00:00:00", 标记="g2"),
    ]
    save_pending("排列5", pending)
    removed = dedupe_pending("排列5")
    assert removed == 1
    kept = {r["标记"] for r in load_pending("排列5")}
    assert kept == {"g-new", "r-old", "g2"}


# ------------------------------------------------------------
# 开奖结算带档位
# ------------------------------------------------------------

def _make_pending_record(issue, draw_rec, tier):
    """构造一条可结算的 pending 记录（预测日期=开奖日 → 真预测）。"""
    return {
        "状态": "pending",
        "来源": "predict",
        "档位": tier,
        "目标期号": issue,
        "期号": issue,
        "预测日期": str(draw_rec.开奖日期),
        "预测号码": [
            {"号码": {k: list(v) for k, v in draw_rec.zone_numbers.items()},
             "策略": "高频策略", "置信度": 0.5},
        ],
    }


def test_evaluate_settles_tiers_independently(tmp_fb):
    """三档同票面各自独立结算（fb_key 带档位，后结算档不丢账）。"""
    from data.loader import load_lottery

    data = load_lottery("排列5")
    rec = max(data.records, key=lambda r: r.期号)
    for tier in ("一般", "稳健", "高鲁棒"):
        save_pending("排列5", [_make_pending_record(rec.期号, rec, tier)])
        # 逐档写入（模拟三档各自入账）；占位记录标记无效，不参与权重
        append_feedback("排列5", {"策略": "占位", "valid_prediction": False})
        evaluate_pending_predictions("排列5")

    history = fb.load_feedback_history("排列5")
    settled = [h for h in history if h.get("档位")]
    assert len(settled) == 3
    assert {h["档位"] for h in settled} == {"一般", "稳健", "高鲁棒"}
    # 同期同票面三档全部入账（档位隔离生效）
    assert len({h["档位"] for h in settled if h.get("期号") == rec.期号}) == 3


def test_evaluate_backward_compat_no_tier(tmp_fb):
    """旧记录无档位 → 结算后档位=一般。"""
    from data.loader import load_lottery

    data = load_lottery("排列5")
    rec = max(data.records, key=lambda r: r.期号)
    pend = _make_pending_record(rec.期号, rec, "一般")
    pend.pop("档位")  # 模拟旧格式
    save_pending("排列5", [pend])
    evaluate_pending_predictions("排列5")
    history = fb.load_feedback_history("排列5")
    assert history and all(h.get("档位", TIER_GENERAL) == TIER_GENERAL for h in history)


# ------------------------------------------------------------
# 策略权重学习仅一般档
# ------------------------------------------------------------

def test_weights_ignore_non_general_tiers(tmp_fb):
    """稳健/高鲁棒档记录不参与权重学习（纯非一般档 → 回退均分默认）。"""
    history = [
        {"档位": "稳健", "策略": "高频策略", "valid_prediction": True,
         "模拟盈亏": 100000.0, "总命中": 5, "总选择": 5, "中奖等级": "直选"},
        {"档位": "高鲁棒", "策略": "遗漏值策略", "valid_prediction": True,
         "模拟盈亏": 100000.0, "总命中": 5, "总选择": 5, "中奖等级": "直选"},
    ]
    for h in history:
        append_feedback("排列5", h)
    weights = update_strategy_weights("排列5", respect_gate=False)
    # 无一般档有效记录 → strategy_scores 为空 → 回退均分默认
    assert set(weights.keys()) == set(fb.DEFAULT_STRATEGIES)
    assert all(abs(w - 1 / 3) < 1e-6 for w in weights.values())


def test_weights_learn_from_general_only(tmp_fb):
    """一般档记录正常参与权重；混入的稳健档高分被忽略。"""
    history = [
        # 一般档：高频策略 10 条大赚
        *[{"档位": "一般", "策略": "高频策略", "valid_prediction": True,
           "模拟盈亏": 100.0, "总命中": 3, "总选择": 5, "中奖等级": "未中"}
          for _ in range(10)],
        # 一般档：遗漏策略 10 条亏
        *[{"档位": "一般", "策略": "遗漏值策略", "valid_prediction": True,
           "模拟盈亏": -2.0, "总命中": 0, "总选择": 5, "中奖等级": "未中"}
          for _ in range(10)],
        # 稳健档：区间均衡暴赚（必须被忽略，否则区间均衡权重最高）
        *[{"档位": "稳健", "策略": "区间均衡策略", "valid_prediction": True,
           "模拟盈亏": 100000.0, "总命中": 5, "总选择": 5, "中奖等级": "直选"}
          for _ in range(10)],
    ]
    for h in history:
        append_feedback("排列5", h)
    weights = update_strategy_weights("排列5", respect_gate=False)
    assert weights["高频策略"] > weights["遗漏值策略"]
    # 稳健档暴赚未计入：区间均衡只有稳健档记录 → 不出现在权重里（get 默认 0）
    assert weights.get("区间均衡策略", 0.0) < weights["高频策略"]


# ------------------------------------------------------------
# 三档对比
# ------------------------------------------------------------

def test_tier_comparison_aggregates(tmp_fb):
    """tier_comparison 三档各自聚合（注数/中奖/得分）。"""
    history = [
        {"档位": "一般", "策略": "高频策略", "valid_prediction": True, "期号": 100,
         "总命中": 2, "总选择": 5, "中奖等级": "未中"},
        {"档位": "稳健", "策略": "共识集成", "valid_prediction": True, "期号": 100,
         "总命中": 3, "总选择": 5, "中奖等级": "未中", "模拟盈亏": -2.0},
        {"档位": "高鲁棒", "策略": "共识集成", "valid_prediction": True, "期号": 100,
         "总命中": 5, "总选择": 5, "中奖等级": "直选", "模拟盈亏": 99998.0},
    ]
    for h in history:
        append_feedback("排列5", h)
    comp = tier_comparison("排列5")
    tiers = comp["tiers"]
    assert tiers["一般"]["预测注数"] == 1
    assert tiers["稳健"]["预测注数"] == 1
    assert tiers["高鲁棒"]["预测注数"] == 1
    assert tiers["高鲁棒"]["中奖次数"] == 1
    assert tiers["稳健"]["平均模拟盈亏"] == -2.0
    assert "不代表未来中奖概率" in comp["说明"]


def test_summary_weights_exclude_tiers(tmp_fb):
    """反馈摘要的权重展示不受非一般档记录影响。"""
    append_feedback("排列5", {"档位": "稳健", "策略": "高频策略",
                              "valid_prediction": True, "模拟盈亏": 5.0,
                              "总命中": 2, "总选择": 5, "中奖等级": "未中"})
    summary = get_feedback_summary("排列5")
    assert summary["strategies"] == {} or all(
        k in fb.DEFAULT_STRATEGIES for k in summary["strategies"])
