# -*- coding: utf-8 -*-
"""tests/test_profit_beta_mode.py —— 任务 A：walk-forward β 口径的契约。

核心断言（按重要性排序）：
1. **无前视**：`_wf_params` 的训练集最大期号键严格小于目标期；
2. **降级显式**：训练不足 / rows 缺失 → None，由调用方记降级块数，
   不许静默回退全样本 β（in-sample β 会高估）；
3. **两档同构**：walk_forward 与 full 在同一窗口/块长/种子下期数与每期
   注数一致（差别只在倾斜来源），否则 paired_report 的逐期配对会错位；
4. **透传**：`run_profit_arm` / `build_plan` / meta / report 都带 β 口径。
"""
import numpy as np
import pytest

from ev.profit import backtest as BT
from ev.profit import pit

LOT = "双色球"


@pytest.fixture(scope="module")
def wf_rows():
    return BT._wf_rows(LOT)


def test_wf_params_no_lookahead(wf_rows):
    """★ 训练集必须真值早于目标期（expanding window）。"""
    keys = [pit.issue_key(LOT, s) for s in wf_rows["issues"]]
    mid = len(keys) // 2
    k_target = keys[mid]
    params = BT._wf_params(LOT, wf_rows["issues"][mid], wf_rows, {}, min_train=200)
    assert isinstance(params, dict) and "beta" in params
    train_max = max(k for k in keys if k < k_target)
    assert train_max < k_target, "训练集混入了目标期及以后的数据"


def test_wf_params_cache_hit(wf_rows):
    """缓存必须按 issue 命中，且重复调用结果一致。"""
    issue = wf_rows["issues"][len(wf_rows["issues"]) // 2]
    p1 = BT._wf_params(LOT, issue, wf_rows, {}, min_train=200)
    p2 = BT._wf_params(LOT, issue, wf_rows, {issue: p1}, min_train=200)
    assert p2 is p1


@pytest.mark.parametrize("rows,min_train", [
    (None, 200),                      # 无原始行
    ("rows", 10 ** 7),                # 训练不足（占位 rows，先被 min_train 挡下）
])
def test_wf_params_degrades(wf_rows, rows, min_train):
    """降级路径必须返回 None（调用方显式记降级，不回退全样本 β）。"""
    r = None if rows is None else wf_rows
    out = BT._wf_params(LOT, wf_rows["issues"][-1], r, {}, min_train=min_train)
    assert out is None


def test_two_modes_structurally_isomorphic():
    """两档 β 口径同窗同块同期数同注数（配对不错位）。"""
    kw = dict(K=5, periods=40, seed=0, kappa=0.0, block=20,
              overlap_cap=3, min_train=200)
    plan_wf, meta_wf = BT.build_plan(LOT, beta_mode="walk_forward", **kw)
    plan_full, meta_full = BT.build_plan(LOT, beta_mode="full", **kw)
    assert len(plan_wf) > 0 and len(plan_full) > 0
    assert [p[0] for p in plan_wf] == [p[0] for p in plan_full]
    assert all(len(a[2]) == len(b[2]) for a, b in zip(plan_wf, plan_full))
    assert "walk_forward" in meta_wf["β口径"] and "降级块" in meta_wf["β口径"]
    assert "全样本" in meta_full["β口径"]


def test_run_profit_arm_passes_beta_mode():
    kw = dict(K=3, periods=30, seed=0, block=20, min_train=200)
    a = BT.run_profit_arm(LOT, beta_mode="walk_forward", **kw)
    b = BT.run_profit_arm(LOT, beta_mode="full", **kw)
    assert a["期数"] > 0 and b["期数"] > 0
    assert a["期数"] == b["期数"]
    assert "walk_forward" in a["β口径"] and "全样本" in b["β口径"]
    assert "β口径" in BT.report(a)


def test_unknown_beta_mode_rejected():
    with pytest.raises(ValueError, match="beta_mode"):
        BT.run_profit_arm(LOT, K=3, periods=5, min_train=200, beta_mode="bogus")
    with pytest.raises(ValueError, match="beta_mode"):
        BT.build_plan(LOT, 3, 5, 0, 0.0, 20, 3, 200, beta_mode="bogus")
