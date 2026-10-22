# -*- coding: utf-8 -*-
"""ev/profit/crowd_tilt.py —— 数字型/七星彩的倾斜来源（代理 π，`crowd_pi_v2`）。

与 popularity（乐透型）的分工
--------------------------
`ev/popularity` 的 μ 模型由「一等奖注数」GLM 反推，只对双色球/大乐透有 Go 口径。
数字型没有那套特征（没有红球形态），改用 `ev.crowd_model.fit_crowd_v2` 的
**逐位边际 π**（Poisson MLE，用全部奖级注数反推）：

    μ(ω) ∝ Π_i π_i(x_i)              （位置独立近似）

⇒ s(ω) = log μ(ω) 中心化 = Σ_i s_i(x_i)（**可加**）⇒ 配分函数可因子分解：

    Z(κ) = Σ_ω exp(−κ·s(ω)) = exp(κ·offset) · Π_i Σ_d π_i(d)^(−κ)

`z_factorization_check` 就是这条恒等式的数值自检（抓实现错误与可加性破裂）。

诚实声明（写进每次输出的 `note`，一字不改）
----------------------------------------
1. π 是**未检验代理**：位置独立是结构假设，没有 Go 门槛（popularity 的 CV 检验
   只对双色球/大乐透做过）⇒ 判决等级封顶「条件性」。
2. 倾斜只覆盖**外层**（七星彩前 6 位 / 数字型全部位）；第 7 位/内层分量恒均匀
   （与乐透型同一 MVP 近似）。
3. 福彩3D 的直选注数自 2021-12-15（约 2022 年）起全为 0 ⇒ π 不可测 ⇒ None。
"""
from __future__ import annotations

import json
import logging
import math
import os
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 走代理 π 的彩种（排列型；福彩3D 会被数据门挡在更前面）
PI_LOTTERIES: Tuple[str, ...] = ("排列3", "排列5", "福彩3D", "七星彩")

# 倾斜来源标识（与 popularity 的 "measured_GO" / "walk_forward" 并列）
CROWD_PI_SOURCE = "crowd_pi_v2"

# 福彩3D 直选注数零点起始（实测：最后一条 >0 是 2021-12-14，之后全 0）
FD3D_ZERO_FROM = "2021-12-15"
FD3D_ZERO_REASON = (
    f"福彩3D 直选注数自 {FD3D_ZERO_FROM}（2022 年起）全为 0"
    "（lottery_data/福彩3D销售奖级数据.csv）⇒ 无法反推拥挤度代理 π，"
    "也不拿 2021 年前的陈旧 π 外推到 2026 年决策"
)

# 3 位彩种默认只用最近 N 期拟合（与 crowd_model.fit_crowd_v2 默认一致：
# 兼顾早年组选口径波动与拟合耗时）
_3DIGIT_LOOKBACK = 2000

_PI_FLOOR = 1e-300          # log 保护（π 的 MLE 可能压到 1e-9 量级）


# ---------------------------------------------------------------- π 拟合窗口

def _fit_window(lottery: str) -> List[dict]:
    """π 拟合所用的观测窗口（与 `crowd_model.fit_crowd_v2(lottery)` 默认口径一致）。

    复刻它的过滤：七星彩/排列5 只保留有票量的期；3 位彩种截最近 2000 期。
    只有拿到**同一窗口**，边际频率检验（κ_upper_marginal）才与 π 条件化自洽。
    """
    from ev import crowd_model as CM

    obs = CM._load_observations(lottery)
    if lottery in ("七星彩", "排列5"):
        obs = [d for d in obs if d["N"] is not None]     # 单奖级必须有票量
    if CM._is_3digit(lottery):
        obs = obs[-_3DIGIT_LOOKBACK:]
    return obs


def unavailable_reason(lottery: str) -> Optional[str]:
    """该彩种为什么没有代理 π（有则返回原因，没有返回 None）。"""
    if lottery == "福彩3D":
        return FD3D_ZERO_REASON
    return None


# ---------------------------------------------------------------- π 模型（带落盘缓存）

def _pi_cache_path(lottery: str) -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    d = os.path.join(root, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"crowd_pi_{lottery}.json")


def _pi_fingerprint(lottery: str) -> Dict[str, Any]:
    """缓存指纹：窗口期数（数据更新 ⇒ 期数变 ⇒ 自动过期重拟合）。"""
    obs = _fit_window(lottery)
    return {"n_obs": len(obs), "lottery": lottery}


def _load_pi_from_disk(lottery: str) -> Optional[Dict[str, Any]]:
    """读磁盘 π 缓存；窗口期数与当前数据不一致 → 视为过期（同 popularity 口径）。"""
    try:
        p = _pi_cache_path(lottery)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        if obj.get("source") != CROWD_PI_SOURCE:
            return None
        n_now = len(_fit_window(lottery))
        if int(obj.get("n_obs", -1)) != int(n_now):
            return None
        return obj
    except Exception as e:                                   # noqa: BLE001
        logger.debug(f"[crowd_tilt] 读 π 缓存失败 {lottery}: {e}")
        return None


def _save_pi_to_disk(lottery: str, obj: Dict[str, Any]) -> None:
    try:
        with open(_pi_cache_path(lottery), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception as e:                                   # noqa: BLE001
        logger.debug(f"[crowd_tilt] 写 π 缓存失败 {lottery}: {e}")


_PI_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def pi_model(lottery: str, refresh: bool = False) -> Optional[Dict[str, Any]]:
    """逐位边际 π 模型（带进程内 + 磁盘两级缓存）。

    返回 {"pi": (位, 号域) 嵌套 list, "zones": [...], "n_obs": int,
          "converged": bool, "n_iter": int, "nll": float,
          "window": {"n": int, "first": str, "last": str}, "source": ...}
    不可用（3D 数据门 / 拟合失败）→ None。
    """
    if lottery not in PI_LOTTERIES:
        return None
    reason = unavailable_reason(lottery)
    if reason is not None:
        logger.info(f"[crowd_tilt] {lottery}: {reason}")
        return None
    if not refresh and lottery in _PI_CACHE:
        return _PI_CACHE[lottery]

    obj = None if refresh else _load_pi_from_disk(lottery)
    if obj is None:
        from ev import crowd_model as CM

        obs = _fit_window(lottery)
        if len(obs) < 200:
            logger.warning(f"[crowd_tilt] {lottery} 有效拟合期仅 {len(obs)}，不拟合")
            _PI_CACHE[lottery] = None
            return None
        m = CM.fit_crowd_v2(lottery, obs=obs)
        if m is None:
            _PI_CACHE[lottery] = None
            return None
        issues = [str(d.get("issue", "")) for d in obs]
        obj = {
            "source": CROWD_PI_SOURCE,
            "lottery": lottery,
            "n_obs": int(m.n_obs),
            "pi": [[float(v) for v in row] for row in np.asarray(m.pi).tolist()],
            "zones": [int(z) for z in m.zones],
            "converged": bool(m.converged),
            "n_iter": int(m.n_iter),
            "nll": float(m.nll),
            "window": {"n": len(obs),
                       "first": issues[0] if issues else "",
                       "last": issues[-1] if issues else ""},
        }
        _save_pi_to_disk(lottery, obj)
    _PI_CACHE[lottery] = obj
    return obj


# ---------------------------------------------------------------- 打分

def _pi_array(pm: Dict[str, Any], width: int) -> np.ndarray:
    """(位, 号域) → 只取前 width 行、前 10 列（外层全是 0-9）。"""
    pi = np.asarray(pm["pi"], dtype=np.float64)
    return np.maximum(pi[:width, :10], _PI_FLOOR)


def _raw_scores(lottery: str, pm: Dict[str, Any]) -> np.ndarray:
    """未中心化的 log-拥挤度分：raw(ω) = Σ_i log π_i(x_i)（只外层位）。

    中心化分数 = raw − raw.mean()；`Tilt.offset` 取 raw.mean()，于是
    `scores + offset == raw`，下游 `exp(scores + offset) = Π π_i(x_i)` 即该组合的
    **相对购买概率**（在宇宙上均值为 1，与 popularity 的 exp(raw) 同语义）。
    配分函数因子分解成立的前提也是这个 offset（见 `z_factorization_check`）。
    """
    from ev.profit.outcomes import outer_universe

    U = outer_universe(lottery)
    w = U.shape[1]
    logpi = np.log(_pi_array(pm, w))
    raw = np.zeros(len(U), dtype=np.float64)
    for i in range(w):
        raw += logpi[i, U[:, i]]
    return raw


def crowd_scores(lottery: str, pm: Optional[Dict[str, Any]] = None) -> Optional[np.ndarray]:
    """外层全集的中心化分数 s(ω) = Σ_i log π_i(x_i) − mean(·)。"""
    if pm is None:
        pm = pi_model(lottery)
    if pm is None:
        return None
    raw = _raw_scores(lottery, pm)
    return raw - float(raw.mean())


def combo_score(lottery: str, digits: Sequence[int],
                pm: Optional[Dict[str, Any]] = None) -> Optional[float]:
    """单个组合的中心化分数（只吃**外层位**；七星彩传 7 位也只学前 6 位）。"""
    if pm is None:
        pm = pi_model(lottery)
    if pm is None:
        return None
    from ev.profit.rules import rules_for

    r = rules_for(lottery)
    w = r.outer_width
    logpi = np.log(_pi_array(pm, w))
    # 中心化偏移 = 外层全集 raw 的均值（与 crowd_scores / crowd_tilt 同口径；
    # 七星彩只学前 6 位，offset 也只来自前 6 位）
    raw = _raw_scores(lottery, pm)
    offset = float(raw.mean())
    return float(sum(logpi[i, int(digits[i])] for i in range(w)) - offset)


def crowd_tilt(lottery: str) -> Optional[Any]:
    """数字型/七星彩的 `Tilt`（source="crowd_pi_v2"）；不可用 → None。"""
    from ev.profit.tilt import Tilt

    pm = pi_model(lottery)
    if pm is None:
        return None
    raw = _raw_scores(lottery, pm)
    if raw is None:
        return None
    offset = float(raw.mean())
    scores = raw - offset
    # 中点秩分位（与 popularity.drawn_quantiles 同口径：公平开奖下 mean_u ≡ 0.5）
    ss = np.sort(scores)
    lt = np.searchsorted(ss, scores, side="left")
    le = np.searchsorted(ss, scores, side="right")
    quantiles = (lt + le) / (2.0 * float(len(ss)))
    return Tilt(
        lottery=lottery, scores=scores, quantiles=quantiles,
        source=CROWD_PI_SOURCE, offset=offset,
        note=("倾斜由**逐位边际 π**（Poisson MLE，全部奖级注数反推，ev/crowd_model v2）"
              "构造：μ(ω)∝Ππ_i(x_i)，s=Σlogπ_i 中心化。★代理未检验：位置独立是结构"
              "假设、无 Go 门槛；只覆盖外层，第 7 位/内层分量恒均匀。"),
    )


# ---------------------------------------------------------------- Z 因子分解自检

def z_factorization_check(lottery: str, kappas: Sequence[float] = (0.5, 1.0, 3.0),
                          tol: float = 1e-9) -> Dict[str, Any]:
    """|Z(κ) − exp(κ·offset)·Π_i Σ_d π_i(d)^(−κ)| / Z < tol。

    直接法对 10^w 个组合求和（七星彩 100 万，numpy 秒级）；分解法是 w 个 10 项和
    的乘积。两者相对误差必须在 tol 内——否则说明打分不可加或实现有错。
    """
    pm = pi_model(lottery)
    if pm is None:
        return {"可用": False, "原因": unavailable_reason(lottery) or "π 模型不可用"}
    raw = _raw_scores(lottery, pm)
    if raw is None:
        return {"可用": False, "原因": "打分失败"}
    offset = float(raw.mean())
    scores = raw - offset
    from ev.profit.outcomes import outer_universe

    U = outer_universe(lottery)
    w = U.shape[1]
    pi = _pi_array(pm, w)
    rows = []
    for k in kappas:
        z_direct = float(np.exp(-k * scores).sum())
        # 因子分解：Z(κ) = exp(κ·offset) · Π_i Σ_d π_i(d)^(−κ)
        z_fact = math.exp(k * offset)
        for i in range(w):
            z_fact *= float((pi[i] ** (-k)).sum())
        rel = abs(z_direct - z_fact) / z_direct if z_direct else float("inf")
        rows.append({"κ": float(k), "Z直接": z_direct, "Z分解": z_fact,
                     "相对误差": rel})
    return {
        "可用": True, "彩种": lottery, "容差": tol,
        "通过": all(r["相对误差"] < tol for r in rows),
        "明细": rows,
        "口径": ("Z(κ)=Σ_ω exp(−κ·s(ω)) 的直接求和 vs exp(κ·offset)·Π_i Σ_d π_i(d)^(−κ) 的"
                 "因子分解（offset=raw.mean() 使 s 中心化）；恒等式成立 ⇒ 打分可加、"
                 "offset 口径正确"),
    }


# ---------------------------------------------------------------- 逐位开奖计数

def draw_counts(lottery: str) -> Optional[np.ndarray]:
    """π 拟合窗口内的**逐位开奖计数** n_{i,d}（只数外层位）。

    七星彩只取前 6 位（第 7 位是内层，倾斜不覆盖）。用于 κ_upper_marginal 的
    边际频率检验——窗口与 π 拟合窗口一致，保证「π 条件化」自洽。
    """
    from ev.profit.rules import rules_for

    r = rules_for(lottery)
    w = r.outer_width
    obs = _fit_window(lottery)
    if not obs:
        return None
    Z = np.array([d["digits"][:w] for d in obs], dtype=int)
    counts = np.zeros((w, 10), dtype=np.int64)
    for i in range(w):
        counts[i] = np.bincount(Z[:, i], minlength=10)
    return counts


__all__ = [
    "CROWD_PI_SOURCE", "FD3D_ZERO_FROM", "FD3D_ZERO_REASON", "PI_LOTTERIES",
    "combo_score", "crowd_scores", "crowd_tilt", "draw_counts", "pi_model",
    "unavailable_reason", "z_factorization_check",
]
