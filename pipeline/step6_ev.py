"""
Step 6: 动态期望值计算（期望时机引擎）

升级自 Step2 的"静态期望"（固定官方返奖率）：
- 固定奖（三~六等奖）：奖金 × 概率，恒定
- 浮动奖（一/二等奖）：用当期真实数据（CSV 已抓取 奖池/注数/奖金）

核心洞察：头奖奖池滚存 → 头奖单注奖金爬升 → 单注总期望值逼近甚至越过 1.0
→ 系统可回答"这一期买，划不划算？"

数据来源：CSV 每期的 奖池奖金/一等奖注数/一等奖奖金/二等奖注数/二等奖奖金/总投注额
"""

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

from config import LOTTERY_CONFIG
from data.schema import LotteryData
from pipeline.step1_probability import get_prize_data

logger = logging.getLogger(__name__)

TICKET_PRICE = 2  # 每注 2 元


@dataclass
class PeriodEV:
    """单期期望计算结果"""
    期号: int
    开奖日期: str
    奖池奖金: Optional[int]
    一等奖注数: Optional[int]
    一等奖奖金: Optional[int]
    总投注额: Optional[int]
    固定奖期望: float      # 固定奖贡献（不含本金）
    浮动奖期望: float      # 一+二等奖贡献
    单注期望: float        # 总期望（含本金扣除 = 固定奖+浮动奖-2）
    是否值得买: bool
    备注: str = ""


def _prize_levels(lottery_name: str) -> List[dict]:
    """获取奖级概率表（红蓝走原逻辑，数字型走通用）"""
    return get_prize_data(lottery_name)["levels"]


def _fixed_prize_ev(lottery_name: str) -> float:
    """固定奖期望：Σ(固定奖金 × 概率)"""
    total = 0.0
    for lv in _prize_levels(lottery_name):
        if not lv.get("is_floating") and lv.get("fixed_amount"):
            total += lv["fixed_amount"] * lv["probability"]
    return total


def _floating_prize_ev(record, lottery_name: str) -> float:
    """
    浮动奖期望：Σ(当期单注奖金 × 概率)
    一等奖：CSV 的 一等奖奖金 已是当期单注奖金（含奖池分摊）
    二等奖：同理
    缺数据时回退历史估算
    """
    ev = 0.0
    levels = {lv["prize_name"]: lv for lv in _prize_levels(lottery_name)}

    for name, amount_attr in (("一等奖", "一等奖奖金"), ("二等奖", "二等奖奖金")):
        lv = levels.get(name)
        if not lv:
            continue
        amount = getattr(record, amount_attr, None)
        if amount:
            ev += amount * lv["probability"]
        else:
            # 回退历史估算
            est = {"双色球": {"一等奖": 5_000_000, "二等奖": 200_000},
                   "大乐透": {"一等奖": 5_000_000, "二等奖": 150_000}}.get(lottery_name, {}).get(name, 0)
            ev += est * lv["probability"]
    return ev


def compute_period_ev(record, lottery_name: str) -> PeriodEV:
    """计算单期动态期望"""
    fixed_ev = _fixed_prize_ev(lottery_name)
    float_ev = _floating_prize_ev(record, lottery_name)
    total_ev = fixed_ev + float_ev - TICKET_PRICE  # 扣本金

    return PeriodEV(
        期号=record.期号,
        开奖日期=str(record.开奖日期 or ""),
        奖池奖金=getattr(record, "奖池奖金", None),
        一等奖注数=getattr(record, "一等奖注数", None),
        一等奖奖金=getattr(record, "一等奖奖金", None),
        总投注额=getattr(record, "总投注额", None),
        固定奖期望=round(fixed_ev, 6),
        浮动奖期望=round(float_ev, 6),
        单注期望=round(total_ev, 6),
        是否值得买=total_ev >= 0,
        # 备注：说明驱动因素
        备注=_build_note(record, total_ev),
    )


def _build_note(record, total_ev: float) -> str:
    """生成备注：什么驱动了期望"""
    if total_ev >= 0:
        return "正期望！奖池滚存推动"
    return "常规期（期望为负）"


def compute_ev_series(lottery_name: str, data: LotteryData, limit: Optional[int] = None, days: Optional[int] = None) -> dict:
    """
    计算期望序列（历史所有期、最近 limit 期、或最近 days 个日历日）

    返回：
    {
        "lottery_name": ...,
        "ticket_price": 2,
        "fixed_ev": 固定奖期望,
        "periods": [PeriodEV...],   # 新的在前
        "stats": {统计}
    }
    """
    records = data.records  # 新的在前

    if days:
        # 日历窗口：保留最近 days 天的开奖（让不同开奖频率的彩种落在同一段日历时间，
        # 避免「近 N 期」因频率不同导致曲线跨度不一致、看起来不在同一时间轴）。
        from datetime import datetime, date, timedelta

        def _to_date(x):
            if isinstance(x, datetime):
                return x.date()
            if isinstance(x, date):
                return x
            if isinstance(x, str) and x:
                try:
                    return datetime.strptime(x[:10], "%Y-%m-%d").date()
                except ValueError:
                    return None
            return None

        dts = [_to_date(getattr(r, "开奖日期", None)) for r in records]
        valid = [d for d in dts if d]
        if valid:
            max_date = max(valid)
            cutoff = max_date - timedelta(days=days)
            records = [r for r, d in zip(records, dts) if d and d >= cutoff]
    elif limit:
        records = records[:limit]

    periods = []
    for rec in records:
        try:
            periods.append(compute_period_ev(rec, lottery_name))
        except Exception as e:
            logger.warning(f"期号 {rec.期号} 期望计算失败: {e}")

    # 统计（期望值不依赖销量，仅需一等奖奖金；缺奖金的最早期期数自动跳过）
    evs = [p.单注期望 for p in periods if p.一等奖奖金]
    if evs:
        stats = {
            "有效期数": len(evs),
            "平均期望": round(sum(evs) / len(evs), 4),
            "最大期望": round(max(evs), 4),
            "最小期望": round(min(evs), 4),
            "正期望期数": sum(1 for v in evs if v >= 0),
            "正期望占比": round(sum(1 for v in evs if v >= 0) / len(evs) * 100, 2),
        }
    else:
        stats = {"有效期数": 0}

    return {
        "lottery_name": lottery_name,
        "ticket_price": TICKET_PRICE,
        "固定奖期望": round(_fixed_prize_ev(lottery_name), 6),
        "periods": [p.__dict__ for p in periods],
        "stats": stats,
    }


def current_period_advice(lottery_name: str, data: LotteryData) -> dict:
    """
    当前期购买建议（供看板/流水线展示）
    """
    series = compute_ev_series(lottery_name, data, limit=1)
    if not series["periods"]:
        return {"lottery": lottery_name, "error": "无数据"}
    p = series["periods"][0]
    return {
        "lottery": lottery_name,
        "期号": p["期号"],
        "开奖日期": p["开奖日期"],
        "单注期望": p["单注期望"],
        "固定奖期望": p["固定奖期望"],
        "浮动奖期望": p["浮动奖期望"],
        "奖池奖金": p["奖池奖金"],
        "是否值得买": p["是否值得买"],
        "建议": "🔥 值得买（正期望）" if p["是否值得买"] else "常规期，建议小注或不买",
    }


def run(data: LotteryData, limit: int = 50) -> dict:
    """CLI 主入口：输出最近 limit 期期望序列"""
    name = data.lottery_name
    logger.info(f"Step6: 动态期望计算 — {name}")
    series = compute_ev_series(name, data, limit=limit)
    advice = current_period_advice(name, data)
    return {"step": 6, **series, "current_advice": advice}
