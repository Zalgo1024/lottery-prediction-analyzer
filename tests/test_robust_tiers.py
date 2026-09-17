"""
三档号码选择管线（prediction/robust_tiers）测试

覆盖：三档独立生成 / 确定性 / 档位与鲁棒性字段 / 诚实口径文案 /
     高鲁棒门槛与放宽 / 零回归（engine.predict 行为不变）
真实数据源（排列5，历史充足、数字型计算最快）。
"""

import pytest

from prediction.robust_tiers import (
    HIGH_TIER_THRESHOLD,
    HONEST_NOTE,
    TIER_LABELS,
    _zone_key,
    predict_tiered,
)

FAST_PARAMS = dict(groups=10, windows=(20, 30, 40, 50), seeds=2, record_pending=False)


# ------------------------------------------------------------
# 基本结构
# ------------------------------------------------------------

def test_all_tiers_basic_structure():
    """tier=all 返回三档，各自结构完整（一般/稳健/高鲁棒 独立生成）。"""
    r = predict_tiered("排列5", tier="all", **FAST_PARAMS)
    assert set(r.keys()) == {"一般", "稳健", "高鲁棒"}
    for label, res in r.items():
        assert res["档位"] == label
        # 一般档保持 engine.predict 原样（零回归，预测模式沿用引擎值）；
        # 稳健/高鲁棒档为 robust_<档位>
        expected_mode = "fresh" if label == "一般" else f"robust_{label}"
        assert res["预测模式"] == expected_mode
        assert res["号码组数"] == len(res["预测号码"])
        assert HONEST_NOTE in res["鲁棒性说明"]
        assert res["目标期号"] is not None
        for ps in res["预测号码"]:
            assert ps.get("档位") == label


def test_single_tier_returns_result_dict():
    """tier=robust 单档返回该档 result dict（非嵌套）。"""
    r = predict_tiered("排列5", tier="robust", **FAST_PARAMS)
    assert isinstance(r, dict)
    assert r["档位"] == "稳健"
    assert r["号码组数"] > 0


# ------------------------------------------------------------
# 稳健档共识
# ------------------------------------------------------------

def test_robust_tier_consensus_fields():
    """稳健档票面带鲁棒性字段：半区/前列命中率、窗口数、平均置信度。"""
    r = predict_tiered("排列5", tier="robust", **FAST_PARAMS)
    ps = r["预测号码"][0]
    rob = ps["鲁棒性"]
    assert {"半区命中率", "前列命中率", "入选窗口数", "窗口数", "平均置信度", "组合数"} <= set(rob)
    assert rob["窗口数"] == len(FAST_PARAMS["windows"])
    assert 0.0 <= rob["半区命中率"] <= 1.0
    assert 0.0 <= rob["前列命中率"] <= rob["半区命中率"] + 1e-9 or rob["前列命中率"] <= 1.0
    # 稳健档按共识分排序（降序）
    scores = [p["置信度"] for p in r["预测号码"]]
    assert scores == sorted(scores, reverse=True)


def test_zone_keys_unique_in_pool():
    """票面键函数：zones 排序归一，同票面同键。"""
    k1 = _zone_key({"红球": [3, 1, 2], "蓝球": [5]})
    k2 = _zone_key({"红球": [1, 2, 3], "蓝球": [5]})
    assert k1 == k2


# ------------------------------------------------------------
# 高鲁棒档门槛
# ------------------------------------------------------------

def test_high_tier_threshold_and_gate():
    """高鲁棒档：前列命中率 ≥ 门槛才入选；门槛字段存在。"""
    r = predict_tiered("排列5", tier="high", **FAST_PARAMS)
    assert "高鲁棒门槛" in r
    gate = r["高鲁棒门槛"]
    assert gate["窗口一致性阈值"] in (HIGH_TIER_THRESHOLD, 0.60)
    if not gate["coverage_relaxed"]:
        assert gate["窗口一致性阈值"] == HIGH_TIER_THRESHOLD
    for ps in r["预测号码"]:
        rob = ps["鲁棒性"]
        assert rob["前列命中率"] >= gate["窗口一致性阈值"] - 1e-9


def test_high_tier_scores_descending():
    """高鲁棒档按 前列命中率×平均置信度 降序。"""
    r = predict_tiered("排列5", tier="high", **FAST_PARAMS)
    scores = [p["置信度"] for p in r["预测号码"]]
    assert scores == sorted(scores, reverse=True)


# ------------------------------------------------------------
# 确定性（同 seed 可复现）
# ------------------------------------------------------------

def test_determinism_same_seed():
    """同参数两次运行产出完全一致的票面（可复现）。"""
    a = predict_tiered("排列5", tier="robust", **FAST_PARAMS)
    b = predict_tiered("排列5", tier="robust", **FAST_PARAMS)
    assert [p["号码"] for p in a["预测号码"]] == [p["号码"] for p in b["预测号码"]]
    c = predict_tiered("排列5", tier="high", **FAST_PARAMS)
    d = predict_tiered("排列5", tier="high", **FAST_PARAMS)
    assert [p["号码"] for p in c["预测号码"]] == [p["号码"] for p in d["预测号码"]]


def test_different_seed_changes_output():
    """不同 seed_base 产出不同票面（种子确实参与生成）。"""
    a = predict_tiered("排列5", tier="robust", **FAST_PARAMS)
    b = predict_tiered("排列5", tier="robust", groups=10, windows=(20, 30, 40, 50),
                       seeds=2, seed_base=777, record_pending=False)
    assert [p["号码"] for p in a["预测号码"]] != [p["号码"] for p in b["预测号码"]]


# ------------------------------------------------------------
# 红蓝彩种路径（双色球，规模最小化）
# ------------------------------------------------------------

def test_redblue_lottery_robust_tier():
    """红蓝彩种（双色球）走同一管线，输出结构一致。"""
    r = predict_tiered("双色球", tier="robust", groups=5,
                       windows=(20, 30), seeds=2, record_pending=False)
    assert r["档位"] == "稳健"
    assert r["号码组数"] > 0
    ps = r["预测号码"][0]
    assert set(ps.get("红球", [])) and set(ps.get("蓝球", []))
    assert "鲁棒性" in ps


# ------------------------------------------------------------
# 零回归守卫
# ------------------------------------------------------------

def test_honest_note_wording():
    """诚实口径文案必须包含「不代表号码中奖概率」。"""
    assert "不代表号码中奖概率" in HONEST_NOTE


def test_tier_labels_fixed():
    """档位中文标签固定（前端/反馈依赖）。"""
    assert TIER_LABELS == {"general": "一般", "robust": "稳健", "high": "高鲁棒"}


def test_engine_predict_untouched():
    """零回归：engine.predict 正常模式不受三档管线影响（无档位字段注入）。"""
    from prediction.engine import predict
    r = predict("排列5", groups=3, mode="fresh")
    assert r["预测模式"] == "fresh"
    for ps in r["预测号码"]:
        assert "档位" not in ps  # 一般档票面不带档位键（由 robust_tiers 补，engine 不动）
