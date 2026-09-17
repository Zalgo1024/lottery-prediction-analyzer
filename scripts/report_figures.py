# -*- coding: utf-8 -*-
"""结题报告配图生成脚本（只读项目数据，产出 docs/assets/*.png）。

所有数字来自项目真实输出：
- 样本量需求：n > 78.4/p0（α=.05, power=.8，泊松两样本近似），见 logs/样本量估算_*.md
- 流行度模型：cache/popularity_model_双色球.json（n=3502, LRT χ²=948.85, p=1.84e-198, CV 5/5）
- 漂移分段/封顶对比：ev/popularity.drift_report 输出（记录于 .workbuddy/memory/2026-09-12.md）
"""
import os

import matplotlib
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

matplotlib.use("Agg")

plt.rcParams["font.sans-serif"] = ["Microsoft YaHei", "SimHei"]
plt.rcParams["axes.unicode_minus"] = False

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "assets")
os.makedirs(OUT, exist_ok=True)

# 统一配色（浅色底报告用）
C_MAIN = "#2F6DB5"      # 主蓝
C_DEEP = "#1B3A5C"      # 深蓝
C_GRAY = "#8A94A6"      # 灰
C_LIGHT = "#E8F0FA"     # 浅蓝底
C_RED = "#C0392B"       # 强调红
C_GREEN = "#2E7D32"     # 通过绿
C_BG = "#FFFFFF"

def _box(ax, x, y, w, h, text, fc=C_LIGHT, ec=C_MAIN, fs=11, tc="#1F2937", lw=1.2, bold=False):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.02,rounding_size=0.015",
                                fc=fc, ec=ec, lw=lw))
    ax.text(x + w / 2, y + h / 2, text, ha="center", va="center", fontsize=fs,
            color=tc, weight="bold" if bold else "normal", linespacing=1.5)

def _arrow(ax, x1, y1, x2, y2, color=C_DEEP, lw=1.6, style="-|>"):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                                 mutation_scale=16, color=color, lw=lw))

# ---------------------------------------------------------------- 图1 系统架构
def fig_architecture():
    fig, ax = plt.subplots(figsize=(11.5, 7.2), dpi=170)
    ax.set_xlim(0, 10); ax.set_ylim(0, 7.4); ax.axis("off")
    ax.set_facecolor(C_BG)

    # 层标题（放在每层方框上方，避免与方框重叠）
    def layer_label(x, y, text):
        ax.text(x, y, text, fontsize=12, color=C_DEEP, weight="bold", ha="left")

    # 数据层
    layer_label(0.55, 7.1, "数据层")
    _box(ax, 0.55, 6.0, 1.9, 0.84, "抓取/清洗\nfetch_500 · schema", fs=10)
    _box(ax, 2.65, 6.0, 1.9, 0.84, "奖级权威\nprize_table", fs=10)
    _box(ax, 4.75, 6.0, 1.9, 0.84, "完整性体检\nhealth_check", fs=10)
    _box(ax, 6.85, 6.0, 2.35, 0.84, "开奖 CSV（6 彩种 20 年）\nlottery_data/", fs=10, fc="#F3F5F8", ec=C_GRAY)

    # 证伪层（核心）
    layer_label(0.55, 5.7, "证伪裁判层（核心）")
    _box(ax, 0.55, 4.6, 2.6, 0.84, "假设登记 + 样本外回测\ncredibility/", fs=10, fc="#FDF3E7", ec="#C87F2F")
    _box(ax, 3.35, 4.6, 2.6, 0.84, "随机基线军团 + FDR\ndiscovery/", fs=10, fc="#FDF3E7", ec="#C87F2F")
    _box(ax, 6.15, 4.6, 3.05, 0.84, "鲁棒性六支柱 + 随机性审计\nrobustness · NIST", fs=10, fc="#FDF3E7", ec="#C87F2F")

    # 优化层
    layer_label(0.55, 4.3, "实得优化层")
    _box(ax, 0.55, 3.2, 2.15, 0.84, "定价口径\nev/payout.py", fs=10)
    _box(ax, 2.9, 3.2, 2.15, 0.84, "流行度模型\nev/popularity.py", fs=10)
    _box(ax, 5.25, 3.2, 1.95, 0.84, "选号器 + 剪枝\nev/selection.py", fs=10)
    _box(ax, 7.4, 3.2, 1.8, 0.84, "A/B 对照\nev/trials.py", fs=10)

    # 展示层
    layer_label(0.55, 2.9, "展示层")
    _box(ax, 0.55, 1.8, 3.9, 0.84, "Web 看板（14 页面 + 诚实看板）\nweb/", fs=10)
    _box(ax, 4.85, 1.8, 4.35, 0.84, "CLI 双入口（credibility/payout/select/trials/…）\ncli.py", fs=10)

    # 底层
    _box(ax, 0.55, 0.45, 8.65, 0.95,
         "支撑：pytest 374 例 · 原子写 · 写生产三道闸 · 自动流水线（调度/事件/自愈） · AGPL-3.0\n"
         "新增：撞号度量/低重叠剪枝（ev/coverage.py） · 中奖归因聚合（ev/attribution.py）",
         fs=10, fc="#F3F5F8", ec=C_GRAY)

    # 层间箭头
    for x in (3.0, 6.9):
        _arrow(ax, x, 6.0, x, 5.46, lw=1.3)
        _arrow(ax, x, 4.6, x, 4.06, lw=1.3)
        _arrow(ax, x, 3.2, x, 2.66, lw=1.3)

    fig.savefig(os.path.join(OUT, "fig_architecture.png"), bbox_inches="tight", facecolor=C_BG)
    plt.close(fig)

# ---------------------------------------------------------------- 图2 证伪流水线
def fig_pipeline():
    fig, ax = plt.subplots(figsize=(11.5, 3.6), dpi=170)
    ax.set_xlim(0, 10); ax.set_ylim(0, 3.6); ax.axis("off")

    steps = [
        ("假设登记", "预先锁定\n防事后偏差", C_LIGHT),
        ("样本外回测", "严格 OOS\n拒绝前视偏差", C_LIGHT),
        ("随机基线军团", "与随机策略\n分布对照", C_LIGHT),
        ("效应量门控", "显著 ≠ 有用\n幅度须过门槛", C_LIGHT),
        ("FDR 校正", "多重比较\n控制假阳性", C_LIGHT),
    ]
    x0, w, gap = 0.25, 1.58, 0.22
    for i, (t, sub, c) in enumerate(steps):
        x = x0 + i * (w + gap)
        _box(ax, x, 1.7, w, 1.1, f"{t}\n{sub}", fs=10, fc=c)
        if i:
            _arrow(ax, x - gap, 2.25, x, 2.25)

    # 裁决
    xr = x0 + 5 * (w + gap) + 0.12
    ax.set_xlim(0, 11.6)
    _box(ax, xr, 2.3, 1.05, 0.6, "放行", fs=11, fc="#E5F3E8", ec=C_GREEN, tc=C_GREEN, bold=True)
    _box(ax, xr, 1.25, 1.05, 0.6, "rejected", fs=10, fc="#FBECEA", ec=C_RED, tc=C_RED, bold=True)
    _arrow(ax, xr - 0.12, 2.6, xr, 2.6, color=C_GREEN)
    _arrow(ax, xr - 0.12, 1.55, xr, 1.55, color=C_RED)

    ax.text(xr + 0.52, 0.7, "实战结果：25 条假设\n全部 rejected，存活 0",
            ha="center", va="center", fontsize=11, color=C_RED, weight="bold")
    _arrow(ax, xr + 0.52, 1.22, xr + 0.52, 0.98, color=C_RED, lw=1.2)

    fig.savefig(os.path.join(OUT, "fig_pipeline.png"), bbox_inches="tight", facecolor=C_BG)
    plt.close(fig)

# ---------------------------------------------------------------- 图3 样本量死结
def fig_sample_size():
    grades = ["六等奖", "五等奖", "四等奖", "三等奖", "二等奖", "一等奖"]
    probs = [1/17, 1/124, 1/2255, 1/109389, 1/1181406, 1/17721088]
    need = [78.4 / p for p in probs]
    labels = ["约 1,300", "约 9,700", "约 17.7 万", "约 858 万", "约 9,270 万", "约 13.9 亿"]

    fig, ax = plt.subplots(figsize=(10.5, 5.4), dpi=170)
    colors = [C_GREEN, "#E0A63C", C_RED, C_RED, C_RED, C_RED]
    bars = ax.bar(grades, need, color=colors, width=0.62, zorder=3)
    ax.set_yscale("log")
    ax.set_ylim(1e2, 1e10)
    ax.set_ylabel("验证 1.5× 提升所需观测注数（对数刻度）", fontsize=11)
    ax.set_title("样本量死结：n > 78.4 / p0（α=.05，power=.8）", fontsize=13, weight="bold", color="#1F2937")
    for b, lab in zip(bars, labels):
        ax.text(b.get_x() + b.get_width() / 2, b.get_height() * 1.6, lab,
                ha="center", fontsize=10.5, weight="bold", color="#1F2937")
    ax.axhline(5200, color=C_GRAY, ls="--", lw=1.2, zorder=2)
    ax.text(0.02, 0.56, "灰虚线：10 注/期 × 100 年 ≈ 5.2 万注\n（个人可持续的购买规模）",
            fontsize=9.5, color="#5F6B7A", transform=ax.transAxes, va="bottom")
    ax.grid(axis="y", alpha=0.25, zorder=0)
    ax.spines[["top", "right"]].set_visible(False)
    fig.text(0.13, 0.015, "结论：三等奖及以上在个人生命周期内无法验证 —— 实得优化证据只能来自历史反事实回测。",
             fontsize=10, color=C_GRAY)
    fig.savefig(os.path.join(OUT, "fig_sample_size.png"), bbox_inches="tight", facecolor=C_BG)
    plt.close(fig)

# ---------------------------------------------------------------- 图4 反事实回测 + 漂移
def fig_backtest():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11.5, 4.6), dpi=170)

    # (a) 分段提升倍数（drift_report，双色球 3502 期）
    segs = ["第1段\n(最早)", "第2段", "第3段", "第4段", "第5段\n(最近)"]
    lift = [1.393, 1.488, 1.504, 1.505, 1.507]
    bars = a1.bar(segs, lift, color=[C_GRAY, "#7FA8D9", C_MAIN, C_MAIN, C_DEEP], width=0.6, zorder=3)
    a1.axhline(1.504, color=C_RED, ls="--", lw=1.4, zorder=2)
    a1.text(4.45, 1.516, "剔除早期段中位 1.504×", fontsize=9.5, color=C_RED, ha="right", va="bottom",
            bbox=dict(facecolor="white", edgecolor="none", pad=1.5))
    a1.axhline(1.0, color="#444444", lw=1.0, zorder=2)
    a1.set_ylim(1.0, 1.62)
    a1.set_ylabel("冷门(10%分位)/热门(90%分位)\n头奖实得倍数", fontsize=10)
    a1.set_title("(a) 分段提升倍数：早期偏低为数据质量\n近期结论更稳（漂移裁决：系数位移·结论稳定）", fontsize=11)
    a1.grid(axis="y", alpha=0.25, zorder=0)
    a1.spines[["top", "right"]].set_visible(False)

    # (b) 封顶建模对比
    xs = ["朴素式\n（不建模封顶）", "显式封顶\nE[min(cap, 池/(1+X))]"]
    ys = [1.545, 1.484]
    bars = a2.bar(xs, ys, color=[C_GRAY, C_MAIN], width=0.5, zorder=3)
    a2.errorbar(1, 1.484, yerr=[[0.101], [0.043]], fmt="none", ecolor=C_DEEP,
                capsize=5, lw=1.5, zorder=4)
    a2.axhline(1.0, color="#444444", lw=1.0, zorder=2)
    a2.set_ylim(1.0, 1.7)
    a2.set_ylabel("头奖实得提升倍数（中位）", fontsize=10)
    a2.set_title("(b) 封顶必须显式建模：朴素式高估\n（n=3292 期，P25–P75：1.383–1.527）", fontsize=11)
    label_ys = [ys[0] + 0.045, ys[1] + 0.043 + 0.035]  # 第二根标签放到误差棒上方
    for i, (v, ly) in enumerate(zip(ys, label_ys)):
        a2.text(i, ly, f"{v:.3f}×", ha="center", fontsize=11.5, weight="bold", color="#1F2937")
    a2.grid(axis="y", alpha=0.25, zorder=0)
    a2.spines[["top", "right"]].set_visible(False)

    fig.tight_layout()
    fig.savefig(os.path.join(OUT, "fig_backtest.png"), bbox_inches="tight", facecolor=C_BG)
    plt.close(fig)

if __name__ == "__main__":
    fig_architecture()
    fig_pipeline()
    fig_sample_size()
    fig_backtest()
    print("done ->", OUT)
