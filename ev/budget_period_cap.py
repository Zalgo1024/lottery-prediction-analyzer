# -*- coding: utf-8 -*-
"""ev/budget_period_cap.py —— 「单期上限」C1/C2/C3 口径的**只读**实现（离线+体检用）。

背景
----
`logs/budget_period_cap_audit.md` 第 6 节留了三项待拍板。C1 已于 2026-11-06
按 ① 收口（`单期上限` = **单彩种 · 单批**，`clamp()` 的 `n_cap = cap // price`）。
本模块实现 C2 / C3，判据写在 `config/budget_period_cap.json`（入库、锁定）：

  · **C2① 补检查**，但拆成两档，避免拿一份口径去罚另一份实现：
      - 真实约束：逐 `(彩种, 目标期号, 重出批号)` 金额 ≤ 单期上限 ⇒ 违反才是问题；
      - 对照读数：同日 / 单彩种同期合计 ⇒ **只披露，不计入真实问题**。
    C1① 下跨彩种合计本来就不受单期上限约束，总量由「月度总额」管，
    把它直接判成超限会天天报假警。
  · **C3① 不受同期累计约束**：重出是用户主动再加一套票，累计划加是设计如此。
    但「不约束」必须可观测：逐期列出批次数 / 累计金额 / 相对上限倍数，
    重出接口回包带同一份读数。`regenerate_gate()` 同时实现 ② 的纯函数分支
    （`C3.约束重出批=true` 才拒绝），生产默认 false，切换需重新授权。

纪律
----
· **只读**：不写台账、不改额度、不夹注数、不阻断出号。
· 缺席≠通过：闸门未启用 / 金额未知 / 日期未记录都显式写原因并计数，
  绝不静默当 0 或从分母里消失。
· 真实花费只认 `config/budget_spend.json`；`resolve_groups()` 的逐彩种返回值不能相加。
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

BASE = Path(__file__).resolve().parents[1]
DECISIONS_PATH = BASE / "config" / "budget_period_cap.json"

DECISION_VERSION = "2026-11-07-A"

DEFAULT_DECISIONS: Dict[str, Any] = {
    "版本": DECISION_VERSION,
    "锁定": True,
    "C1": {"决定": "① 单彩种 · 单批"},
    "C2": {"决定": "① 补检查"},
    "C3": {"决定": "① 不受同期累计约束（保持现状）", "约束重出批": False},
}

NOTE_SCALE_ONLY = (
    "同期合计是**规模对照读数**：按 C1①，`单期上限` 只约束单彩种单批，"
    "不约束跨彩种 / 跨批合计；同期合计越过该值不构成违规，只作披露。"
)
NOTE_C3 = (
    "C3①：重出批不受同期累计约束。重出 = 用户主动再加一套票，"
    "同期累计继续叠加是设计如此，不是自动路径加注；只披露、不夹取、不拒绝。"
)
NOTE_NOT_PROOF = (
    "未发现超限只表示已测维度在该样本量下未见异常，不构成证明。"
)


def load_decisions(path: Optional[Path] = None) -> Dict[str, Any]:
    """读口径判据。缺文件 / 损坏 ⇒ 回落**保守默认**（不阻断、不冒充已拍板），并写原因。"""
    p = Path(path) if path is not None else DECISIONS_PATH
    out: Dict[str, Any] = json.loads(json.dumps(DEFAULT_DECISIONS, ensure_ascii=False))
    if not p.exists():
        out["_缺失"] = f"口径判据文件不存在：{p}"
        return out
    try:
        doc = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:                                    # noqa: BLE001
        out["_损坏"] = f"{type(e).__name__}: {e}"
        return out
    if not isinstance(doc, dict):
        out["_损坏"] = f"顶层非对象：{type(doc).__name__}"
        return out
    out.update(doc)
    return out


def c3_blocks(decisions: Optional[Dict[str, Any]] = None) -> bool:
    """C3 是否拒绝重出（默认 False = 保持现状）。坏数据一律不阻断。"""
    d = decisions if decisions is not None else load_decisions()
    c3 = d.get("C3") if isinstance(d.get("C3"), dict) else {}
    return c3.get("约束重出批") is True


def _amount(rec: Dict[str, Any]) -> Tuple[Optional[float], str]:
    """单条金额。金额字段优先；缺失时由 注数×单价 反算；都拿不到 ⇒ None + 原因。"""
    v = rec.get("金额")
    if v is not None and v != "":
        try:
            f = float(v)
            if f >= 0:
                return f, ""
        except (TypeError, ValueError):
            pass
    try:
        n = float(rec.get("注数") or 0)
        price = float(rec.get("单价") or 0)
        if n >= 0 and price > 0:
            return n * price, "金额字段缺失，按 注数×单价 反算"
    except (TypeError, ValueError):
        pass
    return None, "金额与 注数/单价 均不可读 ⇒ 不计入求和，单独计数"


def _day(rec: Dict[str, Any]) -> Tuple[str, bool]:
    """同日分组键 = 记账时间的日期部分。时间缺失/非法 ⇒ 单独一组并标未记录。"""
    s = str(rec.get("时间") or "")
    try:
        return datetime.fromisoformat(s).date().isoformat(), True
    except (ValueError, TypeError):
        return "未记录日期", False


def _rn(rec: Dict[str, Any]) -> int:
    try:
        return int(rec.get("重出批号") or 0)
    except (TypeError, ValueError):
        return 0


def group_records(records: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """把台账记录按三种口径分组。**所有记录都留在分母内**，不静默丢弃。"""
    by_batch: Dict[Tuple[str, str, str, int], Dict[str, Any]] = {}
    by_issue: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    by_day: Dict[str, Dict[str, Any]] = {}
    unknown_amount, unknown_day = 0, 0
    reasons: Dict[str, int] = defaultdict(int)

    for rec in records or []:
        r = rec if isinstance(rec, dict) else {}
        month = str(r.get("月份") or "")
        lot = str(r.get("彩种") or "未记录彩种")
        issue = str(r.get("目标期号") or "未记录期号")
        day, day_ok = _day(r)
        amt, why = _amount(r)
        if not day_ok:
            unknown_day += 1
        if amt is None:
            unknown_amount += 1
            reasons[why] += 1
        if why and amt is not None:
            reasons[why] += 1
        rn = _rn(r)
        n = int(float(r.get("注数") or 0)) if str(r.get("注数") or "").replace(".", "", 1).isdigit() else 0

        bk = (month, lot, issue, rn)
        b = by_batch.setdefault(bk, {"月份": month, "彩种": lot, "目标期号": issue,
                                    "重出批号": rn, "金额": 0.0, "注数": 0,
                                    "条数": 0, "金额未知条数": 0})
        ik = (month, lot, issue)
        i = by_issue.setdefault(ik, {"月份": month, "彩种": lot, "目标期号": issue,
                                    "金额": 0.0, "批次数": 0, "重出批数": 0,
                                    "注数": 0, "条数": 0})
        d = by_day.setdefault(day, {"日期": day, "金额": 0.0, "彩种数": 0,
                                   "批次数": 0, "注数": 0, "条数": 0,
                                   "彩种": set(), "批键": set()})
        for bucket in (b, i, d):
            if amt is not None:
                bucket["金额"] += amt
            bucket["注数"] += n
            bucket["条数"] += 1
        b["金额未知条数"] += 1 if amt is None else 0
        i["批次数"] = len({k[3] for k in by_batch if k[:3] == ik})
        i["重出批数"] = len({k[3] for k in by_batch if k[:3] == ik and k[3] > 0})
        d["彩种"].add(lot)
        d["批键"].add(bk)

    for d in by_day.values():
        d["彩种数"] = len(d.pop("彩种"))
        d["批次数"] = len(d.pop("批键"))
    for b in by_batch.values():
        b["金额"] = round(b["金额"], 2)
    for i in by_issue.values():
        i["金额"] = round(i["金额"], 2)
    for d in by_day.values():
        d["金额"] = round(d["金额"], 2)
    return {
        "逐批": [by_batch[k] for k in sorted(by_batch)],
        "单彩种同期": [by_issue[k] for k in sorted(by_issue)],
        "同日": [by_day[k] for k in sorted(by_day)],
        "记录数": len(records or []),
        "参与求和条数": len(records or []) - unknown_amount,
        "金额未知条数": unknown_amount,
        "日期未记录条数": unknown_day,
        "原因分布": dict(reasons),
    }


def check(*, records: Optional[Sequence[Dict[str, Any]]] = None,
          limit: Optional[Dict[str, Any]] = None,
          decisions: Optional[Dict[str, Any]] = None,
          spend_path: Optional[Path] = None,
          limit_path: Optional[Path] = None,
          decisions_path: Optional[Path] = None,
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """C2 体检读数。**永不抛出**，失败也返回带原因的 `不适用`（缺席≠通过）。"""
    dec = decisions if decisions is not None else load_decisions(decisions_path)
    out: Dict[str, Any] = {
        "口径版本": dec.get("版本"), "口径来源": str(decisions_path or DECISIONS_PATH),
        "C1": (dec.get("C1") or {}).get("决定"),
        "C2": (dec.get("C2") or {}).get("决定"),
        "C3": (dec.get("C3") or {}).get("决定"),
        "裁定原因": [k + ": " + str(v) for k, v in dec.items() if k.startswith("_")],
        "披露": [NOTE_SCALE_ONLY, NOTE_C3, NOTE_NOT_PROOF],
        "真实问题": [], "对照告警": [],
    }
    try:
        lim = limit
        if lim is None:
            from ev.budget_gate import enabled_limit
            lim = enabled_limit(path=limit_path)
        if lim is None:
            out["适用"] = False
            out["原因"] = "预算闸门未启用 ⇒ 单期上限不构成约束，本项检查不适用（不记为通过）"
            return out
        cap = float(lim["单期上限"])
        out["适用"] = True
        out["单期上限元"] = cap
        out["月度总额元"] = float(lim["月度总额"])

        rows = records
        if rows is None:
            from ev.budget_gate import load_spend
            rows = load_spend(path=spend_path, now=now).get("记录") or []
        g = group_records(rows)
        out["台账"] = {k: g[k] for k in ("记录数", "参与求和条数", "金额未知条数",
                                        "日期未记录条数", "原因分布")}
        out["台账"]["分母口径"] = ("记录数 = 全部台账行；金额未知不进求和但单独计数；"
                                 "日期未记录单独成组，不从分母里消失")

        # ---- ① 真实约束：逐 (彩种, 期号, 重出批号) 金额 ≤ 单期上限 ----
        bad = [dict(b, 超出元=round(b["金额"] - cap, 2))
               for b in g["逐批"] if b["金额"] > cap + 1e-9]
        out["真实约束"] = {
            "检查对象": "单彩种 · 单批（逐 (月份, 彩种, 目标期号, 重出批号)）",
            "受检批数": len(g["逐批"]), "违反批数": len(bad),
            "违反明细": bad[:20],
            "判据": "单批金额 ≤ 单期上限（clamp 的 n_cap = 单期上限 ÷ 单价）",
        }
        for b in bad:
            out["真实问题"].append(
                f"{b['彩种']} 期{b['目标期号']} 批{b['重出批号']} 单批 "
                f"{b['金额']:.0f} 元 > 单期上限 {cap:.0f} 元（超 {b['超出元']:.0f} 元）")

        # ---- ② 对照读数：同日 / 单彩种同期合计（只报不罚） ----
        days = g["同日"]
        mx = max((d["金额"] for d in days), default=0.0)
        over_days = [d for d in days if d["金额"] > cap + 1e-9]
        issues = [{**i, "相对单期上限倍数": round(i["金额"] / cap, 3) if cap else None}
                  for i in g["单彩种同期"]]
        out["同期合计对照"] = {
            "性质": "只报不罚（不构成违规）",
            "同日": days, "同日最大合计元": round(mx, 2),
            "同日越过单期上限的天数": len(over_days),
            "单彩种同期": issues,
            "含重出批的期数": len([i for i in issues if i["重出批数"] > 0]),
            "披露": NOTE_SCALE_ONLY,
        }
        if over_days:
            out["对照告警"].append(
                f"有 {len(over_days)} 个出号日的全彩种合计越过 {cap:.0f} 元"
                f"（最大 {mx:.0f} 元）；按 C1① 这不是违规，仅作规模披露。")

        # ---- ③ C3 读数：重出批同期累计，不约束 ----
        out["C3读数"] = {
            "约束重出批": c3_blocks(dec),
            "重出批次数": sum(1 for b in g["逐批"] if b["重出批号"] > 0),
            "涉及期数": len([i for i in issues if i["重出批数"] > 0]),
            "逐期累计": [i for i in issues if i["重出批数"] > 0],
            "披露": NOTE_C3,
        }
        if out["C3读数"]["约束重出批"]:
            out["对照告警"].append(
                "★ 判据文件把 C3 改成 ②（约束重出批），语义已偏离 C1①，"
                "需重新授权并同步改 clamp()；本模块只读，不会替生产夹注数。")
        return out
    except Exception as e:                                     # noqa: BLE001
        out["适用"] = False
        out["原因"] = f"检查不可用：{type(e).__name__}: {e}（不记为通过）"
        return out


def regenerate_gate(lottery: str, issue: Any, *, 注数: int,
                    records: Optional[Sequence[Dict[str, Any]]] = None,
                    limit: Optional[Dict[str, Any]] = None,
                    decisions: Optional[Dict[str, Any]] = None,
                    decisions_path: Optional[Path] = None,
                    spend_path: Optional[Path] = None,
                    now: Optional[datetime] = None,
                    单价: Optional[float] = None) -> Dict[str, Any]:
    """重出前的 C3 口径读数（**纯查询**，默认不拒绝）。

    两种决定都由本函数实现：
      · C3①（默认）：`拒绝=False`，只回披露；
      · C3②：同期累计 + 预计本批 > 单期上限 ⇒ `拒绝=True` 并写原因。
    ★ 生产默认 ①；切到 ② 等价于把语义换成 C1③，必须重新授权（见判据文件）。
    """
    dec = decisions if decisions is not None else load_decisions(decisions_path)
    blocks = c3_blocks(dec)
    try:
        from config import ticket_price
        price = float(单价 if 单价 is not None else ticket_price(lottery))
    except Exception:                                          # noqa: BLE001
        price = 2.0
    try:
        n = max(0, int(注数))
    except (TypeError, ValueError):
        n = 0
    projected = round(n * price, 2)

    rows = records
    if rows is None:
        try:
            from ev.budget_gate import load_spend
            rows = load_spend(path=spend_path, now=now).get("记录") or []
        except Exception:                                      # noqa: BLE001
            rows = []
    own = [r for r in rows if isinstance(r, dict)
           and str(r.get("彩种") or "") == str(lottery)
           and str(r.get("目标期号") or "") == str(issue)]
    amt, _ = 0.0, ""
    unknown = 0
    for r in own:
        a, why = _amount(r)
        if a is None:
            unknown += 1
        else:
            amt += a
    rn_set = {_rn(r) for r in own}
    before = round(amt, 2)
    after = round(before + projected, 2)

    lim = limit
    if lim is None:
        try:
            from ev.budget_gate import enabled_limit
            lim = enabled_limit()
        except Exception:                                      # noqa: BLE001
            lim = None
    cap = float(lim["单期上限"]) if lim else None

    base = {
        "彩种": str(lottery), "目标期号": str(issue),
        "口径": (dec.get("C3") or {}).get("决定"),
        "约束重出批": blocks,
        "本期已有批数": len(rn_set),
        "本期已有重出批数": len([x for x in rn_set if x > 0]),
        "同期累计元": before, "预计本批元": projected, "重出后累计元": after,
        "单期上限元": cap,
        "相对单期上限倍数": (round(after / cap, 3) if cap else None),
        "金额未知条数": unknown,
        "拒绝": False,
        "披露": [NOTE_C3, NOTE_SCALE_ONLY, NOTE_NOT_PROOF],
    }
    if not blocks:
        base["原因"] = "C3①：不受同期累计约束，不拒绝重出、不夹取注数"
        return base
    if cap is None:
        base["原因"] = "C3② 但闸门未启用 ⇒ 无法确定上限，保守不拒绝（缺席≠通过）"
        return base
    if after > cap + 1e-9:
        base["拒绝"] = True
        base["原因"] = (f"C3②：重出后同期累计 {after:.0f} 元 > 单期上限 {cap:.0f} 元"
                        " ⇒ 拒绝本次重出")
    else:
        base["原因"] = f"C3②：重出后同期累计 {after:.0f} 元 ≤ 单期上限 {cap:.0f} 元"
    return base


__all__ = [
    "DECISION_VERSION", "DECISIONS_PATH", "DEFAULT_DECISIONS",
    "NOTE_C3", "NOTE_NOT_PROOF", "NOTE_SCALE_ONLY",
    "c3_blocks", "check", "group_records", "load_decisions",
    "regenerate_gate",
]
