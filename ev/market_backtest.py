# -*- coding: utf-8 -*-
"""ev/market_backtest.py —— Phase 2 市场假设研究的信号检验（研究工具，不接生产）

目标（用户给定）：用 market.db 新增的市场维度数据——出球顺序 / 头奖分省 / 分省月销量——
检验「这些新数据是否携带可 exploited 的信号」。诚实结论是本模块的核心产出。

检验清单：
  A. 出球顺序均匀性（χ²）：每个出球位置上的号码是否偏离均匀？
     这是「摇奖机/球有物理偏差」能看到的、排序后号码里看不到的特征。
  B. 出球顺序序列相关（lag-1 Pearson）：相邻两期同一位置是否相关（一阶依赖）？
  C. 出球顺序滚动窗口 OOS 预测：用近窗众数预测下一期同位置，命中率是否显著超过
     理论 1/range？这是「出球顺序是否可被预测」最直接的公平检验。
  D. 头奖分省 vs 分省销量（χ² 拟合优度 + Spearman）：各省头奖注数是否与销量成正比？
     若某省长期显著高于其销量占比，才谈得上「被安排中奖」；否则只是「卖得多中得多」。
  E. 分省销量描述统计：趋势 / 头部尾部省份 / 缺失月份——纯描述，明确不构成号码预测。

死数据红线（结论硬约束，详见 ev/market_features.py）：
  - 无全国每注购买量 ⇒ 「最少购买组合」不可直接求得；
  - 无每彩种每期分地区下注量 ⇒ 无法做「某期某省被安排中奖」的细检验。
因此本模块对「开奖是否被人为主导」只能给出**间接、粗粒度**的否定证据，不得越界声称可预测。

统计实现：自包含（不依赖 scipy）。χ² 上尾概率走正则化上不完全 Gamma；
二项精确双侧 p 走小概率法；Spearman p 走学生 t（正则化不完全 Beta）。
"""
from __future__ import annotations

import argparse
import json
import math
from typing import Any, Dict, List, Optional, Tuple

from ev import market_features as mf


# ================================================================ 统计基础

def _gser(a: float, x: float) -> float:
    gln = math.lgamma(a)
    ap = a
    total = 1.0 / a
    delta = total
    for _ in range(1000):
        ap += 1.0
        delta *= x / ap
        total += delta
        if abs(delta) < abs(total) * 1e-15:
            break
    return total * math.exp(-x + a * math.log(x) - gln)


def _gcf(a: float, x: float) -> float:
    gln = math.lgamma(a)
    FPMIN = 1e-300
    b = x + 1.0 - a
    c = 1.0 / FPMIN
    d = 1.0 / b
    h = d
    for i in range(1, 1000):
        an = -i * (i - a)
        b += 2.0
        d = an * d + b
        if abs(d) < FPMIN:
            d = FPMIN
        c = b + an / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return math.exp(-x + a * math.log(x) - gln) * h


def chi2_sf(x: float, df: int) -> float:
    """χ² 上尾概率 P(X² > x | df)。"""
    if x <= 0:
        return 1.0
    a = 0.5 * df
    xx = 0.5 * x
    if xx < a + 1.0:
        return 1.0 - _gser(a, xx)
    return _gcf(a, xx)


def binom_two_sided(k: int, n: int, p: float) -> float:
    """二项精确双侧 p（小概率法：把概率 ≤ P(k) 的所有 k' 累加）。

    用对数空间算 pmf，避免 math.comb(n,i)（超大整数）× float 溢出。
    当观测恰在众数处时，所有结果的 pmf ≤ P(k) ⇒ 累加=1.0，正确「不拒绝」。
    """
    if n <= 0:
        return 1.0
    p = min(max(p, 0.0), 1.0)
    if p == 0.0:
        return 1.0 if k == 0 else 0.0
    if p == 1.0:
        return 1.0 if k == n else 0.0
    logC = math.lgamma(n + 1)

    def logpmf(i: int) -> float:
        if i < 0 or i > n:
            return float("-inf")
        return (logC - math.lgamma(i + 1) - math.lgamma(n - i + 1)
                + i * math.log(p) + (n - i) * math.log(1.0 - p))

    log_obs = logpmf(k)
    total = 0.0
    for i in range(0, n + 1):
        lp = logpmf(i)
        if lp <= log_obs + 1e-12:
            total += math.exp(lp)
    return min(1.0, total)


def _betacf(a: float, b: float, x: float) -> float:
    FPMIN = 1e-300
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < FPMIN:
        d = FPMIN
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < FPMIN:
            d = FPMIN
        c = 1.0 + aa / c
        if abs(c) < FPMIN:
            c = FPMIN
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 1e-15:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    if x < (a + 1.0) / (a + b + 2.0):
        return math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x)) * _betacf(a, b, x)
    return 1.0 - math.exp(lbeta + b * math.log(1.0 - x) + a * math.log(x)) * _betacf(b, a, 1.0 - x)


def student_t_sf(t: float, df: int) -> float:
    """学生 t 上尾概率（双侧用 2×）。"""
    if df <= 0:
        return float("nan")
    x = df / (df + t * t)
    return 0.5 * _betai(df / 2.0, 0.5, x)


def spearman_rho(xs: List[float], ys: List[float]) -> Tuple[float, float]:
    """Spearman 秩相关 + 双侧 p（经 t 近似）。"""
    n = len(xs)
    if n < 3:
        return float("nan"), float("nan")
    rx = _rank(xs)
    ry = _rank(ys)
    mx = sum(rx) / n
    my = sum(ry) / n
    sxy = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    sxx = sum((rx[i] - mx) ** 2 for i in range(n))
    syy = sum((ry[i] - my) ** 2 for i in range(n))
    if sxx == 0 or syy == 0:
        return float("nan"), float("nan")
    r = sxy / math.sqrt(sxx * syy)
    t = r * math.sqrt((n - 2) / max(1e-12, 1.0 - r * r))
    p = 2.0 * student_t_sf(abs(t), n - 2)
    return r, p


def _rank(vals: List[float]) -> List[float]:
    order = sorted(range(len(vals)), key=lambda i: vals[i])
    ranks = [0.0] * len(vals)
    i = 0
    while i < len(vals):
        j = i
        while j + 1 < len(vals) and vals[order[j + 1]] == vals[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _mode(values: List[int]) -> int:
    counts: Dict[int, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return max(counts.items(), key=lambda kv: (kv[1], -kv[0]))[0]


# ================================================================ 数据抽取

def _sequences(code: str, limit: Optional[int] = None) -> List[List[int]]:
    spec = mf.LOT_SPEC.get(code)
    if not spec:
        return []
    pos = int(spec["positions"])
    seqs = [s for (_i, _d, s) in mf.load_seq_front(code, limit=limit) if len(s) == pos]
    return seqs


def _position_values(code: str, limit: Optional[int] = None) -> List[List[int]]:
    seqs = _sequences(code, limit=limit)
    if not seqs:
        return []
    pos = len(seqs[0])
    return [[s[p] for s in seqs] for p in range(pos)]


def _pos_range(code: str, p: int) -> int:
    spec = mf.LOT_SPEC[code]
    if "per_pos_hi" in spec:
        hi = int(spec["per_pos_hi"][p])  # type: ignore[index]
        lo = int(spec["lo"])
        return hi - lo + 1
    return int(spec["hi"]) - int(spec["lo"]) + 1


# 出球顺序数据质量闸门：若源数据是整数序排列（非真实摇出顺序），则 A/B/C 检验无意义。
_DRAW_ORDER_CACHE: Dict[str, bool] = {}


def _is_draw_order(code: str, limit: int = 500) -> bool:
    """抽样判断 seq_front 是否为真实摇出顺序。

    真实摇出顺序不应系统性升序；若抽样中绝大多数行与其升序排列相等，则源数据为
    整数序排列（如七乐彩中彩网源），此时「出球顺序」检验不适用，必须排除。
    """
    if code in _DRAW_ORDER_CACHE:
        return _DRAW_ORDER_CACHE[code]
    seqs = [s for (_i, _d, s) in mf.load_seq_front(code, limit=limit)]
    if not seqs:
        _DRAW_ORDER_CACHE[code] = False
        return False
    frac = sum(1 for s in seqs if s == sorted(s)) / len(seqs)
    res = frac < 0.5
    _DRAW_ORDER_CACHE[code] = res
    return res


def _not_draw_order_note(code: str, n: int) -> Dict[str, Any]:
    return {
        "彩种": mf.CODE_TO_NAME.get(code, code), "代码": code, "可用期数": n,
        "结论": ("源数据为整数序排列（非真实摇出顺序），出球顺序检验不适用"
                 if n else "无出球顺序数据"),
    }


# ================================================================ A. 均匀性

def test_seq_front_uniformity(code: str, limit: Optional[int] = None) -> Dict[str, Any]:
    """每个出球位置 + 汇总的 χ² 均匀性检验。

    多重比较校正：一个彩种有多个位置各自做 χ²，若不做校正，期望会有 ~α·n 个假阳性。
    结论门槛用 Bonferroni（α/n_pos）；汇总为单一检验，沿用 α=0.05。
    """
    cols = _position_values(code, limit=limit)
    if not cols:
        return {"彩种": code, "可用期数": 0, "结论": "无出球顺序数据", "位置": []}
    if not _is_draw_order(code):
        return _not_draw_order_note(code, len(cols[0]))
    n = len(cols[0])
    n_pos = len(cols)
    positions = []
    pooled_obs: Dict[int, int] = {}
    for p, vals in enumerate(cols):
        rng = _pos_range(code, p)
        obs = [0] * rng
        lo = int(mf.LOT_SPEC[code].get("lo", 0))
        for v in vals:
            idx = v - lo
            if 0 <= idx < rng:
                obs[idx] += 1
                pooled_obs[v] = pooled_obs.get(v, 0) + 1
        exp = n / rng
        chi2 = sum((o - exp) ** 2 / exp for o in obs)
        df = rng - 1
        pval = chi2_sf(chi2, df)
        positions.append({
            "位置": p + 1, "号码数": rng, "期数": n,
            "χ²": round(chi2, 3), "df": df,
            "p值": round(pval, 4),
            "偏离均匀": bool(pval < 0.05),
            "均值": round(sum(vals) / len(vals), 3),
        })
    # 汇总：把所有位置的所有值混在一起（仅当各位置同分布时才有意义；乐透型各位置同为 1..R）
    rng_pool = _pos_range(code, 0)
    same_range = all(_pos_range(code, p) == rng_pool for p in range(len(cols)))
    pooled = None
    if same_range:
        obs = [0] * rng_pool
        lo = int(mf.LOT_SPEC[code].get("lo", 0))
        for v, c in pooled_obs.items():
            idx = v - lo
            if 0 <= idx < rng_pool:
                obs[idx] += c
        exp = sum(obs) / rng_pool
        chi2 = sum((o - exp) ** 2 / exp for o in obs)
        df = rng_pool - 1
        pooled = {"χ²": round(chi2, 3), "df": df, "p值": round(chi2_sf(chi2, df), 4),
                  "偏离均匀": bool(chi2_sf(chi2, df) < 0.05)}
    bonf = 0.05 / n_pos if n_pos else 0.05
    any_dev = any(pos["p值"] < bonf for pos in positions) or (
        pooled is not None and pooled["p值"] < 0.05)
    return {
        "彩种": mf.CODE_TO_NAME.get(code, code), "代码": code,
        "可用期数": n, "位置数": n_pos, "Bonferroni阈值": round(bonf, 4),
        "位置": positions, "汇总": pooled,
        "结论": ("在 α=0.05 并经过位置数 Bonferroni 校正后，各位置均与均匀兼容，"
                 "无明显物理偏差"
                 if not any_dev else "存在位置偏离均匀（通过校正），需进一步核查"),
    }


# ================================================================ B. 序列相关

def test_seq_front_serial(code: str, limit: Optional[int] = None) -> Dict[str, Any]:
    """lag-1 序列相关（Pearson）+ 双侧 p。大样本下 |r|>~0.04 即「统计显著」但未必有实际意义，
    故同时给出「实操阈值 |r|>0.10」标注——只有超过该量级才算有可用的一阶依赖。"""
    cols = _position_values(code, limit=limit)
    if not cols:
        return {"彩种": code, "可用期数": 0, "结论": "无出球顺序数据", "位置": []}
    if not _is_draw_order(code):
        return _not_draw_order_note(code, len(cols[0]))
    n = len(cols[0])
    positions = []
    max_abs_r = 0.0
    for p, vals in enumerate(cols):
        xs = vals[:-1]
        ys = vals[1:]
        m = len(xs)
        mx = sum(xs) / m
        my = sum(ys) / m
        sxy = sum((xs[i] - mx) * (ys[i] - my) for i in range(m))
        sxx = sum((xs[i] - mx) ** 2 for i in range(m))
        syy = sum((ys[i] - my) ** 2 for i in range(m))
        r = sxy / math.sqrt(sxx * syy) if sxx and syy else 0.0
        t = r * math.sqrt((m - 2) / max(1e-12, 1.0 - r * r))
        pval = 2.0 * student_t_sf(abs(t), m - 2) if m > 2 else float("nan")
        max_abs_r = max(max_abs_r, abs(r))
        positions.append({
            "位置": p + 1, "样本": m, "lag-1_r": round(r, 4),
            "p值": round(pval, 4) if pval == pval else None,
            "实操显著(|r|>0.10)": bool(abs(r) > 0.10),
        })
    return {
        "彩种": mf.CODE_TO_NAME.get(code, code), "代码": code, "可用期数": n,
        "位置": positions,
        "结论": ("各位置 lag-1 相关均在噪声量级（|r|≤0.10），无可用一阶依赖"
                 if max_abs_r <= 0.10 else "存在位置 lag-1 相关超过实操阈值，需核查"),
    }


# ================================================================ C. 滚动预测

def walk_forward_order_predict(code: str, window: int = 200,
                               limit: Optional[int] = None) -> Dict[str, Any]:
    """滚动窗口 OOS：用近 window 期同位置众数预测下一期；命中率 vs 理论 1/range。

    这是「出球顺序是否可被预测」最直接的公平检验——若命中率显著超过 1/range，
    则顺序存在可利用的非随机结构；否则为公平随机。预测器用「近窗众数」（自适应），
    对照基准是理论均匀命中率 1/range（不依赖任何历史信息）。
    """
    cols = _position_values(code, limit=limit)
    if not cols:
        return {"彩种": code, "可用期数": 0, "结论": "无出球顺序数据"}
    if not _is_draw_order(code):
        return _not_draw_order_note(code, len(cols[0]))
    n = len(cols[0])
    total_pairs = 0
    model_hits = 0
    per_pos = []
    for p, vals in enumerate(cols):
        rng = _pos_range(code, p)
        theo = 1.0 / rng
        if n <= window:
            continue
        mh = 0
        for t in range(window, n):
            w = vals[t - window:t]
            pred = _mode(w)
            if pred == vals[t]:
                mh += 1
            total_pairs += 1
        model_hits += mh
        per_pos.append({
            "位置": p + 1, "range": rng, "理论命中率": round(theo, 4),
            "自适应命中率": round(mh / (n - window), 4),
        })
    if total_pairs == 0:
        return {"彩种": mf.CODE_TO_NAME.get(code, code), "结论": "样本不足（≤窗口）"}
    theo_mean = sum(1.0 / _pos_range(code, p) for p in range(len(cols))) / len(cols)
    p_model = binom_two_sided(model_hits, total_pairs, theo_mean)
    return {
        "彩种": mf.CODE_TO_NAME.get(code, code), "代码": code,
        "窗口": window, "测试对数": total_pairs,
        "理论命中率(均值)": round(theo_mean, 4),
        "自适应总命中率": round(model_hits / total_pairs, 4),
        "自适应_vs_理论_p": round(p_model, 4),
        "位置明细": per_pos,
        "结论": ("命中率与理论 1/range 在统计上无差异 ⇒ 出球顺序无可利用的预测结构（公平随机）"
                 if p_model >= 0.05 else "自适应命中率显著高于理论，存在非随机结构，需复核"),
    }


# ================================================================ D. 分省头奖 vs 销量

def province_jackpot_vs_sales(code: str, agency: str = "福彩") -> Dict[str, Any]:
    """各省头奖注数是否与销量相关（Spearman 为主）+ χ² 拟合优度（辅助）。

    ★关键混杂：region_sales 是「全省福彩/体彩总销量」，没有分彩种维度，也非该彩种销量；
    因此 χ² 拟合优度（检验头奖是否严格正比于总销量）必然受混杂影响而偏大，不能直接读作
    「被安排中奖」证据。真正的信息在 Spearman：若头奖注数与销量强正相关（卖得多中得多），
    则与公平随机一致；rank 关联才是去除量级差异后的稳健信号。

    仅 ssq/qlc/kl8 有头奖分省数据。
    """
    if code not in ("ssq", "qlc", "kl8"):
        return {"彩种": code, "结论": "无头奖分省数据（仅 ssq/qlc/kl8）"}
    jt = mf.province_jackpot_totals(code)
    st = mf.province_sales_totals(agency)
    regions = [r for r in jt if r in st and st[r] > 0]
    if len(regions) < 3:
        return {"彩种": code, "结论": "有效省份过少"}
    obs = [jt[r] for r in regions]
    sales = [st[r] for r in regions]
    total_j = sum(obs)
    total_s = sum(sales)
    exp = [total_j * s / total_s for s in sales]
    chi2 = sum((o - e) ** 2 / e for o, e in zip(obs, exp))
    df = len(regions) - 1
    pval = chi2_sf(chi2, df)
    rho, prho = spearman_rho([float(s) for s in sales], [float(o) for o in obs])
    rho = None if rho != rho else rho
    prho = None if prho != prho else prho
    strong = bool(rho is not None and rho > 0.7 and (prho is not None and prho < 0.05))
    return {
        "彩种": mf.CODE_TO_NAME.get(code, code), "agency": agency,
        "有效省份数": len(regions), "总头奖注数": total_j,
        "χ²拟合优度(辅助)": round(chi2, 3), "df": df, "χ²_p值": round(pval, 4),
        "Spearman_rho": round(rho, 4) if rho is not None else None,
        "Spearman_p": round(prho, 4) if prho is not None else None,
        "结论": ("头奖注数与全省总销量呈强正相关（Spearman ρ≈%.2f），与该省卖得多则中得多一致；"
                 "χ² 拟合优度的偏差主要来自 region_sales 为总销量（无分彩种维度）的混杂，"
                 "非『被安排中奖』证据。省级汇总粒度下无直接反例，但亦无法做更细检验。"
                 % (rho if rho is not None else float('nan'))) if strong else
                ("头奖注数与销量正相关（Spearman ρ≈%.2f），与该省卖得多则中得多一致；"
                 "受总销量混杂限制，只能给弱证据，无力支持亦无法排除『特定地区被安排中奖』。"
                 % (rho if rho is not None else float('nan'))),
        "注": "region_sales 无分彩种维度；库内亦无每期分地区下注量，最细只能做到省级汇总。",
    }


# ================================================================ E. 分省销量描述

def region_sales_summary(agency: str = "福彩") -> Dict[str, Any]:
    rows = mf.load_region_sales(agency)
    if not rows:
        return {"agency": agency, "结论": "无销量数据"}
    yms = sorted({r[0] for r in rows})
    totals = mf.province_sales_totals(agency)
    ranked = sorted(totals.items(), key=lambda kv: kv[1], reverse=True)
    missing = mf.region_sales_missing_months(agency)
    # 最新月 vs 首月全国合计增速
    def nat(ym: str) -> float:
        return sum(r[2] for r in rows if r[0] == ym)
    first_nat = nat(yms[0])
    last_nat = nat(yms[-1])
    growth = (last_nat / first_nat - 1.0) if first_nat else None
    return {
        "agency": agency,
        "月份数": len(yms), "起": yms[0], "止": yms[-1],
        "缺失月份": missing,
        "省份数": len(totals),
        "全国首月合计(亿元)": round(first_nat / 10000.0, 1),
        "全国末月合计(亿元)": round(last_nat / 10000.0, 1),
        "累计增速": (round(growth * 100, 1) if growth is not None else None),
        "头部5省": [(r, round(v / 10000.0, 1)) for r, v in ranked[:5]],
        "尾部5省": [(r, round(v / 10000.0, 1)) for r, v in ranked[-5:]],
        "结论": ("分省销量为描述性市场背景（趋势/地区差异），与开奖号码无因果/预测关系；"
                 "本模块不据此做任何号码预测"),
    }


# ================================================================ 聚合 + CLI

def run_lottery(code: str, window: int = 200,
                limit: Optional[int] = None) -> Dict[str, Any]:
    agency = "福彩" if code in ("ssq", "qlc", "kl8") else "体彩"
    return {
        "彩种": mf.CODE_TO_NAME.get(code, code), "代码": code,
        "均匀性": test_seq_front_uniformity(code, limit=limit),
        "序列相关": test_seq_front_serial(code, limit=limit),
        "滚动预测": walk_forward_order_predict(code, window=window, limit=limit),
        "分省头奖vs销量": province_jackpot_vs_sales(code, agency),
    }


def run_all(window: int = 200, limit: Optional[int] = None) -> Dict[str, Any]:
    seq_codes = [c for c in mf.LOT_SPEC if c in ("ssq", "dlt", "pl5", "sd", "qxc", "qlc")]
    out = {
        "说明": ("Phase 2 市场假设研究：出球顺序/头奖分省/分省销量是否携带可 exploited 信号。"
                 "死数据红线：无全国每注购买量、无每期分地区下注量 ⇒ 「最少购买组合」不可直接求得。"),
        "出球顺序彩种": seq_codes,
        "分省销量描述_福彩": region_sales_summary("福彩"),
        "分省销量描述_体彩": region_sales_summary("体彩"),
        "彩种结果": [run_lottery(c, window=window, limit=limit) for c in seq_codes],
    }
    return out


def _print_report(rep: Dict[str, Any]) -> None:
    print("=" * 70)
    print("Phase 2 市场假设研究 —— 信号检验总报告")
    print("=" * 70)
    print(rep["说明"])
    print()
    for lot in rep["彩种结果"]:
        print(f"### {lot['彩种']} ({lot['代码']})")
        u = lot["均匀性"]
        print(f"  [A 均匀性] 可用期数={u.get('可用期数')} 结论: {u.get('结论')}")
        for pos in u.get("位置", []):
            print(f"      位置{pos['位置']} χ²={pos['χ²']} df={pos['df']} "
                  f"p={pos['p值']} 均值={pos['均值']}")
        if u.get("汇总"):
            s = u["汇总"]
            print(f"      汇总 χ²={s['χ²']} df={s['df']} p={s['p值']}")
        srl = lot["序列相关"]
        print(f"  [B 序列相关] {srl.get('结论')}")
        wf = lot["滚动预测"]
        if wf.get("理论命中率(均值)") is not None:
            print(f"  [C 滚动预测] 窗口={wf.get('窗口')} 测试对数={wf.get('测试对数')} "
                  f"理论={wf.get('理论命中率(均值)')} "
                  f"自适应={wf.get('自适应总命中率')}(p={wf.get('自适应_vs_理论_p')})")
        else:
            print(f"  [C 滚动预测] {wf.get('结论')}")
        print(f"      结论: {wf.get('结论')}")
        pv = lot["分省头奖vs销量"]
        if "χ²拟合优度(辅助)" in pv:
            print(f"  [D 头奖vs销量] χ²={pv['χ²拟合优度(辅助)']} df={pv['df']} "
                  f"χ²p={pv['χ²_p值']} Spearman={pv.get('Spearman_rho')}"
                  f"(p={pv.get('Spearman_p')})")
            print(f"      结论: {pv['结论']}")
        else:
            print(f"  [D 头奖vs销量] {pv.get('结论')}")
        print()
    for key in ("分省销量描述_福彩", "分省销量描述_体彩"):
        rs = rep[key]
        print(f"### {key}  缺失月份数={len(rs.get('缺失月份') or [])} "
              f"增速={rs.get('累计增速')}% 头部={rs.get('头部5省')}")
        print(f"      {rs.get('结论')}")
    print("=" * 70)
    print("总体诚实结论：现有市场数据（出球顺序/头奖分省/分省销量）在可用粒度下均"
          "未呈现可 exploited 的预测信号；且关键购买量数据缺失，原始「最少购买组合」"
          "假设仍**不可直接验证**。")


def main() -> None:
    ap = argparse.ArgumentParser(description="Phase 2 市场假设信号检验")
    ap.add_argument("codes", nargs="*", help="彩种代码，默认全部有出球顺序的彩种")
    ap.add_argument("--window", type=int, default=200)
    ap.add_argument("--limit", type=int, default=None, help="每彩种最多取最近 N 期")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而非文本报告")
    args = ap.parse_args()
    if args.codes:
        seq_codes = [c for c in args.codes if c in mf.LOT_SPEC]
        rep = {
            "说明": "Phase 2 指定彩种信号检验",
            "分省销量描述_福彩": region_sales_summary("福彩"),
            "分省销量描述_体彩": region_sales_summary("体彩"),
            "彩种结果": [run_lottery(c, window=args.window, limit=args.limit)
                        for c in seq_codes],
        }
    else:
        rep = run_all(window=args.window, limit=args.limit)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_report(rep)


if __name__ == "__main__":
    main()
