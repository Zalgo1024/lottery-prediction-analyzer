"""
预测报告生成（迭代版）

差异化设计：
- 报告侧重：策略来源、收益期望、区间分析、历史参考
- 图表：号码分布直方图、组间重叠热力图
- 对比：当前预测 vs 历史平均命中率
"""

import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from config import LOTTERY_CONFIG, TRAINING_DIR, PREDICT_DEFAULTS

logger = logging.getLogger(__name__)

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
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
    _HAS_MPL = True
except ImportError:
    _HAS_MPL = False
    logger.warning("matplotlib unavailable, skipping charts")

# 组间重叠热力图的最大组数（超过则跳过）
# ⚠️ 该图是 O(n²) 全配对矩阵：实测 300 组耗时 62 秒、1000 组 >10 分钟，
#    且 n 很大时热力图本身毫无可读性（1000×1000 个格子挤在 8×6 英寸画布里）。
#    人海战术（--groups 几百~2000）必须跳过，否则报告生成成为唯一瓶颈。
_HEATMAP_MAX_GROUPS = 120


# ============================================================
# 图表生成
# ============================================================

def _generate_charts(result: dict, output_dir: Path) -> List[str]:
    """
    生成预测报告图表（区驱动，兼容红/蓝与新彩种）
    1. 各分区号码分布直方图
    2. 组间重叠热力图（按分区同号数）
    """
    if not _HAS_MPL:
        return []

    from data.schema import get_schema

    charts = []
    lottery = result["lottery_name"]
    numbers = result["预测号码"]
    schema = get_schema(lottery)
    zones = schema.zones

    # 1. 各分区号码分布直方图
    counters = {z.name: Counter() for z in zones}
    for ps in numbers:
        for z in zones:
            for v in ps.get("号码", {}).get(z.name, []):
                counters[z.name][v] += 1

    n = len(zones)
    cols = min(n, 3)
    rows = (n + cols - 1) // cols
    fig, axes = plt.subplots(rows, cols, figsize=(5 * cols, 4 * rows))
    axes = np.array(axes).reshape(-1)
    for idx, z in enumerate(zones):
        ax = axes[idx]
        lo, hi = z.min, z.max
        xs = list(range(lo, hi + 1))
        ys = [counters[z.name].get(v, 0) for v in xs]
        ax.bar(xs, ys, color="#4ECDC4", alpha=0.8, edgecolor="white", linewidth=0.5)
        ax.axhline(y=len(numbers) * z.choose / (hi - lo + 1),
                   color="red", linestyle="--", alpha=0.6, label="均匀期望")
        ax.set_xlabel(f"{z.name}号码")
        ax.set_ylabel("各组出现频次")
        ax.set_title(f"{lottery} — {z.name}分布（{len(numbers)} 组）")
        ax.legend(fontsize=8)
    for j in range(idx + 1, len(axes)):
        axes[j].set_visible(False)
    plt.tight_layout()
    path = output_dir / "charts" / "number_distribution.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(path))
    logger.info(f"distribution chart: {path}")

    # 2. 组间重叠热力图
    if len(numbers) >= 2:
        _chart_overlap_heatmap(numbers, schema, output_dir, charts)

    return charts


def _chart_overlap_heatmap(numbers: list, schema, output_dir: Path, charts: List[str]):
    """组间重叠热力图：重叠数 = 各分区同号数之和（红/蓝彩种即红球重叠数）

    O(n²) 全配对矩阵，n > _HEATMAP_MAX_GROUPS 时直接跳过（见该常量注释）。
    """
    n = len(numbers)
    if n > _HEATMAP_MAX_GROUPS:
        logger.info(f"跳过组间重叠热力图：{n} 组 > {_HEATMAP_MAX_GROUPS}（O(n²) 且不可读）")
        return

    zones = schema.zones
    total = len(zones)
    overlap_matrix = np.zeros((n, n))
    for i in range(n):
        for j in range(n):
            if i == j:
                overlap_matrix[i][j] = total
            else:
                cnt = 0
                for z in zones:
                    a = set(numbers[i].get("号码", {}).get(z.name, []))
                    b = set(numbers[j].get("号码", {}).get(z.name, []))
                    cnt += len(a & b)
                overlap_matrix[i][j] = cnt

    fig, ax = plt.subplots(figsize=(8, 6))
    im = ax.imshow(overlap_matrix, cmap="YlOrRd", vmin=0, vmax=max(total, 1))

    # 标注数值
    for i in range(n):
        for j in range(n):
            ax.text(j, i, int(overlap_matrix[i][j]),
                    ha="center", va="center", fontsize=9, fontweight="bold")

    ax.set_xticks(range(n))
    ax.set_yticks(range(n))
    ax.set_xticklabels([f"#{i+1}" for i in range(n)])
    ax.set_yticklabels([f"#{i+1}" for i in range(n)])
    ax.set_xlabel("组")
    ax.set_ylabel("组")
    ax.set_title(f"组间同号数热力图（最大={total}）")
    fig.colorbar(im, ax=ax, label="同号数")

    path = output_dir / "charts" / "overlap_heatmap.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    charts.append(str(path))
    logger.info(f"overlap heatmap: {path}")


# ============================================================
# 收益期望计算
# ============================================================

def _compute_group_expected_value(reds: List[int], blues: List[int], lottery_name: str, ticket_price: int = 2) -> dict:
    """
    计算每组号码的近似期望收益
    基于理论概率：中奖概率 × 各奖级奖金
    """
    from pipeline.step1_probability import prize_probabilities_ssq, prize_probabilities_dlt

    if lottery_name == "双色球":
        prize_data = prize_probabilities_ssq()
    else:
        prize_data = prize_probabilities_dlt()

    # 简化：用整体期望值，实际期望 = 总组合/每注组合
    ev_per_ticket = -ticket_price
    expected_prize = 0
    for level in prize_data["levels"]:
        if level.get("is_floating"):
            amount = 5_000_000 if lottery_name == "双色球" else 5_000_000
        else:
            amount = level.get("fixed_amount", 0)
        ev = amount * level["probability"]
        expected_prize += ev

    ev = expected_prize - ticket_price

    # 等级评估
    if ev > 0:
        grade = "正值（非零和）"
    elif ev > -1:
        grade = "轻度负值（期望损失 < 50%）"
    else:
        grade = "严重负值（期望损失 > 50%）"

    return {
        "ticket_price": ticket_price,
        "expected_return": round(ev, 4),
        "expected_prize": round(expected_prize, 4),
        "return_rate": f"{expected_prize / ticket_price * 100:.1f}%",
        "grade": grade,
    }


# ============================================================
# 主报告生成
# ============================================================

def generate_prediction_report(result: dict, output_dir: Path) -> str:
    """
    差异化预测报告（区驱动，兼容红/蓝与新彩种）

    1. 预测概要
    2. 号码详情表（含策略、置信度、各分区号码、冷热/遗漏/分布得分）
    3. 收益期望分析（仅双色球/大乐透有奖级表）
    4. 区间分析 + 冷热号参考
    5. 组间对比
    6. 风险提示
    """
    from data.schema import get_schema

    numbers = result["预测号码"]
    lottery = result["lottery_name"]
    schema = get_schema(lottery)
    zones = schema.zones
    zone_header = " | ".join(z.name for z in zones)

    lines = [
        f"# {lottery} — 预测报告",
        f"",
        f"## 概要",
        f"",
        f"- **预测日期**: {result.get('预测日期', 'N/A')}",
        f"- **预测模式**: {result.get('预测模式', 'N/A')}",
        f"- **号码组数**: {result.get('号码组数', 0)}",
        f"- **近50期热号命中率**: {result.get('历史命中率(近50期)', 'N/A')}",
        f"- **生成时间**: {result.get('生成时间', 'N/A')}",
        f"",
    ]

    # ============================================================
    # 号码详情表（列 = 各分区）
    # ============================================================
    lines.extend([
        f"## 号码详情",
        f"",
        f"| # | 策略 | 置信度 | {zone_header} | 热号得分 | 遗漏得分 | 分布得分 |",
        f"|---|------|--------|" + "|".join(["------"] * len(zones)) + "|----------|----------|----------|",
    ])

    for i, ps in enumerate(numbers, 1):
        zone_strs = []
        for z in zones:
            nums = ps.get("号码", {}).get(z.name, [])
            zone_strs.append(" ".join(f"{n:02d}" for n in nums))
        zone_cell = " | ".join(zone_strs)
        conf = f"{ps['置信度']:.4f}"
        strat = ps.get("策略", "-")
        det = ps.get("评分明细", {})
        hot = f"{det.get('冷热号得分', 0):.3f}" if det.get('冷热号得分') is not None else "-"
        miss = f"{det.get('遗漏偏差得分', 0):.3f}" if det.get('遗漏偏差得分') is not None else "-"
        dist = f"{det.get('分布拟合度得分', 0):.3f}" if det.get('分布拟合度得分') is not None else "-"
        lines.append(f"| {i} | {strat} | {conf} | {zone_cell} | {hot} | {miss} | {dist} |")

    # ============================================================
    # 收益期望分析（仅双色球/大乐透有奖级表）
    # ============================================================
    if lottery in ("双色球", "大乐透") and numbers:
        ev_info = _compute_group_expected_value(
            numbers[0].get("号码", {}).get("红球", []),
            numbers[0].get("号码", {}).get("蓝球", []),
            lottery,
        )
        lines.extend([
            f"",
            f"## 期望收益分析",
            f"",
            f"- **单注成本**: {ev_info['ticket_price']} 元",
            f"- **单注期望奖金**: {ev_info['expected_prize']} 元",
            f"- **单注期望收益**: {ev_info['expected_return']} 元",
            f"- **返奖率**: {ev_info['return_rate']}",
            f"- **评级**: {ev_info['grade']}",
            f"- **说明**: 彩票是负期望值游戏，不同号码组合的期望收益相同。",
            f"",
        ])

    # ============================================================
    # 区间分析（每区 3 段）
    # ============================================================
    lines.extend([
        f"## 区间分析",
        f"",
        f"| 组 | 置信度 | 各区间号码数 |",
        f"|----|--------|------------|",
    ])
    for i, ps in enumerate(numbers, 1):
        parts = []
        for z in zones:
            lo, hi = z.min, z.max
            third = (hi - lo + 1) // 3
            z1, z2 = lo + third, lo + 2 * third
            nums = ps.get("号码", {}).get(z.name, [])
            c1 = sum(1 for n in nums if n <= z1)
            c2 = sum(1 for n in nums if z1 < n <= z2)
            c3 = sum(1 for n in nums if n > z2)
            parts.append(f"{z.name}[{c1}/{c2}/{c3}]")
        lines.append(f"| #{i} | {ps['置信度']:.3f} | {' '.join(parts)} |")

    lines.append(f"")
    lines.append(f"*理想状态：号码均匀分布于各区间以实现最大覆盖。*")
    lines.append(f"")

    # ============================================================
    # 冷热号参考（按区）
    # ============================================================
    lines.extend([
        f"## 参考：近期冷热号",
        f"",
    ])
    from data.loader import load_lottery
    from pipeline.statistics import frequency_analysis, missing_value_analysis
    data = load_lottery(lottery)
    freqs = frequency_analysis(data)
    missing = missing_value_analysis(data)

    for z in zones:
        sf = sorted(freqs[z.name]["freq"].items(), key=lambda x: x[1], reverse=True)
        sm = sorted(missing[z.name].items(), key=lambda x: x[1], reverse=True)
        lines.append(f"### {z.name} 历史热度 Top 5")
        for n, f in sf[:5]:
            lines.append(f"- **{n:02d}**: {f} 次")
        lines.append(f"")
        lines.append(f"### {z.name} 当前遗漏 Top 5")
        for n, m in sm[:5]:
            lines.append(f"- **{n:02d}**: {m} 期未出现")
        lines.append(f"")

    # ============================================================
    # 组间对比（按分区同号数）
    # ============================================================
    if len(numbers) >= 2:
        lines.extend([
            f"## 组间重叠分析",
            f"",
        ])
        total_overlap = 0
        pair_count = 0
        for i in range(len(numbers)):
            for j in range(i + 1, len(numbers)):
                cnt = 0
                for z in zones:
                    a = set(numbers[i].get("号码", {}).get(z.name, []))
                    b = set(numbers[j].get("号码", {}).get(z.name, []))
                    cnt += len(a & b)
                total_overlap += cnt
                pair_count += 1
        avg_overlap = total_overlap / pair_count if pair_count > 0 else 0
        total_possible = sum(z.choose for z in zones)
        lines.append(f"- **平均每组同号数**: {avg_overlap:.2f}")
        lines.append(f"- **单组最大同号数**: {total_possible}")
        diversity_pct = (1 - avg_overlap / total_possible) * 100 if total_possible else 0
        lines.append(f"- **多样性得分**: {diversity_pct:.1f}%（越高越分散）")

        lines.append(f"")

    # ============================================================
    # 图表
    # ============================================================
    lines.extend([
        f"## 图表",
        f"",
        f"![号码分布图](charts/number_distribution.png)",
        f"",
    ])
    if len(numbers) >= 2:
        lines.extend([
            f"![组间重叠热力图](charts/overlap_heatmap.png)",
            f"",
        ])

    # ============================================================
    # 评分权重 & 风险提示
    # ============================================================
    w = PREDICT_DEFAULTS
    lines.extend([
        f"## 评分权重",
        f"",
        f"- **置信度** = 热号得分 × w₁ + 遗漏得分 × w₂ + 分布得分 × w₃ + 历史命中 × w₄",
        f"- w₁={w['confidence_w1']}, w₂={w['confidence_w2']}, w₃={w['confidence_w3']}, w₄=0.15",
        f"",
        f"## 风险提示",
        f"",
        f"- 彩票开奖为独立随机事件。",
        f"- 历史规律不保证未来结果。",
        f"- 本分析仅供参考，请理性购彩。",
    ])

    report = "\n".join(lines)

    # 写入文件
    report_path = output_dir / "report.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    logger.info(f"prediction report saved: {report_path}")
    return report


def save_prediction_record(result: dict) -> Path:
    """
    保存预测记录到 training/ 目录
    包含：prediction.json + report.md + charts/
    """
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = result["lottery_name"]
    code = LOTTERY_CONFIG[name]["name_en"]
    record_dir = TRAINING_DIR / f"{timestamp}_{code}_predict"
    record_dir.mkdir(parents=True, exist_ok=True)

    # 保存 JSON（精简版，区驱动：号码存于 号码 字段，红/蓝彩种额外保留兼容字段）
    pred_simple = {
        "lottery": result["lottery_name"],
        "date": result.get("预测日期", ""),
        "mode": result.get("预测模式", ""),
        "历史命中率": result.get("历史命中率(近50期)", 0),
        "numbers": [
            {
                "号码": ps.get("号码", {}),
                "置信度": ps["置信度"],
                "策略": ps.get("策略", ""),
                "备注": ps.get("备注", ""),
                **({"红球": ps["红球"]} if "红球" in ps else {}),
                **({"蓝球": ps["蓝球"]} if "蓝球" in ps else {}),
            }
            for ps in result["预测号码"]
        ],
    }
    with open(record_dir / "prediction.json", "w", encoding="utf-8") as f:
        json.dump(pred_simple, f, ensure_ascii=False, indent=2)

    # 生成图表
    _generate_charts(result, record_dir)

    # 生成报告
    generate_prediction_report(result, record_dir)

    logger.info(f"prediction record saved: {record_dir}")
    return record_dir
