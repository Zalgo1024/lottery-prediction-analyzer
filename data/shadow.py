# -*- coding: utf-8 -*-
"""同预算影子对照 —— 三臂并排记，**不产生任何真实成本**。

要回答的问题不是「模型准不准」，而是：

    同样的注数、同样的钱，覆盖选号比随机选号多拿回了多少？

所以三臂必须在**同一窗口、同一注数、同一预算**下比较：

    当前模型 / 等注数等预算随机 / 旧版本或简单固定规则

★ 三条设计纪律，每条都是为了避免一个已知的自欺方式：

1. **影子不记账**。本模块**禁止**调用 `ev.budget_gate.record_spend` —— 影子若占用
   真实额度，跑对照就等于在偷偷加注，而用户明确要求「先影子运行，不追加投注」。
   `tests/test_shadow_no_budget.py` 断言跑完影子后 `spent_this_month()` 严格不变。
2. **不进 pending**。pending 的语义是「待开奖核对的一批票」，
   `_pending_key=(期号, 档位, 台账域)` 只有生产/研究两值 —— 影子挤进去会与真实批次
   互相覆盖。影子有自己的文件。
3. **失败期照样写一行**。只保留「跑成功」的期，等于把不利样本删掉；
   停发、结算失败都要留行，`结算失败=true`。

★ 本模块只记录与汇总，不改变任何出号决策。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config import BASE_DIR

logger = logging.getLogger(__name__)

SHADOW_DIR = BASE_DIR / "training" / "shadow"

ARM_MODEL = "当前模型"
ARM_RANDOM = "等注数随机"
ARM_FIXED = "固定规则"
ARMS = (ARM_MODEL, ARM_RANDOM, ARM_FIXED)

HONEST_NOTE = (
    "同预算对照比较的是「同一笔钱换回多少」，不是「哪组号码更可能中」。"
    "任何一注的中奖概率不因选号方式改变；三臂差异只反映组合覆盖与运气，"
    "样本不足时不得据此加注。"
)


def shadow_path(lottery: str, arm: str, *, base: Optional[Any] = None) -> Path:
    d = base if base is not None else SHADOW_DIR
    return d / f"{lottery}_{arm}.jsonl"


def record(lottery: str, issue: Any, arm: str, *,
           tickets: Sequence[Any], price: Optional[float] = None,
           result: Optional[Dict[str, Any]] = None,
           note: Optional[str] = None,
           base: Optional[Any] = None) -> Dict[str, Any]:
    """记一行影子结果。

    ★ 这里**只算成本、不记成本**：`记账` 字段恒为 False，表示这笔钱没有真的投出去，
      也没有占用预算额度。要记账请走 `record_pending_prediction` 的生产路径。
    """
    if arm not in ARMS:
        raise ValueError(f"未知影子臂：{arm}（只允许 {ARMS}）")

    from config import ticket_price

    p = shadow_path(lottery, arm, base=base)
    p.parent.mkdir(parents=True, exist_ok=True)
    n = len(list(tickets or []))
    unit = float(price if price is not None else ticket_price(lottery))
    res = dict(result or {})

    row = {
        "期号": issue,
        "臂": arm,
        "注数": n,
        "单价": unit,
        "成本": round(n * unit, 2),
        "记账": False,                 # ★ 影子不占预算，这一列是给汇总时自证用的
        "中奖注数": int(res.get("中奖注数") or 0),
        "税前奖金": float(res.get("税前奖金") or 0.0),
        "结算失败": bool(res.get("结算失败")),
        "口径": res.get("口径"),
        "备注": note,
    }
    row["净盈亏"] = round(row["税前奖金"] - row["成本"], 2)

    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return row


def load(lottery: str, arm: Optional[str] = None, *,
         base: Optional[Any] = None) -> List[Dict[str, Any]]:
    arms = [arm] if arm else list(ARMS)
    out: List[Dict[str, Any]] = []
    for a in arms:
        p = shadow_path(lottery, a, base=base)
        if not p.exists():
            continue
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
    return out


def summary(lottery: str, *, base: Optional[Any] = None) -> Dict[str, Any]:
    """三臂并排汇总。**同预算校验不通过时明说，不给一个平均了事的数。**"""
    rows = load(lottery, base=base)
    by_arm: Dict[str, List[dict]] = {a: [r for r in rows if r.get("臂") == a] for a in ARMS}

    out: Dict[str, Any] = {"彩种": lottery, "臂": {}, "同预算": _same_budget(by_arm),
                           "诚实边界": HONEST_NOTE}
    for a, rs in by_arm.items():
        if not rs:
            out["臂"][a] = {"期数": 0, "缺": True}
            continue
        cost = sum(float(r.get("成本") or 0) for r in rs)
        gross = sum(float(r.get("税前奖金") or 0) for r in rs)
        net = gross - cost
        out["臂"][a] = {
            "期数": len(rs),
            "注数合计": sum(int(r.get("注数") or 0) for r in rs),
            "成本": round(cost, 2),
            "税前奖金": round(gross, 2),
            "净盈亏": round(net, 2),
            "净收益率": (net / cost) if cost else None,
            "中奖注数": sum(int(r.get("中奖注数") or 0) for r in rs),
            "中奖期数": sum(1 for r in rs if int(r.get("中奖注数") or 0) > 0),
            "结算失败期数": sum(1 for r in rs if r.get("结算失败")),
        }
    return out


def _same_budget(by_arm: Dict[str, List[dict]]) -> Dict[str, Any]:
    """三臂是否同窗口同注数同预算。不同则点名，不要悄悄取交集。

    ★ 「同注数」必须**跨臂**逐期比：只在自己臂内部比，永远为真，
      于是「A 臂 10 注、B 臂 50 注」这种最该拦的情况反而查不出来。
    """
    per_arm = {a: {str(r.get("期号")): int(r.get("注数") or 0) for r in rs}
               for a, rs in by_arm.items()}
    active = {a: p for a, p in per_arm.items() if p}
    if len(active) < 2:
        return {"可比较": False, "原因": "可用臂不足 2 个"}

    sets = [set(p) for p in active.values()]
    common = set.intersection(*sets)
    same_issues = all(s == sets[0] for s in sets)

    same_count = True
    for iss in common:
        if len({p[iss] for p in active.values()}) > 1:
            same_count = False
            break

    return {
        "可比较": bool(same_issues and same_count),
        "同窗口": same_issues,
        "同注数": same_count,
        "共同期数": len(common),
        "缺失期": {a: len(set(p) - common) for a, p in active.items()},
        "说明": ("三臂窗口不一致 ⇒ 差异里混了「跑了不同的期」，不能直接比净收益率"
                 if not same_issues else
                 ("三臂注数不一致 ⇒ 差异里混了「买了更多」，不能直接比" if not same_count else "同窗口同注数")),
    }


def assert_no_booking(lottery: str, *, base: Optional[Any] = None) -> None:
    """自检：影子台账里不允许出现「已记账」的行。"""
    bad = [r for r in load(lottery, base=base) if r.get("记账")]
    if bad:
        raise AssertionError(
            f"影子台账出现 {len(bad)} 条「已记账」行 ⇒ 影子正在占用真实预算额度。"
            "这会把它从『对照实验』变成『偷偷加注』。"
        )
