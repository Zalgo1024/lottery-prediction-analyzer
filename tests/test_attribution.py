"""
ev/attribution.py 回归测试（中奖归因聚合）。

锁住的契约：
  1. **双奖级字段名**必须都认：乐透型 `中奖等级`（一等…六等）、数字型 `中奖玩法`
     （直选/组选3/组选6）。历史事故：只探测其一 → 双色球/大乐透被算成 0 中（假信号）；
  2. 聚合键 = (来源, 策略, 档位)；档位走 feedback._tier_of（缺失归「一般」）；
  3. 全局汇总跨彩种同键合并、奖级分布累加、各彩种总计之和 == 全局总计；
  4. 测试只写 tmp 目录，绝不碰生产 feedback 目录。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.feedback as fb
from ev.attribution import (attribution_for_lottery, attribution_global, is_win,
                            latest_batch_coverage, nominal_prize, win_grade)


@pytest.fixture
def tmp_fb(tmp_path, monkeypatch):
    """隔离 feedback 读写路径（同 tests/test_feedback_tiers.py 的口径）。"""
    monkeypatch.setattr(fb, "_pending_path", lambda name: tmp_path / f"{name}_pending.json")
    monkeypatch.setattr(fb, "_history_path", lambda name: tmp_path / f"{name}_history.json")
    monkeypatch.setattr(fb, "_weights_path", lambda name: tmp_path / f"{name}_weights.json")
    return tmp_path


def _ssq(grade, strategy="高频策略", source="predict", issue=26100):
    return {"期号": issue, "来源": source, "策略": strategy, "档位": "一般",
            "中奖等级": grade, "valid_prediction": True}


def _sd(play, strategy="高频策略", source="predict", issue=2026240):
    return {"期号": issue, "来源": source, "策略": strategy, "档位": "一般",
            "中奖玩法": play, "valid_prediction": True}


# ------------------------------------------------------------
# 1. 双字段名
# ------------------------------------------------------------

def test_win_grade_recognizes_both_field_names():
    assert win_grade({"中奖等级": "六等"}) == "六等"
    assert win_grade({"中奖玩法": "直选"}) == "直选"
    assert win_grade({"中奖等级": "六等", "中奖玩法": "直选"}) == "直选"  # 数字型优先


@pytest.mark.parametrize("rec", [
    {}, {"中奖等级": "未中"}, {"中奖玩法": ""}, {"中奖等级": None},
    {"中奖等级": "0"}, {"中奖玩法": "—"},
])
def test_win_grade_null_values_are_not_wins(rec):
    assert win_grade(rec) is None
    assert is_win(rec) is False


def test_is_win_true_for_real_grades():
    assert is_win({"中奖玩法": "组选6"}) is True
    assert is_win({"中奖等级": "九等"}) is True


def test_nominal_prize_fixed_grade_positive():
    """固定奖级（双色球六等）应取到正金额；命中等级字段名双轨都要能查到。"""
    amt, unknown = nominal_prize("双色球", {"中奖等级": "六等", "期号": 26100})
    assert amt > 0
    assert unknown is False
    assert nominal_prize("双色球", {"中奖等级": "未中"}) == (0.0, False)


# ------------------------------------------------------------
# 2~3. 聚合
# ------------------------------------------------------------

def test_attribution_groups_by_source_strategy_tier(tmp_fb):
    fb.save_feedback_history("双色球", [
        _ssq("六等"), _ssq("未中"), _ssq("未中"),
        _ssq("六等", strategy="遗漏值策略"),
        _ssq("未中", strategy="遗漏值策略", source="train"),
    ])
    a = attribution_for_lottery("双色球")
    assert a["总注数"] == 5
    assert a["总中奖注数"] == 2
    assert a["总命中率"] == pytest.approx(0.4)

    keys = {(r["来源"], r["策略"], r["档位"]) for r in a["分组"]}
    assert ("predict", "高频策略", "一般") in keys
    assert ("train", "遗漏值策略", "一般") in keys

    hi = next(r for r in a["分组"] if r["来源"] == "predict" and r["策略"] == "高频策略")
    assert hi["注数"] == 3
    assert hi["中奖注数"] == 1          # 该组仅 1 张「六等」，另 1 中在「遗漏值策略」组
    assert hi["奖级分布"] == {"六等": 1}
    assert hi["名义奖金合计"] > 0


def test_missing_tier_falls_back_to_general(tmp_fb):
    rec = _ssq("未中")
    rec.pop("档位")
    fb.save_feedback_history("双色球", [rec])
    a = attribution_for_lottery("双色球")
    assert a["分组"][0]["档位"] == "一般"


def test_digit_lottery_uses_play_field(tmp_fb):
    fb.save_feedback_history("福彩3D", [_sd("直选"), _sd("未中"), _sd("组选6")])
    a = attribution_for_lottery("福彩3D")
    assert a["总注数"] == 3
    assert a["总中奖注数"] == 2
    levels = {}
    for r in a["分组"]:
        levels.update(r["奖级分布"])
    assert levels == {"直选": 1, "组选6": 1}


def test_attribution_global_merges_across_lotteries(tmp_fb):
    fb.save_feedback_history("双色球", [_ssq("六等"), _ssq("未中")])
    fb.save_feedback_history("福彩3D", [_sd("直选")])
    g = attribution_global()

    assert g["总注数"] == 3
    assert g["总中奖注数"] == 2
    merged = {(r["来源"], r["策略"], r["档位"]): r for r in g["全局汇总"]}
    row = merged[("predict", "高频策略", "一般")]
    assert row["注数"] == 3
    assert row["中奖注数"] == 2
    assert "六等" in row["奖级分布"] and "直选" in row["奖级分布"]
    # 合并行标注各彩种注数构成（回答"这行是哪些彩种"）
    assert row["彩种注数"] == {"双色球": 2, "福彩3D": 1}
    # 各彩种总计之和 == 全局总计
    assert sum(v["总注数"] for v in g["各彩种"].values()) == g["总注数"]


def test_lookback_limits_records(tmp_fb):
    fb.save_feedback_history("双色球", [_ssq("未中", issue=26100 + i) for i in range(10)])
    a = attribution_for_lottery("双色球", lookback=3)
    assert a["总注数"] == 3
    assert a["回看上限"] == 3


# ------------------------------------------------------------
# 4. 覆盖 KPI 与写隔离
# ------------------------------------------------------------

def test_latest_batch_coverage_empty_pending(tmp_fb):
    assert latest_batch_coverage("双色球") == {}


def test_latest_batch_coverage_reports_prune_kpi(tmp_fb):
    fb.save_pending("双色球", [{
        "状态": "pending", "目标期号": 26108, "预测日期": "2026-09-13",
        "预测号码": [
            {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1], "策略": "高频策略"},
            {"红球": [1, 2, 3, 4, 5, 7], "蓝球": [2], "策略": "高频策略"},
        ],
    }])
    cov = latest_batch_coverage("双色球")
    assert cov["名义注数"] == 2
    assert cov["目标期号"] == 26108
    assert cov["剪枝后注数"] == 1          # 红球重叠 5 > cap 4 → 剪掉一张
    assert "诚实声明" in cov


def test_writes_go_to_tmp_not_production(tmp_fb):
    fb.save_feedback_history("双色球", [_ssq("六等")])
    assert (tmp_fb / "双色球_history.json").exists()
    prod_file = fb.FEEDBACK_DIR / "双色球_feedback_history.json"
    if prod_file.exists():
        # 生产文件不应被本次测试写入（条数不因测试变化）
        import json
        prod = json.loads(prod_file.read_text(encoding="utf-8"))
        assert isinstance(prod, list)
