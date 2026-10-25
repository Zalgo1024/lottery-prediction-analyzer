# -*- coding: utf-8 -*-
"""ev/arena/design.py —— 直接在 P(并集) 上贪心的选号设计（研究用，默认不接生产）。

为什么有这个模块
----------------
`ev/tier_coverage.select_tier_coverage` 贪心优化的是**通道权重和**，那是
P(并集) 的**上界**（通道互相重叠，含高阶交叉项），不是 P(并集) 本身。
Stage 0b 实测：双色球 K=200 时代理贪心只吃到理论空间的 1.5%，
直接对 P(并集) 做贪心能吃到 16.8% ⇒ **设计方法是比预算大一个数量级的杠杆**。

做法
----
1. 从「外层均匀」分布采 m 个开奖结局（这是**模型**分布，不是真实历史）；
2. 把每张候选票在每个采样结局上「是否达标」压成 1 bit ⇒ 位图 (P, ceil(m/8))；
3. 在**这些采样结局的并集覆盖率**上做 CELF 贪心（次模 ⇒ 贪心有 1−1/e 保证），
   并强制红球重叠 ≤ cap；
4. ★ **样本外评估**：优化用 `m_fit` 个采样，评估用**另一批独立采样** `m_eval`。
   在同一批采样上既优化又评估 ⇒ winner's curse，Stage 0b 实测高估 1.5~5.3pp，
   **比效应量本身还大**。

诚实边界
--------
· 本模块**不改变单注中奖概率**，只改变 K 注达标事件的**重叠结构**；
· 采样分布是「外层均匀」的模型分布。真实开奖是否服从它，不由本模块声明；
  因此优化得到的优势必须在**真实开奖回放**上复核，且复核结果才是效应量；
· 报告效应量时，`P_fit` 只作诊断、`P_eval` 与回放实测才是可引用的数。
"""
from __future__ import annotations

import heapq
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

import ev.tier_target as TT
from ev.profit.rules import rules_for
from ev.lowcrowd_backtest import PROD_OVERLAP_CAP  # 生产口径重叠上限 = 3

# 位计数查表（uint8 → popcount）
_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

# 采样规模上限（防呆）：P×m 位图内存 = P*m/8 字节
MAX_SAMPLE = 2_000_000

# 走 (外层, 内层) 两层判级的彩种。
# ★ 只登记**已做过逐票审计对账**的两个彩种。七星彩/排列5 结构上也有 (h, j)，
#   但「红球/蓝球」字段语义与判级口径都没验证过，宁可显式拒绝，
#   也不要算出一个没人对过账的数。要扩展须先补审计对账。
REDBLUE_LOTTERIES = frozenset({"双色球", "大乐透"})


# ---------------------------------------------------------------- 达标门
def outcome_gate(lottery: str, target: Any, issue: Any) -> Tuple[np.ndarray, int, int, int]:
    """`(gate, w_out, m_in, n_in)`：gate[h, j] = 外层命中 h 且内层命中 j 是否达标。

    ★ 期号必须显式传入：`大乐透` 有 5 个规则版本，「四等」跨版本不是同一件事。
    """
    if lottery not in REDBLUE_LOTTERIES:
        raise ValueError(
            f"{lottery} 不是已对账过的红蓝型彩种（只支持 {sorted(REDBLUE_LOTTERIES)}）。"
            f"★ 数字型（排列3/福彩3D）不能用前后区拆分口径；"
            f"七星彩/排列5 结构上可行但未做逐票审计对账。")
    r = rules_for(lottery, issue)
    res = TT.resolve_conditions(lottery, target, issue)
    grades = set(res["grades"])
    w_out = int(r.outer_width)
    m_in = max(jj for (_h, jj) in r.grade_table)     # 开奖抽几个内层号
    from ev.tier_coverage import _inner_pool_size
    n_in = _inner_pool_size(r, m_in)                 # 内层池大小（16 / 12）
    gate = np.zeros((w_out + 1, m_in + 1), dtype=bool)
    for (hh, jj), gname in r.grade_table.items():
        if hh <= w_out and jj <= m_in and gname in grades:
            gate[hh, jj] = True
    return gate, w_out, m_in, n_in


# ---------------------------------------------------------------- 采样
def sample_draws(rng: np.random.Generator, n_out: int, k_out: int,
                 n_in: int, k_in: int, m: int) -> Tuple[np.ndarray, np.ndarray]:
    """采 m 个独立均匀开奖结局，返回 one-hot `(R, B)`。"""
    if m > MAX_SAMPLE:
        raise ValueError(f"采样规模 {m} 超出上限 {MAX_SAMPLE}")
    oi = np.argsort(rng.random((m, n_out)), axis=1)[:, :k_out]
    ii = np.argsort(rng.random((m, n_in)), axis=1)[:, :k_in]
    R = np.zeros((m, n_out), dtype=np.float32)
    B = np.zeros((m, n_in), dtype=np.float32)
    np.put_along_axis(R, oi, 1.0, axis=1)
    np.put_along_axis(B, ii, 1.0, axis=1)
    return R, B


def _onehot(tickets: Sequence[Dict[str, Any]], key: str, width: int) -> np.ndarray:
    Mx = np.zeros((len(tickets), width), dtype=np.int8)
    for i, t in enumerate(tickets):
        v = t.get(key)
        for x in (list(v) if isinstance(v, (list, tuple)) else [v]):
            if x is not None:
                Mx[i, int(x) - 1] = 1
    return Mx


def overlap_matrix(pool: Sequence[Dict[str, Any]], lottery: str) -> np.ndarray:
    """候选池的红球两两交集大小（与 `ev.selection._overlap` 同口径）。"""
    A = _onehot(pool, "红球", int(rules_for(lottery).outer_range[1]))
    return (A.astype(np.int16) @ A.astype(np.int16).T).astype(np.int8)


def outcome_masks(pool: Sequence[Dict[str, Any]], lottery: str,
                  R: np.ndarray, B: np.ndarray, gate: np.ndarray,
                  *, chunk: int = 512) -> np.ndarray:
    """把「每张票 × 每个采样结局 是否达标」压成位图 `(P, ceil(m/8))` uint8。"""
    n_out = R.shape[1]
    A = _onehot(pool, "红球", n_out).astype(np.float32)
    Bm = _onehot(pool, "蓝球", B.shape[1]).astype(np.float32)
    Rt = np.ascontiguousarray(R.T.astype(np.float32))
    Bt = np.ascontiguousarray(B.T.astype(np.float32))
    m = R.shape[0]
    out = np.zeros((A.shape[0], (m + 7) // 8), dtype=np.uint8)
    for s in range(0, A.shape[0], chunk):
        e = min(A.shape[0], s + chunk)
        h = (A[s:e] @ Rt).astype(np.int16)
        j = (Bm[s:e] @ Bt).astype(np.int16)
        out[s:e] = np.packbits(gate[h, j], axis=1)
    return out


def union_rate(masks: np.ndarray, sel: Sequence[int]) -> float:
    """给定票集在采样结局上的**并集覆盖率**（= P(并集) 的 MC 估计）。"""
    if not sel:
        return 0.0
    acc = np.zeros(masks.shape[1], dtype=np.uint8)
    for i in sel:
        acc |= masks[i]
    return float(_POP8[acc].sum(dtype=np.int64)) / float(masks.shape[1] * 8)


# ---------------------------------------------------------------- CELF 贪心
def greedy_union(masks: np.ndarray, ov: np.ndarray, K: int, cap: int, *,
                 max_reject: int = 200_000) -> Tuple[List[int], Dict[str, Any]]:
    """在并集覆盖率上做 CELF 贪心，强制两两红球重叠 ≤ cap。

    返回 `(选中下标, trace)`。凑不满 K 时 trace 记录 `cap拒绝` 次数与 `出票不足`。
    ★ cap 只由调用方给，**不做自动放宽**（`ev.tier_coverage.RELAX_MAX` 会把
      cap 悄悄抬到 4 且不报错，那是 G3 污染源，这里不允许）。
    """
    P, nb = masks.shape
    cov = np.zeros(nb, dtype=np.uint8)
    cov_inv = np.full(nb, 0xFF, dtype=np.uint8)
    sel: List[int] = []
    rej = 0
    heap: List[Tuple[float, int, int]] = []
    for i in range(P):
        heapq.heappush(heap, (-float(_POP8[masks[i] & cov_inv].sum(dtype=np.int64)), i, -1))
    step, sat_at = 0, None
    while len(sel) < K and heap:
        if rej > max_reject:
            break
        _neg, i, stamp = heapq.heappop(heap)
        if stamp != step:
            g = float(_POP8[masks[i] & cov_inv].sum(dtype=np.int64))
            heapq.heappush(heap, (-g, i, step))
            continue
        if sel and int(ov[i, sel].max()) > cap:
            rej += 1
            continue
        sel.append(i)
        cov |= masks[i]
        cov_inv = ~cov
        step += 1
        if sat_at is None and not cov_inv.any():
            sat_at = step
    trace = {
        "cap拒绝次数": rej,
        "采样结局数": int(masks.shape[1] * 8),
        "覆盖饱和步号": sat_at,
        "候选池规模": P,
        "重叠上限": cap,
        "放宽次数": 0,          # 恒定 0：本函数不允许放宽
    }
    return sel, trace


# ---------------------------------------------------------------- 选号入口
def select_union_greedy(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                        target: Any = None, issue: Any = None,
                        *, cap: Optional[int] = None, seed: int = 0,
                        m_fit: int = 100_000, m_eval: int = 100_000,
                        eval_seed_offset: int = 10_000) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """在 P(并集) 上贪心选 K 注，并给出**样本外**的覆盖率估计。

    参数
    ----
    cap    红球重叠上限，默认生产口径 `PROD_OVERLAP_CAP = 3`
    seed   优化采样种子（`m_fit` 个结局）
    m_eval 独立评估采样规模；评估种子 = seed + eval_seed_offset（独立流）

    返回 `(票集, meta)`。meta 里：
      · `拟合覆盖率` —— 优化用的那批采样上的覆盖率（**偏高，只作诊断**）
      · `样本外覆盖率` —— 独立采样上的覆盖率（可引用）
      · 两者之差 = winner's curse 的量化
    """
    if cap is None:
        cap = int(PROD_OVERLAP_CAP)
    pool = list(pool)
    if not pool:
        raise ValueError("候选池为空")
    gate, w_out, m_in, n_in = outcome_gate(lottery, target, issue)
    r = rules_for(lottery, issue)
    n_out = int(r.outer_range[1])
    k_out = int(r.outer_width)
    if not getattr(r, "outer_choose", False):
        raise ValueError(f"{lottery} 不是红蓝型彩种，采样贪心未实现")

    t0 = time.time()
    ov = overlap_matrix(pool, lottery)
    rng = np.random.default_rng(int(seed))
    Rf, Bf = sample_draws(rng, n_out, k_out, n_in, m_in, int(m_fit))
    masks = outcome_masks(pool, lottery, Rf, Bf, gate)
    sel, trace = greedy_union(masks, ov, int(K), int(cap))
    fit_rate = union_rate(masks, sel)
    del masks, Rf, Bf

    rng_e = np.random.default_rng(int(seed) + int(eval_seed_offset))
    Re, Be = sample_draws(rng_e, n_out, k_out, n_in, m_in, int(m_eval))
    masks_e = outcome_masks(pool, lottery, Re, Be, gate)
    eval_rate = union_rate(masks_e, sel)
    del masks_e, Re, Be

    tickets = [pool[i] for i in sel]
    res = TT.resolve_conditions(lottery, target, issue)
    p1 = TT.single_ticket_probability(lottery, res["conditions"], issue)
    kp = min(1.0, K * p1)
    meta: Dict[str, Any] = {
        "策略": "并集贪心设计",
        "彩种": lottery,
        "目标": res["label"],
        "期号": None if issue is None else str(issue),
        "规则版本": res["rule_version"],
        "单注达标概率": p1,
        "理论上限Kp": kp,
        "随机基线": 1.0 - (1.0 - p1) ** K if K else 0.0,
        "选中注数": len(tickets),
        "拟合覆盖率": fit_rate,
        "样本外覆盖率": eval_rate,
        "优化乐观量": fit_rate - eval_rate,
        "拟合采样数": int(m_fit),
        "评估采样数": int(m_eval),
        "seed": int(seed),
        "评估seed": int(seed) + int(eval_seed_offset),
        "耗时秒": round(time.time() - t0, 1),
        "说明": ("拟合覆盖率必然 ≥ 样本外覆盖率（同批采样上既优化又评估）；"
                 "引用效应量时只能用样本外覆盖率或真实开奖回放实测。"),
    }
    meta.update(trace)
    if len(tickets) < K:
        meta["容量不足"] = f"只选出 {len(tickets)}/{K} 注（池 {len(pool)}，cap={cap}）"
    return tickets, meta
