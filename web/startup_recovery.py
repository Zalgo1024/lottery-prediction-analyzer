"""
启动时数据恢复：检查本地数据是否滞后，自动在后台补齐。

背景：自动调度器只在 Flask 进程存活时 22:05 触发。用户经常关机/重启，
导致程序启动时数据已滞后多日。此模块在 Flask 启动后后台跑一次，
对"已经开奖但本地没更新"的彩种自动抓取并评估 pending，无需等当晚调度。
"""

import logging
import threading
from datetime import datetime

from config import LOTTERY_CONFIG
from web.utils import get_data_status

logger = logging.getLogger(__name__)


def _needs_recovery(lottery_name: str, status: dict) -> bool:
    """判断该彩种是否需要启动时补数据。"""
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

    today = datetime.now().date()
    now = datetime.now()
    draw_days = set(LOTTERY_CONFIG.get(lottery_name, {}).get("draw_days", []))
    is_daily = len(draw_days) == 7

    # 下期开奖日已过：肯定已经开奖但本地没更新
    if next_draw_d < today:
        return True

    # 每日开奖彩种：只要滞后 >=1 天且已过当晚 21:00，就补
    if is_daily and lag >= 1 and now.hour >= 21:
        return True

    # 非每日开奖：如果今天就是开奖日且已到晚上 21:30，补
    if not is_daily and today.weekday() in draw_days and (now.hour * 60 + now.minute) >= (21 * 60 + 30):
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
    try:
        from config import resolve_groups
        from web.utils import start_auto_pipeline
        tid = start_auto_pipeline(
            lottery_name,
            {"mode": "fresh", "groups": resolve_groups(lottery_name), "skip_predict": False},
        )
        logger.info(f"启动恢复：{lottery_name} 触发完整流水线补齐下一期预测 (task={tid})")
    except Exception as e:
        logger.warning(f"启动恢复触发流水线失败 {lottery_name}: {e}")


def recover_lottery(lottery_name: str, force: bool = False):
    """
    对单个彩种执行补数据 + 评估反馈。
    force=True 时跳过滞后判断，强制抓取并评估。
    返回 update_lottery_data / update_digital_lottery_data 的结果字典，或 None（无需恢复）。
    """
    from data.fetch_500 import update_digital_lottery_data
    from data.fetcher import update_lottery_data
    from data.feedback import evaluate_pending_predictions

    status = get_data_status(lottery_name)
    if not force and not _needs_recovery(lottery_name, status):
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
    """在后台线程运行启动恢复，避免阻塞 Flask 启动。"""
    def _run():
        # 稍微等待，让 scheduler 和缓存预热先启动
        import time
        time.sleep(2)
        recover_all_lotteries(force=False)
        # 再兜一层：数据已同步但流水线没跑过（简报停在旧期）→ 补跑
        try:
            ensure_pipelines_fresh()
        except Exception as e:
            logger.warning(f"启动检查简报新鲜度异常: {e}")

    t = threading.Thread(target=_run, name="startup-recovery", daemon=True)
    t.start()
    logger.info("启动恢复线程已启动")
    return t
