# -*- coding: utf-8 -*-
"""前端 _bundleIntoGroups 的 Python 镜像测试。

JS 渲染函数 _bundleIntoGroups(tickets, groupSize) 把号码按 groupSize 一组打包，
组内按推荐分降序，整组按组内最大推荐分降序，Top1 标 recommended。
为防止后续修改时数值口径漂移，这里用 Python 镜像同一逻辑跑单元测试。
"""
import pytest


def bundle_into_groups(tickets, group_size=5):
    if not tickets:
        return []
    groups = [tickets[i:i + group_size] for i in range(0, len(tickets), group_size)]
    for g in groups:
        g.sort(key=lambda t: t.get("推荐分") or 0, reverse=True)
    decorated = []
    for idx, g in enumerate(groups):
        top = max((t.get("推荐分") or 0) for t in g)
        mean_conf = sum((t.get("置信度") or 0) for t in g) / len(g)
        strategies = "+".join(sorted({t.get("策略", "—") for t in g}))
        decorated.append({
            "idx": idx, "tickets": g, "topScore": top,
            "meanConf": mean_conf, "strategies": strategies,
        })
    decorated.sort(key=lambda x: x["topScore"], reverse=True)
    if decorated:
        decorated[0]["recommended"] = True
    return decorated


def _mk(推荐分, 策略="区间均衡策略", 置信度=0.5):
    return {"推荐分": 推荐分, "策略": 策略, "置信度": 置信度,
            "号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}}


class TestBundleIntoGroups:
    def test_empty_returns_empty(self):
        assert bundle_into_groups([]) == []

    def test_single_ticket_one_group_recommended(self):
        g = bundle_into_groups([_mk(0.1)])
        assert len(g) == 1
        assert g[0]["topScore"] == 0.1
        assert g[0]["recommended"] is True

    def test_groups_of_five_default(self):
        # 12 tickets → 3 groups of [5, 5, 2]（按输入顺序切，最后一组 2 张）；
        # 组按组内最大推荐分降序 → 因为输入推荐分单调递增，末组 top 最大，排首。
        ts = [_mk(i * 0.01) for i in range(12)]
        gs = bundle_into_groups(ts, 5)
        assert [len(g["tickets"]) for g in gs] == [2, 5, 5]
        # 首组 topScore 应是输入最大推荐分 0.11
        assert gs[0]["topScore"] == pytest.approx(0.11)

    def test_groups_sorted_by_top_score_desc(self):
        ts = [_mk(s) for s in [0.1, 0.5, 0.2, 0.9, 0.3, 0.7, 0.4, 0.6, 0.8, 0.05]]
        gs = bundle_into_groups(ts, 5)
        scores = [g["topScore"] for g in gs]
        assert scores == sorted(scores, reverse=True)
        # Top1 must be the group containing 0.9 (it has the global max)
        assert gs[0]["recommended"] is True
        assert gs[0]["topScore"] == 0.9

    def test_within_group_sorted_by_score_desc(self):
        ts = [_mk(0.1), _mk(0.5), _mk(0.2), _mk(0.9), _mk(0.3)]
        gs = bundle_into_groups(ts, 5)
        # 1 group with all 5 tickets, sorted by 推荐分 desc
        order = [t["推荐分"] for t in gs[0]["tickets"]]
        assert order == [0.9, 0.5, 0.3, 0.2, 0.1]

    def test_strategies_aggregated_within_group(self):
        ts = [_mk(0.5, 策略="高频策略"), _mk(0.5, 策略="遗漏值策略"),
              _mk(0.5, 策略="高频策略"), _mk(0.5, 策略="区间均衡策略"),
              _mk(0.5, 策略="遗漏值策略")]
        gs = bundle_into_groups(ts, 5)
        # strategies 用 + 连接，按 sorted unique 去重
        assert gs[0]["strategies"] == "区间均衡策略+遗漏值策略+高频策略"

    def test_mean_confidence(self):
        ts = [_mk(0.5, 置信度=0.4), _mk(0.5, 置信度=0.6),
              _mk(0.5, 置信度=0.5), _mk(0.5, 置信度=0.5),
              _mk(0.5, 置信度=0.5)]
        gs = bundle_into_groups(ts, 5)
        assert gs[0]["meanConf"] == pytest.approx(0.5)

    def test_top1_recommended_others_not(self):
        ts = [_mk(s) for s in [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]]
        gs = bundle_into_groups(ts, 5)
        # 2 groups, top1 = group containing 1.0; second = group with max 0.5
        assert gs[0]["recommended"] is True
        assert gs[1].get("recommended", False) is False

    def test_handles_missing_score(self):
        """缺失 推荐分/置信度/策略 字段不能崩——按 0/— 处理。"""
        ts = [{"推荐分": 0.5}, {"置信度": 0.5}, {"策略": "高频策略"}]
        gs = bundle_into_groups(ts, 5)
        assert len(gs) == 1
        # top=0.5 (from first ticket with 推荐分), meanConf=0.5/3
        assert gs[0]["topScore"] == 0.5
        assert gs[0]["meanConf"] == pytest.approx(0.5 / 3, abs=1e-6)
        # strategies 集合：缺策略字段的记 "—"
        assert gs[0]["strategies"] == "—+高频策略"

    def test_double_recommendation_ties(self):
        """两组 topScore 相等时，仅保留排序第一组 recommended。"""
        ts = [_mk(0.5), _mk(0.5), _mk(0.5), _mk(0.5), _mk(0.5),
              _mk(0.5), _mk(0.5), _mk(0.5), _mk(0.5), _mk(0.5)]
        gs = bundle_into_groups(ts, 5)
        assert gs[0]["recommended"] is True
        # 第二组同分，但只有第一组被标（按 JS 行为）
        assert gs[1].get("recommended", False) is False