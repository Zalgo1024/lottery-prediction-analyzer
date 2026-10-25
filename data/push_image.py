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

# ------------------------------------------------------------
# 分区（"不要堆叠"的核心视觉手段，2026-10-24）
# ------------------------------------------------------------
# 每类内容（开奖号码 / 本期摘要 / 号码清单 / 中奖明细）各占一条**分区条**：
# 浅蓝底 + 左侧主色竖条 + 粗体小标题，把一张图切成互不混淆的几段。
C_SECTION = "#2F6DB5"
C_SECTION_BG = "#EDF3FB"
SEC_H = 0.32            # 分区条高度（英寸）
SEC_GAP = 0.16          # 分区条与下文间距

# ------------------------------------------------------------
# 分页预算（企微图片 2MB 上限 + 手机可读性）
# ------------------------------------------------------------
# 实测：双色球 200 注画成**一张**图 = 1062 KB / 3240 px 高，手机上要一直缩放；
# 50 注/页 ≈ 330 KB / 1368 px，离 2MB 有 6 倍余量。故一律分页。
PAGE_BODY_H = 9.6       # 单列主体高度上限（英寸），120 dpi ≈ 1150 px
PAGE_TICKETS = 50       # 出号图每页注数（10 组 × 5 注）
PAGE_WINS = 6           # 中奖图每页**卡片数**（2026-10-24 由表格 18 行改为卡片 6 张）
PAGE_LINES = 60         # 行式兜底图每页行数
TICKET_COLS = 2         # 分页后固定最多 2 列（3 列在手机上字太小）

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


def _section_bar(fig, *, x_in: float, y_in: float, w_in: float, text: str,
                 fig_w: float, fig_h: float, h_in: float = SEC_H) -> None:
    """画一条**分区条**：浅蓝底块 + 左侧主色竖条 + 粗体小标题。

    y_in 是条**顶部**距图顶的英寸数；宽度 w_in、高度 h_in 同单位。
    text 为空时不画（调用方据此跳过整段）。
    """
    if not str(text or "").strip():
        return
    bottom = 1 - (y_in + h_in) / fig_h
    fig.add_artist(Rectangle((x_in / fig_w, bottom), w_in / fig_w, h_in / fig_h,
                             transform=fig.transFigure,
                             facecolor=C_SECTION_BG, edgecolor="none"))
    fig.add_artist(Rectangle((x_in / fig_w, bottom), 0.05 / fig_w, h_in / fig_h,
                             transform=fig.transFigure,
                             facecolor=C_SECTION, edgecolor="none"))
    fig.text((x_in + 0.17) / fig_w, 1 - (y_in + h_in / 2) / fig_h,
             _clean(text), ha="left", va="center",
             fontsize=9.8, weight="bold", color=C_SECTION)


def _page_tag(page: Optional[Sequence[int]]) -> str:
    """`(2, 5)` → `第 2/5 页`；单页也写 `第 1/1 页`（让人一眼知道没有后续）。"""
    if not page:
        return ""
    try:
        i, n = int(page[0]), int(page[1])
    except (TypeError, IndexError, ValueError):
        return ""
    if i < 1 or n < 1:
        return ""
    return "第 1/1 页" if n <= 1 else f"第 {i}/{n} 页"


def _with_page(subtitle: str, page: Optional[Sequence[int]]) -> str:
    """把页码拼进副标题；**副标题里已有同一页码就不再重复**。

    `第 1/4 页（第 1–50 注）` 这种更详细的写法很自然，若照抄拼接会变成
    「第 1/4 页（第 1–50 注）· 第 1/4 页」。
    """
    tag = _page_tag(page)
    if not tag:
        return subtitle
    if tag in (subtitle or ""):
        return subtitle
    return f"{subtitle} · {tag}" if subtitle else tag


def _zones_of(item: Any) -> Dict[str, Any]:
    """统一取分区：优先 `号码`，否则回落到顶层 `红球`/`蓝球`。

    ★ 一律用**真值**过滤而不是 `is not None`：数字型彩种的记录身上也有
    `红球` 属性，值是 `[]`，按 `is not None` 判会画出一个空分区
    （2026-10-24 开奖号码整行丢失就是同款写法导致的）。
    """
    if not isinstance(item, dict):
        return {}
    zones = item.get("号码")
    if isinstance(zones, dict) and zones:
        return {k: v for k, v in zones.items() if v}
    out: Dict[str, Any] = {}
    for k in ("红球", "蓝球"):
        v = item.get(k)
        if v:
            out[k] = v
    return out


# ------------------------------------------------------------
# 号码图：多列排布全部注号（一注一行，不截断）
# ------------------------------------------------------------
def render_numbers_image(
    lines: Sequence[str],
    *,
    title: str,
    subtitle: str = "",
    max_per_col: int = 30,
    ncol_max: int = 2,
    section: str = "号码清单",
    page: Optional[Sequence[int]] = None,
) -> bytes:
    """把**已格式化好的号码行**渲染成 PNG（bytes）。

    lines      = ["03 11 21 23 29 32 + 05", ...]（由 push_notify.format_ticket_line 产出）
    max_per_col= 每列最多多少行，超出自动分列（最多 ncol_max 列）

    这是**行式兜底**：票面缺 `号码` 结构（老台账）时才用，
    正常路径走 `render_tickets_image`（球珠 + 分组）。行式图本身不分页，
    调用方应按 `PAGE_LINES` 切片后再调。
    """
    _ensure_rc()
    items = [str(x) for x in (lines or []) if str(x).strip()]
    if not items:
        raise ValueError("没有可渲染的号码")

    subtitle = _with_page(subtitle, page)

    n = len(items)
    per_col = max(1, int(max_per_col))
    ncol = max(1, min(int(ncol_max), -(-n // per_col)))
    nrow = -(-n // ncol)

    sec_h = (SEC_H + SEC_GAP) if section else 0.0
    row_h = 0.30                      # 每行英寸
    head_h = 0.90 if subtitle else 0.62
    fig_w = 4.1 * ncol + 0.7
    fig_h = head_h + sec_h + row_h * nrow + 0.36

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    _head(fig, fig_h, title, subtitle)

    if section:
        _section_bar(fig, x_in=0.20, y_in=head_h + 0.02, w_in=fig_w - 0.40,
                     text=section, fig_w=fig_w, fig_h=fig_h)

    body_top = head_h + sec_h + 0.04
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
# 中奖图：**逐注卡片**（2026-10-24 由「期号|号码|命中|奖级」表格改版）
# ------------------------------------------------------------
# 为什么改：表格只能说明「这注中了六等奖」，看不出**到底是哪几个球中的**。
# 卡片把【预测号码】与【实际开奖】并排画成球珠——
#   · 命中球 = 分区色实心 + 白字
#   · 没中的球 = 白底灰环 + 灰字
# 一眼就能看出具体哪些中了，与看板记录卡片同构。
C_MISS = "#9AA3AF"          # 未命中球：灰字
C_MISS_BG = "#EDF0F4"       # 未命中球：淡灰实心底（比空心更好认，缩略图里也能分辨）
C_CARD_BD = "#DCE3EC"       # 卡片描边
C_CHIP_BG = "#EEF1F5"       # 灰药丸（命中 x/y）
C_BADGE_WIN_BG = "#FDECEA"  # 奖级药丸
C_BADGE_WIN_FG = "#C0392B"
C_BADGE_OK_BG = "#E8F5EC"   # 「预测」绿药丸
C_BADGE_OK_FG = "#21874B"
C_BADGE_PAY_BG = "#FFF6D9"  # 奖金黄药丸
C_BADGE_PAY_FG = "#8A6A00"

CARD_PAD = 0.14             # 卡片内边距（英寸）
CARD_HEAD_H = 0.34          # 卡片头（奖级 / 期号 / 命中 / 奖金）
CARD_ROW_H = 0.34           # 每个号码行（预测 / 实际）
CARD_META_H = 0.235         # 每行元信息
CARD_GAP = 0.17             # 卡片之间
CARD_LABEL_W = 1.22         # 「预测号码 / 实际开奖」标签占宽


_CN_NUM = "一二三四五六七八九"


def _grade_label(g: Any) -> str:
    """「六等」→「六等奖」；「组选6」等玩法名原样返回。

    台账里两种写法并存（双色球/大乐透存「六等」，数字型存「六等奖」），
    图上一律带「奖」更好读。
    """
    s = _clean(g)
    if len(s) == 2 and s[0] in _CN_NUM and s[1] == "等":
        return s + "奖"
    return s


def _txt_w(s: str, fontsize: float) -> float:
    """粗略估算文本宽度（英寸）：CJK 按 1em、西文按 0.55em。

    药丸是**顺序排布**的，必须知道前一个占多宽才能放下一个；matplotlib 没有
    简单的 measure 接口，用字宽近似足够（宁可估宽一点，留白比重叠好）。
    """
    w = 0.0
    for ch in str(s):
        w += 1.0 if ord(ch) > 0x2E80 else 0.55
    return w * fontsize / 72.0


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _rec_zones(r: Dict[str, Any], which: str) -> Dict[str, Any]:
    """从历史记录里取**结构化号码**：乐透走 红球/蓝球，数字型走各位置。"""
    if which == "pred":
        z, keys = r.get("预测号码"), ("预测红球", "预测蓝球")
    else:
        z, keys = r.get("实际号码"), ("实际红球", "实际蓝球")
    if isinstance(z, dict) and z:
        return {k: v for k, v in z.items() if v}
    out: Dict[str, Any] = {}
    for k in keys:
        v = r.get(k)
        if v:
            out[k.replace("预测", "").replace("实际", "")] = v
    return out


def _zone_hits(pred: Dict[str, Any], act: Dict[str, Any]) -> Dict[str, set]:
    """逐分区比对 → {分区名: 命中的号码集合}（数字型每分区 1 值也适用）。"""
    out: Dict[str, set] = {}
    for zname in _zone_order(pred):
        aset = {n for n in (_as_int(x) for x in (act.get(zname) or [])) if n is not None}
        out[zname] = {n for n in (_as_int(x) for x in (pred.get(zname) or []))
                      if n is not None and n in aset}
    return out


def _hit_ratio(r: Dict[str, Any]) -> str:
    """卡片头右侧的「命中 3/7」。"""
    h = _as_int(r.get("总命中"))
    if h is None:
        return ""
    t = _as_int(r.get("总选择"))
    if not t:
        t = (len(r.get("预测红球") or []) + len(r.get("预测蓝球") or [])) or None
    return f"命中 {h}/{t}" if t else f"命中 {h}"


def _hit_breakdown(r: Dict[str, Any]) -> str:
    """「分区命中」的紧凑文案：乐透 `红球2/6 · 蓝球1/1`，数字型 `第4位·第7位（2/7）`。"""
    zh = r.get("分区命中")
    if isinstance(zh, dict) and zh:
        names = [_clean(k) for k, v in zh.items() if v]
        n, tot = len(names), len(zh)
        if all("位" in str(k) for k in zh):
            return (f"{'·'.join(names)}（{n}/{tot}）" if names else f"全部未中（0/{tot}）")
        return f"{n}/{tot}"
    rh, bh = _as_int(r.get("红球命中")), _as_int(r.get("蓝球命中"))
    pred, act = _rec_zones(r, "pred"), _rec_zones(r, "act")
    pr = r.get("预测红球") or (pred.get("红球") or [])
    pb = r.get("预测蓝球") or (pred.get("蓝球") or [])
    # 记录里没写命中数（老台账/精简结构）→ 用结构化号码现场比对，别让这行空着
    if (rh is None or bh is None) and pred and act:
        h = _zone_hits(pred, act)
        if rh is None:
            rh = len(h.get("红球") or ())
        if bh is None:
            bh = len(h.get("蓝球") or ())
    seg = []
    if pr:
        seg.append(f"红球{rh or 0}/{len(pr)}")
    if pb:
        seg.append(f"蓝球{bh or 0}/{len(pb)}")
    return " · ".join(seg)


def _gamma_text(r: Dict[str, Any]) -> str:
    """「机制倾斜 未激活（γ=-0.0111）」——口径与看板记录卡片一致。"""
    md = r.get("机制倾斜")
    g = r.get("机制γ")
    if not isinstance(md, dict):
        return ""
    if g is None:
        g = md.get("γ")
    try:
        gs = f"{float(g):+.4f}"
    except (TypeError, ValueError):
        gs = ""
    on = bool(md.get("倾斜"))
    return f"机制倾斜 {'已激活' if on else '未激活'}" + (f"（γ={gs}）" if gs else "")


def _win_meta_lines(r: Dict[str, Any]) -> List[str]:
    """卡片底部的元信息行（最多 2 行），字段与看板记录卡片对齐：

    行1：单注奖金（来源）· 分区命中 · 策略+置信度
    行2：机制倾斜 · 第k注/共N注 · 来源 · 预测日期 · 开奖日期

    ★ 分隔符用单空格 ` · `：8.2in 宽的图上要塞 5~6 个字段，双空格会溢出。
    """
    seg1: List[str] = []
    pay = r.get("奖金") if isinstance(r.get("奖金"), dict) else {}
    if pay.get("文本"):
        s = f"单注奖金 {_clean(pay['文本'])}"
        if pay.get("来源"):
            s += f"（{_clean(pay['来源'])}）"
        seg1.append(s)
    bd = _hit_breakdown(r)
    if bd:
        seg1.append(bd)
    if r.get("策略"):
        s = f"策略 {_clean(r['策略'])}"
        try:
            if r.get("置信度") is not None:
                s += f" {float(r['置信度']) * 100:.0f}%"
        except (TypeError, ValueError):
            pass
        seg1.append(s)

    seg2: List[str] = []
    g = _gamma_text(r)
    if g:
        seg2.append(g)
    seq, tot = _as_int(r.get("批次内序号")), _as_int(r.get("批次规模"))
    if seq and tot:
        seg2.append(f"第 {seq} 注 / 共 {tot} 注")
    if r.get("来源"):
        seg2.append(f"来源 {_clean(r['来源'])}")
    if r.get("预测日期"):
        seg2.append(f"预测 {r['预测日期']}")
    if r.get("开奖日期"):
        seg2.append(f"开奖 {r['开奖日期']}")

    out = [" · ".join(x) for x in (seg1, seg2) if x]
    if not out:
        m = _clean(r.get("match") or r.get("命中") or "")
        if m:
            out.append(m)
    return out


def _card_h(r: Dict[str, Any]) -> float:
    rows = 2 if (_rec_zones(r, "pred") and _rec_zones(r, "act")) else 1
    return (CARD_PAD * 2 + CARD_HEAD_H + CARD_ROW_H * rows
            + CARD_META_H * len(_win_meta_lines(r)))


def _pill(fig, x_in: float, cy_in: float, text: str, *, fontsize: float,
          bg: str, fg: str, fig_w: float, fig_h: float,
          ha: str = "left", weight: str = "bold") -> float:
    """画一颗药丸（圆角底 + 文字），返回**文字右/左端**的 x（英寸，取决于 ha）。"""
    if not text:
        return x_in
    fig.text(x_in / fig_w, 1 - cy_in / fig_h, text, ha=ha, va="center",
             fontsize=fontsize, weight=weight, color=fg,
             bbox=dict(boxstyle="round,pad=0.30", fc=bg, ec="none"))
    w = _txt_w(text, fontsize) + 0.16        # 药丸左右 pad
    return x_in + w if ha == "left" else x_in - w


def _ball_row(fig, *, x_in: float, y_in: float, label: str,
              zones: Dict[str, Any], hits: Optional[Dict[str, set]],
              fig_w: float, fig_h: float) -> None:
    """一行号码：左侧标签 + 球珠。`hits=None` 表示不判命中（实际开奖行）。"""
    r = BALL_D / 2
    cy = y_in + CARD_ROW_H / 2
    fig.text((x_in + CARD_PAD + 0.06) / fig_w, 1 - cy / fig_h, label,
             ha="left", va="center", fontsize=8.8, color=C_SUB)
    x = x_in + CARD_PAD + 0.06 + CARD_LABEL_W
    digital = any("位" in str(k) for k in zones)
    first = True
    for zi, zname in enumerate(_zone_order(zones)):
        vals = zones.get(zname)
        if not isinstance(vals, list) or not vals:
            continue
        if not first:
            x += ZONE_GAP          # 红区 / 蓝区（或各位之间）拉开间距
        first = False
        hset = (hits or {}).get(zname) or set()
        color = _zone_color(zname, zi)
        for n in vals:
            num = _as_int(n)
            if num is None:
                continue
            lit = hits is None or num in hset     # 实心彩色 = 命中（或本行不判命中）
            cx = x / fig_w + r / fig_w
            # Ellipse 双轴分别按图宽/图高换算 → 物理尺寸真为 BALL_D，不被拉扁
            fig.add_artist(Ellipse((cx, 1 - cy / fig_h),
                                   width=2 * r / fig_w, height=2 * r / fig_h,
                                   transform=fig.transFigure,
                                   facecolor=color if lit else C_MISS_BG,
                                   edgecolor="none" if lit else "#D5DBE3",
                                   lw=0 if lit else 1.2))
            fig.text(cx, 1 - cy / fig_h,
                     str(num) if digital else str(num).zfill(2),
                     ha="center", va="center", fontsize=7.8, weight="bold",
                     color="white" if lit else C_MISS)
            x += BALL_D + BALL_GAP


def _win_card(fig, *, x_in: float, y_in: float, w_in: float, h_in: float,
              rec: Dict[str, Any], fig_w: float, fig_h: float) -> None:
    """画一张中奖卡片（边框 + 卡片头 + 号码行 + 元信息行）。"""
    # 卡片底 + 左侧色条（中奖一律主色，暗示"这是中奖记录"）
    bottom = 1 - (y_in + h_in) / fig_h
    fig.add_artist(Rectangle((x_in / fig_w, bottom), w_in / fig_w, h_in / fig_h,
                             transform=fig.transFigure, facecolor="white",
                             edgecolor=C_CARD_BD, lw=1.0))
    fig.add_artist(Rectangle((x_in / fig_w, bottom), 0.045 / fig_w, h_in / fig_h,
                             transform=fig.transFigure, facecolor=C_WIN,
                             edgecolor="none", alpha=0.55))

    cy = y_in + CARD_PAD + CARD_HEAD_H / 2
    x = x_in + CARD_PAD + 0.06

    # ① 奖级药丸（"六等" → "六等奖"：台账里两种写法并存，展示统一带「奖」）
    prize = _grade_label(rec.get("prize") or rec.get("奖级") or "中奖")
    x = _pill(fig, x, cy, prize, fontsize=9.5, bg=C_BADGE_WIN_BG,
              fg=C_BADGE_WIN_FG, fig_w=fig_w, fig_h=fig_h) + 0.10

    # ② 彩种 · 第 N 期
    lot = _clean(rec.get("lottery") or rec.get("彩种") or "")
    issue = _clean(rec.get("issue") or rec.get("期号") or "")
    head = f"{lot} · 第 {issue} 期" if lot else f"第 {issue} 期"
    fig.text(x / fig_w, 1 - cy / fig_h, head, ha="left", va="center",
             fontsize=10.5, weight="bold", color=C_TITLE)
    x += _txt_w(head, 10.5) + 0.12

    # ③「预测」绿药丸（只有真预测才算；训练记录不标）
    #   ★ 注意别用 ✓（U+2713）：Microsoft YaHei 没有该字形，会渲染成豆腐块。
    if rec.get("valid") or rec.get("valid_prediction"):
        x = _pill(fig, x, cy, "√ 预测", fontsize=8.2, bg=C_BADGE_OK_BG,
                  fg=C_BADGE_OK_FG, fig_w=fig_w, fig_h=fig_h) + 0.08

    # ④ 奖金黄药丸（浮动奖级的文案可能很长，过长就不画药丸——元信息行里还有一份）
    pay = rec.get("奖金") if isinstance(rec.get("奖金"), dict) else {}
    pay_txt = _clean(pay.get("文本") or "")
    if pay_txt and len(pay_txt) <= 12 and x < x_in + w_in - CARD_PAD - 1.55:
        _pill(fig, x, cy, pay_txt, fontsize=8.6,
              bg=C_BADGE_PAY_BG, fg=C_BADGE_PAY_FG,
              fig_w=fig_w, fig_h=fig_h)

    # ⑤ 右侧「命中 x/y」
    ratio = _hit_ratio(rec)
    if ratio:
        _pill(fig, x_in + w_in - CARD_PAD, cy, ratio, fontsize=8.6,
              bg=C_CHIP_BG, fg="#4B5563", fig_w=fig_w, fig_h=fig_h, ha="right")

    # ---- 号码行 ----
    y = y_in + CARD_PAD + CARD_HEAD_H
    pred, act = _rec_zones(rec, "pred"), _rec_zones(rec, "act")
    if pred and act:
        hits = _zone_hits(pred, act)
        _ball_row(fig, x_in=x_in, y_in=y, label="预测号码", zones=pred,
                  hits=hits, fig_w=fig_w, fig_h=fig_h)
        y += CARD_ROW_H
        _ball_row(fig, x_in=x_in, y_in=y, label="实际开奖", zones=act,
                  hits=None, fig_w=fig_w, fig_h=fig_h)
        y += CARD_ROW_H
    else:
        # 降级：老台账没有结构化号码 → 退回一行文本（信息不丢，只是没有球珠）
        txt = _clean(rec.get("num") or rec.get("号码") or "")
        fig.text((x_in + CARD_PAD + 0.06) / fig_w, 1 - (y + CARD_ROW_H / 2) / fig_h,
                 txt, ha="left", va="center", fontsize=10.5, color=C_TEXT)
        y += CARD_ROW_H

    # ---- 元信息行 ----
    for line in _win_meta_lines(rec):
        fig.text((x_in + CARD_PAD + 0.06) / fig_w,
                 1 - (y + CARD_META_H / 2) / fig_h, line,
                 ha="left", va="center", fontsize=8.0, color=C_SUB)
        y += CARD_META_H


def render_wins_image(
    records: Sequence[Dict[str, Any]],
    *,
    title: str,
    subtitle: str = "",
    row_max: int = PAGE_WINS,
    section: str = "中奖明细",
    page: Optional[Sequence[int]] = None,
) -> bytes:
    """把中奖记录渲染成 PNG（bytes）：分区条 + 每注一张**卡片**。

    卡片内容（与看板记录卡片同款字段）：
      头：奖级药丸 ·「彩种 · 第 N 期」·「✓ 预测」· 奖金药丸 ……（右）命中 x/y
      体：预测号码（命中球实心、未中灰环） / 实际开奖（全实心）
      脚：单注奖金 · 分区命中 · 策略+置信度 ／ 机制倾斜 · 第k注 · 预测/开奖日期

    ★「具体哪些中了」就靠**球珠实心 vs 灰环**表达——这是本图存在的理由，
      表格版只能说"中了六等奖"却说不出是哪几个球。
    records 里的键取 push_notify 的 hit_records 结构（兼容只有 issue/num/prize
    的老结构：无结构化号码时退化成一行文本卡片）。
    page=(i,N) 会把「第 i/N 页」拼进副标题；N=1 也写，让人知道没有后续。
    """
    _ensure_rc()
    recs = [r for r in (records or []) if isinstance(r, dict)]
    if not recs:
        raise ValueError("没有可渲染的中奖记录")

    extra = 0
    if row_max and len(recs) > row_max:
        extra = len(recs) - row_max
        recs = recs[:row_max]

    subtitle = _with_page(subtitle, page)

    fig_w = 8.2
    sec_h = (SEC_H + SEC_GAP) if section else 0.0
    head_h = 0.92 if subtitle else 0.66
    hs = [_card_h(r) for r in recs]
    body_h = sum(hs) + CARD_GAP * max(0, len(recs) - 1)
    tail = 0.34 if extra else 0.0
    fig_h = head_h + sec_h + body_h + tail + 0.44

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    _head(fig, fig_h, title, subtitle)

    if section:
        _section_bar(fig, x_in=0.20, y_in=head_h + 0.02, w_in=fig_w - 0.40,
                     text=section, fig_w=fig_w, fig_h=fig_h)

    y = head_h + sec_h + 0.02
    for rec, h in zip(recs, hs):
        _win_card(fig, x_in=0.20, y_in=y, w_in=fig_w - 0.40, h_in=h,
                  rec=rec, fig_w=fig_w, fig_h=fig_h)
        y += h + CARD_GAP

    if extra:
        fig.text(0.20 / fig_w, 1 - (y + 0.10) / fig_h,
                 f"…另有 {extra} 条中奖记录，见看板", ha="left", va="top",
                 fontsize=9.5, color=C_SUB)

    _foot(fig, fig_h)
    return _save_png(fig)


__all__ = ["render_numbers_image", "render_tickets_image", "render_wins_image",
           "render_draw_image", "paginate_tickets", "DISCLAIMER",
           "PAGE_TICKETS", "PAGE_WINS", "PAGE_LINES", "PAGE_BODY_H", "TICKET_COLS"]


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

# 开奖球（比票面球大得多：一张图就 5~7 个球，是"今晚开奖"的主角）
BALL_BIG = 0.54         # 开奖球直径（英寸）
BALL_BIG_GAP = 0.12     # 开奖球间距
BALL_BIG_ZGAP = 0.20    # 开奖球「红区 / 蓝区」之间的额外间隔


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


def _draw_tokens(draw: Dict[str, Any]) -> List[tuple]:
    """开奖号码 → [(两位数文本, 颜色, 分区序号), ...]。"""
    zones = _zones_of(draw)
    toks: List[tuple] = []
    for zi, zname in enumerate(_zone_order(zones)):
        v = zones.get(zname)
        if isinstance(v, list):
            for n in v:
                toks.append((str(int(n)).zfill(2), _zone_color(zname, zi), zi))
    return toks


def render_draw_image(
    draw: Dict[str, Any],
    *,
    lottery: str = "",
    title: str = "开奖号码",
    subtitle: str = "",
    notes: Sequence[str] = (),
    heading: str = "开奖号码",
    notes_heading: str = "本期摘要",
) -> bytes:
    """「今晚开奖」图：大号球珠 + 本期摘要（PNG bytes）。

    draw 直接吃 `push_notify._latest_draw` 那份 dict——
    乐透型走 `红球/蓝球`，数字型走 `号码={"第1位": ...}`，两者都由 `_zones_of` 归一。
    没有号码时抛 ValueError（调用方据此**跳过这张图**，不要画一张空图）。

    版式：标题 → 副标题 →「开奖号码」分区条 → 球珠行（居中）→
    「本期摘要」分区条（notes 非空才画）→ 诚实声明。
    """
    _ensure_rc()
    toks = _draw_tokens(draw or {})
    if not toks:
        raise ValueError(f"{lottery or '该彩种'}没有可渲染的开奖号码")

    fig_w = 7.0
    head_h = 0.92 if subtitle else 0.66
    note_list = [c for c in (_clean(x) for x in (notes or [])) if c]

    num_top = head_h + 0.04
    y = num_top + SEC_H + 0.16
    ball_cy = y + BALL_BIG / 2
    y += BALL_BIG + 0.30
    note_top = y
    if note_list:
        y += SEC_H + 0.14 + 0.26 * len(note_list) + 0.10
    fig_h = y + 0.44

    fig = plt.figure(figsize=(fig_w, fig_h), dpi=DPI)
    _head(fig, fig_h, title, subtitle)

    # ---- 分区①：开奖号码 ----
    _section_bar(fig, x_in=0.22, y_in=num_top, w_in=fig_w - 0.44,
                 text=heading, fig_w=fig_w, fig_h=fig_h)

    # 球珠行整体居中；红区/蓝区之间多留一点间隔
    total = BALL_BIG
    for i in range(1, len(toks)):
        total += BALL_BIG + (BALL_BIG_ZGAP if toks[i][2] != toks[i - 1][2]
                             else BALL_BIG_GAP)
    x = max(0.10, (fig_w - total) / 2)
    r = BALL_BIG / 2
    for i, (num, color, zi) in enumerate(toks):
        if i:
            x += BALL_BIG_ZGAP if zi != toks[i - 1][2] else BALL_BIG_GAP
        fig.add_artist(Ellipse((x / fig_w + r / fig_w, 1 - ball_cy / fig_h),
                               width=2 * r / fig_w, height=2 * r / fig_h,
                               transform=fig.transFigure,
                               facecolor=color, edgecolor="none"))
        fig.text(x / fig_w + r / fig_w, 1 - ball_cy / fig_h, num,
                 ha="center", va="center", fontsize=17, weight="bold",
                 color="white")
        x += BALL_BIG

    # ---- 分区②：本期摘要（有内容才画） ----
    if note_list:
        _section_bar(fig, x_in=0.22, y_in=note_top, w_in=fig_w - 0.44,
                     text=notes_heading, fig_w=fig_w, fig_h=fig_h)
        ny = note_top + SEC_H + 0.14
        for s in note_list:
            fig.text(0.40 / fig_w, 1 - ny / fig_h, s, ha="left", va="top",
                     fontsize=10.5, color=C_TEXT)
            ny += 0.26

    _foot(fig, fig_h)
    return _save_png(fig)


def _ticket_balls(t: Dict[str, Any]) -> List[tuple]:
    """票面 → [(数字, 颜色), ...]（顺序即绘制顺序）。"""
    zones = _zones_of(t)
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
    # ★ 组号在**全局**分组时定下（分页后每页各自 index 会让编号从 1 重来，
    #   同一批号在第 2 页又出现「第1组」，与"共 N 注"对不上）
    for i, g in enumerate(groups, 1):
        g["no"] = i
    if groups:
        groups[0]["recommended"] = True
    return groups


def paginate_tickets(
    tickets: Sequence[Dict[str, Any]],
    *,
    group_size: int = 5,
    ncol: int = TICKET_COLS,
    body_max_h: float = PAGE_BODY_H,
) -> List[List[Dict[str, Any]]]:
    """把票面切成若干页（每页是一个 group 列表），供逐页渲染。

    ★ **先全局分组、再切页**。若让每页各自调 `_bundle_tickets`，
      Top1 组会在**每一页**各标一次「★ 推荐使用」，推荐语义被放大 N 倍，
      组号也会每页从 1 重来。`render_tickets_image(groups=页)` 透传即锁住这点。

    200 注 → 40 组 → 容量 `9.6in × 2 列 = 19.2in` → 每页 10 组 → 4 页。
    """
    ts = [t for t in (tickets or []) if isinstance(t, dict)]
    if not ts:
        return []
    groups = _bundle_tickets(ts, max(1, int(group_size)))
    cap = max(1.0, float(body_max_h)) * max(1, int(ncol))
    pages: List[List[Dict[str, Any]]] = []
    cur: List[Dict[str, Any]] = []
    h = 0.0
    for g in groups:
        bh = GRP_HEAD_H + ROW_H * len(g["tickets"]) + GRP_GAP
        if cur and h + bh > cap:
            pages.append(cur)
            cur, h = [], 0.0
        cur.append(g)
        h += bh
    if cur:
        pages.append(cur)
    return pages


def render_tickets_image(
    tickets: Sequence[Dict[str, Any]],
    *,
    title: str,
    subtitle: str = "",
    group_size: int = 5,
    meta_lines: Sequence[str] = (),
    groups: Optional[Sequence[Dict[str, Any]]] = None,
    section: str = "号码清单",
    page: Optional[Sequence[int]] = None,
    body_max_h: float = PAGE_BODY_H,
) -> bytes:
    """把票面 dict 列表渲染成「看板弹窗同款」PNG（bytes）。

    - 每 `group_size` 注一组打包：组头 = 第N组（k 注）· 组内最大置信度 x% · 策略 …，
      Top1 组标绿色「★ 推荐使用」；组内票行 = 彩色球珠 + 策略/置信度注记；
    - 组块自动分列（1 → `TICKET_COLS` 列，按 `body_max_h` 预算选最少列数）；
    - `meta_lines`：记录级元信息（第X期·目标开奖 / 质量压缩说明），第一行加粗。
    - **`groups` 透传**：分页时把 `paginate_tickets()` 切好的一页传进来，
      避免每页各自重排（否则每页都冒出「★ 推荐使用」、组号也从 1 重来）。
    - `section` 分区条 / `page` 页码：把一张图切成"有格式的几段"。
    票面缺 `号码` 结构时抛 ValueError（调用方回退到旧 lines 渲染器 / 文本）。
    """
    _ensure_rc()
    tickets = [t for t in (tickets or []) if isinstance(t, dict)]
    if not tickets or not any(isinstance(t.get("号码"), dict) and t["号码"] for t in tickets):
        raise ValueError("没有可渲染的票面")

    if groups:
        groups = list(groups)
    else:
        groups = _bundle_tickets(tickets, max(1, int(group_size)))

    subtitle = _with_page(subtitle, page)

    # ---- 布局预算：选最少列数使列高 ≤ body_max_h ----
    def block_h(g) -> float:
        return GRP_HEAD_H + ROW_H * len(g["tickets"]) + GRP_GAP

    heights = [block_h(g) for g in groups]
    ncol = 1
    for c in dict.fromkeys((1, max(1, int(TICKET_COLS)))):
        per = -(-len(groups) // c)
        tallest = max(sum(heights[i * per:(i + 1) * per]) for i in range(c)) if len(groups) else 0
        # 近似：按块高降序分箱更均匀，这里用简单切块即可满足真实规模（≤ dozens 组）
        if tallest <= float(body_max_h) or c == max(1, int(TICKET_COLS)):
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
    sec_h = (SEC_H + SEC_GAP) if section else 0.0
    head_h = 0.92 if subtitle else 0.66
    fig_w = 4.45 * ncol + 0.45
    fig_h = head_h + meta_h + sec_h + body_h + 0.42

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

    # ---- 分区条：把"元信息 / 号码清单"两段明确切开，避免整张图糊成一片 ----
    if section:
        _section_bar(fig, x_in=0.24, y_in=y_in, w_in=fig_w - 0.48,
                     text=section, fig_w=fig_w, fig_h=fig_h)
        y_in += SEC_H + SEC_GAP

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
            # 组号优先取全局分组时定下的 no（分页后不从头编号）
            gno = g.get("no") or (groups.index(g) + 1)
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
