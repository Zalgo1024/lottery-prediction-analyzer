"""
内置自动调度器（单进程）

把原来「Windows 任务计划程序 + cli.py 子进程」的自动流水线调度，
收敛进 Flask 进程内部的后台线程：
  - 每个彩种按其开奖日（config.LOTTERY_CONFIG[x].draw_days）在开奖后统一时间触发流水线；
  - 状态（启用/停用、上次运行、正在运行、下次运行）实时可读，不再依赖 Windows 注册表；
  - 状态持久化到 config/auto_scheduler_state.json，重启不丢失已完成记录；
  - 提供 run_now() 供「立即运行全部」按钮手动触发。

这样整个程序 = 一个 Flask 进程，关掉就是关掉 Flask（端口 5000），
没有静态数据、没有杀不干净的子进程。
"""

import json
import os
import threading
from datetime import datetime, timedelta

from config import LOTTERY_CONFIG, BASE_DIR, resolve_groups
from data.holiday import is_draw_day
from logs.logger import setup_logger

logger = setup_logger("scheduler")

# 开奖后统一触发时间（22:00，给数据抓取留足缓冲）
RUN_HOUR = 22
RUN_MINUTE = 5

STATE_FILE = BASE_DIR / "config" / "auto_scheduler_state.json"

# 晚间滞后补跑的重查节流（秒）：今日已跑但开奖数据仍滞后时，
# 每隔这么久才真正读一次数据状态，避免高频读 CSV
RETRY_CHECK_INTERVAL = 5 * 60

# 与 Windows 计划任务(auto_scheduled.ps1)共用的互斥锁文件。
# 两者都通过它串行化，避免并发写同一份 CSV 导致数据损坏。
LOCK_FILE = BASE_DIR / "lottery_data" / ".auto_scheduled.lock"
LOCK_MAX_AGE = 6 * 3600  # 与 ps1 中锁过期时间一致（秒）

# ---- market.db 旁路库的每日补库 ----
# 为什么不跟着各彩种的 22:05 一起跑：market 更新是**全库一次**（不是按彩种），
# 且要等 22:05 那批流水线把 CSV 写完之后再抓，否则抓到的是半天前的数据。
# 23:10 距 22:05 有 65 分钟，足够流水线跑完（其上限 10 分钟）；又早于 23:40 的
# 体检日报，所以当天补库失败能当天被日报捞到。
MARKET_RUN_HOUR = 23
MARKET_RUN_MINUTE = 10
MARKET_STATE_KEY = "_market"   # 与彩种名不会冲突；get_status 只遍历 LOTTERY_CONFIG，不会误显示


def _external_lock_held() -> bool:
    """计划任务是否正在跑（用同一把锁文件互斥）。"""
    if not LOCK_FILE.exists():
        return False
    try:
        age = datetime.now().timestamp() - LOCK_FILE.stat().st_mtime
        return age < LOCK_MAX_AGE
    except Exception:
        return False


def _acquire_external_lock() -> bool:
    if _external_lock_held():
        return False
    try:
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        LOCK_FILE.touch()
        return True
    except Exception:
        return False


def _release_external_lock():
    try:
        if LOCK_FILE.exists():
            LOCK_FILE.unlink()
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


class AutoScheduler:
    def __init__(self):
        self._lock = threading.RLock()
        self.enabled = True
        self.state = {}  # lottery -> {last_run, last_task_id, last_status, running}
        self._thread = None
        self._stop = threading.Event()
        self._digest_running = False
        self._market_thread = None
        self._load()

    # ---------- 持久化 ----------
    def _load(self):
        try:
            if STATE_FILE.exists():
                d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self.enabled = bool(d.get("enabled", True))
                self.state = d.get("state", {}) or {}
        except Exception as e:
            logger.warning(f"调度器状态读取失败，使用默认值: {e}")

    def _save(self):
        """原子写：先写临时文件再 os.replace。

        旧实现直接 write_text（先截断），进程被杀或并发写时会留下 0 字节空文件，
        导致重启后调度器读不到 last_run → 误判"从未跑过"而重复触发流水线。
        """
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"enabled": self.enabled, "state": self.state},
                          f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(tmp), str(STATE_FILE))
        except Exception as e:
            logger.warning(f"调度器状态保存失败: {e}")

    # ---------- 调度计算 ----------
    def _next_run(self, lottery: str, now=None):
        """
        返回该彩种「最近一个应当已触发」的开奖日 22:05（不管是否已过）。
        语义：只要当前时间 >= 返回时间、且该日还没跑过，就应立即补跑。
        这样 Flask 在 22:05 之后才启动也能补上当天流水线（不再顺延到下一天）。

        `now` 可注入固定时刻，只为测试确定性（overdue 依赖"是否已过 22:05"，
        不注入的话相关用例每天只有 22:05~24:00 之间能过）。
        """
        cfg = LOTTERY_CONFIG.get(lottery)
        if not cfg:
            return None
        now = now or datetime.now()
        # 休市日不排班：跳过 MARKET_CLOSURE 窗口，回落到休市前最后一个开奖日。
        # 有界 for（15 天）而非 while：无死循环风险；超长休市（2020 疫情 49 天）返回 None = 不触发。
        for d in range(0, 15):
            cand = now.date() - timedelta(days=d)
            if is_draw_day(lottery, cand):
                return datetime(cand.year, cand.month, cand.day, RUN_HOUR, RUN_MINUTE)
        return None

    def _next_future_run(self, lottery: str, now=None):
        """返回该彩种「下一次真的会触发」的开奖日 22:05（严格晚于当前时间）。

        与 _next_run 的区别：`_next_run` 返回"最近一个应已触发"的时刻，用于判断
        今天该不该补跑，那个时间点通常已经过去；直接把它显示在看板「下次运行」列，
        会出现"下次运行 = 两天前"这种自相矛盾的展示，让人误判调度停摆。
        本方法只用于展示，不参与触发判断。
        休市日历感知（2026-10-02）：休市窗口内的日期不算开奖日，自动跳到恢复后首个开奖日。
        """
        cfg = LOTTERY_CONFIG.get(lottery)
        if not cfg:
            return None
        now = now or datetime.now()
        for d in range(0, 15):
            cand = now.date() + timedelta(days=d)
            if is_draw_day(lottery, cand):
                t = datetime(cand.year, cand.month, cand.day, RUN_HOUR, RUN_MINUTE)
                if t > now:
                    return t
        return None

    # ---------- 对外状态 ----------
    def get_status(self, now=None):
        now = now or datetime.now()
        lotteries = []
        for lot in LOTTERY_CONFIG:
            st = self.state.get(lot, {})
            nr = self._next_run(lot, now=now)
            nf = self._next_future_run(lot, now=now)
            # 该跑却没跑：应触发时刻已过，且当天没有运行记录
            overdue = False
            if nr and nr <= now:
                last = st.get("last_run") or ""
                try:
                    overdue = bool(last[:10] < nr.date().isoformat()) if last else True
                except Exception:
                    overdue = True
            # 用任务真实状态修正 running / last_status（避免异步线程未回调）
            running = bool(st.get("running", False))
            tid = st.get("last_task_id")
            if running and tid:
                try:
                    from web.task_store import task_store
                    t = task_store.get(tid)
                    if t is None:
                        # 关联的任务已不存在（进程重启/被清理，线程被kill），
                        # 视为已完成，清除 running 避免永远显示"运行中"
                        running = False
                        with self._lock:
                            st["running"] = False
                            st["last_status"] = st.get("last_status") or "完成"
                            self._save()
                    elif t.get("status") in ("done", "success", "failed", "error"):
                        running = False
                        with self._lock:
                            st["running"] = False
                            st["last_status"] = t.get("status")
                            self._save()
                except Exception:
                    pass
            lotteries.append({
                "lottery": lot,
                # 展示口径：下一次真正会触发的时刻（未来时间）
                "next_run": nf.isoformat() if nf else None,
                # 调度口径：最近一个应已触发的时刻（可能已过去），供排查用
                "due_run": nr.isoformat() if nr else None,
                "overdue": overdue,
                "last_run": st.get("last_run"),
                "last_status": st.get("last_status"),
                "running": running,
                "draw_days": cfg_draw_days(lot),
            })
        return {
            "enabled": self.enabled,
            "now": now.isoformat(timespec="seconds"),
            "lotteries": lotteries,
        }

    def set_enabled(self, val: bool):
        with self._lock:
            self.enabled = bool(val)
            self._save()
        logger.info(f"自动调度已{'启用' if self.enabled else '停用'}")
        return {"ok": True, "enabled": self.enabled}

    # ---------- 触发流水线 ----------
    def _launch(self, lottery: str, respect_lock: bool = False) -> bool:
        # 与 Windows 计划任务共用一把锁文件，避免并发写 CSV
        if respect_lock:
            if _external_lock_held():
                return False
            if not _acquire_external_lock():
                return False
        try:
            from web.utils import start_auto_pipeline
            from web.task_store import task_store
            tid = start_auto_pipeline(lottery, {"mode": "fresh", "groups": resolve_groups(lottery), "skip_predict": False})
            # 自动触发(respect_lock)时持有锁直到流水线线程完成，确保与计划任务串行；
            # 手动触发不等待，避免阻塞 API 调用。
            if respect_lock:
                import time as _t
                deadline = datetime.now().timestamp() + 10 * 60
                final_status = "done"
                while datetime.now().timestamp() < deadline:
                    try:
                        t = task_store.get(tid)
                        if t is None or t.get("status") in ("done", "success", "failed", "error"):
                            final_status = (t or {}).get("status") or "done"
                            break
                    except Exception:
                        break
                    _t.sleep(3)
            with self._lock:
                st = self.state.setdefault(lottery, {})
                st["last_task_id"] = tid
                # 自动触发(respect_lock)时已等待流水线完成，标为已完成；
                # 手动触发不等待，保留 running=True，由 get_status 据任务真实状态修正。
                st["running"] = False if respect_lock else True
                st["last_status"] = final_status if respect_lock else "running"
                st["last_run"] = _now_iso()
                self._save()
            logger.info(f"调度器触发 {lottery} 流水线 (task={tid})")
            # 开奖后结算钩子：自动触发（已等流水线把最新开奖抓回）后，
            # 把影子台账里「已记录、未结算」的历史期对照实际开奖回填。
            # 只动 research-only 的 training/profit 影子台账，不影响 pending/生产。
            if respect_lock:
                self._settle_profit_ledger(lottery)
            return True
        except Exception as e:
            logger.warning(f"调度器触发 {lottery} 失败: {e}")
            with self._lock:
                st = self.state.setdefault(lottery, {})
                st["last_status"] = f"失败: {e}"
                self._save()
            return False
        finally:
            if respect_lock:
                _release_external_lock()

    # ---------- market.db 每日补库（分省头奖 / 出球顺序靠它） ----------
    def _market_due(self, now=None) -> bool:
        """今天 23:10 是否该补库且还没补过。

        与各彩种同一套语义：只要「应触发时刻已过」且「当天没跑过」就补，
        所以 23:10 之后才开机（或服务重启）也能当天补上，不会顺延到明天。
        """
        now = now or datetime.now()
        target = now.replace(hour=MARKET_RUN_HOUR, minute=MARKET_RUN_MINUTE,
                             second=0, microsecond=0)
        if now < target:
            return False
        with self._lock:
            st = self.state.get(MARKET_STATE_KEY) or {}
        last = st.get("last_run")
        if not last:
            return True
        try:
            return datetime.fromisoformat(last).date() < now.date()
        except Exception:
            return True

    def _run_market_update(self, reason: str = "daily", mode: str = None) -> bool:
        """后台线程跑 market 更新。返回是否真的起来了（已在跑则 False）。

        全量约 3~5 分钟，绝不能占着 tick 线程 —— 那会让当天的流水线触发顺延。
        """
        if self._market_thread is not None and self._market_thread.is_alive():
            return False
        if mode not in (None, "incremental", "full"):
            logger.warning("未知的补库模式 %r，按增量处理" % (mode,))
            mode = None
        mode_override = mode

        def _work():
            try:
                from market.update import run_update
                # 周日全量翻页（重建完整历史，10~20 分钟），其余日子增量抓最近一页
                # （十几秒）。全量每天跑没必要，还会把官方接口翻 500+ 页。
                mode = (mode_override
                        or ("full" if datetime.now().weekday() == 6 else "incremental"))
                # 用 logger 而不是 print：调度器在 Flask 进程里，stdout 被重定向进
                # logs/flask_stdout.log，走 logger 才能按级别过滤、带时间戳。
                r = run_update(mode=mode, log=lambda m: logger.info("[market] %s" % m))
            except Exception as e:
                logger.warning(f"market 补库异常（不影响主流程）: {e}")
                r = {"ok": False, "status": "fail",
                     "error": "%s: %s" % (type(e).__name__, str(e)[:300])}

            with self._lock:
                st = self.state.setdefault(MARKET_STATE_KEY, {})
                st["last_run"] = _now_iso()
                st["last_status"] = r.get("status") or ("ok" if r.get("ok") else "fail")
                st["last_reason"] = reason
                st["last_mode"] = r.get("mode")
                st["last_elapsed"] = r.get("elapsed")
                st["last_rows"] = r.get("rows")
                st["last_failed"] = r.get("failed") or []
                st["last_skipped"] = r.get("skipped") or []
                if r.get("error"):
                    st["last_error"] = r["error"]
                else:
                    st.pop("last_error", None)
                self._save()

            if r.get("ok"):
                logger.info(f"market 补库完成：{r.get('rows')} 期，{r.get('elapsed')}s")
            else:
                logger.warning("market 补库未全部成功：失败源=%s 错误=%s"
                               % (r.get("failed") or [], r.get("error") or "-"))

        self._market_thread = threading.Thread(target=_work, name="market-update", daemon=True)
        self._market_thread.start()
        return True

    def trigger_market_update(self, reason: str = "manual", mode: str = None) -> bool:
        """手动触发补库（页面「更新数据」按钮 / 接口）。

        故意与定时触发共用同一条路径和同一份状态：手动跑过之后当天 23:10
        就不会再跑一遍（数据已经是最新的，没必要重复抓 5 分钟）。
        """
        return self._run_market_update(reason=reason, mode=mode)

    def market_status(self) -> dict:
        """给接口/页面看的补库状态。"""
        with self._lock:
            st = dict(self.state.get(MARKET_STATE_KEY) or {})
        try:
            from market.update import is_running
            running = bool(is_running())
        except Exception:
            running = False
        return {
            "enabled": self.enabled,
            "running": running or bool(self._market_thread and self._market_thread.is_alive()),
            "last_run": st.get("last_run"),
            "last_status": st.get("last_status"),
            "last_elapsed": st.get("last_elapsed"),
            "last_rows": st.get("last_rows"),
            "last_failed": st.get("last_failed") or [],
            "last_skipped": st.get("last_skipped") or [],
            "last_mode": st.get("last_mode"),
            "last_error": st.get("last_error"),
            "schedule": "%02d:%02d" % (MARKET_RUN_HOUR, MARKET_RUN_MINUTE),
            "due_today": self._market_due(),
        }

    # ---------- 七星彩每日补抓（纯数据，不跑流水线/不生成预测） ----------
    def _daily_qxc_supplement(self):
        """七星彩数据源恒延迟约 1 期：若在开奖日(周二/五/日)才抓取，
        非开奖日不抓 → 日历滞后会跨 2~3 天。
        每天做一次纯数据 fetch（update_lottery_data，幂等：无新数据则 fetched=0），
        把滞后从「跨2天」压到「跨≤1天」。不触发预测生成。"""
        lot = "七星彩"
        now = datetime.now()
        st = self.state.setdefault(lot, {})
        last_sup = st.get("last_supplement")
        if last_sup:
            try:
                if datetime.fromisoformat(last_sup).date() >= now.date():
                    return  # 今天已补抓过
            except Exception:
                pass
        if _external_lock_held():
            return  # 计划任务在跑，让出（补抓不急）
        try:
            from data.fetcher import update_lottery_data
            res = update_lottery_data(lot)
            fetched = res.get("fetched", 0) if isinstance(res, dict) else 0
            logger.info(f"七星彩每日补抓(纯数据): fetched={fetched}")
            st["last_supplement"] = _now_iso()
            self._save()
        except Exception as e:
            logger.warning(f"七星彩每日补抓失败: {e}")

    def _daily_digest(self):
        """离线体检：到点跑日报，周日加一次全量扫描。

        跑在独立线程里：周日那份会做六彩种全量扫描（数十秒），
        占着 tick 线程会让当天的流水线触发整体顺延一分钟 —— 不值得。
        先用 is_due() 在主线程做一次毫秒级判断，避免每 60s 无谓地起线程。
        """
        if self._digest_running:
            return
        try:
            from web.health_digest import is_due
            if not is_due():
                return
        except Exception as e:
            logger.warning(f"离线体检判定异常(降级): {e}")
            return

        self._digest_running = True

        def _run():
            try:
                from web.health_digest import maybe_run
                res = maybe_run()
                if res:
                    logger.info(f"离线体检已执行: {res.get('已执行')}")
            except Exception as e:
                # 体检自身绝不能让调度循环挂掉
                logger.warning(f"离线体检异常(降级): {e}")
            finally:
                self._digest_running = False

        threading.Thread(target=_run, name="health-digest", daemon=True).start()

    def _settle_profit_ledger(self, lottery: str):
        """开奖后结算钩子：把某彩种影子台账里『已记录、未结算』的历史期回填。

        只动 research-only 的 training/profit 影子台账（不参与生产、不写 pending），
        流水线把最新开奖抓回后才调用，确保实际开奖数据已就位。任何异常只记日志，
        绝不让结算失败拖垮主调度循环。
        """
        try:
            from ev.profit import settle as ST
            res = ST.settle_pending(lottery)
            newly = res.get("新结算期数", 0)
            if newly:
                logger.info(f"影子台账结算（{lottery}）：新结算 {newly} 期，"
                            f"已结算 {res.get('已结算期数', 0)} 期")
            else:
                logger.debug(f"影子台账结算（{lottery}）：无新结算期")
        except Exception as e:
            logger.warning(f"影子台账结算钩子异常（不影响主流程）: {e}")

    def run_now(self, lottery=None):
        """手动立即运行：指定彩种或（默认）全部。返回成功触发的彩种列表。
        手动触发无视锁（用户明确要跑），且不阻塞等待。"""
        lots = [lottery] if lottery else list(LOTTERY_CONFIG.keys())
        launched = []
        for lot in lots:
            if lot not in LOTTERY_CONFIG:
                continue
            if self._launch(lot, respect_lock=False):
                launched.append(lot)
        return {"ok": True, "launched": launched}

    # ---------- 后台循环 ----------
    def _tick(self):
        while not self._stop.is_set():
            try:
                if self.enabled:
                    now = datetime.now()
                    # 计划任务正在跑 -> 本进程让出，避免并发写 CSV
                    if _external_lock_held():
                        self._stop.wait(60)
                        continue
                    for lot in LOTTERY_CONFIG:
                        target = self._next_run(lot)   # 最近应触发时间（可能已过）
                        if not target or now < target:
                            continue
                        with self._lock:
                            st = self.state.setdefault(lot, {})
                            already = False
                            last = st.get("last_run")
                            if last:
                                try:
                                    already = datetime.fromisoformat(last).date() >= target.date()
                                except Exception:
                                    already = False
                            last_recheck = st.get("last_recheck")
                        should_run = not already
                        # 晚间滞后补跑（时间戳节流，替代旧的 minute%10==0 —— 那写法依赖
                        # 60s 步进恰好踩中整 10 分，循环相位漂移会整窗错过，且 21 点窗口
                        # 之外永远不重试）：今日已跑、已过 21:00、距上次重查 ≥5 分钟
                        # 才真正读一次数据状态；仍滞后 → 强制补跑。
                        if not should_run and 21 <= now.hour <= 23:
                            due = True
                            if last_recheck:
                                try:
                                    gap = (now - datetime.fromisoformat(last_recheck)).total_seconds()
                                    due = gap >= RETRY_CHECK_INTERVAL
                                except Exception:
                                    due = True
                            if due:
                                with self._lock:
                                    self.state.setdefault(lot, {})["last_recheck"] = _now_iso()
                                    self._save()
                                try:
                                    from web.utils import get_data_status
                                    lag = (get_data_status(lot) or {}).get("data_lag_days") or 0
                                    if lag > 0:
                                        should_run = True
                                        logger.info(f"调度器晚间补跑 {lot}（今日已跑但数据仍滞后 {lag} 天）")
                                except Exception:
                                    pass
                        if should_run:
                            logger.info(f"调度器触发 {lot}（应触发 {target.isoformat(timespec='minutes')}，上次 {last or '从未'}）")
                            self._launch(lot, respect_lock=True)
                    # 七星彩每日补抓（纯数据，压滞后用；与开奖日流水线互不冲突）
                    self._daily_qxc_supplement()
                    # market.db 每日补库（23:10）：官方源里才有分省头奖、出球顺序，
                    # 这些字段 CSV 不存，只能靠这条线保持最新。
                    if self._market_due():
                        if self._run_market_update(reason="daily"):
                            logger.info("调度器触发 market.db 每日补库（23:10 档）")
                    # 离线体检日报/周检（2026-10-21）：到点才跑，一天只跑一次。
                    # 只在有问题时推送 —— 你不开机的时候，它就是那个替你看着的人。
                    self._daily_digest()
            except Exception as e:
                logger.warning(f"调度器循环异常: {e}")
            self._stop.wait(60)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._tick, name="auto-scheduler", daemon=True)
        self._thread.start()
        logger.info("自动调度器已启动")

    def stop(self):
        self._stop.set()
        logger.info("自动调度器已停止")


def cfg_draw_days(lottery: str):
    return LOTTERY_CONFIG.get(lottery, {}).get("draw_days", list(range(7)))


# 全局单例（整个进程共享一份状态）
scheduler = AutoScheduler()
