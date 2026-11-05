# -*- coding: utf-8 -*-
"""离线体检周报（digest）—— 你不在的时候，它替你看着。

为什么单独做这一层（2026-10-21）：

用户原话是「国庆之后没有天天开了，能不能让没有每天开的情况下不至于那么频繁的维护」。
此前项目里已经有很好的检查器（`scripts/health_check.py` 六项全扫描 + 推送），
但它**没有任何调用方** —— 它只会被人手动叫醒。人不开机，它就不跑，
这恰好把「维护」绑死在了「人得在线」上。

所以这里补两个缺口：

1. **谁来叫醒它**：`web/scheduler.py` 每 60s tick 一次，在 tick 里判定
   「今日日报跑过没」/「本周全检跑过没」，没跑就触发。
2. **冷启补报**：进程启动时若上次日报不是今天，立刻补一份
   （`maybe_run(..., cold_start=True)`）。这样开机第一屏就看到过去 N 天状况，
   不用自己去翻六个页面。

分层（重活和轻活分开，避免每天跑全扫描）：

  - 日报（`run_digest`）：只读 status.json / pending / CSV 最新期，秒级。
    **只在有问题时推送**，正常静默 —— 否则会变成每天一条噪音，你照样会关掉它。
  - 周检（`run_full_health`）：调 `scripts/health_check.py` 的六项全扫描，
    报告落 `logs/项目体检_<日期>.md`，无条件推送（一周一条不算吵）。

诚实边界：这里只汇总「机器自己跑出来的事实」，不做任何推断。
没数据就写没数据，不补 0、不假装正常。
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent

# 状态文件（与 push_notify.json 同级；测试由根 conftest 重定向到 tmp）
STATE_PATH = BASE_DIR / "config" / "digest_state.json"

STATUS_FILE = BASE_DIR / "logs" / "automation" / "status.json"
DIGEST_DIR = BASE_DIR / "logs" / "automation"
HEALTH_DIR = BASE_DIR / "logs"

WINDOW_DAYS = 7          # 日报默认回看窗口
DIGEST_HOUR = 23         # 日报时点（开奖 21:30 结算之后）
DIGEST_MINUTE = 40
FULL_HEALTH_WEEKDAY = 6  # 周日跑一次全扫描
LOG_RETENTION_DAYS = 90  # 日志保留天数（周日顺带清理）
DIGEST_KEEP = 30         # 本地保留最近 30 份 digest


# ------------------------------------------------------------
# 状态读写
# ------------------------------------------------------------
def _load_state() -> Dict[str, Any]:
    try:
        if STATE_PATH.exists():
            return json.loads(STATE_PATH.read_text(encoding="utf-8")) or {}
    except Exception as e:  # 状态文件损坏不该让调度器瘫掉
        logger.warning(f"读取 digest 状态失败: {e}")
    return {}


def _save_state(state: Dict[str, Any]) -> None:
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception as e:
        logger.warning(f"写入 digest 状态失败: {e}")


# ------------------------------------------------------------
# 取数（全部只读；任一段失败只降级，不抛）
# ------------------------------------------------------------
def _pipeline_window(days: int) -> Dict[str, Any]:
    """从 status.json 的 history 统计窗口内流水线次数/失败明细。"""
    out: Dict[str, Any] = {"总次数": 0, "成功": 0, "失败": 0,
                           "失败明细": [], "窗口内无记录": False}
    try:
        state = json.loads(STATUS_FILE.read_text(encoding="utf-8")) or {}
    except Exception:
        out["窗口内无记录"] = True
        out["说明"] = "status.json 不存在或不可读"
        return out

    cutoff = datetime.now() - timedelta(days=days)
    seen = 0
    oldest: Optional[datetime] = None
    for entry in state.get("history") or []:
        raw = entry.get("time") or ""
        try:
            ts = datetime.fromisoformat(str(raw)[:19])
        except Exception:
            continue
        if oldest is None or ts < oldest:
            oldest = ts
        if ts < cutoff:
            continue
        seen += 1
        out["总次数"] += 1
        if entry.get("success"):
            out["成功"] += 1
        else:
            out["失败"] += 1
            if len(out["失败明细"]) < 8:
                out["失败明细"].append({
                    "彩种": entry.get("lottery") or "-",
                    "时间": str(raw)[:19],
                    "摘要": str(entry.get("message") or "")[:80],
                })
    # 诚实边界：history 是旋转窗口（MAX_HISTORY 条），若它已被写满且最早一条
    # 仍落在回看窗口内，说明更早的记录已被挤掉 ⇒ 本窗口的计数是「至少这么多」，
    # 不能当成全量。宁可写成不确定，也不假装完整。
    try:
        from data.automation_status import MAX_HISTORY
        hist_len = len(state.get("history") or [])
        if hist_len >= MAX_HISTORY and oldest is not None and oldest >= cutoff:
            out["计数可能偏少"] = True
            out["说明"] = (f"history 已满 {MAX_HISTORY} 条且最早一条({oldest:%m-%d %H:%M})"
                          f"仍在窗口内，更早记录已旋转淘汰")
    except Exception:
        pass
    if seen == 0:
        out["窗口内无记录"] = True
        out["说明"] = (f"history 里没有 {days} 天内的记录（窗口只保留最近 "
                       f"{len(state.get('history') or [])} 条，可能是长期没跑）")
    return out


def _pending_status() -> Dict[str, Any]:
    """下一期 pending 是否齐、目标期号是否已过。复用 groups_invariant 的口径。"""
    try:
        from web.utils import groups_invariant
        inv = groups_invariant()
    except Exception as e:
        return {"可用": False, "说明": f"注数体检不可用: {e}"}

    missing = [r["彩种"] for r in inv.get("彩种", []) if r.get("状态") == "无预测"]
    bad = [f"{r['彩种']} {r.get('组数')}/{r.get('滑轨')}"
           for r in inv.get("彩种", []) if r.get("状态") == "不一致"]
    return {
        "可用": True,
        "全部一致": bool(inv.get("全部一致")),
        "不一致": bad,
        "无预测": missing,
        "彩种": inv.get("彩种", []),
    }


def _market_freshness() -> Dict[str, Any]:
    """market.db 旁路库的新鲜度（每日 23:10 由调度器补库）。

    为什么要进日报：这个库里的**分省头奖、出球顺序**是 CSV 里根本没有的字段，
    补库一旦悄悄停掉，界面上照样能翻旧数据，肉眼发现不了 —— 只有日报能看出来。
    但它终究是可删可重建的旁路库，所以"从未启用"不当故障，只如实展示。
    """
    try:
        from market.update import last_update
    except Exception as e:
        return {"可用": False, "说明": "market 模块不可用: %s" % str(e)[:80]}

    info = last_update()
    if not info.get("available"):
        return {
            "可用": False,
            "说明": info.get("reason") or "不可用",
            "最新开奖日": info.get("latest_draw_date"),
        }

    # 正在跑的时候不能报"停滞"：全量抓取在晚间高峰能跑到 20 分钟以上，
    # 这期间 last_update 还停在上一轮的记录上，按时间判会误报。
    running = False
    try:
        from market.update import is_running
        running = bool(is_running())
    except Exception:
        pass

    return {
        "可用": True,
        "最近更新": info.get("finished_at"),
        "距今小时": info.get("age_hours"),
        "状态": info.get("status"),
        "失败源": info.get("failed") or [],
        "进行中": running,
        "更新期数": info.get("rows"),
        "最新开奖日": info.get("latest_draw_date"),
        "抓取时间": info.get("latest_fetch"),
    }


def _data_lag() -> List[Dict[str, Any]]:
    """各彩种数据滞后天数（0 = 最新）。"""
    from config import LOTTERY_CONFIG
    rows: List[Dict[str, Any]] = []
    for lot in LOTTERY_CONFIG:
        try:
            from web.utils import get_data_status
            st = get_data_status(lot) or {}
            rows.append({
                "彩种": lot,
                "最新期": st.get("latest_draw"),
                "最新日期": st.get("latest_date"),
                "滞后天数": st.get("data_lag_days"),
            })
        except Exception as e:
            rows.append({"彩种": lot, "错误": str(e)[:60]})
    return rows


def _latest_health_report() -> Dict[str, Any]:
    """找最近一份体检报告，报告它有几个真实问题（不重跑扫描）。"""
    try:
        files = sorted(HEALTH_DIR.glob("项目体检_*.json"))
    except Exception:
        files = []
    if not files:
        return {"可用": False, "说明": "从未跑过全量体检"}
    p = files[-1]
    try:
        d = json.loads(p.read_text(encoding="utf-8")) or {}
        probs = d.get("真实问题") or []
        return {"可用": True, "日期": d.get("日期") or p.stem, "问题数": len(probs),
                "样例": [f"{a}：{b}" for a, b in probs[:5]]}
    except Exception as e:
        return {"可用": False, "说明": f"体检报告不可读: {e}"}


# ------------------------------------------------------------
# 组装
# ------------------------------------------------------------
# ------------------------------------------------------------------
# 策略鲁棒性（夜间跑一次、落盘；build_digest 只读缓存）
# ------------------------------------------------------------------
ROBUSTNESS_PATH = BASE_DIR / "logs" / "automation" / "strategy_robustness.json"
ROBUSTNESS_MAX_AGE_DAYS = 3


def _worst_retention(r: Dict[str, Any]) -> Optional[float]:
    rets = [s.get("保留率") for s in (r.get("情景") or [])
            if isinstance(s.get("保留率"), (int, float))]
    return min(rets) if rets else None


def strategy_robustness() -> Dict[str, Any]:
    """读取策略鲁棒性缓存（夜间 refresh 写入）。**纯只读、快**，可在 Web 请求里调。"""
    try:
        if not ROBUSTNESS_PATH.exists():
            return {"可用": False, "原因": "尚未运行（夜间日报后生成）"}
        d = json.loads(ROBUSTNESS_PATH.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        return {"可用": False, "原因": f"读取失败：{type(e).__name__}: {e}"[:200]}

    try:
        gen = datetime.fromisoformat(str(d.get("生成时间")))
        age = (datetime.now() - gen).total_seconds() / 86400.0
        if age > ROBUSTNESS_MAX_AGE_DAYS:
            return {"可用": False,
                    "原因": f"缓存已 {age:.1f} 天未更新（> {ROBUSTNESS_MAX_AGE_DAYS} 天）",
                    "生成时间": d.get("生成时间")}
    except Exception:                                        # noqa: BLE001
        pass

    # 老缓存没有口径字段 ⇒ 显式写「未标注 + 原因」，不许静默留空（缺席≠通过）。
    from ev.profit.ev_basis import UNANNOTATED_REASON

    out: Dict[str, Any] = {
        "可用": True, "生成时间": d.get("生成时间"), "彩种": {},
        "EV口径": d.get("EV口径") or UNANNOTATED_REASON,
        "历史读数说明": d.get("历史读数说明") or UNANNOTATED_REASON,
    }
    for lot, r in (d.get("彩种") or {}).items():
        out["彩种"][lot] = {
            "判决": r.get("判决"),
            # ★ 老缓存里没有 `已测` 字段 ⇒ 视为 True（那时只有真跑出来的结果才会写进缓存）
            "已测": bool(r.get("已测", True)),
            "基准优势_元每注": r.get("基准优势_元每注"),
            "最差保留率": _worst_retention(r),
            "前视检查": r.get("前视检查"),
            "原因": r.get("原因"),
            # 以下三项只给看板用：日报/推送不读，但页面上要能看见「优势随失真衰减」的整条曲线，
            # 否则只有一个「最差保留率」的孤立数字，读者无法判断它是在哪一档掉下来的。
            "期号": r.get("期号"),
            "K": r.get("K"),
            "判据版本": r.get("判据版本"),
            "情景": list(r.get("情景") or []),
        }
    lots = out["彩种"]
    out["已检验"] = [k for k, v in lots.items() if v["已测"]]
    out["未纳入"] = [k for k, v in lots.items() if not v["已测"]]
    return out


def _latest_target_issue(lottery: str) -> Optional[str]:
    try:
        from data import feedback as fb
        prod = fb.latest_production_pending(fb.load_pending(lottery))
        return (prod or {}).get("目标期号")
    except Exception:                                        # noqa: BLE001
        return None


def _robustness_skip_reason(lottery: str) -> Optional[str]:
    """这个彩种**为什么结构上不做**压力检验；返回 `None` 表示可测。

    ★ 必须显式给理由，不许靠「结果表里没有它」来表达 —— 缺席会被读成「测过且通过」。
      这是本项目反复踩的那条：「缺席 ≠ 通过」。
    ★ 判据只用**结构性事实**（有没有浮动奖）+ 倾斜来源是否过 Go 门槛，不做任何近似测量。
    ★ 读取出错**不在这里**吞掉（不返回理由）：那是「这次没读成」，不是「没有可测的东西」。
      两者混用会把一次临时故障永久写成「不适用」，见 `refresh_strategy_robustness` 的
      `不可判` 分支。
    """
    from ev.profit import tilt as T
    from ev.profit.rules import rules_for

    rules = rules_for(lottery)
    if not any(g.kind != "fixed" for g in rules.grades.values()):
        return ("无浮动奖 ⇒ 优势只可能来自分奖，本彩种结构性优势恒为 0"
                "（不是测出来为 0，是没有可测的东西）")

    tl = T.outer_tilt(lottery)
    if tl is None:
        return "倾斜不可用（热度模型未达 Go 门槛或不支持该彩种）⇒ 策略与随机无差别"

    src = str(getattr(tl, "source", "") or "")
    if not (src.startswith("measured_GO") or src.startswith("walk_forward")):
        return (f"倾斜来源 {src or '未知'} 是未检验代理（无 Go 门槛）"
                "⇒ 不许它主导选票，检验也没有意义")
    return None


def refresh_strategy_robustness(lotteries: Optional[Sequence[str]] = None,
                                K: int = 20) -> Dict[str, Any]:
    """跑一次策略压力测试并落盘（夜间日报调用；双色球约 30~60 秒）。**永不抛出**。

    ★ `lotteries=None` ⇒ **覆盖全部彩种**：能测的测，不能测的**写明理由**存进同一份缓存。
      只写「测了的那个」会让看板显示成一行，读者无从知道另外几个是没测还是通过了。
    ★ 实际只跑「有可测优势」的彩种：无浮动奖（数字型）、热度模型未达 Go 门槛（大乐透、
      七乐彩、快乐8）、倾斜来源是未检验代理（排列3/排列5/七星彩）⇒ 全部只记理由。
    """
    from ev.profit import stress as ST
    from ev.profit.ev_basis import annotate

    if lotteries is None:
        from config import LOTTERY_CONFIG

        lotteries = list(LOTTERY_CONFIG)

    # ★ 口径标注：EV 绝对水平在 2026-11-04 修过一次（旧读数约真实的 1/5），
    #   不标口径的话，新旧缓存里的「元/注」会被当成同一把尺子直接比较。
    out: Dict[str, Any] = annotate({
        "生成时间": datetime.now().isoformat(timespec="seconds"), "彩种": {}})
    for lot in lotteries:
        try:
            reason = _robustness_skip_reason(lot)
        except Exception as e:                               # noqa: BLE001
            # 读取失败 ≠ 结构性不适用：标 `不可判`，不要写成「没东西可测」
            out["彩种"][lot] = {"彩种": lot, "判决": "不可判", "已测": False,
                                "原因": f"适用范围判定失败：{type(e).__name__}: {e}"[:200],
                                "情景": []}
            continue
        if reason:
            out["彩种"][lot] = {"彩种": lot, "判决": "不适用", "已测": False,
                                "基准优势_元每注": None, "情景": [], "原因": reason}
            continue
        try:
            issue = _latest_target_issue(lot)
            if not issue:
                # 没期号 ⇒ 根本没跑 ⇒ 与「不适用」一样按**未纳入**计（带理由，不静默消失）
                out["彩种"][lot] = {"彩种": lot, "判决": "不可判", "已测": False,
                                    "原因": "无可用目标期号", "情景": []}
                continue
            rec = dict(ST.stress_test(lot, issue, K=K))
            rec["已测"] = True
            out["彩种"][lot] = rec
        except Exception as e:                               # noqa: BLE001
            out["彩种"][lot] = {"彩种": lot, "判决": "不可判", "已测": False,
                                "原因": f"{type(e).__name__}: {e}"[:200], "情景": []}
    try:
        ROBUSTNESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        ROBUSTNESS_PATH.write_text(
            json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    except Exception as e:                                   # noqa: BLE001
        logger.warning(f"策略鲁棒性落盘失败: {e}")
    return out


def build_digest(days: int = WINDOW_DAYS) -> Dict[str, Any]:
    """汇总过去 N 天的运行事实。纯只读，可在 Web 请求里直接调。

    注意开销：`_data_lag()` 要读六个彩种的 CSV（实机约 3s）。给**接口**用的
    是下面的 `cached_digest()`（60s 缓存），日报走 `run_digest()` 不走缓存。
    """
    days = max(1, min(int(days or WINDOW_DAYS), 30))
    now = datetime.now()

    pipeline = _pipeline_window(days)
    pending = _pending_status()
    lag = _data_lag()
    health = _latest_health_report()
    market = _market_freshness()
    robustness = strategy_robustness()

    problems: List[Dict[str, str]] = []
    for it in pipeline.get("失败明细") or []:
        problems.append({"彩种": it["彩种"], "项": "流水线失败",
                         "详情": f"{it['时间']} {it['摘要']}"})
    for x in pending.get("不一致") or []:
        problems.append({"彩种": x.split(" ")[0], "项": "注数不一致", "详情": x})
    for lot in pending.get("无预测") or []:
        problems.append({"彩种": lot, "项": "下一期无预测", "详情": "pending 为空"})
    for r in lag:
        d = r.get("滞后天数")
        if isinstance(d, int) and d > 0:
            problems.append({"彩种": r["彩种"], "项": "数据滞后",
                             "详情": f"{d} 天（最新 {r.get('最新期')}）"})
    if health.get("可用") and health.get("问题数"):
        problems.append({"彩种": "全量体检", "项": "历史遗留",
                         "详情": f"{health['日期']} 报告有 {health['问题数']} 项待处理"})

    # 开奖数据库（旁路库）补库健康度
    if market.get("可用"):
        if market.get("状态") in ("fail", "partial"):
            srcs = "、".join(market.get("失败源") or []) or "-"
            problems.append({"彩种": "开奖数据库", "项": "补库失败",
                             "详情": "%s 失败源：%s" % (market.get("最近更新"), srcs)})
        age = market.get("距今小时")
        # 36 小时 = 连续两个 23:10 档都没成功；比"一天没跑"更保守，
        # 避免刚过点（比如 23:20）就被误报成停滞。
        # 正在跑时一律不报 —— 长任务会把 last_update 停在上一轮，按时间判必误报。
        if isinstance(age, (int, float)) and age > 36 and not market.get("进行中"):
            problems.append({"彩种": "开奖数据库", "项": "补库停滞",
                             "详情": f"已 {age} 小时未更新（最新开奖日 {market.get('最新开奖日')}）"})

    return {
        "生成时间": now.isoformat(timespec="seconds"),
        "窗口天数": days,
        "窗口起": (now - timedelta(days=days)).date().isoformat(),
        "结论": "正常" if not problems else f"有 {len(problems)} 项需要处理",
        "问题": problems,
        "流水线": pipeline,
        "注数一致性": pending,
        "数据滞后": lag,
        "全量体检": health,
        "开奖数据库": market,
        "策略鲁棒性": robustness,
    }


# ------------------------------------------------------------
# 落盘 / 推送
# ------------------------------------------------------------
_CACHE: Dict[str, Any] = {}
_CACHE_TTL = 60.0


def cached_digest(days: int = WINDOW_DAYS) -> Dict[str, Any]:
    """接口专用：60 秒缓存。

    不加缓存时每次看板加载都要读六个彩种 CSV（实机 ~3s），
    前端那个摘要徽章会把页面拖慢到肉眼可感。60s 对"过去 7 天"这个粒度毫无损失。
    """
    import time as _t
    key = str(int(days))
    hit = _CACHE.get(key)
    now = _t.time()
    if hit and now - hit[0] < _CACHE_TTL:
        return hit[1]
    d = build_digest(days)
    _CACHE[key] = (now, d)
    return d


def _write_digest(d: Dict[str, Any]) -> Path:
    DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    p = DIGEST_DIR / f"digest_{stamp}.json"
    p.write_text(json.dumps(d, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    # 只保留最近 DIGEST_KEEP 份（按文件名日期倒序）
    try:
        olds = sorted(DIGEST_DIR.glob("digest_*.json"))[:-DIGEST_KEEP]
        for f in olds:
            f.unlink(missing_ok=True)
    except Exception as e:
        logger.warning(f"清理旧 digest 失败: {e}")
    return p


def run_digest(*, notify: bool = True, days: int = WINDOW_DAYS,
               refresh_stress: bool = False) -> Dict[str, Any]:
    """生成并落盘一份日报；有问题时推送。返回 digest 字典。

    `refresh_stress=True`（夜间调度传）⇒ 先跑一次策略鲁棒性并落盘，供 `build_digest` 读取。
    默认关闭：冷启补报与单测不该被 30~60 秒的压力测试拖慢。
    """
    if refresh_stress:
        try:
            refresh_strategy_robustness()
        except Exception as e:                               # noqa: BLE001
            logger.warning(f"策略鲁棒性刷新失败(降级): {e}")
    d = build_digest(days)
    try:
        d["本地文件"] = str(_write_digest(d))
    except Exception as e:
        d["本地文件"] = ""
        logger.warning(f"digest 落盘失败: {e}")

    d["已推送"] = False
    if notify:
        try:
            from data.push_notify import push_digest
            res = push_digest(d)
            d["已推送"] = bool(res and res.get("ok"))
        except Exception as e:
            # 推送失败绝不影响体检本身（沿用项目一贯的降级约定）
            logger.warning(f"digest 推送失败(降级): {e}")
    return d


def run_full_health(*, notify: bool = True) -> Dict[str, Any]:
    """调 scripts/health_check.py 的六项全扫描（重活，一周一次）。"""
    try:
        from scripts.health_check import main as health_main
        health_main()
        return {"ok": True}
    except Exception as e:
        logger.warning(f"全量体检失败: {e}")
        return {"ok": False, "error": str(e)[:200]}


# ------------------------------------------------------------
# 调度入口
# ------------------------------------------------------------
def is_due(now: Optional[datetime] = None, *, cold_start: bool = False) -> bool:
    """毫秒级判断「现在该跑吗」，供调度器先判定再决定要不要起线程。

    只读状态文件，不做任何重活 —— 它每 60s 被调用一次。
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    st = _load_state()
    if cold_start and st.get("last_boot") != today:
        return True
    if (now.hour, now.minute) >= (DIGEST_HOUR, DIGEST_MINUTE) and st.get("last_daily") != today:
        return True
    return False


def maybe_run(now: Optional[datetime] = None, *, cold_start: bool = False,
              notify: bool = True,
              refresh_stress: bool = False) -> Optional[Dict[str, Any]]:
    """到点就跑，同一时点一天只跑一次。

    两个独立时点（互不挤占）：
      - boot  ：进程启动补报（`cold_start=True`），让开机第一眼就有结论
      - daily ：每日 DIGEST_HOUR 之后的当日总结
    另有每周日 FULL_HEALTH_WEEKDAY 的全量扫描。
    """
    now = now or datetime.now()
    today = now.date().isoformat()
    st = _load_state()
    ran: List[str] = []
    dirty = False

    if cold_start and st.get("last_boot") != today:
        try:
            run_digest(notify=notify)
        except Exception as e:
            logger.warning(f"冷启补报失败: {e}")
        else:
            st["last_boot"] = today
            ran.append("boot")
            dirty = True

    due_daily = (now.hour, now.minute) >= (DIGEST_HOUR, DIGEST_MINUTE)
    if due_daily and st.get("last_daily") != today:
        try:
            run_digest(notify=notify, refresh_stress=refresh_stress)
        except Exception as e:
            logger.warning(f"日报失败: {e}")
        else:
            st["last_daily"] = today
            ran.append("daily")
            dirty = True

        if now.weekday() == FULL_HEALTH_WEEKDAY and st.get("last_full") != today:
            try:
                run_full_health(notify=notify)
            except Exception as e:
                logger.warning(f"周检失败: {e}")
            else:
                st["last_full"] = today
                ran.append("full_health")
                dirty = True
            # 周日顺带清一次旧日志（严格日期命名，保留 LOG_RETENTION_DAYS 天）
            try:
                from scripts.prune_logs import apply as _prune_logs
                res = _prune_logs(LOG_RETENTION_DAYS)
                if res.get("删除"):
                    logger.info(f"日志清理: 删除 {res['删除']} 个，"
                                f"释放 {res['释放KB']} KB（保留 {res['保留天数']} 天）")
            except Exception as e:
                logger.warning(f"日志清理失败(降级): {e}")

    if dirty:
        _save_state(st)
    return {"已执行": ran} if ran else None
