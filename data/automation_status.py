"""
自动化流水线运行状态追踪

记录每次 auto 流水线的运行结果（成功/失败、时间、摘要），供 Web 看板展示。
这样流水线跑挂了用户打开看板就能看到，而不是翻日志。

数据文件：logs/automation/status.json
结构：
{
  "last_run": {           # 最近一次运行（全局最近，可能是任一彩种）
    "time": "2026-08-07T21:30:05",
    "lottery": "双色球",
    "success": true/false,
    "message": "摘要",
    "steps": ["..."],
    "duration_sec": 12.3,
    "predictions": [ {"红球":[...],"蓝球":[...],"策略":"...","置信度":0.3} ],  # 本次生成的预测号码
    "draw_numbers": {"期号":26090,"红球":[...],"蓝球":[...],"日期":"2026-08-06"},  # 抓取到的开奖号
    "hit_summary": "🎉 命中1注二等",   # 本期命中摘要
    "anomaly_flags": ["卡方: 红球23 显著偏离"],  # 异常检测告警
    "retrain": {"model":"statistical","window":50,"hit_rate":0.18}  # 模型重训结果(可空)
  },
  "history": [ ... ],     # 最近 MAX_HISTORY 次运行记录（旋转窗口）
  "last_run_per_lottery": {   # 持久彩种级最近一次（**不被历史窗口淘汰**，
                              # 看板简报用这个，否则大乐透/双色球等周几彩种会被
                              # 七星彩/数字型的高频流水轮转出窗口 → 误报"暂无记录"）
    "双色球": { ... },
    "大乐透": { ... },
    ...
  },
  "failed_count": 1,      # 最近 MAX_HISTORY 中失败次数
  "last_success": "...",  # 最近一次成功时间
  "last_failed": "..."    # 最近一次失败时间
}
"""

import json
import logging
import os
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from config import BASE_DIR

logger = logging.getLogger(__name__)

STATUS_FILE = BASE_DIR / "logs" / "automation" / "status.json"
MAX_HISTORY = 50      # 旋转历史窗口（30→50：保证周几彩种至少一次落窗内，
                       # 即便数字型每天 3-4 条也至少保 ~12 天回看）
_MAIN_LOTTERIES = ("双色球", "大乐透")   # 看板简报主彩种（与前端 mainLotteries 对齐）

# 保护 status.json 的并发读写：web 调度器可能在同一时刻触发多个彩种流水线，
# 多个线程并发写同一个文件会导致 JSON 损坏（出现非法 utf-8 字节）。
_status_lock = threading.Lock()


def _load() -> Dict:
    """读取当前状态（无文件或文件损坏时返回默认结构）

    兼容旧文件：若 ``last_run_per_lottery`` 字段不存在则从历史回填，确保看板
    升级前已有的运行记录也能展示给用户。
    """
    if STATUS_FILE.exists():
        try:
            with open(STATUS_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, UnicodeDecodeError, OSError) as e:
            # 文件损坏时备份，避免丢失历史；然后返回默认结构让页面能正常加载
            try:
                backup = STATUS_FILE.with_suffix(f".json.bak.{datetime.now().strftime('%Y%m%d%H%M%S')}")
                shutil.copy2(STATUS_FILE, backup)
                logger.warning(f"自动化状态文件损坏，已备份到 {backup}，错误: {e}")
            except Exception as be:
                logger.warning(f"备份损坏的自动化状态文件失败: {be}")
        else:
            # 旧文件结构无 last_run_per_lottery：回填一次（按彩种取最近一条）
            if "last_run_per_lottery" not in state:
                rebuilt = {}
                entries = []
                if state.get("last_run"):
                    entries.append(state["last_run"])
                entries.extend(state.get("history", []))
                for e in entries:
                    lot = e.get("lottery")
                    if not lot:
                        continue
                    if lot not in rebuilt or e.get("time", "") > rebuilt[lot].get("time", ""):
                        rebuilt[lot] = e
                state["last_run_per_lottery"] = rebuilt
                # 立即回写，让后续读路径直接命中结构化字段
                try:
                    with open(STATUS_FILE, "w", encoding="utf-8") as f:
                        json.dump(state, f, ensure_ascii=False, indent=2)
                except OSError as ee:
                    logger.warning(f"回填 last_run_per_lottery 失败: {ee}")
            return state
    return {"last_run": None, "history": [], "failed_count": 0,
            "last_success": None, "last_failed": None,
            "last_run_per_lottery": {}}


def record_run(
    lottery: str,
    success: bool,
    message: str,
    steps: Optional[List[str]] = None,
    duration_sec: Optional[float] = None,
    predictions: Optional[List[dict]] = None,
    draw_numbers: Optional[dict] = None,
    hit_summary: Optional[str] = None,
    hit_records: Optional[List[dict]] = None,
    anomaly_flags: Optional[List[str]] = None,
    retrain: Optional[dict] = None,
):
    """记录一次流水线运行结果

    新增结构化字段（供看板展示「到底跑了什么」）：
    - predictions: 本次生成的预测号码组 [{红球,蓝球,策略,置信度}, ...]
    - draw_numbers: 本次抓取到的开奖号码 {期号,红球,蓝球,日期}
    - hit_summary: 本期命中摘要文案（如「🎉 命中1注二等」）
    - anomaly_flags: 异常检测告警列表（如 ["卡方: 红球23 显著偏离"]）
    - retrain: 模型重训结果 {model, window, hit_rate} 或 None
    """
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)

    # 整个「读-改-写」加锁，防止 web 多线程并发操作状态导致丢失更新或文件损坏
    with _status_lock:
        state = _load()

        now = datetime.now().isoformat(timespec="seconds")
        entry = {
            "time": now,
            "lottery": lottery,
            "success": bool(success),
            "message": message,
            "steps": steps or [],
            "duration_sec": round(duration_sec, 1) if duration_sec else None,
            "predictions": predictions or [],
            "draw_numbers": draw_numbers or {},
            "hit_summary": hit_summary or "",
            "hit_records": hit_records or [],
            "anomaly_flags": anomaly_flags or [],
            "retrain": retrain or None,
        }

        state["last_run"] = entry
        history = state.get("history", [])
        history.insert(0, entry)
        state["history"] = history[:MAX_HISTORY]

        # 持久彩种级最近一次：不受 MAX_HISTORY 旋转淘汰，看板简报永远展示最近值
        per_lot = state.setdefault("last_run_per_lottery", {})
        cur = per_lot.get(lottery)
        if cur is None or entry.get("time", "") >= cur.get("time", ""):
            per_lot[lottery] = entry

        # 汇总统计
        recent = state["history"]
        state["failed_count"] = sum(1 for h in recent if not h["success"])
        successes = [h["time"] for h in recent if h["success"]]
        failures = [h["time"] for h in recent if not h["success"]]
        state["last_success"] = successes[0] if successes else None
        state["last_failed"] = failures[0] if failures else None

        try:
            # 原子写：先写入临时文件，再替换目标文件，避免并发/异常导致原文件半写损坏
            tmp_file = STATUS_FILE.with_suffix(".json.tmp")
            with open(tmp_file, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            try:
                os.replace(tmp_file, STATUS_FILE)
            except OSError:
                # Windows 下若目标文件被其他进程占用，os.replace 会失败；
                # 降级为直接覆盖，仍受 _status_lock 保护同进程并发。
                shutil.copy2(tmp_file, STATUS_FILE)
                try:
                    os.remove(tmp_file)
                except OSError:
                    pass
        except OSError as e:
            logger.warning(f"写入自动化状态失败: {e}")


def get_status() -> Dict:
    """读取当前状态（Web 用）

    额外返回 ``last_run_by_lottery``：合并持久彩种级最近一次（不受历史窗口淘汰）
    + history 中的同彩种更新条目。供前端同时展示双色球、大乐透的简报。
    """
    state = _load()
    # 兼容旧数据
    state.setdefault("failed_count", 0)
    state.setdefault("last_success", None)
    state.setdefault("last_failed", None)
    state.setdefault("last_run_per_lottery", {})

    # 优先取持久彩种级映射（不受 MAX_HISTORY 旋转影响），再用 history 覆盖更新条目
    by_lottery: Dict[str, dict] = dict(state.get("last_run_per_lottery", {}))
    for entry in state.get("history", []):
        lottery = entry.get("lottery")
        if not lottery:
            continue
        cur = by_lottery.get(lottery)
        if cur is None or entry.get("time", "") > cur.get("time", ""):
            by_lottery[lottery] = entry
    # 双/大主彩种若仍无记录，从 state["last_run"] 兼容一次（最后兜底）
    if state.get("last_run"):
        lr_lot = state["last_run"].get("lottery")
        if lr_lot and lr_lot in _MAIN_LOTTERIES and lr_lot not in by_lottery:
            by_lottery[lr_lot] = state["last_run"]
    state["last_run_by_lottery"] = by_lottery
    return state
