# -*- coding: utf-8 -*-
"""ev/tier_coverage.py —— 奖级达标覆盖选号（加权 max-coverage 贪心）。

要解决的问题
------------
固定每期 K 注，想让 **P(至少一注达到 ≥目标奖级)** 尽量高。
各号码组合等可能 ⇒ 单注概率 p 是常数，K 注的达标概率只取决于
「K 注各自的达标**结局集**有多大重叠」：

    完全不相交（理想上界）  Kp
    完全独立（随机基线）    1 − (1−p)^K
    二者之差 ≈ (Kp)²/2      ⇒ 覆盖能拿到的最大相对增益 ≈ Kp/2

本模块用**加权 max-coverage 贪心**逼近「不相交」：每步选「新增通道权重和」最大的票。
权重 = 该通道对应的结局概率，故贪心优化的是 Σ权重（P(并集) 的**上界**），
真实 P(并集) 由 `exact_union_probability`（外层×内层全枚举）或实测回测给出。

诚实边界（写死在这里，改代码不许绕过）
------------------------------------
1. 本模块**不改变**任何一注的中奖概率，只改变 K 注之间「达标结局」的重叠结构；
2. Σ通道权重是 P(并集) 的**上界**，不是估计值：通道互相重叠，包含-排斥的高阶项
   会把上界系统性抬高。**不许**把它当结论写进报告；
3. 「达标率提高了多少」只能由实测回测回答，且必须与**同预算全单倍随机**对照
   （见 `ev/tier_backtest.py`）。
"""
from __future__ import annotations

import logging
import math
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ev import tier_target as TT
from ev.lowcrowd_backtest import PROD_OVERLAP_CAP

logger = logging.getLogger(__name__)

# 候选池上限（防呆）：单期重选时池子越大越慢，且边际收益很快衰减
MAX_POOL = 8000

# 贪婪选号的「重叠约束放宽阶梯」：当前 cap 挑不出票时逐级 +1，最多到 cap+RELAX_MAX
RELAX_MAX = 4


# ---------------------------------------------------------------- 重叠矩阵
def _eq_matrix(pool: Sequence[Dict[str, Any]], lottery: str) -> np.ndarray:
    """把候选票变成 0/1 矩阵，使两票的内积 == `ev.selection._overlap`。

    · 红蓝型（双色球/大乐透）：列 = 红球号码（1..33 / 1..35），内积 = 红球交集大小
    · 位置型（七星彩/数字型） ：列 = 位置×10+数字，内积 = 同位相同个数
    """
    from ev.profit.rules import rules_for

    r = rules_for(lottery)
    if r.outer_choose:
        hi = r.outer_range[1]
        A = np.zeros((len(pool), hi), dtype=np.int16)
        for i, t in enumerate(pool):
            for x in t.get("红球") or []:
                A[i, int(x) - 1] = 1
        return A

    k = r.outer_width
    A = np.zeros((len(pool), k * 10), dtype=np.int16)
    for i, t in enumerate(pool):
        nums = [int(x) for x in (t.get("号码") or [])][:k]
        for pos, d in enumerate(nums):
            A[i, pos * 10 + d] = 1
    return A


def _worst_grade(grades: Any) -> Optional[str]:
    """达标奖级集里**最差**的那一级（用于把「≥X等」压成一个名字；不连续的集返回 None）。"""
    order = [g for g in (grades or ()) if g in TT._GRADE_ORDER]
    if not order:
        return None
    return max(order, key=TT._GRADE_ORDER.index)


# ---------------------------------------------------------------- 覆盖贪心
def select_tier_coverage(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                         target: Any = None, issue: Any = None,
                         cap: Optional[int] = None, seed: int = 0,
                         with_exact: bool = False,
                         trace: bool = False) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """加权 max-coverage 贪心选号。

    参数
    ----
    pool   候选票（dict，双色/大乐透用 "红球"/"蓝球"）
    K      目标注数
    target 达标线（见 `ev.tier_target.normalize_target`），默认 ≥四等
    issue  期号（决定规则版本；大乐透 2026-02 起九级→七级）
    cap    重叠上限；**默认取生产口径 `PROD_OVERLAP_CAP = 3`**，
           不是 `ev.selection._OVERLAP_CAP["双色球"] = 4`（用 4 会把 cap 差异
           混进「选号差异」里）
    seed   仅用于给同分候选做确定性扰动（不影响覆盖目标本身）
    with_exact  是否额外算一次**精确** P(至少一注达标)（走 exact_metrics，慢）
    trace  是否记录**逐步贪心轨迹**（纯诊断，P5 增强）：每步的池内下标、
           边际覆盖权重、累计覆盖权重、当前最大红球重叠与 cap 放宽事件，
           汇总进 `meta["贪心轨迹"]`。★ 不改变选号结果（trace=True/False
           必须选出**同一批票**），也不参与任何判据。

    返回 (选中的票, meta)。凑不满 K 时 meta 记录 `容量不足` 与放宽次数，由调用方兜底。
    """
    from ev.profit.rules import rules_for

    res = TT.resolve_conditions(lottery, target, issue)
    conds = res["conditions"]
    if not conds:
        raise ValueError(f"{lottery} 在 {res['rule_version']} 下的目标 {res['label']} 没有可取条件")
    if cap is None:
        cap = int(PROD_OVERLAP_CAP)

    pool = list(pool)[:MAX_POOL]
    m = len(pool)
    K = int(K)
    if m == 0:
        raise ValueError("候选池为空")

    rules = rules_for(lottery, issue)
    # ---- 每票通道 + 权重，并给通道编号
    key_id: Dict[int, int] = {}
    owners: List[List[int]] = []
    ticket_cols: List[List[Tuple[int, float]]] = []
    for i, t in enumerate(pool):
        seen: Dict[int, float] = {}
        for k, w in TT.coverage_channels_of(t, lottery, conds, issue):
            if k not in seen:
                seen[k] = w
        cols: List[Tuple[int, float]] = []
        for k, w in seen.items():
            c = key_id.get(k)
            if c is None:
                c = key_id[k] = len(owners)
                owners.append([])
            owners[c].append(i)
            cols.append((c, w))
        ticket_cols.append(cols)

    ncol = len(owners)
    covered = np.zeros(ncol, dtype=bool)
    gain = np.array([sum(w for _, w in cols) for cols in ticket_cols], dtype=np.float64)

    A = _eq_matrix(pool, lottery)
    maxov = np.full(m, -1, dtype=np.int16)
    rng = np.random.default_rng(seed)
    jitter = rng.standard_normal(m) * 1e-15        # 仅用于打破完全同分的确定性偏置

    picked: List[int] = []
    taken = np.zeros(m, dtype=bool)
    cur_cap, relax = cap, 0
    covered_weight = 0.0
    # ---- trace：逐步贪心轨迹（仅 trace=True 填充，纯诊断不改选号）
    steps: List[Dict[str, Any]] = []
    relax_at: List[int] = []              # cap 放宽发生的步号
    sat_at: Optional[int] = None          # 覆盖饱和步号（全部通道被覆盖的那一步）

    while len(picked) < K:
        ok = (~taken) & (maxov <= cur_cap)
        if not ok.any():
            if cur_cap - cap >= RELAX_MAX:
                break
            cur_cap += 1
            relax += 1
            if trace:
                relax_at.append(len(picked) + 1)
            continue
        score = np.where(ok, gain + jitter, -np.inf)
        i = int(np.argmax(score))
        if not np.isfinite(score[i]):
            break
        marginal = float(gain[i])            # 本步边际 = 选中前 gain[i]（随后会被扣减）
        picked.append(i)
        taken[i] = True
        maxov = np.maximum(maxov, A @ A[i])
        new_chs: List[int] = []
        for c, w in ticket_cols[i]:
            if not covered[c]:
                covered[c] = True
                covered_weight += w
                new_chs.append(int(c))
                for j in owners[c]:
                    gain[j] -= w
        if trace:
            steps.append({
                "步号": len(picked),
                "票下标": int(i),
                "本步边际": round(marginal, 6),
                "新增通道数": len(new_chs),
                "新增通道": new_chs,
                "累计覆盖权重": round(float(covered_weight), 6),
                "当前最大重叠": int(maxov.max()),
            })
            if sat_at is None and bool(covered.all()):
                sat_at = len(picked)

    tickets = [pool[i] for i in picked]
    p1 = TT.single_ticket_probability(lottery, conds, issue)
    kp = min(1.0, K * p1)
    # ★ Σ通道权重必然 ≥ P(并集)，但它可能比「不相交上界 Kp」还松（同一票的通道互相重叠）。
    #   取两者较小者才是**可用的**上界；Kp ≥ 1 或 Σw ≫ 1 时上界是空的，必须显式标出来。
    ub = min(kp, covered_weight)
    meta: Dict[str, Any] = {
        "策略": "奖级达标策略",
        "彩种": lottery,
        "目标": res["label"],
        "目标级": _worst_grade(res["grades"]),
        "达标奖级": sorted(res["grades"]),
        "条件集": list(conds),
        "期号": None if issue is None else str(issue),
        "规则版本": res["rule_version"],
        "单注达标概率": p1,
        "理论上限Kp": kp,
        "随机基线": 1.0 - (1.0 - p1) ** K if K else 0.0,
        "选中注数": len(tickets),
        "覆盖通道数": int(covered.sum()),
        "通道总数": ncol,
        "通道权重和": covered_weight,
        "并集概率上界": ub,
        "上界是否有效": bool(kp < 0.5 and ub < 1.0),
        "上界失效原因": ("" if (kp < 0.5 and ub < 1.0) else
                    f"Kp={kp:.3f}、Σ通道权重={covered_weight:.3f}，两者都接近或超过 1 ⇒ "
                    f"解析上界无信息量，只能看实测"),
        "通道权重口径": TT.UPPER_BOUND_NOTE,
        "重叠上限": cur_cap,
        "初始重叠上限": cap,
        "放宽次数": relax,
        "候选池规模": m,
        "说明": ("通道权重和是 P(至少一注达标) 的**上界**（通道互相重叠），"
                 "不是估计值，也不得当作结论；判定以逐期实测为准。"),
    }
    if len(tickets) < K:
        meta["容量不足"] = f"只选出 {len(tickets)}/{K} 注（池 {m}，上限 cap={cur_cap}）"
    if trace:
        # ★ 纯诊断：不参与任何判据、不进预注册契约。语义对齐 design.greedy_union 的
        #   trace，但这里要逐步才看得见「边际怎么衰减、何时零边际、cap 何时放宽」。
        meta["贪心轨迹"] = {
            "口径": ("每步记录选中的池内下标、边际覆盖权重（= 累计覆盖权重的增量）、"
                     "累计覆盖权重、当前与已选集合的最大红球重叠；另记 cap 放宽事件。"
                     "纯诊断，不改变选号结果。"),
            "逐步": steps,
            "汇总": {
                "步数": len(steps),
                "末步累计覆盖权重": round(float(covered_weight), 6),
                "覆盖饱和步号": sat_at,
                "零边际首现步号": next((s["步号"] for s in steps if s["新增通道数"] == 0), None),
                "cap放宽步号": relax_at,
                "候选池规模": m,
                "初始重叠上限": int(cap),
                "最终重叠上限": int(cur_cap),
            },
        }
    from ev.mechanism.narrative import TIER_COVERAGE_CAVEAT

    meta["诚实边界"] = TIER_COVERAGE_CAVEAT
    if with_exact:
        try:
            meta["精确达标签概率"] = exact_portfolio_coverage(lottery, tickets, target, issue)
        except Exception as e:                       # noqa: BLE001
            meta["精确达标签概率"] = None
            meta["精确口径失败"] = str(e)
    return tickets, meta


# ---------------------------------------------------------------- 精确概率
def exact_portfolio_coverage(lottery: str, tickets: Sequence[Dict[str, Any]],
                             target: Any = None, issue: Any = None,
                             kappa: float = 0.0) -> float:
    """整组票「至少一注达标」的**精确**概率（复用 `exact_metrics(min_grade=...)`）。

    ⚠️ 只支持「连续奖级」目标（即达标奖级集恰为「某级及以上」）；
       金额线目标若命中的奖级不连续（例：只中四等不中五等）会报错 —— 这种目标
       本来也无法用单个 min_grade 表达，需要逐级求和，属未实现。
    ⚠️ 该函数的概率是**模型口径**（与 EV 模型同一套独立性近似），
       与实测回测的 realized 比率不是一回事，两者不许混报。
    """
    from ev.profit import evaluate as EV
    from ev.profit import payouts as P

    res = TT.resolve_conditions(lottery, target, issue)
    grades = res["grades"]
    order = [g for g in grades if g in TT._GRADE_ORDER]
    if not order:
        raise ValueError(f"{lottery} 目标 {res['label']} 的达标奖级不含「X等」，无法用 min_grade 表达")
    worst = max(order, key=TT._GRADE_ORDER.index)
    if set(TT.grade_conditions_at_or_above(lottery, worst, issue)) != set(res["conditions"]):
        raise ValueError(f"{lottery} 目标 {res['label']} 的达标奖级不连续，无法用单个 min_grade 表达")

    # ★ 2026-10-24 起改走 `exact_union_probability`（外层×内层全枚举，无独立性近似）。
    #   旧实现走 `EV.exact_metrics(...).P_达标`，它在「命中数桶」内用 `1-(1-p)^H`，
    #   即假设同桶各票的达标事件独立。内层槽位极小（双色球蓝球 16 选 1、
    #   大乐透 12 选 2）⇒ 同桶票的内层命中互斥，独立近似不成立，实测偏差为
    #   −11σ（双色球，低估）到 +703σ（大乐透，高估 52pp），幅度远大于效应量。
    #   该函数已与均匀开奖 MC 回放逐格对账在 ±1.3σ 内。
    if float(kappa) != 0.0:
        raise NotImplementedError(
            "kappa≠0 的外层加权分布尚无无近似枚举实现；"
            "旧实现的桶内独立性近似在 κ≠0 下同样不成立，不提供静默回退。")
    return exact_union_probability(lottery, tickets, target=target, issue=issue)


# ---------------------------------------------------------------- 无近似枚举
# 外层结局枚举规模上限（防呆）：双色球 C(33,6)=1,107,568、大乐透 C(35,5)=324,632
MAX_OUTER_ENUM = 2_000_000
# 内层结局枚举规模上限（防呆）：双色球 C(16,1)=16、大乐透 C(12,2)=66
MAX_INNER_ENUM = 4096


def _popcount64(x: np.ndarray) -> np.ndarray:
    """uint64 数组的 popcount（不依赖 numpy 版本）。"""
    b = np.ascontiguousarray(x).view(np.uint8).reshape(-1, 8)
    return np.unpackbits(b, axis=1).sum(axis=1, dtype=np.int64)


def _inner_pool_size(r, m_in: int) -> int:
    """内层**池大小**（双色球 16、大乐透 12），由 `C(n, m) == r.inner_size` 反解。

    ⚠️ `r.inner_size` 是内层**结局总数**（双色球 C(16,1)=16、大乐透 C(12,2)=66），
       不是池大小。双色球两者恰好相等，大乐透不等（66 vs 12）；
       直接拿 `inner_size` 当池大小会枚举出 C(66,2)=2145 个伪内层结局。
    """
    total = int(r.inner_size)
    for n in range(m_in, 64):
        if math.comb(n, m_in) == total:
            return n
    raise ValueError(f"无法从 inner_size={total}、m={m_in} 反解内层池大小")


def exact_union_probability(lottery: str, tickets: Sequence[Dict[str, Any]],
                            target: Any = None, issue: Any = None,
                            *, chunk: int = 1024) -> float:
    """整组票「至少一注达标」的**精确**概率 —— 无独立性近似。

    做法（外层 × 内层**全枚举** + 位掩码求并）：
      1. 内层结局 B 全枚举（双色球 C(16,1)=16；大乐透 C(12,2)=66）；
      2. 对每张票 i、每个外层命中数 h，预计算「哪些 B 使 (h, |B∩b_i|) 达标」
         的位掩码 `mask[h, i]`；
      3. 外层结局 ρ 全枚举，取 K 张票的掩码按位或 ⇒ 达标的内层结局数 ÷ N_in；
      4. 对外层结局取均值（外层均匀）。

    与旧口径的差别：旧口径在命中数桶内假设同桶票独立；本函数不做任何独立性假设，
    因此**支持非连续奖级目标**（例：只中四等不中五等），不受 `min_grade` 表达限制。

    ⚠️ 仅支持红蓝型彩种（`rules.outer_choose` 为真）且 κ=0（外层均匀）。
       位置型彩种 / κ≠0 / 枚举规模超上限 ⇒ `ValueError`，不静默回退。
    ⚠️ 该值是**模型口径**（外层均匀 ⇒ 等价于公平开奖下的真实频率），
       与实测回测的 realized 比率不是一回事，两者不许混报。
    """
    from ev.profit.rules import rules_for
    from ev.profit.outcomes import outer_encoding, _encode_outer, tickets_to_outer

    tks = list(tickets)
    K = len(tks)
    if K == 0:
        return 0.0

    r = rules_for(lottery, issue)
    if not getattr(r, "outer_choose", False):
        raise ValueError(f"{lottery} 不是红蓝型彩种，无近似的并集枚举未实现")
    r0 = rules_for(lottery)
    if (int(r0.outer_width), int(r0.outer_range[1])) != \
            (int(r.outer_width), int(r.outer_range[1])):
        raise ValueError(
            f"{lottery} 期号 {issue} 的外层结构（{int(r.outer_width)}/"
            f"{int(r.outer_range[1])}）与枚举表（{int(r0.outer_width)}/"
            f"{int(r0.outer_range[1])}）不一致，无法枚举")

    res = TT.resolve_conditions(lottery, target, issue)
    grades = set(res["grades"])
    w_out = int(r.outer_width)
    # 开奖抽几个内层号 = grade_table 的 j 上限（双色球 1，大乐透 2）
    m_in = max(jj for (_h, jj) in r.grade_table)
    n_in = _inner_pool_size(r, m_in)      # 内层池大小（16 / 12），不是 inner_size

    n_inner_enum = math.comb(n_in, m_in)
    if m_in < 1 or n_inner_enum > MAX_INNER_ENUM:
        raise ValueError(f"{lottery} 内层枚举规模 C({n_in},{m_in}) 超出上限 {MAX_INNER_ENUM}")

    inners = np.array(list(combinations(range(n_in), m_in)), dtype=np.int16)
    N_in = int(inners.shape[0])
    W = (N_in + 63) // 64

    gate = np.zeros((w_out + 1, m_in + 1), dtype=bool)
    for (hh, jj), gname in r.grade_table.items():
        if hh <= w_out and jj <= m_in and gname in grades:
            gate[hh, jj] = True
    if not gate.any():
        return 0.0

    # ---- 每张票的内层命中数 j_i(B)  (K, N_in)
    blue_sets = np.zeros((K, n_in), dtype=np.int16)
    for i, t in enumerate(tks):
        v = t.get("蓝球")
        for x in (list(v) if isinstance(v, (list, tuple)) else [v]):
            if x is not None:
                blue_sets[i, int(x) - 1] = 1
    IM = np.zeros((n_in, N_in), dtype=np.int16)       # (内层号, 内层结局)
    IM[inners.ravel(), np.repeat(np.arange(N_in), m_in)] = 1
    j_mat = blue_sets @ IM                            # (K, N_in)

    # ---- mask[h, i] : (w_out+1, K, W) uint64
    mask = np.zeros((w_out + 1, K, W), dtype=np.uint64)
    for h in range(w_out + 1):
        ok = gate[h][j_mat]                           # (K, N_in) bool
        for i in range(K):
            for t in np.flatnonzero(ok[i]):
                mask[h, i, int(t) >> 6] |= np.uint64(1 << (int(t) & 63))

    # ---- 外层全枚举
    E_all = outer_encoding(lottery)                   # (M_out, outer_size) uint8
    M_out = int(E_all.shape[0])
    if M_out > MAX_OUTER_ENUM:
        raise ValueError(f"{lottery} 外层枚举规模 {M_out} 超出上限 {MAX_OUTER_ENUM}")
    E_tk = _encode_outer(r, tickets_to_outer(lottery, tks)).T.copy()   # (d, K)

    idx = np.arange(K, dtype=np.int64)[None, :]
    total = np.zeros(M_out, dtype=np.int64)
    for s in range(0, M_out, chunk):
        e = min(s + chunk, M_out)
        hits = (E_all[s:e].astype(np.float32) @ E_tk.astype(np.float32)).astype(np.int64)
        orred = np.bitwise_or.reduce(mask[hits, idx], axis=1)         # (c, W)
        cnt = np.zeros(orred.shape[0], dtype=np.int64)
        for w in range(W):
            cnt += _popcount64(orred[:, w])
        total[s:e] = cnt

    return float(total.mean()) / N_in
