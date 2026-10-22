# -*- coding: utf-8 -*-
"""ev/profit/pit.py —— 期号归一化、PIT（as-of）视图、销量预测。

这个模块存在的唯一目的：**物理上不让你用到未来信息**。

三条硬约束（对应方案 §五）
--------------------------
1. `奖池奖金(t)` 是**开奖后**余额（实测 corr(Δ奖池_t, 本期头奖总额_t) = −0.845）。
   ⇒ 决策 t 期只能用 `pool_before(t) = 奖池奖金(t−1)`。本模块只提供 `pool_before`，
     不暴露 `奖池奖金(t)`，并让越界访问直接抛 `PitError`。
2. `总投注额 = 0` 是**整行未回填的哨兵**（双色球 11 期 / 大乐透 7 期，同一行的
   奖池、注数、奖金同时为 0）。销售额 0 物理上不可能 ⇒ 一律当缺失处理，
   不许任何 `notna()` 式判据把它当真实 0。
3. t 期销售额在开奖前不可知 ⇒ 只能用 t−1 及更早做滚动中位预测（实测 MAE 5.7%）。
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Dict, List, Optional, Tuple

ZERO_FILL_EPS = 1e-9

# ★ 期号归一化已下沉到 data/issue_key.py（data 层也要用，而 ev/ 不能反向被 data/ 依赖）。
#   PitError 保持为同一异常类型，否则 `except PitError` 抓不到 issue_key 抛出的错误。
from data.issue_key import IssueKeyError as PitError  # noqa: E402  (兼容旧捕获)
from data.issue_key import issue_key  # noqa: E402,F401  (re-export)


def is_zero_filled(sales: Optional[float], pool: Optional[float],
                   first_winners: Optional[float] = None) -> bool:
    """判断是否为「整行未回填」的零填充行。

    判据：销售额缺失或 ≤0（销售额为 0 在物理上不可能）。奖池/注数只作辅证。
    """
    if sales is None or sales <= ZERO_FILL_EPS:
        return True
    if pool is not None and pool < -ZERO_FILL_EPS:
        return True
    if first_winners is not None and first_winners < 0:
        return True
    return False


@dataclass
class Settled:
    """一期已结算数据（只含开奖后才知道的量，仅供 t+1 及以后使用）。"""
    issue: str
    key: Tuple[int, int]
    draw_date: Optional[date]
    sales: float
    pool_after: Optional[float]          # ★ 开奖后余额，不得用于同期决策
    notes: Dict[str, float] = field(default_factory=dict)    # 奖级 → 中奖注数
    prizes: Dict[str, float] = field(default_factory=dict)   # 奖级 → 单注奖金


@dataclass
class PitView:
    """决策时点 t 的可见信息。**只暴露 t−1 及更早**。"""
    lottery: str
    target_issue: str
    target_key: Tuple[int, int]
    history: List[Settled]               # 升序，且全部早于 target
    warnings: List[str] = field(default_factory=list)

    # ---- 合法可见量 ----
    @property
    def prev(self) -> Optional[Settled]:
        return self.history[-1] if self.history else None

    def pool_before(self) -> Optional[float]:
        """进入本期可用的奖池 = 上期开奖后余额（实测口径）。"""
        p = self.prev
        return p.pool_after if p else None

    def forecast_sales(self, window: int = 8) -> Dict[str, Optional[float]]:
        """本期销量预测：滚动中位 + P10/P90（只用 history，天然不看未来）。"""
        vals = [r.sales for r in self.history[-window:] if r.sales > 0]
        if not vals:
            return {"median": None, "p10": None, "p90": None, "n": 0}
        s = sorted(vals)
        def q(f):
            i = min(len(s) - 1, max(0, int(round(f * (len(s) - 1)))))
            return s[i]
        return {"median": statistics.median(vals), "p10": q(0.10),
                "p90": q(0.90), "n": len(vals)}

    def n_observed(self) -> int:
        return len(self.history)


def _to_date(v: Any) -> Optional[date]:
    if isinstance(v, date):
        return v
    s = str(v or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _load_redblue(lottery: str) -> List[Settled]:
    """双色球/大乐透：走 data.loader（六个财务字段都在）。"""
    from data.loader import load_lottery

    data = load_lottery(lottery)
    out: List[Settled] = []
    for r in (data.records or []):
        issue = getattr(r, "期号", None)
        if not issue:
            continue
        sales = getattr(r, "总投注额", None)
        pool = getattr(r, "奖池奖金", None)
        n1 = getattr(r, "一等奖注数", None)
        p1 = getattr(r, "一等奖奖金", None)
        n2 = getattr(r, "二等奖注数", None)
        p2 = getattr(r, "二等奖奖金", None)
        if is_zero_filled(sales, pool, n1):
            continue                       # ★ 零填充一律丢弃
        notes, prizes = {}, {}
        if n1 is not None:
            notes["一等"] = float(n1)
        if p1 is not None:
            prizes["一等"] = float(p1)
        if n2 is not None:
            notes["二等"] = float(n2)
        if p2 is not None:
            prizes["二等"] = float(p2)
        out.append(Settled(
            issue=str(issue), key=issue_key(lottery, issue),
            draw_date=_to_date(getattr(r, "开奖日期", None)),
            sales=float(sales), pool_after=float(pool) if pool else None,
            notes=notes, prizes=prizes,
        ))
    out.sort(key=lambda x: (x.draw_date or date.min, x.key))
    return out


def _load_digital(lottery: str) -> List[Settled]:
    """数字型/七星彩：走 ev.mechanism.sales_grade 的 DigitalFrame（含销售额与各奖级）。"""
    from ev.mechanism.sales_grade import load_digital_frame

    fr = load_digital_frame(lottery)
    if fr is None or fr.n == 0:
        return []
    out: List[Settled] = []
    for i in range(fr.n):
        sales = float(fr.sales[i] or 0.0)
        if is_zero_filled(sales, None):
            continue
        notes = {k: float(v[i]) for k, v in fr.notes.items()
                 if v[i] == v[i]}                    # 过滤 NaN
        prizes = {k: float(v[i]) for k, v in fr.prizes.items()
                  if v[i] == v[i]}
        out.append(Settled(
            issue=str(fr.issues[i]), key=issue_key(lottery, fr.issues[i]),
            draw_date=fr.dates[i], sales=sales,
            pool_after=None,                    # 该口径不带奖池
            notes=notes, prizes=prizes,
        ))
    return out


def as_of(lottery: str, issue: Any, min_history: int = 0) -> PitView:
    """构造决策时点 t 的可见视图。

    只返回 t 之前的记录；历史不足 `min_history` 时在 `warnings` 里说明
    （**不抛**，因为调用方可能需要输出「数据不足」而不是崩溃）。
    """
    key = issue_key(lottery, issue)
    if lottery in ("双色球", "大乐透"):
        rows = _load_redblue(lottery)
    else:
        rows = _load_digital(lottery)

    hist = [r for r in rows if r.key < key]
    view = PitView(lottery=lottery, target_issue=str(issue), target_key=key,
                   history=hist)
    if not hist:
        view.warnings.append("目标期之前没有任何有效记录（首期或数据缺失）")
    elif min_history and len(hist) < min_history:
        view.warnings.append(f"历史仅 {len(hist)} 期，低于门槛 {min_history}")
    if hist and hist[-1].pool_after is None:
        view.warnings.append("上期无奖池字段（该彩种/口径不提供）⇒ 浮动奖只能用历史中位基准")
    return view
