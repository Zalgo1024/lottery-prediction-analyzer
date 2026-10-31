# -*- coding: utf-8 -*-
"""ev/budget_gate.py —— 生产注数的**预算闸门**（任务 2：把注数增长纳入风险门）。

它解决的具体问题
----------------
`ev/budget_envelope.py` 已经把「少亏」做成了纪律（定额 / 单期上限 / 亏完即停 /
默认不参与），但**生产完全没接**：`config.py::resolve_groups` 直接读滑轨，
`cli.py predict --groups 2000` 甚至完全不夹取。于是「注数」在生产上是无上限的。

本模块只做一件事：**把想要的注数夹到一个可执行注数**，并把「夹过」的说清楚。
它不选号、不改任何一注的中奖概率、不判断「该不该买」——那由包络的
`gate_verdict()` 回答，本模块只负责**额度**。

★ 三条安全属性（与包络一脉相承）
  ① **只降不升**：`clamp()` 的返回值永远 ≤ 请求值；`auto` 来源还额外 ≤ 基线注数
     （自动路径永不加注 —— `E[中奖注数]=K·p`，加注只放大方差）。
  ② **不静默 fail-open**：`启用=false` 时放行并**显式**返回 `受限=false / 闸门未启用`；
     `启用=true` 但 `月度总额` 为空 ⇒ **抛错拒绝**。绝不允许「配置写坏了就当成没上限」。
  ③ **幂等记账**：键 = (月份, 目标期号, 档位, 台账域, 彩种)，重复生成是**覆盖**不是叠加；
     `已投` 由明细**重算**（不是累加），所以重跑流水线不会把月度额度虚增成两倍。

★ 为什么「重生成不递缩」
  同一期被重算时，那条记录本来就要被覆盖。若先把旧记录算进「已投」再算额度，
  第二次调用的可用额度就已经少了一笔 —— 反复触发自愈会让注数**单调塌到 0**。
  故 `clamp()` 先扣掉「本彩种本月最近一条记录」的金额，再算额度。

★ 本模块不写 pending、不触碰 `data/ticket_size.json`（注数仍由滑轨决定，
  闸门只可能把它压小）。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config import LOTTERY_CONFIG, ticket_price
from ev.mechanism.narrative import EV_NEGATIVE_CAVEAT

logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent.parent

# 人编辑的额度文件（唯一改额度的地方）。放 config/ 且**不进仓库**：
# .gitignore 的 config/*.json 规则会排除它，额度属于本机运行状态。
LIMIT_PATH = _BASE_DIR / "config" / "budget_aggregate.json"

# 运行时台账（「本月已投」）。同属本机状态。
SPEND_PATH = _BASE_DIR / "config" / "budget_spend.json"

SOURCES = ("auto", "manual", "research")

HONEST_NOTE = (
    "本闸门只封顶**投入**，不改变任何一注的中奖概率：按恒等式 E[中奖注数]=K·p，"
    "加注不改变单注期望，只放大方差与回撤。"
    "「未触发闸门」不等于「该投入是划算的」。" + EV_NEGATIVE_CAVEAT
)


class GateConfigError(RuntimeError):
    """额度配置坏了。**一律抛，不 fail-open。**"""


# ------------------------------------------------------------------ 读配置
# 配置缓存。★ 键里必须带**路径**：只按 mtime 缓存时，换一个文件（测试隔离、
# 切换额度档）恰好同 mtime 就会读到上一个文件的额度 —— 一个不会报错的错额度。
_LIMIT_CACHE: Dict[str, Any] = {"key": None, "doc": None}


def load_limit(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """读额度配置（按「路径 + mtime」缓存）。坏文件抛 `GateConfigError`。

    ★ 为什么缓存：`resolve_groups()` 被 UI 高频调用
      （`web/utils.py` / 健康检查 / 快照都走它）。每次 `stat` + 一次 JSON 读是
      微秒级，而「每次都解析 + 校验」会把这些调用点变成热点。
    """
    p = Path(path) if path is not None else LIMIT_PATH
    if not p.exists():
        # 缺文件 = 闸门未启用（与 `启用=false` 同义）。这是**默认状态**，不是错误：
        # 机制装好但没启用时，行为必须与接入前一致。
        return {"版本": 1, "启用": False, "月度总额": None, "单期上限": None,
                "锁定": True, "备注": "未创建额度文件 ⇒ 闸门未启用"}
    try:
        mt = p.stat().st_mtime
    except OSError:
        mt = None
    key = (str(p), mt)
    if _LIMIT_CACHE["doc"] is not None and _LIMIT_CACHE["key"] == key:
        return dict(_LIMIT_CACHE["doc"])
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        raise GateConfigError(f"额度文件解析失败：{p}：{e}") from e
    if not isinstance(doc, dict):
        raise GateConfigError(f"额度文件顶层必须是对象：{p}")
    doc.setdefault("启用", False)
    _LIMIT_CACHE.update({"key": key, "doc": doc})
    return dict(doc)


def _money(v: Any) -> Optional[float]:
    """金额字段：None / "" / 非法 → None（**不静默当 0**）。"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f >= 0 else None


def enabled_limit(*, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """取「已启用且配置完整」的额度；未启用返回 None。

    ★ `启用=true` 但月度总额缺失 ⇒ **抛**。这是唯一允许的失败方式：
      静默 fail-open 会让一次配置手误变成「没有上限」，而系统不会有任何提示。
    """
    lim = load_limit(path=path)
    if not lim.get("启用"):
        return None
    monthly = _money(lim.get("月度总额"))
    if monthly is None:
        raise GateConfigError(
            "闸门已启用（启用=true）但 `月度总额` 为空/非法 ⇒ 拒绝出号。"
            "★ 不允许静默 fail-open：没有总额的闸门等于没有闸门。")
    cap = _money(lim.get("单期上限"))
    return {"月度总额": monthly,
            "单期上限": (monthly if cap is None else min(cap, monthly)),
            "锁定": bool(lim.get("锁定", True)),
            "备注": lim.get("备注") or "",
            "更新于": lim.get("更新于")}


# ------------------------------------------------------------------ 台账读写
def _empty_spend(month: str) -> Dict[str, Any]:
    return {"版本": 2, "月份": month, "记录": [], "已投": 0.0, "停发": []}


def _month_of(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m")


def load_spend(*, path: Optional[Path] = None, now: Optional[datetime] = None
               ) -> Dict[str, Any]:
    """读台账；跨月**归零**（不结转、不预支）。"""
    p = Path(path) if path is not None else SPEND_PATH
    month = _month_of(now)
    if not p.exists():
        return _empty_spend(month)
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                   # noqa: BLE001
        # 台账坏了不能影响出号：退回空账并记日志（额度只会更保守：从 0 起算，
        # 相当于「本月还没花」，这可能**放宽**额度 ⇒ 必须显式标出来给调用方看）
        logger.error(f"预算台账解析失败，按空账处理（本月已投按 0 计）：{p}：{e}")
        d = _empty_spend(month)
        d["★台账损坏"] = f"{type(e).__name__}: {e}"
        return d
    if not isinstance(doc, dict) or doc.get("月份") != month:
        return _empty_spend(month)
    doc.setdefault("记录", [])
    doc["已投"] = round(sum(float(r.get("金额") or 0.0) for r in doc["记录"]), 2)
    return doc


def _atomic_write(path: Path, doc: Dict[str, Any]) -> None:
    """原子写：先写临时文件再 `os.replace`（半截文件=台账损坏的经典来源）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _rec_key(rec: Dict[str, Any]) -> tuple:
    """幂等键：(月份, 目标期号, 档位, 台账域, 彩种)。

    ★ 与 `data/feedback.py::_pending_key` 同构 + 彩种：
      同构是为了「台账记录」与「pending 记录」能一一对上；
      加彩种是因为六彩种共用一个月度总额。
    """
    return (str(rec.get("月份") or ""),
            str(rec.get("目标期号") or ""),
            str(rec.get("档位") or "一般"),
            str(rec.get("台账域") or "生产"),
            str(rec.get("彩种") or ""))


def record_spend(lottery: str, *, 目标期号: Any, 注数: int,
                 档位: str = "一般", 台账域: str = "生产",
                 单价: Optional[float] = None,
                 path: Optional[Path] = None,
                 now: Optional[datetime] = None) -> Dict[str, Any]:
    """把一次出号记进本月台账（幂等 upsert）。返回更新后的台账摘要。

    ★ 「覆盖不是叠加」：同一 (期号, 档位, 域, 彩种) 重复写只保留一条，
      金额取**最新**那次的注数 —— 因为后写的才是最终真正要出的号。
    """
    price = float(ticket_price(lottery) if 单价 is None else 单价)
    n = max(0, int(注数))
    p = Path(path) if path is not None else SPEND_PATH
    doc = load_spend(path=p, now=now)
    month = doc["月份"]
    rec = {"月份": month, "彩种": lottery, "目标期号": str(目标期号),
           "档位": str(档位 or "一般"), "台账域": str(台账域 or "生产"),
           "注数": n, "单价": price, "金额": round(n * price, 2),
           "时间": (now or datetime.now()).isoformat(timespec="seconds")}
    key = _rec_key(rec)
    kept = [r for r in doc["记录"] if _rec_key(r) != key]
    kept.append(rec)
    doc["记录"] = kept
    doc["已投"] = round(sum(float(r.get("金额") or 0.0) for r in kept), 2)
    _atomic_write(p, doc)
    return {"月份": month, "已投": doc["已投"], "本条金额": rec["金额"],
            "记录数": len(kept)}


def spent_this_month(*, path: Optional[Path] = None,
                     now: Optional[datetime] = None) -> float:
    """本月已投（元）。跨月自动归零。"""
    return float(load_spend(path=path, now=now).get("已投") or 0.0)


# ------------------------------------------------------------------ 夹取
def _latest_same_month(doc: Dict[str, Any], lottery: str) -> Optional[Dict[str, Any]]:
    rows = [r for r in doc.get("记录") or [] if str(r.get("彩种")) == lottery]
    if not rows:
        return None
    rows.sort(key=lambda r: str(r.get("时间") or ""))
    return rows[-1]


def clamp(lottery: str, want: int, *, baseline: Optional[int] = None,
          source: str = "auto", 目标期号: Any = None,
          path: Optional[Path] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """把「想买多少注」夹成「可以买多少注」。

    返回 dict（可直接进日志/界面）：
      `注数`（最终可执行）、`受限`（是否被夹）、`原因`、`请求注数`、`基线注数`、
      `已投元`、`本彩种本月已记元`、`剩余额度元`、`月度总额`、`单期上限`、`单价`、`来源`

    ★ 三种来源
      · `auto`    —— 调度器 / worker / 自愈：强行夹取，**且不超过基线**（永不自动加注）；
      · `manual`  —— 用户在界面上显式出号：夹取（额度是硬约束），但可以**超过基线**；
      · `research`—— 回测 / 训练：**豁免**（不占生产账），但返回值里显式标 `豁免=true`。
    """
    if source not in SOURCES:
        raise ValueError(f"未知来源 {source!r}（只能是 {SOURCES}）")
    w = max(0, int(want))
    price = float(ticket_price(lottery))

    def _out(n: int, limited: bool, reason: str, **kw: Any) -> Dict[str, Any]:
        out = {"彩种": lottery, "注数": int(n), "受限": bool(limited), "原因": reason,
               "请求注数": w, "基线注数": (None if baseline is None else int(baseline)),
               "单价": price, "来源": source, "豁免": source == "research"}
        out.update(kw)
        if int(n) == 0:
            out["停发"] = True
        return out

    if source == "research":
        return _out(w, False, "研究模式：不占生产账，不做额度夹取")

    lim = enabled_limit(path=path)
    if lim is None:
        base_cap = w
        if source == "auto" and baseline is not None:
            base_cap = min(w, int(baseline))
        return _out(base_cap, base_cap != w,
                    ("闸门未启用" if base_cap == w else "闸门未启用；auto 来源不超过基线"),
                    月度总额=None, 单期上限=None, 已投元=None, 剩余额度元=None)

    monthly, cap = float(lim["月度总额"]), float(lim["单期上限"])
    doc = load_spend(path=path, now=now)
    spent = float(doc.get("已投") or 0.0)

    # ★ 重生成不递缩：本期这条记录本来就会被覆盖 ⇒ 先把它的金额加回可用额度。
    #   ★★ 键必须**精确**：早期实现写成「目标期号 is None 就豁免最近一条」，
    #   等于给每个彩种开了一个无上限额度（最近那条的金额永远被加回来），
    #   而且不会报错 —— 故缺省时先解析出「本次要出的那一期」，解析不到就不豁免。
    issue = 目标期号
    if issue is None:
        try:
            from data.feedback import _next_issue
            issue = _next_issue(lottery)
        except Exception:                                     # noqa: BLE001
            issue = None
    last = _latest_same_month(doc, lottery)
    rebate = 0.0
    if last is not None and issue is not None \
            and str(last.get("目标期号")) == str(issue):
        rebate = float(last.get("金额") or 0.0)

    remaining = max(0.0, monthly - spent + rebate)
    n_month = int(remaining // price) if price > 0 else 0
    n_cap = int(cap // price) if price > 0 else 0
    n = min(w, n_month, n_cap)

    why: List[str] = []
    if n < w:
        if n_cap < w:
            why.append(f"单期上限 {cap:g} 元 ÷ {price:g} 元 = {n_cap} 注")
        if n_month < w:
            why.append(f"本月剩余 {remaining:g} 元 ÷ {price:g} 元 = {n_month} 注")
    if source == "auto" and baseline is not None and int(baseline) < n:
        n = int(baseline)
        why.append(f"auto 来源不超过基线 {int(baseline)} 注（自动路径永不加注）")

    n = max(0, int(n))
    return _out(n, n != w, "；".join(why) if why else ("未受限" if n == w else "被夹取"),
                月度总额=monthly, 单期上限=cap, 已投元=round(spent, 2),
                剩余额度元=round(remaining, 2), 本彩种本月已记元=round(rebate, 2),
                锁定=bool(lim.get("锁定", True)))


def mark_stopped(lottery: str, reason: str, *, 目标期号: Any = None,
                 path: Optional[Path] = None, now: Optional[datetime] = None) -> Dict[str, Any]:
    """记一条「停发」标记（幂等：同彩种同期号只留一条）。"""
    p = Path(path) if path is not None else SPEND_PATH
    doc = load_spend(path=p, now=now)
    rows = [r for r in (doc.get("停发") or [])
            if not (str(r.get("彩种")) == lottery
                    and str(r.get("目标期号")) == str(目标期号))]
    rows.append({"彩种": lottery, "目标期号": str(目标期号), "原因": str(reason),
                 "时间": (now or datetime.now()).isoformat(timespec="seconds")})
    doc["停发"] = rows
    _atomic_write(p, doc)
    return {"停发条数": len(rows)}


# ------------------------------------------------------------------ 对账
def reconcile(*, path: Optional[Path] = None, now: Optional[datetime] = None,
              lotteries: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """拿生产 pending 核对台账：缺的补记、多的标「账外」。

    ★ 只在流水线起步调用（扫 pending 是要读盘的重活）。
    ★ 「账外」不删除、不自动冲销 —— 它可能是人工补单，也可能是上游 bug；
      两种都不该由一个对账脚本替人决定。
    """
    from data.feedback import (DOMAIN_PROD, _domain_of, _tier_of, _next_issue,
                               load_pending, latest_production_pending)

    p = Path(path) if path is not None else SPEND_PATH
    doc = load_spend(path=p, now=now)
    month = doc["月份"]
    known = {_rec_key(r) for r in doc.get("记录") or []}
    added: List[Dict[str, Any]] = []
    extra: List[Dict[str, Any]] = []

    for lot in (lotteries or list(LOTTERY_CONFIG)):
        try:
            pend = load_pending(lot)
        except Exception as e:                                # noqa: BLE001
            logger.warning(f"对账：{lot} 读 pending 失败：{e}")
            continue
        for rec in pend or []:
            if _domain_of(rec) != DOMAIN_PROD:
                continue
            when = str(rec.get("生成时间") or rec.get("记录时间") or "")
            if not when.startswith(month):
                continue
            tickets = rec.get("预测号码") or []
            item = {"月份": month, "彩种": lot,
                    "目标期号": str(rec.get("目标期号") or ""),
                    "档位": _tier_of(rec), "台账域": DOMAIN_PROD,
                    "注数": len(tickets), "单价": float(ticket_price(lot)),
                    "金额": round(len(tickets) * float(ticket_price(lot)), 2),
                    "时间": when, "来源": "对账补记"}
            k = _rec_key(item)
            if k in known:
                continue
            known.add(k)
            doc["记录"].append(item)
            added.append(item)

    for r in doc.get("记录") or []:
        if r.get("来源") == "对账补记":
            continue
        pend = None
        try:
            pend = latest_production_pending(
                load_pending(str(r.get("彩种"))), target_issue=r.get("目标期号"))
        except Exception:                                     # noqa: BLE001
            pass
        if pend is None:
            extra.append({"彩种": r.get("彩种"), "目标期号": r.get("目标期号"),
                          "金额": r.get("金额"), "原因": "pending 里找不到对应生产批"})

    doc["已投"] = round(sum(float(x.get("金额") or 0.0) for x in doc["记录"]), 2)
    if added or extra:
        _atomic_write(p, doc)
    return {"月份": month, "补记条数": len(added), "账外条数": len(extra),
            "已投": doc["已投"], "补记": added[:20], "账外": extra[:20]}


# ------------------------------------------------------------------ 计划
def periods_per_month(lottery: str) -> int:
    """每月开奖期数：乐透型 13 期、数字型 30 期（与 ev/budget_envelope 同口径）。"""
    from ev.budget_envelope import periods_per_month as _ppm
    return int(_ppm(lottery))


def month_plan(lotteries: Optional[Sequence[str]] = None, *,
               groups: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    """按**现行**生效注数折算的月度计划投入（六彩种合计）。

    ★ 这是「现在这套配置一个月要花多少」，不是建议预算；报预算前必须现读
      `resolve_groups()`，不许抄旧表（10-26/10-27 两次实测生产注数与记录差 2.6 倍）。
    """
    from config import resolve_groups

    rows: Dict[str, Any] = {}
    total = 0.0
    for lot in (lotteries or list(LOTTERY_CONFIG)):
        n = int((groups or {}).get(lot) if groups and lot in groups
                else resolve_groups(lot))
        price = float(ticket_price(lot))
        ppm = periods_per_month(lot)
        m = n * price * ppm
        rows[lot] = {"注数": n, "单价": price, "期数每月": ppm,
                     "单期元": round(n * price, 2), "月度元": round(m, 2)}
        total += m
    return {"彩种": rows, "月度合计元": round(total, 2), "单价真源": "config.ticket_price"}
