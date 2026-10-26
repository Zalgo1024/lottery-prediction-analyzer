# -*- coding: utf-8 -*-
"""ev/arena/packing.py —— 按**单一目标奖级**构造票组（研究用，默认不接生产）。

与 `ev/arena/design.py::select_union_greedy` 的区别（三处，都可量化）
--------------------------------------------------------------------
1. **多起点**：贪心从若干个不同的初始票集出发各跑一遍，取样本外最好的一个。
   单起点贪心的早期选择错误无法撤销，多起点是廉价的纠偏。
2. **swap 局部搜索**：贪心只做「加」，本模块在满员后做「换一张」的精修
   （移出一张、试入若干候选、只在覆盖提升且 cap 相容时接受）。
3. **三段独立采样**：`m_fit`（起点贪心）/ `m_swap`（局部搜索）/ `m_eval`（唯一可引用的评估）。
   ★ 这三段必须互相独立：在同一批采样上既搜索又评估 = winner's curse，
     Stage 0b 实测高估 1.5~5.3pp，**比效应量本身还大**。本模块把三段种子分开。

为什么还要报 `Bonferroni 二阶上界`
----------------------------------
`P(∪A_i) ≤ Σ P(A_i) − Σ_{i<j} P(A_i∩A_j)` 是**任意票集**的可证明上界（二阶 Bonferroni），
比 `min(1, K·p)` 更紧。它不需要任何统计假设，也不需要额外计算 ——
采样掩码的 `popcount(m_i & m_j)` 就是 `P(A_i∩A_j)` 的 MC 估计。
⇒ 「当前票集离这个上界还有多远」是比「离 K·p 还有多远」更硬的诊断：
   若精确 P 已经贴上二阶上界，就说明**这个门槛上不存在可争取的空间**，
   换任何优化器都一样。

诚实边界（与 design.py 同款）
-----------------------------
· 本模块**不改变单注中奖概率**，只改变 K 注达标事件的重叠结构；
· 采样是「外层均匀」的**模型**分布；真实开奖是否服从它不由本模块声明，
  故优势必须在真实开奖回放上复核，且复核结果才是效应量；
· `样本外覆盖率` 与 `精确 P(并集)` 都是模型口径，与实测命中期比例**不许混报**。
"""
from __future__ import annotations

import heapq
import math
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

import ev.tier_target as TT
from ev.arena.design import (outcome_gate, outcome_masks, overlap_matrix,
                             sample_draws, union_rate, _POP8)
from ev.lowcrowd_backtest import PROD_OVERLAP_CAP
from ev.profit.rules import rules_for


# ------------------------------------------------------------------ 基础工具
def _pop(m: np.ndarray) -> int:
    """uint8 位图的 popcount 总和。"""
    return int(_POP8[m].sum(dtype=np.int64))


def _or_selected(masks: np.ndarray, sel: Sequence[int]) -> np.ndarray:
    acc = np.zeros(masks.shape[1], dtype=np.uint8)
    for i in sel:
        acc |= masks[i]
    return acc


def _unpack(row: np.ndarray, m: int) -> np.ndarray:
    """把 packed 的 uint8 行解回 0/1 数组（取前 m 位）。"""
    return np.unpackbits(np.asarray(row, dtype=np.uint8))[:m]


def coverage_band(masks: np.ndarray, sel: Sequence[int]) -> Dict[str, Any]:
    """覆盖率夹逼：`S1 − S2 ≤ P(∪) ≤ S1`，两者都由**每样本达标票数** n(ω) 的矩给出。

    令 `n(ω)` = 该采样结局下 `sel` 里达标的票数，则逐样本恒有
        `1{n>0} ≥ n − n(n−1)/2`      （n=1: 1≥1；n=2: 1≥1；n=3: 1≥0 …）
    取平均得 `P(∪) ≥ E[n] − E[C(n,2)]`；而上界 `P(∪) ≤ E[n]` 也是同一恒等式。

    ★ 为什么用 n 的矩而不是「逐对掩码 AND」：`P(Aᵢ∩Aⱼ)` 在极小 p 下只有 1e-7 量级，
      而 50k 采样的分辨率是 2e-5 ⇒ 逐对统计**大多观测到 0 次、系统性低估重叠**，
      算出来的「下界」会跑到精确 P 上面去（实测过：0.01775 vs 精确 0.017685）。
      用 n(ω) 则把 780 对的重叠合并成一次计数，无稀疏偏差。

    ★ 但二阶下界在 K 大时本来就**松**（三阶、四阶项全被丢掉）。它只能证明
      「至少这么高」，不能证明「已经到顶」。判「到顶」请用 `覆盖率效率 = 精确P / K·p`。
    """
    K = len(sel)
    if K == 0:
        return {"S1": 0.0, "S1_SE": 0.0, "二阶下界": 0.0, "重叠矩": 0.0,
                "样本数": 0, "说明": "空票集"}
    m = masks.shape[1] * 8
    n = np.zeros(m, dtype=np.int16)
    for i in sel:
        n += _unpack(masks[i], m).astype(np.int16)
    s1 = float(n.mean())
    s2 = float((n.astype(np.float64) * (n - 1) / 2.0).mean())
    se = float(n.std(ddof=1) / np.sqrt(m)) if m > 1 else 0.0
    return {
        "S1": s1,                       # = ΣᵢP(Aᵢ) 的 MC 估计（P(∪) 的上界）
        "S1_SE": se,
        "二阶下界": s1 - s2,             # 松下界，只作参考
        "重叠矩": s2,                    # = Σ_{i<j}P(Aᵢ∩Aⱼ) 的 MC 估计
        "样本数": int(m),
        "最大同现票数": int(n.max()) if m else 0,
        "★口径": "n(ω)=每样本达标票数；逐样本 1{n>0} ≥ n−C(n,2)",
    }


# ---------------------------------------------------------------- 解析联合分布
def _joint_outer(n: int, k: int, c: int) -> Dict[Tuple[int, int], float]:
    """红球交集 `c` 时两票红球命中数 `(h1, h2)` 的**精确**联合分布（多元超几何）。

    号码分四类：只属于 i（k−c 个）、只属于 j（k−c 个）、共有（c 个）、都不属于。
    开奖从这四类里分别抽 a1..a4 个，`h1 = a1 + a3`、`h2 = a2 + a3`。
    """
    nA = k - c
    nB = k - c
    nC = c
    nD = n - (2 * k - c)
    tot = math.comb(n, k)
    out: Dict[Tuple[int, int], float] = {}
    for a1 in range(min(nA, k) + 1):
        for a2 in range(min(nB, k - a1) + 1):
            for a3 in range(min(nC, k - a1 - a2) + 1):
                a4 = k - a1 - a2 - a3
                if a4 < 0 or a4 > nD:
                    continue
                p = (math.comb(nA, a1) * math.comb(nB, a2)
                     * math.comb(nC, a3) * math.comb(nD, a4)) / tot
                key = (a1 + a3, a2 + a3)
                out[key] = out.get(key, 0.0) + p
    return out


def _joint_inner(n_in: int, m: int, b: int) -> Dict[Tuple[int, int], float]:
    """内层交集 `b` 时两票内层命中数 `(j1, j2)` 的精确联合分布（同构）。

    双色球 `m=1`（蓝球抽 1 个）、大乐透 `m=2`（后区抽 2 个）。
    """
    nA = m - b
    nB = m - b
    nC = b
    nD = n_in - (2 * m - b)
    tot = math.comb(n_in, m)
    out: Dict[Tuple[int, int], float] = {}
    for a1 in range(min(nA, m) + 1):
        for a2 in range(min(nB, m - a1) + 1):
            for a3 in range(min(nC, m - a1 - a2) + 1):
                a4 = m - a1 - a2 - a3
                if a4 < 0 or a4 > nD:
                    continue
                p = (math.comb(nA, a1) * math.comb(nB, a2)
                     * math.comb(nC, a3) * math.comb(nD, a4)) / tot
                key = (a1 + a3, a2 + a3)
                out[key] = out.get(key, 0.0) + p
    return out


def pair_hit_table(lottery: str, target: Any, issue: Any
                   ) -> Tuple[np.ndarray, np.ndarray, int, int]:
    """`F[c, b]` = 红球交集 c、内层交集 b 的两票**同时达标**的精确概率。

    ★ 这是本模块「直接计算中奖事件并集」的核心构件：只需一张
      `(k_out+1) × (m_in+1)` 的小表（双色球 7×2、大乐透 6×3），
      就能对**任意**票对查到 `P(Aᵢ∩Aⱼ)`，完全没有采样噪声。
    """
    from ev.arena.design import outcome_gate
    from ev.tier_coverage import _inner_pool_size

    gate, w_out, m_in, n_in = outcome_gate(lottery, target, issue)
    r = rules_for(lottery, issue)
    n = int(r.outer_range[1])
    k = int(r.outer_width)
    F = np.zeros((k + 1, m_in + 1), dtype=np.float64)
    cache_out = {c: _joint_outer(n, k, c) for c in range(k + 1)}
    cache_in = {b: _joint_inner(n_in, m_in, b) for b in range(m_in + 1)}
    for c in range(k + 1):
        for b in range(m_in + 1):
            tot = 0.0
            for (h1, h2), po in cache_out[c].items():
                if h1 > w_out or h2 > w_out:
                    continue
                for (j1, j2), pi in cache_in[b].items():
                    if gate[h1, j1] and gate[h2, j2]:
                        tot += po * pi
            F[c, b] = tot
    return F, np.array([0.0]), k, m_in


def inner_overlap_matrix(pool: Sequence[Dict[str, Any]], lottery: str,
                         n_in: int) -> np.ndarray:
    """候选池的内层（蓝球/后区）两两交集大小。"""
    B = np.zeros((len(pool), n_in), dtype=np.int16)
    for i, t in enumerate(pool):
        v = t.get("蓝球")
        for x in (list(v) if isinstance(v, (list, tuple)) else [v]):
            if x is not None:
                B[i, int(x) - 1] = 1
    return (B @ B.T).astype(np.int8)


def analytic_union(sel: Sequence[int], ov: np.ndarray, bv: np.ndarray,
                   F: np.ndarray, p1: float) -> Dict[str, float]:
    """两两修正的并集近似：`P(∪) ≈ K·p − Σ_{i<j} P(Aᵢ∩Aⱼ)`。

    ★ 严格说它是 **Bonferroni 二阶下界**（丢掉的三阶及以上项全为正 ⇒ 它偏低），
      但实测偏低量极小：K=60、`K·p=0.027` 时只差 0.013%（−3.5e-6）；
      两票时**完全相等**（没有高阶项）。
      ⇒ 用作**搜索目标**足够了，而且它是确定性的、没有采样噪声 ——
        这才是它相对 MC 覆盖率的决定性优势。
    ★ 引用「并集概率」时**必须换成精确枚举值**（`exact_union_probability`），
      不要把本函数的返回值当结论报出去。
    """
    K = len(sel)
    if K < 2:
        return {"两两修正": float(K * p1), "两两重叠总量": 0.0, "K·p": float(K * p1)}
    ix = np.asarray(list(sel), dtype=np.int64)
    sub_c = ov[np.ix_(ix, ix)]
    sub_b = bv[np.ix_(ix, ix)]
    pair = F[sub_c, sub_b]
    iu = np.triu_indices(K, 1)
    overlap = float(pair[iu].sum())
    return {"两两修正": float(K * p1 - overlap), "两两重叠总量": overlap,
            "K·p": float(K * p1)}


def _swap_refine_analytic(sel: List[int], ov: np.ndarray, bv: np.ndarray,
                          F: np.ndarray, cap: int, rng: np.random.Generator,
                          *, rounds: int = 5, n_cand: int = 64
                          ) -> Tuple[List[int], Dict[str, int]]:
    """用**解析**目标做「换一张」局部搜索 —— 无采样噪声，故不会像 MC 版那样过拟合。

    可接受条件（三条同时满足）：
      1. 新票与其余票的红球交集 ≤ cap（口径不许放宽）；
      2. 与其余票的两两重叠总量**严格下降**（等价于一阶近似严格上升）；
      3. 新票不在当前票集里。
    """
    sel = list(sel)
    K = len(sel)
    if K < 2:
        return sel, {"尝试": 0, "接受": 0}
    P = ov.shape[0]
    tried = accepted = 0
    for _ in range(int(rounds)):
        for slot in rng.permutation(K):
            slot = int(slot)
            rest = sel[:slot] + sel[slot + 1:]
            ridx = np.asarray(rest, dtype=np.int64)
            rows_c = ov[:, ridx]                 # (P, K-1)
            rows_b = bv[:, ridx]
            cur_costs = F[rows_c, rows_b].sum(axis=1)     # 全池每张票的候选成本
            in_use = set(rest)
            cands = rng.choice(P, size=min(int(n_cand) * 8, P), replace=False)
            best, best_cost = -1, cur_costs[sel[slot]]
            for c in cands:
                c = int(c)
                if c in in_use:
                    continue
                tried += 1
                if rest and int(ov[c, ridx].max()) > cap:
                    continue
                cost = float(cur_costs[c])
                if cost < best_cost - 1e-18:
                    best, best_cost = c, cost
            if best >= 0:
                sel[slot] = best
                accepted += 1
    return sel, {"尝试": tried, "接受": accepted}
def _greedy_from(masks: np.ndarray, ov: np.ndarray, K: int, cap: int,
                 init: Sequence[int], *, max_reject: int = 200_000
                 ) -> Tuple[List[int], Dict[str, Any]]:
    """CELF 贪心，从给定的初始票集出发补满 K 张（cap 只许满足，不许放宽）。"""
    P, nb = masks.shape
    sel: List[int] = list(init)
    acc = _or_selected(masks, sel)
    inv = ~acc
    used = set(sel)
    rej = 0
    heap: List[Tuple[float, int, int]] = []
    for i in range(P):
        if i in used:
            continue
        heapq.heappush(heap, (-float(_pop(masks[i] & inv)), i, -1))
    step = 0
    while len(sel) < K and heap:
        if rej > max_reject:
            break
        _neg, i, stamp = heapq.heappop(heap)
        if stamp != step:
            heapq.heappush(heap, (-float(_pop(masks[i] & inv)), i, step))
            continue
        if sel and int(ov[i, sel].max()) > cap:
            rej += 1
            continue
        sel.append(i)
        acc |= masks[i]
        inv = ~acc
        step += 1
    return sel, {"cap拒绝次数": rej, "初始票数": len(list(init)),
                 "重叠上限": int(cap), "放宽次数": 0}


# ------------------------------------------------------------------ swap 精修
def _swap_refine(masks: np.ndarray, ov: np.ndarray, sel: List[int], K: int,
                 cap: int, rng: np.random.Generator, *,
                 rounds: int = 2, n_cand: int = 16
                 ) -> Tuple[List[int], Dict[str, int]]:
    """**仅作对照**：在 MC 覆盖率上做「换一张」局部搜索（主流程**不用**它）。

    ★ 保留它是为了让「MC 目标不work」这件事可复现，而不是靠记忆：
      实测（双色球 K=200、≥四等、m_swap=100k）搜索批上覆盖率 +2.4~3.5pp、
      独立评估批 −0.12~+0.06pp ⇒ 全部是采样噪声的过拟合。
      根因是 `K·p ≈ 0.089` 时 MC 覆盖率的相对标准误太大（S1 在搜索批上被
      winner's curse 抬到 0.036，而独立批只有 0.018，差近 2 倍）。
      ⇒ 主流程改用 `_swap_refine_analytic`（解析目标，无采样噪声）。
    """
    sel = list(sel)
    if len(sel) < K or K < 2:
        return sel, {"尝试": 0, "接受": 0}
    P = masks.shape[0]
    cur = _pop(_or_selected(masks, sel))
    tried = accepted = 0
    for _ in range(int(rounds)):
        for slot in rng.permutation(K):
            slot = int(slot)
            rest = sel[:slot] + sel[slot + 1:]
            base = _or_selected(masks, rest)
            base_pop = _pop(base)
            in_use = set(rest)
            cands = rng.choice(P, size=min(int(n_cand) * 4, P), replace=False)
            best_i, best_pop = -1, cur
            for c in cands:
                c = int(c)
                if c in in_use:
                    continue
                if rest and int(ov[c, rest].max()) > cap:
                    continue
                tried += 1
                pop = _pop(base | masks[c])
                if pop > best_pop:
                    best_i, best_pop = c, pop
            if best_i >= 0:
                sel[slot] = best_i
                cur = best_pop
                accepted += 1
            else:
                _ = base_pop          # 未接受时 cur 不变（显式保留，便于调试）
    return sel, {"尝试": tried, "接受": accepted}


# ------------------------------------------------------------------ 选号入口
def select_tier_packing(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                        target: Any = None, issue: Any = None,
                        *, cap: Optional[int] = None, seed: int = 0,
                        m_fit: int = 100_000, m_swap: int = 100_000,
                        m_eval: int = 100_000, n_starts: int = 2,
                        n_init: int = 3, swap_rounds: int = 2,
                        swap_candidates: int = 16,
                        seed_offsets: Optional[Dict[str, int]] = None
                        ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """针对**单一目标奖级**构造 K 注票组，返回 `(票集, meta)`。

    参数
    ----
    cap              红球重叠上限，默认生产口径 `PROD_OVERLAP_CAP = 3`
    n_starts         多起点个数（每个起点换一组初始票）
    n_init           每个起点的初始票数（之后由贪心补满）
    swap_rounds      swap 局部搜索的轮数（每轮遍历全部 K 个位置）
    swap_candidates  每个位置尝试的候选票数
    seed_offsets     采样种子偏移（fit=0 / eval=+10000）
    m_swap           ★ **已弃用**：swap 改用解析目标（无采样），此参数仅为兼容保留

    搜索目标为什么用解析式而不是 MC 覆盖率
    --------------------------------------
    MC 覆盖率在 `K·p ≪ 1` 时几乎全是噪声：实测同一副票在搜索批上覆盖率 0.036、
      在独立评估批上 0.018（差近 2 倍）⇒ 在 MC 上做局部搜索只会**过拟合采样噪声**
      （实测 swap 在搜索批上 +2.4pp、样本外 −0.1pp）。
    改用 `P(Aᵢ∩Aⱼ)` 的解析表后，目标函数是确定的，不存在这个问题。
    """
    if cap is None:
        cap = int(PROD_OVERLAP_CAP)
    pool = list(pool)
    if not pool:
        raise ValueError("候选池为空")
    off = {"fit": 0, "swap": 5_000, "eval": 10_000}
    if seed_offsets:
        off.update({k: int(v) for k, v in seed_offsets.items()})

    gate, _w, m_in, n_in = outcome_gate(lottery, target, issue)
    r = rules_for(lottery, issue)
    if not getattr(r, "outer_choose", False):
        raise ValueError(f"{lottery} 不是红蓝型彩种，packing 未实现")
    n_out = int(r.outer_range[1])
    k_out = int(r.outer_width)

    t0 = time.time()
    ov = overlap_matrix(pool, lottery)
    bv = inner_overlap_matrix(pool, lottery, n_in)
    F, _dummy, _kk, _mm = pair_hit_table(lottery, target, issue)
    res = TT.resolve_conditions(lottery, target, issue)
    p1 = TT.single_ticket_probability(lottery, res["conditions"], issue)

    def _draw(m: int, offset: int):
        rng = np.random.default_rng(int(seed) + int(offset))
        R, B = sample_draws(rng, n_out, k_out, n_in, m_in, int(m))
        return outcome_masks(pool, lottery, R, B, gate)

    # ---- 阶段 1：多起点贪心（m_fit）
    masks_fit = _draw(m_fit, off["fit"])
    rng = np.random.default_rng(int(seed) + 7919)
    starts: List[Dict[str, Any]] = []
    for s in range(int(n_starts)):
        init: List[int] = []
        for _ in range(int(n_init)):
            for _try in range(200):
                c = int(rng.integers(0, len(pool)))
                if c in init:
                    continue
                if init and int(ov[c, init].max()) > cap:
                    continue
                init.append(c)
                break
        sel, tr = _greedy_from(masks_fit, ov, int(K), int(cap), init)
        starts.append({"起点": s, "选中": sel, "轨迹": tr,
                       "拟合覆盖率": union_rate(masks_fit, sel)})
    best = max(starts, key=lambda d: (d["拟合覆盖率"], -len(d["选中"])))
    sel = list(best["选中"])
    fit_rate = float(best["拟合覆盖率"])
    del masks_fit
    a0 = analytic_union(sel, ov, bv, F, p1)

    # ---- 阶段 2：swap 精修（**解析目标**，独立于任何采样 ⇒ 不会过拟合采样噪声）
    sel, swap_tr = _swap_refine_analytic(sel, ov, bv, F, int(cap), rng,
                                        rounds=swap_rounds, n_cand=swap_candidates)
    a1 = analytic_union(sel, ov, bv, F, p1)

    # ---- 阶段 3：样本外评估（m_eval，独立流）—— 报告用，不参与任何搜索
    masks_eval = _draw(m_eval, off["eval"])
    eval_rate = union_rate(masks_eval, sel)
    eval_sel_pre = list(best["选中"])
    eval_pre = union_rate(masks_eval, eval_sel_pre)
    band = coverage_band(masks_eval, sel)
    del masks_eval

    tickets = [pool[i] for i in sel]
    kp = min(1.0, K * p1)
    meta: Dict[str, Any] = {
        "策略": "奖级专用构造",
        "彩种": lottery,
        "目标": res["label"],
        "期号": None if issue is None else str(issue),
        "规则版本": res["rule_version"],
        "单注达标概率": p1,
        "理论上限Kp": kp,
        "随机基线": 1.0 - (1.0 - p1) ** K if K else 0.0,
        "选中注数": len(tickets),
        "拟合覆盖率": fit_rate,
        "swap前覆盖率_解析": a0["两两修正"],
        "swap后覆盖率_解析": a1["两两修正"],
        "样本外覆盖率": eval_rate,
        "swap前_样本外": eval_pre,
        "swap增益_解析": a1["两两修正"] - a0["两两修正"],
        "swap增益_样本外": eval_rate - eval_pre,
        "两两重叠总量": a1["两两重叠总量"],
        "覆盖率上界_S1": band["S1"],
        "覆盖率上界_SE": band["S1_SE"],
        "二阶下界_松": band["二阶下界"],
        "重叠矩": band["重叠矩"],
        "最大同现票数": band["最大同现票数"],
        "覆盖率占Kp比": (eval_rate / kp) if kp > 0 else None,
        "多点起点数": int(n_starts),
        "起点拟合覆盖率": [round(float(d["拟合覆盖率"]), 6) for d in starts],
        "cap拒绝次数": int(sum(d["轨迹"]["cap拒绝次数"] for d in starts)),
        "swap尝试数": swap_tr["尝试"],
        "swap接受数": swap_tr["接受"],
        "重叠上限": int(cap),
        "放宽次数": 0,
        "拟合采样数": int(m_fit),
        "评估采样数": int(m_eval),
        "swap轮数": int(swap_rounds),
        "swap候选数": int(swap_candidates),
        "seed": int(seed),
        "两阶段seed": {"fit": int(seed) + off["fit"],
                       "eval": int(seed) + off["eval"]},
        "耗时秒": round(time.time() - t0, 1),
        "说明": ("搜索目标 = 解析一阶并集（无采样噪声），报告覆盖率 = 独立评估批 + 精确枚举。"
                 "`覆盖率上界_S1` = ΣᵢP(Aᵢ) 的 MC 估计（P(∪) 的严格上界，含 SE）；"
                 "`二阶下界_松` 是 Bonferroni 二阶下界，K 大时偏松，只作参考。"
                 "★ 判「是否已到顶」用 `覆盖率效率 = 精确P / K·p`，不要用下界。"
                 "★ `swap增益_解析` 与 `swap增益_样本外` 必须成对读：两者同号同量级"
                 "才说明改进是真的；若只有解析增益而没有样本外增益，说明目标函数与"
                 "真实并集脱节（早期 MC 版就是这个症状：搜索批 +2.4pp、样本外 −0.1pp）。"),
    }
    if len(tickets) < K:
        meta["容量不足"] = f"只选出 {len(tickets)}/{K} 注（池 {len(pool)}，cap={cap}）"
    return tickets, meta
