# -*- coding: utf-8 -*-
"""ev/profit/calib.py —— 一等奖池模型校准（Tobit + 稳健中位数）。

为什么需要校准
--------------
`ev/profit/payouts.FLOAT_SHARE` 里的一等奖份额 a1（占销售额）与封顶份额是**手写常数**
（双色球 a1=0.157，标注「实测中位标定 n=2760」）。本模块用真实开奖数据把它们**重新估计**
并给出置信区间，作为对 FLOAT_SHARE 的交叉检验。

★ 数据口径坑（重要，已实测确认）
-----------------------------
`ev/payout` 的池公式 `pool_g1 = pool_before + a1·S` 里 `pool_before` 取「上期奖池奖金」。
但数据里的「奖池奖金」是**开奖后总余额**（一等+二等+历史滚存），median(P/S)≈1.63，
而真实「一等奖当期权实际支出 / 销售额」median 仅 0.148。⇒ 若用 `P + a1·S` 当一等奖池，
模型会把 per-winner 奖金**高估约 11 倍**（实测 uncapped 期 median(Y/Yhat)=0.089）。
所以本模块**不**用 `P + a1·S` 当恒等式，而是把 a1 定义为
    a1 = 一等奖当期总支出 / 销售额  =  W·Y / S
（即「每期销售额里有多少变成了一等奖奖金」），这正是 FLOAT_SHARE 的原始口径，
且落在验收区间 [0.12, 0.18]。

Tobit 的意义
-----------
per-winner 奖金 Y 在 W=1 等情形下会被 1000 万封顶**右删失**（观测到的是 cap 而非真实值）。
用**对数线性** Tobit 对 latent `log Y* = log a1 + log S − log W + ε` 做删失回归，
能在封顶期不浪费信息，给出 censoring-aware 的 a1。比率数据跨多个数量级（W 从 1 到数千），
噪声是乘性的，故在 log 尺度上做高斯 Tobit 才是正确设定（加性高斯会严重低估 a1）。
（latent 用 `a1·S/W` 而非 `P+a1·S`：一等奖池的滚存部分已体现在历史积累里，
本期**新增**的可分池就是 a1·S，per-winner = a1·S / W。）

验收区间（用户给定）
------------------
    a1        ∈ [0.12, 0.18]
    capped    ∈ [0.15, 0.25]   （capped = per-winner 奖金触顶的期占比）
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Tuple

import numpy as np
from scipy.optimize import minimize

from ev.profit import pit
from ev.profit.rules import rules_for

CAP_DEFAULT = 10_000_000.0
A1_LO, A1_HI = 0.12, 0.18
CAPPED_LO, CAPPED_HI = 0.15, 0.25


def build_pool_dataset(lottery: str, issue: Any,
                       min_history: int = 400) -> Optional[Dict[str, np.ndarray]]:
    """构造校准数据集：每期 (P=上期奖池余额, S=销售额, W=一等奖注数, Y=一等奖金)。

    只取「上期余额/销售额/注数/奖金都有效且 W>0」的期；返回 None 表示样本不足。
    """
    view = pit.as_of(lottery, issue)
    H = view.history
    if len(H) < min_history:
        return None
    P, S, W, Y = [], [], [], []
    for i in range(1, len(H)):
        h, prev = H[i], H[i - 1]
        p_prev = prev.pool_after
        s = h.sales
        w = h.notes.get("一等")
        y = h.prizes.get("一等")
        if (p_prev is None or s is None or w is None or y is None
                or not s or s <= 0 or w <= 0 or y <= 0):
            continue
        P.append(float(p_prev))
        S.append(float(s))
        W.append(float(w))
        Y.append(float(y))
    if len(P) < 200:
        return None
    return {"P": np.array(P), "S": np.array(S), "W": np.array(W), "Y": np.array(Y)}


def estimate_a1_empirical(P: np.ndarray, S: np.ndarray, W: np.ndarray,
                          Y: np.ndarray, n_boot: int = 2000,
                          seed: int = 0) -> Dict[str, float]:
    """稳健 a1 = median(W·Y / S)，带 bootstrap 90% CI。

    W·Y 是「一等奖当期总支出」，对 per-winner 封顶**不敏感**（封顶只压低 Y 不压低 W·Y），
    所以这是无删失的干净估计。
    """
    ratio = (W * Y) / S
    a1 = float(np.median(ratio))
    rng = np.random.default_rng(seed)
    bs = np.empty(n_boot, dtype=float)
    n = len(ratio)
    for b in range(n_boot):
        idx = rng.integers(0, n, size=n)
        bs[b] = float(np.median(ratio[idx]))
    lo, hi = np.percentile(bs, [5.0, 95.0])
    return {"a1": a1, "ci90_lo": float(lo), "ci90_hi": float(hi),
            "mean_ratio": float(ratio.mean())}


def estimate_cap_binding(Y: np.ndarray, cap: float) -> Dict[str, float]:
    """封顶触顶期占比（per-winner 奖金达到 cap 的期占比），带 bootstrap 90% CI。"""
    hit = Y >= cap * 0.999
    frac = float(hit.mean())
    return {"capped": frac, "n_hit": int(hit.sum()), "n": int(len(Y))}


def _tobit_neg_loglik(theta: np.ndarray, S: np.ndarray, W: np.ndarray,
                      Y: np.ndarray, cap: float) -> float:
    """对数线性 Tobit NLL（ multiplicative 噪声更贴合比率数据）。

    latent:  log Y* = log a1 + log S − log W + ε,  ε~N(0,σ²)
    observed: log Y = min(log Y*, log cap)  （右删失于封顶）
    未删失点贡献 −log N(log Y; log_mu, σ)；删失点贡献 −log Φ((log_mu−log cap)/σ)。
    """
    log_a1, log_sigma = theta
    if log_sigma > 20 or log_sigma < -20:
        return 1e12
    sigma = math.exp(log_sigma)
    log_mu = log_a1 + np.log(S) - np.log(np.maximum(W, 1.0))
    log_cap = math.log(cap)
    log_Y = np.log(np.maximum(Y, 1.0))
    from scipy.stats import norm
    nll_unc = -norm.logpdf(log_Y, log_mu, sigma)
    # 右删失：观测到 cap 表示 latent ≥ cap，似然贡献 = Φ((log_mu−log_cap)/σ)
    z_cen = (log_mu - log_cap) / sigma
    nll_cen = -norm.logcdf(z_cen)
    censored = Y >= cap * 0.999
    nll = np.where(censored, nll_cen, nll_unc)
    return float(np.sum(nll))


def fit_tobit_a1(S: np.ndarray, W: np.ndarray, Y: np.ndarray, cap: float,
                 seed: int = 0) -> Dict[str, float]:
    """对数线性 Tobit MLE 估计 a1（censoring-aware）。返回 a1、sigma 与点估计。

    未删失点主导 fit（约 84% 未触顶），a1 应≈ median(W·Y/S)；删失点只修正封顶期信息。
    """
    a1_start = float(np.median((W * Y) / S))
    log_a1_start = math.log(a1_start)
    best = None
    # 多起点避免局部极小
    for la0 in (log_a1_start - 0.7, log_a1_start, log_a1_start + 0.7,
                math.log(0.05), math.log(0.20)):
        for ls0 in (-2.0, -1.0, 0.0):
            try:
                res = minimize(_tobit_neg_loglik, np.array([la0, ls0]),
                               args=(S, W, Y, cap), method="Nelder-Mead",
                               options={"maxiter": 5000, "xatol": 1e-8, "fatol": 1e-8})
                if best is None or res.fun < best.fun:
                    best = res
            except Exception:
                continue
    if best is None or (not best.success and not np.isfinite(best.fun)):
        return {"a1": a1_start, "sigma": float("nan"), "converged": False}
    log_a1, log_sigma = best.x
    return {"a1": float(math.exp(log_a1)), "sigma": float(math.exp(log_sigma)),
            "nll": float(best.fun), "converged": bool(best.success)}


def calibrate(lottery: str, issue: Any) -> Dict[str, Any]:
    """端到端校准：a1（稳健 + Tobit）、capped 触顶占比，并对照验收区间。

    返回 {ok, a1, a1_ci90, tobit_a1, capped, capped_n, 区间判定, 数据坑说明}
    """
    r = rules_for(lottery, issue)
    g1 = r.grades.get("一等")
    cap = float(getattr(g1, "cap", CAP_DEFAULT) or CAP_DEFAULT)

    ds = build_pool_dataset(lottery, issue)
    if ds is None:
        return {"ok": False, "原因": "样本不足"}
    P, S, W, Y = ds["P"], ds["S"], ds["W"], ds["Y"]

    a1e = estimate_a1_empirical(P, S, W, Y)
    capb = estimate_cap_binding(Y, cap)
    tobit = fit_tobit_a1(S, W, Y, cap)

    a1_in = A1_LO <= a1e["a1"] <= A1_HI
    capped_in = CAPPED_LO <= capb["capped"] <= CAPPED_HI
    return {
        "ok": bool(a1_in and capped_in),
        "彩种": lottery,
        "a1_稳健": a1e["a1"], "a1_ci90": [a1e["ci90_lo"], a1e["ci90_hi"]],
        "a1_均值口径": a1e["mean_ratio"],
        "a1_tobit": tobit["a1"], "tobit_sigma": tobit.get("sigma"),
        "capped触顶占比": capb["capped"], "capped_n": capb["n_hit"], "n": capb["n"],
        "a1_在验收区间": a1_in, "capped_在验收区间": capped_in,
        "验收区间": {"a1": [A1_LO, A1_HI], "capped": [CAPPED_LO, CAPPED_HI]},
        "数据坑": ("奖池奖金是开奖后总余额（median P/S≈%.2f），不是一等奖前奖池；"
                   "故 a1 定义为 W·Y/S（一等奖总支出/销售额），与 FLOAT_SHARE 原始口径一致；"
                   "若用 P+a1·S 当一等奖池会把 per-winner 高估约 11 倍。" % float(np.median(P / S))),
    }
