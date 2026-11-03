# -*- coding: utf-8 -*-
"""精确覆盖优化「值不值得接生产」的**决策规则**（先于数据写下）。

## 为什么单独一个模块

这套流程里最容易出错的一步不是算法，是**看完曲线再解释**。
所以判据要在跑数据之前落成代码常量：跑完只允许调用 `verdict(rows)`，
不许回头改阈值。改阈值可以，但必须同时改这里的 `判据版本` 并在提交说明里写明为什么。

`config/preregistration.json` 那套是给 arena 正式检验用的（要改哈希、要过测试），
本模块是**轻量决策规则**：回答「这个方向要不要继续投入」，不是「这个策略是否显著」。

## 判据（写在 2026-11-03，尚未看到 K 曲线）

对每个 K 档，比较三条臂的**精确并集概率**：

    Δ(K) = P_精确贪心(K) − mean(真随机(K) 的多个种子)
    z(K) = Δ(K) / sd(真随机(K))

高档 K 取 `0.7 × 候选池规模`。**不是**取生产的 500：
当期候选池就是 pending 那一批（500 注），K=池 时「选号」退化成「全选」，
优化器没有任何自由度 —— 那种数不能用来说明算法好坏。

三种结局：

| 判决 | 条件（按顺序判，命中即停） | 含义 |
| --- | --- | --- |
| **C 收益不足** | 所有 K 档 `Δ < 0.5pp` | 这个方向的收益撑不起复杂度，结案 |
| **B 瓶颈在约束** | 高档 `Δ` 比 `K≥200` 区间的最大值回落超过 `0.05pp` | 增益被 cap/池容量吃掉，瓶颈不是目标函数 |
| **A 值得接生产** | 高档 `Δ ≥ 1.5pp` **且** `z ≥ 2` | 目标函数选错确实是主要损耗，接生产候选池 |
| **边缘** | 以上都不满足 | 有方向但不够，需补测（更大池 / 更多档） |
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

判据版本 = "2026-11-03-A"
# ---- 阈值（写死于此处，跑完数据不许回来改）----
THRESH_C_PP = 0.50          # 低于它 = 收益不足（百分点，绝对值）
THRESH_A_PP = 1.50          # 高于它才谈得上值得接生产
THRESH_Z = 2.0              # 相对随机种子的偏离至少要 2 个标准差
FALLBACK_TOL_PP = 0.05      # 判「回落」的容差，避免把噪声当拐点
HIGH_K_RATIO = 0.70         # 高档 K = 0.7 × 池（再高就快退化成全选）


def high_k(pool_size: int) -> int:
    return max(1, int(round(HIGH_K_RATIO * int(pool_size))))


def verdict(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """按预先写死的阈值给出 A/B/C/边缘。`rows` 每项至少含 K / Δ_pp / z。"""
    rs = [r for r in rows if r.get("Δ_pp") is not None and r.get("z") is not None]
    if not rs:
        return {"判决": "无数据", "判据版本": 判据版本, "原因": "没有可比的行"}

    k_high = max(int(r["K"]) for r in rs)
    d_high = next(float(r["Δ_pp"]) for r in rs if int(r["K"]) == k_high)
    z_high = next(float(r["z"]) for r in rs if int(r["K"]) == k_high)
    d_max = max(float(r["Δ_pp"]) for r in rs)
    k_at_max = max(int(r["K"]) for r in rs if float(r["Δ_pp"]) == d_max)
    # 「中后段」= K ≥ 200 的档（若都不够 200，就退化为全部档）
    mid = [float(r["Δ_pp"]) for r in rs if int(r["K"]) >= 200] or \
          [float(r["Δ_pp"]) for r in rs]
    d_mid_max = max(mid)

    if d_max < THRESH_C_PP:
        return {"判决": "C 收益不足", "判据版本": 判据版本,
                "原因": f"全档最大 Δ={d_max:.3f}pp < {THRESH_C_PP}pp",
                "高档K": k_high, "高档Δ_pp": d_high}

    if d_high < d_mid_max - FALLBACK_TOL_PP:
        return {"判决": "B 瓶颈在约束", "判据版本": 判据版本,
                "原因": (f"高档 K={k_high} 的 Δ={d_high:.3f}pp 比中后段峰值 "
                         f"{d_mid_max:.3f}pp（出现在 K={k_at_max}）回落超过 "
                         f"{FALLBACK_TOL_PP}pp ⇒ 增益被 cap/池容量吃掉"),
                "高档K": k_high, "高档Δ_pp": d_high, "峰值K": k_at_max}

    if d_high >= THRESH_A_PP and z_high >= THRESH_Z:
        return {"判决": "A 值得接生产", "判据版本": 判据版本,
                "原因": f"高档 K={k_high}: Δ={d_high:.3f}pp ≥ {THRESH_A_PP}pp，"
                        f"z={z_high:.2f} ≥ {THRESH_Z}",
                "高档K": k_high, "高档Δ_pp": d_high}

    return {"判决": "边缘", "判据版本": 判据版本,
            "原因": (f"高档 K={k_high}: Δ={d_high:.3f}pp（未达 {THRESH_A_PP}pp "
                     f"或 z={z_high:.2f} 未达 {THRESH_Z}），但也不是收益不足"),
            "高档K": k_high, "高档Δ_pp": d_high}


def describe_pool_ratio(pool_size: int, k: int) -> str:
    """候选池与 K 的关系说明（K/池 越高，选号自由度越低）。"""
    ratio = k / float(pool_size) if pool_size else 0.0
    if ratio >= 0.999:
        return "退化：K = 池规模，等价于「全选」，优化器无自由度"
    if ratio >= 0.8:
        return f"K/池 = {ratio:.0%}，自由度很低，结论参考价值有限"
    return f"K/池 = {ratio:.0%}"
