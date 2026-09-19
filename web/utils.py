"""
Web 后端工具层
包装现有的 CLI 模块，供 Flask 路由调用
含：数据刷新、预测回测、异步任务管理
"""

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from flask import jsonify

import pandas as pd

from config import (LOTTERY_CONFIG, TRAINING_DIR, PREDICT_DEFAULTS,
                    resolve_groups, TRIAL_ARMS)
from data.loader import load_lottery, FILE_PATHS, _file_path
from data.schema import LotteryData, parse_draw_date, get_schema, is_redblue, record_zone_numbers

logger = logging.getLogger(__name__)


# ============================================================
# 通用分区视图（方向A：红蓝彩种 ↔ 数字型统一渲染结构）
# 把任意彩种的「预测/实际/命中」归一化为 zones 列表：
#   红球 -> cls="red"、蓝球 -> cls="blue"、其余(cls=None)由前端按调色板着色。
# 双色球/大乐透的记录仍保留原有 红球/蓝球 字段（零回归），
# 此视图仅作为前端通用渲染的输入。
# ============================================================

def feedback_record_view(lottery_name: str, rec: dict) -> dict:
    """把一条反馈记录归一化为通用 zones 结构，供前端对比卡/战绩表渲染。"""
    schema = get_schema(lottery_name)
    is_rb = is_redblue(lottery_name)
    if is_rb:
        pred = {"红球": rec.get("预测红球", []), "蓝球": rec.get("预测蓝球", [])}
        actual = {"红球": rec.get("实际红球", []), "蓝球": rec.get("实际蓝球", [])}
        hit_map = {"红球": rec.get("红球命中", 0), "蓝球": rec.get("蓝球命中", 0)}
        total_choose = schema.red_zone.choose + schema.blue_zone.choose
    else:
        pred = rec.get("预测号码") or {}
        actual = rec.get("实际号码") or {}
        hit_map = rec.get("分区命中") or {}
        total_choose = rec.get("总选择") or sum(z.choose for z in schema.zones)
    zones = []
    for z in schema.zones:
        name = z.name
        zones.append({
            "name": name,
            "pred": list(pred.get(name, []) or []),
            "actual": list(actual.get(name, []) or []),
            "hit": int(hit_map.get(name, 0) or 0),
            "choose": z.choose,
            "cls": "red" if name == "红球" else ("blue" if name == "蓝球" else None),
        })
    total_hit = sum(z["hit"] for z in zones)
    return {
        "is_redblue": is_rb,
        "zones": zones,
        "total_choose": total_choose,
        "total_hit": total_hit,
    }


def predict_set_view(lottery_name: str, ps: dict) -> dict:
    """把一组预测号码（PredictSet.to_dict）归一化为通用 zones 结构（仅预测球）。"""
    schema = get_schema(lottery_name)
    zones_dict = ps.get("号码") or {}
    if not zones_dict and ("红球" in ps or "蓝球" in ps):
        zones_dict = {"红球": ps.get("红球", []), "蓝球": ps.get("蓝球", [])}
    zones = []
    for z in schema.zones:
        name = z.name
        zones.append({
            "name": name,
            "pred": list(zones_dict.get(name, []) or []),
            "cls": "red" if name == "红球" else ("blue" if name == "蓝球" else None),
        })
    return {"is_redblue": is_redblue(lottery_name), "zones": zones}


# ============================================================
# API 错误边界（统一捕获异常返回 JSON 而非 HTML 500）
# ============================================================

def api_error_handler(f):
    """装饰器：捕获异常返回 JSON 而非 HTML 500"""
    @wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except Exception as e:
            logger.exception(f"API error in {f.__name__}")
            return jsonify({"error": str(e), "route": f.__name__}), 500
    return wrapper


# ============================================================
# 数据状态 & 刷新
# ============================================================

def _compute_next_draw_date(latest_date: str, draw_days: List[int]) -> str:
    """根据最近开奖日期和开奖日表，计算下一期开奖日期（严格在 latest_date 之后）。"""
    if not latest_date:
        return ""
    try:
        d = datetime.strptime(str(latest_date).split()[0], "%Y-%m-%d")
    except Exception:
        return ""
    for i in range(1, 14):
        cand = d + timedelta(days=i)
        if cand.weekday() in draw_days:
            return cand.strftime("%Y-%m-%d")
    return ""


def _expected_latest_date(draw_days: List[int], now: Optional[datetime] = None) -> datetime.date:
    """
    根据开奖日规则，计算"最近一个应该已经开奖的日期"。
    每日开奖：当天已过 21:00 则今天，否则昨天。
    非每日：从今天往前找最近的开奖日；若落在今天，需已过 21:30 才视为已开奖。
    """
    if now is None:
        now = datetime.now()
    today = now.date()
    unique_days = set(int(d) for d in draw_days)
    is_daily = len(unique_days) == 7

    if is_daily:
        cutoff = now.replace(hour=21, minute=0, second=0, microsecond=0)
        return today if now >= cutoff else today - timedelta(days=1)

    for i in range(14):
        cand = today - timedelta(days=i)
        if cand.weekday() in unique_days:
            if i == 0:
                cutoff = now.replace(hour=21, minute=30, second=0, microsecond=0)
                if now >= cutoff:
                    return cand
                continue
            return cand
    return today


def get_data_status(lottery_name: str) -> dict:
    """
    检查指定彩票的数据状态：
    - 文件是否存在
    - 文件修改时间
    - 数据总期数
    - 最新期号 + 日期
    - 清洗后数据情况
    """
    # 统一使用 loader 的通用路径推导，支持双色球/大乐透 + 数字型/七星彩
    csv_path = _file_path(lottery_name)
    result = {
        "lottery": lottery_name,
        "csv_exists": False,
        "csv_size_kb": 0,
        "csv_modified": "",
        "cleaned_exists": False,
        "total_records": 0,
        "latest_draw": "",
        "latest_date": "",
        "earliest_draw": "",
    }

    if csv_path and csv_path.exists():
        result["csv_exists"] = True
        result["csv_size_kb"] = round(csv_path.stat().st_size / 1024, 1)
        result["csv_modified"] = datetime.fromtimestamp(
            csv_path.stat().st_mtime
        ).strftime("%Y-%m-%d %H:%M:%S")

        try:
            data = load_lottery(lottery_name)
            result["total_records"] = data.total_records
            if data.records:
                result["latest_draw"] = str(data.records[0].期号)
                result["latest_date"] = str(data.records[0].开奖日期 or "")
                result["earliest_draw"] = str(data.records[-1].期号)
                cfg = LOTTERY_CONFIG.get(lottery_name, {})
                result["next_draw_date"] = _compute_next_draw_date(result["latest_date"], cfg.get("draw_days", []))
                try:
                    ld = datetime.strptime(str(result["latest_date"]).split()[0], "%Y-%m-%d").date()
                    expected = _expected_latest_date(cfg.get("draw_days", []))
                    if ld < expected:
                        result["data_lag_days"] = (expected - ld).days
                    else:
                        result["data_lag_days"] = 0
                except Exception:
                    result["data_lag_days"] = 0
        except Exception as e:
            result["error"] = str(e)

    # 检查清洗文件
    cleaned_path = csv_path.parent / f"{csv_path.stem}_cleaned.csv" if csv_path else None
    if cleaned_path and cleaned_path.exists():
        result["cleaned_exists"] = True

    return result


def refresh_data(lottery_name: str) -> dict:
    """
    刷新数据：重新清洗 CSV + 清除缓存
    返回刷新结果
    """
    from data.cleaner import run_clean
    from data.cache import StatsCache

    try:
        # 数字型/七星彩无需红蓝清洗，直接清缓存重载
        if is_redblue(lottery_name):
            run_clean(lottery_name)
        # 清除缓存（统计缓存 + 内存数据缓存）
        StatsCache().invalidate(lottery_name)
        from data.loader import invalidate_lottery_cache
        invalidate_lottery_cache(lottery_name)
        # 重新加载验证
        data = load_lottery(lottery_name)
        return {
            "success": True,
            "lottery": lottery_name,
            "total_records": data.total_records,
            "latest_draw": str(data.records[0].期号),
            "message": f"刷新完成：{data.total_records} 条记录，最新期号 {data.records[0].期号}",
        }
    except Exception as e:
        return {"success": False, "lottery": lottery_name, "error": str(e)}


def get_all_data_status() -> Dict[str, dict]:
    """获取所有彩票的数据状态"""
    return {name: get_data_status(name) for name in LOTTERY_CONFIG}


# ============================================================
# 训练结果 vs 最新号码对比分析
# ============================================================

def compare_training_with_actual(lottery_name: str) -> dict:
    """
    对比最近一次训练/预测结果与实际开奖号码。
    支持双色球/大乐透（红蓝区）与数字型/七星彩（通用分区）。
    """
    data = load_lottery(lottery_name)
    if not data.records:
        return {"error": "无数据"}

    latest = data.records[0]  # 最新一期实际开奖
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = get_schema(lottery_name)
    is_rb = is_redblue(lottery_name)

    # 查找最近的预测记录（兼容新 ASCII 目录名与历史中文目录名）
    code = LOTTERY_CONFIG[lottery_name]["name_en"]
    predict_records = sorted(
        [d for pat in (f"*_{code}_predict", f"*_{lottery_name}_predict")
         for d in TRAINING_DIR.glob(pat) if d.is_dir()],
        reverse=True,
    )

    actual_zones = _actual_zone_numbers(latest, schema, is_rb)
    result = {
        "lottery": lottery_name,
        "is_redblue": is_rb,
        "actual_draw": str(latest.期号),
        "actual_date": str(latest.开奖日期 or ""),
        "actual_zones": [
            {"name": name, "numbers": nums, "cls": _zone_cls(name)}
            for name, nums in actual_zones.items()
        ],
        "predictions_analyzed": 0,
        "predictions": [],
        "summary": {},
        "historical_hit_rate": _compute_historical_hit_rate(data),
    }
    # 红蓝彩种保留旧字段，确保前端旧路径零回归
    if is_rb:
        result["actual_red"] = latest.红球
        result["actual_blue"] = latest.蓝球

    if not predict_records:
        result["error"] = "暂无预测记录，请先运行 predict"
        return result

    # 分析最近的预测记录（最多 3 条）
    for pred_dir in predict_records[:3]:
        pred_file = pred_dir / "prediction.json"
        if not pred_file.exists():
            continue

        try:
            with open(pred_file, encoding="utf-8") as f:
                pred_data = json.load(f)
        except (json.JSONDecodeError, IOError):
            continue

        numbers = pred_data.get("numbers", [])
        if not numbers:
            continue

        result["predictions_analyzed"] += 1
        pred_analysis = {
            "record_dir": pred_dir.name,
            "date": pred_data.get("date", ""),
            "mode": pred_data.get("mode", ""),
            "groups": len(numbers),
            "groups_detail": [],
        }

        zone_totals = {z.name: 0 for z in schema.zones}
        total_choose = schema.total_choose
        best_group_hits = 0
        best_group_idx = -1

        for i, ps in enumerate(numbers):
            pred_zones = _extract_zone_numbers(ps, schema, is_rb)
            group_hits = 0
            zone_hits = {}
            for z in schema.zones:
                actual_set = set(actual_zones[z.name])
                pred_set = set(pred_zones[z.name])
                zh = len(pred_set & actual_set)
                zone_hits[z.name] = zh
                zone_totals[z.name] += zh
                group_hits += zh

            if group_hits > best_group_hits:
                best_group_hits = group_hits
                best_group_idx = i

            detail = {
                "group": i + 1,
                "total_hits": group_hits,
                "confidence": ps.get("置信度", 0),
                "pred_zones": [
                    {"name": z.name, "numbers": pred_zones[z.name], "cls": _zone_cls(z.name)}
                    for z in schema.zones
                ],
                "actual_zones": result["actual_zones"],
                "zone_hits": zone_hits,
            }
            # 红蓝彩种保留旧字段
            if is_rb:
                detail["pred_red"] = sorted(pred_zones.get("红球", []))
                detail["pred_blue"] = sorted(pred_zones.get("蓝球", []))
                detail["red_hits"] = zone_hits.get("红球", 0)
                detail["blue_hits"] = zone_hits.get("蓝球", 0)
            pred_analysis["groups_detail"].append(detail)

        n_groups = len(numbers)
        summary = {
            "avg_total_hits": round(sum(zone_totals.values()) / n_groups, 2) if n_groups else 0,
            "total_hits": sum(zone_totals.values()),
            "best_group": best_group_idx + 1,
            "best_group_hits": best_group_hits,
            "zone_avg_hits": {
                z.name: round(zone_totals[z.name] / n_groups, 2) if n_groups else 0
                for z in schema.zones
            },
            "total_choose": total_choose,
        }
        if is_rb:
            summary["avg_red_hits"] = round(zone_totals.get("红球", 0) / n_groups, 2) if n_groups else 0
            summary["avg_blue_hits"] = round(zone_totals.get("蓝球", 0) / n_groups, 2) if n_groups else 0
            summary["total_red_hits"] = zone_totals.get("红球", 0)
            summary["total_blue_hits"] = zone_totals.get("蓝球", 0)
        pred_analysis["summary"] = summary

        result["predictions"].append(pred_analysis)

    # 汇总统计
    if result["predictions"]:
        newest = result["predictions"][0]
        s = newest["summary"]
        total_hits = s["total_hits"]
        possible = newest["groups"] * s["total_choose"]
        overall_rate = total_hits / max(possible, 1) * 100
        result["summary"] = {
            "groups_analyzed": newest["groups"],
            "avg_total_hits_per_group": s["avg_total_hits"],
            "overall_hit_rate": f"{overall_rate:.1f}%",
            "best_group": f"#{s['best_group']} ({s['best_group_hits']} hits)",
            "historical_avg": f"{result['historical_hit_rate'] * 100:.1f}%",
            "verdict": _generate_verdict_general(
                s["avg_total_hits"], s["total_choose"], result["historical_hit_rate"]
            ),
            "zone_avg_hits": s.get("zone_avg_hits", {}),
        }
        if is_rb:
            total_possible_red = newest["groups"] * cfg["red_count"]
            result["summary"].update({
                "avg_red_hits_per_group": s["avg_red_hits"],
                "avg_blue_hits_per_group": s["avg_blue_hits"],
                "overall_red_hit_rate": f"{s['total_red_hits'] / max(total_possible_red, 1) * 100:.1f}%",
            })

    return result


def _zone_cls(name: str) -> Optional[str]:
    """分区 -> 前端样式类：红球/蓝球保留，其余数字型分区返回 null。"""
    if name == "红球":
        return "red"
    if name == "蓝球":
        return "blue"
    return None


def _extract_zone_numbers(ps: dict, schema, is_rb: bool) -> Dict[str, List[int]]:
    """从预测组字典中提取各区号码。"""
    if is_rb:
        return {
            "红球": list(ps.get("红球", []) or []),
            "蓝球": list(ps.get("蓝球", []) or []),
        }
    nums = ps.get("号码") or {}
    return {z.name: list(nums.get(z.name, []) or []) for z in schema.zones}


def _actual_zone_numbers(rec, schema, is_rb: bool) -> Dict[str, List[int]]:
    """从开奖记录中提取各区实际号码。"""
    if is_rb:
        return {"红球": list(rec.红球), "蓝球": list(rec.蓝球)}
    return {z.name: record_zone_numbers(rec, z) for z in schema.zones}


def _generate_verdict_general(avg_total_hits: float, total_choose: int, historical_hit: float) -> str:
    """生成通用对比结论（兼容红蓝与数字型）。"""
    if total_choose <= 0:
        return "无法评估命中情况"
    rate = avg_total_hits / total_choose
    hist_rate = historical_hit
    if rate >= hist_rate * 1.2:
        return f"总命中({avg_total_hits:.1f}/{total_choose}位)高于历史热号基准({hist_rate*100:.1f}%)"
    elif rate >= hist_rate * 0.8:
        return f"总命中({avg_total_hits:.1f}/{total_choose}位)与历史热号基准({hist_rate*100:.1f}%)接近"
    else:
        return f"总命中({avg_total_hits:.1f}/{total_choose}位)低于历史热号基准({hist_rate*100:.1f}%)"


def _compute_historical_hit_rate(data: LotteryData) -> float:
    """计算近 50 期热号（Top10）的命中率（通用：红蓝取红球，数字型取所有分区和）。"""
    from pipeline.statistics import frequency_analysis
    schema = get_schema(data.lottery_name)
    freqs = frequency_analysis(data)

    # 构建统一的热号集合：每个分区取 TopN，N 按该区 choose 缩放
    hot_set = set()
    total_choose = 0
    for z in schema.zones:
        zf = freqs.get(z.name, {}).get("freq", {})
        topn = max(z.choose * 2, 5)
        sorted_nums = sorted(zf.items(), key=lambda x: x[1], reverse=True)
        hot_set.update(n for n, _ in sorted_nums[:topn])
        total_choose += z.choose

    recent = data.records[:50]
    if len(recent) < 10 or total_choose == 0:
        return 0.0

    hits = 0
    total = 0
    for r in recent:
        for z in schema.zones:
            nums = record_zone_numbers(r, z)
            total += len(nums)
            hits += sum(1 for n in nums if n in hot_set)
    return hits / max(total, 1)


def _generate_verdict(avg_red, avg_blue, historical_hit, cfg) -> str:
    """生成对比结论"""
    parts = []
    if avg_red >= 1.5:
        parts.append(f"红球命中({avg_red:.1f}/组)高于随机期望")
    elif avg_red >= 0.8:
        parts.append(f"红球命中({avg_red:.1f}/组)处于正常范围")
    else:
        parts.append(f"红球命中({avg_red:.1f}/组)偏低")

    if avg_blue >= 0.5:
        parts.append(f"，蓝球命中({avg_blue:.1f}/组)较好")
    elif avg_blue > 0:
        parts.append(f"，蓝球命中({avg_blue:.1f}/组)")
    else:
        parts.append(f"，蓝球未命中")

    return "".join(parts)


# ============================================================
# 异步任务管理（SQLite 持久化）
# ============================================================

from web.task_store import task_store

create_task = task_store.create
update_task = task_store.update
get_task    = task_store.get


def run_async(func):
    """装饰器：将函数作为异步任务运行（SQLite 持久化）"""
    @wraps(func)
    def wrapper(task_id: str, *args, **kwargs):
        try:
            update_task(task_id, status="running", progress=0, message="启动中...")
            result = func(task_id, *args, **kwargs)
            update_task(task_id, status="done", progress=100, message="完成", result=result)
        except Exception as e:
            logger.exception(f"Task {task_id} failed")
            update_task(task_id, status="error", message=str(e), error=str(e))
    return wrapper


# ============================================================
# 训练异步任务
# ============================================================

@run_async
def run_train_task(task_id: str, lottery_name: str, params: dict):
    """异步运行训练任务"""
    from train.engine import train

    update_task(task_id, progress=5, message=f"加载 {lottery_name} 数据...")
    time.sleep(0.5)

    result = train(
        lottery_name=lottery_name,
        iterations=params.get("iterations"),
        window=params.get("window"),
        window_list=params.get("window_list"),
        model_type=params.get("model_type", "statistical"),
        patience=params.get("patience"),
    )

    update_task(task_id, progress=90, message="保存完成")

    window_results = {}
    for w, wr in result.get("multi_window_results", {}).items():
        if "error" in wr:
            continue
        sev = wr.get("selection_eval") or {}
        window_results[w] = {
            "best_loss": wr["best_loss"],
            "best_hit_rate": wr.get("best_hit_rate", 0),
            "selection_total_hits": wr.get("selection_total_hits"),
            "expected_total_hits": sev.get("expected_total_hits"),
            "total_advantage": sev.get("total_advantage"),
            "p_value": sev.get("p_value"),
            "significant_05": sev.get("significant_05"),
            "significance_note": sev.get("significance_note"),
        }

    best_wr = result.get("multi_window_results", {}).get(str(result.get("best_window")), {})
    best_sev = best_wr.get("selection_eval") or {}
    return {
        "record_dir": result.get("record_dir"),
        "best_window": result.get("best_window"),
        "best_loss": result.get("best_loss"),
        "selection_total_hits": best_wr.get("selection_total_hits"),
        "selection_advantage": best_sev.get("total_advantage"),
        "selection_p_value": best_sev.get("p_value"),
        "selection_significance": best_sev.get("significance_note"),
        "window_results": window_results,
    }


def start_train(lottery_name: str, params: dict) -> str:
    """启动训练任务，返回 task_id"""
    task_id = create_task("train", {"lottery": lottery_name, **params})
    thread = threading.Thread(target=run_train_task, args=(task_id, lottery_name, params), daemon=True)
    thread.start()
    return task_id


# ============================================================
# 滚动预测训练异步任务
# ============================================================

@run_async
def run_rolling_task(task_id: str, lottery_name: str, params: dict):
    """异步运行滚动预测训练任务"""
    from train.rolling_trainer import rolling_train

    update_task(task_id, progress=5, message=f"加载 {lottery_name} 数据...")
    time.sleep(0.3)

    result = rolling_train(
        lottery_name=lottery_name,
        window_size=params.get("window_size", 50),
        eval_periods=params.get("eval_periods", 100),
    )

    if "error" in result:
        raise RuntimeError(result["error"])

    update_task(task_id, progress=90, message="生成报告完成")

    # 精简返回（剔除超长明细，只保留统计）
    # record_dir 已是目录名（rolling_train 返回 .name）；防御性再取一次 name
    rd = result.get("record_dir")
    if rd:
        from pathlib import Path
        rd = Path(rd).name
    return {
        "record_dir": rd,
        "is_redblue": result.get("is_redblue"),
        "window_size": result.get("window_size"),
        "eval_periods": result.get("eval_periods"),
        "total_predictions": result.get("total_predictions"),
        "strategy_stats": result.get("strategy_stats", {}),
        "training_time": result.get("training_time"),
    }


def start_rolling(lottery_name: str, params: dict) -> str:
    """启动滚动预测训练任务，返回 task_id"""
    task_id = create_task("rolling", {"lottery": lottery_name, **params})
    thread = threading.Thread(
        target=run_rolling_task, args=(task_id, lottery_name, params), daemon=True
    )
    thread.start()
    return task_id


# ============================================================
# 随机锚点走前验证异步任务（2026-09-18，持续训练）
# ============================================================

@run_async
def run_anchor_task(task_id: str, lottery_name: str, params: dict):
    """异步运行一轮随机锚点走前验证（与常驻循环同一实现，口径不漂移）。"""
    from train.anchor_trainer import anchor_train

    update_task(task_id, progress=5, message=f"加载 {lottery_name} 历史并抽取锚点...")
    time.sleep(0.3)

    result = anchor_train(
        lottery_name,
        n_trials=int(params.get("n_trials", 10)),
        window_size=int(params.get("window_size", 50)),
        min_history=int(params.get("min_history", 100)),
    )
    if "error" in result:
        raise RuntimeError(result["error"])

    update_task(task_id, progress=90, message="汇总配对差值与显著性...")
    # 精简返回：只保留汇总，不回传逐试验明细（明细在 JSONL 台账里）
    return {
        "lottery_name": result.get("lottery_name"),
        "n_trials": result.get("n_trials"),
        "seed": result.get("seed"),
        "window_size": result.get("window_size"),
        "anchor_issues": result.get("anchor_issues", []),
        "is_redblue": result.get("is_redblue"),
        "stats": result.get("stats", {}),
        "training_time": result.get("training_time"),
        "诚实声明": result.get("诚实声明"),
    }


def start_anchor(lottery_name: str, params: dict) -> str:
    """启动随机锚点走前验证任务，返回 task_id"""
    task_id = create_task("anchor", {"lottery": lottery_name, **params})
    thread = threading.Thread(
        target=run_anchor_task, args=(task_id, lottery_name, params), daemon=True
    )
    thread.start()
    return task_id


# ============================================================
# 管道异步任务
# ============================================================

def _simplify_pipeline_result(results: dict) -> dict:
    """简化管道结果为 API 友好格式"""
    simplified = {}
    for step, data in results.items():
        s = {"step": step}
        if step == 1:
            s["total_combinations"] = data["total_combinations"]
            s["head_prize_prob"] = data["head_prize_probability"]
            s["total_win_prob"] = data["total_win_probability"]
            s["prize_levels"] = [{
                "name": l["prize_name"],
                "prob": l["probability"],
                "condition": l["condition"],
                "amount": l.get("fixed_amount", "浮动"),
            } for l in data["prize_levels"]]
        elif step == 2:
            s["expected_return"] = data["expected_value"]["单注期望收益"]
            s["payout_rate"] = data["official_return_rate"]["理论返奖率"]
            s["conclusion"] = data["expected_value"]["结论"]
        elif step == 3:
            s["binom_100"] = data["二项分布(100期)"]["至少中一次概率"]
            s["first_win_expect"] = data["几何分布(首次中奖)"]["期望首次中奖期数"]
            s["sim_profit_rate"] = data["蒙特卡洛模拟"]["简化盈亏统计"]["盈利比例"]
        elif step == 4:
            s["std_dev"] = data["方差与标准差"]["标准差"]
            s["entropy"] = data["信息熵"]["总信息熵"]
            s["ci_95"] = data["置信区间"]["95%置信区间"]
        elif step == 5:
            s["kelly"] = data["凯利准则"]["综合凯利比例"]
            s["sharpe"] = data["夏普比率"]["夏普比率"]
            fairness = data.get("假设检验(公平性)", {})
            if "红球检验" in fairness:
                s["fairness_red"] = fairness["红球检验"]["结论"]
                s["fairness_blue"] = fairness["蓝球检验"]["结论"]
            else:
                # 数字型：按分区列出公平性结论
                zone_fairness = fairness.get("各分区检验", {})
                s["fairness_zones"] = {
                    z: v["结论"] for z, v in zone_fairness.items()
                }
        simplified[str(step)] = s
    return simplified


@run_async
def run_pipeline_task(task_id: str, lottery_name: str, params: dict):
    """异步执行管道计算"""
    from pipeline.runner import run_pipeline

    steps = params.get("steps", [1, 2, 3, 4, 5])
    update_task(task_id, progress=5, message=f"加载 {lottery_name} 数据...")
    time.sleep(0.3)

    results = run_pipeline(lottery_name, steps=steps)
    update_task(task_id, progress=90, message="计算完成，整理结果...")

    return _simplify_pipeline_result(results)


def start_pipeline(lottery_name: str, params: dict) -> str:
    """启动管道计算任务，返回 task_id"""
    task_id = create_task("pipeline", {"lottery": lottery_name, **params})
    thread = threading.Thread(
        target=run_pipeline_task, args=(task_id, lottery_name, params), daemon=True
    )
    thread.start()
    return task_id


# ============================================================
# 异常检测异步任务
# ============================================================

@run_async
def run_anomaly_task(task_id: str, lottery_name: str, params: dict):
    """异步执行异常检测"""
    from prediction.anomaly import detect_anomalies
    from datetime import datetime

    update_task(task_id, progress=10, message="开始异常检测...")
    result = detect_anomalies(
        lottery_name=lottery_name,
        threshold=params.get("threshold", "normal"),
        chart=True,
    )

    # 号码频率数据（供前端 Canvas 交互图）—— 通用化：按 Schema.zones 遍历
    # 双色球/大乐透 zone 名即 红球/蓝球；数字型为 第1位/第2位/... 等分区名，前端统一按 zones 渲染
    from data.loader import load_lottery
    from config import LOTTERY_CONFIG
    from data.schema import get_schema, record_zone_numbers
    from collections import Counter
    data = load_lottery(lottery_name)
    schema = get_schema(lottery_name)
    zone_counts = {z.name: Counter() for z in schema.zones}
    for rec in data.records:
        for z in schema.zones:
            zone_counts[z.name].update(record_zone_numbers(rec, z))
    freq_data = {"zones": {}, "理论频率": {}, "每区选取数": {}, "总期数": len(data.records)}
    for z in schema.zones:
        lo, hi = z.range_tuple()
        counts = zone_counts[z.name]
        freq_data["zones"][z.name] = [
            {"号码": n, "次数": counts.get(n, 0)} for n in range(lo, hi + 1)
        ]
        freq_data["理论频率"][z.name] = z.choose / (hi - lo + 1)
        freq_data["每区选取数"][z.name] = z.choose
    # 向后兼容：红/蓝双区彩种额外挂顶层 红球/蓝球 键，避免任何旧取值路径报错
    if schema.red_zone and schema.blue_zone:
        freq_data["红球"] = freq_data["zones"]["红球"]
        freq_data["蓝球"] = freq_data["zones"]["蓝球"]

    # 单档返回详细结构；all 模式返回三档对比结构，字段不同，需分别取值避免 KeyError
    is_all = result.get("阈值等级") == "all"
    if is_all:
        comparison = result.get("三档对比", {})
        total_records = result.get("总期数", freq_data.get("总期数", 0))
        # 以 normal 档的异常总数作为代表展示
        total_anomalies = comparison.get("normal", {}).get("异常总条数", 0)
        by_method = {}
        details = []
        chart_files = result.get("图表文件", [])
        csv_file = result.get("导出文件", "")
        output_dir = result.get("输出目录", "")
    else:
        comparison = result.get("三档对比")
        total_records = result.get("总期数", freq_data.get("总期数", 0))
        total_anomalies = result.get("异常总条数", 0)
        by_method = result.get("各方法异常数", {})
        # 后端已生成"代表性明细"（全局 top + 每类保底），此处仅做前端展示量上限
        details = result.get("异常明细", [])[:60]
        chart_files = result.get("图表文件", [])
        csv_file = result.get("导出文件", "")
        output_dir = result.get("输出目录", "")

    update_task(task_id, progress=95, message="生成报告...")
    return {
        "lottery": result.get("lottery_name"),
        "threshold": result.get("阈值等级"),
        "total_records": total_records,
        "total_anomalies": total_anomalies,
        "by_method": by_method,
        "details": details,
        "chart_files": chart_files,
        "csv_file": csv_file,
        "output_dir": output_dir,
        "comparison": comparison,
        "freq_data": freq_data,
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


def start_anomaly(lottery_name: str, params: dict) -> str:
    """启动异常检测任务，返回 task_id"""
    task_id = create_task("anomaly", {"lottery": lottery_name, **params})
    thread = threading.Thread(
        target=run_anomaly_task, args=(task_id, lottery_name, params), daemon=True
    )
    thread.start()
    return task_id


# ============================================================
# 预测异步任务
# ============================================================

@run_async
def run_predict_task(task_id: str, lottery_name: str, params: dict):
    """异步执行预测"""
    from prediction.engine import predict
    from prediction.reporter import save_prediction_record

    update_task(task_id, progress=10, message=f"加载 {lottery_name} 数据...")
    result = predict(
        lottery_name=lottery_name,
        groups=resolve_groups(lottery_name, params.get("groups")),
        mode=params.get("mode", "fresh"),
    )

    update_task(task_id, progress=50, message="生成报告和图表...")
    path = save_prediction_record(result)

    update_task(task_id, progress=95, message="预测完成")
    return {
        "lottery": result["lottery_name"],
        "date": result["预测日期"],
        "mode": result["预测模式"],
        "groups": result["号码组数"],
        "numbers": result["预测号码"],
        "hit_rate": result.get("历史命中率(近50期)", 0),
        "record_dir": str(path),
    }


def start_predict(lottery_name: str, params: dict) -> str:
    """启动预测任务，返回 task_id"""
    task_id = create_task("predict", {"lottery": lottery_name, **params})
    thread = threading.Thread(
        target=run_predict_task, args=(task_id, lottery_name, params), daemon=True
    )
    thread.start()
    return task_id


# ============================================================
# 数据抓取更新异步任务（500.com）
# ============================================================

@run_async
def run_update_task(task_id: str, lottery_name: str, params: dict):
    """异步从 500.com 抓取最新开奖数据并更新本地 CSV"""
    from data.fetcher import update_lottery_data

    update_task(task_id, progress=5, message=f"正在连接 500.com 抓取 {lottery_name} 数据...")
    time.sleep(0.3)

    result = update_lottery_data(lottery_name, clean_after=True)

    if "error" in result:
        raise RuntimeError(result["error"])

    update_task(task_id, progress=95, message="更新完成")

    return {
        "lottery": result.get("lottery", lottery_name),
        "latest_before": result.get("latest_before"),
        "latest_after": result.get("latest_after"),
        "fetched": result.get("fetched", 0),
        "new_records": result.get("new_records", []),
        "message": result.get("message", ""),
        "cleaned": result.get("cleaned", False),
    }


def start_update(lottery_name: str, params: dict = None) -> str:
    """启动数据抓取更新任务，返回 task_id"""
    params = params or {}
    task_id = create_task("update", {"lottery": lottery_name, **params})
    thread = threading.Thread(
        target=run_update_task, args=(task_id, lottery_name, params), daemon=True
    )
    thread.start()
    return task_id


# ============================================================
# 数据补齐异步任务（启动恢复 / 一键补齐）
# ============================================================

@run_async
def run_recover_task(task_id: str, params: dict):
    """异步执行一键补齐：对所有滞后彩种抓取 + 评估 pending"""
    from web.startup_recovery import recover_all_lotteries

    update_task(task_id, progress=10, message="扫描滞后彩种...")
    results = recover_all_lotteries(force=params.get("force", False))

    changed = {k: v for k, v in results.items() if v.get("fetched", 0) > 0}
    update_task(task_id, progress=100, message=f"补齐完成：{len(changed)} 个彩种有更新")

    return {
        "total": len(results),
        "updated": len(changed),
        "results": results,
    }


def start_recover_all(params: dict = None) -> str:
    """启动一键补齐任务，返回 task_id"""
    params = params or {}
    task_id = create_task("recover", params)
    thread = threading.Thread(
        target=run_recover_task, args=(task_id, params), daemon=True
    )
    thread.start()
    return task_id


# ============================================================
# 自动流水线异步任务（抓取→评估→更新权重→预测）
# ============================================================

@run_async
def run_auto_task(task_id: str, lottery_name: str, params: dict):
    """异步执行自动流水线（Web 手动触发 / 调度器 22:05 触发）"""
    return run_auto_pipeline_core(lottery_name, params, task_id=task_id)


def run_auto_pipeline_core(lottery_name: str, params: dict, task_id: Optional[str] = None):
    """自动流水线核心：抓取 → 评估 → 异常检测 → 预测 → 期望 → 写看板简报。

    同一份实现被三条触发路径复用，避免口径漂移：
    - Web 手动 / 调度器 22:05      → run_auto_task（带 task_id，前端可看进度）
    - worker 事件驱动（开奖数据到达）→ worker._run_auto_pipeline（task_id=None）
    - 启动恢复（App 启动补滞后数据后）→ startup_recovery（task_id=None）

    历史教训（2026-09-11 排查）：worker 事件驱动与启动恢复原本只做
    evaluate + robustness，既不生成下一期预测、也不写看板简报 →
    非每日彩种（双色球 / 大乐透 / 七星彩）在 App 关机跨过开奖时段后，
    整期预测丢失，且看板卡片会长期停在上一期（甚至上上期）开奖号。
    """
    from data.fetcher import update_lottery_data
    from data.feedback import evaluate_pending_predictions
    from prediction.engine import predict
    from prediction.reporter import save_prediction_record
    from collections import Counter

    def _upd(progress: int, message: str):
        """task_id 为空时（worker / 启动恢复调用）不写任务表，静默跳过。"""
        if task_id:
            update_task(task_id, progress=progress, message=message)

    steps = []
    predictions = []          # 真实预测号码（供看板展示）
    anomaly_flags = []        # 异常检测告警
    hit_summary = "本期无新增命中反馈"  # 评估命中摘要

    # ① 抓取
    _upd(5, "抓取最新开奖数据...")
    update = update_lottery_data(lottery_name)
    if "error" in update:
        raise RuntimeError(f"抓取失败: {update['error']}")
    steps.append(f"抓取: {update['latest_before']}→{update['latest_after']} (+{update['fetched']})")

    # ①b 销售额+各奖级中奖注数（数字型彩种，任务1.1；失败降级不阻塞主流水线）
    if lottery_name in ("排列5", "福彩3D", "排列3", "七星彩"):
        try:
            from data.fetch_sales import update_sales_data
            u = update_sales_data(lottery_name)
            steps.append(f"销售: {u.get('message', '')} 最新{u.get('latest_after', '-')}")
        except Exception as e:
            steps.append(f"销售更新失败(降级): {e}")

    # ② 评估
    _upd(40, "评估 pending 预测...")
    eval_result = evaluate_pending_predictions(lottery_name)
    weights_str = " ".join(f"{k}:{v:.2f}" for k, v in eval_result['updated_weights'].items())
    steps.append(
        f"评估: {eval_result['evaluated_count']}条, 新反馈{eval_result['new_feedback_count']}, "
        f"权重[{weights_str}]"
    )

    # 命中摘要（本期新增「真预测」反馈的等级分布，马后炮训练样本不算预测命中）
    new_recs_all = eval_result.get('new_feedback_records') or []
    new_recs = [r for r in new_recs_all if r.get('valid_prediction', True)]
    if new_recs:
        grades = Counter(r.get('中奖等级', '未中') for r in new_recs)
        non_zero = {k: v for k, v in grades.items() if k != '未中'}
        hit_summary = "🎉 命中 " + "，".join(f"{k}×{v}" for k, v in non_zero.items()) if non_zero else f"本期新增{len(new_recs)}条预测反馈，均未中奖"
    else:
        hit_summary = "本期无新增预测命中反馈"

    # 结构化命中记录（含真预测/训练回测标识，供前端/推送区分展示）
    from data.push_notify import _fmt_feedback_ticket, _fmt_match
    hit_records = []
    for r in new_recs_all:
        prize = r.get('中奖等级', '未中')
        if prize == '未中':
            continue
        hit_records.append({
            "prize": prize,
            "num": _fmt_feedback_ticket(r),
            "match": _fmt_match(r),
            "count": 1,
            "valid": bool(r.get('valid_prediction', True)),
            "type": r.get('记录类型', '训练' if not r.get('valid_prediction', True) else '预测'),
            "issue": r.get('目标期号') or r.get('期号'),
            "predict_date": r.get('预测日期'),
            "draw_date": r.get('开奖日期'),
        })

    # ③ 预测（除非跳过）
    pred_info = ""
    record_dir = ""
    if not params.get("skip_predict"):
        _upd(70, "生成下一期预测...")
        # 智能跳过：下一期若已有 pending 预测则不重复生成（与 cli auto 口径一致）。
        # 三条触发路径（调度器 22:05 / worker 事件驱动 / 启动恢复）可能几乎同时到达，
        # 没有这道闸门就会为同一目标期号写入多份 pending，污染后续反馈结算。
        from data.feedback import _next_issue, load_pending
        target_issue = _next_issue(lottery_name)
        existing = [p for p in load_pending(lottery_name) if p.get("目标期号") == target_issue]
        if existing:
            predictions = (existing[0].get("预测号码") or [])
            pred_info = f"复用下一期 {target_issue} 已有预测 {len(predictions)}组"
            steps.append(f"跳过预测(下期{target_issue}已预测)")
        else:
            pred = predict(
                lottery_name=lottery_name,
                groups=resolve_groups(lottery_name, params.get("groups")),
                mode=params.get("mode", "fresh"),
            )
            path = save_prediction_record(pred)
            record_dir = str(path)
            predictions = pred.get("预测号码") or []
            pred_info = f"预测: {pred['预测日期']} {pred['号码组数']}组"
            steps.append(f"{pred_info} ({pred['预测模式']})")

    # ③b 前瞻 A/B 对照实验（NetPayout WP3）
    #   先结算已开奖期次（开奖数据刚在①更新），再为下一期记录 A/B 两组号码。
    #   ⚠️ 只积累证据、不参与出号：任何失败一律降级，绝不阻塞主流水线。
    try:
        import config as _cfg
        if getattr(_cfg, "TRIALS_ENABLED", False) and lottery_name in ("双色球", "大乐透"):
            from ev import trials as _T
            line = _T.pipeline_step(lottery_name, n=TRIAL_ARMS)
            if line:
                steps.append(line)
    except Exception as e:
        logger.warning(f"A/B 对照实验失败: {e}")
        steps.append(f"A/B对照失败(降级): {e}")

    # ③.5 异常检测（4 法，无图表）
    try:
        from prediction.anomaly import detect_anomalies
        anom = detect_anomalies(lottery_name, threshold="normal", chart=False)
        cnt = anom.get("异常总条数", 0)
        if cnt > 0:
            for a in anom.get("异常明细", [])[:3]:
                anomaly_flags.append(
                    f"{a.get('类型', '?')}: {a.get('号码类型', '')}{a.get('号码', '')} 偏离(z={a.get('z_score', 0):.1f})"
                )
            steps.append(f"异常检测: 发现{cnt}处异常")
        else:
            steps.append("异常检测: 无异常")
    except Exception as e:
        logger.warning(f"异常检测失败: {e}")
        steps.append(f"异常检测失败: {e}")

    # ④ 当期期望值（期望时机引擎）
    ev_line = ""
    try:
        from data.loader import load_lottery
        from pipeline.step6_ev import current_period_advice
        ev_advice = current_period_advice(lottery_name, load_lottery(lottery_name))
        ev_line = f"期望 {ev_advice.get('单注期望', 0):.3f}"
        ev_line += " 🔥 正期望值得买" if ev_advice.get("是否值得买") else "（常规期）"
        steps.append(ev_line)
    except Exception as e:
        logger.warning(f"期望计算失败: {e}")
        ev_line = f"期望计算失败: {e}"

    _upd(95, "流水线完成")

    # 抓取到的开奖号码（供看板「到底抓到了什么」）
    draw_numbers = {}
    try:
        from data.loader import load_lottery
        _rec = load_lottery(lottery_name).records[0]
        if is_redblue(lottery_name):
            draw_numbers = {
                "期号": _rec.期号,
                "红球": _rec.红球,
                "蓝球": _rec.蓝球,
                "日期": str(_rec.开奖日期) if _rec.开奖日期 else "",
            }
        else:
            draw_numbers = {
                "期号": _rec.期号,
                "号码": getattr(_rec, "zone_numbers", {}) or {},
                "日期": str(_rec.开奖日期) if _rec.开奖日期 else "",
            }
    except Exception:
        draw_numbers = {}

    # 写入自动化状态（含真实产出，供看板「今日简报 / 运行明细」）
    try:
        from data.automation_status import record_run
        record_run(
            lottery_name, True, ev_line or "流水线完成",
            steps=steps, predictions=predictions, draw_numbers=draw_numbers,
            hit_summary=hit_summary, hit_records=hit_records,
            anomaly_flags=anomaly_flags, retrain=None,
        )
    except Exception as e:
        logger.warning(f"记录自动化状态失败: {e}")

    # ⑤ 微信推送（出号 + 中奖记录送上门；2026-09-16）
    #    未启用/无新内容/失败一律静默跳过，绝不影响主流水线。
    try:
        from data.push_notify import push_pipeline_result
        push_pipeline_result(
            lottery_name,
            predictions=predictions,
            eval_result=eval_result,
            hit_summary=hit_summary,
            hit_records=hit_records,
            draw_numbers=draw_numbers,
            pred_info=pred_info,
            fresh_prediction=bool(record_dir),
        )
    except Exception as e:
        logger.warning(f"微信推送失败(降级): {e}")

    return {
        "lottery": lottery_name,
        "steps": steps,
        "fetched": update.get("fetched", 0),
        "latest_after": update.get("latest_after"),
        "evaluated": eval_result.get("evaluated_count", 0),
        "new_feedback": eval_result.get("new_feedback_count", 0),
        "weights": eval_result.get("updated_weights", {}),
        "prediction": pred_info,
        "record_dir": record_dir,
        "ev_advice": ev_line,
    }


def start_auto_pipeline(lottery_name: str, params: dict) -> str:
    """启动自动流水线任务，返回 task_id"""
    task_id = create_task("auto", {"lottery": lottery_name, **params})
    thread = threading.Thread(
        target=run_auto_task, args=(task_id, lottery_name, params), daemon=True
    )
    thread.start()
    return task_id


# 单个流水线里可选的 mode 集合（与 dashboard 下拉保持一致）
AUTO_PIPELINE_MODES = ("fresh", "rule", "trained")


def _expand_lotteries(value) -> list:
    """把前端传来的彩种参数展开成彩种列表：
    - None / "__all__" -> LOTTERY_CONFIG 全部彩种
    - 字符串 / 单元素列表 -> 自身
    其他按原样返回（已是非空列表时）
    """
    if value is None or value == "__all__":
        return list(LOTTERY_CONFIG.keys())
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value:
        return value
    return list(LOTTERY_CONFIG.keys())


def _expand_modes(value, fallback: str) -> list:
    """把前端传来的 mode 参数展开为列表：
    - None / "__all__" -> AUTO_PIPELINE_MODES
    - 字符串 / 单元素列表 -> 自身
    - 其他 -> 退化到 [fallback]
    """
    if value is None or value == "__all__":
        return list(AUTO_PIPELINE_MODES)
    if isinstance(value, str):
        return [value]
    if isinstance(value, list) and value:
        return value
    return [fallback]


def start_auto_batch(lotteries, modes, params: dict) -> str:
    """启动一批自动流水线任务（多彩种 × 多模式串行执行），返回 1 个 batch task_id。

    每个 (lottery, mode) 子任务依次复用 run_auto_task，result 累积在
    task.result["sub_results"] 列表里，前端按子结果轮询展示。
    """
    lotteries = _expand_lotteries(lotteries)
    modes = _expand_modes(modes, params.get("mode", "fresh"))
    batch_params = {
        "lotteries": lotteries,
        "modes": modes,
        # None → 由 run_auto_pipeline_core 按每个彩种 resolve_groups() 解析
        "groups": params.get("groups"),
        "skip_predict": params.get("skip_predict", False),
    }
    task_id = create_task("auto_batch", batch_params)
    thread = threading.Thread(
        target=run_auto_batch_task,
        args=(task_id, lotteries, modes, dict(params)),
        daemon=True,
    )
    thread.start()
    return task_id


def run_auto_batch_task(task_id: str, lotteries: list, modes: list, params: dict):
    """串行跑 (lottery, mode) 组合的自动流水线。子任务若失败，记录后继续。"""
    total = max(1, len(lotteries) * len(modes))
    sub_results: list = []
    completed = 0
    update_task(
        task_id, status="running", progress=0,
        message=f"批流水线: 0/{total} (彩种 {len(lotteries)} × 模式 {len(modes)})",
    )

    for lot in lotteries:
        for mode in modes:
            sub_params = dict(params)
            sub_params["mode"] = mode
            label = f"[{completed + 1}/{total}] {lot} / {mode}"
            update_task(
                task_id, progress=int(completed * 100 / total),
                message=f"{label} — 执行中...",
            )
            sub = {
                "lottery": lot, "mode": mode,
                "status": "running", "steps": [], "error": None,
            }
            sub_results.append(sub)
            # 给前端一个中间态可见
            update_task(task_id, result={"sub_results": sub_results,
                                          "total": total, "done": completed})

            sub_tid = create_task(
                "auto", {"lottery": lot, "mode": mode,
                         "groups": params.get("groups"),
                         "skip_predict": params.get("skip_predict", False),
                         "parent_batch": task_id},
            )
            # 同步执行子任务（run_auto_task 自身会更新子任务进度，
            # 我们这里串行是为了避免对同彩种并发抓取/写文件时数据竞争）
            try:
                run_auto_task(sub_tid, lot, sub_params)
                sub_t = get_task(sub_tid) or {}
                sub["status"] = "done" if sub_t.get("status") == "done" else sub_t.get("status", "done")
                sub["steps"] = (sub_t.get("result") or {}).get("steps", []) if sub_t.get("result") else []
                if sub_t.get("status") == "done":
                    r = sub_t.get("result") or {}
                    sub["fetched"] = r.get("fetched", 0)
                    sub["latest_after"] = r.get("latest_after")
                    sub["evaluated"] = r.get("evaluated", 0)
                    sub["new_feedback"] = r.get("new_feedback", 0)
                    sub["prediction"] = r.get("prediction", "")
                    sub["ev_advice"] = r.get("ev_advice", "")
                else:
                    sub["error"] = sub_t.get("error") or "子任务未完成"
            except Exception as e:
                logger.exception(f"批任务子流水线失败: {lot}/{mode}: {e}")
                sub["status"] = "error"
                sub["error"] = str(e)

            completed += 1
            update_task(
                task_id, progress=int(completed * 100 / total),
                message=f"{label} — 完成 ({sub['status']})",
                result={"sub_results": sub_results, "total": total, "done": completed},
            )

    update_task(
        task_id, status="done", progress=100,
        message=f"批流水线完成: {completed}/{total}",
        result={"sub_results": sub_results, "total": total, "done": completed,
                "lotteries": lotteries, "modes": modes},
    )


def build_pending_with_weights(lottery_name: str) -> dict:
    """加载待开奖预测并附策略权重/推荐分/推荐使用标记。

    返回 {total, strategy_weights, records}，records 内每组带 策略权重/推荐分/recommended。
    供 feedback 页与 EV 页复用，避免逻辑漂移。
    """
    from data.feedback import load_pending
    from prediction.optimizer import get_strategy_weights

    pending = load_pending(lottery_name)
    weights = get_strategy_weights(lottery_name)
    if not weights:
        weights = {"高频策略": 0.25, "遗漏值策略": 0.25, "区间均衡策略": 0.25, "ML策略": 0.25}

    enriched = []
    for rec in pending:
        rec = dict(rec)  # 浅拷贝，避免污染原始存储
        groups = rec.get("预测号码", [])
        scored = []
        for g in groups:
            strat = g.get("策略", "")
            w = weights.get(strat, 0.25)
            conf = g.get("置信度", 0.0) or 0.0
            rec_score = round(w * conf, 4)
            g = dict(g)
            g["策略权重"] = round(w, 4)
            g["推荐分"] = rec_score
            scored.append((rec_score, g))
        # Top1（推荐分最高）标记 recommended
        if scored:
            scored.sort(key=lambda x: x[0], reverse=True)
            best = scored[0][0]
            # 同分并列也标 recommended
            for _, g in scored:
                g["recommended"] = (g["推荐分"] == best)
        rec["预测号码"] = [g for _, g in scored]
        enriched.append(rec)

    return {"total": len(enriched), "strategy_weights": weights, "records": enriched}


def get_training_honesty(lottery_name: str) -> dict:
    """取最近一次训练「选号命中 vs 随机基线」诚实指标，供 EV 页展示。

    返回 {has_training, model_type, best_window, selection_total_hits, selection_eval, note}。
    """
    from prediction.engine import _get_trained_params

    params = _get_trained_params(lottery_name)
    if not params or not params.get("model_params"):
        return {"has_training": False, "note": "暂无训练记录，请先运行 train"}
    mp = params["model_params"]
    sev = mp.get("selection_eval") or {}
    return {
        "has_training": True,
        "model_type": mp.get("model_type"),
        "best_window": mp.get("best_window"),
        "selection_total_hits": mp.get("selection_total_hits"),
        "selection_eval": sev,
        "note": sev.get("significance_note", ""),
    }

