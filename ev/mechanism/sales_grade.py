# -*- coding: utf-8 -*-
r"""数字型 / 七星彩的**原始销售奖级 CSV** 装载层。

## 为什么必须直读 CSV

`data/loader.load_lottery` 对 排列3/排列5/福彩3D/七星彩 只透传
`期号 / 开奖日期 / 号码`，`一等奖注数` / `总投注额` 等字段**恒为 None**
（2026-10-03 实测：0/7738 非空）。而 P2 的
A′ 轨（数字型注数比）与 D 轨（派奖期）都需要这些列，所以只能直读
`lottery_data/*销售奖级数据.csv`。

## 三个已实测的口径陷阱（都曾/会造出假信号，勿重蹈）

1. **销售额不是「直选玩法的票量」**。
   排列3/福彩3D 的销售额覆盖**全部玩法**（直选 + 组选3 + 组选6 + …）。
   实测各玩法份额之和 `0.6766 + 0.0829 + 0.2294 = 0.9889 ≈ 1`，
   反过来证明了「销售额 ÷ 2 = 全部玩法的总票量」这个解释成立；
   同时说明 `Σ注数 / Σ(N·p)` 量的是**投注结构占比**（排列3 直选份额 67.7%），
   **不是**机制缝隙。⇒ 排列3 / 福彩3D **不接入 A′ 轨**（见 `excluded_a_reason`）。

2. **七星彩规则变更日之前不能用现行概率表**。
   2020-10-11 起组合空间由 `10^7` 变为 `10^6 × 15 = 1.5e7`。
   实测把 2016 起的全段混算，会得到 一等 R=1.19 / 三等 R=2.92 的巨大「假信号」；
   只用现行段（≥ 2020-10-11）后，六个奖级的 R 分别
   **1.034 / 1.018 / 0.973 / 0.996 / 0.998 / 0.991** —— 全部与 1 相容。
   ⚠️ 这与 `credibility/data_quality.py` 只对**第 7 位**做切段的既有口径不同：
   整个**概率标尺**（不只是第 7 位边际）都换过了，所以 A′ 轨必须整段切。

3. **福彩3D 的注数数据 2022 起死掉**。
   `直选注数` 2022-01 起 100% 为 0（2021 年已有 17/351 为 0）⇒ 福彩3D 的
   注数系一律不可用。排列3 的 `直选注数` 从未出现 0。

## 与既有口径的一致性

`QXC_RULE_CUTOFF = date(2020, 10, 11)` 与下面四处**同源**，由
`tests/test_gap_a_digital.py` 锁死（四处任一改动都会红）：
  · `credibility/data_quality.py::_QXC_CUTOFF`
  · `credibility/randomness_audit.py::_QXC_CUTOFF`
  · `discovery/always_valid.py::_QXC_CUTOFF`
  · `discovery/info_theory.py::_QXC_CUTOFF`
"""
from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

BASE_DIR = Path(__file__).resolve().parents[2]
DATA_DIR = BASE_DIR / "lottery_data"

# 有「销售奖级数据.csv」的数字型彩种
SALES_CSV: Dict[str, str] = {
    "排列3": "排列3销售奖级数据.csv",
    "排列5": "排列5销售奖级数据.csv",
    "福彩3D": "福彩3D销售奖级数据.csv",
    "七星彩": "七星彩销售奖级数据.csv",
}

DIGITAL_LOTTERIES: Tuple[str, ...] = tuple(SALES_CSV)

# 七星彩现行规则（前6位 0-9 + 后区 0-14，组合空间 10^6×15）生效日。
# ⚠️ 与 data_quality / randomness_audit / always_valid / info_theory 四处同源。
QXC_RULE_CUTOFF = date(2020, 10, 11)
RULE_CUTOFF: Dict[str, date] = {"七星彩": QXC_RULE_CUTOFF}

def _fnum(v: Optional[str]) -> Optional[float]:
    """空串 / 非法值 → None（**不**当 0；0 与缺失必须区分）。"""
    if v is None:
        return None
    s = str(v).strip()
    if s == "":
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fdate(v: Optional[str]) -> Optional[date]:
    s = (v or "").strip()
    if not s:
        return None
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


@dataclass(frozen=True)
class DigitalFrame:
    """按**开奖日期升序**对齐的原始销售奖级记录。"""

    lottery: str
    issues: List[str]
    dates: List[date]
    sales: np.ndarray                 # (n,) float，缺失/非正 → 0.0
    notes: Dict[str, np.ndarray]      # 奖级类别 → 中奖注数（缺失 → nan）
    prizes: Dict[str, np.ndarray]     # 奖级类别 → 单注奖金（缺失 → nan）
    award: np.ndarray                 # (n,) bool，派奖期
    has_award_columns: bool
    source: str

    @property
    def n(self) -> int:
        return len(self.issues)

    def rule_mask(self) -> np.ndarray:
        """现行规则段掩码（无规则变更的彩种 → 全 True）。"""
        cut = RULE_CUTOFF.get(self.lottery)
        if cut is None:
            return np.ones(self.n, dtype=bool)
        return np.array([d >= cut for d in self.dates], dtype=bool)

    def usable_mask(self) -> np.ndarray:
        """有销售额（>0）**且**在现行规则段内。"""
        return (self.sales > 0) & self.rule_mask()


def load_digital_frame(lottery: str) -> Optional[DigitalFrame]:
    """装载原始销售奖级 CSV。文件缺失/为空 → None（调用方显式降级）。"""
    fn = SALES_CSV.get(lottery)
    if fn is None:
        return None
    p = DATA_DIR / fn
    if not os.path.exists(p):
        return None
    try:
        with open(p, "r", encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.DictReader(fh))
    except Exception:                                     # noqa: BLE001
        return None
    if not rows:
        return None

    cols = list(rows[0].keys())
    note_cols = [c for c in cols if c.endswith("注数") and "派奖" not in c]

    # 「一等奖注数」→ 「一等」；「直选注数」→ 「直选」；「组选3注数」→ 「组选3」
    def cat_of(c: str) -> str:
        base = c[:-2]                       # 去掉「注数」→ 「一等奖」/「直选」
        return base[:-1] if base.endswith("奖") else base

    award_cols = [c for c in cols if "派奖" in c and c.endswith("注数")]

    issues: List[str] = []
    dates: List[date] = []
    sales: List[float] = []
    note_buf: Dict[str, List[float]] = {cat_of(c): [] for c in note_cols}
    prize_buf: Dict[str, List[float]] = {cat_of(c): [] for c in note_cols}
    award_buf: List[bool] = []
    nan = float("nan")

    for r in rows:
        d = _fdate(r.get("开奖日期"))
        if d is None:
            continue
        issues.append(str(r.get("期号", "")).strip())
        dates.append(d)
        s = _fnum(r.get("销售额"))
        sales.append(float(s) if s is not None and s > 0 else 0.0)
        for c in note_cols:
            v = _fnum(r.get(c))
            note_buf[cat_of(c)].append(float(v) if v is not None else nan)
            pv = _fnum(r.get(c[:-2] + "单注奖金"))
            prize_buf[cat_of(c)].append(float(pv) if pv is not None else nan)
        hit = False
        for c in award_cols:
            v = _fnum(r.get(c))
            if v is not None and v > 0:
                hit = True
                break
        award_buf.append(hit)

    if not issues:
        return None
    # 按日期升序（派奖/口径分段都依赖顺序）
    order = sorted(range(len(issues)), key=lambda i: (dates[i], issues[i]))
    return DigitalFrame(
        lottery=lottery,
        issues=[issues[i] for i in order],
        dates=[dates[i] for i in order],
        sales=np.asarray([sales[i] for i in order], dtype=np.float64),
        notes={k: np.asarray([v[i] for i in order], dtype=np.float64)
               for k, v in note_buf.items()},
        prizes={k: np.asarray([v[i] for i in order], dtype=np.float64)
                for k, v in prize_buf.items()},
        award=np.asarray([award_buf[i] for i in order], dtype=bool),
        has_award_columns=bool(award_cols),
        source=str(p),
    )


# ---------------------------------------------------------------- A′ 轨可用性
# A′ 轨可用奖级。**键是 CSV 里的奖级名**（由「N等奖注数 / XX注数」列名推出），
# 与 `ev/payout.grade_probabilities` 的键不一定同名 → 用 `prob_key()` 映射。
#   排列5：CSV 列名是「一等奖注数」，但规则上它只有一个奖级「直选」（p=1e-5，见
#          `ev/rulebook.py::_prob_pl5` 的说明：六形态只是投注方式、不是独立奖级）。
#   七星彩：CSV 与 payout 都用「N等」。
A_PRIME_LOTTERIES: Tuple[str, ...] = ("排列5", "七星彩")

A_PRIME_CATEGORY: Dict[str, Tuple[str, ...]] = {
    "排列5": ("一等",),
    "七星彩": ("一等", "二等", "三等", "四等", "五等", "六等"),
}

# A′ 轨的**主口径**奖级（注数期望最大、bootstrap se 最小）
A_PRIME_PRIMARY: Dict[str, str] = {
    "排列5": "一等",
    "七星彩": "六等",
}

# CSV 奖级名 → `ev/payout.grade_probabilities` 的键
_CAT_TO_PKEY: Dict[str, Dict[str, str]] = {
    "排列5": {"一等": "直选"},
    "七星彩": {c: c for c in A_PRIME_CATEGORY["七星彩"]},
}


def prob_key(lottery: str, cat: str) -> str:
    """把 CSV 奖级名映射到 `grade_probabilities` 的键（未知则原样返回）。"""
    return _CAT_TO_PKEY.get(lottery, {}).get(cat, cat)

EXCLUDED_A_REASON: Dict[str, str] = {
    "排列3": ("销售额覆盖**全部玩法**（直选+组选3+组选6+和值…），"
              "而 `直选注数` 只覆盖直选 → Σ注数/Σ(N·p) 量的是**投注结构占比**"
              "（实测直选份额 0.6766、组选3 0.0829、组选6 0.2294，三者之和 0.9889≈1）"
              "而非机制缝隙。要判缝隙必须知道各玩法票量，本数据不可得。"),
    "福彩3D": ("同 排列3：销售额覆盖全部玩法；且 `直选注数` 自 2022-01 起 100% 为 0"
               "（2021 年已有 17/351 为 0），注数系已失效。"),
}


def excluded_a_reason(lottery: str) -> str:
    return EXCLUDED_A_REASON.get(lottery, "")


def award_periods(fr: DigitalFrame) -> Dict[str, object]:
    """派奖期识别（任一 `*派奖注数 > 0`）。无派奖列的彩种 → applicable False。"""
    if not fr.has_award_columns:
        return {"applicable": False,
                "note": (f"{fr.lottery} 的销售奖级 CSV 无「派奖」列"
                         f"（列数不足），D 轨不适用；不硬凑代理。")}
    idx = np.nonzero(fr.award)[0]
    return {
        "applicable": True,
        "n": int(len(idx)),
        "期号": [fr.issues[i] for i in idx],
        "日期": [fr.dates[i].isoformat() for i in idx],
        "mask": fr.award,
        "note": f"任一「*派奖注数 > 0」判为派奖期，共 {len(idx)} 期",
    }


__all__ = [
    "BASE_DIR", "DATA_DIR", "SALES_CSV", "DIGITAL_LOTTERIES", "QXC_RULE_CUTOFF",
    "RULE_CUTOFF", "DigitalFrame", "load_digital_frame", "excluded_a_reason",
    "award_periods", "A_PRIME_LOTTERIES", "A_PRIME_CATEGORY", "A_PRIME_PRIMARY",
    "EXCLUDED_A_REASON", "prob_key",
]
