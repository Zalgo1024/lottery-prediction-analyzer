# -*- coding: utf-8 -*-
"""data/prize_amounts.py —— 从 `market.db` 取**当期实测单注金额**（结算真源）。

为什么要这一层
--------------
回测的「已实现」结算需要「我这张票中了 X 等，实际能拿多少」。
过去这个金额来自 `ev/coverage_backtest.TIER_PAYOUT` —— 那是**第三份**手写九级表，
在 2026-02 之后全是错的（七等按 100 元算，实际 5 元）。

三条必须遵守的取数口径（都实测过）：
1. **按 `level_name` 归一匹配，不用 `level` 号**：`level` 跨规则段不可对齐
   （2019 的 level7 = 4+0/100 元；2026 的 level7 = 3+0 等/5 元）。
2. **派奖期要「基本 + 派奖」相加**：源里常只剩「X等奖派奖」行且金额恰为基本的一半
   （21039：5000/1500/150/100/50/8/2）。只取基本名会少一半，只取派奖名会少三分之二。
   只有派奖行、没有基本行时 ⇒ 规则表基本额 + 派奖行。
3. **`amount <= 0` 是「0 注/未结算」哨兵**，必须过滤；追加行（level>=101 或含「追加」）
   不参与基本投注结算。
"""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CODE = {"双色球": "ssq", "大乐透": "dlt", "七星彩": "qxc",
        "排列3": "pl3", "排列5": "pl5", "福彩3D": "sd"}

# 源优先级：体彩/福彩官方 > 中彩网 > csv500
_SRC_ORDER = {"sporttery": 0, "cwl": 0, "zhcw": 1, "csv500": 2}

_CN = "一二三四五六七八九十"
_RE_GRADE = re.compile(rf"^([{_CN}])等奖$")

_DB: Optional[Path] = None


def _db_path() -> Optional[Path]:
    global _DB
    if _DB is not None:
        return _DB if _DB.exists() else None
    root = Path(__file__).resolve().parents[1]
    p = root / "market" / "market.db"
    _DB = p
    return p if p.exists() else None


def normalize_level_name(name: str) -> Tuple[str, bool, bool]:
    """`三等奖派奖` → ("三等", is_award=True, is_addon=False)。"""
    s = (name or "").strip()
    is_addon = "追加" in s
    is_award = "派奖" in s
    s = s.replace("基本派奖", "").replace("派奖", "").replace("追加", "").replace("基本", "")
    m = _RE_GRADE.match(s)
    if m:
        s = m.group(1) + "等"
    return s.strip(), is_award, is_addon


def actual_per_bet(lottery: str, issue: Any, grade: str,
                   rule_amount: Optional[float] = None) -> Tuple[Optional[float], str]:
    """该期该奖级的实测单注金额 → (金额, note)。

    note ∈ {"实测", "实测+派奖", "规则表"}；`None` 表示库里没有该级数据。
    """
    code = CODE.get(lottery)
    db = _db_path()
    if code is None or db is None:
        return None, "规则表"
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return None, "规则表"
    # ★ 期号格式按源不同：福彩 cwl 是 7 位（2026120），体彩是 5 位（26120）。
    #   直接拿一种格式去查会整片查空（曾让双色球所有奖级都回退到规则表）。
    try:
        from data.issue_key import issue_key
        y, seq = issue_key(lottery, issue)
        cands = sorted({str(issue), f"{y}{seq:03d}", f"{y % 100}{seq:03d}",
                        f"{y % 100}{seq:02d}"})
    except Exception:
        cands = [str(issue)]
    ph = ",".join("?" * len(cands))
    try:
        rows: List[Tuple[str, int, str, float]] = con.execute(
            f"SELECT source, level, level_name, amount FROM prize "
            f"WHERE lottery=? AND issue IN ({ph}) AND amount > 0",
            (code, *cands)).fetchall()
    except sqlite3.Error:
        return None, "规则表"
    finally:
        con.close()
    if not rows:
        return None, "规则表"

    # 同 issue 可能有多源 ⇒ 只认优先级最高、且确实有该奖级的那一源
    rows.sort(key=lambda r: _SRC_ORDER.get(r[0], 9))
    for src in sorted({r[0] for r in rows}, key=lambda s: _SRC_ORDER.get(s, 9)):
        base: Optional[float] = None
        award: Optional[float] = None
        for s0, level, name, amt in rows:
            if s0 != src:
                continue
            if int(level) >= 101:
                continue                     # 追加各等不参与基本投注结算
            g, is_award, is_addon = normalize_level_name(name)
            if is_addon or g != grade:
                continue
            if is_award:
                award = (award or 0.0) + float(amt)
            else:
                base = (base or 0.0) + float(amt)
        if base is None and award is None:
            continue
        if base is None:
            # 只有派奖行 ⇒ 规则表基本额 + 派奖行
            if rule_amount is None:
                return None, "规则表"
            return float(rule_amount) + float(award or 0.0), "实测+派奖"
        if award:
            return base + award, "实测+派奖"
        return base, "实测"
    return None, "规则表"
