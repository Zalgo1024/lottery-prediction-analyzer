# -*- coding: utf-8 -*-
"""「出号数量 = 最终注数」的回归锁（2026-10-21）。

真实事故：`predict("双色球", groups=200)` 会偶尔只产出 **198** 组。
成因不是随机性，而是三处**结构性收缩**叠加：
  1) 三个策略都从 Top30 热门号里抽样，200 组时号码组合已经饱和；
  2) `_dedup_and_record` 要求两两红球重叠 ≤3，越到后面越难满足；
  3) `_apply_prune` 的第 2 阶段扫完整个候选表也凑不满 target。
后果不是"少 2 注"这么轻：生产复用判据是「pending 组数 == 当前出号数量」
（web/utils.py），少注 ⇒ 每一轮流水线都判定"不一致"⇒ 重新生成 ⇒
**下一期预测号码被无声改写**（上午看到和晚上看到的是两套号）。

本文件锁死 `prediction.engine._pad_to_groups` 的契约：出口组数恒等于请求值。
"""

import random

import pytest

from data.schema import get_schema
from prediction.engine import PredictSet, _pad_to_groups


def _distinct_redblue(count, seed=20261021):
    """造 count 组互不相同的红蓝票（模拟策略已产出的真实分布）。"""
    rng = random.Random(seed)
    seen, out = set(), []
    while len(out) < count:
        reds = tuple(sorted(rng.sample(range(1, 34), 6)))
        blues = (rng.randint(1, 16),)
        if (reds, blues) in seen:
            continue
        seen.add((reds, blues))
        out.append(PredictSet(reds=list(reds), blues=list(blues), strategy="高频策略"))
    return out


def test_shortfall_is_padded_to_exact_target():
    """198 → 200：少几注必须补齐（这是复用判据成立的前提）。"""
    sets = _distinct_redblue(198)
    out, kpi = _pad_to_groups(sets, 200, get_schema("双色球"), lottery_name="双色球")
    assert len(out) == 200
    assert kpi["随机补齐"] == 2
    assert kpi["回填剪枝票"] == 0


def test_padded_tickets_are_distinct_and_legal():
    """补齐出来的票必须合法（红 6 / 蓝 1）且互不重复。"""
    sets = _distinct_redblue(190)
    out, _ = _pad_to_groups(sets, 200, get_schema("双色球"), lottery_name="双色球")
    assert len(out) == 200
    keys = set()
    for ps in out:
        assert len(ps.reds) == 6 and len(set(ps.reds)) == 6
        assert all(1 <= n <= 33 for n in ps.reds)
        assert len(ps.blues) == 1 and 1 <= ps.blues[0] <= 16
        keys.add((tuple(sorted(ps.reds)), tuple(ps.blues)))
    assert len(keys) == 200, "补齐后出现重复票"


def test_refill_pool_is_used_before_random():
    """优先回填被剪枝掉的票（保留原策略打分），而不是直接随机补。"""
    kept = _distinct_redblue(195)
    dropped = _distinct_redblue(5, seed=99)
    out, kpi = _pad_to_groups(kept, 200, get_schema("双色球"),
                              lottery_name="双色球", refill_pool=dropped)
    assert len(out) == 200
    assert kpi["回填剪枝票"] == 5
    assert kpi["随机补齐"] == 0
    assert {id(p) for p in dropped}.issubset({id(p) for p in out})


def test_already_full_is_a_noop():
    sets = _distinct_redblue(200)
    out, kpi = _pad_to_groups(sets, 200, get_schema("双色球"), lottery_name="双色球")
    assert len(out) == 200
    assert kpi["随机补齐"] == 0 and kpi["回填剪枝票"] == 0 and kpi["截断"] == 0


def test_overflow_is_truncated():
    sets = _distinct_redblue(205)
    out, kpi = _pad_to_groups(sets, 200, get_schema("双色球"), lottery_name="双色球")
    assert len(out) == 200
    assert kpi["截断"] == 5


def test_digital_schema_is_padded_per_position():
    """数字型按 schema 逐位补（每位 0-9 可重复），不能退化成红蓝口径。"""
    rng = random.Random(7)
    seen, sets = set(), []
    while len(sets) < 45:
        digits = tuple(rng.randint(0, 9) for _ in range(3))
        if digits in seen:
            continue
        seen.add(digits)
        zones = {"第1位": [digits[0]], "第2位": [digits[1]], "第3位": [digits[2]]}
        sets.append(PredictSet(zones=zones, strategy="高频策略"))
    out, kpi = _pad_to_groups(sets, 50, get_schema("排列3"), lottery_name="排列3")
    assert len(out) == 50
    assert kpi["随机补齐"] == 5
    for ps in out:
        assert set(ps.zones) == {"第1位", "第2位", "第3位"}
        assert all(0 <= v[0] <= 9 for v in ps.zones.values())


def test_score_fn_is_applied_to_padded_tickets():
    """补齐票也要有置信度/评分明细，否则前端排序与归因会出现 None。"""
    sets = _distinct_redblue(199)
    out, _ = _pad_to_groups(
        sets, 200, get_schema("双色球"), lottery_name="双色球",
        score_fn=lambda ps: (0.1234, {"来源": "补齐打分"}),
    )
    pad = [p for p in out if p.strategy == "随机补齐"]
    assert len(pad) == 1
    assert pad[0].confidence == pytest.approx(0.1234)
    assert pad[0].score_detail == {"来源": "补齐打分"}


def test_zero_or_none_groups_is_pass_through():
    sets = _distinct_redblue(3)
    out0, _ = _pad_to_groups(sets, 0, get_schema("双色球"), lottery_name="双色球")
    outn, _ = _pad_to_groups(sets, None, get_schema("双色球"), lottery_name="双色球")
    assert len(out0) == 3 and len(outn) == 3


def test_predict_sets_target_count_for_redblue():
    """红/蓝路径必须写「目标注数」（2026-10-21）。

    此前只有数字型路径写该字段 ⇒ `record_pending_prediction` 调
    `compress_tickets(target_count=None)` 会退回比例口径，在"质量线 + 重叠"处
    **真减注**（回填只保底 min_keep）⇒ pending 组数 < 滑轨 ⇒ 复用判据永不成立。
    """
    from prediction.engine import predict

    p = predict("大乐透", groups=6, mode="fresh", record_pending=False)
    assert p["目标注数"] == 6
    assert p["号码组数"] == len(p["预测号码"]) == 6
