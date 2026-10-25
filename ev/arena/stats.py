# -*- coding: utf-8 -*-
"""ev/arena/stats.py —— 配对统计与多重比较校正。

三条硬约束（都是 Stage 0 用事故换来的）
--------------------------------------
1. **期窗必须逐位相等**：`pair()` 先断言 `a.issues == b.issues`。
   它**没有** `min(len)` 截断字段、也没有 `截断` 输出 ——
   出现过「`paired_report` 静默 `min()` 截断」的事故（方案 §1.2 G2），
   截断会制造出「两臂在同一批期上比较」的假象。宁可抛异常。

2. **效应量与方差必须同口径**：`n*` 用传入的 `effect` 与该批期的 `sd_d` 算。
   若 effect 来自精确枚举（最新规则）而 sd 来自跨版本回放（旧规则含小奖），
   n* 会失真 —— 大乐透实测两种口径差 2.3~6 倍。调用方负责保证同口径，
   本函数只在 `effect_source` 里把它显式记下来。

3. **多重比较必须校正**：一份完整报告有 22 个分区检验，名义 5% 时
   P(至少亮一个) = 67.6%。裸 P95 / 裸 z 不算判据。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

# 双侧 α=0.05 的 z 与 80% 功效的 z
Z_ALPHA = 1.959964
Z_POWER = 0.841621


def pair(a_hit: np.ndarray, b_hit: np.ndarray, *,
         issues_a: Sequence[Any], issues_b: Sequence[Any],
         effect: Optional[float] = None,
         effect_source: str = "实测",
         name_a: str = "A", name_b: str = "B",
         metric: str = "binary") -> Dict[str, Any]:
    """两臂的配对差统计。

    a_hit / b_hit：逐期的**同口径**序列，必须同长度、同期窗。
      · `metric="binary"`：逐期是否达标（0/1）。ρ̂ 用伯努利方差 `p(1-p)`。
      · `metric="money"` ：逐期净额（元）。ρ̂ 用**样本方差**。
        ★ 金额侧不能用 `p(1-p)` —— 那会把 −400 元/期的方差算成负数或荒谬值。

    effect：可选的**外部**效应量（例：精确枚举 Δ）。给了就同时用它算 n*，
           以便与实测口径对照；两者不一致时以**保守**（n* 更大）者为准。

    ★ 金额侧有一条必须读的话：**E[单期实得] 与选哪些票恒等无关**
      （`E[中奖注数]=K·p` 的同一恒等式），所以净收益的**期望差本来就应该是 0**。
      它显著 ≠ 0 时先查实现缺陷或分摊效应，不要当成"策略赚钱"。
    """
    if list(issues_a) != list(issues_b):
        raise ValueError(
            f"期窗不一致（{name_a} {len(issues_a)} 期 vs {name_b} {len(issues_b)} 期）。"
            f"★ 不允许截断对齐 —— 回到 build_periods 统一生成期窗。")
    if metric not in ("binary", "money"):
        raise ValueError(f"metric 只支持 binary / money，收到 {metric!r}")
    ha = np.asarray(a_hit, dtype=float)
    hb = np.asarray(b_hit, dtype=float)
    n = ha.size
    if n != hb.size:
        raise ValueError(f"长度不一致：{n} vs {hb.size}")
    d = ha - hb
    ma, mb = float(ha.mean()), float(hb.mean())
    sd = float(d.std(ddof=1)) if n > 1 else float("nan")
    diff = ma - mb
    # ★ sd == 0 的两种情形必须分开，不能都退化成 nan：
    #   · diff == 0 ⇒ 两臂逐期完全相同，z 应为 0（不是 nan）；
    #   · diff != 0 ⇒ 差是恒定的，标准误为 0 ⇒ z 在数学上是 ±∞。
    #   旧实现在这里恒返回 nan，会让「两臂逐位相同」被误读成「无法判断」。
    if sd == 0.0:
        se = 0.0
        z = 0.0 if abs(diff) <= 1e-15 else math.copysign(float("inf"), diff)
        lo = hi = diff
    elif n > 1 and math.isfinite(sd):
        se = sd / math.sqrt(n)
        z = diff / se if se > 0 else float("nan")
        lo, hi = diff - Z_ALPHA * se, diff + Z_ALPHA * se
    else:
        se = float("nan")
        z = float("nan")
        lo = hi = float("nan")
    if metric == "binary":
        va, vb = ma * (1 - ma), mb * (1 - mb)
    else:
        va = float(ha.var(ddof=1)) if n > 1 else float("nan")
        vb = float(hb.var(ddof=1)) if n > 1 else float("nan")
    # var(a-b) = va + vb - 2cov ⇒ cov = (va+vb-sd²)/2；ρ = cov/(sd_a·sd_b)
    rho = ((va + vb - sd ** 2) / (2 * math.sqrt(va * vb))
           if va > 0 and vb > 0 and math.isfinite(sd) else float("nan"))

    def n_star(e: float) -> float:
        if abs(e) <= 1e-15:
            return float("inf")
        if sd == 0.0:
            return 1.0            # 零方差 ⇒ 一期就够（不是 inf：那是「效应为 0」的情形）
        if not math.isfinite(sd):
            return float("inf")
        return ((Z_ALPHA + Z_POWER) * sd / abs(e)) ** 2

    row: Dict[str, Any] = {
        "臂A": name_a, "臂B": name_b, "期数": n,
        "口径": metric, "A均值": ma, "B均值": mb, "实测Δ": diff,
        "配对差SD": sd, "配对SE": se, "配对ρ̂": rho,
        "z_实测": z, "CI95下": lo, "CI95上": hi,
        "n*_实测效应": n_star(diff),
        "n*_可用期数比": None,
        "效应量口径": effect_source,
    }
    if metric == "binary":          # 兼容既有报告字段名（金额侧没有「达标率」）
        row["A达标率"] = ma
        row["B达标率"] = mb
    if effect is not None:
        row["外部效应量"] = float(effect)
        row["n*_外部效应"] = n_star(float(effect))
        row["n*_保守"] = max(row["n*_实测效应"], row["n*_外部效应"])
        row["口径差"] = float(effect) - diff
    else:
        row["n*_保守"] = row["n*_实测效应"]

    if metric == "money" and n > 1:
        # ★ 配对级集中度：Δ 是不是被**单独一期**撑起来的
        #   （单臂的「单次大奖占总实得比」抓不到这种情形 —— 主格里 random 中一注
        #     二等只占它自己的 27%，却贡献了 Δ 的 92%。）
        i = int(np.argmax(np.abs(d)))
        d_ex = np.delete(d, i)
        sd_ex = float(d_ex.std(ddof=1)) if d_ex.size > 1 else float("nan")
        diff_ex = float(d_ex.mean()) if d_ex.size else float("nan")
        se_ex = sd_ex / math.sqrt(d_ex.size) if (d_ex.size and math.isfinite(sd_ex)) else float("nan")
        row["最大配对期"] = issues_a[i]
        row["最大配对差"] = float(d[i])
        row["Δ_剔除最大配对期"] = diff_ex
        row["z_剔除"] = (diff_ex / se_ex) if (se_ex and math.isfinite(se_ex) and se_ex > 0) else float("nan")
        # 「这一期单独贡献了 Δ 的百分之多少」（>1 表示剔除后会翻号）
        row["单期占Δ比"] = (float(d[i]) / n) / diff if abs(diff) > 1e-12 else float("nan")
    return row


def achievable(row: Dict[str, Any], available: int) -> Dict[str, Any]:
    """给 `pair()` 的结果补上「相对可用期数是否够」。"""
    ns = row.get("n*_保守")
    row["可用期数"] = int(available)
    row["n*_可用期数比"] = (ns / available) if (ns is not None and math.isfinite(ns)) else float("inf")
    row["判决"] = ("可判" if row["n*_可用期数比"] <= 1.0 else
                   ("边缘" if row["n*_可用期数比"] <= 3.0 else "不可判"))
    return row


# ---------------------------------------------------------------- 多重比较
def adjust_multiplicity(pvals: Sequence[float], *, method: str = "bh",
                        alpha: float = 0.05) -> Dict[str, Any]:
    """BH / Bonferroni 校正。返回 `{原始p, 校正后p, 阈值, 通过数, 方法}`。

    ★ 不提供「不校正」这个选项：Stage 0 里出现过「裸 P95 被当成判据」的问题，
      22 个分区在名义 5% 下的假阳概率是 67.6%。
    """
    p = np.asarray([float(x) for x in pvals], dtype=float)
    m = p.size
    if m == 0:
        return {"原始p": [], "校正后p": [], "阈值": [], "通过数": 0, "方法": method}
    order = np.argsort(p)
    ranked = p[order]
    if method == "bonferroni":
        thr = np.full(m, alpha / m)
        adj = np.minimum(p * m, 1.0)
    elif method == "bh":
        thr = alpha * (np.arange(1, m + 1) / m)
        adj_sorted = np.minimum.accumulate((ranked * m / np.arange(1, m + 1))[::-1])[::-1]
        adj = np.empty(m)
        adj[order] = np.minimum(adj_sorted, 1.0)
    else:
        raise ValueError(f"未知方法 {method!r}（只支持 bh / bonferroni）")
    passed = int((p[order] <= thr).sum())
    return {"原始p": p.tolist(), "校正后p": adj.tolist(),
            "阈值": thr.tolist(), "通过数": passed, "方法": method,
            "检验数": m, "名义α": alpha}


def z_to_p_two_sided(z: float) -> float:
    """z → 双侧 p（用 erfc，不依赖 scipy）。"""
    if not math.isfinite(z):
        return float("nan")
    return math.erfc(abs(z) / math.sqrt(2.0))
