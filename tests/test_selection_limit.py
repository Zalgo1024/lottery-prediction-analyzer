"""tests/test_selection_limit.py —— Step B（限号前置）+ Step F（组合方差指标）

覆盖四组契约：
  1. 七星彩逐注路径：排序键=限号EV（回归保护），选中行回填 限号暴露概率/μ，
     返回 dict 含「限号修正」与「组合方差指标」。
  2. 固定赔率拒绝：含 --sample 指引 + 限号修正标注。
  3. _portfolio_metrics：同注集 m_eff→1、零重叠 m_eff=m、口径声明「只降方差」。
  4. 大乐透（浮动奖池制）返回含 限号修正=不适用 + 组合方差指标。
"""
import pytest

from ev import selection


# ---------------- 组 1：七星彩限号前置 ----------------
def test_qxc_rows_carry_limit_keys_and_sort_by_limit_ev(monkeypatch):
    """排序键必须仍是限号EV（回归保护），且限号明细前置存储后回填到选中行。"""
    import ev.engine as engine

    def fake_net_ev(lottery, nums, issue=None):
        if nums == [0] * 7:
            return {"名义EV": -1.0, "限号EV": -1.01, "限号暴露概率": 0.1,
                    "机制": "mock", "详情": {"同号期望注数μ": 1.0}}
        if nums == [1, 1, 1, 0, 0, 0, 1]:
            return {"名义EV": -1.0, "限号EV": -1.05, "限号暴露概率": 0.5,
                    "机制": "mock", "详情": {"同号期望注数μ": 5.0}}
        return {"名义EV": -1.0}          # 无 限号EV → 与真实 error/降级路径一致地被跳过

    monkeypatch.setattr(engine, "net_ev", fake_net_ev)
    cands = [
        {"第1位..第7位": None, "号码": [0] * 7},
        {"第1位..第7位": None, "号码": [1, 1, 1, 0, 0, 0, 1]},   # 与 t0 按位重叠=3 ≤ cap
    ]
    r = selection.select_tickets("七星彩", n=2, candidates=cands, seed=1)

    assert r["选中数"] == 2
    assert r["限号修正"] == "已前置（排序键=限号EV）"
    rows = r["选中"]
    # 回归保护：限号EV 最高的 t0 排第一（排序键=限号EV，未被新键破坏）
    assert rows[0]["号码"]["号码"] == [0] * 7
    assert rows[0]["净EV"] == pytest.approx(-1.01)
    assert rows[0]["限号暴露概率"] == pytest.approx(0.1)
    assert rows[0]["μ"] == pytest.approx(1.0)
    assert rows[1]["限号暴露概率"] == pytest.approx(0.5)
    assert rows[1]["μ"] == pytest.approx(5.0)
    # 组合方差指标挂到返回 dict
    assert r["组合方差指标"]["注数"] == 2


# ---------------- 组 2：固定赔率拒绝 ----------------
def test_fixed_odds_rejection_keeps_sample_pointer():
    r = selection.select_tickets("排列3", n=2)
    assert r.get("拒绝")
    assert "--sample" in r["原因"]
    assert "固定赔率" in r["限号修正"]


# ---------------- 组 3：组合方差指标 ----------------
def test_portfolio_metrics_identical_tickets_collapse():
    """两注红球全同 → oᵢⱼ=1 → m_eff=1（方差无分散，诚实揭示）。"""
    from ev.selection import _portfolio_metrics
    same = [
        {"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}},
        {"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [2]}},
    ]
    m = _portfolio_metrics(same, "双色球")
    assert m["注数"] == 2
    assert m["有效独立注数"] == pytest.approx(1.0)
    assert m["最大重叠"] == 6
    assert m["平均重叠"] == pytest.approx(6.0)
    assert "只降方差" in m["口径"]


def test_portfolio_metrics_disjoint_tickets_full_independence():
    """两注红球零交集 → m_eff=m=2（独立度最高）。"""
    from ev.selection import _portfolio_metrics
    dis = [
        {"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}},
        {"号码": {"红球": [7, 8, 9, 10, 11, 12], "蓝球": [1]}},
    ]
    m = _portfolio_metrics(dis, "双色球")
    assert m["有效独立注数"] == pytest.approx(2.0)
    assert m["最大重叠"] == 0
    assert m["平均重叠"] == pytest.approx(0.0)


def test_portfolio_metrics_single_and_empty():
    from ev.selection import _portfolio_metrics
    one = _portfolio_metrics([{"号码": {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}}], "双色球")
    assert one["有效独立注数"] == pytest.approx(1.0)
    empty = _portfolio_metrics([], "双色球")
    assert empty["注数"] == 0 and empty["有效独立注数"] is None


# ---------------- 组 4：浮动奖池制标注 ----------------
def test_dlt_return_has_limit_note_and_portfolio():
    r = selection.select_tickets("大乐透", n=2, pool_size=30, seed=5)
    assert r["限号修正"] == "不适用（浮动奖池制，无固定赔付限额）"
    pm = r["组合方差指标"]
    assert pm["注数"] == r["选中数"]
    assert 1.0 <= pm["有效独立注数"] <= r["选中数"]
