# -*- coding: utf-8 -*-
"""ev/profit/robustness.py —— 四轴稳健性检查（决策不脆弱于常数取巧）。

`decide` 给出「可参与 / 不建议」判决时，不应是因为某个常数（β、销量、a1、税口径）
取巧而脆弱。本模块在四个轴向上做**良性扰动**，逐一重新解 κ*，检验判决是否翻车：

  1. β 形状误差：把 log-popularity 分数整体缩放 ±δ（模拟 β 误标）
  2. 销量 P10/P90：把销量预测换成滚动窗口的 P10 / P90（而非中位）
  3. 池模型两口径：FLOAT_SHARE 手写 a1  vs  calib 稳健中位数 a1
  4. 税两口径：同期合并计税（BASIS_ISSUE）vs 逐注计税（BASIS_TICKET）

每个轴返回 {扰动, κ*, κ*_基准, 翻车, 稳定}。
「翻车」= 在扰动下 κ* 跌破已测机制上界（判决从「不建议」翻成「可参与」）。
另含反例入口 `_kstar_with_beta` 接受任意 δ，测试里用大 δ 验证检查确实敏感。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from unittest import mock

import numpy as np

from ev.profit import calib
from ev.profit import pit
from ev.profit import payouts as P
from ev.profit import tilt as T
from ev.profit import tax as TX
from ev.profit import crowd_tilt as CT
from ev.profit.solvers import (solve_kappa_star, VERDICT_LEVEL_FULL,
                               VERDICT_LEVEL_CONDITIONAL)
from ev.profit import select as SEL

# 扰动求解用较粗网格：只需确认 κ* 仍高于机制上界，无需高分辨率（基准 κ* 仍全分辨率）。
ROBUST_GRID = (0.0, 0.5, 1.0, 1.5, 2.0, 3.0)


# ------------------------------------------------------------------- 通用求解
def _solve_kstar(lottery: str, tickets: Sequence[dict], ctx: P.Ctx,
                 k_up: Optional[float], tax_basis: str = TX.BASIS_ISSUE,
                 grid: Sequence[float] = ROBUST_GRID) -> Dict[str, Any]:
    ks = solve_kappa_star(lottery, tickets, ctx, tax_basis=tax_basis, grid=grid)
    kstar = ks["κ*"]
    flips = (kstar is not None and k_up is not None and kstar <= k_up)
    return {"κ*": kstar, "翻车": bool(flips), "稳定": not flips,
            "单调": ks.get("单调"), "κ*_是否有解": ks.get("κ*是否有解")}


# --------------------------------------------------------------- 轴 1：β 形状
def _perturbed_tilt(lottery: str, delta: float) -> Optional[T.Tilt]:
    """把 log-popularity 分数整体缩放 (1+δ)，模拟 β 误标。

    同时缩放 scores 与 offset 以保持 μ 宇宙的同一性：
        raw = scores + offset；  raw' = raw·(1+δ)
        scores' = raw' − mean(raw')；  offset' = mean(raw')
    """
    tl = T.outer_tilt(lottery)
    if tl is None:
        return None
    raw = tl.scores.astype(np.float64) + tl.offset
    sraw = raw * (1.0 + delta)
    return T.Tilt(
        lottery=lottery,
        scores=sraw - sraw.mean(),
        quantiles=tl.quantiles,
        source=tl.source,
        offset=float(sraw.mean()),
        note=tl.note,
        beta_asof=tl.beta_asof,
    )


def _kstar_with_beta(lottery: str, tickets: Sequence[dict], ctx: P.Ctx,
                     delta: float, k_up: Optional[float],
                     kstar_base: Optional[float]) -> Dict[str, Any]:
    pt = _perturbed_tilt(lottery, delta)
    if pt is None:
        return {"δ": delta, "κ*": None, "翻车": False, "稳定": False,
                "原因": "倾斜不可用"}
    with mock.patch.object(T, "outer_tilt", return_value=pt):
        out = _solve_kstar(lottery, tickets, ctx, k_up)
    out["δ"] = delta
    out["κ*_基准"] = kstar_base
    return out


# --------------------------------------------------------------- 轴 2：销量带
def _kstar_with_sales(lottery: str, issue: Any, tickets: Sequence[dict],
                      view: pit.PitView, ctx: P.Ctx, sales: Optional[float],
                      k_up: Optional[float], kstar_base: Optional[float]
                      ) -> Dict[str, Any]:
    if sales is None or sales <= 0:
        return {"销量": sales, "κ*": None, "翻车": False, "稳定": False,
                "原因": "该销量带不可用"}
    ctx2 = P.build_ctx(lottery, issue, sales_override=sales, view=view)
    out = _solve_kstar(lottery, tickets, ctx2, k_up)
    out["销量"] = sales
    out["κ*_基准"] = kstar_base
    return out


# ----------------------------------------------------------- 轴 3：池模型口径
def _kstar_with_a1(lottery: str, issue: Any, tickets: Sequence[dict],
                   view: pit.PitView, ctx: P.Ctx, a1: float,
                   k_up: Optional[float], kstar_base: Optional[float],
                   label: str) -> Dict[str, Any]:
    base = dict(P.FLOAT_SHARE.get(lottery, {}))
    base["一等"] = (a1, label)
    with mock.patch.dict(P.FLOAT_SHARE, {lottery: base}):
        ctx2 = P.build_ctx(lottery, issue, view=view)
        out = _solve_kstar(lottery, tickets, ctx2, k_up)
    out["a1"] = a1
    out["口径"] = label
    out["κ*_基准"] = kstar_base
    return out


# ------------------------------------------------------------- 轴 4：税口径
def _kstar_with_tax(lottery: str, tickets: Sequence[dict], ctx: P.Ctx,
                    tax_basis: str, k_up: Optional[float],
                    kstar_base: Optional[float]) -> Dict[str, Any]:
    out = _solve_kstar(lottery, tickets, ctx, k_up, tax_basis=tax_basis)
    out["税口径"] = tax_basis
    out["κ*_基准"] = kstar_base
    return out


# --------------------------------------------------------------- 编排入口
def robustness_check(lottery: str, issue: Any, K: int = 20,
                     min_history: int = 1200, seed: int = 0,
                     beta_deltas: Sequence[float] = (-0.1, 0.1)
                     ) -> Dict[str, Any]:
    """对单期跑四轴稳健性检查，返回各轴扰动下的 κ* 与翻车判定。"""
    view = pit.as_of(lottery, issue, min_history=min_history)
    ctx = P.build_ctx(lottery, issue, view=view)
    if len(view.history) < min_history:
        return {"ok": False, "原因": f"历史仅 {len(view.history)} 期 < {min_history}"}
    if ctx.sales_forecast <= 0:
        return {"ok": False, "原因": "本期销量不可得"}

    sel = SEL.select_profit_tickets(lottery, K, ctx, kappa=0.0, seed=seed)
    tickets = sel["票"]
    if not tickets:
        return {"ok": False, "原因": "选号为空"}

    tl = T.outer_tilt(lottery)
    if tl is None:
        return {"ok": False, "原因": "倾斜不可用（无法做 β 轴）"}
    判决等级 = (VERDICT_LEVEL_FULL if tl.source == "measured_GO"
               else VERDICT_LEVEL_CONDITIONAL)

    ks0 = solve_kappa_star(lottery, tickets, ctx)
    kstar0 = ks0["κ*"]
    up = (T.kappa_upper_marginal(lottery, tl)
          if tl.source == CT.CROWD_PI_SOURCE else T.kappa_upper(lottery))
    k_up = (up.get("kappa_upper", up.get("kappa")) if up.get("可用") else None)

    fc = view.forecast_sales()
    try:
        calib_a1 = calib.calibrate(lottery, issue).get("a1_稳健")
    except Exception:
        calib_a1 = None

    beta_block = [_kstar_with_beta(lottery, tickets, ctx, d, k_up, kstar0)
                  for d in beta_deltas]
    sales_block = [
        _kstar_with_sales(lottery, issue, tickets, view, ctx, fc.get("p10"),
                          k_up, kstar0),
        _kstar_with_sales(lottery, issue, tickets, view, ctx, fc.get("p90"),
                          k_up, kstar0),
    ]
    pool_block = {
        "FLOAT_SHARE手写": _kstar_with_a1(
            lottery, issue, tickets, view, ctx,
            P.FLOAT_SHARE.get(lottery, {}).get("一等", (None,))[0], k_up, kstar0,
            "FLOAT_SHARE 手写常数"),
    }
    if calib_a1 is not None:
        pool_block["calib稳健中位"] = _kstar_with_a1(
            lottery, issue, tickets, view, ctx, calib_a1, k_up, kstar0,
            "calib 稳健中位数")

    tax_block = {
        "合并计税": _kstar_with_tax(lottery, tickets, ctx, TX.BASIS_ISSUE,
                                    k_up, kstar0),
        "逐注计税": _kstar_with_tax(lottery, tickets, ctx, TX.BASIS_TICKET,
                                    k_up, kstar0),
    }

    def _any_flip(x):
        if isinstance(x, dict) and "翻车" in x:
            return x["翻车"]
        if isinstance(x, list):
            return any(_any_flip(d) for d in x)
        if isinstance(x, dict):
            return any(_any_flip(v) for v in x.values())
        return False

    out = {
        "ok": True,
        "彩种": lottery, "期号": str(issue),
        "判决等级": 判决等级,
        "κ*_基准": kstar0, "机制上界": k_up,
        "β形状误差": beta_block,
        "销量P10_P90": sales_block,
        "池模型两口径": pool_block,
        "税两口径": tax_block,
    }
    out["全部稳定"] = not (_any_flip(beta_block) or _any_flip(sales_block)
                           or _any_flip(pool_block) or _any_flip(tax_block))
    return out


# ----------------------------------------------------- 反例：对抗大扰动应敏感
def adversarial_beta_flip(lottery: str, issue: Any, K: int = 20,
                          min_history: int = 1200, seed: int = 0,
                          delta: float = 0.5) -> Dict[str, Any]:
    """给定足够大的对抗 β 扰动，κ* 应明显低于基准（甚至跌破上界）。

    用作反例断言：证明稳健性检查真的对 β 形状敏感（不是恒稳定糊弄）。
    """
    view = pit.as_of(lottery, issue, min_history=min_history)
    ctx = P.build_ctx(lottery, issue, view=view)
    sel = SEL.select_profit_tickets(lottery, K, ctx, kappa=0.0, seed=seed)
    tickets = sel["票"]
    tl = T.outer_tilt(lottery)
    up = (T.kappa_upper_marginal(lottery, tl)
          if tl.source == CT.CROWD_PI_SOURCE else T.kappa_upper(lottery))
    k_up = (up.get("kappa_upper", up.get("kappa")) if up.get("可用") else None)
    ks0 = solve_kappa_star(lottery, tickets, ctx)["κ*"]
    adv = _kstar_with_beta(lottery, tickets, ctx, delta, k_up, ks0)
    return {"κ*_基准": ks0, "κ*_对抗": adv["κ*"], "δ": delta,
            "跌幅比例": (None if adv["κ*"] is None or ks0 in (None, 0)
                         else (ks0 - adv["κ*"]) / ks0),
            "对抗翻车": adv["翻车"]}
