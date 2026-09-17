"""
训练报告生成器（迭代版）

图表：
1. 收敛曲线（双Y轴：损失值 + 命中率）
2. 多窗口对比柱状图（损失 + 命中率）
3. 损失热力图（窗口 × 迭代）
4. 回测盈亏曲线（累计收益 vs 期数）
5. 多模型方案对比（statistical vs logistic vs random_forest）

报告 Markdown：回测详情表、模型对比、最优参数建议
"""

import json
import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from config import TRAINING_DIR
from data.features import FEATURE_VERSION_STANDARD, FEATURE_VERSION_RICH

logger = logging.getLogger(__name__)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.ticker as ticker
    # 配置中文字体：SimHei（黑体）
    _CN_FONT = "SimHei"
    try:
        fm = matplotlib.font_manager
        if not any(f.name == _CN_FONT for f in fm.fontManager.ttflist):
            raise RuntimeError(f"Font {_CN_FONT} not found")
        plt.rcParams["font.sans-serif"] = [_CN_FONT, "Microsoft YaHei", "sans-serif"]
        plt.rcParams["axes.unicode_minus"] = False  # 正确显示负号
    except Exception as e:
        logger.warning(f"中文字体 {_CN_FONT} 配置失败: {e}")
    import numpy as np
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False
    _HAS_NP = False
    logger.warning("matplotlib 未安装，图表生成跳过")
else:
    try:
        import numpy as np
        _HAS_NP = True
    except ImportError:
        _HAS_NP = False


# ============================================================
# 图表生成
# ============================================================

def generate_report_charts(result: dict, output_dir: Path) -> List[str]:
    """
    生成全部训练报告图表
    返回图表文件路径列表
    """
    if not _HAS_MPL:
        return []

    charts = []
    lottery_name = result["lottery_name"]
    charts_dir = output_dir / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)

    # 1. 增强收敛曲线（双Y轴）
    _chart_convergence(result, charts_dir, charts)

    # 2. 多窗口对比柱状图
    _chart_window_comparison(result, charts_dir, charts)

    # 3. 损失热力图
    if len(result.get("multi_window_results", {})) >= 2:
        _chart_loss_heatmap(result, charts_dir, charts)

    # 4. 回测盈亏曲线
    _chart_backtest_profit(result, charts_dir, charts)

    # 5. 多模型方案对比
    _chart_model_comparison(result, charts_dir, charts)

    # 6. 概率校准曲线（取综合分最高的 ML 窗口）
    _chart_calibration(result, charts_dir, charts)

    return charts


def _chart_convergence(result: dict, charts_dir: Path, charts: List[str]):
    """1. 增强收敛曲线：双Y轴 — 左损失 + 右命中率"""
    windows_data = {}
    for w_key, wr in result.get("multi_window_results", {}).items():
        if "error" in wr or not wr.get("loss_history"):
            continue
        windows_data[w_key] = wr["loss_history"]

    if not windows_data:
        return

    fig, ax1 = plt.subplots(figsize=(12, 6))
    ax2 = ax1.twinx()

    colors = ["#4ECDC4", "#FF6B6B", "#45B7D1", "#F9CA24", "#A29BFE"]
    for idx, (w_key, history) in enumerate(sorted(windows_data.items())):
        color = colors[idx % len(colors)]
        iters = [h["iteration"] for h in history]
        losses = [h["loss"] for h in history]
        hit_rates = [h.get("hit_rate", 0) * 100 for h in history]

        line1 = ax1.plot(iters, losses, color=color, linewidth=1.5,
                         label=f"Window {w_key} (loss)", linestyle="-")
        if hit_rates and any(hit_rates):
            line2 = ax2.plot(iters, hit_rates, color=color, linewidth=1.0,
                             label=f"Window {w_key} (hit%)", linestyle="--")

    ax1.set_xlabel("迭代次数")
    ax1.set_ylabel("损失值")
    ax2.set_ylabel("命中率 (%)")
    ax1.set_title(f"{result['lottery_name']} — 收敛曲线（损失值 + 命中率）")
    ax1.grid(True, alpha=0.3)

    # 合并图例
    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right", fontsize=9)

    path = charts_dir / "convergence_curve.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(path))
    logger.info(f"convergence chart: {path}")


def _chart_window_comparison(result: dict, charts_dir: Path, charts: List[str]):
    """2. 多窗口对比：双柱状图（损失 + 命中率）"""
    windows = []
    losses = []
    hit_rates = []
    for w_key, wr in result.get("multi_window_results", {}).items():
        if "error" in wr:
            continue
        windows.append(f"W{w_key}")
        losses.append(wr["best_loss"])
        hit_rates.append(wr.get("best_hit_rate", 0) * 100)

    if not windows:
        return

    fig, ax1 = plt.subplots(figsize=(10, 6))
    ax2 = ax1.twinx()

    x_pos = range(len(windows))
    bar_w = 0.35

    bars1 = ax1.bar([x - bar_w / 2 for x in x_pos], losses, bar_w,
                    label="Best Loss", color="#FF6B6B", alpha=0.85)
    bars2 = ax2.bar([x + bar_w / 2 for x in x_pos], hit_rates, bar_w,
                    label="Hit Rate (%)", color="#4ECDC4", alpha=0.85)

    ax1.set_xlabel("滚动窗口")
    ax1.set_ylabel("损失值")
    ax2.set_ylabel("命中率 (%)")
    ax1.set_title(f"{result['lottery_name']} — 多窗口对比")
    ax1.set_xticks(list(x_pos))
    ax1.set_xticklabels(windows)

    lines1, labels1 = ax1.get_legend_handles_labels()
    lines2, labels2 = ax2.get_legend_handles_labels()
    ax1.legend(lines1 + lines2, labels1 + labels2, loc="upper right")

    # 在柱子上标注数值
    for i, (l, h) in enumerate(zip(losses, hit_rates)):
        ax1.text(i - bar_w / 2, l + 0.005, f"{l:.3f}", ha="center", va="bottom", fontsize=8, color="#FF6B6B")
        ax2.text(i + bar_w / 2, h + 0.5, f"{h:.1f}%", ha="center", va="bottom", fontsize=8, color="#4ECDC4")

    path = charts_dir / "window_comparison.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(path))
    logger.info(f"window comparison chart: {path}")


def _chart_loss_heatmap(result: dict, charts_dir: Path, charts: List[str]):
    """3. 损失热力图：窗口 × 迭代"""
    windows = []
    max_len = 0
    for w_key, wr in result.get("multi_window_results", {}).items():
        if "error" in wr:
            continue
        windows.append(w_key)
        max_len = max(max_len, len(wr.get("loss_history", [])))

    if max_len < 2:
        return

    data = []
    for w_key in windows:
        wr = result["multi_window_results"][w_key]
        losses = [h["loss"] for h in wr.get("loss_history", [])]
        while len(losses) < max_len:
            losses.append(float("nan"))
        data.append(losses)

    arr = np.ma.masked_invalid(np.array(data, dtype=float))

    fig, ax = plt.subplots(figsize=(12, 4))
    im = ax.imshow(arr, aspect="auto", cmap="viridis_r")

    ax.set_yticks(range(len(windows)))
    ax.set_yticklabels([f"W{w}" for w in windows])
    ax.set_xlabel("迭代次数")
    ax.set_title(f"{result['lottery_name']} — 损失热力图（窗口 × 迭代）")
    fig.colorbar(im, ax=ax, label="损失值")

    path = charts_dir / "loss_heatmap.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(path))


def _chart_backtest_profit(result: dict, charts_dir: Path, charts: List[str]):
    """
    4. 回测盈亏曲线
    模拟每期投入 2 元，命中号码的累计收益曲线
    用每个窗口的 best_hit_rate 近似模拟累计收益
    """
    windows_data = {}
    for w_key, wr in result.get("multi_window_results", {}).items():
        if "error" in wr or not wr.get("loss_history"):
            continue
        hit_rates = [h.get("hit_rate", 0) for h in wr["loss_history"]]
        if hit_rates:
            windows_data[w_key] = hit_rates

    if not windows_data:
        return

    fig, ax = plt.subplots(figsize=(12, 6))

    colors = ["#4ECDC4", "#FF6B6B", "#45B7D1", "#F9CA24", "#A29BFE"]
    for idx, (w_key, hit_rates) in enumerate(sorted(windows_data.items())):
        color = colors[idx % len(colors)]

        # 模拟累计盈亏
        # 每期投注 2 元，如果命中则得 5 元（固定奖均价），净利 3 元
        # 累计收益 = Σ(每期净收益)
        ticket_price = 2
        avg_prize = 5
        net_per_win = avg_prize - ticket_price  # 3元

        cumulative = []
        total = 0
        for hr in hit_rates:
            # 每轮迭代的平均收益 = 命中率 × 净利 + (1-命中率) × (-成本)
            iter_profit = hr * net_per_win - (1 - hr) * ticket_price
            total += iter_profit
            cumulative.append(total)

        iterations = list(range(1, len(cumulative) + 1))
        ax.plot(iterations, cumulative, color=color, linewidth=1.5,
                label=f"Window W{w_key}")

    ax.axhline(y=0, color="gray", linestyle="--", linewidth=0.8, alpha=0.5)
    ax.set_xlabel("迭代次数")
    ax.set_ylabel("累计盈亏（元）")
    ax.set_title(f"{result['lottery_name']} — 回测盈亏曲线（累计）")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)

    # 格式化 Y 轴为整数
    ax.yaxis.set_major_formatter(ticker.FormatStrFormatter("%.0f"))

    path = charts_dir / "backtest_profit.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(path))
    logger.info(f"backtest profit chart: {path}")


def _chart_model_comparison(result: dict, charts_dir: Path, charts: List[str]):
    """
    5. 多模型方案对比
    如果 multi_window_results 包含多个模型类型，生成对比图
    目前训练引擎在一次运行中只使用一种模型，
    所以这里对比同一模型在不同窗口的表现
    """
    model_type = result.get("model_type", "unknown")

    windows = []
    losses = []
    hit_rates = []
    for w_key, wr in result.get("multi_window_results", {}).items():
        if "error" in wr:
            continue
        windows.append(str(w_key))
        losses.append(wr["best_loss"])
        hit_rates.append(wr.get("best_hit_rate", 0) * 100)

    if len(windows) < 2:
        return

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12, 5))

    # 左：损失值折线
    x = range(len(windows))
    ax1.plot(x, losses, "o-", color="#FF6B6B", linewidth=2, markersize=8)
    ax1.set_xticks(list(x))
    ax1.set_xticklabels(windows)
    ax1.set_xlabel("窗口")
    ax1.set_ylabel("损失值")
    ax1.set_title(f"模型: {model_type} — 各窗口损失值")
    ax1.grid(True, alpha=0.3)
    for i, v in enumerate(losses):
        ax1.annotate(f"{v:.3f}", (i, v), textcoords="offset points",
                     xytext=(0, 10), ha="center", fontsize=8)

    # 右：命中率折线
    ax2.plot(x, hit_rates, "s-", color="#4ECDC4", linewidth=2, markersize=8)
    ax2.set_xticks(list(x))
    ax2.set_xticklabels(windows)
    ax2.set_xlabel("窗口")
    ax2.set_ylabel("命中率 (%)")
    ax2.set_title(f"模型: {model_type} — 各窗口命中率")
    ax2.grid(True, alpha=0.3)
    for i, v in enumerate(hit_rates):
        ax2.annotate(f"{v:.1f}%", (i, v), textcoords="offset points",
                     xytext=(0, 10), ha="center", fontsize=8)

    plt.tight_layout()
    path = charts_dir / "model_comparison.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(path))
    logger.info(f"model comparison chart: {path}")


def _chart_calibration(result: dict, charts_dir: Path, charts: List[str]):
    """
    6. 概率校准曲线：综合分最高的 ML 窗口，预测概率均值 vs 实际出现频率
    越贴近对角线(灰色虚线)表示模型给出的概率越可信。
    """
    # 找校准数据最好的 ML 窗口（综合分最高）
    best_calib = None
    for w, wr in result.get("multi_window_results", {}).items():
        c = wr.get("calibration")
        if c and c.get("calibration_curve"):
            if best_calib is None or c.get("combined_score", 0) > best_calib.get("combined_score", 0):
                best_calib = c
    if not best_calib:
        return

    curve = best_calib["calibration_curve"]
    preds = [p["mean_pred"] for p in curve]
    actuals = [p["actual_freq"] for p in curve]
    counts = [p["count"] for p in curve]

    fig, ax = plt.subplots(figsize=(7, 7))
    sizes = [max(30, c * 3) for c in counts]
    ax.scatter(preds, actuals, s=sizes, color="#4ECDC4", alpha=0.85,
               edgecolors="#2C9C94", label="概率桶（点大小=样本数）")
    ax.plot([0, 1], [0, 1], color="gray", linestyle="--", linewidth=1.2, label="完美校准")
    ax.set_xlabel("预测概率均值")
    ax.set_ylabel("实际出现频率")
    ax.set_title(f"{result['lottery_name']} — 概率校准曲线")
    ax.legend(fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_xlim(0, 1)
    ax.set_ylim(0, 1)

    path = charts_dir / "calibration_curve.png"
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(path))
    logger.info(f"calibration chart: {path}")


# ============================================================
# 报告 Markdown
# ============================================================

def generate_report_md(result: dict) -> str:
    """
    增强版训练报告 Markdown

    章节：
    1. 训练概要
    2. 多窗口对比表（含损失、命中率、早停位置、对比最佳）
    3. 模型方案详情
    4. 最优参数建议
    5. 回测详情表
    6. 图表索引
    """
    model_map = {
        "statistical": "统计学模型（基于频率）",
        "logistic": "逻辑回归",
        "random_forest": "随机森林",
    }
    model_desc = model_map.get(result.get("model_type", ""), result.get("model_type", "unknown"))

    lines = [
        f"# {result['lottery_name']} — 训练报告",
        f"",
        f"## 概要",
        f"",
        f"- **训练耗时**: {result.get('training_time', 'N/A')}",
        f"- **模型类型**: {model_desc}",
        f"- **总记录数**: {result.get('total_records', 'N/A')}",
        f"- **最优窗口**: {result.get('best_window', 'N/A')} 期",
        f"- **最优损失**: {result.get('best_loss', 'N/A'):.4f}",
        f"- **数据版本(本地最新期号)**: {result.get('data_version', {}).get('max_issue', 'N/A')}",
        f"",
    ]

    # 选号命中率 vs 随机基线（关键指标，放在最前面）
    lines.extend([
        f"## 选号命中率 vs 随机基线（关键指标）",
        f"",
        f"> 注意：下表『选号平均命中』是按规则买一组号码时的真实命中数；",
        f"> 『随机基线期望』是闭着眼睛随机选一组的期望命中数。",
        f"> 只有『优势』显著为正，模型才可能具备真实预测力。",
        f"",
        f"| 窗口 | 选号平均命中 | 随机基线期望 | 优势 (Δ) | 评估期数 | p 值 | 显著性 |",
        f"|------|-------------|-------------|---------|---------|------|--------|",
    ])
    for w, wr in result.get("multi_window_results", {}).items():
        if "error" in wr:
            lines.append(f"| {w} | ERROR | - | - | - | - | {wr.get('error', '')} |")
        else:
            sev = wr.get("selection_eval")
            if sev:
                obs = sev["observed_total_hits"]
                exp = sev["expected_total_hits"]
                adv = sev["total_advantage"]
                n = sev["n_periods"]
                p = sev["p_value"]
                sig = "✅ 显著优于随机" if sev.get("significant_05") and adv > 0 else (
                    "❌ 显著差于随机" if sev.get("significant_05") and adv < 0 else "➖ 与随机无显著差异"
                )
                lines.append(
                    f"| {w} | {obs:.3f} | {exp:.3f} | {adv:+.3f} | {n} | {p:.4f} | {sig} |"
                )
            else:
                lines.append(f"| {w} | - | - | - | - | - | 未计算 |")
    lines.append(f"")

    # 校准摘要（顶层，取最优窗口）
    calib = result.get("calibration")
    if calib:
        lines.extend([
            f"## 概率校准（统一评分尺）",
            f"",
            f"- **命中率**: {calib['hit_rate']*100:.2f}%",
            f"- **校准误差**: {calib['calibration_error']:.4f}（越低=概率越可信）",
            f"- **综合分**: {calib['combined_score']:.4f}（命中率×0.7 + 校准度×0.3，越高越好）",
            f"",
        ])

    # 多窗口对比表
    lines.extend([
        f"## 多窗口对比",
        f"",
        f"| 窗口 | 最优损失 | 命中率 | 早停位置 | 对比最优 |",
        f"|------|----------|--------|----------|----------|",
    ])
    best_loss = float("inf")
    for w, wr in result.get("multi_window_results", {}).items():
        if "error" not in wr:
            best_loss = min(best_loss, wr["best_loss"])

    for w, wr in result.get("multi_window_results", {}).items():
        if "error" in wr:
            lines.append(f"| {w} | ERROR | - | - | {wr.get('error', '')} |")
        else:
            hit = wr.get("best_hit_rate", 0) * 100
            stop = wr.get("early_stopped_at", "N/A")
            diff = f"{wr['best_loss'] - best_loss:+.4f}" if wr['best_loss'] > best_loss else "**最优**"
            lines.append(f"| {w} | {wr['best_loss']:.4f} | {hit:.2f}% | {stop} | {diff} |")

    # 模型详情
    lines.extend([
        f"",
        f"## 模型详情",
        f"",
        f"- **模型类型**: {model_desc}",
        f"- **训练配置**: {json.dumps(result.get('config', {}), ensure_ascii=False)}",
        f"- **损失函数**: Loss = α × 区间偏差 + β × 遗漏偏差",
        f"- **α (区间权重)**: {result.get('config', {}).get('alpha', 'N/A')}",
        f"- **β (遗漏权重)**: {result.get('config', {}).get('beta', 'N/A')}",
        f"",
    ])

    # 回测摘要
    lines.extend([
        f"## 回测摘要",
        f"",
        f"| 窗口 | 总迭代数 | 最终损失 | 最终命中率 | 改进幅度 |",
        f"|------|----------|----------|------------|----------|",
    ])
    for w, wr in sorted(result.get("multi_window_results", {}).items()):
        if "error" in wr:
            continue
        lh = wr.get("loss_history", [])
        if len(lh) >= 2:
            initial_loss = lh[0]["loss"]
            final_loss = lh[-1]["loss"]
            improvement = f"{(initial_loss - final_loss) / initial_loss * 100:+.2f}%"
        else:
            initial_loss = wr["best_loss"]
            final_loss = wr["best_loss"]
            improvement = "0.00%"
        total_iter = len(lh) if lh else 0
        final_hit = lh[-1].get("hit_rate", 0) * 100 if lh else 0
        lines.append(f"| {w} | {total_iter} | {final_loss:.4f} | {final_hit:.2f}% | {improvement} |")

    # 最优建议
    best_w = result.get("best_window", "N/A")
    lines.extend([
        f"",
        f"## 建议",
        f"",
        f"- **最优滚动窗口**: {best_w} 期（损失={result.get('best_loss', 'N/A')}）",
        f"- **推荐模型**: {model_desc}",
        f"- **注意**: 以上结果基于历史规律分析。彩票开奖是独立随机事件，历史规律不保证未来结果。",
        f"",
    ])

    # 各 ML 窗口校准对比表
    calib_rows = [
        (w, wr["calibration"])
        for w, wr in result.get("multi_window_results", {}).items()
        if isinstance(wr, dict) and wr.get("calibration")
    ]
    if calib_rows:
        lines.extend([
            f"## 各窗口概率校准对比",
            f"",
            f"| 窗口 | 命中率 | 校准误差 | 综合分 |",
            f"|------|--------|----------|--------|",
        ])
        for w, c in calib_rows:
            lines.append(
                f"| {w} | {c['hit_rate']*100:.2f}% | {c['calibration_error']:.4f} | {c['combined_score']:.4f} |"
            )
        lines.append(f"")
        lines.append(f"![概率校准曲线](charts/calibration_curve.png)")
        lines.append(f"")

    # 参数自动搜索结果（按当前彩种+特征版本读取对应文件）
    try:
        from train.search import _best_config_path
        sc_path = _best_config_path(
            result.get("lottery_name", ""),
            result.get("feature_version", FEATURE_VERSION_STANDARD),
        )
        if sc_path.exists():
            with open(sc_path, encoding="utf-8") as f:
                sc = json.load(f)
            if sc.get("lottery_name") == result.get("lottery_name"):
                best = sc.get("best")
                lines.extend([
                    f"## 参数自动搜索（统一评分尺）",
                    f"",
                ])
                if best:
                    lines.append(
                        f"- **最优配置**: 窗口={best['window']} 模型={best['model_type']} "
                        f"特征版本={best['feature_version']} 综合分={best['combined_score']:.4f}"
                    )
                lines.append(f"- 候选结果（按综合分排序）:")
                for r in sorted(sc.get("results", []), key=lambda x: x.get("combined_score", -1), reverse=True):
                    if "combined_score" in r:
                        lines.append(
                            f"  - 窗口={r['window']} 模型={r['model_type']} "
                            f"命中率={r['hit_rate']:.4f} 校准误差={r['calibration_error']:.4f} "
                            f"综合分={r['combined_score']:.4f}"
                        )
                    else:
                        lines.append(f"  - 窗口={r.get('window')} 模型={r.get('model_type')} 跳过({r.get('error')})")
                lines.append(f"")
    except Exception as e:
        logger.warning(f"读取搜索结果失败: {e}")

    lines.extend([
        f"## 图表",
        f"",
        f"![收敛曲线](charts/convergence_curve.png)",
        f"",
        f"![多窗口对比](charts/window_comparison.png)",
        f"",
        f"![回测盈亏](charts/backtest_profit.png)",
        f"",
    ])

    # 附加热力图和模型对比
    lines.append(f"![损失热力图](charts/loss_heatmap.png)")
    lines.append(f"")
    lines.append(f"![模型对比](charts/model_comparison.png)")
    lines.append(f"")

    return "\n".join(lines)


# ============================================================
# 一站式保存：报告 + 图表
# ============================================================

def save_report(result: dict, output_dir: Path) -> Path:
    """保存报告和图表，返回报告文件路径"""
    # 生成图表
    generate_report_charts(result, output_dir)

    # 生成报告
    result["_output_dir"] = str(output_dir)
    report = generate_report_md(result)

    report_path = output_dir / "report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    logger.info(f"report saved: {report_path}")
    return report_path
