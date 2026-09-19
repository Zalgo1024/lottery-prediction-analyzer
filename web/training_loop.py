# -*- coding: utf-8 -*-
"""持续训练循环（用户需求：六个彩种"自己训练、每时每刻都在训"的落地形态）。

两层节奏（数据驱动的合理化）：
- **空闲持续循环**：每隔 `interval_seconds` 秒给一个彩种跑一轮随机锚点走前验证
  （statistical，秒级）；开奖数据没变时换随机锚点=新考题，仍有增量。
- **夜间重训**：每天 `nightly_hour` 点整，对全部彩种跑一次完整 lightgbm 训练
  （走前交叉验证，产物进 training/，与手动 `cli.py train` 同一条管线），
  完成后推送一条汇总。

诚实设计：
- 全部试验**全量落账**（`train/anchor_trainer.py` 的 JSONL），不挑好的不丢差的；
- 显著性只标「本次抽样」，最终以裁判层为准；推送里带诚实声明；
- 循环是 daemon 线程 + 单飞锁，任何异常只告警，绝不影响出号主流水线。
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

STORE_PATH = Path(__file__).resolve().parent.parent / "config" / "training_loop.json"

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "interval_seconds": 60,        # 空闲循环周期
    "trials_per_cycle": 1,         # 每周期锚点试验数
    "window_size": 50,
    "min_history": 100,
    "nightly_lightgbm": True,      # 夜间完整 lightgbm 重训
    "nightly_hour": 3,             # 夜训触发时刻（凌晨，避开出号/推送高峰）
    "push_summary": True,          # 夜训完成推一条汇总
}

_lock = threading.Lock()
_nightly_done_date: Optional[str] = None
_last_cycle: Dict[str, Any] = {}
_running_since: Optional[str] = None
_thread = None


def sanitize(data: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    data = data or {}
    if isinstance(data.get("enabled"), bool):
        cfg["enabled"] = data["enabled"]
    for k in ("interval_seconds", "trials_per_cycle", "window_size", "min_history", "nightly_hour"):
        if k in data:
            try:
                cfg[k] = max(1, int(data[k]))
            except (TypeError, ValueError):
                pass
    if "interval_seconds" in cfg:
        cfg["interval_seconds"] = min(cfg["interval_seconds"], 3600)
    for k in ("nightly_lightgbm", "push_summary"):
        if isinstance(data.get(k), bool):
            cfg[k] = data[k]
    return cfg


def load_config() -> Dict[str, Any]:
    try:
        return json.loads(STORE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    cfg = sanitize(cfg)
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STORE_PATH)
    return cfg


def _lotteries():
    from config import LOTTERY_CONFIG
    return list(LOTTERY_CONFIG.keys())


def run_cycle(cfg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """空闲循环的一个周期：轮询一个彩种跑一轮随机锚点验证。"""
    from train.anchor_trainer import anchor_train
    idx = (_last_cycle.get("_idx", 0) + 1) % len(_lotteries())
    _last_cycle["_idx"] = idx
    lot = _lotteries()[idx]
    res = anchor_train(
        lot,
        n_trials=int(cfg.get("trials_per_cycle", 1)),
        window_size=int(cfg.get("window_size", 50)),
        min_history=int(cfg.get("min_history", 100)),
    )
    if "error" in res:
        logger.warning(f"锚点循环 {lot}: {res['error']}")
        return None
    _last_cycle.update({"ts": res["training_time"], "lottery": lot,
                        "seed": res["seed"], "n_trials": res["n_trials"]})
    top = next(iter(res["stats"].items()))
    logger.info(f"锚点循环 {lot}: {res['n_trials']} 次试验 | {top[0]} 差值={top[1]['配对差值']} ({top[1]['显著性']})")
    return res


def run_nightly(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """夜训：全部彩种完整 lightgbm 训练（走前 CV，产物与手动训练同一管线）。"""
    from train.engine import train
    summary, errors = [], []
    for lot in _lotteries():
        try:
            t0 = time.time()
            r = train(lot, model_type="lightgbm", window=int(cfg.get("window_size", 50)))
            summary.append(f"- **{lot}**：完成，最优窗口 {r.get('best_window')}（loss {r.get('best_loss')}）"
                           f" -> {Path(str(r.get('record_dir', ''))).name}")
            logger.info(f"夜训 {lot} 完成 ({time.time()-t0:.0f}s) -> {r.get('record_dir', '')}")
        except Exception as e:
            errors.append(lot)
            summary.append(f"- **{lot}**：训练失败 {str(e)[:80]}")
            logger.warning(f"夜训 {lot} 失败: {e}")
    body = "**持续训练｜夜训汇总（lightgbm）**\n" + "\n".join(summary) + \
        (f"\n\n失败 {len(errors)} 个" if errors else "") + \
        "\n\n> 仅供研究记录；走前交叉验证口径，不构成任何中奖率提升的证据。"
    if cfg.get("push_summary"):
        try:
            from data.push_notify import send, effective_ready, sanitize as pn_sanitize, load_config as pn_load
            c = pn_sanitize(pn_load())
            if effective_ready(c):
                send("彩票助手｜持续训练", body, c)
        except Exception as e:
            logger.warning(f"夜训推送失败（不影响训练）: {e}")
    return {"summary": summary, "errors": errors}


def _loop():
    global _nightly_done_date
    while True:
        cfg = sanitize(load_config())
        if not cfg.get("enabled"):
            time.sleep(30)
            continue
        try:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            nightly_due = (cfg.get("nightly_lightgbm") and now.hour == int(cfg.get("nightly_hour", 3))
                           and _nightly_done_date != today)
            if nightly_due:
                if _lock.acquire(blocking=False):
                    try:
                        _nightly_done_date = today
                        run_nightly(cfg)
                    finally:
                        _lock.release()
            else:
                if _lock.acquire(blocking=False):   # 夜训进行中则跳过本周期
                    try:
                        run_cycle(cfg)
                    finally:
                        _lock.release()
        except Exception as e:
            logger.warning(f"持续训练循环异常（继续运行）: {e}")
        time.sleep(int(cfg.get("interval_seconds", 60)))


def start_training_loop(daemon: bool = True) -> bool:
    """启动持续训练线程（幂等）；返回是否真正启动。"""
    global _thread, _running_since
    if _thread is not None and _thread.is_alive():
        return False
    _thread = threading.Thread(target=_loop, daemon=daemon, name="training-loop")
    _thread.start()
    _running_since = datetime.now().isoformat(timespec="seconds")
    logger.info("持续训练循环已启动（空闲锚点循环 + 夜间 lightgbm 重训）")
    return True


def get_status() -> Dict[str, Any]:
    return {
        "running": bool(_thread is not None and _thread.is_alive()),
        "started_at": _running_since,
        "config": sanitize(load_config()),
        "last_cycle": {k: v for k, v in _last_cycle.items() if not k.startswith("_")},
        "nightly_done_date": _nightly_done_date,
    }
