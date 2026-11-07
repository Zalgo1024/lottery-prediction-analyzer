# -*- coding: utf-8 -*-
"""臂间重合 + 零假设（`ev/overlap.py`）的契约锁。

为什么必须带零假设
------------------
外部参考程序只报「两臂 Top-N 重合率」，于是「重合率下降」被当成「策略解耦成功」。
但从 N 张池子里各挑 k 张，即使双方都均匀随机也会自然重合 k²/N 张 ——
不把这个基线减掉，重合率就是一个没有意义的数。

★ 本文件锁三件事：
  ① 零假设期望/方差算对（独立抽样 ⇒ z 在 0 附近；同一批票 ⇒ z 极大）；
  ② 数字型这种「两臂产不出足量不重复票」的彩种必须标不适用，**不许**算出 z；
  ③ 文案里必须有「重合 ≠ 独立 ≠ 有信息」这句判读纪律。
"""
import itertools

import itertools

from ev.coverage_backtest import build_pool
from ev.overlap import NOTE, overlap_stats, ticket_key


# 用组合枚举保证票**互不相同**：早先按 i%28 拼号会撞出重复票，
# 于是「去重后 k」小于预期，期望值算出来对不上（测试自己先失真了）。
_COMBOS = list(itertools.combinations(range(1, 34), 6))


def _t(i):
    """构造第 i 张唯一的双色球票（当独立样本用）。"""
    return {"号码": {"红球": list(_COMBOS[i]), "蓝球": [1]}}


def test_ticket_key_is_order_insensitive():
    a = {"号码": {"红球": [3, 1, 2], "蓝球": [9]}}
    b = {"号码": {"蓝球": [9], "红球": [2, 3, 1]}}
    assert ticket_key(a) == ticket_key(b)


def test_identical_sets_have_huge_z():
    pool = build_pool("双色球", 500, 7)
    s = pool[:100]
    r = overlap_stats(s, s, 500)
    assert r["重合票数"] == 100
    assert r["z"] is not None and r["z"] > 5


def test_disjoint_sets_are_below_expectation():
    pool = build_pool("双色球", 500, 7)
    r = overlap_stats(pool[:100], pool[100:200], 500)
    assert r["重合票数"] == 0
    assert r["零假设期望重合"] > 0
    assert r["z"] is not None and r["z"] < 0


def test_expectation_is_k1_times_k2_over_n():
    """单看公式：期望重合 = k₁k₂/N。"""
    a = [_t(i) for i in range(50)]
    b = [_t(i) for i in range(50, 100)]
    r = overlap_stats(a, b, 1000)
    assert r["零假设期望重合"] == 2.5          # 50×50/1000
    assert r["零假设标准差"] is not None


def test_duplicates_within_an_arm_are_deduped():
    """同一臂里重复的票对「有效容量」贡献为 0，必须先去掉再算 k。"""
    same = _t(1)
    r = overlap_stats([same] * 100, [_t(i) for i in range(100)], 1000)
    assert r["臂A去重票数"] == 1


def test_verdict_wording_is_honest():
    """判读必须带「重合 ≠ 独立 ≠ 有信息」，且低重合不许读成「更准」。"""
    assert "重合 ≠ 独立 ≠ 有信息" in NOTE
    assert "不代表谁更准" in NOTE


def test_arm_overlap_reports_inapplicable_instead_of_z(client=None):
    """数字型两臂只有 1 张有效票 ⇒ 必须标不适用并给原因，不能返回 z=44 这种读数。"""
    from ev.overlap import arm_overlap

    r = arm_overlap("福彩3D", k=200, pool_size=2000)
    assert r["适用"] is False
    assert "不重复票" in r["原因"]


def test_arm_overlap_on_lottery_type_is_reproducible():
    """同池同种子 ⇒ 同结果（读数要可复核）。"""
    from ev.overlap import arm_overlap

    a = arm_overlap("双色球", k=50, pool_size=500, seed=3)
    b = arm_overlap("双色球", k=50, pool_size=500, seed=3)
    assert a["适用"] and b["适用"]
    assert a["重合票数"] == b["重合票数"]
    assert a["z"] == b["z"]
    assert a["注数K"] == 50 and a["候选池"] == 500


def test_endpoint_contract():
    """`/api/audit/overlap/<彩种>` 的字段契约（前端按这些键渲染）。"""
    from web.app import app

    c = app.test_client()
    r = c.get("/api/audit/overlap/双色球")
    assert r.status_code == 200
    d = r.get_json()
    for key in ("适用", "重合票数", "零假设期望重合", "z", "判读", "口径", "说明"):
        assert key in d, "缺字段 %s" % key

    r2 = c.get("/api/audit/overlap/福彩3D")
    d2 = r2.get_json()
    assert d2["适用"] is False and d2["原因"]
