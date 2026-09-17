"""
持续计算核心（L4 第2期）——三种形态共用同一个 ContinuousWorker：

  形态 A（常驻轮询）: 嵌入 Flask 进程，app.py 启动时 start_worker(daemon=True)
  形态 B（任务队列）: 消费 tasks.db 中的 pending 任务（robustness_*/predict_tiered/evaluate）
  形态 C（独立服务）: `python -m web.worker` 独立进程，与 A 共用本模块

职责（与自动流水线互补，不重复跑 auto 流水线——那是 scheduler.py 的事）：
  1. 消费队列：串行认领并执行 worker 类型任务（避免 CSV 并发写，延续锁纪律）
  2. 事件驱动：轮询各彩种最新期号，开奖数据更新 → 入队 评估 + 鲁棒性计算
     （数字型=light 高频轻量；乐透型=full 每期重算），状态文件去重防重复入队
  3. 心跳：config/worker_state.json（看板可读）；任务级心跳写回 tasks.db
  4. 孤儿回收：启动时 + 周期性，把心跳超时的 running 任务标为 failed
"""

import argparse
import json
import os
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, Optional

from config import BASE_DIR, LOTTERY_CONFIG, resolve_groups
from logs.logger import setup_logger

logger = setup_logger("worker")

WORKER_STATE_FILE = BASE_DIR / "config" / "worker_state.json"
EVENT_STATE_FILE = BASE_DIR / "config" / "worker_event_state.json"

# 本 worker 认领的任务类型（旧版 auto/auto_batch 等仍由线程池直跑，不归 worker 管）
WORKER_TASK_TYPES = ("robustness_full", "robustness_light", "predict_tiered",
                     "evaluate", "auto_pipeline")

# 心跳/孤儿回收参数
TASK_HEARTBEAT_SECONDS = 30      # 执行中任务心跳续期间隔
ORPHAN_STALE_SECONDS = 600       # running 任务心跳超过 10 分钟视为孤儿
EVENT_POLL_SECONDS = 60          # 期号事件轮询间隔（数据抓取节奏为分钟级，无需更快）


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _atomic_write_json(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(str(tmp), str(path))


def _read_json(path: Path, default):
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return default


def _is_digital(lottery: str) -> bool:
    """数字型（每日开，轻量高频监控）vs 乐透型（每周开，每期重算）"""
    from data.schema import is_redblue
    return not is_redblue(lottery)


def _is_daily(lottery: str) -> bool:
    """是否每天开奖（数字型 4 个彩种）。双色球/大乐透/七星彩一周只开 2-3 次。"""
    return len(set(LOTTERY_CONFIG.get(lottery, {}).get("draw_days", []))) == 7


# ---------- 任务处理器（worker 的执行域） ----------

def _run_robustness(task_id: str, params: dict) -> dict:
    from credibility.robustness import compute_robustness_report
    lottery = params.get("lottery")
    mode = params.get("mode") or ("light" if _is_digital(lottery) else "full")
    report = compute_robustness_report(lottery, mode=mode, write=True)
    return {
        "lottery": lottery,
        "mode": report.get("mode", mode),
        "verdict": report.get("verdict"),
        "alarms": report.get("alarms"),
        "data_upto_issue": report.get("data_upto_issue"),
    }


def _run_predict_tiered(task_id: str, params: dict) -> dict:
    from prediction.robust_tiers import predict_tiered
    lottery = params.get("lottery")
    tier = params.get("tier", "robust")
    res = predict_tiered(
        lottery,
        groups=resolve_groups(lottery, params.get("groups")),
        tier=tier,
        record_pending=bool(params.get("record_pending", True)),
        general_mode=params.get("general_mode", "fresh"),
    )
    # result 只存摘要（完整号码在待开奖页/落盘文件里），避免 tasks.db 膨胀
    return {
        "lottery": lottery,
        "tier": res.get("档位", tier),
        "groups": len(res.get("predictions", [])),
        "target_issue": res.get("目标期号"),
    }


def _run_evaluate(task_id: str, params: dict) -> dict:
    """对 pending 待开奖预测做开奖结算 + 策略权重更新（与 auto 流水线里的评估步骤等价）"""
    from data.feedback import evaluate_pending_predictions, update_strategy_weights
    lottery = params.get("lottery")
    eval_res = evaluate_pending_predictions(lottery) or {}
    weights = {}
    try:
        w = update_strategy_weights(lottery)
        weights = w if isinstance(w, dict) else {}
    except Exception as e:
        logger.warning(f"权重更新失败({lottery}): {e}")
    return {
        "lottery": lottery,
        "evaluated": eval_res.get("evaluated_count", 0),
        "new_feedback": eval_res.get("new_feedback_count", 0),
        "weights_updated": bool(weights),
    }


def _run_auto_pipeline(task_id: str, params: dict) -> dict:
    """完整自动流水线：抓取 → 评估 → 异常检测 → 出号 → 期望 → 写看板简报。

    与调度器 22:05 / Web 手动触发的是同一条实现（web.utils.run_auto_pipeline_core），
    只是不传 task_id（本任务自身就是 worker 任务，无需再写一份进度）。

    为什么事件驱动要跑它（2026-09-11 修复的真实故障）：
    老版本数据更新时只入队 evaluate + robustness，开奖后既不生成下一期预测、
    也不刷新看板简报 → 非每日彩种（双色球/大乐透/七星彩）在 App 关机跨过开奖
    时段后整期丢号，且看板卡片长期停在上一期开奖号。
    """
    from web.utils import run_auto_pipeline_core
    lottery = params.get("lottery")
    res = run_auto_pipeline_core(lottery, {
        "mode": params.get("mode", "fresh"),
        "groups": params.get("groups"),  # None → 由核心按彩种 resolve_groups() 解析
        "skip_predict": False,
    }, task_id=None) or {}
    return {
        "lottery": lottery,
        "fetched": res.get("fetched"),
        "latest_after": res.get("latest_after"),
        "prediction": res.get("prediction"),
        "steps": res.get("steps"),
    }


TASK_HANDLERS: Dict[str, Callable[[str, dict], dict]] = {
    "robustness_full": _run_robustness,
    "robustness_light": _run_robustness,
    "predict_tiered": _run_predict_tiered,
    "evaluate": _run_evaluate,
    "auto_pipeline": _run_auto_pipeline,
}


# ---------- ContinuousWorker ----------

class ContinuousWorker:
    def __init__(self, poll_interval: int = 30, mode: str = "embedded",
                 worker_id: Optional[str] = None):
        self.poll_interval = max(10, int(poll_interval))
        self.mode = mode  # embedded | standalone
        self.worker_id = worker_id or f"worker-{os.getpid()}-{mode}"
        self._stop = threading.Event()
        self._current_task = None            # 正在执行的任务 dict（tasks.db 行）
        self._current_thread: Optional[threading.Thread] = None
        self._stats = {"claimed": 0, "done": 0, "failed": 0, "events_enqueued": 0}
        self._last_reclaim = 0.0
        self._last_event_poll = 0.0

    # ---------- 对外状态 ----------
    def status(self) -> dict:
        cur = self._current_task
        return {
            "worker_id": self.worker_id,
            "mode": self.mode,
            "pid": os.getpid(),
            "alive": not self._stop.is_set(),
            "poll_interval": self.poll_interval,
            "last_beat": _now_iso(),
            "current_task": (
                {"id": cur["id"], "type": cur["type"], "params": cur.get("params")}
                if cur else None
            ),
            "stats": dict(self._stats),
        }

    def write_heartbeat(self):
        try:
            _atomic_write_json(WORKER_STATE_FILE, self.status())
        except Exception as e:
            logger.warning(f"worker 心跳写入失败: {e}")

    # ---------- 生命周期 ----------
    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, name="continuous-worker", daemon=True)
        t.start()
        return t

    def stop(self):
        self._stop.set()

    def run(self):
        from web.task_store import task_store
        from web.scheduler import _external_lock_held

        logger.info(f"持续计算核心启动 ({self.mode}, pid={os.getpid()}, id={self.worker_id})")
        # 启动即回收孤儿：本进程接手前，上一个 worker 崩溃遗留的 running 任务
        try:
            n = task_store.reclaim_orphans(
                WORKER_TASK_TYPES, stale_seconds=0,
                reason="worker 重启，接管前回收遗留 running 任务",
            )
            if n:
                logger.warning(f"启动孤儿回收: {n} 个任务被标为 failed")
        except Exception as e:
            logger.warning(f"启动孤儿回收失败: {e}")
        self.write_heartbeat()

        while not self._stop.is_set():
            try:
                self.write_heartbeat()

                # 计划任务在跑（同一把锁文件）→ 让出，不抢 CSV
                if _external_lock_held():
                    self._current_task_heartbeat()
                    self._stop.wait(self.poll_interval)
                    continue

                # 周期性孤儿回收（每 10 分钟一次足够）
                if time.time() - self._last_reclaim > 600:
                    self._last_reclaim = time.time()
                    try:
                        n = task_store.reclaim_orphans(
                            WORKER_TASK_TYPES, stale_seconds=ORPHAN_STALE_SECONDS)
                        if n:
                            logger.warning(f"孤儿回收: {n} 个心跳超时任务被标为 failed")
                    except Exception as e:
                        logger.warning(f"孤儿回收失败: {e}")

                # 前一个任务仍在执行 → 只做心跳续期，不认领新任务（串行纪律）
                if self._current_thread is not None and self._current_thread.is_alive():
                    self._current_task_heartbeat()
                else:
                    self._finalize_current()
                    self._claim_and_execute()

                # 事件轮询（每 60s 一次，与任务执行互不阻塞）
                if time.time() - self._last_event_poll > EVENT_POLL_SECONDS:
                    self._last_event_poll = time.time()
                    self._poll_events()
            except Exception as e:
                logger.warning(f"worker 循环异常: {e}\n{traceback.format_exc()}")
            self._stop.wait(self.poll_interval)

        self.write_heartbeat()
        logger.info("持续计算核心已停止")

    # ---------- 队列消费（形态 B） ----------
    def _claim_and_execute(self):
        from web.task_store import task_store
        try:
            task = task_store.claim_next(WORKER_TASK_TYPES, self.worker_id)
        except Exception as e:
            logger.warning(f"认领任务失败: {e}")
            return
        if not task:
            return
        self._current_task = task
        self._stats["claimed"] += 1
        handler = TASK_HANDLERS.get(task["type"])
        th = threading.Thread(
            target=self._execute_task, args=(task, handler), daemon=True,
            name=f"worker-task-{task['id']}",
        )
        self._current_thread = th
        th.start()
        logger.info(f"认领任务 {task['id']} type={task['type']} params={task.get('params')}")

    def _execute_task(self, task: dict, handler: Optional[Callable]):
        from web.task_store import task_store
        task_id = task["id"]
        params = task.get("params") or {}
        if handler is None:
            task_store.update(task_id, status="failed",
                              error=f"未知任务类型: {task['type']}")
            self._stats["failed"] += 1
            return
        try:
            result = handler(task_id, params)
            task_store.update(task_id, status="done", progress=100,
                              message="完成", result=result or {})
            self._stats["done"] += 1
            logger.info(f"任务完成 {task_id} type={task['type']} -> {result}")
        except Exception as e:
            task_store.update(task_id, status="failed",
                              message="执行失败", error=f"{e}")
            self._stats["failed"] += 1
            logger.warning(f"任务失败 {task_id} type={task['type']}: {e}\n{traceback.format_exc()}")

    def _current_task_heartbeat(self):
        """执行中任务每 30s 续一次心跳，防止被孤儿回收误杀（长任务 full 模式可达数分钟）"""
        from web.task_store import task_store
        cur = self._current_task
        if cur and self._current_thread and self._current_thread.is_alive():
            try:
                task_store.heartbeat(cur["id"])
            except Exception:
                pass

    def _finalize_current(self):
        from web.task_store import task_store
        cur, th = self._current_task, self._current_thread
        if cur and th is not None and not th.is_alive():
            # 线程结束但任务状态仍 running（异常路径兜底）
            t = task_store.get(cur["id"])
            if t and t.get("status") == "running":
                task_store.update(cur["id"], status="failed",
                                  error="worker 线程异常退出，任务未完成")
                self._stats["failed"] += 1
        self._current_task = None
        self._current_thread = None

    # ---------- 事件驱动（开奖数据更新 → 入队） ----------
    def _poll_events(self):
        from web.task_store import task_store
        from credibility.robustness import _latest_issue

        ev_state = _read_json(EVENT_STATE_FILE, {})
        changed = False
        for lot in LOTTERY_CONFIG:
            try:
                issue = _latest_issue(lot)
            except Exception as e:
                logger.warning(f"事件轮询读取最新期号失败({lot}): {e}")
                continue
            if issue is None:
                continue
            st = ev_state.setdefault(lot, {})
            last = st.get("last_issue")
            if last is None:
                # 首次启动：只记录基线不触发（补历史缺口是 scheduler/startup_recovery 的职责）
                st["last_issue"] = issue
                changed = True
                continue
            if int(last) >= int(issue):
                continue
            # 新开奖数据到达 → 入队：结算评估 + 鲁棒性（数字型 light / 乐透型 full）
            rtype = "robustness_light" if _is_digital(lot) else "robustness_full"
            tasks = [
                ("evaluate", {"lottery": lot}),
                (rtype, {"lottery": lot, "mode": "light" if _is_digital(lot) else "full"}),
            ]
            # 非每日彩种（双色球/大乐透/七星彩）一周只开 2-3 次，漏一次就是整期无号：
            # 数据到达时顺带补齐「下一期预测 + 看板简报」。
            # 数字型每日开奖、每晚 22:05 调度器必跑一次完整流水线，此处不重复触发。
            if not _is_daily(lot):
                tasks.append(("auto_pipeline", {"lottery": lot}))
            enqueued = []
            try:
                for ttype, p in tasks:
                    if task_store.has_pending((ttype,), {"lottery": lot}):
                        continue  # 去重：同彩种同类型已有 pending
                    task_store.create(ttype, p)
                    enqueued.append(ttype)
            except Exception as e:
                logger.warning(f"事件入队失败({lot}): {e}")
                continue  # 不更新 last_issue，下轮重试
            st["last_issue"] = issue
            st["last_event"] = _now_iso()
            if enqueued:
                self._stats["events_enqueued"] += len(enqueued)
                logger.info(f"事件触发 {lot} 期号 {last}->{issue}: 入队 {enqueued}")
            changed = True
        if changed:
            try:
                _atomic_write_json(EVENT_STATE_FILE, ev_state)
            except Exception as e:
                logger.warning(f"事件状态保存失败: {e}")


# ---------- 进程级单例与入口 ----------

_worker: Optional[ContinuousWorker] = None
_worker_lock = threading.Lock()


def get_worker() -> ContinuousWorker:
    global _worker
    with _worker_lock:
        if _worker is None:
            _worker = ContinuousWorker(mode="embedded")
        return _worker


def start_worker(daemon: bool = True) -> threading.Thread:
    """嵌入模式入口（app.py 调用）：启动共享单例 worker"""
    w = get_worker()
    return w.start()


def main():
    """独立模式入口（形态 C）：python -m web.worker"""
    parser = argparse.ArgumentParser(prog="web.worker", description="持续计算核心（独立进程）")
    parser.add_argument("--interval", type=int, default=30, help="轮询间隔秒数（默认 30）")
    args = parser.parse_args()

    # 独立模式防双开：另一个活心跳（90s 内）且 pid 不同 → 拒绝启动
    st = _read_json(WORKER_STATE_FILE, {})
    try:
        if (st.get("mode") == "standalone" and st.get("pid") != os.getpid()
                and st.get("last_beat")):
            age = (datetime.now() - datetime.fromisoformat(st["last_beat"])).total_seconds()
            if age < 90:
                print(f"已有独立 worker 在运行 (pid={st['pid']}，心跳 {age:.0f}s 前)，退出。")
                return
    except Exception:
        pass

    w = ContinuousWorker(poll_interval=args.interval, mode="standalone")
    w.run()


if __name__ == "__main__":
    main()
