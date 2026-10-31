"""
启动时数据恢复：检查本地数据是否滞后，自动在后台补齐。

背景：自动调度器只在 Flask 进程存活时 22:05 触发。用户经常关机/重启，
导致程序启动时数据已滞后多日。此模块在 Flask 启动后后台跑一次，
对"已经开奖但本地没更新"的彩种自动抓取并评估 pending，无需等当晚调度。
"""

import logging
import threading
from datetime import datetime
from typing import Optional

from config import LOTTERY_CONFIG
from data.holiday import is_draw_day, is_holiday
from web.utils import get_data_status

logger = logging.getLogger(__name__)

# ------------------------------------------------------------
# 单飞与去重（2026-10-21）
# ------------------------------------------------------------
# 事故背景：冷启时三条路径几乎同时开火 —— 启动恢复的 recover_lottery、
# 紧接着的 ensure_pipelines_fresh、以及 worker 的「数据变更」事件触发。
# 实测同一轮冷启对双色球触发 2 次流水线、七星彩 3 次：重复抓取、重复出号、
# 重复写自动化简报（status.json 里能看到同时刻两条几乎相同的记录）。
#
# 这次加两道闸：
#   1) _startup_lock/_started —— 整个进程生命周期内启动恢复只跑一次；
#   2) _triggered —— 同一轮恢复里每个彩种最多触发一次流水线（跨函数共享）。
_startup_lock = threading.Lock()
_started = False
_pass_lock = threading.Lock()
_triggered: set = set()


def _reset_trigger_registry() -> None:
    """开始新一轮启动恢复前清空「本轮已触发」集合。"""
    with _pass_lock:
        _triggered.clear()


def _claim_trigger(lottery_name: str) -> bool:
    """抢占某彩种本轮的流水线触发权；已抢过则返回 False（调用方应跳过）。"""
    with _pass_lock:
        if lottery_name in _triggered:
            return False
        _triggered.add(lottery_name)
        return True


def _needs_recovery(lottery_name: str, status: dict, now: Optional[datetime] = None) -> bool:
    """判断该彩种是否需要启动时补数据。now 可注入（测试不得依赖真实时钟）。"""
    lag = status.get("data_lag_days") or 0
    if lag <= 0:
        return False

    next_draw = status.get("next_draw_date", "")
    if not next_draw:
        return False

    try:
        next_draw_d = datetime.strptime(str(next_draw)[:10], "%Y-%m-%d").date()
    except Exception:
        return False

    now = now or datetime.now()
    today = now.date()

    # 休市日：没有新开奖可补，抓取必然 0 期，不空跑流水线（P0 修好后 lag 本就为 0，
    # 这里是双保险——防止任何一条旁路仍用自然日差口径时把休市误判成滞后）
    if is_holiday(today):
        return False

    # 下期开奖日已过：肯定已经开奖但本地没更新
    if next_draw_d < today:
        return True

    draw_days = set(LOTTERY_CONFIG.get(lottery_name, {}).get("draw_days", []))
    is_daily = len(draw_days) == 7

    # 每日开奖彩种：只要滞后 >=1 天且已过当晚 21:00，就补
    if is_daily and lag >= 1 and now.hour >= 21:
        return True

    # 非每日开奖：今天必须是"合法开奖日"（含休市判断）且已到晚上 21:30，补
    if not is_daily and is_draw_day(lottery_name, today) and (now.hour * 60 + now.minute) >= (21 * 60 + 30):
        return True

    return False


def _is_daily(lottery_name: str) -> bool:
    """是否每天开奖（数字型 4 个彩种）。双色球/大乐透/七星彩一周只开 2-3 次。"""
    return len(set(LOTTERY_CONFIG.get(lottery_name, {}).get("draw_days", []))) == 7


def _trigger_pipeline_if_needed(lottery_name: str):
    """补完开奖数据后，确保「下一期预测 + 看板简报」也补齐。

    2026-09-11 排查到的真实故障：非每日彩种（双色球/大乐透/七星彩）若 App 关机
    跨过开奖时段，启动恢复只做了「抓数据 + 评估 pending」——既没有生成下一期预测，
    也没有写自动化简报 → 整期丢号，且看板卡片长期停在上一期（甚至上上期）开奖号。

    这里触发一次完整流水线（web.utils.run_auto_pipeline_core）。
    该核心对「下期已有 pending」做了去重，因此与 worker 事件驱动重复触发是幂等的。
    """
    if _is_daily(lottery_name):
        return
    if not _claim_trigger(lottery_name):
        logger.info(f"启动恢复：{lottery_name} 本轮已触发过流水线，跳过重复触发")
        return
    try:
        from web.utils import start_auto_pipeline
        # ★ 2026-10-24：不在触发时刻固化 groups，交给出号时刻的 resolve_groups()
        #   读最新滑轨（同 web/scheduler.py 的理由：固化会让旧值覆盖用户新设定）。
        tid = start_auto_pipeline(
            lottery_name,
            {"mode": "fresh", "skip_predict": False},
        )
        logger.info(f"启动恢复：{lottery_name} 触发完整流水线补齐下一期预测 (task={tid})")
    except Exception as e:
        # 触发失败要归还触发权，否则本轮再没有机会补
        with _pass_lock:
            _triggered.discard(lottery_name)
        logger.warning(f"启动恢复触发流水线失败 {lottery_name}: {e}")


def recover_lottery(lottery_name: str, force: bool = False, now: Optional[datetime] = None):
    """
    对单个彩种执行补数据 + 评估反馈。
    force=True 时跳过滞后判断，强制抓取并评估。
    now 可注入（测试不得依赖真实时钟）。
    返回 update_lottery_data / update_digital_lottery_data 的结果字典，或 None（无需恢复）。
    """
    from data.fetch_500 import update_digital_lottery_data
    from data.fetcher import update_lottery_data
    from data.feedback import evaluate_pending_predictions

    status = get_data_status(lottery_name, now=now)
    if not force and not _needs_recovery(lottery_name, status, now=now):
        return None

    lag = status.get("data_lag_days", 0)
    logger.info(f"启动恢复：{lottery_name} 数据滞后 {lag} 天（最新 {status.get('latest_date')}），开始抓取...")

    if lottery_name in ("排列3", "排列5", "福彩3D", "七星彩"):
        result = update_digital_lottery_data(lottery_name)
    else:
        result = update_lottery_data(lottery_name)

    if "error" in result:
        logger.warning(f"启动恢复抓取失败 {lottery_name}: {result['error']}")
        return result

    fetched = result.get("fetched", 0)
    if fetched > 0:
        logger.info(f"启动恢复：{lottery_name} 新增 {fetched} 期，评估 pending...")
        evaluate_pending_predictions(lottery_name)
        _trigger_pipeline_if_needed(lottery_name)
    else:
        logger.info(f"启动恢复：{lottery_name} 抓取 0 条，远端也可能尚未更新")

    return result


def ensure_pipelines_fresh() -> dict:
    """启动时检查「非每日彩种」的自动化简报是否落后于本地开奖数据，落后就补跑流水线。

    与 _trigger_pipeline_if_needed 的分工：
    - 那条只在「本轮刚补抓到新数据」时触发；
    - 这条兜的是「数据早就补齐了、但流水线从没跑过」——正是 2026-09-11 用户看到
      大乐透卡片停在 26102（本地 CSV 其实已有 26103）的成因。
    返回 {彩种: {"brief_issue": 简报期号, "latest": 本地最新期号}}。
    """
    from data.automation_status import get_status
    from data.loader import load_lottery

    triggered: dict = {}
    try:
        state = get_status()
        by_lot = state.get("last_run_by_lottery", {}) or {}
    except Exception as e:
        logger.warning(f"启动检查：读取自动化状态失败: {e}")
        return triggered

    for lottery_name in LOTTERY_CONFIG:
        if _is_daily(lottery_name):
            continue
        try:
            recs = load_lottery(lottery_name).records
            if not recs:
                continue
            latest = max(int(r.期号 or 0) for r in recs)
            entry = by_lot.get(lottery_name) or {}
            brief_issue = (entry.get("draw_numbers") or {}).get("期号")
            if brief_issue is None or int(brief_issue) < latest:
                logger.info(
                    f"启动检查：{lottery_name} 简报停在 {brief_issue}，本地已到 {latest}，触发流水线补齐"
                )
                _trigger_pipeline_if_needed(lottery_name)
                triggered[lottery_name] = {"brief_issue": brief_issue, "latest": latest}
        except Exception as e:
            logger.warning(f"启动检查简报新鲜度失败 {lottery_name}: {e}")
    return triggered


def ensure_pending_groups() -> dict:
    """启动时核对「下一期 pending 组数 == 当前出号数量」，不一致或缺失就补跑流水线。

    为什么这条比补数据更重要（2026-10-21 事故）：
      看板「一键自动化」曾把 `groups:5` 写死发给后端，把 6 彩种的生产 pending
      从 200/38/50/44 覆盖成 5 注——**机器照跑、日志全绿、报表照长**，页面上没有
      一个字提示。这类失真的唯一可靠判据是「滑轨 == 实际组数」。
      而 `_needs_recovery` 只看数据滞后：数据新鲜时启动恢复什么都不做，
      错误的 pending 会原样留存到当天 22:05 之后。故这里做一次**结构自愈**。

    返回 {彩种: {"滑轨", "原组数", "目标期号", "动作"}}（仅列出需要处理的彩种）。
    """
    from config import resolve_groups
    from data.feedback import _next_issue, load_pending, latest_production_pending
    from web.utils import start_auto_pipeline

    # ★ 2026-10-31：配置健康度哨兵。配置缺失/被兜底时，`resolve_groups()` 返回的是
    #   **默认值**而非用户设定值 —— 于是「自愈」会按错误注数重新生成 pending，
    #   比不修更糟。这里必须先确认配置可信，否则整轮跳过（停发）。
    try:
        from data import ticket_size as _TS
        _hc = _TS.health()
    except Exception as _e:                                  # noqa: BLE001
        logger.error(f"配置健康度哨兵执行失败（跳过自愈）：{_e}")
        return {}
    if not _hc.get("安全"):
        reason = _TS.stop_reason(_hc)
        logger.error(f"启动自愈跳过：{reason}")
        return {lot: {"滑轨": 0, "原组数": None, "目标期号": None,
                      "动作": f"配置不安全，不触发流水线（{reason}）"}
                for lot in LOTTERY_CONFIG}

    fixed: dict = {}
    for lottery_name in LOTTERY_CONFIG:
        try:
            slide = int(resolve_groups(lottery_name))
            # ★ 2026-10-30（任务 2）：滑轨 0 = 预算闸门停发。必须在 `_claim_trigger`
            #   **之前**返回 —— 否则会先占住本轮任务再触发流水线，重生成照样 0 注，
            #   下一轮再占一次：自愈风暴 + 日志假故障。
            if slide <= 0:
                logger.info(f"启动自愈：{lottery_name} 预算包络停发（滑轨 0）⇒ 不触发流水线")
                fixed[lottery_name] = {"滑轨": 0, "原组数": None,
                                       "目标期号": None, "动作": "停发，不触发流水线"}
                continue
            next_issue = _next_issue(lottery_name)
            # ★ 只看生产批：研究批（200 注）若被算进来，滑轨恰好 200 时会「一致」而跳过自愈，
            #   真正的生产批缺失反而修不上；只有研究批时必须视为「无 pending」并触发补跑。
            p = latest_production_pending(load_pending(lottery_name), target_issue=next_issue)
            if p is not None:
                actual = len(p.get("预测号码") or [])
                if actual == slide:
                    continue
                reason = f"组数 {actual} ≠ 滑轨 {slide}"
            else:
                reason = "下一期无 pending"
            if not _claim_trigger(lottery_name):
                logger.info(f"启动自愈：{lottery_name} 本轮已有流水线任务，交由它修复（{reason}）")
                continue
            tid = start_auto_pipeline(
                lottery_name,
                # ★ 同样不固化 slide：自愈的判据用触发时刻的 slide，但**生成时**
                #   必须读最新滑轨，否则修复动作本身又会用旧值把 pending 写成旧注数。
                {"mode": "fresh", "skip_predict": False},
            )
            logger.warning(f"启动自愈：{lottery_name} {reason} → 已触发流水线重生成 (task={tid})")
            fixed[lottery_name] = {
                "滑轨": slide,
                "原组数": actual if p is not None else 0,
                "目标期号": next_issue,
                "动作": "重新生成下一期预测",
            }
        except Exception as e:
            logger.warning(f"启动自愈检查失败 {lottery_name}: {e}")
    return fixed


def recover_all_lotteries(force: bool = False) -> dict:
    """遍历所有彩种，补滞后数据并评估反馈。返回汇总结果。"""
    results = {}
    for lottery_name in LOTTERY_CONFIG:
        try:
            r = recover_lottery(lottery_name, force=force)
            if r:
                results[lottery_name] = {
                    "fetched": r.get("fetched", 0),
                    "latest_before": r.get("latest_before"),
                    "latest_after": r.get("latest_after"),
                    "message": r.get("message", ""),
                }
        except Exception as e:
            results[lottery_name] = {"error": str(e)}
    return results


def run_startup_recovery():
    """在后台线程运行启动恢复，避免阻塞 Flask 启动。

    单飞：整个进程只跑一次。run.py 看门狗热重启会拉起新进程 ⇒ 自然重置。
    """
    global _started
    with _startup_lock:
        if _started:
            logger.info("启动恢复已在进行或已完成，跳过重复启动")
            return None
        _started = True

    def _run():
        # 稍微等待，让 scheduler 和缓存预热先启动
        import time
        time.sleep(2)
        _reset_trigger_registry()
        results = recover_all_lotteries(force=False)
        # 再兜一层：数据已同步但流水线没跑过（简报停在旧期）→ 补跑
        try:
            ensure_pipelines_fresh()
        except Exception as e:
            logger.warning(f"启动检查简报新鲜度异常: {e}")
        # 结构自愈：下一期 pending 组数 ≠ 出号数量（被覆盖/引擎少注）→ 重生成
        try:
            fixed = ensure_pending_groups()
            if fixed:
                results["_注数自愈"] = fixed
        except Exception as e:
            logger.warning(f"启动检查注数一致性异常: {e}")
        # 启动自检推送（2026-09-18）：补数据结果推一次；未启用/失败一律降级。
        try:
            from data.push_notify import push_recovery_summary
            push_recovery_summary(results)
        except Exception as e:
            logger.warning(f"启动自检推送失败(降级): {e}")
        # 离线体检补报（2026-10-21）：开机第一眼就能看到「过去 N 天怎么样」，
        # 不用自己去翻六个页面。只在有问题时推送，正常静默。
        try:
            from web.health_digest import maybe_run
            maybe_run(cold_start=True)
        except Exception as e:
            logger.warning(f"离线体检补报失败(降级): {e}")

    t = threading.Thread(target=_run, name="startup-recovery", daemon=True)
    t.start()
    logger.info("启动恢复线程已启动")
    return t
