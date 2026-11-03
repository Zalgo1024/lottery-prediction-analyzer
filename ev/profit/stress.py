# -*- coding: utf-8 -*-
"""ev/profit/stress.py —— 不确定情景下的策略压力测试（盈利策略 vs 等注数随机基线）。

## 要回答的问题
把「开奖前不可见 / 销售信息缺失 / 热门度估计偏差」当成不确定情景，检验盈利策略
在这些情景下**是否仍优于等注数随机基线**；若结果一变就大幅退化，就降低可信度或停用。

## 口径（唯一，勿改）
盈利策略的 EV 优势**只来自浮动奖的分奖**：选低拥挤票 → 中奖时少人分 → 实得高。
固定奖对所有票相同；κ=0 时机制倾斜也不动（本项目 25 条 edge 全未检出）。所以：
  · 无浮动奖的彩种（排列3/福彩3D/排列5）⇒ 优势恒为 0（策略 = 随机）；
  · 有浮动奖但热度模型未 Go（例如大乐透当前 popularity 未通过）⇒ 同样为 0；
  · 只有「有浮动奖 + 热度模型 Go」（如双色球）才有可测优势。

## 三个情景
1. **开奖前不可见**：由 PIT 保证（`pit.as_of` 的 history 严格早于目标期）。本模块**校验**
   这一点（`history[-1].key < target_key`）并如实报告，不是假设。
2. **热度估计偏差 / 噪声**：把 log-popularity 分数整体缩放 `(1+δ)` 或加 `N(0,σ)`；
   选号用**扰动后**的分数，评估仍用**基准**分数 —— 这样才看得出「被误导后真实优势还剩多少」。
3. **拥挤度/销售缺失**：分数归零（无分奖信息）⇒ 退化为均匀，优势应为 0。

## 判据（写死在常量里；改判据必须同时改版本号）
- 基准优势 ≤ `ADV_EPS` ⇒ **无优势**（策略在最好的情况下也不优于随机）。
- 所有情景保留率 ≥ `RETENTION_STABLE` ⇒ **稳定**。
- 任一情景保留率 ≤ 0 ⇒ **退化**（扰动下反而不如随机）。
- 其余 ⇒ **脆弱**。

## 不在本模块范围内
「开奖被完全操控」：若开奖方可任意选择号码，任何固定票都会被避开 ⇒ 策略与随机**同时**
归零，优势不存在。这不是策略的缺陷而是「开奖是否可信」的前提问题；本模块只能说明：
一旦开奖可被操控，选号策略（含随机）都不具备正期望，唯一的防线是对开奖的审计。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ev.lowcrowd_backtest import PROD_OVERLAP_CAP
from ev.profit import payouts as P
from ev.profit import pit
from ev.profit import tilt as T
from ev.profit.outcomes import outer_universe
from ev.profit.select import ev_proxy, _overlap_ok

STRESS_VERSION = "2026-11-04-A"

ADV_EPS = 1e-4            # 元/注：低于此视为「无优势」
RETENTION_STABLE = 0.5    # 优势保留率阈值
DEFAULT_NOISE = (0.1, 0.3, 0.5, 1.0)
DEFAULT_BIAS = (-0.5, -0.3, 0.3, 0.5)


# ---------------------------------------------------------------- 扰动
def _perturb(tl: T.Tilt, *, noise_sigma: float = 0.0, scale: float = 1.0,
             seed: int = 0) -> T.Tilt:
    """把 log-popularity 分数整体加噪声 / 缩放，保持 μ 宇宙同一性。

    raw = scores + offset；raw' = raw·scale + N(0,σ)；scores' = raw'−mean(raw')；
    offset' = mean(raw')。scale=0 ⇒ 分数全 0、offset=0 = **无分奖信息**（均匀）。
    """
    raw = tl.scores.astype(np.float64) + float(tl.offset)
    if noise_sigma:
        rng = np.random.default_rng(int(seed))
        raw = raw + rng.normal(0.0, float(noise_sigma), size=raw.shape)
    raw = raw * float(scale)
    return T.Tilt(lottery=tl.lottery, scores=raw - raw.mean(),
                  quantiles=tl.quantiles, source=tl.source,
                  offset=float(raw.mean()), note=tl.note,
                  beta_asof=tl.beta_asof)


# ---------------------------------------------------------------- 选票 / 评估
def _pool_idx(lottery: str, pool_size: int, seed: int) -> np.ndarray:
    U = outer_universe(lottery)
    if pool_size >= len(U):
        return np.arange(len(U))
    rng = np.random.default_rng(int(seed))
    return np.sort(rng.choice(len(U), size=int(pool_size), replace=False))


def _select_globals(lottery: str, U: np.ndarray, idx: np.ndarray, tl: T.Tilt,
                    ctx: P.Ctx, K: int, cap: int, seed: int = 0) -> List[int]:
    """用给定 tilt 的分数选 K 注（EV 降序 + 重叠上限），返回全局行号。

    ★ 并列（尤其「无信息」情景下 EV 全相等）必须用**种子化随机**打破：否则 `argsort`
      按行号顺序选（低行号＝特定组合，如 01 02 03 04 05 06），是有偏的、不是随机基线。
      1e-12 的 jitter 远小于真实差异（~1e-3），不会改变正常排序。
    """
    ev = ev_proxy(lottery, U[idx], tl.scores[idx], ctx, 0.0, tilt=tl)
    jitter = np.random.default_rng(int(seed) + 12345).random(ev.size) * 1e-12
    order = np.argsort(-(ev + jitter))
    ordered = not ctx.rules.outer_choose
    chosen_rows: List[List[int]] = []
    chosen_globals: List[int] = []
    for j in order:
        g = int(idx[j])
        row = [int(x) for x in U[g]]
        if _overlap_ok(row, chosen_rows, cap, ordered=ordered):
            chosen_rows.append(row)
            chosen_globals.append(g)
        if len(chosen_globals) >= K:
            break
    return chosen_globals


def _corrupt_fraction(tl: T.Tilt, fraction: float = 0.2, seed: int = 0) -> T.Tilt:
    """把一部分票的热度标签**打乱**（模拟「局部错标」）。

    均匀缩放 `(1+δ)` 只改幅度、不改排序 ⇒ 对按排名选票的策略毫无影响（实测保留率 100%）。
    真正会伤策略的是**排序被打乱**：把 frac 比例的分数与随机位置的分数对调。
    """
    s = tl.scores.astype(np.float64).copy()
    n = max(1, int(len(s) * float(fraction)))
    rng = np.random.default_rng(int(seed) + 777)
    pick = rng.choice(len(s), size=n, replace=False)
    s[pick] = s[rng.permutation(len(s))[:n]]
    return T.Tilt(lottery=tl.lottery, scores=s - s.mean(), quantiles=tl.quantiles,
                  source=tl.source, offset=float(tl.offset), note=tl.note,
                  beta_asof=tl.beta_asof)


def _mean_ev(lottery: str, U: np.ndarray, globals: Sequence[int],
             tl: T.Tilt, ctx: P.Ctx) -> Optional[float]:
    """用**给定 tilt** 评估这些票的平均单注 EV（用于「真实」口径）。"""
    sel = np.asarray(list(globals), dtype=int)
    if sel.size == 0:
        return None
    ev = ev_proxy(lottery, U[sel], tl.scores[sel], ctx, 0.0, tilt=tl)
    return float(ev.mean())


def _random_ev(lottery: str, U: np.ndarray, idx: np.ndarray, tl: T.Tilt,
               ctx: P.Ctx, K: int, seed: int) -> Optional[float]:
    rng = np.random.default_rng(int(seed) + 99991)
    n = min(int(K), len(idx))
    pick = idx[rng.choice(len(idx), size=n, replace=False)]
    ev = ev_proxy(lottery, U[pick], tl.scores[pick], ctx, 0.0, tilt=tl)
    return float(ev.mean())


# ---------------------------------------------------------------- 编排
def stress_test(lottery: str, issue: Any, K: int = 20, seed: int = 0,
                pool_size: int = 20000, cap: Optional[int] = None,
                noise: Sequence[float] = DEFAULT_NOISE,
                bias: Sequence[float] = DEFAULT_BIAS) -> Dict[str, Any]:
    """单期压力测试：策略 EV 优势在热度失真下是否存活。

    选号用**扰动后**的分数、评估用**基准**分数 —— 这是关键：只有这样才能量化
    「模型把我们引向错误票之后，真实的优势还剩多少」。
    """
    ctx = P.build_ctx(lottery, issue)
    view = pit.as_of(lottery, issue)
    no_lookahead = bool(len(view.history) == 0 or view.history[-1].key < view.target_key)

    base_tl = T.outer_tilt(lottery)
    head = {"彩种": lottery, "期号": str(issue), "K": int(K),
            "前视检查": no_lookahead, "判据版本": STRESS_VERSION}
    if base_tl is None:
        return {**head, "判决": "无优势", "基准优势_元每注": 0.0,
                "原因": "倾斜不可用（热度模型未 Go / 不支持）⇒ 策略与随机无差别",
                "情景": []}

    U = outer_universe(lottery)
    idx = _pool_idx(lottery, pool_size, seed)
    cap = PROD_OVERLAP_CAP if cap is None else int(cap)

    g_base = _select_globals(lottery, U, idx, base_tl, ctx, K, cap, seed=seed)
    ev_strategy = _mean_ev(lottery, U, g_base, base_tl, ctx)
    ev_random = _random_ev(lottery, U, idx, base_tl, ctx, K, seed)
    adv_base = ((ev_strategy - ev_random)
                if (ev_strategy is not None and ev_random is not None) else 0.0)

    def _scenario(name: str, pt: T.Tilt) -> Dict[str, Any]:
        g = _select_globals(lottery, U, idx, pt, ctx, K, cap, seed=seed)
        ev_s = _mean_ev(lottery, U, g, base_tl, ctx)      # ★ 用基准分数评估
        adv_s = (ev_s - ev_random) if ev_s is not None else None
        ret = (adv_s / adv_base) if (adv_base > ADV_EPS and adv_s is not None) else None
        return {"情景": name, "策略EV_元每注": ev_s, "随机EV_元每注": ev_random,
                "优势_元每注": adv_s, "保留率": ret,
                "仍优于随机": bool(adv_s is not None and adv_s > ADV_EPS)}

    scen: List[Dict[str, Any]] = []
    for s in noise:
        scen.append(_scenario(f"热度噪声σ={s:g}",
                              _perturb(base_tl, noise_sigma=s, seed=seed)))
    for d in bias:
        scen.append(_scenario(f"热度偏差δ={d:+g}",
                              _perturb(base_tl, scale=1.0 + d, seed=seed)))
    scen.append(_scenario("局部错标20%",
                          _corrupt_fraction(base_tl, fraction=0.2, seed=seed)))
    # ★ 拥挤度缺失：无分奖信息 ⇒ 选号**等价于随机**，优势按定义为 0（不是抽样出来的近似）
    scen.append({"情景": "拥挤度缺失(无分奖信息)", "策略EV_元每注": ev_random,
                 "随机EV_元每注": ev_random, "优势_元每注": 0.0,
                 "保留率": (0.0 if adv_base > ADV_EPS else None), "仍优于随机": False})

    if adv_base <= ADV_EPS:
        verdict = "无优势"
        reason = (f"基准优势 {adv_base:+.6f} 元/注 ≤ {ADV_EPS:g} ⇒ 策略在最好的情况下"
                  "也不优于随机（该彩种无可用分奖杠杆/热度模型）")
    else:
        # 「优势消失」= 某情景下优势 ≤ ADV_EPS（不再优于随机）。不区分"轻微为负"，
        #   因为随机基线本身有 ~1e-2 量级抽样噪声，硬抠符号没有意义。
        gone = [s for s in scen if s["优势_元每注"] is not None
                and s["优势_元每注"] <= ADV_EPS]
        rets = [s["保留率"] for s in scen if s["保留率"] is not None]
        if gone:
            worst = min(gone, key=lambda s: s["优势_元每注"])
            verdict = "退化"
            reason = (f"基准优势 {adv_base:+.6f} 元/注，但「{worst['情景']}」下优势降到 "
                      f"{worst['优势_元每注']:+.6f} ⇒ 该情景下已不优于随机，建议停用或降级")
        elif all(r >= RETENTION_STABLE for r in rets):
            verdict = "稳定"
            reason = (f"基准优势 {adv_base:+.6f} 元/注；所有情景保留率 ≥ {RETENTION_STABLE:.0%}"
                      "（在模型失真下优势仍存活）")
        else:
            verdict = "脆弱"
            worst_ret = min(rets)
            reason = (f"基准优势 {adv_base:+.6f} 元/注；最差情景保留率 {worst_ret:.0%} "
                      f"< {RETENTION_STABLE:.0%} ⇒ 优势部分依赖热度模型的准确性")

    return {
        **head,
        "判决": verdict, "原因": reason,
        "基准策略EV_元每注": ev_strategy, "随机EV_元每注": ev_random,
        "基准优势_元每注": adv_base,
        "情景": scen,
        "说明": [
            "优势只来自浮动奖分奖（低拥挤票少人分），固定奖对所有票相同",
            "选号用扰动后分数、评估用基准分数 ⇒ 量化「被误导后真实优势还剩多少」",
            "开奖前不可见由 PIT 校验（history 严格早于目标期），不是假设",
            "开奖若可被完全操控 ⇒ 策略与随机同时归零，任何选号策略都无正期望",
        ],
    }
