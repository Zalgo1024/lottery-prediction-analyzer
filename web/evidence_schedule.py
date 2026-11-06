# -*- coding: utf-8 -*-
"""方案 B 开奖证据的**采集频率**调度（21:05–21:45，每 2 分钟一次）。

为什么放在 Flask 内置调度器里
------------------------------
仓库的自动流水线早已从「Windows 任务计划程序 + 子进程」收敛为 Flask 进程内的
后台线程（`web/scheduler.py`）。本模块沿用同一条路：不新增系统计划任务、
不改注册表、不引入常驻守护进程，服务在跑就会按窗口采。

为什么是 21:05–21:45 / 2 分钟
------------------------------
声明值（福彩 21:15、体彩 21:25、快乐8 21:30）是开奖事件的**下界**；
证据里的「首次可观测时刻」是**上界**，只用来否定下界。上界离真实开奖越远，
交叉校验越迟钝（隔几小时采只能得到「不矛盾」）。窗口贴着三类开奖时刻前后各留
余量，2 分钟一次才能把「结果首次可见」的观测误差压到分钟级。

★ 采集本身**只读数据源、只追加证据文件**；不改 CSV / pending / 台账 / 预算。
★ 任何采集失败都只记日志 + 返回状态，绝不抛到调度循环、更不影响出号。
★ 频率与窗口是本模块的单一真源；`scripts/collect_evidence_window.py` 与
  `web/scheduler.py` 都只调用这里的 `in_window()` / `is_due()` / `run_once()`。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from config import BASE_DIR
from logs.logger import setup_logger

logger = setup_logger("evidence_schedule")

# ---- 单一真源：窗口、节拍、超时 ----
WINDOW_START_MIN = 21 * 60 + 5        # 21:05（含）
WINDOW_END_MIN = 21 * 60 + 45         # 21:45（含）
INTERVAL_SECONDS = 120                # 每 2 分钟
DUE_TOLERANCE = 10                   # tick 是 60s 步进，容忍相位漂移
POLL_SECONDS = 15                    # 独立窗口脚本的轮询间隔
SUBPROCESS_TIMEOUT = 110             # 必须小于节拍，否则两轮会重叠
LOCK_STALE_SECONDS = 300             # 崩溃残留的锁自动失效

# pytest 期间由 conftest 关掉实时采集（避免测试进程真的去抓外网）
ENABLED = True

STATE_KEY = "_evidence"
COLLECTOR = BASE_DIR / "scripts" / "collect_draw_evidence.py"
LOG_PATH = BASE_DIR / "logs" / "evidence_collect.log"
STATE_PATH = BASE_DIR / "logs" / "evidence_schedule_state.json"
LOCK_PATH = BASE_DIR / "logs" / ".evidence_collect.lock"
PYTHON = sys.executable

NO_EVIDENCE_NOTE = (
    "本次窗口内没有任何证据行参与交叉校验；这只说明「未发现矛盾」，"
    "不等于声明时刻已被验证正确。"
)


# ---------------------------------------------------------------- 纯函数（可注入时刻）
def minute_of_day(now: datetime) -> int:
    """只取时分，忽略秒与时区标记（窗口是本地墙钟口径）。"""
    return now.hour * 60 + now.minute


def in_window(now: datetime) -> bool:
    return WINDOW_START_MIN <= minute_of_day(now) <= WINDOW_END_MIN


def is_due(now: datetime, last_run: Any,
           interval: int = INTERVAL_SECONDS) -> bool:
    """距上次成功启动采集 ≥ 节拍（减容忍）就该再跑一次。

    `last_run` 缺失 / 不可解析 / 来自更早的日期 ⇒ 一律判该跑（不 fail-close，
    否则一次脏状态会让整晚静默停采）。
    """
    if last_run in (None, ""):
        return True
    try:
        last = datetime.fromisoformat(str(last_run))
        if last.tzinfo is not None and now.tzinfo is None:
            last = last.replace(tzinfo=None)
        elif last.tzinfo is None and now.tzinfo is not None:
            now = now.replace(tzinfo=None)
        gap = (now - last).total_seconds()
    except (ValueError, TypeError):
        return True
    return gap >= max(0, interval - DUE_TOLERANCE)


def next_due_minute(now: datetime) -> Optional[int]:
    """给界面/日志看的「本次窗口内下一次应采时刻」（分钟数），窗口外返回 None。"""
    if not in_window(now):
        return None
    m = minute_of_day(now)
    step = INTERVAL_SECONDS // 60
    nxt = WINDOW_START_MIN + ((m - WINDOW_START_MIN + step - 1) // step) * step
    return nxt if nxt <= WINDOW_END_MIN else None


# ---------------------------------------------------------------- 状态与日志
def load_state(*, path: Optional[Path] = None) -> Dict[str, Any]:
    p = Path(path) if path is not None else STATE_PATH
    if not p.exists():
        return {}
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_state(doc: Dict[str, Any], *, path: Optional[Path] = None) -> None:
    p = Path(path) if path is not None else STATE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(str(tmp), str(p))


def _append_log(line: str, *, path: Optional[Path] = None) -> None:
    p = Path(path) if path is not None else LOG_PATH
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(line.rstrip() + "\n")
    except OSError as exc:
        logger.warning(f"证据采集日志写入失败：{exc}")


# ---------------------------------------------------------------- 单实例锁
def _lock_payload(now: datetime, held: bool) -> str:
    return json.dumps({"持有": held, "时间": now.isoformat(timespec="seconds"),
                       "pid": os.getpid()}, ensure_ascii=False)


def _acquire_lock(*, path: Optional[Path] = None,
                  now: Optional[datetime] = None) -> bool:
    """单实例锁：用**覆写标记**表达持有/释放，不删文件。

    ★ 为什么不 `unlink()`：本机 WorkBuddy 的安全删除守卫会拦截 `Path.unlink()`，
      在带活动异常的 `finally` 里直接抛 `SystemExit` —— 锁释放反而把调度线程
      打断（实测 2026-11-06）。锁文件是个常驻标记，释放只把 `持有` 置 false。
    ★ 残留超过 LOCK_STALE_SECONDS 的锁按崩溃处理，允许下一轮接管（不 fail-close）。
    """
    p = Path(path) if path is not None else LOCK_PATH
    now = now or datetime.now()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        if not p.exists():
            fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, _lock_payload(now, True).encode("utf-8"))
                return True
            finally:
                os.close(fd)
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            d = {}
        if d.get("持有"):
            try:
                age = now.timestamp() - datetime.fromisoformat(
                    str(d.get("时间"))).timestamp()
            except (ValueError, TypeError):
                age = now.timestamp() - p.stat().st_mtime
            if -LOCK_STALE_SECONDS <= age < LOCK_STALE_SECONDS:
                return False
        p.write_text(_lock_payload(now, True), encoding="utf-8")
        return True
    except OSError as exc:
        logger.warning(f"证据采集锁建立失败（按未取得处理）：{exc}")
        return False


def _release_lock(*, path: Optional[Path] = None) -> None:
    p = Path(path) if path is not None else LOCK_PATH
    try:
        p.write_text(_lock_payload(datetime.now(), False), encoding="utf-8")
    except OSError as exc:
        logger.warning(f"证据采集锁释放失败（下一轮靠 TTL 接管）：{exc}")


# ---------------------------------------------------------------- 单次采集
def run_once(*, now: Optional[datetime] = None,
             python: Optional[str] = None,
             collector: Optional[Path] = None,
             state_path: Optional[Path] = None,
             log_path: Optional[Path] = None,
             lock_path: Optional[Path] = None,
             timeout: int = SUBPROCESS_TIMEOUT) -> Dict[str, Any]:
    """跑一次 `--all` 采集。返回结构化状态，任何失败都不抛到调用方。

    用子进程而不是在 Flask 进程里 import 采集器：网络抓取会阻塞、可能读环境变量，
    隔离后失败不会影响 Web 主线程；同时保证与命令行手工跑的是同一条实现。
    """
    now = now or datetime.now()
    if not _acquire_lock(path=lock_path, now=now):
        return {"状态": "跳过", "原因": "已有采集在跑（单实例锁）",
                "时间": now.isoformat(timespec="seconds")}
    started = time.monotonic()
    result: Dict[str, Any] = {
        "时间": now.isoformat(timespec="seconds"),
        "状态": "失败", "返回码": None, "耗时": None, "原因": "",
    }
    try:
        py = python or PYTHON
        script = Path(collector) if collector is not None else COLLECTOR
        proc = subprocess.run(
            [py, str(script), "--all"],
            cwd=str(BASE_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
        result["返回码"] = proc.returncode
        result["状态"] = "成功" if proc.returncode == 0 else "部分失败"
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        result["汇总"] = out.splitlines()[-1] if out else ""
        if proc.returncode != 0:
            result["原因"] = "采集脚本返回非零（有彩种未取到证据，见汇总）"
            _append_log(f"[{result['时间']}] 部分失败 rc={proc.returncode}\n{out}\n{err}",
                        path=log_path)
        else:
            _append_log(f"[{result['时间']}] 成功 {result['汇总']}", path=log_path)
    except subprocess.TimeoutExpired:
        result["状态"] = "失败"
        result["原因"] = f"采集超时（>{timeout}s），已终止；本轮证据不完整"
        _append_log(f"[{result['时间']}] 超时 {result['原因']}", path=log_path)
        logger.warning(f"证据采集超时：{result['原因']}")
    except Exception as exc:                                  # noqa: BLE001
        result["状态"] = "失败"
        result["原因"] = f"{type(exc).__name__}: {exc}"
        _append_log(f"[{result['时间']}] 异常 {result['原因']}", path=log_path)
        logger.warning(f"证据采集异常（不影响出号）：{result['原因']}")
    finally:
        result["耗时"] = round(time.monotonic() - started, 2)
        _release_lock(path=lock_path)

    try:
        st = load_state(path=state_path)
        st.update({
            "last_run": result["时间"], "last_status": result["状态"],
            "last_returncode": result["返回码"],
            "last_elapsed": result["耗时"],
            "last_error": result["原因"] or None,
            "last_summary": result.get("汇总") or None,
        })
        if st.get("last_date") != now.date().isoformat():
            st["last_date"] = now.date().isoformat()
            st["runs_today"] = 0
        if result["状态"] != "跳过":
            st["runs_today"] = int(st.get("runs_today") or 0) + 1
        _save_state(st, path=state_path)
    except Exception as exc:                                  # noqa: BLE001
        logger.warning(f"证据采集状态保存失败：{exc}")
    return result


# ---------------------------------------------------------------- 独立窗口循环
def run_window(*, now_fn=datetime.now, sleep_fn=time.sleep,
               max_runs: int = 40, **kwargs) -> Dict[str, Any]:
    """服务未启动时的一键窗口循环；跑满窗口或达到 max_runs 即退出。

    `now_fn` / `sleep_fn` 可注入，便于纯测试。返回逐轮结果汇总。
    """
    runs: list = []
    while len(runs) < max_runs:
        now = now_fn()
        if not in_window(now):
            break
        st = load_state(path=kwargs.get("state_path"))
        if is_due(now, st.get("last_run")):
            runs.append(run_once(now=now, **kwargs))
        sleep_fn(POLL_SECONDS)
    return {"轮数": len(runs), "结果": runs,
            "退出原因": "窗口结束" if len(runs) < max_runs else "达到轮数上限"}


def status(*, now: Optional[datetime] = None,
           state_path: Optional[Path] = None) -> Dict[str, Any]:
    """给日志 / 排障看的状态：窗口、节拍、上次运行、今日轮数、下次应采。"""
    now = now or datetime.now()
    st = load_state(path=state_path)
    nxt = next_due_minute(now)
    return {
        "启用": ENABLED, "窗口": "21:05-21:45", "节拍秒": INTERVAL_SECONDS,
        "当前在窗口内": in_window(now),
        "本次应采": (f"{nxt // 60:02d}:{nxt % 60:02d}" if nxt is not None else None),
        "该跑": in_window(now) and is_due(now, st.get("last_run")),
        "last_run": st.get("last_run"), "last_status": st.get("last_status"),
        "last_error": st.get("last_error"),
        "runs_today": st.get("runs_today") or 0,
    }
