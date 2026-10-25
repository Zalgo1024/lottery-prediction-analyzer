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

支持范围：双色球 + 大乐透 —— popularity 模块原生支持这两种（k=6/上界33 与 k=5/上界35）。

★ 必须区分两件事，别混为一谈：
  · 「不在支持范围」= **从未建模**（没为它写过特征、没跑过拟合）⇒ 例如七星彩；
  · 「在支持范围但 No-Go」= **建过模、证据齐备，但判据没过** ⇒ 大乐透
    （2,932 期完整拟合，LRT 显著而 CV 未达门槛，见 docs/P1_大乐透拥挤度重建_结论.md）。

这两档在 `ev/arena/crowd.py` 的三重探测里是**不同的卡点**，`卡在` 字段必须分开报。
把「做了但没通过」报成「没做过」是最像黑箱借口的一种说法 —— 前者有证据可查，
后者听起来只是「我们还没来得及」。
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

SUPPORTED = ("双色球", "大乐透")

MIN_TRAIN = 1200   # walk-forward 最小训练期数
STEP = 50          # walk-forward 块步长（每期重拟合需约 3.5 小时，必须块式）


def is_supported(lottery: str) -> bool:
    """有没有为这个彩种**建过**拥挤度代理（≠ 是否可用）。

    建过模 ⇒ True，哪怕模型 No-Go；从未建模 ⇒ False。
    可用性由 `go_or_none()` 单独判，两者是 `ev/arena/crowd.py` 三重探测里的两档。
    """
    return lottery in SUPPORTED


def go_evidence(lottery: str) -> Dict[str, Any]:
    """把「为什么没过 Go 门槛」的**实测数字**取出来，供原因文案引用。

    ★ 不许在文案里硬编码「CV 仅 2/5 折」这类数字 —— 数据每期都在长，
      硬编码的结论会静默过期，而读者看不出来。一律从模型报告里现取。

    返回 {"applicable","n","lrt_p","cv_better_folds","cv_full","cv_null","verdict"}；
    取不到 → {"applicable": False, "verdict": "..."}（调用方须如实说「取不到」）。
    """
    out: Dict[str, Any] = {"applicable": False, "verdict": "取不到模型报告"}
    if not is_supported(lottery):
        out["verdict"] = f"{lottery} 不在支持范围 {list(SUPPORTED)} 内（从未建模）"
        return out
    try:
        from ev.popularity import get_popularity_model
        rep = (get_popularity_model(lottery) or {}).get("report", {}) or {}
    except Exception as e:                                         # noqa: BLE001
        out["verdict"] = f"读模型报告失败：{type(e).__name__}: {e}"
        return out
    if not rep:
        return out
    for k in ("applicable", "n", "lrt_p", "cv_better_folds", "cv_sig_folds",
              "cv_fold_wilcoxon_p", "cv_global_perm_p", "cv_full",
              "cv_null", "verdict", "go", "y_mean", "y_zero_rate"):
        if k in rep:
            out[k] = rep[k]
    return out


def go_or_none(lottery: str) -> bool:
    """拥挤度代理是否可用（双色球 True / 大乐透 False）。

    大乐透**建过模**（2,932 期）但未达 Go 门槛：LRT 显著（φ 校正后 p≈1.2e-6），
    而时序 5 折 CV 仅 2/5 折优于零模型（门槛 ≥3）。NB2 复核后仍是 2/5 ——
    详见 docs/P1_大乐透拥挤度重建_结论.md。
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
