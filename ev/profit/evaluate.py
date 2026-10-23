# -*- coding: utf-8 -*-
"""ev/profit/evaluate.py —— 精确四指标内核（P≥1 / 中奖注数 / 净EV / 净盈利概率）。

为什么可以精确算
----------------
奖级只取决于 (外层命中数 h, 内层类别 j)。给定结局 ρ 与内层类别 j 后，
「整组 S 里有多少注中奖、拿多少钱」都是**确定值**，所以中奖注数的**分布**也是
精确的（对 (ρ,j) 的混合），不需要正态/泊松近似。

四条指标必须分开报，不许互相推导。双色球 K=200、κ=0 的实测：
P(≥1)≈0.999999、E[中奖注数]=13.4、净EV≈−208 元/期、P(净盈利)≈0.006
——「几乎必中」与「几乎必亏」可以同时成立。
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

import numpy as np

from ev.payout import grade_probabilities, pool_share_expected_batch
from ev.profit import outcomes as O
from ev.profit import payouts as P
from ev.profit import tax as TX
from ev.profit import tilt as T
from ev.profit.rules import rules_for


def _outer_weights(lottery: str, kappa: float) -> np.ndarray:
    """外层权重（已归一）。κ=0 或倾斜不可用 ⇒ 均匀。"""
    M = len(O.outer_universe(lottery))
    if kappa <= 0:
        return np.full(M, 1.0 / M)
    tl = T.outer_tilt(lottery)
    if tl is None:
        return np.full(M, 1.0 / M)
    return T.weights(tl, kappa)


def _float_pay_matrix(lottery: str, ctx: P.Ctx, M: int) -> Dict[str, np.ndarray]:
    """浮动奖级 → 逐结局 ρ 的单注期望实得（已含封顶与撞号分摊）。

    ★ 性能关键：μ(ρ) 只由**少数几个不同的分数**决定（1.1M 个组合只有 9 维粗特征，
      分数 bit 级并列）。所以先对 μ 取唯一值、只在唯一值上算 E[min(cap,池/(1+X))]，
      再映射回 M —— 否则要逐 110 万行调 scipy，单期就能跑掉一分多钟。
    """
    r = ctx.rules          # ★ 规则随期次版本变化，必须用 ctx 里的那份
    tl = T.outer_tilt(lottery)
    raw = (tl.scores + tl.offset) if tl is not None else None
    gprobs = grade_probabilities(lottery, version=r.rule_version)
    out: Dict[str, np.ndarray] = {}
    for g, gr in r.grades.items():
        if gr.kind != "floating":
            continue
        pool = ctx.payout_basis(g)
        if pool is None or pool <= 0:
            out[g] = np.zeros(M)
            continue
        cap = float(gr.cap) if gr.cap else P.CAP_DEFAULT
        p_g = gprobs.get(g, 0.0)
        if raw is None:
            mu = np.full(M, max(ctx.total_tickets * p_g, 0.0))
        else:
            # 同号期望（不含自己）：N × p_g × exp(raw)
            mu = np.maximum(ctx.total_tickets * p_g * np.exp(raw), 0.0)
        uniq, inv = np.unique(mu, return_inverse=True)
        if len(uniq) > 3000:
            # ★ 降本：E[min(cap, 池/(1+X))] 对 μ 是光滑函数，而 μ 有 20 万+ 个不同取值
            #   （粗特征导致分数几乎两两不同）。对数分箱到 2000 个网格点，实测误差 <0.1%，
            #   但把逐行 scipy 调用从 20 万降到 2000（单期 18s → 1s 级）。
            pos = uniq[uniq > 0]
            lo = float(pos.min()) if len(pos) else 1e-12
            edges = np.geomspace(max(lo, 1e-12), float(uniq.max()) + 1e-12, 2001)
            centers = np.sqrt(edges[:-1] * edges[1:])
            pay_c = pool_share_expected_batch(float(pool), centers, cap)
            idx = np.clip(np.searchsorted(edges, mu, side="right") - 1, 0, len(centers) - 1)
            out[g] = pay_c[idx]
        else:
            pay_u = pool_share_expected_batch(float(pool), uniq, cap)
            out[g] = pay_u[inv]
    return out


def exact_metrics(lottery: str, tickets: Sequence[dict], ctx: P.Ctx,
                  kappa: float = 0.0, want_dist: bool = True,
                  weights: Optional[np.ndarray] = None,
                  tax_basis: str = TX.BASIS_ISSUE,
                  min_grade: Optional[str] = None) -> Dict[str, Any]:
    """整组 S 的四指标（单期，精确）。

    weights：可注入已算好的外层权重（回测按块缓存用），避免重复建权重。
    min_grade：把「中奖」收窄为「达到 ≥min_grade 的奖级」（如 "四等"）。
      · None（默认）⇒ 行为与旧实现**逐位一致**（所有中奖格都算）。
      · 非 None ⇒ `ind` 只置「排名 ≤ min_grade」的格，于是
        `P至少中一注` / `中奖注数期望` / `中奖注数分布` 都变成**达标**口径；
        而 `税前期望` / `期望税` / `净EV` 仍是**全部奖金**（钱不打折）。
      ⇒ 两者合起来才能回答「提高达标率值不值」——只看前者会自欺。
    """
    r = ctx.rules          # ★ 同上：按上下文版本取规则
    if not tickets:
        raise ValueError("候选集为空")
    K = len(tickets)

    H = O.hit_histogram(lottery, tickets)          # (M, h_max+1)，每行和 == K
    M, ncols = H.shape
    h_max = ncols - 1
    w = weights if weights is not None else _outer_weights(lottery, kappa)
    if len(w) != M:
        raise ValueError(f"权重长度 {len(w)} 与结局数 {M} 不一致")

    p_j = O.inner_class_prob(r)                     # 内层类别边际概率
    j_max = len(p_j) - 1

    gate: Optional[int] = None
    if min_grade is not None:
        from ev.profit.rules import _GRADE_ORDER

        if min_grade not in _GRADE_ORDER:
            raise ValueError(f"未知奖级 {min_grade!r}，应为 {_GRADE_ORDER} 之一")
        gate = _GRADE_ORDER.index(min_grade)

    ind = np.zeros((h_max + 1, j_max + 1))          # 该 (h,j) 是否计入「中奖/达标」
    for (h, j), g in r.grade_table.items():
        if h <= h_max and j <= j_max:
            if gate is None or (g in _GRADE_ORDER and _GRADE_ORDER.index(g) <= gate):
                ind[h, j] = 1.0

    float_pay = _float_pay_matrix(lottery, ctx, M)
    cost = ctx.rules.cost * K

    # ---- ① P(至少中一注)：用直方图做 Π_c(1−P_c) = Π_h(1−p_win_h)^H[ρ,h] ----
    p_win_h = ind @ p_j
    prod = np.ones(M)
    for h in range(h_max + 1):
        v = 1.0 - p_win_h[h]
        col = H[:, h]
        if v <= 0.0:
            prod[col > 0] = 0.0
        elif v < 1.0:
            prod *= np.power(v, col.astype(np.float64))
    p_any = float(1.0 - (w * prod).sum())
    p_any = min(1.0, max(0.0, p_any))

    # ---- ②③④ 按内层类别 j 聚合（固定奖与 ρ 无关；浮动奖逐 ρ） ----
    win_mean = 0.0
    gross = 0.0
    tax_exp = 0.0
    dist = np.zeros(K + 1) if want_dist else None
    grade_cnt: Dict[str, float] = {}

    for j in range(j_max + 1):
        pj = float(p_j[j])
        if pj <= 0:
            continue
        per_h = np.zeros(h_max + 1)                 # 固定奖的逐 h 金额
        for h in range(h_max + 1):
            g = r.grade_of(h, j)
            if g is not None and r.grades[g].kind == "fixed":
                per_h[h] = float(r.grades[g].fixed)

        g_rho = H @ per_h                            # 固定奖总额（逐 ρ）
        for h in range(h_max + 1):
            g = r.grade_of(h, j)
            if g is None:
                continue
            col = float_pay.get(g)
            if col is not None:
                g_rho = g_rho + H[:, h] * col

        win_mean += pj * float((w * (H @ ind[:, j])).sum())
        gross += pj * float((w * g_rho).sum())

        if tax_basis == TX.BASIS_TICKET:
            # 逐注计税：每注的 gross 独立与阈值比较，整组只对有奖注逐注计征。
            # 同一 (ρ,h) 桶内的注，其固定奖 = per_h[h]、浮动奖 = 该结局 ρ 的池分摊
            # （与具体哪注无关），故逐注可加；再按桶内注数 H[ρ,h] 累加。
            tax_pt = np.zeros(M)
            for h in range(h_max + 1):
                g = r.grade_of(h, j)
                if g is None:
                    continue
                col = float_pay.get(g)
                gtc = per_h[h] + (col if col is not None else 0.0)
                taxed = np.where(gtc > TX.TAX_THRESHOLD, TX.TAX_RATE * gtc, 0.0)
                tax_pt = tax_pt + H[:, h] * taxed
            tax_exp += pj * float((w * tax_pt).sum())
        else:
            # 同期同彩种合并计税（默认）：整组中奖总额 > 阈值才按 20% 全额计征。
            tax_rho = np.where(g_rho > TX.TAX_THRESHOLD, TX.TAX_RATE * g_rho, 0.0)
            tax_exp += pj * float((w * tax_rho).sum())

        if dist is not None:
            cnt = (H @ ind[:, j]).astype(np.int64)
            dist += pj * np.bincount(cnt, weights=w, minlength=K + 1)[:K + 1]

    net_ev = gross - tax_exp - cost
    out: Dict[str, Any] = {
        "彩种": lottery, "注数": K, "κ": float(kappa),
        "P至少中一注": p_any,
        "中奖注数期望": float(win_mean),
        "税前期望": float(gross),
        "期望税": float(tax_exp),
        "净EV": float(net_ev),
        "净EV单注": float(net_ev / K),
        "成本": float(cost),
        "税口径": tax_basis,
    }
    if min_grade is not None:
        # 达标口径：上面那三个「中奖」量实际是「达标」量，故显式标注并给出别名
        out["达标线"] = min_grade
        out["P_达标"] = p_any
        out["达标注数期望"] = float(win_mean)
    if dist is not None:
        key = "中奖注数分布" if min_grade is None else "达标注数分布"
        out[key] = {int(i): float(v) for i, v in enumerate(dist) if v > 1e-12}
    return out


def simulate_net_prob(lottery: str, tickets: Sequence[dict], ctx: P.Ctx,
                      kappa: float = 0.0, n: int = 20000, seed: int = 0,
                      weights: Optional[np.ndarray] = None) -> Dict[str, Any]:
    """P(净盈利) 的**抽样估计**。

    为什么不对它做解析：给定结局 ρ 后，每注的内层类别 j 是**各自独立**抽的
    （双色球每注独立撞蓝球，不是整组共用一次抽签）。于是整组 gross 是 K 个
    非同分布两点/三点随机变量之和，解析式要逐 ρ 做多重卷积 —— M=110 万时不可行。
    ⇒ 用抽样，并且在输出里明确标 `估算(MC)`，绝不与解析量混报。

    P(≥1)、E[中奖注数]、净EV 都是线性的，解析式正确，不走这里。
    """
    r = ctx.rules
    U = O.outer_universe(lottery)
    M = len(U)
    w = weights if weights is not None else _outer_weights(lottery, kappa)
    rng = np.random.default_rng(seed)
    idx = rng.choice(M, size=n, p=w)

    T_out = O.tickets_to_outer(lottery, tickets)
    T_in = O.tickets_to_inner(lottery, tickets)
    E_tk = O._encode_outer(r, T_out).T.astype(np.float32)     # (d,K)
    cost = ctx.rules.cost * len(tickets)

    float_pay = _float_pay_matrix(lottery, ctx, M)

    pos = 0
    for s in idx:
        rho = U[int(s)]
        e = O._encode_outer(r, rho.reshape(1, -1)).astype(np.float32)   # (1,d)
        h = (e @ E_tk)[0].astype(np.int16)                              # (K,) 外层命中数

        # 内层：每注**独立**抽签（不是整组共用一次）
        j = _inner_class_of(r, T_in, rng)

        gross = 0.0
        for c in range(len(tickets)):
            g = r.grade_of(int(h[c]), int(j[c]))
            if g is None:
                continue
            gr = r.grades[g]
            if gr.kind == "fixed":
                gross += float(gr.fixed)
            else:
                gross += float(float_pay[g][int(s)])
        tax = TX.tax_on(gross, basis=TX.BASIS_ISSUE)
        if gross - tax - cost > 0:
            pos += 1
    return {"净盈利概率": pos / float(n), "样本数": int(n),
            "口径": "估算(MC)", "seed": int(seed),
            "免责": "抽样估计；P(≥1)/中奖注数/净EV 为解析值，不与此混用。"}


def _inner_class_of(r, T_in: np.ndarray, rng) -> np.ndarray:
    """由内层抽签推出每注的内层命中类（每注独立）。"""
    if r.inner_size == 1:
        return np.zeros(len(T_in), dtype=np.int16)
    if r.name == "双色球":                       # 蓝球 1..16
        b = int(rng.integers(1, 17))
        return (T_in == b).astype(np.int16)
    if r.name == "七星彩":                       # 第 7 位 0..14
        d = int(rng.integers(0, 15))
        return (T_in == d).astype(np.int16)
    if r.name == "大乐透":                       # 后区 1..12 里抽 2 个
        pick = rng.choice(np.arange(1, 13), size=2, replace=False)
        return (T_in == pick[None, :]).sum(axis=1).astype(np.int16)
    raise NotImplementedError(f"未实现的内层抽样: {r.name}")
