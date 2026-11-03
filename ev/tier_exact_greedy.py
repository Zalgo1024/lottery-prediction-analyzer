# -*- coding: utf-8 -*-
"""精确边际增益的组合优化器：直接最大化「至少一注达标」的**并集概率**。

## 它解决什么问题

`ev/tier_coverage.py` 用「通道权重和」当打分（Σ 通道概率）。那是 P(并集) 的
**一阶上界**，且同一票内 h 大被 h 小包含 ⇒ 系统性高估（双色球 ≥四等：单票
Σw ≈ 12.7 × p）。更糟的是：每张候选票的通道数与权重和**完全相同**，
所以那个贪心的第一注实际上是由 1e-15 的 jitter 决定的（等价于任意起手），
真正起作用的只是「号码子集去重」这条启发式。

本模块把目标换成用户定义的那个式子：

    P(至少一注达标) = Σ_ω P(ω) · 1{票组中至少一注在开奖状态 ω 达标}

在**外层均匀**（κ=0）下，这就是「被覆盖的 (外层结局, 内层结局) 对数 ÷ 总对数」。
于是每加一注的**增量**是一个可精确计数的整数：新覆盖了多少个 (ρ, B) 格。
贪心每步选「新覆盖格数最多」的票 —— 这正是子模覆盖的最大化，
与 `ev/arena/design.py` 的 MC 版 `union_greedy` 同一目标，但**没有采样误差**。

## 为什么能算得动（关键实现技巧）

外层结局全枚举：双色球 C(33,6)=1,107,568、大乐透 C(35,5)=324,632。
对每张候选票，逐个 ρ 算增益是 1.1M 次运算 ⇒ 太慢（实测 54 ms/次）。

但 `gate[h][j]` 只在少数 h 上为真（双色球 ≥四等只在 h=6/5/4 为真），
所以一张票的**支撑集**（可能贡献增益的 ρ）只有 5,428 个而不是 110 万个。
故：每张候选票**只算一次** hits（取 h ≥ h_min 的 ρ 下标，缓存），
之后每次评估只在支撑集上做位运算 + popcount（实测 0.2 ms）。

## 与代理版的关系（诚实边界）

1. 本模块**不改变任何一注的中奖概率**：它只改变 K 注之间「达标结局」的重叠结构。
   单注达标概率 p 与选哪些号无关（`E[达标注数] = K·p`）。
2. 可争取空间受 `P(并集) ≤ min(1, K·p)` 约束，且实际空间是 O((K·p)²) 量级 ——
   报收益**必须同时报 K 与 K·p**，否则数字无法解释。
3. 本模块只做「覆盖」这一个目标。它不预测开奖号码、不保证长期盈利。
4. 次级目标（如 ≥5 元）**只用于并列时的次级排序**，不与主目标加权成一个总分 ——
   混分会为了堆小奖牺牲高奖，且权重无法事先约定。

## 适用边界

- 仅支持**红蓝型**彩种（`rules.outer_choose=True`：双色球 / 大乐透）。
  数字型与七星彩是位置型，判级不按 (h,j) 分桶，本模块不接管（直接 raise，不静默降级）。
- 外层枚举上限复用 `tier_coverage.MAX_OUTER_ENUM`（2,000,000）：七乐彩
  C(30,7)=2,035,800 恰超上限 ⇒ 不接管；快乐8 状态空间过大 ⇒ 不接管。
- `κ≠0`（机制倾斜）下的外层非均匀分布未实现，直接 raise。
"""

from __future__ import annotations

import math
import time
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ev import tier_target as TT

# 单次评估的候选池上限（防呆）。预注册的生产口径是 8000，故放到 8000。
# ★ 代价是内存：每张票的支撑集 ≈ 5,428 × (int32 下标 4B + int8 命中数 1B) ≈ 27 KB，
#   8000 注约 217 MB。再往上加（例如 20000）就会把机器吃掉，别随手调大。
MAX_POOL = 8000
# 24 位以内用字节查表做 popcount，避免依赖 numpy 版本（1.24 无 bitwise_count）
_LUT = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
# ★ 双色球内层只有 C(16,1)=16 个结局 ⇒ 整个覆盖掩码是一个 uint16。
#   于是 popcount 可以退化成**一次 65536 项查表**（比通用按字节拆位快约一个量级），
#   而贪心的内循环要跑 K×池 次，这个常数直接决定 K=500 能不能跑。
_LUT16 = np.array([bin(i).count("1") for i in range(1 << 16)], dtype=np.uint8)


def _popcount_words(arr: np.ndarray) -> np.ndarray:
    """(... , W) uint64 → (...) int64 popcount（按字节查表，不依赖 numpy 版本）。"""
    if arr.size == 0:
        return np.zeros(arr.shape[:-1], dtype=np.int64)
    b = np.ascontiguousarray(arr).view(np.uint8).reshape(-1, arr.shape[-1] * 8)
    return _LUT[b].sum(axis=1, dtype=np.int64).reshape(arr.shape[:-1])


class _Ctx:
    """一次优化所需的全部静态结构（按 彩种+目标+期号 构建一次，可复用）。"""

    __slots__ = ("lottery", "issue", "w_out", "n_in", "N_in", "W", "gate",
                 "mask", "h_min", "E_all", "M_out", "rules", "res", "cost",
                 "fast16", "mask16", "inners", "_inner_index", "INT")

    def __init__(self, lottery: str, target: Any, issue: Any):
        from ev.profit.outcomes import outer_encoding
        from ev.profit.rules import rules_for
        from ev.tier_coverage import MAX_OUTER_ENUM, _inner_pool_size

        self.lottery = lottery
        self.issue = issue
        r = rules_for(lottery, issue)
        if not getattr(r, "outer_choose", False):
            raise ValueError(
                f"{lottery} 不是红蓝型彩种：判级不是按 (外层命中 h, 内层命中 j) 分桶，"
                "本模块的枚举口径不适用（数字型/位置型请走 ev/tier_coverage 的通道口径）")
        self.rules = r
        self.cost = float(getattr(r, "cost", 2.0) or 2.0)

        res = TT.resolve_conditions(lottery, target, issue)
        self.res = res
        grades = set(res["grades"])
        self.w_out = int(r.outer_width)
        m_in = max(jj for (_h, jj) in r.grade_table)
        self.n_in = _inner_pool_size(r, m_in)          # 内层**池**大小（16 / 12）
        self.N_in = math.comb(self.n_in, m_in)         # 内层**结局**数（16 / 66）
        self.W = (self.N_in + 63) // 64

        gate = np.zeros((self.w_out + 1, m_in + 1), dtype=bool)
        for (hh, jj), gname in r.grade_table.items():
            if hh <= self.w_out and jj <= m_in and gname in grades:
                gate[hh, jj] = True
        if not gate.any():
            raise ValueError(f"{lottery} 目标 {res['label']} 没有任何可达格")
        self.gate = gate
        # 只有这些外层命中数可能贡献增益（其余 h 的 mask 全零）
        hs = [h for h in range(self.w_out + 1) if gate[h].any()]
        self.h_min = min(hs)

        inners = np.array(list(combinations(range(self.n_in), m_in)), dtype=np.int16)
        # ★ 掩码必须按「票面内层**组合**」索引，不能按单个内层号：
        #   双色球蓝球 1 个号时两者恰好一致（16 个槽位），但大乐透后区是 2 选 2、
        #   N_in=C(12,2)=66，用单个号索引会算错一个量级（实测 0.0040 vs 0.0075）。
        self.inners = [tuple(int(x) for x in row) for row in inners]
        self._inner_index = {s: i for i, s in enumerate(self.inners)}
        # INT[s, B] = 票面内层组合 s 与开奖内层组合 B 的交集大小（就是 j）
        INT = np.zeros((self.N_in, self.N_in), dtype=np.int16)
        sets = [set(s) for s in self.inners]
        for s in range(self.N_in):
            for k in range(self.N_in):
                INT[s, k] = len(sets[s] & sets[k])
        self.INT = INT

        # mask[h, s, w]：票面内层组合 = s、外层命中 h 时，**达标的内层结局**位掩码
        mask = np.zeros((self.w_out + 1, self.N_in, self.W), dtype=np.uint64)
        for h in range(self.w_out + 1):
            for s in range(self.N_in):
                for k in np.flatnonzero(gate[h][INT[s]]):
                    mask[h, s, int(k) >> 6] |= np.uint64(1 << (int(k) & 63))
        self.mask = mask

        E_all = outer_encoding(lottery)
        self.M_out = int(E_all.shape[0])
        if self.M_out > MAX_OUTER_ENUM:
            raise ValueError(
                f"{lottery} 外层枚举规模 {self.M_out} 超出上限 {MAX_OUTER_ENUM}（精确枚举不接管）")
        self.E_all = E_all
        # 快路径标记：内层结局 ≤ 16 ⇒ 覆盖可用 uint16 表示，popcount 走 64K 查表
        self.fast16 = bool(self.N_in <= 16 and self.W == 1)
        self.mask16 = np.zeros((self.w_out + 1, self.N_in), dtype=np.uint16)
        if self.fast16:
            for h in range(self.w_out + 1):
                for s in range(self.N_in):
                    self.mask16[h, s] = np.uint16(int(self.mask[h, s, 0]))

    def _inner_slot(self, ticket: Dict[str, Any]) -> int:
        """票面内层组合 → `inners` 里的下标（双色球蓝球槽位 / 大乐透后区组合）。"""
        v = ticket.get("蓝球")
        raw = list(v) if isinstance(v, (list, tuple)) else [v]
        nums = tuple(sorted(int(x) - 1 for x in raw if x is not None))
        slot = self._inner_index.get(nums)
        if slot is None:
            raise ValueError(
                f"{self.lottery} 票面内层 {nums} 不是合法的 {len(self.inners[0])} 选组合"
                f"（可选 {len(self.inners)} 种）")
        return int(slot)

    # ---------------- 单票支撑集 ----------------
    def support(self, ticket: Dict[str, Any]) -> Tuple[np.ndarray, np.ndarray, int]:
        """(ρ 下标, 该 ρ 的 h, 内层槽位 b)。只保留 h ≥ h_min 的 ρ。

        为什么只留支撑集：mask[h] 在 h < h_min 时全零，那些 ρ 永远不贡献增益；
        但它们在 110 万个里占 99.5%，逐个过一遍纯属浪费（实测 54 ms → 0.2 ms）。
        """
        return self.supports([ticket])[0]

    def supports(self, pool: Sequence[Dict[str, Any]]
                 ) -> List[Tuple[np.ndarray, np.ndarray, int]]:
        """**批量**算支撑集（一次矩阵乘），而不是每票乘一次。

        ★ 为什么必须批量：单票一次 1.1M×33 的矩阵乘要 37 ms，500 注就是 18 秒，
          而整次贪心的实际计算只占其中一小半（实测 K=40/池200：8.5 秒里 7.4 秒在算
          支撑集）。合成一次 (M_out × 33) @ (33 × K) 后，同样的事只要 1~2 秒。
          代价是峰值内存：分块做，块大小按「块行数 × K ≤ 1600 万」控制（约 65 MB）。
        """
        from ev.profit.outcomes import _encode_outer, tickets_to_outer
        K = len(pool)
        E_tk = _encode_outer(self.rules, tickets_to_outer(self.lottery, list(pool))).T
        E_tk = np.ascontiguousarray(E_tk.astype(np.float32))
        d = E_tk.shape[0]
        M = int(self.M_out)
        step = max(1, int(16_000_000 // max(1, K)))
        idx_parts: List[List[np.ndarray]] = [[] for _ in range(K)]
        h_parts: List[List[np.ndarray]] = [[] for _ in range(K)]
        E32 = self.E_all.astype(np.float32)
        for s in range(0, M, step):
            e = min(s + step, M)
            H = np.rint(E32[s:e] @ E_tk).astype(np.int8)      # (c, K)
            for j in range(K):
                col = H[:, j]
                sel = np.flatnonzero(col >= self.h_min)
                if sel.size:
                    # int32 下标 + int8 命中数：8000 注时比 int64 省一半内存
                    idx_parts[j].append((sel + s).astype(np.int32))
                    h_parts[j].append(col[sel].astype(np.int8))
        out: List[Tuple[np.ndarray, np.ndarray, int]] = []
        for j, t in enumerate(pool):
            idx = (np.concatenate(idx_parts[j]) if idx_parts[j]
                   else np.empty(0, dtype=np.int32))
            hv = (np.concatenate(h_parts[j]) if h_parts[j]
                  else np.empty(0, dtype=np.int8))
            out.append((idx, hv, self._inner_slot(t)))
        return out


def _total_log(cls_ctx) -> int:
    return int(cls_ctx.M_out) * int(cls_ctx.N_in)


def exact_union_from_ctx(ctx: _Ctx, tickets: Sequence[Dict[str, Any]],
                        cache: Optional[Dict[int, Any]] = None) -> float:
    """在给定 ctx 上精确算 P(至少一注达标)（与 `tier_coverage.exact_union_probability` 同口径）。

    复用同一套支撑集：被覆盖的 (ρ, B) 对数 ÷ 总对数。
    """
    cov = np.zeros((ctx.M_out, ctx.W), dtype=np.uint64)
    for (idx, h, b) in ctx.supports(list(tickets)):
        cov[idx] |= ctx.mask[h, b]
    n = int(_popcount_words(cov).sum())
    return n / _total_log(ctx)


def _overlap_matrix(pool: Sequence[Dict[str, Any]], lottery: str) -> np.ndarray:
    """候选池 → 0/1 矩阵，内积 == 两票外层交集大小。

    ★ 直接用 `tier_coverage._eq_matrix` 而不是自写一套：重叠上限必须与生产
      `ev.selection._overlap` 同口径，两套实现迟早对不上（本项目已有先例）。
    """
    from ev.tier_coverage import _eq_matrix
    return _eq_matrix(pool, lottery)


def select_exact_tier(
    lottery: str,
    pool: Sequence[Dict[str, Any]],
    K: int,
    target: Any = None,
    issue: Any = None,
    *,
    cap: Optional[int] = None,
    seed: int = 0,
    secondary: Any = None,
    budget: Optional[float] = None,
    max_evals: Optional[int] = None,
    relax: bool = False,
    trace: bool = False,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """精确边际增益贪心：每步选「新增达标概率」最大的票。

    参数
    ----
    pool     候选票（必须来自真实候选池，不许现场造票）
    K        目标注数（与 `budget` 谁先到就停在谁那）
    target   目标奖级/金额线（`ev.tier_target` 口径，按期解析规则版本）
    cap      两两重叠上限（默认生产的 3，与 `PROD_OVERLAP_CAP` 一致）
    secondary  次级目标（如 `{"kind":"payout","min_payout":5}`）。**只用于并列时排序**，
               不与主目标加权成一个总分 —— 混分会为堆小奖牺牲高奖目标。
    budget   单期预算（元）。每注成本取自该彩种规则表。
    max_evals 评估次数上限（防呆；到顶即停并如实标「未跑完」）

    返回 (tickets, meta)。meta 里带 `精确达标签概率`，可直接与
    `tier_coverage.select_tier_coverage` 的结果同台比较。
    """
    t0 = time.time()
    from ev.lowcrowd_backtest import PROD_OVERLAP_CAP
    from ev.tier_coverage import RELAX_MAX
    cap = int(PROD_OVERLAP_CAP if cap is None else cap)

    ctx = _Ctx(lottery, target, issue)
    pool = list(pool)[:MAX_POOL]
    if not pool:
        raise ValueError("候选池为空")
    K = int(K)
    if budget is not None and budget > 0:
        K = min(K, int(budget // ctx.cost))

    total_log = _total_log(ctx)
    rng = np.random.default_rng(int(seed))

    # ---- 一次性的支撑集与重叠矩阵（批量算，见 _Ctx.supports 的注释）----
    sups: List[Tuple[np.ndarray, np.ndarray, int]] = ctx.supports(pool)
    vecs = _overlap_matrix(pool, lottery)

    # 次级目标独立维护自己的覆盖位图：**不能**拿主目标的 cov 去算次级增益 ——
    # ≥四等 与 ≥5元 是两个不同的达标集合，混用会算出没有意义的差值。
    sec_ctx = None
    cov2 = None
    if secondary is not None:
        try:
            sec_ctx = _Ctx(lottery, secondary, issue)
            if sec_ctx.W == ctx.W and sec_ctx.M_out == ctx.M_out:
                cov2 = np.zeros((ctx.M_out, ctx.W), dtype=np.uint64)
            else:
                sec_ctx, cov2 = None, None
        except Exception:            # noqa: BLE001 - 次级不可用只降级，绝不影响主目标
            sec_ctx, cov2 = None, None

    cov = np.zeros((ctx.M_out, ctx.W), dtype=np.uint64)
    fast = bool(getattr(ctx, "fast16", False))
    cov16 = np.zeros(ctx.M_out, dtype=np.uint16) if fast else None
    maxov = np.zeros(len(pool), dtype=np.int16)     # 与已选集合的最大两两重叠
    taken = np.zeros(len(pool), dtype=bool)
    picked: List[int] = []
    cur_cap = int(cap)
    relax_count = 0
    relax_steps: List[int] = []
    evals = 0
    hits_per_step: List[int] = []
    gain_per_step: List[float] = []

    def _gain(i: int) -> int:
        """候选 i 相对当前覆盖的**精确**新增 (ρ,B) 格数。"""
        idx, h, b = sups[i]
        if fast:
            # 内层 ≤16：整段覆盖是一个 uint16，popcount 退化成一次 64K 查表
            return int(_LUT16[np.bitwise_and(ctx.mask16[h, b],
                                             np.bitwise_not(cov16[idx]))].sum())
        return int(_popcount_words(ctx.mask[h, b] & ~cov[idx]).sum())

    def _apply(i: int) -> None:
        idx, h, b = sups[i]
        if fast:
            cov16[idx] |= ctx.mask16[h, b]
        else:
            cov[idx] |= ctx.mask[h, b]

    while len(picked) < K:
        ok = (~taken) & (maxov <= cur_cap)
        if not ok.any():
            # ★ 严格模式下**必须停在这里并把「容量不足」报出去**：
            #   上一版直接 break 却什么都不说，于是「要 500 注、实选 431 注」被当成
            #   正常结果拿去和 500 注的随机臂比 —— 比出来是负的，看着像算法失效，
            #   其实是**少买了 69 注**。这类静默缺斤最容易被误读成结论。
            if relax and cur_cap - cap < RELAX_MAX:
                cur_cap += 1
                relax_count += 1
                relax_steps.append(len(picked) + 1)
                continue
            break
        cand = np.flatnonzero(ok)
        # 主目标：精确新增格数（子模覆盖 ⇒ 贪心是 (1-1/e) 近似）
        gains = np.empty(len(cand), dtype=np.int64)
        for j, i in enumerate(cand):
            gains[j] = _gain(int(i))
            evals += 1
            if max_evals is not None and evals >= max_evals:
                break
        best = int(np.argmax(gains))
        # 并列时的次级排序（例如 ≥5 元）：只对**恰好并列**者计算，不做加权合成。
        # ★ 为什么不混成一个总分：主目标是「至少一注达四等」，次级是「顺带堆多少 5 元」；
        #   加权后必然出现「为了多中几个 5 元而牺牲四等覆盖」的交换，而那个交换比率
        #   没法事先说清，更没法在看完开奖后还保持诚实。
        tied = np.flatnonzero(gains == gains[best])
        if sec_ctx is not None and len(tied) > 1:
            try:
                sec_best, sec_gain = None, -1
                for j in tied:
                    i = int(cand[j])
                    idx2, h2, b2 = sec_ctx.support(pool[i])
                    g = int(_popcount_words(sec_ctx.mask[h2, b2] & ~cov2[idx2]).sum())
                    if g > sec_gain:
                        sec_best, sec_gain = i, g
                if sec_best is not None:
                    best = int(np.flatnonzero(cand == sec_best)[0])
            except Exception:        # noqa: BLE001 - 次级失败退回池序，主目标不受影响
                pass
        elif len(tied) > 1:
            # 仍并列 ⇒ 用 seed 定序，保证可复现（不许用全局随机态）
            best = int(tied[int(rng.integers(len(tied)))])

        i = int(cand[best])
        if gains[best] <= 0:
            break
        picked.append(i)
        taken[i] = True
        gain_per_step.append(float(gains[best]) / total_log)
        hits_per_step.append(int(gains[best]))
        idx, h, b = sups[i]
        _apply(i)
        if cov2 is not None:
            idx2, h2, b2 = sec_ctx.support(pool[i])
            cov2[idx2] |= sec_ctx.mask[h2, b2]
        maxov = np.maximum(maxov, vecs @ vecs[i])

    tickets = [pool[i] for i in picked]
    if fast:
        covered = int(_LUT16[cov16].sum()) if tickets else 0
    else:
        covered = int(_popcount_words(cov).sum()) if tickets else 0
    exact_p = covered / total_log if tickets else 0.0
    p1 = exact_union_from_ctx(ctx, [pool[0]]) if pool else 0.0
    kp = p1 * len(tickets)
    # ★ 报告「已选票两两之间的最大重叠」。不能用 maxov.max()：选中的票 j 满足
    #   vecs[j]·vecs[j] = 票面号数（双色球 6），会把自己读成违规，看起来像 cap 失效。
    if len(picked) > 1:
        sel = vecs[picked]
        pair = sel @ sel.T
        np.fill_diagonal(pair, 0)
        ov_max = int(pair.max())
    else:
        ov_max = 0

    meta: Dict[str, Any] = {
        "策略": "select_exact_tier",
        "彩种": lottery,
        "目标": ctx.res["label"],
        "目标级": ctx.res.get("label"),
        "达标奖级": sorted(ctx.res["grades"]),
        "条件集": [list(c) for c in ctx.res["conditions"]],
        "期号": issue,
        "规则版本": ctx.res.get("rule_version"),
        "单注达标概率": p1,
        "理论上限Kp": kp,
        "选中注数": len(tickets),
        "精确达标签概率": exact_p,
        "精确达标签概率_口径": "外层×内层全枚举（位掩码并集），无采样、无独立性近似",
        "重叠上限": ov_max,
        "cap": cap,
        "最终cap": int(cur_cap),
        "放宽次数": int(relax_count),
        "cap放宽步号": list(relax_steps),
        "★cap污染": bool(relax_count > 0),
        "候选池规模": len(pool),
        "评估次数": evals,
        "耗时秒": round(time.time() - t0, 2),
        "次级目标": None if secondary is None else TT.resolve_conditions(
            lottery, secondary, issue).get("label"),
        "预算元": budget,
        "每注成本元": ctx.cost,
        "总成本元": round(ctx.cost * len(tickets), 2),
        "诚实边界": (
            "不改变任何一注自己的中奖概率（E[达标注数] = K·p 恒成立）；"
            "本模块只改变 K 注之间「达标结局」的重叠结构。"
            "可争取空间受 P(并集) ≤ min(1, K·p) 约束、量级为 O((K·p)²)，"
            "报收益必须同时报 K 与 K·p。不预测开奖号码、不保证盈利。"
        ),
    }
    if len(tickets) < K and not (max_evals is not None and evals >= max_evals):
        meta["容量不足"] = (
            f"要求 {K} 注，实选 {len(tickets)} 注：cap={cap} 下候选已被吃光"
            + ("（已阶梯放宽到 %d）" % cur_cap if relax_count else "（relax=False，未放宽）"))
    if max_evals is not None and evals >= max_evals and len(tickets) < K:
        meta["未跑完"] = f"评估次数达到上限 {max_evals}，只选了 {len(tickets)}/{K} 注"
    if trace:
        meta["贪心轨迹"] = [
            {"步": n + 1, "新增格数": h, "新增概率": g}
            for n, (h, g) in enumerate(zip(hits_per_step, gain_per_step))
        ]
    return tickets, meta
