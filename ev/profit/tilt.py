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

from ev.profit.outcomes import outer_universe
from ev.profit.rules import rules_for

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
    source: str              # "measured_GO"
    offset: float            # 中心化偏移量（|offset| 应远小于 1）
    note: str


_CACHE: Dict[str, Optional[Tilt]] = {}


def outer_tilt(lottery: str) -> Optional[Tilt]:
    """取外层结局的倾斜依据；模型未 Go / 不支持 → None（必须显式降级）。"""
    if lottery in _CACHE:
        return _CACHE[lottery]

    if lottery not in SUPPORTED:
        _CACHE[lottery] = None
        return None

    from ev import popularity as pop

    params = pop.get_popularity_model(lottery)
    if not params.get("go"):
        _CACHE[lottery] = None
        return None

    R = outer_universe(lottery)
    F = pop.features_matrix(np.asarray(R, dtype=np.int16))
    s_ = np.asarray(params["s_"], dtype=np.float64)
    m_ = np.asarray(params["m_"], dtype=np.float64)
    beta = np.asarray(params["beta"], dtype=np.float64)
    raw = ((F - m_) / s_) @ beta
    offset = float(raw.mean())
    scores = raw - offset
    u = pop.drawn_quantiles(lottery, R)
    if not np.isfinite(u).all():
        _CACHE[lottery] = None
        return None

    t = Tilt(
        lottery=lottery, scores=scores.astype(np.float64),
        quantiles=np.asarray(u, dtype=np.float64),
        source="measured_GO", offset=offset,
        note=("倾斜只建在红球/前区形态上（popularity MVP 近似）⇒ 蓝球/后区边际严格均匀，"
              "只中蓝球的奖级（双色球六等）概率不随 κ 变。"),
    )
    _CACHE[lottery] = t
    return t


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
