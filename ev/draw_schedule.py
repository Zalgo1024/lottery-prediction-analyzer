# -*- coding: utf-8 -*-
"""ev/draw_schedule.py —— 方案 A「开奖时刻表」的加载 / 校验 / 时间组装（离线研究侧）。

定位
----
影子验证接线（logs/prefix_shadow_design.md 方案 A）的**前置准备件**。
本模块：

  · 纯函数：所有路径由调用方以参数传入，无模块级落盘常量、无模块级 IO；
  · 不读取任何系统当前时刻，时间一律由参数注入；
  · 不在 ev/__init__.py 登记，零生产调用点，默认不参与出号 / 结算；
  · 不创建任何 config/*.json，不写影子台账，不改 import 护栏。

为什么需要它
------------
全仓（lottery_data/*.csv、market.db 的 draw.draw_date、data/holiday.py、
config/*.json）只有**日级开奖日期**；DRAW_CUTOFF="21:00"、
DAILY_CUTOFF=(21, 0)、WEEKLY_CUTOFF=(21, 30) 是「开奖前 / 已开奖」的粗界，
不是开奖事件时刻；秒级时间戳全是抓取 / 记账 / 日志旁证，不能等同开奖时刻。
而 ev/prefix_shadow.gate_snapshot(draw_time=...) 需要 datetime 级开奖时刻来判定
「批次时间严格早于开奖」，draw_dt 缺失时直接判不合格。

★ 时刻表是**声明值**而非实测。核验状态必须显式写出；标注「未经外部源核验」
  时本模块照常加载并披露，绝不据此声称先后关系已被证明。

纪律
----
  · 分母纪律：按 config.LOTTERY_CONFIG 逐彩种核对，缺席写「未参与，原因=…」，
    不静默过滤、不判通过；「覆盖」给总数 / 参与 / 未参与三数并列；
  · 不 fail-open：缺文件 / 坏 JSON / 结构错 / 时刻非法一律显式抛 ScheduleError；
  · 开奖星期唯一真源 = config.LOTTERY_CONFIG[*].draw_days，不手抄彩种清单；
  · 时区口径单一：批次时间 / 登记时间 / 开奖时间必须同为 naive 或同为 aware，
    混用会让 gate_snapshot 的比较抛 TypeError 而不是判不合格，故在此显式拦截；
  · 日级日期不能充作时刻（DATE 字符串会被 fromisoformat 解析成当日零点）。

本模块不产生任何新统计读数，不改变影子验证「未开判」的当前结论。
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from config import LOTTERY_CONFIG
from data.holiday import is_draw_day

SCHEMA_VERSION = "2026-11-05-A"
UNVERIFIED = "未经外部源核验"
VERIFIED = "已经外部源核验"
TIMEZONE = "Asia/Shanghai"
_UTC_OFFSET = timedelta(hours=8)
_CLOCK_RE = re.compile(r"^(\d{2}):(\d{2})(?::(\d{2}))?$")

DECLARATION = (
    "开奖时刻表是声明值而非实测；未经外部源核验时只提供可比的时间基准，"
    "不构成对开奖时刻的证明，也不代表影子验证已通过。"
)
MIXED_TZ_REASON = (
    "批次时间 / 登记时间 / 开奖时间时区口径混用："
    "gate_snapshot 比较 naive 与 aware 会抛 TypeError，而不是判不合格；"
    "必须三者统一口径。"
)


class ScheduleError(ValueError):
    """时刻表不可用。一律显式抛，绝不 fail-open。"""


def expected_lots() -> Dict[str, Dict[str, Any]]:
    """8 彩种及其开奖星期，唯一真源 = config.LOTTERY_CONFIG（不手写清单）。"""
    return {
        name: {
            "代码": cfg.get("name_en"),
            "开奖星期": list(cfg.get("draw_days") or []),
        }
        for name, cfg in LOTTERY_CONFIG.items()
    }


def input_template() -> Dict[str, Any]:
    """待填模板：彩种与星期按真源预填，**开奖时刻留空**。

    返回的模板不可直接用于 build_draw_time（时刻未填 ⇒ ScheduleError）。
    本函数不代填任何权威时刻数值。
    """
    lots = {
        name: {
            "代码": meta["代码"],
            "开奖星期": list(meta["开奖星期"]),
            "开奖时刻": None,
        }
        for name, meta in expected_lots().items()
    }
    return {
        "版本": SCHEMA_VERSION,
        "时区": TIMEZONE,
        "核验状态": UNVERIFIED,
        "来源": None,
        "声明": DECLARATION,
        "彩种": lots,
    }


def render_template() -> str:
    """把待填模板渲染成 JSON 文本（只读，不落盘）。"""
    return json.dumps(input_template(), ensure_ascii=False, indent=2)


def load_schedule(path: Any) -> Dict[str, Any]:
    """从显式传入的路径读取时刻表；缺文件 / 坏 JSON / 顶层非对象一律抛错。"""
    p = Path(path)
    if not p.exists():
        raise ScheduleError(
            f"时刻表文件缺失：{p}（原因：S1 数值尚未提供；不 fail-open）"
        )
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ScheduleError(f"时刻表读取或解析失败：{p}，原因：{exc}") from exc
    if not isinstance(doc, dict):
        raise ScheduleError(f"时刻表顶层必须是对象，实际为 {type(doc).__name__}")
    return doc


def _parse_clock(value: Any) -> Optional[time]:
    """严格解析 HH:MM 或 HH:MM:SS；日级日期、占位串、越界值一律返回 None。"""
    if not isinstance(value, str):
        return None
    m = _CLOCK_RE.match(value.strip())
    if not m:
        return None
    hour, minute, second = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    if hour > 23 or minute > 59 or second > 59:
        return None
    return time(hour, minute, second)


def _coerce_date(value: Any) -> date:
    """只接受日级 date 或 ISO date 字符串；datetime 会被拒绝（避免静默丢时刻）。"""
    if isinstance(value, datetime):
        raise ScheduleError("开奖日期必须是日级 date，不能传 datetime（会静默丢掉时刻）")
    if isinstance(value, date):
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value.strip())
        except (ValueError, TypeError) as exc:
            raise ScheduleError(f"开奖日期不可解析：{value}") from exc
    raise ScheduleError(f"开奖日期类型不支持：{type(value).__name__}")


def validate_schedule(doc: Any) -> Dict[str, Any]:
    """逐彩种核对时刻表，返回带分母三数与逐条缺席原因的结构。

    结构性错误（缺「彩种」对象 / 时区不符 / 核验状态缺失或自相矛盾 /
    出现未知彩种）⇒ 抛 ScheduleError；逐彩种问题 ⇒ 写进「未参与」，不抛、
    不静默过滤、不判通过。
    """
    if not isinstance(doc, dict):
        raise ScheduleError(f"时刻表必须是对象，实际为 {type(doc).__name__}")
    lots = doc.get("彩种")
    if not isinstance(lots, dict):
        raise ScheduleError("时刻表缺少「彩种」对象（不 fail-open）")

    tz = doc.get("时区")
    if tz != TIMEZONE:
        raise ScheduleError(f"时区口径不符：应为 {TIMEZONE}，实际 {tz!r}")
    state = doc.get("核验状态")
    if state not in (UNVERIFIED, VERIFIED):
        raise ScheduleError(
            f"核验状态缺失或非法：{state!r}；必须显式标 {UNVERIFIED} 或 {VERIFIED}"
        )
    source = doc.get("来源")
    if state == VERIFIED and not source:
        raise ScheduleError("标注「已经外部源核验」时必须给出「来源」")

    truth = expected_lots()
    unknown = [k for k in lots if k not in truth]
    if unknown:
        raise ScheduleError(f"时刻表含未知彩种：{unknown}（彩种清单以 config 为准）")

    rows: Dict[str, Any] = {}
    absent = []
    joined = 0
    for name, meta in truth.items():
        entry = lots.get(name)
        if not isinstance(entry, dict):
            reason = "未参与，原因=彩种在时刻表中缺席"
            rows[name] = {"参与": False, "原因": reason}
            absent.append({"彩种": name, "原因": reason})
            continue
        errs = []
        if list(entry.get("开奖星期") or []) != meta["开奖星期"]:
            errs.append("开奖星期与 config.LOTTERY_CONFIG 不一致（不许手抄清单）")
        code = entry.get("代码")
        if code not in (None, meta["代码"]):
            errs.append(f"彩种代码与 name_en 不一致：{code!r}")
        clk = _parse_clock(entry.get("开奖时刻"))
        if clk is None:
            errs.append("开奖时刻缺失或格式非法（须 HH:MM / HH:MM:SS；日级日期不能充数）")
        if errs:
            reason = "未参与，原因=" + "；".join(errs)
            rows[name] = {"参与": False, "原因": reason}
            absent.append({"彩种": name, "原因": reason})
        else:
            joined += 1
            rows[name] = {
                "参与": True,
                "代码": meta["代码"],
                "开奖星期": list(meta["开奖星期"]),
                "开奖时刻": clk.strftime("%H:%M:%S"),
            }

    total = len(truth)
    return {
        "可用": joined == total,
        "版本": doc.get("版本") or "未标注（老配置，原因：无版本字段）",
        "时区": tz,
        "核验状态": state,
        "来源": source,
        "声明": DECLARATION,
        "覆盖": {"总数": total, "参与": joined, "未参与": total - joined},
        "彩种": rows,
        "未参与": absent,
    }


def build_draw_time(lottery: str, draw_date: Any, doc: Any,
                    *, aware: bool = False) -> str:
    """把「日级开奖日期 + 声明的开奖时刻」组装成 gate_snapshot 需要的 ISO 时间。

    · 日期是否为开奖日走 data.holiday.is_draw_day（星期 + 休市单一真源）；
    · 默认输出 naive ISO；aware=True 时输出带 +08:00 的 ISO；
    · 批次时间 / 登记时间须与返回值同口径，另见 check_time_basis()。
    """
    v = validate_schedule(doc)
    row = v["彩种"].get(lottery)
    if row is None or not row["参与"]:
        raise ScheduleError(f"{lottery} 未参与：{row.get('原因') if row else '未知彩种'}")
    d = _coerce_date(draw_date)
    if not is_draw_day(lottery, d):
        raise ScheduleError(f"{d.isoformat()} 不是 {lottery} 的开奖日（星期 / 休市真源）")
    clk = _parse_clock(row["开奖时刻"])
    if clk is None:
        raise ScheduleError(f"{lottery} 开奖时刻不可解析")
    dt = datetime.combine(d, clk)
    if aware:
        dt = dt.replace(tzinfo=timezone(_UTC_OFFSET))
    return dt.isoformat()


def check_time_basis(*values: Any) -> Dict[str, Any]:
    """校验批次时间 / 登记时间 / 开奖时间的时区口径是否统一。

    混用 naive 与 aware ⇒ 抛 ScheduleError（写清「会抛 TypeError 而不是判
    不合格」），避免把口径错悄悄变成一次「不合格」。
    """
    parsed = []
    for idx, value in enumerate(values, start=1):
        try:
            parsed.append(datetime.fromisoformat(str(value).strip()))
        except (ValueError, TypeError) as exc:
            raise ScheduleError(f"第 {idx} 个时间不可解析：{value!r}") from exc
    kinds = {dt.tzinfo is not None for dt in parsed}
    if len(kinds) > 1:
        raise ScheduleError(MIXED_TZ_REASON)
    kind = "aware" if True in kinds else "naive"
    return {
        "一致": True,
        "口径": kind,
        "数量": len(parsed),
        "声明": "口径一致只表示可比，不代表登记资格已通过；资格由 gate_snapshot 判定。",
    }
