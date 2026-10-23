# -*- coding: utf-8 -*-
"""ev/tier_ledger.py —— 奖级达标策略的影子台账（jsonl，一行一期，append-only）。

为什么不走 pending
------------------
`data.feedback._pending_key = (目标期号, 档位, 台账域)`，台账域只有「生产/研究」两值。
本策略是**影子**性质（不产生实际投注），且低拥挤策略已占用研究域，
若也落研究域会在同一 (期号, 一般, 研究) 上互删。
⇒ 独立 jsonl：`training/prize_tier/<彩种>.jsonl`，结构照 `ev/profit/ledger.py`。

三条硬约束
----------
1. **永不写生产 pending**（`record_pending` 恒 False）。本模块不 import pending 链路。
2. 策略名 `"奖级达标策略"` 已在 `data.feedback.NON_LEARNING_STRATEGIES` 里 ⇒
   即使有人把它塞进反馈历史，也不会参与权重学习（双保险）。
3. 按期幂等：同期重跑覆盖该行；结算只回填，不新增行。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config import TRAINING_DIR

STRATEGY = "奖级达标策略"
TIER_DIR = TRAINING_DIR / "prize_tier"

# 与生产链路彻底隔开：本模块**不**导入 data.feedback 的 pending 相关函数
RECORD_PENDING_ALLOWED = False


def path(lottery: str) -> Path:
    return TIER_DIR / f"{lottery}.jsonl"


def _ensure_dir() -> None:
    TIER_DIR.mkdir(parents=True, exist_ok=True)


def _read(lottery: str) -> List[Dict[str, Any]]:
    p = path(lottery)
    if not p.exists():
        return []
    rows = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


def _write_atomic(p: Path, rows: Iterable[Dict[str, Any]]) -> None:
    _ensure_dir()
    tmp = p.with_suffix(p.suffix + f".{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
    last = None
    for attempt in range(5):
        try:
            os.replace(tmp, p)
            return
        except PermissionError as e:      # pragma: no cover - 依赖外部占用时序
            last = e
            time.sleep(0.05 * (attempt + 1))
    try:
        os.unlink(tmp)
    except OSError:
        pass
    raise last if last else RuntimeError("write failed")


def record(lottery: str, issue: Any, target: Any, tickets: List[dict],
           meta: Optional[Dict[str, Any]] = None,
           record_pending: bool = False) -> Dict[str, Any]:
    """记录一期的号码与口径（按期幂等）。`record_pending` 仅供显式声明，恒不生效。"""
    if record_pending:
        raise ValueError("奖级达标策略是研究入口，禁止写生产 pending（record_pending 必须为 False）")
    rows = _read(lottery)
    key = str(issue)
    meta = dict(meta or {})
    row = {
        "期号": key,
        "彩种": lottery,
        "策略": STRATEGY,
        "台账域": "研究",
        "记录时间": datetime.now().isoformat(timespec="seconds"),
        "目标": meta.get("目标"),
        "规则版本": meta.get("规则版本"),
        "注数": len(tickets),
        "倍数": [1] * len(tickets),
        "号码": tickets,
        "单注达标概率": meta.get("单注达标概率"),
        "理论上限Kp": meta.get("理论上限Kp"),
        "覆盖通道数": meta.get("覆盖通道数"),
        "通道权重和(上界)": meta.get("通道权重和"),
        "重叠上限": meta.get("重叠上限"),
        "结算": None,          # 开奖后回填
    }
    replaced = False
    for i, r in enumerate(rows):
        if str(r.get("期号")) == key:
            rows[i] = row
            replaced = True
            break
    if not replaced:
        rows.append(row)
    _write_atomic(path(lottery), rows)
    return {"ok": True, "期号": key, "覆盖": replaced, "台账": str(path(lottery))}


def settle(lottery: str, issue: Any, gross: float, cost: float,
           hit_target: Optional[bool] = None) -> Dict[str, Any]:
    """开奖后回填结算（不新增行）。"""
    rows = _read(lottery)
    key = str(issue)
    for r in rows:
        if str(r.get("期号")) == key:
            r["结算"] = {
                "实得": float(gross), "成本": float(cost),
                "净收益": float(gross) - float(cost),
                "达标": None if hit_target is None else bool(hit_target),
                "结算时间": datetime.now().isoformat(timespec="seconds"),
            }
            _write_atomic(path(lottery), rows)
            return {"ok": True, "期号": key}
    return {"ok": False, "期号": key, "原因": "台账中无该期记录"}


def summary(lottery: str) -> Dict[str, Any]:
    """汇总：期数、达标期占比、净收益、最大回撤、单期集中度。"""
    rows = _read(lottery)
    settled = [r for r in rows if r.get("结算")]
    if not settled:
        return {"彩种": lottery, "策略": STRATEGY, "期数": len(rows), "已结算": 0}
    nets = [float(r["结算"]["净收益"]) for r in settled]
    hits = [r["结算"].get("达标") for r in settled if r["结算"].get("达标") is not None]
    cum = peak = mdd = 0.0
    mdd_len = cur_len = 0
    for v in nets:
        cum += v
        if cum > peak:
            peak, cur_len = cum, 0
        else:
            cur_len += 1
            if peak - cum > mdd:
                mdd, mdd_len = peak - cum, cur_len
    total = sum(nets)
    best = max(nets) if nets else 0.0
    return {
        "彩种": lottery, "策略": STRATEGY,
        "期数": len(rows), "已结算": len(settled),
        "达标期数": sum(1 for h in hits if h),
        "达标期占比": round(sum(1 for h in hits if h) / len(hits), 4) if hits else None,
        "净收益": round(total, 2),
        "净盈利期占比": round(sum(1 for v in nets if v > 0) / len(nets), 4),
        "最大回撤": round(mdd, 2),
        "最大回撤持续期数": mdd_len,
        "单期集中度": (round(best / total, 4) if total else None),
        "说明": "影子台账（研究域），不参与生产对账与权重学习。",
    }
