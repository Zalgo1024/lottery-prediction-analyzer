# -*- coding: utf-8 -*-
"""ev/edge_ceiling.py —— 可获利偏差上界与检出力（把「未检出」变成可结算的边界）

问题
    项目既有的偏离检验只回答「有没有看出来」。这不是可结算的结论：没看出来
    不等于不存在，而任何结论都必须能被后续数据推翻。本模块补上那个反问题。

三段
    ① **需求侧（解析）**：名义返奖率 r0 ⇒ 要让单注 EV 转正，所选组合的概率必须
       相对均匀值高出 G_req = 1/r0 倍。若各因子同幅承担，则每个因子需要
       δ_req = G_req^(1/因子数) − 1。
         · 数字型（各位独立、每位一个符号）：P(组合) = Π p_i ⇒ **精确**。
         · 乐透型（区内不放回抽 c 个）：P(S)/P_均匀(S) ≈ Π_{i∈S}(w_i/p0)，为
           **一阶近似**（误差 O(δ²)）。浮动奖封顶还会削掉上端收益 ⇒ 乐透侧的
           G 是**乐观值**，对本模块「能否排除」的判断属保守方向。
    ② **检出力（闭式）**：池内单取值边际 p0 = 抽数/K，计数相对标准差
       sd_rel = sqrt((1-p0)/(N·p0))。池内 K 个取值同时检验的临界值用 Šidák
       （max 型），得到「power 下的最小可检出相对偏差 δ_min」「δ_req 处的检出
       力」「要达到目标检出力需要多少期」。多池按 Bonferroni 分摊 α。
    ③ **联合检出力**：要让增益真的兑现，得**每个因子都有偏差**都成立 ⇒
       joint_power = Π_池 power^(抽数)。判决就用它：≥ power 判「可判」，否则
       「不可判」并给出需期数。与项目既有的「可判 / 不可判 + n*」同一套纪律。

附带诊断（**不作判决依据**）
    `Pool.ceiling_gain()` 在 χ² 置信区域上最大化组合增益，得到「数据容许的最坏
    情况」。它只对单池有意义；多因子相乘后会天然爆到几百 %，所以只作为
    「为何不可判」的量化旁证，不参与判决。
    `Pool.box_gain()` 是「逐取值同时置信上界再相乘」的**错误口径**，保留它只为
    量化其高估幅度（它忽略了 Σp_i = 抽数 这条约束）。

措辞纪律
    「排除」一律指「在给定 α 与检出力口径下」。「不可判」= 检出力不足，
    既不表示存在 edge，也不构成任何选号建议；本模块不产号、不写 pending。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

from credibility.randomness_audit import _QXC_CUTOFF
from credibility.robustness import _modern_cutoff
from data.loader import load_lottery
from data.schema import get_schema, record_zone_numbers

__all__ = [
    "LOTS", "RETURN_RATE", "RETURN_RATE_SOURCE", "Pool",
    "sidak_threshold", "rel_sd", "detectable_shift", "power_at", "n_required",
    "zone_pools", "joint_power", "joint_n_required",
    "analyze_lottery", "run_all", "render_markdown",
]

# 有开奖数据、且参与本模块结算的彩种（七乐彩/快乐8 当前无本地数据文件）
LOTS: Tuple[str, ...] = ("双色球", "大乐透", "福彩3D", "排列3", "排列5", "七星彩")

# 名义返奖率（**单注固定奖口径**，即「买一注、按该玩法派奖」的返还比例）
#   福彩3D / 排列3 直选：1040 元 / (1000 × 2 元) = 52%
#   排列5：100000 元 / (100000 × 2 元) = 50%
#   七星彩：官方返奖比例 50%
#   双色球 / 大乐透：官方返奖比例 50% / 51%
# 与 ev/mechanism/gap_a_ticket_ratio.py 的「设计返奖率量级校验」（0.50~0.52）同阶；
# 那里是**全玩法混合**的实得派奖率，本模块要的是**单注通道**，故直选取精确值。
RETURN_RATE: Dict[str, float] = {
    "双色球": 0.50,
    "大乐透": 0.51,
    "福彩3D": 0.52,
    "排列3": 0.52,
    "排列5": 0.50,
    "七星彩": 0.50,
}
RETURN_RATE_SOURCE = ("直选固定奖 / 官方返奖比例；量级校验见 "
                      "ev/mechanism/gap_a_ticket_ratio.py::派奖率")

# 哪些彩种必须按「现代段」裁剪：判据是「变点文件里记了检出点，且该差异**不是**
# 单个分区的规则变更」。大乐透早期两段红球边际不均匀（P2-2 变点 13096），属此列；
# 七星彩的变点 20111 恰好就是第 7 位 0-9→0-14 的规则变更，已由分区级
# `_QXC_CUTOFF` 处理，若再全局裁剪反而把前 6 位白丢 2400 期。
MODERN_ONLY_DEFAULT: Dict[str, bool] = {"大乐透": True}


# ================================================================ 统计工具

def sidak_threshold(K: int, alpha: float = 0.05, sides: int = 2) -> float:
    """池内 K 个取值「同时」检验的临界 z（Šidák，max 型）。

    单侧：P(max Z_i ≤ t) = Φ(t)^K = 1-α ⇒ t = Φ⁻¹((1-α)^(1/K))
    双侧：P(max |Z_i| ≤ t) = (2Φ(t)-1)^K = 1-α
    对「取最大值」这个用途它比 Bonferroni 紧，且是正确的那一个。
    """
    if K < 1:
        raise ValueError("K 必须 ≥ 1")
    if not (0.0 < alpha < 1.0):
        raise ValueError("alpha 必须在 (0,1)")
    if sides == 1:
        return float(stats.norm.ppf((1.0 - alpha) ** (1.0 / K)))
    if sides == 2:
        return float(stats.norm.ppf((1.0 + (1.0 - alpha) ** (1.0 / K)) / 2.0))
    raise ValueError("sides 只能是 1 或 2")


def rel_sd(N: float, p0: float) -> float:
    """单取值计数的**相对**标准差：Var(count)=N·p0(1-p0) ⇒ sqrt((1-p0)/(N p0))。"""
    if N <= 0 or p0 <= 0 or p0 >= 1:
        raise ValueError(f"参数越界：N={N}, p0={p0}")
    return math.sqrt((1.0 - p0) / (N * p0))


def shift_alternative(K: int, choose: int, delta: float) -> Tuple[float, float, float]:
    """把「每因子相对偏差 δ」翻译成一个具体的备择分布。

    构造：top-choose 个取值取 a = p0(1+δ)，其余 K-choose 个等分剩余质量
    b = (choose − choose·a)/(K−choose)。返回 (a, b, 每期非中心参数)。

    非中心参数 = N · Σ (p_i − p0)²/p0 —— 这正是 χ² 拟合优度检验在该备择下的
    λ。对 choose=c 的乐透池，c 个值同时偏移，λ 远大于「只有一个值偏移」的情形。
    """
    p0 = choose / K
    a = p0 * (1.0 + delta)
    if choose >= K:
        b = 0.0
    else:
        b = (choose - choose * a) / (K - choose)
    lam1 = choose * (a - p0) ** 2 / p0
    if K > choose:
        lam1 += (K - choose) * (b - p0) ** 2 / p0
    return a, b, lam1


def max_shift(K: int, choose: int) -> float:
    """δ 的物理上限（再大就有取值概率为负）。"""
    return max(K / choose - 1.0, 1e-9)


def pool_power(N: float, K: int, choose: int, delta: float,
               alpha: float = 0.05) -> float:
    """该池的 χ² 均匀性检验在「δ 备择」下的检出力（非中心 χ² 精确式）。"""
    if delta <= 0:
        return float(alpha)
    a, b, lam1 = shift_alternative(K, choose, delta)
    if b < 0:
        return 1.0
    crit = float(stats.chi2.ppf(1.0 - alpha, K - 1))
    return float(stats.ncx2.sf(crit, K - 1, N * lam1))


def detectable_shift(N: float, K: int, choose: int,
                     alpha: float = 0.05, power: float = 0.8) -> float:
    """检出力为 power 时，该池能检出的**最小相对偏差** δ（二分反解）。"""
    hi = max_shift(K, choose)
    if pool_power(N, K, choose, hi, alpha) < power:
        return float("inf")
    lo = 1e-9
    for _ in range(120):
        mid = 0.5 * (lo + hi)
        if pool_power(N, K, choose, mid, alpha) >= power:
            hi = mid
        else:
            lo = mid
    return hi


def power_at(N: float, K: int, choose: int, delta: float,
             alpha: float = 0.05) -> float:
    """别名，便于外部按旧名调用。"""
    return pool_power(N, K, choose, delta, alpha)


def n_required(p0: float, K: int, choose: int, delta: float,
               alpha: float = 0.05, power: float = 0.8) -> float:
    """单池把 δ 检到 power 所需的期数（等比放大后二分反解）。"""
    if delta <= 0:
        return float("inf")
    lo, hi = 1.0, 1e9
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if pool_power(mid, K, choose, delta, alpha) >= power:
            hi = mid
        else:
            lo = mid
    return hi


# ================================================================ 池结构

@dataclass
class Pool:
    """一个「取值池」：池内 K 个取值，每期抽 choose 个（数字型 choose=1）。"""
    彩种: str
    分区: str
    K: int
    choose: int
    N: int
    counts: np.ndarray
    meta: Dict[str, Any] = field(default_factory=dict)

    @property
    def p0(self) -> float:
        return self.choose / self.K

    @property
    def sd_rel(self) -> float:
        return rel_sd(self.N, self.p0)

    # ------------------------------------------------------------ 诊断类
    def point_gain(self) -> float:
        """实测最偏组合的相对增益（点估计，含噪声，不是上界）。"""
        p = self.counts / float(self.N)
        if self.choose == 1:
            return float(p.max() / self.p0)
        top = np.sort(p)[::-1][: self.choose]
        return float(np.prod(top / self.p0))

    def box_gain(self, alpha: float = 0.05) -> Tuple[float, np.ndarray, float]:
        """**箱形**上界（错误口径，仅用于量化高估幅度）。

        ⚠ 它允许 choose 个取值同时顶到各自上界、而其余 K−choose 个不下降，
        完全忽略 `Σp_i = choose`：把 choose 个值顶上去必须把其余压下来，那是有
        卡方代价的。实测该口径高估约 20~40%。
        """
        p = self.counts / float(self.N)
        se = math.sqrt(self.p0 * (1.0 - self.p0) / float(self.N))
        t = sidak_threshold(self.K, alpha, 1)
        ucb = p + t * se
        if self.choose == 1:
            return float(ucb.max() / self.p0), ucb, t
        top = np.sort(ucb)[::-1][: self.choose]
        return float(np.prod(top / self.p0)), ucb, t

    def ceiling_gain(self, alpha: float = 0.05) -> Dict[str, Any]:
        """置信区域内「组合增益」的上确界（施加 Σp_i = choose 的约束）。

            区域 R = {p : Σ_i (n_i − N p_i)²/(N p_i) ≤ χ²_{K−1, 1−α}}
            目标   G(p) = Π_{i ∈ top-choose} (p_i / p0)
        由对称性，最优解形如「top-choose 个取 a、其余取 b，
        choose·a + (K−choose)·b = choose」，二维问题降成对 a 的一维二分。
        """
        if self.choose > self.K:
            raise ValueError("choose 不能大于 K")
        order = np.argsort(self.counts)[::-1]
        top, rest = order[: self.choose], order[self.choose:]
        n_a = float(self.counts[top].mean())
        n_b = float(self.counts[rest].mean()) if rest.size else 0.0
        K, c, N, p0 = self.K, self.choose, float(self.N), self.p0
        chi_crit = float(stats.chi2.ppf(1.0 - alpha, K - 1))

        def stats_at(a: float) -> Tuple[float, float]:
            b = (c - c * a) / (K - c) if K > c else 0.0
            if a <= 0 or b < 0:
                return float("inf"), b
            chi = c * (n_a - N * a) ** 2 / (N * a)
            if K > c:
                chi += (K - c) * (n_b - N * b) ** 2 / (N * b)
            return chi, b

        # f(a) 的最小值未必在 p0（池自身若已偏离，f(p0) 就很大，先降后升）
        # ⇒ 必须「先找最小、再找 + 侧过零点」
        grid = np.linspace(p0, 1.0 - 1e-9, 4000)
        vals = np.array([stats_at(float(a))[0] for a in grid])
        vals = np.where(np.isfinite(vals), vals, np.inf)
        i_min = int(np.argmin(vals))
        ahead = np.where(vals[i_min:] >= chi_crit)[0]
        if ahead.size == 0:
            a_cei = 1.0 - 1e-9
        else:
            i = i_min + int(ahead[0])
            lo = float(grid[i - 1]) if i > 0 else float(grid[i_min])
            hi = float(grid[i])
            try:
                from scipy.optimize import brentq
                a_cei = float(brentq(lambda x: stats_at(x)[0] - chi_crit,
                                     lo, hi, xtol=1e-12))
            except Exception:                        # noqa: BLE001
                a_cei = hi
        _chi, b_cei = stats_at(a_cei)
        gain_pt = float((n_a / N) / p0) ** c
        chi_obs = float(np.sum((self.counts - N * p0) ** 2 / (N * p0)))
        return {
            "gain": max(float((a_cei / p0) ** c), gain_pt),
            "gain_ceiling": float((a_cei / p0) ** c),
            "gain_point": gain_pt,
            "a": a_cei, "b": b_cei,
            "chi2_crit": chi_crit,
            "chi2_at_ceiling": float(_chi),
            "chi2_obs": chi_obs,
            "chi2_p": float(stats.chi2.sf(chi_obs, K - 1)),
            "top_values": [int(v) + int(self.meta.get("区间", [0, 0])[0])
                           for v in top],
            "top_counts": [int(self.counts[v]) for v in top],
            "N": self.N,
        }


def zone_pools(lottery: str, records: Optional[Sequence[Any]] = None,
               modern_only: bool = False) -> List[Pool]:
    """按 schema 把某彩种拆成取值池并计数。

    两处口径裁剪，都是为了避免「拿旧段基准比新段」的伪影：
    · 七星彩第 7 位只取 2020-10-11 起的现行段（0-9 → 0-14 规则变更）；
    · `modern_only=True` 时按 P2-2 变点文件的现代段起点裁剪（大乐透有早期段）。
    """
    if records is None:
        records = load_lottery(lottery).records
    recs0 = list(records)
    if modern_only:
        cut = _modern_cutoff(lottery)
        if cut:
            recs0 = [r for r in recs0
                     if (getattr(r, "期号", None) or 0) > cut]
    schema = get_schema(lottery)
    out: List[Pool] = []
    for zone in schema.zones:
        lo = int(zone.min)
        K = int(zone.max) - lo + 1
        choose = int(zone.choose)
        recs = recs0
        if K == 15:                        # 七星彩第7位现行段
            recs = [r for r in recs
                    if getattr(r, "开奖日期", None) and r.开奖日期 >= _QXC_CUTOFF]
        cnt = np.zeros(K, dtype=np.int64)
        N = 0
        for rec in recs:
            vals = [v for v in record_zone_numbers(rec, zone) if lo <= v < lo + K]
            if not vals:
                continue
            N += 1
            np.add.at(cnt, np.asarray([v - lo for v in vals], dtype=np.int64), 1)
        if N == 0:
            continue
        out.append(Pool(lottery, zone.name, K, choose, N, cnt,
                        {"区间": [lo, lo + K - 1],
                         "现代段": (f"期号>{_modern_cutoff(lottery)}"
                                    if modern_only and _modern_cutoff(lottery)
                                    else "全史"),
                         "现行段起": (str(_QXC_CUTOFF) if K == 15 else None)}))
    return out


# ================================================================ 联合口径

def joint_power(pools: Sequence[Pool], delta: float,
                alpha: float = 0.05) -> float:
    """跨池**并集**检出力：任一池显现即可发现问题 ⇒ 1 − Π(1 − power_pool)。

    ★ 用并集而不是交集：交集（Π power）要求每个池都独立显著，对 7 因子彩种会
    被压到 1e-5 量级，那是「同时证明每一池都偏」的苛刻事件，不是本项目要问的
    「能不能看出来」。
    """
    if not pools:
        return 0.0
    a_pool = alpha / len(pools)                    # 多池 Bonferroni 分摊
    miss = 1.0
    for p in pools:
        miss *= (1.0 - pool_power(p.N, p.K, p.choose, delta, alpha=a_pool))
    return float(1.0 - miss)


def worst_distribution_power(pools: Sequence[Pool], g_req: float,
                             alpha: float = 0.05, n_draw: int = 3000,
                             seed: int = 0) -> Dict[str, Any]:
    """在「必需增益 G_req 怎么分摊到各池」上取**最难检测**的那一种。

    ★ 为什么不能固定「各因子同幅」：那只是分摊方案之一。偏差集中在某个池时
    该池的 δ 很大、很容易看出来；摊得越薄越难看出来。所以判决必须用**最坏分摊**
    下的检出力，否则就是在挑一个对自己有利的假设。

    参数化：令池 i 承担的对数增益比重 w_i（Σw_i = 1, w_i ≥ 0），则池 i 内每个
    因子的相对偏差 δ_i = G_req^(w_i/choose_i) − 1，且 Π(1+δ_i)^choose_i = G_req。
    在单纯形上取「等分 + 全部顶点 + Dirichlet 随机采样」的最小值。

    返回 {'检出力': 最小并集检出力, '分摊': w, '各池δ': [...]}。
    """
    n = len(pools)
    if n == 0:
        return {"检出力": 0.0, "分摊": [], "各池δ": []}
    log_g = math.log(g_req)
    a_pool = alpha / n

    def union_power(w: np.ndarray) -> float:
        miss = 1.0
        for i, p in enumerate(pools):
            delta = (math.exp(log_g * float(w[i]) / p.choose) - 1.0
                     if w[i] > 0 else 0.0)
            miss *= (1.0 - pool_power(p.N, p.K, p.choose, delta, alpha=a_pool))
        return float(1.0 - miss)

    cands: List[np.ndarray] = [np.full(n, 1.0 / n)]
    cands += [np.eye(n)[i] for i in range(n)]
    rng = np.random.default_rng(seed)
    cands += list(rng.dirichlet(np.ones(n), size=n_draw))

    best_w, best_p = None, 1.0
    for w in cands:
        pw = union_power(w)
        if pw < best_p:
            best_p, best_w = pw, w
    assert best_w is not None
    deltas = [(math.exp(log_g * float(best_w[i]) / p.choose) - 1.0
               if best_w[i] > 0 else 0.0) for i, p in enumerate(pools)]
    return {"检出力": best_p,
            "分摊": [float(x) for x in best_w],
            "各池δ": [float(x) for x in deltas],
            "等分检出力": union_power(np.full(n, 1.0 / n))}


def joint_n_required(pools: Sequence[Pool], weights: Optional[Sequence[float]] = None,
                     g_req: float = 2.0, alpha: float = 0.05,
                     power: float = 0.8) -> Dict[str, float]:
    """全局等比放大期数，使**最坏分摊**下的并集检出力达到 power。

    等比放大是因为所有池共用同一份期数预算（同一彩种同期开奖）。
    `weights` 缺省取等分。
    """
    n_cur = max((p.N for p in pools), default=0)
    if not pools:
        return {"倍数": float("nan"), "需期数": float("nan"), "现有期数": 0}
    n = len(pools)
    w = np.asarray(weights if weights is not None else np.full(n, 1.0 / n),
                   dtype=float)
    log_g = math.log(g_req)
    a_pool = alpha / n
    deltas = [(math.exp(log_g * float(w[i]) / p.choose) - 1.0
               if w[i] > 0 else 0.0) for i, p in enumerate(pools)]

    def f(s: float) -> float:
        miss = 1.0
        for i, p in enumerate(pools):
            miss *= (1.0 - pool_power(p.N * s, p.K, p.choose, deltas[i],
                                      alpha=a_pool))
        return (1.0 - miss) - power

    if f(1.0) >= 0:
        return {"倍数": 1.0, "需期数": n_cur, "现有期数": n_cur}
    lo, hi = 1.0, 2.0
    for _ in range(80):
        if f(hi) >= 0:
            break
        hi *= 2.0
    else:
        return {"倍数": float("inf"), "需期数": float("inf"), "现有期数": n_cur}
    for _ in range(120):
        mid = 0.5 * (lo + hi)
        if f(mid) >= 0:
            hi = mid
        else:
            lo = mid
    return {"倍数": hi, "需期数": hi * n_cur, "现有期数": n_cur}


# ================================================================ 主分析

def analyze_lottery(lottery: str, alpha: float = 0.05,
                    power: float = 0.8, modern_only: Optional[bool] = None,
                    records: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    """单彩种：需求侧 G_req × 检出力 ⇒ 可判 / 不可判 + 需期数。"""
    if lottery not in RETURN_RATE:
        raise KeyError(f"未登记返奖率：{lottery}")
    if modern_only is None:
        modern_only = bool(MODERN_ONLY_DEFAULT.get(lottery, False))
    r0 = float(RETURN_RATE[lottery])
    g_req = 1.0 / r0
    pools = zone_pools(lottery, records, modern_only=modern_only)
    if not pools:
        return {"彩种": lottery, "状态": "无数据", "判决": "未评估"}

    n_factors = sum(p.choose for p in pools)
    delta_req = g_req ** (1.0 / n_factors) - 1.0
    a_pool = alpha / len(pools)

    rows: List[Dict[str, Any]] = []
    for p in pools:
        cg = p.ceiling_gain(a_pool)
        rows.append({
            "分区": p.分区,
            "K": p.K,
            "抽": p.choose,
            "N": p.N,
            "p0": p.p0,
            "sd_rel": p.sd_rel,
            "δ_min": detectable_shift(p.N, p.K, p.choose,
                                      alpha=a_pool, power=power),
            "δ_req": delta_req,
            "δ_req处检出力": pool_power(p.N, p.K, p.choose, delta_req,
                                        alpha=a_pool),
            "单池需期数": n_required(p.p0, p.K, p.choose, delta_req,
                                      alpha=a_pool, power=power),
            "增益点估计": p.point_gain(),
            "增益最坏(宽松)": cg["gain"],
            "增益箱形(错误口径)": p.box_gain(a_pool)[0],
            "χ²观测": cg["chi2_obs"],
            "χ²临界": cg["chi2_crit"],
            "χ²p": cg["chi2_p"],
            "池自身已偏离": bool(cg["chi2_p"] < a_pool),
            "实测最多占比": float(p.counts.max() / p.N),
            "最偏取值": cg["top_values"],
        })

    jp = joint_power(pools, delta_req, alpha=alpha)
    wd = worst_distribution_power(pools, g_req, alpha=alpha)
    jp_worst = wd["检出力"]
    jn = joint_n_required(pools, weights=wd["分摊"], g_req=g_req,
                          alpha=alpha, power=power)
    g_worst = 1.0
    for d in rows:
        g_worst *= d["增益最坏(宽松)"]
    g_pt = 1.0
    for d in rows:
        g_pt *= d["增益点估计"]

    bottleneck = min(rows, key=lambda r: r["δ_req处检出力"])
    judged = bool(jp_worst >= power)

    return {
        "彩种": lottery,
        "状态": "ok",
        "口径": ("现代段" if modern_only else "全史"),
        "期数": max(p.N for p in pools),
        "池数": len(pools),
        "因子数": n_factors,
        "r0": r0,
        "G_req": g_req,
        "δ_req": delta_req,
        "联合检出力": jp,                 # 等分分摊
        "最坏分摊检出力": jp_worst,        # ★ 判决依据
        "最坏分摊": dict(zip([p.分区 for p in pools], wd["分摊"])),
        "最坏分摊下各池δ": dict(zip([p.分区 for p in pools], wd["各池δ"])),
        "判决": ("可判：可获利偏差被数据排除" if judged
                 else "不可判：检出力不足"),
        "瓶颈分区": bottleneck["分区"],
        "需期数": jn["需期数"],
        "期数缺口": (jn["需期数"] / jn["现有期数"]) if jn["现有期数"] else float("nan"),
        "G_最坏(宽松)": g_worst,
        "r_最坏(宽松)": r0 * g_worst,
        "G_点估计": g_pt,
        "r_点估计": r0 * g_pt,
        "分区明细": rows,
    }


def run_all(alpha: float = 0.05, power: float = 0.8,
            lots: Sequence[str] = LOTS) -> List[Dict[str, Any]]:
    return [analyze_lottery(nm, alpha=alpha, power=power) for nm in lots]


# ================================================================ 渲染

def render_markdown(report: Sequence[Dict[str, Any]],
                    alpha: float = 0.05, power: float = 0.8) -> str:
    L: List[str] = []
    L.append(f"## 一、需求侧 × 检出力（α={alpha:.2f}，目标检出力 {power:.0%}）")
    L.append("")
    L.append("| 彩种 | 口径 | 期数 | 因子 | r0 | G_req | **δ_req(等分)** | "
             "**最坏分摊检出力** | 等分分摊 | 判决 | 瓶颈池 | 需期数 | 缺口 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in report:
        if r.get("状态") != "ok":
            L.append(f"| {r['彩种']} | — | — | — | — | — | — | — | — | {r['判决']} | — | — | — |")
            continue
        L.append(f"| {r['彩种']} | {r['口径']} | {r['期数']:,} | {r['因子数']} | "
                 f"{r['r0']:.2f} | {r['G_req']:.3f} | **{r['δ_req']:.2%}** | "
                 f"**{r['最坏分摊检出力']:.3f}** | {r['联合检出力']:.3f} | "
                 f"{r['判决']} | {r['瓶颈分区']} | "
                 f"{r['需期数']:,.0f} | {r['期数缺口']:.1f}× |")
    L.append("")
    L.append("> `δ_req(等分)` = 让单注 EV 转正所需的每因子相对偏差（若各因子同幅承担）。"
             "`最坏分摊检出力` = 在「必需增益如何分摊到各池」上取**最难检测**的那种，"
             "现有数据能看出它的概率 —— **判决用它**。等分只是分摊方案之一，"
             "单看它会挑到对自己有利的假设。")
    L.append("")
    L.append(f"## 二、逐池明细")
    L.append("")
    L.append("| 彩种 | 分区 | K | 抽 | N | p0 | sd_rel | δ_min | δ_req "
             "| δ_req处检出力 | 单池需期数 | 增益点估计 | χ²p |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for r in report:
        if r.get("状态") != "ok":
            continue
        for d in r["分区明细"]:
            L.append(f"| {r['彩种']} | {d['分区']} | {d['K']} | {d['抽']} | "
                     f"{d['N']:,} | {d['p0']:.4f} | {d['sd_rel']:.2%} | "
                     f"{d['δ_min']:.2%} | {d['δ_req']:.2%} | "
                     f"{d['δ_req处检出力']:.2f} | {d['单池需期数']:,.0f} | "
                     f"{d['增益点估计']:.4f} | {d['χ²p']:.4f} |")
    L.append("")
    L.append("> `δ_min` = 检出力为 "
             f"{power:.0%} 时该池能检出的最小相对偏差。`δ_min < δ_req` 才说明"
             "「若真有可获利偏差，本池看得出来」。")
    L.append("")
    return "\n".join(L)
