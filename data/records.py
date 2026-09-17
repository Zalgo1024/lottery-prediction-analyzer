"""
策略战绩聚合模块

两层战绩数据：
1. 实盘战绩：从 feedback_history（开奖后真实对比）聚合
2. 回测战绩：从最近的 rolling 报告（历史滚动预测）读取

用途：Web 战绩榜 /records 页面的数据源
"""

import json
import logging
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from config import LOTTERY_CONFIG, TRAINING_DIR
from data.feedback import normalize_strategy_name, ML_STRATEGY
from data.schema import is_redblue, get_schema

logger = logging.getLogger(__name__)

# 战绩榜支持的策略显示顺序（ML策略 归一自 ML(logistic)/统计训练(Wxx)）
STRATEGY_ORDER = ["高频策略", "遗漏值策略", "区间均衡策略", "ML策略", "规则优选"]

FEEDBACK_DIR = TRAINING_DIR / "feedback"


# ============================================================
# 实盘战绩（feedback_history）
# ============================================================

def _load_feedback_history(lottery_name: str) -> List[dict]:
    """加载反馈历史（最近 500 条）"""
    path = FEEDBACK_DIR / f"{lottery_name}_feedback_history.json"
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        history = json.load(f)
    return history[-500:]


def _trend_arrow(history: List[dict], strategy: str, metric_key: str = "总命中") -> str:
    """近 10 期 vs 前 10 期的命中趋势：↑ / ↓ / →

    红蓝彩种默认用「总命中」做趋势（与数字型统一），避免数字型无红球字段。
    """
    strat = [h for h in history if normalize_strategy_name(h.get("策略")) == strategy]
    if len(strat) < 10:
        return "—"
    recent = [h.get(metric_key, 0) for h in strat[:10]]
    older = [h.get(metric_key, 0) for h in strat[10:20]]
    if not older:
        return "—"
    diff = np.mean(recent) - np.mean(older)
    if diff > 0.15:
        return "↑"
    if diff < -0.15:
        return "↓"
    return "→"


def get_feedback_records(lottery_name: str) -> Dict:
    """
    实盘战绩：按策略聚合 feedback_history
    返回 {strategies: {策略名: 统计}, total, last_evaluated, is_redblue}
    """
    history = _load_feedback_history(lottery_name)
    rb = is_redblue(lottery_name)
    if not history:
        return {"strategies": {}, "total": 0, "last_evaluated": "", "is_redblue": rb}

    # 按策略分组（按时间倒序，history 最新在前）
    # 自适应：收集历史中实际出现的策略，顺序按 STRATEGY_ORDER 优先
    # ML 家族（ML(logistic)/统计训练(Wxx)）统一归并为 "ML策略"
    seen = list(dict.fromkeys(normalize_strategy_name(h.get("策略")) for h in history if h.get("策略")))
    ordered_names = [s for s in STRATEGY_ORDER if s in seen] + [s for s in seen if s not in STRATEGY_ORDER]

    stats = {}
    for name in ordered_names:
        strat = [h for h in history if normalize_strategy_name(h.get("策略")) == name]
        if not strat:
            continue
        if rb:
            red_hits = [h.get("红球命中", 0) for h in strat]
            blue_hits = [h.get("蓝球命中", 0) for h in strat]
        total_hits = [h.get("总命中", 0) for h in strat]
        prize_won = sum(1 for h in strat if h.get("中奖等级", "未中") != "未中")
        grades = defaultdict(int)
        for h in strat:
            grades[h.get("中奖等级", "未中")] += 1
        row = {
            "样本数": len(strat),
            "平均总命中": round(float(np.mean(total_hits)), 3),
            "中奖次数": prize_won,
            "中奖率": round(prize_won / len(strat), 4),
            "中奖等级分布": dict(grades),
            "趋势": _trend_arrow(history, name),
        }
        if rb:
            row["平均红球命中"] = round(float(np.mean(red_hits)), 3)
            row["平均蓝球命中"] = round(float(np.mean(blue_hits)), 3)
        stats[name] = row

    return {
        "strategies": stats,
        "total": len(history),
        "last_evaluated": history[0].get("评估时间", "") if history else "",
        "is_redblue": rb,
    }


# ============================================================
# 回测战绩（rolling 报告）
# ============================================================

def get_rolling_records(lottery_name: str) -> Optional[Dict]:
    """读取最近的滚动训练报告（回测战绩）"""
    records = sorted(TRAINING_DIR.glob(f"*_{lottery_name}_rolling"), reverse=True)
    if not records:
        return None
    latest = records[0]
    report_file = latest / "rolling_report.json"
    if not report_file.exists():
        return None
    try:
        with open(report_file, encoding="utf-8") as f:
            report = json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        logger.warning(f"读取 rolling 报告失败: {e}")
        return None

    return {
        "record_dir": latest.name,
        "window_size": report.get("window_size"),
        "eval_periods": report.get("eval_periods"),
        "total_predictions": report.get("total_predictions"),
        "training_time": report.get("training_time"),
        "strategy_stats": report.get("strategy_stats", {}),
    }


# ============================================================
# 汇总接口（Web 用）
# ============================================================

def get_records_summary(lottery_name: str) -> Dict:
    """战绩榜汇总：实盘 + 回测两层"""
    rb = is_redblue(lottery_name)
    return {
        "lottery": lottery_name,
        "is_redblue": rb,
        "feedback": get_feedback_records(lottery_name),
        "rolling": get_rolling_records(lottery_name),
    }


if __name__ == "__main__":
    import sys
    for name in LOTTERY_CONFIG:
        r = get_records_summary(name)
        print(f"===== {name} =====")
        print(f"实盘: {r['feedback']['total']} 条 | 回测: {'有' if r['rolling'] else '无'}")
        for s, st in r["feedback"]["strategies"].items():
            print(f"  {s}: 样本{st['样本数']} 红{st['平均红球命中']} 中奖率{st['中奖率']} 趋势{st['趋势']}")
