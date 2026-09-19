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
from matplotlib.patches import Ellipse, Rectangle

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
    """剔除无字形字符（emoji 等），并把因此产生的多余空格收敛掉。

    ★/☆/◎ 等 CJK 字体必有的符号在**白名单**里保留（图片排版要用：★推荐使用）。
    """
    allow = {"★", "☆", "◎", "●", "◆"}
    out = []
    for ch in str(text):
        cp = ord(ch)
        if ch not in allow and any(lo <= cp <= hi for lo, hi in _EMOJI_RANGES):
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


__all__ = ["render_numbers_image", "render_tickets_image", "render_wins_image", "DISCLAIMER"]


# ------------------------------------------------------------
# 票面图（2026-09-18）：与看板「待开奖预测」弹窗同款排版——
# 彩色球珠 + 每 5 注一组打包 + 组头统计 + Top1 组标「★ 推荐使用」。
# 数据直接吃 pending 的票面 dict（号码/策略/置信度），与网页同一字段。
# ------------------------------------------------------------
ZONE_PALETTE = ["#E24B4A", "#1E90FF", "#2E9E5B", "#E0A020",
                "#8E44AD", "#16A085", "#D35400", "#34495E"]
C_RED = "#E24B4A"      # 与看板 .red-ball 同色系
C_BLUE = "#1E90FF"     # 与看板 .blue-ball 同色系
C_GROUP = "#6B7280"    # 组头灰
C_GREEN = "#27AE60"    # 推荐组绿
C_BAR = "#E5E7EB"      # 普通行左侧灰条

BALL_D = 0.235          # 球珠直径（英寸）
BALL_GAP = 0.045        # 球间距
ZONE_GAP = 0.14         # 分区间隔
ROW_H = 0.30            # 票行高
GRP_HEAD_H = 0.24       # 组头高
GRP_GAP = 0.16          # 组间距


def _zone_order(zones: Dict[str, Any]) -> List[str]:
    """分区顺序：数字型按『第N位』排序；乐透型 红→蓝。"""
    if any("位" in str(k) for k in zones):
        def _idx(k: str) -> int:
            try:
                return int(str(k).replace("第", "").replace("位", ""))
            except ValueError:
                return 99
        return sorted(zones.keys(), key=_idx)
    order = []
    for k in ("红球", "蓝球"):
        if k in zones:
            order.append(k)
    order += [k for k in zones if k not in order]
    return order


def _zone_color(zname: str, idx: int) -> str:
    if zname == "红球":
        return C_RED
    if zname == "蓝球":
        return C_BLUE
    return ZONE_PALETTE[idx % len(ZONE_PALETTE)]


def _ticket_balls(t: Dict[str, Any]) -> List[tuple]:
    """票面 → [(数字, 颜色), ...]（顺序即绘制顺序）。"""
    zones = t.get("号码")
    if not isinstance(zones, dict) or not zones:
        zones = {}
        if t.get("红球"):
            zones["红球"] = t["红球"]
        if t.get("蓝球"):
            zones["蓝球"] = t["蓝球"]
    out = []
    for zi, zname in enumerate(_zone_order(zones)):
        v = zones.get(zname)
        if isinstance(v, list):
            for n in v:
                out.append((str(int(n)).zfill(2), _zone_color(zname, zi)))
    return out


def _bundle_tickets(tickets: List[Dict[str, Any]], group_size: int) -> List[Dict[str, Any]]:
    """与看板 _bundleIntoGroups 同逻辑：每组 5 注，组内按置信度降序，
    整组按组内最大置信度降序，Top1 组 recommended=True（票面无持久化推荐分，
    用置信度做排序代理——诚实口径：只影响展示顺序，不影响任何概率）。"""
    groups = []
    for i in range(0, len(tickets), group_size):
        g = list(tickets[i:i + group_size])
        g.sort(key=lambda t: float(t.get("置信度") or 0), reverse=True)
        confs = [float(t.get("置信度") or 0) for t in g]
        strategies = []
        for t in g:
            s = str(t.get("策略") or "—")
            if s not in strategies:
                strategies.append(s)
        groups.append({"tickets": g,
                       "top": max(confs) if confs else 0.0,
                       "mean": (sum(confs) / len(confs)) if confs else 0.0,
                       "strategies": "+".join(strategies),
                       "recommended": False})
    groups.sort(key=lambda g: g["top"], reverse=True)
    if groups:
        groups[0]["recommended"] = True
    return groups


def render_tickets_image(
    tickets: Sequence[Dict[str, Any]],
    *,
    title: str,
    subtitle: str = "",
    group_size: int = 5,
    meta_lines: Sequence[str] = (),
) -> bytes:
    """把票面 dict 列表渲染成「看板弹窗同款」PNG（bytes）。

    - 每 `group_size` 注一组打包：组头 = 第N组（k 注）· 组内最大置信度 x% · 策略 …，
      Top1 组标绿色「★ 推荐使用」；组内票行 = 彩色球珠 + 策略/置信度注记；
    - 组块自动分列（1→2→3 列，按列高预算选最少列数），注数再多也不截断；
    - meta_lines：记录级元信息（第X期·目标开奖 / 质量压缩说明），第一行加粗。
    票面缺 `号码` 结构时抛 ValueError（调用方回退到旧 lines 渲染器 / 文本）。
    """
    _ensure_rc()
    tickets = [t for t in (tickets or []) if isinstance(t, dict)]
    if not tickets or not any(isinstance(t.get("号码"), dict) and t["号码"] for t in tickets):
        raise ValueError("没有可渲染的票面")

    groups = _bundle_tickets(tickets, max(1, int(group_size)))

    # ---- 布局预算：选最少列数使列高 ≤ 12 英寸 ----
    def block_h(g) -> float:
        return GRP_HEAD_H + ROW_H * len(g["tickets"]) + GRP_GAP

    heights = [block_h(g) for g in groups]
    ncol = 1
    for c in (1, 2, 3):
        per = -(-len(groups) // c)
        tallest = max(sum(heights[i * per:(i + 1) * per]) for i in range(c)) if len(groups) else 0
        # 近似：按块高降序分箱更均匀，这里用简单切块即可满足真实规模（≤ dozens 组）
        if tallest <= 12.0 or c == 3:
            ncol = c
            break

    per_col = -(-len(groups) // ncol)
    # 块高降序 + 轮转分箱（LPT），列高更均衡
    order = sorted(range(len(groups)), key=lambda i: heights[i], reverse=True)
    cols: List[List[int]] = [[] for _ in range(ncol)]
    col_h = [0.0] * ncol
    for bi in order:
        c = min(range(ncol), key=lambda k: col_h[k])
        cols[c].append(bi)
        col_h[c] += heights[bi]
    body_h = max(col_h) if col_h else 0.0

    meta_h = 0.26 * len(meta_lines) + (0.10 if meta_lines else 0.0)
    head_h = 0.92 if subtitle else 0.66
    fig_w = 4.45 * ncol + 0.45
    fig_h = head_h + meta_h + body_h + 0.42

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    _head(fig, fig_h, title, subtitle)

    # ---- 记录级元信息（第一行加粗，模拟看板卡片头） ----
    y_in = head_h + 0.06
    for i, ml in enumerate(meta_lines or []):
        fig.text(0.24 / fig_w, 1 - y_in / fig_h, _clean(ml),
                 ha="left", va="top",
                 fontsize=11.5 if i == 0 else 9,
                 weight="bold" if i == 0 else "normal",
                 color=C_TEXT if i == 0 else C_SUB)
        y_in += 0.26
    y_in += 0.10

    # ---- 组块（列间从左到右，列内按均衡分箱结果自上而下） ----
    span = (fig_w - 0.45) / ncol
    r = BALL_D / 2
    for c, blocks in enumerate(cols):
        col_x = 0.24 + c * span
        # 组头字符预算：9pt 中文 ≈ 0.13in/字，右侧给「★推荐使用」绿标留 ~1.0in
        budget = int((span - 1.05) / 0.130)
        y = y_in
        for bi in blocks:
            g = groups[bi]
            gno = groups.index(g) + 1
            prefix = f"第{gno}组（{len(g['tickets'])}注）· 最高置信度 {g['top'] * 100:.0f}% · "
            # 策略列表贪心截断：放不下就 "+…"
            names = g["strategies"].split("+")
            strat = ""
            for i, nm in enumerate(names):
                cand = nm if not strat else strat + "+" + nm
                if len(_clean(prefix + cand)) <= budget or not strat:
                    strat = cand
                else:
                    strat += "+…"
                    break
            head_txt = prefix + strat
            fig.text(col_x / fig_w, 1 - (y + GRP_HEAD_H * 0.72) / fig_h,
                     _clean(head_txt), ha="left", va="center",
                     fontsize=9, weight="bold", color=C_GROUP)
            if g["recommended"]:
                fig.text((col_x + span - 1.00) / fig_w,
                         1 - (y + GRP_HEAD_H * 0.72) / fig_h,
                         "★ 推荐使用", ha="left", va="center", fontsize=8,
                         color="white",
                         bbox=dict(boxstyle="round,pad=0.32", fc=C_GREEN, ec="none"))
            y += GRP_HEAD_H
            # 票行
            for t in g["tickets"]:
                bar_y0, bar_y1 = y + 0.035, y + ROW_H - 0.035
                fig.add_artist(Rectangle((col_x / fig_w, 1 - bar_y1 / fig_h),
                                         0.035 / fig_w, (bar_y1 - bar_y0) / fig_h,
                                         transform=fig.transFigure,
                                         facecolor=C_GREEN if g["recommended"] else C_BAR,
                                         edgecolor="none"))
                x = col_x + 0.09
                cy = y + ROW_H / 2
                for num, color in _ticket_balls(t):
                    # Ellipse 双轴分别按图宽/图高换算 → 物理尺寸都是直径 BALL_D，真圆
                    # （Circle 在 figure 分数坐标下半径被各向同性使用，窄图会被纵向拉长）
                    fig.add_artist(Ellipse((x / fig_w + (r / fig_w), 1 - cy / fig_h),
                                           width=2 * r / fig_w, height=2 * r / fig_h,
                                           transform=fig.transFigure,
                                           facecolor=color, edgecolor="none"))
                    fig.text(x / fig_w + (r / fig_w), 1 - cy / fig_h, num,
                             ha="center", va="center", fontsize=7.8,
                             weight="bold", color="white")
                    x += BALL_D + BALL_GAP
                conf = float(t.get("置信度") or 0)
                note = f"{_clean(t.get('策略') or '')}  置信度 {conf * 100:.0f}%".strip()
                fig.text((x + 0.10) / fig_w, 1 - cy / fig_h, note,
                         ha="left", va="center", fontsize=8.2, color=C_GROUP)
                y += ROW_H
            y += GRP_GAP

    _foot(fig, fig_h)
    return _save_png(fig)
