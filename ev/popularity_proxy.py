# -*- coding: utf-8 -*-
"""
ev/popularity_proxy.py —— 拥挤度**代理**层（低拥挤目标策略的打分地基）

定位：薄封装 ev/popularity.py，把它的输出重标为「拥挤度代理分数」，并写死循环性声明。
**不改 popularity.py 的 β 拟合逻辑**（避免影响既有 WP1/WP3 结论与测试）。

★ 为什么只能叫「代理」，不能叫「真实购彩偏好」
  popularity 用**一等奖注数**反推 β（Poisson GLM，offset=log 票量）。
  而一等奖注数本身会受开奖选择机制影响 —— 用这个量去估计「谁买得多」，
  二者之间存在循环。因此本层输出的不是购彩偏好的直接测量，
  只在同一代理口径下可比；换口径即换结论。

★ crowd(c) 与 μ 的关系
  策略定义 crowd(c) = 预计总投注量 × 组合受欢迎程度。
  popularity 的 μ = 票量 × P(奖级) × exp(β·f)，其中 exp(β·f) 正是「受欢迎程度」，
  故 **μ 即 crowd 的自然度量**（差一个与组合无关的常数 P）。
  z(crowd) 取 z(β·f)，等价于对 log crowd 做标准化。

支持范围：首版仅双色球（大乐透 popularity 模型 No-Go ⇒ 无数据驱动的拥挤度，
必须显式降级，不许静默回退到启发式）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

logger = logging.getLogger(__name__)

# 循环性声明（单一真源）。◆ 文案过 tests/test_narrative_unbinding.py 双向黑名单扫描。
CROWD_PROXY_CAVEAT = (
    "拥挤度代理分数，不是购彩偏好的直接测量：popularity 用一等奖注数反推 β，"
    "而一等奖注数本身会受开奖选择机制影响，二者存在循环；"
    "本层输出只在同一代理口径下可比，换口径即换结论。"
)

# 分位口径声明：分位是相对**全部红球组合**（双色球 1,107,568），不是候选池内的相对分位。
# 用池内分位顶替会让 gate 的语义随池大小与 seed 漂移。
QUANTILE_CALIBER = "popularity 组合分位（中点秩，相对全部红球组合）；0=最冷，1=最热"

SUPPORTED = ("双色球",)

MIN_TRAIN = 1200   # walk-forward 最小训练期数
STEP = 50          # walk-forward 块步长（每期重拟合需约 3.5 小时，必须块式）


def is_supported(lottery: str) -> bool:
    """首版支持范围。"""
    return lottery in SUPPORTED


def go_or_none(lottery: str) -> bool:
    """拥挤度代理是否可用（双色球 True / 大乐透 False）。

    大乐透 popularity 模型未达 Go 门槛（LRT 显著但 CV 仅 2/5 折优于零模型）
    ⇒ 无数据驱动的拥挤度，调用方**必须显式降级**，不许静默改用启发式代理。
    """
    if not is_supported(lottery):
        logger.warning(f"[crowd_proxy] {lottery} 不在首版支持范围 {SUPPORTED}，按不可用处理")
        return False
    try:
        from ev.popularity import get_popularity_model
        return bool(get_popularity_model(lottery).get("go"))
    except Exception as e:
        logger.warning(f"[crowd_proxy] 读 {lottery} 模型失败: {e}")
        return False


def _reds_matrix(tickets: Sequence[Dict[str, Any]]) -> np.ndarray:
    """候选票 → (m, k) 红球矩阵（只取主区，蓝球/后区不参与拥挤度打分）。"""
    rows = []
    for t in tickets:
        r = t.get("红球")
        if r is None:
            r = t.get("号码")
        rows.append([int(x) for x in r])
    return np.asarray(rows, dtype=np.int16)


def crowd_quantiles(lottery: str, tickets: Sequence[Dict[str, Any]],
                    beta: Optional[np.ndarray] = None,
                    mu_: Optional[np.ndarray] = None,
                    sd_: Optional[np.ndarray] = None,
                    persist: bool = True) -> np.ndarray:
    """候选票的拥挤度代理分位 (m,)，0=最冷 1=最热。

    薄封装 popularity.drawn_quantiles —— 不另开打分路径，避免两条口径各算一遍
    （实测不同内存布局会让分数差 1 ULP，落在大并列组里分位可差 1.5e-5）。

    不可用（未 Go / 不支持）→ 返回全 NaN，由调用方显式降级。
    beta/mu_/sd_ 显式传入时不查模型缓存（walk-forward 用），但**仍校验支持范围**。
    """
    from ev.popularity import drawn_quantiles

    if not is_supported(lottery):
        return np.full(len(tickets), np.nan)
    if beta is None and not go_or_none(lottery):
        return np.full(len(tickets), np.nan)

    R = _reds_matrix(tickets)
    if len(R) == 0:
        return np.zeros(0, dtype=np.float64)
    return np.asarray(
        drawn_quantiles(lottery, R, beta=beta, mu_=mu_, sd_=sd_, persist=persist),
        dtype=np.float64)


def load_rows(lottery: str) -> Optional[Dict[str, Any]]:
    """读 walk-forward 需要的原始行（升序）。薄封装 popularity._load_draw_rows。"""
    from ev.popularity import _load_draw_rows
    return _load_draw_rows(lottery)


def walk_forward_beta(lottery: str, rows: Optional[Dict[str, Any]] = None,
                      min_train: int = MIN_TRAIN,
                      step: int = STEP) -> List[Dict[str, Any]]:
    """块级 walk-forward β：切点 c 处只用 c 之前的数据重拟合，供 c..c+step-1 期打分。

    照抄 ev/mechanism/gap_b_drawn_quantile.py 的做法（fit_null=False + persist=False，
    不落盘）。**每期重拟合不可行**（3506 期 × 3.6s ≈ 3.5 小时），故按 step 分块。

    返回 [{"切点": c, "止": hi, "期号": [...], "params": fit_beta 结果 或 None}, ...]
    """
    from ev.popularity import fit_beta

    rows = rows if rows is not None else load_rows(lottery)
    if rows is None:
        return []
    n = int(len(rows["y"]))
    if n <= min_train:
        return []

    out: List[Dict[str, Any]] = []
    for c in range(int(min_train), n, int(step)):
        hi = min(c + int(step), n)
        try:
            fb = fit_beta(rows["reds"][:c], rows["prev"][:c],
                          rows["y"][:c], rows["N"][:c], fit_null=False)
        except Exception as e:
            logger.warning(f"[crowd_proxy] 切点 {c} 拟合失败: {e}")
            fb = None
        out.append({
            "切点": c, "止": hi,
            "期号": list(rows["issues"][c:hi]),
            "params": fb if fb else None,
        })
    return out


def walk_forward_quantiles(lottery: str, tickets: Sequence[Dict[str, Any]],
                           rows: Optional[Dict[str, Any]] = None,
                           min_train: int = MIN_TRAIN,
                           step: int = STEP) -> List[Dict[str, Any]]:
    """逐块给出候选票的拥挤度代理分位（回测用，生产不用）。

    返回 [{"切点","止","期号","分位": (m,) ndarray 或 None(拟合失败)}, ...]
    拟合失败的块分位为 None ⇒ 调用方须跳过该块，不许用相邻块的 β 顶替（那等于前向泄漏）。
    """
    blocks = walk_forward_beta(lottery, rows=rows, min_train=min_train, step=step)
    out: List[Dict[str, Any]] = []
    for b in blocks:
        p = b.get("params")
        if not p:
            out.append({**b, "分位": None})
            continue
        u = crowd_quantiles(lottery, tickets,
                            beta=p["beta"], mu_=p["m_"], sd_=p["s_"], persist=False)
        out.append({**b, "分位": u})
    return out


def proxy_note() -> Dict[str, Any]:
    """给对外文案/元数据用的口径说明。"""
    return {
        "循环性声明": CROWD_PROXY_CAVEAT,
        "分位口径": QUANTILE_CALIBER,
        "支持范围": list(SUPPORTED),
        "walk_forward": {"最小训练期数": MIN_TRAIN, "块步长": STEP},
    }
