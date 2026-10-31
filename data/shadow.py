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

import base64
import contextlib
import json
import logging
import os
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from config import BASE_DIR

logger = logging.getLogger(__name__)

SHADOW_DIR = BASE_DIR / "training" / "shadow"

ARM_MODEL = "当前模型"
ARM_RANDOM = "等注数随机"
ARM_FIXED = "固定规则"
ARMS = (ARM_MODEL, ARM_RANDOM, ARM_FIXED)

KIND_RECORD = "记录"
KIND_SETTLE = "结算"

HONEST_NOTE = (
    "同预算对照比较的是「同一笔钱换回多少」，不是「哪组号码更可能中」。"
    "任何一注的中奖概率不因选号方式改变；三臂差异只反映组合覆盖与运气，"
    "样本不足时不得据此加注。"
)


def shadow_path(lottery: str, arm: str, *, base: Optional[Any] = None) -> Path:
    d = base if base is not None else SHADOW_DIR
    return d / f"{lottery}_{arm}.jsonl"


_THREAD_LOCKS: Dict[str, Any] = {}
_THREAD_LOCKS_GUARD = __import__("threading").Lock()


def _thread_lock(lottery: str):
    import threading

    with _THREAD_LOCKS_GUARD:
        lk = _THREAD_LOCKS.get(lottery)
        if lk is None:
            lk = threading.Lock()
            _THREAD_LOCKS[lottery] = lk
        return lk


@contextlib.contextmanager
def shadow_lock(lottery: str, *, base: Optional[Any] = None, timeout: float = 20.0):
    """影子台账的互斥：**线程锁 + 跨进程文件锁**两层。

    ★ 为什么两层都要：
      - `run_auto_pipeline` 的三条触发路径（调度器 22:05 / worker 事件驱动 /
        启动恢复）**可能几乎同时到达**（代码注释里记着这个历史教训）。它们多半是
        **同一进程的不同线程** —— 只有 `file_lock` 时，Windows 上第二个线程
        `open(lock_file, "a+")` 直接 `PermissionError`（实测），锁没起到排队作用，
        反而把出号流程打挂（所以这里只能吞异常，等于没锁）。
      - 而 `file_lock` 管的是**不同进程**（web 服务 + 独立调度脚本可能同时写）。
    ★ 单行压缩后 > 4KB（500 注约 9KB），交错 append 会产生半行；
      `load()` 遇到残行只跳过 ⇒ 那一期少一两条臂，`同预算` 判成不可比，还查不出原因。
    """
    from data.file_lock import file_lock

    tl = _thread_lock(lottery)
    if not tl.acquire(timeout=timeout):
        raise TimeoutError(f"影子台账线程锁超时（{lottery}）")
    try:
        with file_lock(shadow_path(lottery, ARMS[0], base=base), timeout=timeout):
            yield
    finally:
        tl.release()



def _pack(tickets: Sequence[Any]) -> str:
    """号码压成单字段（base64(zlib(json))）。

    ★ 为什么必须存号码而不是只存注数：开奖前不知道结果，结算时必须有号码才能判级。
      为什么不存明文：三臂 × 六彩种 × 500 注/期，明文 jsonl 一年约 150MB；
      zlib 后约 1/6（号码结构高度重复）。存「生成参数 + 事后重放」看似更省，
      但重放一旦与当时不一致就会**悄悄**结算成别的号码，属于不可发现的错误。
    """
    raw = json.dumps(list(tickets or []), ensure_ascii=False, default=str).encode("utf-8")
    return base64.b64encode(zlib.compress(raw, 6)).decode("ascii")


def _unpack(blob: Optional[str]) -> List[Any]:
    if not blob:
        return []
    try:
        return json.loads(zlib.decompress(base64.b64decode(blob)).decode("utf-8"))
    except Exception as e:                                    # noqa: BLE001
        logger.warning(f"影子号码解压失败（按无号码处理）：{e}")
        return []


def record(lottery: str, issue: Any, arm: str, *,
           tickets: Sequence[Any], price: Optional[float] = None,
           result: Optional[Dict[str, Any]] = None,
           note: Optional[str] = None,
           base: Optional[Any] = None) -> Dict[str, Any]:
    """记一行影子结果。

    ★ 这里**只算成本、不记成本**：`记账` 字段恒为 False，表示这笔钱没有真的投出去，
      也没有占用预算额度。要记账请走 `record_pending_prediction` 的生产路径。

    ★ `tickets` 会连同号码一起落盘（`号码压缩`），供开奖后结算用；
      `结算=False` 表示这一行只记了「生成」，结果列要等 `settle_row()`。
    """
    if arm not in ARMS:
        raise ValueError(f"未知影子臂：{arm}（只允许 {ARMS}）")

    from config import ticket_price

    p = shadow_path(lottery, arm, base=base)
    p.parent.mkdir(parents=True, exist_ok=True)
    tks = list(tickets or [])
    n = len(tks)
    unit = float(price if price is not None else ticket_price(lottery))
    res = dict(result or {})

    row = {
        "类型": KIND_RECORD,
        "期号": issue,
        "臂": arm,
        "注数": n,
        "单价": unit,
        "成本": round(n * unit, 2),
        "记账": False,                 # ★ 影子不占预算，这一列是给汇总时自证用的
        "中奖注数": int(res.get("中奖注数") or 0),
        "税前奖金": float(res.get("税前奖金") or 0.0),
        "结算失败": bool(res.get("结算失败")),
        "结算": bool(res.get("结算")),
        "口径": res.get("口径"),
        "备注": note,
    }
    if tks:
        row["号码压缩"] = _pack(tks)
    row["净盈亏"] = round(row["税前奖金"] - row["成本"], 2)

    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return row


def settle_row(lottery: str, issue: Any, arm: str, *,
               result: Dict[str, Any],
               note: Optional[str] = None,
               base: Optional[Any] = None) -> Dict[str, Any]:
    """追加一行「结算」，不修改原记录行。

    ★ 为什么不原地改：影子台账与留证台账同为 append-only。原地重写会让
      「什么时候结算的、当时用的什么口径」永久消失，而这两件事恰是事后核对
      同预算对照是否被偷偷重算的唯一依据。
    """
    if arm not in ARMS:
        raise ValueError(f"未知影子臂：{arm}（只允许 {ARMS}）")

    from datetime import datetime

    p = shadow_path(lottery, arm, base=base)
    p.parent.mkdir(parents=True, exist_ok=True)
    res = dict(result or {})
    row = {
        "类型": KIND_SETTLE,
        "期号": issue,
        "臂": arm,
        "中奖注数": int(res.get("中奖注数") or 0),
        "税前奖金": float(res.get("税前奖金") or 0.0),
        "结算失败": bool(res.get("结算失败")),
        "口径": res.get("口径"),
        "开奖号码": res.get("开奖号码"),
        "金额未知注数": int(res.get("金额未知注数") or 0),
        "结算时间": res.get("结算时间") or datetime.now().isoformat(),
        "备注": note,
    }
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())
    return row


def _read_raw(lottery: str, arm: Optional[str] = None, *,
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


def load(lottery: str, arm: Optional[str] = None, *,
         base: Optional[Any] = None) -> List[Dict[str, Any]]:
    """读影子台账，返回**每 (期号, 臂) 一行**（结算行覆盖到记录行上）。

    ★ 「每期每臂一行」这个语义是刻意的：同一期重复生成（自愈/重跑）只会留下
      最新一条，否则 `期数` 会被重跑次数放大，净收益率跟着失真。
    """
    gen: Dict[Any, Dict[str, Any]] = {}
    stl: Dict[Any, Dict[str, Any]] = {}
    for r in _read_raw(lottery, arm, base=base):
        key = (str(r.get("期号")), r.get("臂"))
        if r.get("类型") == KIND_SETTLE:
            stl[key] = r
        elif r.get("类型") == KIND_RECORD or "类型" not in r:
            gen[key] = r
    out: List[Dict[str, Any]] = []
    for key, g in gen.items():
        s = stl.get(key)
        row = dict(g)
        if s:
            for k in ("中奖注数", "税前奖金", "结算失败", "口径", "开奖号码",
                      "金额未知注数", "结算时间"):
                if k in s and s[k] is not None:
                    row[k] = s[k]
            row["结算"] = True
            row["净盈亏"] = round(float(row.get("税前奖金") or 0)
                              - float(row.get("成本") or 0), 2)
        out.append(row)
    out.sort(key=lambda r: (str(r.get("期号")), str(r.get("臂"))))
    return out


def pending_issues(lottery: str, *, base: Optional[Any] = None) -> List[str]:
    """已记录但还没结算的期号（去重、按字符串排序）。"""
    done = {str(r.get("期号")) for r in load(lottery, base=base) if r.get("结算")}
    seen = {str(r.get("期号")) for r in load(lottery, base=base)}
    return sorted(seen - done)


def tickets_of(lottery: str, issue: Any, arm: str, *,
               base: Optional[Any] = None) -> List[Any]:
    """取某期某臂当时记下的号码（没有则空列表）。"""
    for r in load(lottery, arm, base=base):
        if str(r.get("期号")) == str(issue):
            return _unpack(r.get("号码压缩"))
    return []



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
            # ★ 必须分开报：`净收益率 = -100%` 在两种情况下长得一模一样 ——
            #   「一期都还没开奖（奖金尚未产生）」与「开了 30 期全没中」。
            #   前者不构成任何结论，后者才是读数。只给一个 -100% 会被读成后者。
            "已结算期数": sum(1 for r in rs if r.get("结算")),
            "待结算期数": sum(1 for r in rs if not r.get("结算")),
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
