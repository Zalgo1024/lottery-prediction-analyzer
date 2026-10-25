# -*- coding: utf-8 -*-
"""
ev/lowcrowd_select.py —— 低拥挤目标策略选号器

策略假设：开奖在常规态偏向开出全国购买量最少的组合 ⇒ 选号偏向估计低购买量的组合。

★ 为什么不是「按 draw_score = -λ·z(crowd) 纯排序」
  实测链路：ev/selection.py 按 `一等奖实得 eff1` 降序排序，而
  eff1 = E[min(cap, 池/(1+X))], X~Poisson(μ) 随 **μ 单调递减**，
  μ = 票量×P(一等)×exp(β·f) 随拥挤度单调递增。
  ⇒ 「按低拥挤降序」≡「按一等奖实得降序」≡ 现有净 EV 选号。
  **对任意 λ>0，纯排序选出同一批号，λ 是数学上的空操作**
  （与 docs/机制缝隙_策略层可见性设计.md 第八节的 γ 共线坑同源）。

★ 因此差异化来自排序键之外，两个真参数：
  1) gate_q：拥挤度分位门控 —— 改变**候选集合**（真杠杆）；
  2) λ：双目标加权 key(c) = (1−λ)·ĝ(c) + λ·cold(c) —— 改变**覆盖与冷门的相对权重**。
     λ=0 → 纯覆盖（退化为 select_coverage）；λ=1 → 纯冷门（退化为净 EV）；
     0<λ<1 才是本策略。

★ ĝ(c) 盯的是 **≥10 元通道**（五等：4红 或 3红+1蓝）。
  不是 ≥5 元：双色球六等只要求命中蓝球，K≥50 时 16 个蓝球必被覆盖满，
  该指标在生产注数下恒饱和、对选号无区分力。

诚实边界：换选号**不**提高单注中奖概率、也不改变平均中奖次数；
λ 与 gate 只改变「K 注之间中奖结果的重叠结构」与「奖级通道的覆盖结构」。
"""
from __future__ import annotations

import logging
from itertools import combinations
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 每注覆盖的 ≥10 元通道键数：C(6,4)=15 个红球 4-子集 + C(6,3)×1 蓝=20 个 (3-子集,蓝)
KEYS_PER_TICKET = 35
_B = 34            # 键编码基数（红球 1..33）
_OFF3 = _B ** 4    # 3-子集键的偏移，避免与 4-子集键碰撞

RELAX_LADDER = (0.20, 0.35, 0.50, 1.00)   # 门控凑不满时的放宽阶梯
BEAM_W = 150                              # beam 贪心窗口（实测 0.17s，比 w=400 只少 2% 覆盖）


# 情景参数（λ / gate_q）配置文件路径。◆ λ 是**情景参数，非历史反推的点估计**：
# 三档全部保留可配置，回测按网格全覆盖、报告按情景分行，不挑单一「最优值」。
CONFIG_PATH = "E:/707/config/lowcrowd.json"

DEFAULT_PARAMS: Dict[str, Any] = {
    "情景档位": [
        {"名": "偏覆盖", "λ": 0.25, "gate_q": 0.20},
        {"名": "中性", "λ": 0.50, "gate_q": 0.30},
        {"名": "偏冷门", "λ": 0.75, "gate_q": 0.30},
    ],
    "生产默认档": "中性",
    "候选池": 20000,
    "重叠上限": 3,
    # 启用后由自动流水线（web/utils ③a 与 cli auto ④a）在同一期额外生成一条
    # **研究台账批**：台账域=研究、固定 200 注、不跟滑轨。
    # 它参与战绩榜与命中页，但不参与出号轮播 / 权重学习 / 动态注数安全阀。
    "调度器平行批次": {
        "启用": False, "注数": 200,
        "_说明": "启用后额外生成研究台账批（台账域=研究、固定 200 注）。"
                 "参与战绩榜与命中页，不参与出号轮播/权重/动态注数。默认关闭。",
    },
}


def load_params(scenario: Optional[str] = None) -> Dict[str, Any]:
    """读情景参数。生产默认取「中性」档（λ=0.5 / gate=0.30）。

    ★ 选「中性」的理由是它是「覆盖 ↔ 冷门」的中点，**不是**因为它在回测里分数最高
    （回测里 λ=0.5 覆盖键略高于 λ=0，那是 beam 窗口遍历顺序的副产品，不得当作拟合依据）。
    """
    import json
    import os

    p = dict(DEFAULT_PARAMS)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as f:
                p.update(json.load(f) or {})
    except Exception as e:      # noqa: BLE001
        logger.warning(f"[lowcrowd] 读配置失败，用内置默认: {e}")

    out = dict(p)
    name = scenario or p.get("生产默认档") or "中性"
    for d in (p.get("情景档位") or []):
        if d.get("名") == name:
            out["λ"] = float(d.get("λ", 0.5))
            out["gate_q"] = float(d.get("gate_q", 0.30))
            break
    out.setdefault("λ", 0.5)
    out.setdefault("gate_q", 0.30)
    out["档位名"] = name
    return out


def _blue_of(t: Dict[str, Any]) -> int:
    b = t.get("蓝球")
    if b is None:
        return 0
    if isinstance(b, (list, tuple)):
        return int(b[0]) if b else 0
    return int(b)


def keys_of(t: Dict[str, Any]) -> List[int]:
    """单注覆盖的 ≥10 元通道键（35 个）。

    4-子集键：命中 4 红 ⇒ 五等（10 元）或四等（200 元，若蓝球也中），均 ≥10 元。
    (3-子集, 蓝) 键：命中 3 红 + 蓝 ⇒ 五等（10 元）。
    """
    rs = sorted(int(x) for x in (t.get("红球") or []))
    b = _blue_of(t)
    out: List[int] = []
    for c in combinations(rs, 4):
        out.append(((c[0] * _B + c[1]) * _B + c[2]) * _B + c[3])
    for c in combinations(rs, 3):
        out.append(_OFF3 + (((c[0] * _B + c[1]) * _B + c[2]) * 16 + (b - 1)))
    return out


def _red_mask(t: Dict[str, Any]) -> int:
    m = 0
    for x in (t.get("红球") or []):
        m |= 1 << (int(x) - 1)
    return m


def _red_indicator(t: Dict[str, Any], width: int = 33) -> np.ndarray:
    v = np.zeros(width, dtype=np.int32)
    for x in (t.get("红球") or []):
        v[int(x) - 1] = 1
    return v


def _greedy_capacity(pool: Sequence[Dict[str, Any]], cap: int,
                     stop_at: Optional[int] = None) -> int:
    """在只受重叠约束下，该候选池最多能出多少注。

    ★ 低拥挤组合在 9 维粗特征上是聚集的（共享「跨度大 / 和值高 / 连号少」等形态），
    天然更容易红球重叠 ⇒ 门控后的可行容量可能远小于候选数。
    实测（双色球，池 20000，K=200）：不门控 ≤3 重叠只出 772 注；gate≤0.05 只出 117 注
    ⇒ 41.5% 会落到「随机补齐」，策略归因被稀释。故必须在选号前先探容量。
    """
    picked: List[int] = []
    for t in pool:
        if stop_at is not None and len(picked) >= stop_at:
            return len(picked)
        m = _red_mask(t)
        if all((m & pm).bit_count() <= cap for pm in picked):
            picked.append(m)
    return len(picked)


def _ladder(gate_q: float) -> List[float]:
    """门控尝试序列：请求值 → 阶梯中更大的档 → 1.0（不门控）。"""
    seq = [float(gate_q)]
    for q in RELAX_LADDER:
        if q > gate_q + 1e-12:
            seq.append(float(q))
    if seq[-1] < 1.0 - 1e-12:
        seq.append(1.0)
    return seq


def _beam_pick(r: int, gkeys: List[List[int]], gmask: np.ndarray,
               gcold: np.ndarray, K: int, cap: int, lam: float,
               beam_w: int = BEAM_W) -> Tuple[List[int], set]:
    """beam 贪心：每步只在窗口内挑 key 最大者，O(1) 交换摘除。

    gmask/gcold/gkeys 均以「门控后按冷→热排序」的坐标编号（0..r-1）。
    """
    order = np.arange(r, dtype=np.int64)
    picked: List[int] = []
    pm = np.zeros((0, gmask.shape[1]), dtype=np.int32)
    covered: set = set()
    pos = 0
    while len(picked) < K and pos < r:
        wnd = order[pos:pos + beam_w]
        if len(picked):
            ov = gmask[wnd] @ pm.T                 # (w, P) int32
            ok = ov.max(axis=1) <= cap
        else:
            ok = np.ones(len(wnd), dtype=bool)
        best, bi = -1.0, -1
        for j in range(len(wnd)):
            if not ok[j]:
                continue
            i = int(wnd[j])
            g = 0
            for k in gkeys[i]:
                if k not in covered:
                    g += 1
            val = (1.0 - lam) * (g / float(KEYS_PER_TICKET)) + lam * float(gcold[i])
            if val > best:
                best, bi = val, j
        if bi < 0:
            pos += len(wnd)      # 整窗都过不了重叠约束 → 跳过
            continue
        if bi != 0:
            order[pos], order[pos + bi] = order[pos + bi], order[pos]
        chosen = int(order[pos])
        picked.append(chosen)
        pm = np.vstack([pm, gmask[chosen:chosen + 1]])
        covered.update(gkeys[chosen])
        pos += 1
    return picked, covered


def select_lowcrowd(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                    u: np.ndarray, *, lam: float = 0.5, gate_q: float = 0.30,
                    cap: Optional[int] = None, beam_w: int = BEAM_W,
                    relax_ladder: Sequence[float] = RELAX_LADDER,
                    beta_caliber: str = "冻结β"
                    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """低拥挤目标策略选号。

    参数：
      pool  候选票（dict，含「红球」/「蓝球」）
      K     目标注数
      u     拥挤度代理分位 (m,)，0=最冷；来自 popularity_proxy.crowd_quantiles
      lam   覆盖↔冷门加权，0=纯覆盖，1=纯冷门
      gate_q 拥挤度分位上界（保留 u ≤ gate_q）
      cap   红球重叠上限；None = 取 ev.selection._OVERLAP_CAP（生产须显式传 3）
      beta_caliber 记入元数据的 β 口径说明

    返回 (选中的票, meta)。凑不满 K 时 meta 记录放宽次数与容量，由调用方兜底。
    """
    from ev import popularity_proxy as pp
    from ev.selection import _OVERLAP_CAP

    if cap is None:
        cap = int(_OVERLAP_CAP.get(lottery, 3))
    u = np.asarray(u, dtype=np.float64)
    M = len(pool)
    meta: Dict[str, Any] = {
        "彩种": lottery, "λ": float(lam), "目标注数": int(K),
        "重叠上限": int(cap), "beam窗口": int(beam_w),
        "β口径": beta_caliber,
        "循环性声明": pp.CROWD_PROXY_CAVEAT,
        "分位口径": pp.QUANTILE_CALIBER,
    }

    finite = np.isfinite(u)
    if M == 0 or not finite.any():
        meta["降级"] = "拥挤度代理不可用（未 Go 或不支持该彩种）→ 不出号，由调用方显式降级"
        return [], meta

    ladder = _ladder(float(gate_q))
    chosen: List[int] = []
    covered: set = set()
    q_used, n_relax, m_used = None, 0, 0
    for q in ladder:
        idx = np.where(finite & (u <= q + 1e-12))[0]
        if len(idx) < K:
            # 这一档候选数本身就不够 K ⇒ 已经是一次放宽，必须计数，
            # 否则会出现「请求 0.01 / 生效 0.20 / 放宽 0 次」这种自相矛盾的元数据。
            n_relax += 1
            continue
        idx = idx[np.argsort(u[idx], kind="stable")]      # 冷 → 热
        r = len(idx)
        gcold = 1.0 - np.arange(r, dtype=np.float64) / max(r - 1, 1)
        gkeys = [keys_of(pool[int(i)]) for i in idx]
        gmask = np.vstack([_red_indicator(pool[int(i)]) for i in idx])
        picked, cov = _beam_pick(r, gkeys, gmask, gcold, K, cap, float(lam), beam_w)
        if len(picked) >= K:
            chosen = [int(idx[i]) for i in picked]
            covered, q_used, m_used = cov, float(q), r
            break
        # 记录最好的一次尝试，阶梯走完仍不够时用它
        if len(picked) > len(chosen):
            chosen, covered, q_used, m_used = [int(idx[i]) for i in picked], cov, float(q), r
        n_relax += 1

    tickets = [pool[i] for i in chosen]
    us = u[finite]
    meta.update({
        "门控": {
            "请求分位": float(gate_q),
            "生效分位": q_used,
            "放宽次数": int(n_relax),
            "门控前候选数": int(M),
            "门控后候选数": int(m_used),
            "门控后容量": _greedy_capacity(tickets, cap, stop_at=None) if tickets else 0,
            "全池分位": {"min": round(float(us.min()), 4),
                         "median": round(float(np.median(us)), 4),
                         "max": round(float(us.max()), 4)},
        },
        "覆盖": {
            "选中覆盖键": len(covered),
            "理论上限": KEYS_PER_TICKET * int(K),
            "目标奖级": "≥10 元（五等：4红 或 3红+1蓝）",
        },
        "实出注数": len(tickets),
        "z口径": "门控后候选池内秩归一（1=最冷，0=最热）",
    })
    if len(tickets) < K:
        meta["不足"] = (f"门控至 1.0 仍只出 {len(tickets)}/{K} 注，"
                        f"缺口由调用方按既定补齐口径处理")
    return tickets, meta


def select_lowcrowd_from_pool(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                              *, lam: float = 0.5, gate_q: float = 0.30,
                              cap: Optional[int] = None, beta: Any = None,
                              mu_: Any = None, sd_: Any = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """一步到位：先算拥挤度代理分位，再选号。生产入口用这个（走冻结 β）。

    beta/mu_/sd_ 显式传入时用于 walk-forward（回测）；不传则取生产冻结 β。
    """
    from ev import popularity_proxy as pp
    u = pp.crowd_quantiles(lottery, pool, beta=beta, mu_=mu_, sd_=sd_, persist=(beta is None))
    caliber = "冻结β（与 ev/selection 同口径）" if beta is None else "walk-forward 块重拟合β"
    return select_lowcrowd(lottery, pool, K, u, lam=lam, gate_q=gate_q,
                           cap=cap, beta_caliber=caliber)


# ==================================================================== ★ P5：联合目标
# select_joint 想修的正是 Stage 1 抓到的那个病：select_lowcrowd 的覆盖项用的是
# **通道计数代理**（每注 35 个 ≥10元 通道键里有几个是新覆盖的），而 Stage 1 实测
# 「优化上界 ≠ 优化 P(并集)」——tier_coverage 优化的就是上界， union_greedy 直接在
# 采样结局上优化 P(并集)，两者差 3pp 量级。
#
# 所以联合目标 = 把低拥挤的 **冷门权重** 与 **采样结局上的真实并集边际** 放在同一个
# 打分里：
#     val(i) = (1−λ)·[g_i 的并集边际] + λ·cold(i)
#
# ★ 两条不能省的设计：
#   1) `cov_source="proxy"` 必须与 select_lowcrowd **逐位一致**（这是它被允许
#      进生产之前必须过的保真性门槛 —— 新算法先把老路径原样复现一遍，
#      才谈得上"新"的东西带来差别）；
#   2) λ 必须是真杠杆（λ=0 与 λ=0.5 选出的票集要真的不同），
#      否则又是一次「纯排序 ⇒ λ 空操作」。
# ====================================================================

_POP8 = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)

# 联合通道：'proxy'=旧代理（必须与 select_lowcrowd 一致）；'union'=采样结局上的真实边际
COV_SOURCES = ("proxy", "union")


def joint_union_masks(lottery: str, tickets: Sequence[Dict[str, Any]],
                      target: Any, issue: Any, *, seed: int = 0,
                      m_fit: int = 100_000) -> Tuple[np.ndarray, Dict[str, Any]]:
    """给一批票建「票 × 采样结局」位图（P5 联合目标的输入）。

    与 `ev/arena/design.py::select_union_greedy` 用的是**同一套**采样与压位口径，
    ★ 但注意这只是 MC 估计：m_fit 越大越准，代价是内存 `n_tickets × m_fit / 8` 字节。
    """
    from ev.arena.design import outcome_gate, outcome_masks, sample_draws
    from ev.profit.rules import rules_for

    tickets = list(tickets)
    if not tickets:
        raise ValueError("候选票为空")
    gate, w_out, m_in, n_in = outcome_gate(lottery, target, issue)
    r = rules_for(lottery, issue)
    n_out = int(r.outer_range[1])
    k_out = int(r.outer_width)
    if not getattr(r, "outer_choose", False):
        raise ValueError(f"{lottery} 不是红蓝型彩种，联合并集通道未实现")

    rng = np.random.default_rng(int(seed))
    Rf, Bf = sample_draws(rng, n_out, k_out, n_in, m_in, int(m_fit))
    masks = outcome_masks(tickets, lottery, Rf, Bf, gate)
    meta = {"采样结局数": int(masks.shape[1] * 8), "拟合seed": int(seed),
            "MC规模": int(m_fit), "目标": str(target)}
    return masks, meta


def _joint_beam_pick(r: int, gkeys: List[List[int]], gmask: np.ndarray,
                     gcold: np.ndarray, K: int, cap: int, lam: float, *,
                     cov_source: str = "proxy", masks: Optional[np.ndarray] = None,
                     beam_w: int = BEAM_W) -> Tuple[List[int], set]:
    """beam 贪心（联合通道版）。

    `cov_source="proxy"` 时**逐位复刻** `_beam_pick`（同样打分、同样的窗内交换顺序），
    这是 select_joint 被允许当新方法用的前提：先把老路径原样复现一遍。
    """
    if cov_source not in COV_SOURCES:
        raise ValueError(f"未知 cov_source {cov_source!r}，可选 {COV_SOURCES}")
    if cov_source == "union" and masks is None:
        raise ValueError("cov_source='union' 必须给 `masks`")
    if cov_source == "union" and masks.shape[0] != r:
        raise ValueError(f"masks 行数 {masks.shape[0]} != 候选数 {r}")

    total_out = int(masks.shape[1] * 8) if masks is not None else 1
    cov_bits = np.zeros(masks.shape[1], dtype=np.uint8) if masks is not None else None

    order = np.arange(r, dtype=np.int64)
    picked: List[int] = []
    pm = np.zeros((0, gmask.shape[1]), dtype=np.int32)
    covered: set = set()
    pos = 0
    while len(picked) < K and pos < r:
        wnd = order[pos:pos + beam_w]
        if len(picked):
            ov = gmask[wnd] @ pm.T                 # (w, P) int32
            ok = ov.max(axis=1) <= cap
        else:
            ok = np.ones(len(wnd), dtype=bool)
        # ★★ 两条通道的量纲差一个数量级以上：
        #     cold ∈ [0,1]（窗内的冷门秩归一），而并集边际是「新增覆盖结局占比」，
        #     量级只有 p_single ≈ 0.067 且随步数衰减。直接线性加权时
        #     λ=0.5 与 λ=1.0 会选出同一批票（实测 Jaccard=1.0）⇒ λ 在 [0.3,1] 上退化。
        #     故在**当前窗口的可行候选内**各自 z 标准化后再加权：
        #       · 标准化在窗内是单调变换 ⇒ λ=0 仍是「窗内并集边际最大」，
        #         λ=1 仍是「窗内最冷」，端点语义不变；
        #       · 0<λ<1 才是真正的联合。
        vals: List[float] = []
        cols: List[float] = []
        feas: List[int] = []
        for j in range(len(wnd)):
            if not ok[j]:
                continue
            i = int(wnd[j])
            if cov_source == "proxy":
                g = 0
                for k in gkeys[i]:
                    if k not in covered:
                        g += 1
                g = g / float(KEYS_PER_TICKET)
            else:
                g = float(_POP8[masks[i] & ~cov_bits].sum(dtype=np.int64)) / float(total_out)
            vals.append(g)
            cols.append(float(gcold[i]))
            feas.append(j)

        if not feas:
            pos += len(wnd)
            continue

        if cov_source == "union":
            c = np.asarray(vals, dtype=np.float64)
            d = np.asarray(cols, dtype=np.float64)
            sc, sd = float(c.std(ddof=0)), float(d.std(ddof=0))
            zu = (c - c.mean()) / sc if sc > 1e-12 else np.zeros_like(c)
            zd = (d - d.mean()) / sd if sd > 1e-12 else np.zeros_like(d)
            comb = (1.0 - float(lam)) * zu + float(lam) * zd
        else:
            # proxy 分支必须与 `_beam_pick` 逐位一致 ⇒ **不许**标准化
            comb = np.array([(1.0 - lam) * v + lam * c for v, c in zip(vals, cols)],
                            dtype=np.float64)

        bj = int(np.argmax(comb))
        best, bi = float(comb[bj]), feas[bj]
        if bi != 0:
            order[pos], order[pos + bi] = order[pos + bi], order[pos]
        chosen = int(order[pos])
        picked.append(chosen)
        pm = np.vstack([pm, gmask[chosen:chosen + 1]])
        covered.update(gkeys[chosen])
        if cov_bits is not None:
            cov_bits = cov_bits | masks[chosen]
        pos += 1
    return picked, covered


def select_joint(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                 u: np.ndarray, *, lam: float = 0.5, gate_q: float = 0.30,
                 cap: Optional[int] = None, beam_w: int = BEAM_W,
                 cov_source: str = "union", target: Any = None,
                 issue: Any = None, seed: int = 0, m_fit: int = 100_000,
                 union_pool_cap: int = 4000,
                 beta_caliber: str = "冻结β"
                 ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """联合目标选号：覆盖通道 + 冷门通道，同一个打分里加权。

    参数
    ----
    cov_source   'proxy' = 旧通道计数代理（≙ select_lowcrowd 的覆盖项）；
                 'union' = 采样结局上的真实 P(并集) 边际（本轮新增）
    union_pool_cap  'union' 模式下最多给多少张票建位图（按最冷排序截断），
                    控制内存 `n × m_fit / 8`；截断原因会写进 meta。

    ★ `u` 必须与 `pool` **逐元素对齐**，长度不等一律抛 —— 长度不等时 numpy 会
      先 broadcast 或后面静默错位，选出来的票会对应错误的拥挤度。
    """
    from ev import popularity_proxy as pp
    from ev.selection import _OVERLAP_CAP

    if cap is None:
        cap = int(_OVERLAP_CAP.get(lottery, 3))
    if cov_source not in COV_SOURCES:
        raise ValueError(f"未知 cov_source {cov_source!r}，可选 {COV_SOURCES}")

    pool = list(pool)
    u = np.asarray(u, dtype=np.float64)
    if u.shape[0] != len(pool):
        raise ValueError(f"u 长度 {u.shape[0]} != 候选池 {len(pool)}（必须逐元素对齐）")

    M = len(pool)
    meta: Dict[str, Any] = {
        "选择器": "select_joint（覆盖 + 冷门 联合目标）",
        "覆盖通道": cov_source, "彩种": lottery, "λ": float(lam),
        "目标注数": int(K), "重叠上限": int(cap), "beam窗口": int(beam_w),
        "β口径": beta_caliber,
        "循环性声明": pp.CROWD_PROXY_CAVEAT,
        "分位口径": pp.QUANTILE_CALIBER,
    }

    finite = np.isfinite(u)
    if M == 0 or not finite.any():
        meta["降级"] = "拥挤度代理不可用（未 Go 或不支持该彩种）→ 不出号，由调用方显式降级"
        return [], meta

    ladder = _ladder(float(gate_q))
    chosen: List[int] = []
    covered: set = set()
    q_used, n_relax, m_used = None, 0, 0
    for q in ladder:
        idx = np.where(finite & (u <= q + 1e-12))[0]
        if len(idx) < K:
            n_relax += 1
            continue
        idx = idx[np.argsort(u[idx], kind="stable")]      # 冷 → 热
        r = len(idx)
        gcold = 1.0 - np.arange(r, dtype=np.float64) / max(r - 1, 1)
        gkeys = [keys_of(pool[int(i)]) for i in idx]
        gmask = np.vstack([_red_indicator(pool[int(i)]) for i in idx])

        masks = None
        trunc = None
        if cov_source == "union":
            cand = np.arange(r)
            if r > int(union_pool_cap):
                cand = cand[:int(union_pool_cap)]
                trunc = int(union_pool_cap)
            sub = [pool[int(idx[i])] for i in cand]
            full, mm = joint_union_masks(lottery, sub, target, issue,
                                         seed=seed, m_fit=m_fit)
            # 位图按 cand 局部编号；窗口外的票给一张全 0 位图（永远选不上）
            masks = np.zeros((r, full.shape[1]), dtype=np.uint8)
            masks[cand] = full
            meta.update({"联合位图": mm})
            if trunc is not None:
                meta["联合位图截断"] = f"候选 {r} → 取最冷 {trunc} 张建位图"
        picked, cov = _joint_beam_pick(r, gkeys, gmask, gcold, K, cap, float(lam),
                                       cov_source=cov_source, masks=masks, beam_w=beam_w)
        if len(picked) >= K:
            chosen = [int(idx[i]) for i in picked]
            covered, q_used, m_used = cov, float(q), r
            break
        if len(picked) > len(chosen):
            chosen, covered, q_used, m_used = [int(idx[i]) for i in picked], cov, float(q), r
        n_relax += 1

    tickets = [pool[i] for i in chosen]
    us = u[finite]
    meta.update({
        "门控": {
            "请求分位": float(gate_q), "生效分位": q_used,
            "放宽次数": int(n_relax),
            "门控前候选数": int(M), "门控后候选数": int(m_used),
            "门控后容量": _greedy_capacity(tickets, cap, stop_at=None) if tickets else 0,
            "全池分位": {"min": round(float(us.min()), 4),
                         "median": round(float(np.median(us)), 4),
                         "max": round(float(us.max()), 4)},
        },
        "覆盖": {
            "选中覆盖键": len(covered),
            "理论上限": KEYS_PER_TICKET * int(K),
            "目标奖级": "≥10 元（五等：4红 或 3红+1蓝）" if cov_source == "proxy"
                        else f"P(并集) MC 边际（{target}）",
        },
        "实出注数": len(tickets),
        "z口径": "门控后候选池内秩归一（1=最冷，0=最热）",
    })
    if len(tickets) < K:
        meta["不足"] = (f"门控至 1.0 仍只出 {len(tickets)}/{K} 注，"
                        f"缺口由调用方按既定补齐口径处理")
    return tickets, meta
