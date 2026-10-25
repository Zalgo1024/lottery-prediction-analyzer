# -*- coding: utf-8 -*-
"""ev/arena/types.py —— 对照场的数据结构。

设计约束：所有字段用**中文键**的 dict 作外部契约（便于进 JSON 与报告），
内部计算用 numpy 数组；两者通过 `to_row()` 显式转换，不允许隐式混用。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

import numpy as np


@dataclass(frozen=True)
class ArmSpec:
    """一个「臂」的定义：怎么选票。"""
    名: str
    选择器: str                    # select 函数名
    参数: Dict[str, Any] = field(default_factory=dict)
    说明: str = ""


@dataclass
class ArmResult:
    """一个臂在一批期上的回放结果。"""
    臂: str
    彩种: str
    issues: List[Any] = field(default_factory=list)
    hit: Optional[np.ndarray] = None        # (n,) bool  —— 主终点（单期达标）
    nhit: Optional[np.ndarray] = None       # (n,) int   —— 哨兵（单期达标注数）
    cost: float = 0.0                       # 总成本 = Σ 每期实际注数 × COST_PER_BET
    # ---- 净收益侧（2026-10-24 新增，见 ev/arena/payout.py）----
    payout: Optional[np.ndarray] = None     # (n,) float —— 每期实得（**税前**主口径）
    money: Dict[str, Any] = field(default_factory=dict)   # money_series 的汇总（标量为主）
    net: Optional[np.ndarray] = None        # (n,) float —— 每期净额 = 实得 − 成本
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def n(self) -> int:
        return len(self.issues)

    def to_row(self) -> Dict[str, Any]:
        h = self.hit
        n = self.n
        row: Dict[str, Any] = {
            "臂": self.臂,
            "彩种": self.彩种,
            "期数": n,
            "期号起": self.issues[0] if n else None,
            "期号止": self.issues[-1] if n else None,
            "成本": round(self.cost, 2),
        }
        if h is not None and n:
            row["达标率"] = float(h.mean())
            row["达标期数"] = int(h.sum())
            row["抽样SE"] = float(np.sqrt(max(h.mean() * (1 - h.mean()), 0.0) / n))
        if self.nhit is not None and n:
            row["哨兵_单期达标注数"] = float(self.nhit.mean())
        # 净收益侧的标量汇总（序列本身不进 row，避免 JSON 里塞两个长数组）
        for k, v in (self.money or {}).items():
            if isinstance(v, (int, float, str, bool)) or v is None:
                row.setdefault(k, v)
        row.update({k: v for k, v in self.meta.items() if k not in row})
        return row


def arm_result_from(arm: str, lottery: str, issues: Sequence[Any],
                    hit: np.ndarray, nhit: np.ndarray,
                    nbet_per_period: Optional[Sequence[int]] = None,
                    cost_per_bet: float = 2.0,
                    **meta: Any) -> ArmResult:
    """构造 `ArmResult`，成本一律按**实际投注注数**累加（不允许 `n*K*2`）。"""
    nb = (np.asarray(nbet_per_period, dtype=np.int64) if nbet_per_period is not None
          else np.full(len(issues), int(nhit.shape[0] if False else 0), dtype=np.int64))
    cost = float(nb.sum()) * float(cost_per_bet)
    return ArmResult(臂=arm, 彩种=lottery, issues=list(issues),
                     hit=np.asarray(hit, dtype=bool),
                     nhit=np.asarray(nhit, dtype=np.int64),
                     cost=cost, meta=dict(meta, 每期注数最大值=int(nb.max()) if nb.size else 0,
                                          每期注数最小值=int(nb.min()) if nb.size else 0))
