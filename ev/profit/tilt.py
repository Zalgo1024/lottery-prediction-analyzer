# -*- coding: utf-8 -*-
"""ev/profit/tilt.py —— 开奖机制倾斜 P(ω|κ)。

定义（唯一口径，勿改）
----------------------
    s(ω) = log μ(ω) 的中心化分数          μ = 该组合的**期望购买注数**
    P(ω|κ) = exp(−κ·s(ω)) / Z(κ)          ⇒ P ∝ μ^(−κ)
    Z(κ) = Σ_ω exp(−κ·s(ω))

κ 的语义：**κ=1 表示「购买量减半的组合，被开出的概率翻倍」**。
不是「概率倍数」、不是「奖金倍数」、不是 λ。κ=0 ⇒ 严格均匀（公平开奖）。

★ 为什么用 log μ 而不是 μ 的分位：分位有界且非线性，Z 与外推都难算，κ 也无法相加；
  log μ 是指数族的自然参数，Z 可精确求和、κ₁+κ₂ 有闭式语义。
  分位只在**报告层**用（因为机制测量的实测强度是用分位表达的）。

★ 模型前提（会写进每份输出的 `备注`）：
  倾斜**不含蓝球/后区/第 7 位分量**（这是 `ev/popularity.features_matrix` 只吃
  红球/前区的 MVP 近似）。推论：双色球六等（只中蓝球）的概率**不随 κ 变**。
  这不是实现偷懒，是模型的一部分——报数时必须说清。

不可用即返回 None（调用方必须显式降级），不许静默退化成均匀。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np
from scipy.stats import chi2

# 与 ev/popularity 的 Go 门槛对齐：只有 model.go=True 才有形态打分口径
SUPPORTED = ("双色球", "大乐透")

# κ 网格与硬上限（κ>3 时最冷 1% 的概率提升已 >2.5×，模型失去可信度）
KAPPA_MAX = 3.0
KAPPA_GRID = np.linspace(0.0, KAPPA_MAX, 33)


@dataclass(frozen=True)
class Tilt:
    lottery: str
    scores: np.ndarray       # (M,) 中心化后的 log-popularity 分数
    quantiles: np.ndarray    # (M,) 中点秩分位（0=最冷）
    source: str              # "measured_GO"（全样本）| "walk_forward"（块级重拟合）
    offset: float            # 中心化偏移量（|offset| 应远小于 1）
    note: str
    beta_asof: Optional[str] = None   # walk_forward 时 = 拟合只用该期号之前的数据


_CACHE: Dict[str, Optional[Tilt]] = {}


def outer_tilt(lottery: str, *, beta: Optional[np.ndarray] = None,
               mu_: Optional[np.ndarray] = None, sd_: Optional[np.ndarray] = None,
               asof: Optional[str] = None) -> Optional[Tilt]:
    """取外层结局的倾斜依据；模型未 Go / 不支持 → None（必须显式降级）。

    默认（无显式参数）= **全样本**口径：`popularity.get_popularity_model` 的 β
    （进程内缓存，同一彩种只建一次）。
    显式传入 beta/mu_/sd_ = **walk-forward** 口径：β 由调用方用「asof 期号之前」
    的数据拟合，本函数只负责打分；`source="walk_forward"`、`beta_asof=asof`，
    与全样本口径在报告里可区分（回测元数据的「β口径」即取自这里）。
    """
    if beta is None:
        if lottery in _CACHE:
            return _CACHE[lottery]

        # 数字型/七星彩：代理 π（`crowd_pi_v2`）倾斜来源（位置独立、无 Go 门槛）
        if lottery in ("排列3", "排列5", "福彩3D", "七星彩"):
            from ev.profit import crowd_tilt as CT
            t = CT.crowd_tilt(lottery)
            _CACHE[lottery] = t          # 缓存 None 也入（不可用即显式降级）
            return t

        if lottery not in SUPPORTED:
            _CACHE[lottery] = None
            return None

        from ev import popularity as pop

        params = pop.get_popularity_model(lottery)
        if not params.get("go"):
            _CACHE[lottery] = None
            return None

        us = pop.universe_scores(lottery)
        if us is None or us["quantiles"] is None:
            _CACHE[lottery] = None
            return None

        t = Tilt(
            lottery=lottery, scores=us["scores"],
            quantiles=us["quantiles"],
            source="measured_GO", offset=float(us["offset"]),
            note=("倾斜只建在红球/前区形态上（popularity MVP 近似）⇒ 蓝球/后区边际严格均匀，"
                  "只中蓝球的奖级（双色球六等）概率不随 κ 变。"),
        )
        _CACHE[lottery] = t
        return t

    # ---- walk-forward 口径：调用方的 β（无前视），不读模型缓存 ----
    if lottery not in SUPPORTED:
        return None

    from ev import popularity as pop

    us = pop.universe_scores(lottery, beta=beta, mu_=mu_, sd_=sd_)
    if us is None or us["quantiles"] is None:
        return None

    return Tilt(
        lottery=lottery, scores=us["scores"],
        quantiles=us["quantiles"],
        source="walk_forward", offset=float(us["offset"]),
        note=("倾斜只建在红球/前区形态上（popularity MVP 近似）⇒ 蓝球/后区边际严格均匀，"
              "只中蓝球的奖级（双色球六等）概率不随 κ 变。"
              + (f" β 为 walk-forward：只用 {asof} 之前的数据拟合。" if asof else "")),
        beta_asof=asof,
    )


def weights(tilt: Tilt, kappa: float) -> np.ndarray:
    """P(ω|κ) 在**外层**上的边际权重（已归一化，和为 1）。"""
    if kappa <= 0:
        m = len(tilt.scores)
        return np.full(m, 1.0 / m)
    z = np.exp(-kappa * tilt.scores)
    return z / z.sum()


def normalization(tilt: Tilt, kappa: float) -> float:
    """Z(κ)（未归一化的配分函数，用于日志与自检）。"""
    if kappa <= 0:
        return float(len(tilt.scores))
    return float(np.exp(-kappa * tilt.scores).sum())


def implied_mean_quantile(tilt: Tilt, kappa: float) -> float:
    """E_u(κ) = Σ_ω P(ω|κ)·u(ω)。公平开奖时恒为 0.5（中点秩的无偏性）。"""
    if kappa <= 0:
        return float(tilt.quantiles.mean())
    w = weights(tilt, kappa)
    return float((w * tilt.quantiles).sum())


def cold_boost(tilt: Tilt, kappa: float, q: float = 0.01) -> float:
    """最冷 q 比例的组合，其被开出概率相对均匀的倍数（报告用换算量）。"""
    if kappa <= 0:
        return 1.0
    thr = np.quantile(tilt.quantiles, q)
    cold = tilt.quantiles <= thr
    if not cold.any():
        return float("nan")
    w = weights(tilt, kappa)
    return float(w[cold].sum() / cold.mean())


def kappa_for_mean_quantile(tilt: Tilt, target: float) -> float:
    """反解使 E_u(κ)=target 的 κ（网格 + 线性插值；超出网格返回 KAPPA_MAX）。"""
    if target >= implied_mean_quantile(tilt, 0.0):
        return 0.0
    grid = [implied_mean_quantile(tilt, k) for k in KAPPA_GRID]
    for i in range(1, len(grid)):
        if grid[i] <= target <= grid[i - 1]:
            lo, hi = grid[i - 1], grid[i]
            if abs(lo - hi) < 1e-15:
                return float(KAPPA_GRID[i])
            f = (lo - target) / (lo - hi)
            return float(KAPPA_GRID[i - 1] + f * (KAPPA_GRID[i] - KAPPA_GRID[i - 1]))
    return KAPPA_MAX


def kappa_upper(lottery: str, alpha: float = 0.05,
                observed: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """**已测机制强度的上界**（一参数族内）。

    取实测 mean_u 的单侧 (1−alpha) 下界，反解 κ。样例内（in-sample）口径，
    不等于「独立证据」——裁决仍要看 ev/mechanism 的 walk-forward 版本。

    返回 {可用, kappa, mean_u, se, n, 口径, 免责}
    """
    tilt = outer_tilt(lottery)
    if tilt is None:
        return {"可用": False, "原因": "拥挤度打分不可用（模型未 Go 或不支持该彩种）"}

    if observed is None:
        from ev import popularity as pop
        rep = pop.actual_draw_quantile_report(lottery)
        if not rep.get("applicable") or rep.get("se") in (None, 0):
            return {"可用": False, "原因": "实测分位报告不可用"}
        observed = rep

    mean_u = float(observed["mean_u"])
    se = float(observed["se"])
    n = int(observed.get("n") or 0)
    z = 1.6448536269514722 if alpha == 0.05 else _z(alpha)
    target = mean_u - z * se                      # 单侧下界
    k = kappa_for_mean_quantile(tilt, target)
    return {
        "可用": True,
        "kappa": round(k, 4),
        "mean_u": mean_u,
        "se": se,
        "n": n,
        "alpha": alpha,
        "目标平均分位": round(target, 5),
        "口径": "in-sample（模型见过这些开奖号，非独立证据）",
        "免责": ("κ_upper 只约束「平均分位的偏移」，不排除形状不同的一族机制"
                 "（例如只把最冷 0.1% 的概率提高 10 倍而均值不变）。属族内上界。"),
    }


def _z(alpha: float) -> float:
    """单侧正态分位数（避免为一个小数引入 scipy 依赖的额外路径）。"""
    from scipy.stats import norm
    return float(norm.ppf(1 - alpha))


def kappa_upper_marginal(lottery: str, tilt: Optional[Tilt] = None,
                         alpha: float = 0.05) -> Dict[str, Any]:
    """**代理 π 条件下的边际频率一致性强度的上界**（一参数族内）。

    把每个位置 i 的开奖边缘分布模型为

        p_i(d;κ) = π_i(d)^(−κ) / Σ_d' π_i(d')^(−κ)

    用 π 拟合窗口内的逐位开奖计数 n_{i,d} 做极大似然：

        LL(κ) = Σ_i Σ_d n_{i,d}·log p_i(d;κ)

    κ̂ = argmax LL(κ)；上界 = max{κ : 2(LL(κ̂) − LL(κ)) ≤ χ²_{1,1−2α}}。

    ★ 与 `kappa_upper`（mean_u 法，乐透型专用）**同约定**（都是对 κ 这一机制倾斜
      强度的上界，κ>0 = 冷号被开出概率更高），但来自不同的族内证据。

    返回 {可用, kappa_hat, kappa_upper, kappa_lower, alpha, n, 口径, 免责}
    """
    from ev.profit import crowd_tilt as CT

    pm = CT.pi_model(lottery)
    if pm is None:
        return {"可用": False, "原因": CT.unavailable_reason(lottery) or "π 模型不可用"}
    counts = CT.draw_counts(lottery)
    if counts is None:
        return {"可用": False, "原因": "逐位开奖计数不可用"}

    w = counts.shape[0]
    pi = np.maximum(np.asarray(CT._pi_array(pm, w), dtype=np.float64), 1e-300)
    if pi.shape[0] != w or pi.shape[1] != counts.shape[1]:
        return {"可用": False, "原因": "π 号域与计数号域不一致"}
    n_total = float(counts.sum())

    # κ 网格（含负区间：负 κ = 实测边际偏向热号）
    grid = np.linspace(-3.0, KAPPA_MAX, 601)
    ll = np.empty(len(grid), dtype=np.float64)
    log_pi = np.log(pi)

    def logZ(k: float) -> np.ndarray:
        return np.log(np.sum(pi ** (-k), axis=1))      # (w,)

    for gi, k in enumerate(grid):
        lz = logZ(k)
        ll[gi] = float(np.sum(counts * (-k * log_pi - lz[:, None])))

    i_hat = int(np.argmax(ll))
    k_hat = float(grid[i_hat])
    ll_max = ll[i_hat]
    crit = float(chi2.ppf(1.0 - 2.0 * alpha, 1)) if alpha < 0.5 else 0.0

    def _right_endpoint() -> float:
        """从 κ̂ 向上找 LR 区间右端点（LL 凹 ⇒ 满足集为区间）。"""
        last_ok = k_hat
        for gi in range(i_hat, len(grid)):
            if 2.0 * (ll_max - ll[gi]) <= crit:
                last_ok = float(grid[gi])
            else:
                if gi > i_hat:
                    lo_k, hi_k = grid[gi - 1], grid[gi]
                    lo_v, hi_v = 2.0 * (ll_max - ll[gi - 1]), 2.0 * (ll_max - ll[gi])
                    if hi_v != lo_v:
                        f = (crit - lo_v) / (hi_v - lo_v)
                        return float(lo_k + f * (hi_k - lo_k))
                return last_ok
        return last_ok

    def _left_endpoint() -> float:
        last_ok = k_hat
        for gi in range(i_hat, -1, -1):
            if 2.0 * (ll_max - ll[gi]) <= crit:
                last_ok = float(grid[gi])
            else:
                if gi < i_hat:
                    hi_k, lo_k = grid[gi + 1], grid[gi]
                    hi_v, lo_v = 2.0 * (ll_max - ll[gi + 1]), 2.0 * (ll_max - ll[gi])
                    if hi_v != lo_v:
                        f = (crit - lo_v) / (hi_v - lo_v)
                        return float(lo_k + f * (hi_k - lo_k))
                return last_ok
        return last_ok

    return {
        "可用": True,
        "kappa_hat": round(k_hat, 4),
        "kappa_upper": round(_right_endpoint(), 4),
        "kappa_lower": round(_left_endpoint(), 4),
        "alpha": alpha,
        "LR临界(1-2α)": round(crit, 4),
        "n": int(n_total),
        "位宽": int(w),
        "口径": ("边际频率一致性（代理 π 条件化）：每个位置的边际开奖频率用 "
                 "p_i(d;κ)=π_i(d)^(−κ)/Σ_d'π_i(d')^(−κ) 拟合，LL 取极大；上界为 "
                 "χ²_{1,1−2α} LR 区间右端点。与 `kappa_upper`（mean_u 法）同 κ 约定。"),
        "免责": ("只约束**逐位边际**频率，不约束位间联合分布；π 是位置独立的未检验代理"
                 "（无 Go 门槛），故本上界仅作「代理条件下」的强度包络，不得当作独立证据。"),
    }
