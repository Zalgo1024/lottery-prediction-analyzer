# -*- coding: utf-8 -*-
"""ev/arena/tier_metrics.py —— 同一批票上的**多门**指标（≥10元 / ≥四等 / ≥三等）。

为什么单独一个模块
------------------
`replay()` 只算「一个目标」的达标率。任务 1 与任务 3 要在**同一批票**上同时看
三个门，并回答「提升是因为票多了，还是每元买到的覆盖真变好了」。这需要三样
`replay` 不提供的东西：

  ① 每个门的**解析单注概率** `p`（用来算 `K·p`、随机基线、可争取空间）；
  ② 每个门的**条件集**（`(h,j)` 格），且必须按规则版本分开；
  ③ 跨门的统一口径：同一批期、同一批票、同一套成本。

★★ 大乐透的规则版本陷阱（本模块最容易写错的地方）
   `resolve_conditions(lottery, {"kind":"grade","grade":"四等"}, issue)` **必须
   逐期调用**，而且必须按**该期**的规则版本缓存。实测同一句「四等」：

     14052 `dlt-2014-05` → [(3,2),(4,1),(4,2),(5,0),(5,1),(5,2)]
     19019 `dlt-2019-02` → [(4,2),(5,0),(5,1),(5,2)]        ← 没有 (4,1)
     26123 `dlt-2026-02` → [(4,1),(4,2),(5,0),(5,1),(5,2)]

   把 2026 的条件集套到 2019 的期上，`p` 会偏，达标率会偏，`可争取空间` 会偏，
   而且**不会报错**。同理 `rule_issue`（判级版本）与 `issue`（当期版本、查库/派奖）
   是两个参数，不许合并。

口径（两个都必须显式给出，禁止混报）
------------------------------------
  · `periodic`：逐期按**当期**规则判级 —— 历史事实口径；
  · `current` ：全部期按 `target_issue` 那一版规则判 —— 反事实口径。
  两者本模块都支持，但结果里的 `口径` 字段会写明是哪种，**不提供「默认混用」**。

★ 本模块不改变任何一注的中奖概率，也不产出「买什么号」。
  `可争取空间 = min(1, K·p) − [1 − (1−p)^K]` 描述的是「K 注达标事件去相关」
  这件事最多能值多少，而不是「覆盖选号能提升多少」。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

import ev.tier_target as TT
from ev.arena.design import outcome_gate
from ev.mechanism.narrative import TIER_COVERAGE_CAVEAT
from ev.profit.rules import rules_for

__all__ = ["GATE_ORDER", "GATE_TARGETS", "gate_target", "gate_series",
           "gate_block", "sanity_violations", "to_row", "HONEST_NOTE"]

# 门（标签）→ target 定义。标签用「≥四等」这种**人话**，内部转成 resolve_conditions 的 target。
GATE_TARGETS: Dict[str, Dict[str, Any]] = {
    "≥10元": {"kind": "payout", "min_payout": 10.0},
    "≥四等": {"kind": "grade", "grade": "四等"},
    "≥三等": {"kind": "grade", "grade": "三等"},
}

GATE_ORDER: Tuple[str, ...] = ("≥10元", "≥四等", "≥三等")

HONEST_NOTE = (
    "本表只做**达标门**的对照测量：它回答「这一组 K 注里至少一注达到某奖级的期次占比」，"
    "不回答「该买哪一注」。同一个门在不同规则版本下不是同一件事（大乐透「四等」"
    "跨版本条件集不同），故所有分版本给出的数必须分版本读，禁止合并成一个平均。"
    + TIER_COVERAGE_CAVEAT
)


# ------------------------------------------------------------------ 门定义
def gate_target(label: str) -> Dict[str, Any]:
    """标签 → `resolve_conditions` 的 target。

    也接受已写好的 target dict（原样返回），便于调用方临时试别的门。
    """
    if isinstance(label, dict):
        return dict(label)
    if label in GATE_TARGETS:
        return dict(GATE_TARGETS[label])
    # 「≥N等」的宽松写法：允许「≥4等」「≥四等奖」这类别名
    s = str(label).strip().lstrip("≥").replace("奖", "").replace("等", "等")
    alias = {"4": "四", "3": "三", "5": "五", "6": "六", "7": "七", "8": "八", "9": "九"}
    if s and s[0].isdigit():
        s = alias.get(s[0], s[0]) + s[1:]
    if s.endswith("等"):
        return {"kind": "grade", "grade": s}
    raise ValueError(f"无法解析的门标签：{label!r}（已知 {list(GATE_TARGETS)}）")


# ------------------------------------------------------------------ 单门
def _version_blocks(lottery: str, records: Sequence[Any]) -> Dict[str, List[int]]:
    """按**当期**规则版本给期下标分组（顺序稳定，便于逐位对账）。"""
    out: Dict[str, List[int]] = {}
    for p, rec in enumerate(records):
        ver = rules_for(lottery, rec.期号).rule_version
        out.setdefault(str(ver), []).append(p)
    return out


def _hit_counts(h: np.ndarray, j: np.ndarray, gate: np.ndarray) -> np.ndarray:
    """`(K, n)` 命中数（每期达标的票数）。"""
    return gate[h, j].sum(axis=0).astype(np.int64)


def gate_series(lottery: str, h: np.ndarray, j: np.ndarray,
                records: Sequence[Any], label: str, *,
                rule_mode: str = "periodic",
                target_issue: Any = None,
                K: Optional[int] = None) -> Dict[str, Any]:
    """一个门在「一批票 × 一批期」上的达标序列与解析量。

    参数
    ----
    h, j : `(K, n)` int16，`ArmResult.h` / `ArmResult.j`（同一批票的命中矩阵）
    records : 长度 n 的开奖记录（升序），与 `h` 的列一一对应
    rule_mode : `periodic`（逐期按当期规则）/ `current`（全期按 target_issue 那版）
    target_issue : `current` 口径必填；`periodic` 下仅作 fallback

    返回（标量进 `to_row()`，数组放在 `_` 前缀键里，**不进 JSON 契约**）
    ------
    `期占比` / `达标期数` / `单期SE` / `p`（期加权单注概率）/ `K·p` /
    `随机基线` / `可争取空间` / `覆盖率效率` / `条件集`（按规则版本分开）/
    `分版本`（每版的期数、达标期数、p、条件集）

    ★ `覆盖率效率 = 期占比 / min(1, K·p)`。它可能 **>1**（实测 60 期时常见），
      因为分母是**真实概率**的上界、分子是它的估计量，小样本下估计量摆上去很正常。
      所以读效率必须同时读 `期数` 与 `单期SE`，**不能**把 >1 当成「超过上界」的证据。
    """
    if rule_mode not in ("periodic", "current"):
        raise ValueError(f"rule_mode 必须是 periodic/current，收到 {rule_mode!r}")
    recs = list(records)
    n = len(recs)
    if n == 0:
        raise ValueError("开奖记录为空")
    h = np.asarray(h)
    j = np.asarray(j)
    if h.shape != j.shape or h.ndim != 2:
        raise ValueError(f"h/j 形状不一致或不是二维：{h.shape} vs {j.shape}")
    if h.shape[1] != n:
        raise ValueError(f"h/j 的列数 {h.shape[1]} 与期数 {n} 不一致")
    n_tickets = int(h.shape[0]) if K is None else int(K)
    if n_tickets != int(h.shape[0]):
        raise ValueError(f"K={K} 与 h 的票数 {h.shape[0]} 不一致")

    target = gate_target(label)
    blocks = _version_blocks(lottery, recs)
    if rule_mode == "current":
        src_issue = target_issue if target_issue is not None else recs[-1].期号
        blocks = {str(rules_for(lottery, src_issue).rule_version): list(range(n))}

    # 每个规则版本只解析一次（大乐透最多 5 版）；p 与 gate 都按该版本算
    nhit = np.zeros(n, dtype=np.int64)
    p_arr = np.zeros(n, dtype=float)
    per_version: Dict[str, Dict[str, Any]] = {}
    cond_by_version: Dict[str, List[Tuple[int, int]]] = {}
    unsupported: Dict[str, str] = {}

    for ver, idx in blocks.items():
        issue = recs[idx[0]].期号 if rule_mode == "periodic" else (
            target_issue if target_issue is not None else recs[-1].期号)
        try:
            res = TT.resolve_conditions(lottery, target, issue)
            gate, _w, _m, _nn = outcome_gate(lottery, target, issue)
            p = float(TT.single_ticket_probability(lottery, res["conditions"], issue))
        except (ValueError, KeyError) as exc:            # 该版本没有这个奖级
            unsupported[ver] = f"{type(exc).__name__}: {exc}"
            per_version[ver] = {"规则版本": ver, "期数": len(idx),
                                "不支持": unsupported[ver]}
            continue
        sub_h, sub_j = h[:, idx], j[:, idx]
        sub_nhit = _hit_counts(sub_h, sub_j, gate)
        nhit[idx] = sub_nhit
        p_arr[idx] = p
        cond_by_version[ver] = list(res["conditions"])
        per_version[ver] = {
            "规则版本": ver,
            "期数": len(idx),
            "达标期数": int((sub_nhit > 0).sum()),
            "期占比": float((sub_nhit > 0).mean()),
            "p": p,
            "K·p": n_tickets * p,
            "条件集": [list(c) for c in res["conditions"]],
            "奖级集": sorted(res["grades"]),
            "说明": res["note"],
        }

    valid = nhit > 0
    hit = valid
    rate = float(hit.mean()) if n else 0.0
    se = float(np.sqrt(max(rate * (1 - rate), 0.0) / n)) if n else 0.0
    p_mean = float(p_arr.mean())
    kp = n_tickets * p_mean
    # 随机基线：K 注**各自独立随机**时的并集概率（逐期算再平均，不是先平均 p 再代）
    baseline = float(np.mean(1.0 - (1.0 - p_arr) ** n_tickets))
    space = float(min(1.0, kp) - baseline)
    eff = float(rate / min(1.0, kp)) if kp > 0 else None

    return {
        "门": str(label),
        "彩种": lottery,
        "口径": ("逐期按当期规则（历史事实口径）" if rule_mode == "periodic"
                else f"全期按 {target_issue if target_issue is not None else recs[-1].期号} 那版规则（反事实口径）"),
        "期数": n,
        "K": n_tickets,
        "达标期数": int(hit.sum()),
        "期占比": rate,
        "单期SE": se,
        "p": p_mean,
        "K·p": kp,
        "随机基线": baseline,
        "可争取空间": space,
        "覆盖率效率": eff,
        "条件集": {v: [list(c) for c in c] for v, c in cond_by_version.items()},
        "分版本": per_version,
        "不支持版本": unsupported,
        # ---- 数组（不进 JSON 契约，供 bootstrap / 分窗检验用）----
        "_每期达标": hit,
        "_每期达标注数": nhit,
        "_每期p": p_arr,
        "_期号": [str(r.期号) for r in recs],
    }


# ------------------------------------------------------------------ 多门
def gate_block(lottery: str, h: np.ndarray, j: np.ndarray,
               records: Sequence[Any], K: Optional[int] = None, *,
               labels: Sequence[str] = GATE_ORDER,
               rule_mode: str = "periodic",
               target_issue: Any = None) -> Dict[str, Any]:
    """一次算全部门（同一批票，不重跑 replay）。"""
    series = {str(lb): gate_series(lottery, h, j, records, lb,
                                   rule_mode=rule_mode,
                                   target_issue=target_issue, K=K)
              for lb in labels}
    return {
        "彩种": lottery,
        "K": int(h.shape[0]) if K is None else int(K),
        "期数": len(records),
        "口径": series[str(labels[0])]["口径"] if labels else "",
        "门": series,
        "诚实边界": HONEST_NOTE,
    }


def sanity_violations(block: Dict[str, Any], *, tol_se: float = 3.0) -> List[str]:
    """层 1 否决力：达标率不得超过 `min(1, K·p) + tol_se·SE`。

    `min(1, K·p)` 是**真实概率**的硬上界（Bonferroni：`P(∪Aᵢ) ≤ ΣP(Aᵢ) = K·p`，
    再截到 1），对**同一批票**严格成立。

    ★ 但表里的「期占比」是那个真实概率的**估计量**，不是它本身：n 期里
      达标期数服从 Binomial(n, P)，所以实测值会围着上界上下摆。
      `tol_se` 吸收的正是这部分抽样波动，不是给口径错误留余地。
      判据仍是「越界 ⇒ 先查池/条件集/h-j 同步」，因为口径错误的典型症状
      就是**远超**容差的越界（例如把 2019 的条件集套到 2026 的期上）。

    ★ 反过来**不成立**：达标率低于上界是正常的（票之间往往高度重叠），
      所以本函数只判越界，不判「太低」。
    """
    bad: List[str] = []
    for label, s in (block.get("门") or {}).items():
        if "期占比" not in s:
            continue
        cap = min(1.0, float(s["K·p"]))
        limit = cap + tol_se * float(s["单期SE"])
        if float(s["期占比"]) > limit + 1e-12:
            bad.append(
                f"{label}：达标率 {s['期占比']:.6f} 超过硬上界 min(1,K·p)={cap:.6f}"
                f"（含 {tol_se}σ 容差后 {limit:.6f}）⇒ 判实现缺陷，先查池/条件集/h-j 同步")
    return bad


def to_row(block: Dict[str, Any]) -> Dict[str, Any]:
    """把 `gate_block` 压成可 JSON 的标量表（数组键以 `_` 开头，一律剔除）。"""
    rows: Dict[str, Any] = {}
    for label, s in (block.get("门") or {}).items():
        rows[label] = {k: v for k, v in s.items() if not k.startswith("_")}
    return {"彩种": block.get("彩种"), "K": block.get("K"),
            "期数": block.get("期数"), "口径": block.get("口径"), "门": rows}
