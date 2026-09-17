"""
滚动预测训练模块（反馈闭环 - 阶段1）

核心思路：
- 用前 N 期数据预测第 N+1 期，对比真实开奖记录命中
- 滚动推进，统计各策略在历史预测中的真实表现
- 输出策略表现报告，为后续动态加权提供数据基础

与旧训练逻辑的区别：
- 旧：拟合历史模式，损失函数无实际意义
- 新：预测下一期号码，用真实开奖评估，目标明确
"""

import json
import logging
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import LOTTERY_CONFIG, TRAINING_DIR
from data.loader import load_lottery
from data.schema import LotteryData, DrawRecord, record_zone_numbers, is_redblue, schema_from_cfg
from pipeline.statistics import frequency_analysis, missing_value_analysis

logger = logging.getLogger(__name__)


# ============================================================
# 策略实现（独立版本，不依赖 prediction/engine.py 的去重逻辑）
# ============================================================

def _strategy_high_freq(window: List[DrawRecord], cfg: dict, schema) -> dict:
    """高频策略：从窗口内各分区频率最高的号码中抽取（红/蓝与数字型通用）"""
    zones_out = {}
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        freq = defaultdict(int)
        for rec in window:
            for n in record_zone_numbers(rec, zone):
                freq[n] += 1
        top = sorted(freq.items(), key=lambda x: x[1], reverse=True)[: zone.choose]
        zones_out[zone.name] = sorted(n for n, _ in top)
    return {"zones": zones_out, "策略": "高频策略"}


def _strategy_missing_value(window: List[DrawRecord], cfg: dict, schema) -> dict:
    """遗漏值策略：从各分区当前遗漏值最高的号码中抽取"""
    zones_out = {}
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        miss = {n: 0 for n in range(lo, hi + 1)}
        for rec in window:
            nums = set(record_zone_numbers(rec, zone))
            for n in range(lo, hi + 1):
                if n in nums:
                    break
                miss[n] += 1
        top = sorted(miss.items(), key=lambda x: x[1], reverse=True)[: zone.choose]
        zones_out[zone.name] = sorted(n for n, _ in top)
    return {"zones": zones_out, "策略": "遗漏值策略"}


def _strategy_balanced(window: List[DrawRecord], cfg: dict, schema) -> dict:
    """区间均衡策略：各分区分三等分区间，按区间均衡 + 频率加权选择"""
    zones_out = {}
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        size = hi - lo + 1
        freq = defaultdict(int)
        for rec in window:
            for n in record_zone_numbers(rec, zone):
                freq[n] += 1
        thirds = [
            (lo, lo + size // 3),
            (lo + size // 3 + 1, lo + 2 * size // 3),
            (lo + 2 * size // 3 + 1, hi),
        ]
        per = [zone.choose // 3] * 3
        rem = zone.choose - sum(per)
        for i in range(rem):
            per[i] += 1
        chosen = []
        for i, (a, b) in enumerate(thirds):
            pool = [(n, freq.get(n, 0)) for n in range(a, b + 1)]
            pool.sort(key=lambda x: x[1], reverse=True)
            chosen.extend(n for n, _ in pool[: per[i]])
        zones_out[zone.name] = sorted(chosen)
    return {"zones": zones_out, "策略": "区间均衡策略"}


def _strategy_rule_based(window: List[DrawRecord], cfg: dict, schema, is_rb: bool) -> dict:
    """
    规则优选策略（策略包3）：仅红/蓝双区支持严格规则筛选；
    数字型（无红/蓝分区）规则不适用，降级为高频策略。
    """
    if not is_rb:
        # 数字型无红/蓝分区，规则优选不适用，降级为高频策略，但保留策略名以便统计
        res = _strategy_high_freq(window, cfg, schema)
        res["策略"] = "规则优选"
        return res

    from prediction.rule_strategy import _hot_cold_zones, _sample_reds, _sample_blues

    if len(window) < 20:
        logger.warning(f"规则优选回测: 窗口仅 {len(window)} 期，冷热分档参考期数不足20")

    zones = _hot_cold_zones(window, cfg)

    for _ in range(500):
        reds = _sample_reds(zones, cfg)
        if reds is None:
            continue
        blues = _sample_blues(zones, cfg, reds, used_blues=set())
        if blues is None:
            continue
        return {"红球": sorted(reds), "蓝球": sorted(blues), "策略": "规则优选"}

    # 兜底：退化为高频策略
    logger.warning("规则优选回测采样失败，降级为高频策略")
    return _strategy_high_freq(window, cfg, schema)


# ============================================================
# 命中评估
# ============================================================

def _evaluate_hit(pred: dict, actual: DrawRecord, schema, is_rb: bool,
                  lottery_name: str = "") -> dict:
    """评估单次预测的命中情况（红/蓝与数字型通用）"""
    if is_rb:
        # ⚠️ 通用策略（高频/遗漏/区间均衡）返回 {"zones": {区名: 号码}}，
        # 只有「规则优选」返回 {"红球":…, "蓝球":…}。旧代码只读后者，
        # 导致前三个策略的红/蓝命中恒为 0（回测报告系统性失真）。
        zp = pred.get("zones") or {}
        pred_red_set = set(pred.get("红球") or zp.get(schema.red_zone.name, []))
        pred_blue_set = set(pred.get("蓝球") or zp.get(schema.blue_zone.name, []))
        actual_red_set = set(actual.红球)
        actual_blue_set = set(actual.蓝球)
        red_hits = len(pred_red_set & actual_red_set)
        blue_hits = len(pred_blue_set & actual_blue_set)
        # 判级复用 data.feedback 官方规则（双色球六等级 / 大乐透2023九级），
        # 此前本文件的复制版按双色球规则"近似"大乐透，产生系统性误判。
        level = (_classify_dlt_prize(red_hits, blue_hits)
                 if lottery_name == "大乐透" else _classify_prize(red_hits, blue_hits))
        return {
            "策略": pred["策略"],
            "红球命中": red_hits,
            "蓝球命中": blue_hits,
            "总命中": red_hits + blue_hits,
            "是否中奖": level != "未中",
            "中奖等级": level,
        }
    # 数字型：逐区命中
    zones_pred = pred.get("zones", {})
    zone_hits = {}
    total = 0
    for zone in schema.zones:
        pred_set = set(zones_pred.get(zone.name, []))
        act_set = set(record_zone_numbers(actual, zone))
        h = len(pred_set & act_set)
        zone_hits[zone.name] = h
        total += h
    total_choose = schema.total_choose
    exact = (total == total_choose)
    return {
        "策略": pred["策略"],
        "红球命中": total,       # 兼容前端列：数字型取总位数命中
        "蓝球命中": 0,
        "总命中": total,
        "总位数": total_choose,
        "分区命中": zone_hits,
        "是否中奖": exact,
        "中奖等级": "精确全中" if exact else "未中",
    }


def _is_prize_won(red_hits: int, blue_hits: int) -> bool:
    """[已废弃，保留占位防外部引用] 简化中奖判断——请改用官方判级结果 != "未中"。"""
    return red_hits >= 3 or blue_hits >= 1


# 判级统一复用 data.feedback 官方规则（删除了本文件此前的双色球复制版）
from data.feedback import _classify_prize, _classify_dlt_prize  # noqa: E402


# ============================================================
# 滚动预测训练主入口
# ============================================================

def rolling_train(
    lottery_name: str,
    window_size: int = 50,
    eval_periods: int = 100,
    strategies: Optional[List[str]] = None,
) -> dict:
    """
    滚动预测训练：用前 window_size 期预测第 window_size+1 期，连续评估 eval_periods 期

    参数：
    - lottery_name: 彩票类型
    - window_size: 训练窗口大小（用前 N 期预测下一期）
    - eval_periods: 评估期数（最近多少期的预测结果）
    - strategies: 参与评估的策略列表，默认全部三种

    返回：
    - 各策略的命中表现统计
    - 逐期预测明细
    - 最优窗口建议
    """
    if strategies is None:
        strategies = ["高频策略", "遗漏值策略", "区间均衡策略", "规则优选"]

    logger.info(f"滚动预测训练: {lottery_name}, window={window_size}, eval_periods={eval_periods}")

    data = load_lottery(lottery_name)
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg, lottery_name)
    is_rb = is_redblue(lottery_name)
    records = data.records  # 从新到旧

    # 反转为从旧到新（便于时序滚动）
    records_chronological = list(reversed(records))

    if len(records_chronological) < window_size + 1:
        return {"error": f"数据不足，需要至少 {window_size + 1} 期"}

    # 策略函数映射（红/蓝与数字型通用，按 schema.zones 生成预测）
    strategy_funcs = {
        "高频策略": lambda w: _strategy_high_freq(w, cfg, schema),
        "遗漏值策略": lambda w: _strategy_missing_value(w, cfg, schema),
        "区间均衡策略": lambda w: _strategy_balanced(w, cfg, schema),
        "规则优选": lambda w: _strategy_rule_based(w, cfg, schema, is_rb),
    }

    # 滚动预测
    all_predictions = []
    n_eval = min(eval_periods, len(records_chronological) - window_size)

    # 从倒数第 n_eval 期开始，向前滚动到最新一期
    start_idx = len(records_chronological) - n_eval

    for t in range(start_idx, len(records_chronological)):
        # 训练窗口：前 window_size 期
        train_window = records_chronological[t - window_size : t]
        # 真实目标：第 t 期
        actual = records_chronological[t]

        # 用各策略生成预测
        for strat_name in strategies:
            strat_fn = strategy_funcs[strat_name]
            try:
                pred = strat_fn(train_window)
                hit = _evaluate_hit(pred, actual, schema, is_rb, lottery_name)
                hit["期号"] = actual.期号
                hit["开奖日期"] = str(actual.开奖日期) if actual.开奖日期 else ""
                # 通用分区表示（数字型与红/蓝双区统一）
                zones_pred = pred.get("zones", {"红球": pred.get("红球", []), "蓝球": pred.get("蓝球", [])})
                hit["预测分区"] = zones_pred
                hit["实际分区"] = {z.name: record_zone_numbers(actual, z) for z in schema.zones}
                if is_rb:
                    hit["预测红球"] = pred.get("红球", zones_pred.get("红球", []))
                    hit["预测蓝球"] = pred.get("蓝球", zones_pred.get("蓝球", []))
                    hit["实际红球"] = actual.红球
                    hit["实际蓝球"] = actual.蓝球
                all_predictions.append(hit)
            except Exception as e:
                logger.warning(f"策略 {strat_name} 在期号 {actual.期号} 预测失败: {e}")

    # 统计各策略表现
    strategy_stats = _compute_strategy_stats(all_predictions, strategies, schema, is_rb)

    # 生成报告
    result = {
        "lottery_name": lottery_name,
        "is_redblue": is_rb,
        "window_size": window_size,
        "eval_periods": n_eval,
        "total_predictions": len(all_predictions),
        "training_time": datetime.now().isoformat(),
        "strategy_stats": strategy_stats,
        "predictions_detail": all_predictions,
    }

    # 保存报告
    record_dir = _save_rolling_report(result, lottery_name)
    # 返回目录名（完整路径由调用方自行拼接 TRAINING_DIR，避免前端双重路径）
    result["record_dir"] = record_dir.name if record_dir else ""

    return result


def _compute_strategy_stats(predictions: List[dict], strategies: List[str], schema, is_rb: bool) -> dict:
    """统计各策略的命中表现（红/蓝与数字型通用）"""
    stats = {}
    for name in strategies:
        strat_preds = [p for p in predictions if p["策略"] == name]
        if not strat_preds:
            stats[name] = {"error": "无预测数据"}
            continue

        total_hits_list = [p["总命中"] for p in strat_preds]
        prize_won = sum(1 for p in strat_preds if p["是否中奖"])

        # 中奖等级分布
        prize_grades = defaultdict(int)
        for p in strat_preds:
            prize_grades[p["中奖等级"]] += 1

        if is_rb:
            red_hits_list = [p["红球命中"] for p in strat_preds]
            blue_hits_list = [p["蓝球命中"] for p in strat_preds]
            stats[name] = {
                "样本数": len(strat_preds),
                "平均红球命中": round(float(np.mean(red_hits_list)), 4),
                "平均蓝球命中": round(float(np.mean(blue_hits_list)), 4),
                "平均总命中": round(float(np.mean(total_hits_list)), 4),
                "最大红球命中": max(red_hits_list),
                "最大蓝球命中": max(blue_hits_list),
                "中奖次数": prize_won,
                "中奖率": round(prize_won / len(strat_preds), 4),
                "红球命中分布": {str(k): v for k, v in _histogram(red_hits_list).items()},
                "蓝球命中分布": {str(k): v for k, v in _histogram(blue_hits_list).items()},
                "中奖等级分布": dict(prize_grades),
            }
        else:
            # 数字型：逐区平均命中 + 总位数命中
            zone_avg = {}
            for zone in schema.zones:
                lst = [p.get("分区命中", {}).get(zone.name, 0) for p in strat_preds]
                zone_avg[zone.name] = round(float(np.mean(lst)), 4) if lst else 0.0
            stats[name] = {
                "样本数": len(strat_preds),
                "平均红球命中": round(float(np.mean(total_hits_list)), 4),
                "平均蓝球命中": 0,
                "平均总命中": round(float(np.mean(total_hits_list)), 4),
                "总位数": schema.total_choose,
                "分区平均命中": zone_avg,
                "中奖次数": prize_won,
                "中奖率": round(prize_won / len(strat_preds), 4),
                "中奖等级分布": dict(prize_grades),
            }

    return stats


def _histogram(data: List[int]) -> dict:
    """简单直方图"""
    hist = defaultdict(int)
    for v in data:
        hist[v] += 1
    return dict(sorted(hist.items()))


def _save_rolling_report(result: dict, lottery_name: str) -> Path:
    """保存滚动训练报告到 training/ 目录"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    code = LOTTERY_CONFIG[lottery_name]["name_en"]
    record_dir = TRAINING_DIR / f"{timestamp}_{code}_rolling"
    record_dir.mkdir(parents=True, exist_ok=True)

    # 保存完整 JSON
    with open(record_dir / "rolling_report.json", "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2, default=str)

    # 生成可读 Markdown 报告
    report_md = _generate_markdown_report(result)
    with open(record_dir / "report.md", "w", encoding="utf-8") as f:
        f.write(report_md)

    logger.info(f"滚动训练报告已保存: {record_dir}")
    return record_dir


def _generate_markdown_report(result: dict) -> str:
    """生成 Markdown 格式的滚动训练报告"""
    lines = [
        f"# {result['lottery_name']} 滚动预测训练报告",
        f"",
        f"## 训练概要",
        f"",
        f"- **训练时间**: {result['training_time']}",
        f"- **窗口大小**: {result['window_size']} 期",
        f"- **评估期数**: {result['eval_periods']} 期",
        f"- **总预测次数**: {result['total_predictions']}",
        f"",
        f"## 各策略表现对比",
        f"",
    ]

    is_rb = result.get("is_redblue", True)
    if is_rb:
        lines.append(
            f"| 策略 | 样本数 | 平均红球命中 | 平均蓝球命中 | 平均总命中 | 最大红球 | 最大蓝球 | 中奖次数 | 中奖率 |"
        )
        lines.append(f"|------|--------|-------------|-------------|-----------|---------|---------|---------|--------|")
        for name, stats in result["strategy_stats"].items():
            if "error" in stats:
                lines.append(f"| {name} | - | ERROR | - | - | - | - | - | - |")
                continue
            lines.append(
                f"| {name} | {stats['样本数']} | {stats['平均红球命中']} | "
                f"{stats['平均蓝球命中']} | {stats['平均总命中']} | "
                f"{stats['最大红球命中']} | {stats['最大蓝球命中']} | "
                f"{stats['中奖次数']} | {stats['中奖率']*100:.2f}% |"
            )
    else:
        lines.append(
            f"| 策略 | 样本数 | 平均总位数命中 | 中奖次数 | 中奖率 | 分区平均命中 |"
        )
        lines.append(f"|------|--------|-------------|---------|--------|------------|")
        for name, stats in result["strategy_stats"].items():
            if "error" in stats:
                lines.append(f"| {name} | - | ERROR | - | - | - |")
                continue
            zone_avg = stats.get("分区平均命中", {})
            zone_str = "，".join(f"{z}:{v}" for z, v in zone_avg.items())
            lines.append(
                f"| {name} | {stats['样本数']} | {stats['平均总命中']} | "
                f"{stats['中奖次数']} | {stats['中奖率']*100:.2f}% | {zone_str} |"
            )

    lines.extend([
        f"",
        f"## 策略详细统计",
        f"",
    ])

    for name, stats in result["strategy_stats"].items():
        if "error" in stats:
            continue
        if is_rb:
            lines.extend([
                f"### {name}",
                f"",
                f"- **红球命中分布**: {stats['红球命中分布']}",
                f"- **蓝球命中分布**: {stats['蓝球命中分布']}",
                f"- **中奖等级分布**: {stats['中奖等级分布']}",
                f"",
            ])
        else:
            zone_avg = stats.get("分区平均命中", {})
            zone_str = "，".join(f"{z}:{v}" for z, v in zone_avg.items())
            lines.extend([
                f"### {name}",
                f"",
                f"- **分区平均命中**: {zone_str}",
                f"- **中奖等级分布**: {stats['中奖等级分布']}",
                f"",
            ])

    # 最近 10 期预测明细
    lines.extend([
        f"## 最近 10 期预测明细",
        f"",
        f"| 期号 | 策略 | 预测号码 | 实际号码 | 总命中 | 中奖 |",
        f"|------|------|---------|---------|--------|------|",
    ])

    recent = result["predictions_detail"][-30:]  # 最近 30 条（约 10 期 × 3 策略）
    for p in recent:
        pred_str = "  ".join(f"{z}:{','.join(str(n) for n in v)}" for z, v in p.get("预测分区", {}).items())
        actual_str = "  ".join(f"{z}:{','.join(str(n) for n in v)}" for z, v in p.get("实际分区", {}).items())
        lines.append(
            f"| {p['期号']} | {p['策略']} | {pred_str} | {actual_str} | "
            f"{p['总命中']} | {p['中奖等级']} |"
        )

    lines.extend([
        f"",
        f"## 说明",
        f"",
        f"- 本报告基于滚动预测训练生成：用前 {result['window_size']} 期数据预测下一期",
        f"- 评估窗口为最近 {result['eval_periods']} 期，每期用 3 种策略分别预测",
        f"- 中奖率 = (红球≥3 或 蓝球≥1 的次数) / 总预测次数",
        f"- 中奖等级按双色球规则分类（大乐透为近似）",
    ])

    return "\n".join(lines)
