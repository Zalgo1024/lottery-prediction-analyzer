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
from typing import Any, Dict, List, Optional, Sequence, Tuple

CODE = {"双色球": "ssq", "大乐透": "dlt", "七星彩": "qxc",
        "排列3": "pl3", "排列5": "pl5", "福彩3D": "sd",
        # 2026-11-01：七乐彩/快乐8 的实测单注金额在 market.db 里齐备
        # （七乐彩 7 档、快乐8 39 档，2011~2111 期），不接这一层就只能永远用名义值。
        "七乐彩": "qlc", "快乐8": "kl8"}

# 源优先级：体彩/福彩官方 > 中彩网 > csv500
_SRC_ORDER = {"sporttery": 0, "cwl": 0, "zhcw": 1, "csv500": 2}

_CN = "一二三四五六七八九十"
_CN_0 = "零一二三四五六七八九十"
_RE_GRADE = re.compile(rf"^([{_CN}])等奖$")
# 快乐8 的库内奖级名是 x10z10（选10中10），与规则层的「选十中十」互转
_RE_KL8 = re.compile(r"^[xX](\d{1,2})[zZ](\d{1,2})$")

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
    """`三等奖派奖` → ("三等", is_award=True, is_addon=False)。

    快乐8 的 `x10z10` → "选十中十"（与 ev/profit/rules.kl8_grade_name 同一命名）。
    ★ 不做这条归一，快乐8 的 39 档实测金额一条都查不到，会静默全部回退名义值。
    """
    s = (name or "").strip()
    is_addon = "追加" in s
    is_award = "派奖" in s
    s = s.replace("基本派奖", "").replace("派奖", "").replace("追加", "").replace("基本", "")
    m = _RE_GRADE.match(s)
    if m:
        s = m.group(1) + "等"
    else:
        m2 = _RE_KL8.match(s)
        if m2:
            a, z = int(m2.group(1)), int(m2.group(2))
            if 0 <= a <= 10 and 0 <= z <= 10:
                s = f"选{_CN_0[a]}中{_CN_0[z]}"
    return s.strip(), is_award, is_addon


def _issue_candidates(lottery: str, issue: Any) -> List[str]:
    """期号的多种库内格式（福彩 7 位 / 体彩 5 位 / 两位数年份）。

    ★ 期号格式按源不同：福彩 cwl 是 7 位（2026120），体彩是 5 位（26120）。
      直接拿一种格式去查会整片查空（曾让双色球所有奖级都回退到规则表）。
    """
    try:
        from data.issue_key import issue_key
        y, seq = issue_key(lottery, issue)
        return sorted({str(issue), f"{y}{seq:03d}", f"{y % 100}{seq:03d}",
                       f"{y % 100}{seq:02d}"})
    except Exception:
        return [str(issue)]


def _fetch_rows(code: str, db: Path, cands: Sequence[str]) -> List[Tuple[str, int, str, float]]:
    """直查：一个期号（多格式）的全部 prize 行。查不到/出错一律空列表。"""
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    ph = ",".join("?" * len(cands))
    try:
        return con.execute(
            f"SELECT source, level, level_name, amount FROM prize "
            f"WHERE lottery=? AND issue IN ({ph}) AND amount > 0",
            (code, *cands)).fetchall()
    except sqlite3.Error:
        return []
    finally:
        con.close()


# ---------------------------------------------------------------- 预载（批量 IO）
# `(彩种, str(期号)) → prize 行`。**只省 IO，不改结算语义**：命中缓存后沿用与
# 直查完全相同的行集合与源优先级逻辑。未预载的期号自动走 `_fetch_rows`。
_ROWS: Dict[Tuple[str, str], List[Tuple[str, int, str, float]]] = {}


def clear_preload() -> None:
    """清空预载缓存（测试 / 数据刷新后调用）。"""
    _ROWS.clear()


def preload(lottery: str, issues: Sequence[Any], *, chunk: int = 300) -> Dict[str, Any]:
    """把这些期号的 `prize` 行**一次批量**读进内存，返回载入统计。

    为什么需要
    ----------
    `actual_per_bet` 每期每奖级开一次 sqlite 连接。arena 的净收益结算要按
    3,518 期 × 7~9 个奖级取金额 ⇒ 三万次连接（纯 IO 约 1~2 分钟，且随期数线性涨）。
    预载把它压成一次全表扫描 + 分块 IN 查询。

    边界
    ----
    · 只做 IO 批量化，金额口径零变化（同一份行、同一套优先级）；
    · 单块 300 个期号（SQLite 变量上限最保守的版本是 999）；
    · 同一行可能被多个期号格式同时命中 → 按整行去重；
    · 查不到的期号也会写入空列表（=「查过了，确实没有」），避免重复回源。
    """
    code = CODE.get(lottery)
    db = _db_path()
    if code is None or db is None:
        return {"载入期数": 0, "载入行数": 0, "原因": "无彩种代码或 market.db 不存在"}
    keys: List[Tuple[Any, List[str]]] = [(iss, _issue_candidates(lottery, iss))
                                        for iss in issues]
    # ★ 幂等：已载入的期号直接跳过（replay 每个臂都会调 preload，不跳会重复全表扫描）
    keys = [(iss, cs) for iss, cs in keys if (lottery, str(iss)) not in _ROWS]
    if not keys:
        return {"载入期数": 0, "载入行数": 0, "跳过已载": len(issues),
                "原因": "全部已预载"}
    allc = sorted({c for _i, cs in keys for c in cs})
    by_issue: Dict[str, List[Tuple[str, int, str, float]]] = {}
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    except sqlite3.Error as e:
        return {"载入期数": 0, "载入行数": 0, "原因": f"打不开 market.db：{e}"}
    try:
        for s in range(0, len(allc), max(1, int(chunk))):
            part = allc[s:s + max(1, int(chunk))]
            ph = ",".join("?" * len(part))
            for iss, src, lvl, name, amt in con.execute(
                    f"SELECT issue, source, level, level_name, amount FROM prize "
                    f"WHERE lottery=? AND issue IN ({ph}) AND amount > 0",
                    (code, *part)).fetchall():
                by_issue.setdefault(str(iss), []).append(
                    (src, int(lvl), name, float(amt)))
    except sqlite3.Error as e:
        return {"载入期数": 0, "载入行数": 0, "原因": f"查询失败：{e}"}
    finally:
        con.close()

    n_rows = 0
    for iss, cands in keys:
        got: List[Tuple[str, int, str, float]] = []
        for c in cands:
            got += by_issue.get(c, [])
        merged = list({(r[0], r[1], r[2], r[3]): r for r in got}.values())
        _ROWS[(lottery, str(iss))] = merged
        n_rows += len(merged)
    return {"载入期数": len(keys), "载入行数": n_rows, "期号格式数": len(allc)}


def actual_per_bet(lottery: str, issue: Any, grade: str,
                   rule_amount: Optional[float] = None) -> Tuple[Optional[float], str]:
    """该期该奖级的实测单注金额 → (金额, note)。

    note ∈ {"实测", "实测+派奖", "规则表"}；`None` 表示库里没有该级数据。
    """
    code = CODE.get(lottery)
    db = _db_path()
    if code is None or db is None:
        return None, "规则表"
    cands = _issue_candidates(lottery, issue)
    rows = _ROWS.get((lottery, str(issue)))
    if rows is None:                     # 未预载 ⇒ 走原直查路径（行为完全一致）
        rows = _fetch_rows(code, db, cands)
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
