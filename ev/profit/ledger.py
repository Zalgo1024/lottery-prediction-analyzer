# -*- coding: utf-8 -*-
"""ev/profit/ledger.py —— 影子台账（jsonl，一行一期，append-only）。

为什么要独立 jsonl 而不走 pending
--------------------------------
`data.feedback._pending_key = (目标期号, 档位, 台账域)`，台账域只有「生产/研究」两值。
盈利策略若也落研究域，会与低拥挤策略在同一个 (期号, 一般, 研究) 上**互删**。
且本策略是**影子**性质的（不增加实际投注），本就不该占用生产/研究对账链路。

结构照 `ev/trials.py`：按期幂等（同期重跑不新增行）、结算时**回填**不新增行。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from config import TRAINING_DIR

PROFIT_DIR = TRAINING_DIR / "profit"


def path(lottery: str) -> Path:
    return PROFIT_DIR / f"{lottery}.jsonl"


def _ensure_dir() -> None:
    PROFIT_DIR.mkdir(parents=True, exist_ok=True)


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
    # ★ Windows 上 os.replace 偶发 WinError 5（杀毒/索引服务短暂占用目标文件）。
    #   退避重试 5 次即可；仍失败才抛出（不静默丢数据）。
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


def record(lottery: str, issue: Any, decision: Dict[str, Any],
           tickets: Optional[List[dict]] = None) -> Dict[str, Any]:
    """记录一期决策（按期幂等：同期已存在则覆盖该行，不新增）。"""
    rows = _read(lottery)
    key = str(issue)
    row = {
        "期号": key,
        "彩种": lottery,
        "记录时间": datetime.now().isoformat(timespec="seconds"),
        "策略": "盈利策略",
        "判决": decision.get("判决"),
        "判决等级": decision.get("判决等级"),
        "原因": decision.get("原因"),
        "注数": decision.get("注数"),
        "净EV单注": (decision.get("κ0基线") or {}).get("净EV单注"),
        "P至少中一注": (decision.get("κ0基线") or {}).get("P至少中一注"),
        "中奖注数期望": (decision.get("κ0基线") or {}).get("中奖注数期望"),
        "κ*": (decision.get("κ*求解") or {}).get("κ*"),
        "κ_upper": (decision.get("机制强度上界") or {}).get("kappa"),
        "缺口倍数": decision.get("缺口倍数"),
        "税口径": decision.get("税口径"),
        "号码": tickets,
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
    return {"ok": True, "期号": key, "覆盖": replaced}


def settle(lottery: str, issue: Any, gross: float, tax: float,
           cost: float) -> Dict[str, Any]:
    """开奖后回填结算（不新增行）。"""
    rows = _read(lottery)
    key = str(issue)
    for r in rows:
        if str(r.get("期号")) == key:
            r["结算"] = {
                "税前奖金": float(gross), "税": float(tax), "成本": float(cost),
                "净收益": float(gross) - float(tax) - float(cost),
                "结算时间": datetime.now().isoformat(timespec="seconds"),
            }
            _write_atomic(path(lottery), rows)
            return {"ok": True, "期号": key}
    return {"ok": False, "期号": key, "原因": "台账中无该期记录"}


def summary(lottery: str) -> Dict[str, Any]:
    """汇总：参与期数、净收益、净盈利期占比、最大回撤、单期集中度。"""
    rows = _read(lottery)
    settled = [r for r in rows if r.get("结算")]
    if not settled:
        return {"彩种": lottery, "期数": len(rows), "已结算": 0}
    nets = [float(r["结算"]["净收益"]) for r in settled]
    cum, peak, mdd, mdd_len, cur_len = 0.0, 0.0, 0.0, 0, 0
    for v in nets:
        cum += v
        if cum > peak:
            peak, cur_len = cum, 0
        else:
            cur_len += 1
            if peak - cum > mdd:
                mdd, mdd_len = peak - cum, cur_len
    total = sum(nets)
    best = max(nets)
    return {
        "彩种": lottery, "期数": len(rows), "已结算": len(settled),
        "净收益": round(total, 2),
        "净盈利期占比": round(sum(1 for v in nets if v > 0) / len(nets), 4),
        "最大回撤": round(mdd, 2),
        "最大回撤持续期数": mdd_len,
        "单期最大净收益": round(best, 2),
        "单期集中度": (round(best / total, 4) if total else None),
        "判决分布": _count(rows, "判决"),
    }


def _count(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for r in rows:
        v = str(r.get(key) or "未定")
        out[v] = out.get(v, 0) + 1
    return out
