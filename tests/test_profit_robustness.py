# -*- coding: utf-8 -*-
"""tests/test_profit_robustness.py —— 四轴稳健性检查 + 反例断言。

锁定的不变量：
1. 双色球真实案例下，四轴良性扰动均不翻车（全部稳定=True）——决策对常数取巧不脆弱。
2. β 形状误差是**真正敏感**的旋钮：过高估计 β（δ>0）使 κ* 下降、过低估计（δ<0）使 κ* 上升
   （反例：若 mock 没接上、扰动是空操作，这个单调性断言会失败）。
3. 翻转检测逻辑双向正确：k_up 高于 κ* 判翻车、低于则不翻（反例：若翻车标志写死，会失败）。
4. 池/销量/税三轴均被正确接线（κ* 有限、不翻车），其常数误差远小于 κ* 缺口，故不影响判决。
"""
import pytest

from ev.profit import pit, payouts as P, tilt as T, crowd_tilt as CT
from ev.profit import select as SEL
from ev.profit import tax as TX
from ev.profit import robustness as RB
from ev.profit.solvers import solve_kappa_star

LOT, ISSUE, K = "双色球", "2026280", 20


@pytest.fixture(scope="module")
def fx():
    view = pit.as_of(LOT, ISSUE, min_history=1200)
    ctx = P.build_ctx(LOT, ISSUE, view=view)
    sel = SEL.select_profit_tickets(LOT, K, ctx, kappa=0.0, seed=0)
    tickets = sel["票"]
    tl = T.outer_tilt(LOT)
    up = (T.kappa_upper_marginal(LOT, tl)
          if tl.source == CT.CROWD_PI_SOURCE else T.kappa_upper(LOT))
    k_up = (up.get("kappa_upper", up.get("kappa")) if up.get("可用") else None)
    return {"view": view, "ctx": ctx, "tickets": tickets, "k_up": k_up}


@pytest.fixture(scope="module")
def res(fx):
    return RB.robustness_check(LOT, ISSUE, K=K, min_history=1200, seed=0)


def test_robustness_all_axes_stable(res):
    assert res["ok"] is True
    assert res["全部稳定"] is True
    assert res["κ*_基准"] is not None
    assert res["机制上界"] is not None


def test_beta_axis_is_sensitive_monotone(res):
    beta = res["β形状误差"]
    assert len(beta) == 2
    base = res["κ*_基准"]
    d_lo, d_hi = beta[0], beta[1]          # [0]=δ=-0.1, [1]=δ=+0.1
    assert d_lo["δ"] == -0.1 and d_hi["δ"] == 0.1
    # 过高估计 β（δ>0）→ κ* 更低（策略看起来更可行）；过低估计 → κ* 更高
    assert d_hi["κ*"] < base < d_lo["κ*"]
    # 且扰动确实改变了 κ*（不是空操作）
    assert d_hi["κ*"] != base and d_lo["κ*"] != base
    assert d_lo["翻车"] is False and d_hi["翻车"] is False


def test_flip_detection_counter_example(fx):
    # 反例：把机制上界抬到高于 κ*，翻转检测必须报翻车；降到远低于则否。
    out_high = RB._solve_kstar(LOT, fx["tickets"], fx["ctx"], k_up=2.0)
    assert out_high["翻车"] is True
    out_low = RB._solve_kstar(LOT, fx["tickets"], fx["ctx"], k_up=0.1)
    assert out_low["翻车"] is False


def test_pool_sales_tax_axes_wired_and_stable(res):
    # 池模型两口径：均有限、不翻车（a1 误差 ≪ κ* 缺口）
    pool = res["池模型两口径"]
    for v in pool.values():
        assert v["κ*"] is not None
        assert v["翻车"] is False
    # 销量 P10/P90
    for v in res["销量P10_P90"]:
        assert v["κ*"] is not None
        assert v["翻车"] is False
    # 税两口径
    tax = res["税两口径"]
    for v in tax.values():
        assert v["κ*"] is not None
        assert v["翻车"] is False
    # 逐注计税是更宽松的口径 ⇒ κ* 不应高于合并计税（至多相等）
    assert tax["逐注计税"]["κ*"] <= tax["合并计税"]["κ*"] + 1e-9
    assert tax["合并计税"]["税口径"] == TX.BASIS_ISSUE
    assert tax["逐注计税"]["税口径"] == TX.BASIS_TICKET
