"""把「号码 / 中奖记录」渲染成 PNG 图片，供企业微信群机器人以**图片消息**推送。

为什么要图片（2026-09-18）：
  企业微信群机器人 markdown 单条上限 **4096 字节**（中文 3 字节/字），号码注数一多
  （双色球一次上百注、大乐透一次 90+ 注）必然被截断，末尾的诚实声明也会被砍掉。
  改成图片后号码可以**一张全展示、不截断**，且中奖明细也能排成表格。

设计要点：
  - **纯布局函数**：只接收**已格式化好的字符串**（号码行 / 中奖行），
    不 import `data.push_notify`，避免循环依赖；格式化（`format_ticket_line` 等）
    仍由 `push_notify` 负责。
  - 中文字体与 `scripts/report_figures.py:17` 同款（Microsoft YaHei / SimHei）。
  - 失败时**向上抛异常**，由调用方降级为文本推送（本模块不吞错、不打印）。
  - 输出 PNG bytes（内存 BytesIO），是否落盘由调用方决定。
"""

from __future__ import annotations

import io
from typing import Any, Dict, List, Optional, Sequence

import matplotlib

try:  # 无显示环境；若 pyplot 已被别处初始化则保持原样
    matplotlib.use("Agg")
except Exception:  # pragma: no cover - 后端已固定的极端情况
    pass

import matplotlib.pyplot as plt
from matplotlib.lines import Line2D

# ------------------------------------------------------------
# 主题（浅色底，与结题报告配图同风格）
# ------------------------------------------------------------
DPI = 120
_FONTS = ["Microsoft YaHei", "SimHei", "SimSun", "DejaVu Sans"]

C_TITLE = "#1B3A5C"
C_SUB = "#6B7280"
C_TEXT = "#1F2937"
C_HEAD = "#2F6DB5"
C_RULE = "#D8E1EC"
C_WIN = "#C0392B"

# 诚实声明：图片消息没有 caption，声明必须画进图里
DISCLAIMER = "仅供研究记录；单注中奖概率恒定，整体期望为负，请勿用于投注决策。"

# 中文字体（Microsoft YaHei / SimHei）没有 emoji 字形，直接画会变成"豆腐块"，
# 故渲染前统一剔除（企微 markdown 文本里仍保留 emoji，只影响图片）。
_EMOJI_RANGES = (
    (0x1F000, 0x1FAFF),   # emoji / 表情 / 旗帜
    (0x2600, 0x27BF),     # ☀✅❌✨ 等杂项符号与装饰符号
    (0x2B00, 0x2BFF),     # ⬛⭐ 等
    (0xFE00, 0xFE0F),     # 变体选择符
    (0x200D, 0x200D),     # 零宽连接符
    (0x20E3, 0x20E3),     # 组合键帽
)


def _clean(text: object) -> str:
    """剔除无字形字符（emoji 等），并把因此产生的多余空格收敛掉。"""
    out = []
    for ch in str(text):
        cp = ord(ch)
        if any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES):
            continue
        out.append(ch)
    return " ".join("".join(out).split())


_rc_done = False


def _ensure_rc() -> None:
    global _rc_done
    if _rc_done:
        return
    plt.rcParams["font.sans-serif"] = _FONTS
    plt.rcParams["axes.unicode_minus"] = False
    _rc_done = True


def _save_png(fig) -> bytes:
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=DPI, facecolor="white",
                    bbox_inches="tight", pad_inches=0.16)
    finally:
        plt.close(fig)
    return buf.getvalue()


def _head(fig, fig_h: float, title: str, subtitle: str = "") -> None:
    """居中标题 +（可选）副标题。"""
    fig.text(0.5, 1 - 0.06 / fig_h, _clean(title), ha="center", va="top",
             fontsize=15, weight="bold", color=C_TITLE)
    if subtitle:
        fig.text(0.5, 1 - 0.43 / fig_h, _clean(subtitle), ha="center", va="top",
                 fontsize=9.5, color=C_SUB)


def _foot(fig, fig_h: float, note: str = DISCLAIMER, y_in: float = 0.10) -> None:
    fig.text(0.5, y_in / fig_h, _clean(note), ha="center", va="bottom",
             fontsize=8, color=C_SUB)


def _rule(fig, y_in: float, fig_h: float, x0: float, x1: float) -> None:
    fig.add_artist(Line2D([x0, x1], [1 - y_in / fig_h] * 2,
                          transform=fig.transFigure, color=C_RULE, lw=0.9))


# ------------------------------------------------------------
# 号码图：多列排布全部注号（一注一行，不截断）
# ------------------------------------------------------------
def render_numbers_image(
    lines: Sequence[str],
    *,
    title: str,
    subtitle: str = "",
    max_per_col: int = 40,
    ncol_max: int = 3,
) -> bytes:
    """把**已格式化好的号码行**渲染成 PNG（bytes）。

    lines      = ["03 11 21 23 29 32 + 05", ...]（由 push_notify.format_ticket_line 产出）
    max_per_col= 每列最多多少行，超出自动分列（最多 ncol_max 列）
    注数过多时图会变长（横向分列控制高度）；不省略任何一注。
    """
    _ensure_rc()
    items = [str(x) for x in (lines or []) if str(x).strip()]
    if not items:
        raise ValueError("没有可渲染的号码")

    n = len(items)
    per_col = max(1, int(max_per_col))
    ncol = max(1, min(int(ncol_max), -(-n // per_col)))
    nrow = -(-n // ncol)

    row_h = 0.30                      # 每行英寸
    head_h = 0.90 if subtitle else 0.62
    fig_w = 4.1 * ncol + 0.7
    fig_h = head_h + row_h * nrow + 0.36

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    _head(fig, fig_h, title, subtitle)

    body_top = head_h + 0.04
    span = fig_w - 0.50               # 可用横向英寸（右侧留白）
    for i, s in enumerate(items):
        c, r = divmod(i, nrow)
        x = (0.28 + c * span / ncol) / fig_w
        y = 1 - (body_top + r * row_h) / fig_h
        fig.text(x, y, f"{i + 1:>3}. {s}", ha="left", va="top",
                 fontsize=10.5, color=C_TEXT)

    _foot(fig, fig_h)
    return _save_png(fig)


# ------------------------------------------------------------
# 中奖图：期号 | 号码 | 命中 | 奖级
# ------------------------------------------------------------
_COLS = (("issue", "期号"), ("num", "号码"), ("match", "命中"), ("prize", "奖级"))
_COL_X = (0.02, 0.20, 0.62, 0.80)


def _win_row(r: Dict[str, Any]) -> Dict[str, str]:
    # 单元格文本一律 _clean：奖级/摘要里可能带 emoji，中文字体画不出来
    return {
        "issue": _clean(r.get("issue") or r.get("期号") or ""),
        "num": _clean(r.get("num") or r.get("号码") or ""),
        "match": _clean(r.get("match") or r.get("命中") or ""),
        "prize": _clean(r.get("prize") or r.get("奖级") or ""),
    }


def render_wins_image(
    records: Sequence[Dict[str, Any]],
    *,
    title: str,
    subtitle: str = "",
    row_max: int = 120,
) -> bytes:
    """把中奖记录渲染成 PNG（bytes）：表头 `期号 | 号码 | 命中 | 奖级`。

    records 里的键取 issue/num/match/prize（`push_notify` 的 hit_records 结构）；
    超出 row_max 条的只画前 row_max 条并在末尾注明（防极端长图）。
    """
    _ensure_rc()
    recs = [r for r in (records or []) if isinstance(r, dict)]
    if not recs:
        raise ValueError("没有可渲染的中奖记录")

    extra = 0
    if row_max and len(recs) > row_max:
        extra = len(recs) - row_max
        recs = recs[:row_max]

    row_h = 0.32
    head_h = 0.90 if subtitle else 0.62
    tbl_h = 0.40
    tail = 0.34 if extra else 0.0
    fig_w = 8.0
    fig_h = head_h + tbl_h + row_h * len(recs) + tail + 0.40

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    _head(fig, fig_h, title, subtitle)

    # 表头
    for (key, label), x in zip(_COLS, _COL_X):
        fig.text(x, 1 - (head_h + 0.20) / fig_h, label, ha="left", va="center",
                 fontsize=11, weight="bold", color=C_HEAD)
    _rule(fig, head_h + 0.34, fig_h, 0.02, 0.98)
    _rule(fig, head_h + tbl_h + row_h * len(recs) + 0.04, fig_h, 0.02, 0.98)

    for i, r in enumerate(recs):
        row = _win_row(r)
        y = 1 - (head_h + tbl_h + i * row_h + row_h / 2) / fig_h
        for (key, _label), x in zip(_COLS, _COL_X):
            fig.text(x, y, row[key], ha="left", va="center", fontsize=10.5,
                     color=C_WIN if key == "prize" else C_TEXT,
                     weight="bold" if key == "prize" else "normal")

    if extra:
        fig.text(0.02, 1 - (head_h + tbl_h + row_h * len(recs) + 0.20) / fig_h,
                 f"…另有 {extra} 条中奖记录，见看板", ha="left", va="center",
                 fontsize=9.5, color=C_SUB)

    _foot(fig, fig_h)
    return _save_png(fig)


__all__ = ["render_numbers_image", "render_wins_image", "DISCLAIMER"]
