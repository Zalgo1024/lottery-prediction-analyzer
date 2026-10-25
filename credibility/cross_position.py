# -*- coding: utf-8 -*-
"""credibility/cross_position.py —— 同一期内「不同位之间」的依赖

为什么这是新维度
    `credibility/randomness_audit.py` 的 T4「序列(Serial m=2)」检验的是**跨期**
    相邻有序对 (x_t, x_{t+1})；同一期内部、第 1 位与第 2 位之间的依赖从来没有被
    检验过。两者是不同的备择：前者是「时间上的粘滞」，后者是「同一台/同一组摇奖
    设备在一次开奖内的耦合」。

为什么它值钱
    位间正相关会抬高「各位取同一数字」这类形态的概率，而**固定奖票面不变**
    ⇒ 这是与「池内边际偏差」**互不重叠**的第二条组合概率增益通道。若为真，
    直接体现为单注 EV 的改善；因此必须要么检出、要么给出上界。

口径
    · 只做数字型（位 = 真实的一次独立摇出）。乐透型的「位」是出球顺序，不是
      独立设备，另属 `ev/market_backtest.py` 的出球顺序检验。
    · 零假设一律用**带估计边际的独立性**：同号率的期望用 Σ p̂_x(i)·p̂_y(i)，
      而不是 1/K —— 边际本身不均匀时用 1/K 会把边际偏差算成依赖（口径伪影）。
    · 效应量门与项目其余裁判同口径：不只看 p，还要求 |z| 过门 + BH 校正。
    · 七星彩第 7 位只取 2020-10-11 起现行段（0-9 → 0-14 规则变更）。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
from scipy import stats

from credibility.randomness_audit import _QXC_CUTOFF, _bh_fdr
from data.loader import load_lottery
from data.schema import get_schema, record_zone_numbers

__all__ = [
    "DIGIT_LOTS", "digit_matrix", "same_rate_test", "pair_independence",
    "diagonal_gain", "analyze", "run_all", "render_markdown",
]

DIGIT_LOTS: Tuple[str, ...] = ("福彩3D", "排列3", "排列5", "七星彩")

# 效应量门：|z| 至少 3.0 才谈「实质」，避免大样本把 0.1% 的偏差报成发现
Z_GATE = 3.0


def digit_matrix(lottery: str,
                 records: Optional[Sequence[Any]] = None
                 ) -> Tuple[np.ndarray, List[str], List[int], int]:
    """把数字型开奖摊成 (N, d) 的取值矩阵。

    只保留「每一位都有值」的期；七星彩第 7 位按现行段过滤后，其余 6 位也跟着
    限制到同一批期上（同期比较，不能跨段混）。
    返回 (mat, 分区名, 每位的 K, N)。
    """
    if records is None:
        records = load_lottery(lottery).records
    schema = get_schema(lottery)
    names = [z.name for z in schema.zones]
    Ks = [int(z.max) - int(z.min) + 1 for z in schema.zones]
    los = [int(z.min) for z in schema.zones]
    any15 = any(K == 15 for K in Ks)

    rows: List[List[int]] = []
    for rec in sorted(records, key=lambda r: getattr(r, "期号", 0) or 0):
        if any15 and not (getattr(rec, "开奖日期", None)
                          and rec.开奖日期 >= _QXC_CUTOFF):
            continue
        vals: List[int] = []
        ok = True
        for z, lo, K in zip(schema.zones, los, Ks):
            v = record_zone_numbers(rec, z)
            if not v:
                ok = False
                break
            x = int(v[0])
            if not (lo <= x < lo + K):
                ok = False
                break
            vals.append(x - lo)
        if ok:
            rows.append(vals)
    if not rows:
        return np.zeros((0, len(names)), dtype=int), names, Ks, 0
    return np.asarray(rows, dtype=int), names, Ks, len(rows)


def same_rate_test(x: np.ndarray, y: np.ndarray, Kx: int, Ky: int) -> Dict[str, Any]:
    """同号率检验：M = #{t : x_t == y_t}。

    零假设：X ⟂ Y 且边际为实测边际 ⇒ θ0 = Σ_i p̂x(i)·p̂y(i)（只共有的取值），
    M ~ Bin(N, θ0)。这样边际不均匀不会被误算成依赖。
    """
    n = int(x.size)
    if n == 0:
        return {"N": 0, "状态": "无数据"}
    px = np.bincount(x, minlength=Kx) / n
    py = np.bincount(y, minlength=Ky) / n
    m = min(Kx, Ky)
    theta0 = float(np.dot(px[:m], py[:m]))
    obs = int(np.count_nonzero(x == y))
    rate = obs / n
    se = math.sqrt(theta0 * (1.0 - theta0) / n) if 0 < theta0 < 1 else float("nan")
    z = (rate - theta0) / se if se and math.isfinite(se) and se > 0 else float("nan")
    p = float(2.0 * stats.norm.sf(abs(z))) if math.isfinite(z) else float("nan")
    return {
        "N": n,
        "观测同号": obs,
        "同号率": rate,
        "零假设同号率": theta0,
        "相对偏差": (rate / theta0 - 1.0) if theta0 > 0 else float("nan"),
        "z": float(z),
        "p": p,
        "过效应门": bool(math.isfinite(z) and abs(z) >= Z_GATE),
    }


def pair_independence(x: np.ndarray, y: np.ndarray,
                      Kx: int, Ky: int) -> Dict[str, Any]:
    """Kx×Ky 列联表的独立性 χ²（df=(Kx-1)(Ky-1)）+ Cramér's V。"""
    n = int(x.size)
    if n == 0:
        return {"N": 0, "状态": "无数据"}
    tab = np.zeros((Kx, Ky), dtype=np.int64)
    np.add.at(tab, (x, y), 1)
    # 去掉全零行列（该取值从未出现）以免 df 虚高
    tab = tab[tab.sum(axis=1) > 0][:, tab.sum(axis=0) > 0]
    r, c = tab.shape
    if r < 2 or c < 2:
        return {"N": n, "状态": "退化表"}
    chi2, p, dof, _exp = stats.chi2_contingency(tab, correction=False)
    v = math.sqrt(chi2 / (n * (min(r, c) - 1))) if n else float("nan")
    return {
        "N": n, "χ²": float(chi2), "df": int(dof), "p": float(p),
        "Cramér_V": float(v),
        "过效应门": bool(v >= 0.05 and p <= 0.05),
        "最小期望计数": float(_exp.min()),
    }


def diagonal_gain(x: np.ndarray, y: np.ndarray, Kx: int, Ky: int,
                  alpha: float = 0.05) -> Dict[str, Any]:
    """「取同一取值」通道的组合概率增益上界（对角格）。

    对角格 (i,i) 在独立性下的期望概率 = p̂x(i)·p̂y(i)；实测 = n_ii/N。
    增益 = 实测/期望。取 max over i 的同时置信上界（Šidák，单侧）。
    """
    n = int(x.size)
    m = min(Kx, Ky)
    if n == 0:
        return {"N": 0, "状态": "无数据"}
    px = np.bincount(x, minlength=Kx) / n
    py = np.bincount(y, minlength=Ky) / n
    cnt = np.array([int(np.count_nonzero((x == i) & (y == i))) for i in range(m)],
                   dtype=float)
    base = px[:m] * py[:m]
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(base > 0, (cnt / n) / base, np.nan)
    t = float(stats.norm.ppf((1.0 - alpha) ** (1.0 / m))) if m > 1 else 1.96
    se = np.sqrt(np.maximum(base * (1.0 - base), 0.0) / n)
    with np.errstate(divide="ignore", invalid="ignore"):
        ucb = np.where(base > 0, ((cnt / n) + t * se) / base, np.nan)
    return {
        "N": n,
        "对角格数": m,
        "最大实测增益": float(np.nanmax(ratio)) if np.any(np.isfinite(ratio)) else float("nan"),
        "最大增益上界": float(np.nanmax(ucb)) if np.any(np.isfinite(ucb)) else float("nan"),
        "Šidák t*": t,
    }


def analyze(lottery: str, alpha: float = 0.05,
            records: Optional[Sequence[Any]] = None) -> Dict[str, Any]:
    mat, names, Ks, n = digit_matrix(lottery, records)
    if n == 0:
        return {"彩种": lottery, "状态": "无数据", "判决": "未评估"}
    pairs: List[Dict[str, Any]] = []
    p_same, p_chi = [], []
    for i in range(len(names)):
        for j in range(i + 1, len(names)):
            s = same_rate_test(mat[:, i], mat[:, j], Ks[i], Ks[j])
            s["位对"] = f"{names[i]}~{names[j]}"
            ch = pair_independence(mat[:, i], mat[:, j], Ks[i], Ks[j])
            dg = diagonal_gain(mat[:, i], mat[:, j], Ks[i], Ks[j], alpha=alpha)
            s.update({f"χ²_{k}" if k in ("χ²", "df", "p", "Cramér_V",
                                         "过效应门", "最小期望计数") else k: v
                      for k, v in ch.items() if k != "N"})
            s["对角最大增益"] = dg.get("最大实测增益")
            s["对角增益上界"] = dg.get("最大增益上界")
            pairs.append(s)
            p_same.append(s.get("p", float("nan")))
            p_chi.append(ch.get("p", float("nan")))

    q_same = _bh_fdr(np.asarray([p if math.isfinite(p) else 1.0 for p in p_same]))
    q_chi = _bh_fdr(np.asarray([p if math.isfinite(p) else 1.0 for p in p_chi]))
    hits: List[str] = []
    for k, pr in enumerate(pairs):
        pr["q(BH)_同号率"] = float(q_same[k])
        pr["q(BH)_独立性"] = float(q_chi[k])
        pr["实质_同号率"] = bool(pr.get("过效应门") and q_same[k] <= 0.05)
        pr["实质_独立性"] = bool(pr.get("过效应门") and q_chi[k] <= 0.05)
        if pr["实质_同号率"] or pr["实质_独立性"]:
            hits.append(pr["位对"])

    max_abs_z = max((abs(pr["z"]) for pr in pairs
                     if isinstance(pr.get("z"), float) and math.isfinite(pr["z"])),
                    default=0.0)
    max_gain_ub = max((pr["对角增益上界"] for pr in pairs
                       if isinstance(pr.get("对角增益上界"), float)
                       and math.isfinite(pr["对角增益上界"])), default=float("nan"))
    return {
        "彩种": lottery,
        "状态": "ok",
        "期数": n,
        "位对": len(pairs),
        "最大|z|": float(max_abs_z),
        "最大对角增益上界": float(max_gain_ub),
        "实质检出": hits,
        "判决": (f"⚠ 检出同期跨位依赖 [{'、'.join(hits)}]" if hits
                 else "未检出任何同期跨位依赖（已测位对全部未过效应量门 + BH）"),
        "明细": pairs,
    }


def run_all(alpha: float = 0.05,
            lots: Sequence[str] = DIGIT_LOTS) -> List[Dict[str, Any]]:
    return [analyze(nm, alpha=alpha) for nm in lots]


def render_markdown(report: Sequence[Dict[str, Any]], alpha: float = 0.05) -> str:
    L: List[str] = []
    L.append("## 同期跨位依赖（新维度：跨期相邻对之外的备择）")
    L.append("")
    L.append("| 彩种 | 期数 | 位对 | 最大\\|z\\|(同号率) | 最大对角增益上界 "
             "| 实质检出 | 判决 |")
    L.append("|---|---|---|---|---|---|---|")
    for r in report:
        if r.get("状态") != "ok":
            L.append(f"| {r['彩种']} | — | — | — | — | — | {r['判决']} |")
            continue
        L.append(f"| {r['彩种']} | {r['期数']:,} | {r['位对']} | {r['最大|z|']:.2f} "
                 f"| {r['最大对角增益上界']:.3f} | "
                 f"{'、'.join(r['实质检出']) if r['实质检出'] else '无'} "
                 f"| {r['判决']} |")
    L.append("")
    L.append(f"效应量门：|z| ≥ {Z_GATE:.1f} 且 BH q ≤ {alpha:.2f}。"
             "同号率的零假设用**实测边际**（Σ p̂x·p̂y），"
             "避免把边际不均匀误算成依赖。")
    L.append("")
    return "\n".join(L)
