# -*- coding: utf-8 -*-
"""tests/test_arena_report.py —— 报告措辞与判决矩阵的回归钉。

重点不是排版，是**三件不许退化的性质**：
1. 「不可判」必须带免责句，不能被读成「无效」；
2. 净收益侧被单次大奖支配时必须**强制降级**；
3. 判决矩阵必须覆盖全部 3×3 组合，缺一个就抛。
"""
from __future__ import annotations

import pytest

from ev.arena import report as RPT
from ev.arena import runner as RUN

HIT = ("改善", "不可判", "恶化")
NET = ("改善", "不可判", "恶化")


def test_matrix_covers_all_nine():
    assert len(RPT.AO_VERDICT_TEXT) == 9
    for h in HIT:
        for n in NET:
            assert (h, n) in RPT.AO_VERDICT_TEXT


def test_matrix_rejects_unknown_pair():
    with pytest.raises(ValueError, match="未知判决组合"):
        RPT.verdict_matrix("改善", "大幅改善")


def test_dominant_single_prize_forces_downgrade():
    """单次大奖占总实得 > 0.3 ⇒ 净收益「改善」强制变「不可判」。"""
    r = RPT.verdict_matrix("不可判", "改善", max_single_share=0.512)
    assert r["净收益侧"] == "不可判"
    assert r["强制降级"] is True
    assert r["措辞"] == RPT.AO_VERDICT_TEXT[("不可判", "不可判")]


def test_no_downgrade_when_share_small():
    r = RPT.verdict_matrix("不可判", "改善", max_single_share=0.12)
    assert r["净收益侧"] == "改善"
    assert r["强制降级"] is False


def test_undecidable_text_never_reads_as_invalid():
    """「不可判/不可判」的措辞必须显式否掉「无效」这个读法。"""
    t = RPT.AO_VERDICT_TEXT[("不可判", "不可判")]
    assert "不是「策略无效」" in t


def test_hit_improve_net_undecidable_forbids_revenue_claim():
    """命中侧可判、净收益不可判时，措辞必须禁止「提高收益」这类说法。"""
    t = RPT.AO_VERDICT_TEXT[("改善", "不可判")]
    assert "不得出现「提高收益」" in t


def test_verdict_cell_takes_most_conservative():
    run = {
        "主假设": True,
        "臂": {
            "a": {"配对": {"判决": "可判"}},
            "b": {"配对": {"判决": "边缘"}},
            "c": {"配对": {"判决": "不可判"}},
        },
    }
    assert RPT.verdict_cell(run) == "不可判"


def test_verdict_cell_uses_preregistered_arm():
    """★ 指定了主假设臂就必须只按它判 —— 否则最差的臂会决定主格。"""
    run = {
        "主假设": True,
        "臂": {
            "union_greedy": {"配对": {"判决": "可判"}},
            "tier_coverage": {"配对": {"判决": "不可判"}},
            "lowcrowd": {"配对": {"判决": "可判"}},
        },
    }
    cell = {"主假设臂": "union_greedy"}
    assert RPT.verdict_cell(run, cell) == "可判"
    assert RPT.verdict_cell(run) == "不可判"      # 未指定 ⇒ 退回保守


def test_verdict_cell_reports_missing_primary_arm():
    run = {"主假设": True, "臂": {"x": {"配对": {"判决": "可判"}}}}
    out = RPT.verdict_cell(run, {"主假设臂": "union_greedy"})
    assert "缺失" in out


def test_verdict_cell_non_primary():
    assert "描述性" in RPT.verdict_cell({"主假设": False, "臂": {}})


def test_header_echoes_preregistration_hash():
    doc = {
        "版本": "1.0", "锁定日": "2026-10-24",
        "哈希": "0123456789abcdef0123456789abcdef",
        "基线臂": "random", "臂": ["random"],
        "常量": {"候选池规模": 8000, "重叠上限": 3, "MC拟合采样数": 100000,
                 "MC评估采样数": 100000, "评估seed偏移": 10000},
    }
    txt = "\n".join(RPT.header(doc, ["random"]))
    assert "0123456789abcdef" in txt
    assert "不可判" in txt and "不等于「策略无效」" in txt


# ---------------------------------------------------------------- 净收益侧两级集中度守门
def test_money_gate_level2_blocks_baseline_jackpot():
    """★ 级 2 必须能拦下「基线单独一期中大奖把 Δ 撑起来」。

    主格实测的真实数字：`random` 一注二等 134,885 元只占它自己总实得 27.5%
    ⇒ 级 1 放行；但它贡献 Δ 的 92% ⇒ 级 2 必须拦。
    """
    mp = {"实测Δ": -41.62, "z_实测": -1.08, "单期占Δ比": 0.9207,
          "最大配对期": 20128, "Δ_剔除最大配对期": -3.30}
    money = {"单次大奖占总实得比": 0.2748, "净收益": -916_350,
             "净收益每100元": -65.12, "净收益每100元_剔除最大一期": -74.70}
    out = RUN.money_gate(dict(mp), money)
    assert out["判决"] == "不可判"
    assert "单期支配" in out["集中度守门"]
    assert "92%" in out["集中度守门"]


def test_money_gate_passes_when_spread_out():
    mp = {"实测Δ": -41.62, "z_实测": -1.08, "单期占Δ比": 0.20,
          "最大配对期": 20128, "Δ_剔除最大配对期": -40.0}
    money = {"单次大奖占总实得比": 0.05, "净收益": -916_350,
             "净收益每100元": -65.12, "净收益每100元_剔除最大一期": -64.0}
    out = RUN.money_gate(dict(mp), money)
    assert out["集中度守门"] == "通过"


def test_money_gate_level1_still_wins_when_both_fire():
    """两级都触发时报级 1（更早、更直白的那条）。"""
    mp = {"实测Δ": 300.0, "z_实测": 0.99, "单期占Δ比": 1.01,
          "最大配对期": 24001, "Δ_剔除最大配对期": -3.43}
    money = {"单次大奖占总实得比": 0.739, "净收益": 31_427,
             "净收益每100元": 2.67, "净收益每100元_剔除最大一期": -73.23}
    out = RUN.money_gate(dict(mp), money)
    assert out["判决"] == "不可判"
    assert "大奖占总实得比" in out["集中度守门"]
    assert "73.9%" in out["集中度守门"]


def test_money_gate_flags_identity_violation():
    """★ |z|>4 判实现缺陷（期望差恒为 0），不能读成效应。"""
    mp = {"实测Δ": -41.62, "z_实测": 5.2, "单期占Δ比": 0.1,
          "最大配对期": 1, "Δ_剔除最大配对期": -40.0}
    money = {"单次大奖占总实得比": 0.01, "净收益": -1,
             "净收益每100元": -75.0, "净收益每100元_剔除最大一期": -75.0}
    out = RUN.money_gate(dict(mp), money)
    assert "实现缺陷" in out["★恒等式判据"]
