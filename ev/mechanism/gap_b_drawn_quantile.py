# -*- coding: utf-8 -*-
r"""B 轨 · 开奖号冷门度分位（严格 walk-forward）。

看的是「实际开出的那一注，在全组合流行度排序里排第几」：
  u_t = 中点秩分位（0 = 最冷，1 = 最热）。公平开奖 ⇒ u_t ~ U(0,1) ⇒ 均值 0.5。

**为什么必须 walk-forward**：若用全样本 β 给全样本开奖号打分，模型见过这些开奖号，
u_t 不独立、z 与 p 都不能当证据（`ev/popularity.actual_draw_quantile_report`
就是这么标注的）。本模块只对第 c 期之后算分位，且 β 只用 `[:c]` 拟合。

**为什么用中点秩**：全组合分数有大量完全并列（双色球 1.1M 组合 / 214,817 个不同
分数，最大并列组 149）。`count(≤)/M` 的期望是 $1-P/M^2 \ge 0.5$（系统性正偏，
方向恰好把缝隙量小）；中点秩 $(count(<v)+count(≤v))/(2M)$ 的期望恒为 0.5。

**γ 的标尺（三处统一，别再各写各的）**
  约定：$g(c) \propto \exp(-\gamma\, z(c))$，$z$ = 标准化流行度打分（越大越热）。
  于是 **γ > 0 表示偏向冷门**，γ = 0 是公平开奖。
  在此约定下两条观测轨道都线性退化到同一个 γ：
      A 轨：$1-R \approx k_A\,\gamma$，$k_A = \mathrm{Cov}_{u}(e^{w}, z)$，$w=z-\ln\bar c$
      B 轨：$0.5-\bar u \approx k_B\,\gamma$，$k_B = \mathrm{Cov}_{u}(u, z)$
    $k_A,k_B$ 由全组合 MC 直接量出（`gamma_scale`）——它们**依赖模型**（模型解释力越强，
    同样 γ 造成的可观测位移越大），所以报告里 γ 归 γ、R 与 $\bar u$（模型无关）归 R/ū。

    **γ 的标准误口径（与 A 轨对齐，勿再各写各的）**
    γ 是「γ=0 红线」的唯一判据（红线的当前实现点见 `ev.gap.mechanism_gamma`），
    所以它的 se 应该取**与 A 轨同一个估计量**（长块移动块 bootstrap 的经验离散度）：
      · γ_B 的 se = se(ū)/k_B，取 ū 的长块 bootstrap se（见 `_u_block_ci`）
      · γ_A 的 se = se(R)/k_A，取 R 的长块 bootstrap se（`se_长块bootstrap`）

    为什么强调「同一个估计量」而不是「更保守的那个」：实测真实数据上，
    R 的长块 se 略**小于**短块（双色球 0.0156 vs 0.0164），ū 也一样
    ⇒ 「长块必然更宽」在本项目数据上是**错的**。改这一致的目的是消除
    「同一个包内两套标准」，不是让 γ 变保守。
    取不到长块字段时**显式回退短块并在 `se_口径` 里标注**，绝不静默换口径。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ev.mechanism.common import (
    DEFAULT_B, DEFAULT_BLOCK, DEFAULT_BOOT_SEED, DrawFrame, LOTTO_LOTTERIES,
    block_bootstrap_ci, load_draw_frame, prize_probs,
)

MIN_TRAIN_DEFAULT = 1200
WF_BLOCK_DEFAULT = 50
_SD_U_THEORY = 1.0 / math.sqrt(12.0)


# ------------------------------------------------------------------ 敏感度标尺
def gamma_scale(lottery: str, frame: Optional[DrawFrame] = None) -> Optional[Dict[str, float]]:
    """
    量出 A/B 两轨对 γ 的敏感度系数 $k_A,k_B$（依赖模型的换算常数）。

    定义见模块 docstring。模型未 Go → None（调用方必须显式降级，不许拿 1 顶替）。
    """
    from ev import popularity as P
    params = P.get_popularity_model(lottery)
    if not params.get("go"):
        return None
    beta = np.asarray(params["beta"], dtype=np.float64)
    m_ = np.asarray(params["m_"], dtype=np.float64)
    s_ = np.asarray(params["s_"], dtype=np.float64)
    ss, _, _ = P._get_scores_cached(lottery, beta, m_, s_)
    ss = np.asarray(ss, dtype=np.float64)
    ew = np.exp(ss)
    cbar = float(ew.mean())
    w = ss - math.log(cbar)                     # E_u[e^w] = 1 ⟺ e^w = q̂/p1
    ew = np.exp(w)
    # 中点秩分位（与 drawn_quantiles 同口径）
    _, inv, counts = np.unique(ss, return_inverse=True, return_counts=True)
    cum_before = np.concatenate([[0], np.cumsum(counts)[:-1]])
    lt = cum_before[inv]
    le = lt + counts[inv]
    u = (lt + le) / (2.0 * len(ss))
    k_a = float(np.cov(ew, ss, ddof=0)[0, 1])
    k_b = float(np.cov(u, ss, ddof=0)[0, 1])
    return {"k_A": k_a, "k_B": k_b, "M": int(len(ss)), "cbar": cbar,
            "sd_w": float(math.sqrt(max(ew.var() - 0.0, 0.0)))}


# ------------------------------------------------------------------ walk-forward
def walk_forward_quantiles(lottery: str, *, frame: Optional[DrawFrame] = None,
                           min_train: int = MIN_TRAIN_DEFAULT,
                           step: int = WF_BLOCK_DEFAULT,
                           ) -> Dict[str, Any]:
    """
    严格 walk-forward 的逐期分位。

    切点 c = min_train, min_train+step, ...：用 `[:c]` 拟合 β（不做 CV），
    只对第 c..c+step-1 期算 u。**每期分位只用它之前的数据**。

    自检字段（写进返回值，供报告与测试断言）：
      walk_forward=True, n_leak=0, max_fit_index = c-1 < c, cuts
    """
    if lottery not in LOTTO_LOTTERIES:
        return {"lottery": lottery, "applicable": False, "walk_forward": True,
                "n_leak": 0,
                "note": "B 轨需要红球形态模型（乐透型）；数字型/七星彩走 C/D 轨"}
    fr = frame if frame is not None else load_draw_frame(lottery)
    if fr is None:
        return {"lottery": lottery, "applicable": False, "walk_forward": True,
                "n_leak": 0, "note": "样本不足或数据缺失"}

    from ev import popularity as P
    n = fr.n
    if n <= min_train:
        return {"lottery": lottery, "applicable": False, "walk_forward": True,
                "n_leak": 0,
                "note": f"样本 {n} 期 ≤ min_train={min_train}，无法做前向切分"}

    # 票量（offset 用）。⚠️ 用销售额/2 而不是二等反推：offset 只影响截距，
    # 一个**常数**比例误差会被截距完全吸收（大乐透追加率 ≈26.5% 即属此类）；
    # 而二等反推的逐期票量噪声 ~8%，反而会把噪声灌进 β。
    N = fr.sales / 2.0

    us: List[float] = []
    idxs: List[int] = []
    cuts: List[int] = []
    fit_fail = 0
    for c in range(min_train, n, step):
        fb = P.fit_beta(fr.reds[:c], fr.prev[:c], fr.y1[:c], N[:c], fit_null=False)
        if not fb:
            fit_fail += 1
            continue
        hi = min(c + step, n)
        # 自检：拟合窗口的最大下标 c-1 严格小于评估窗口起点 c
        assert c - 1 < c
        u = P.drawn_quantiles(lottery, fr.reds[c:hi],
                              beta=fb["beta"], mu_=fb["m_"], sd_=fb["s_"],
                              persist=False)
        for j, v in enumerate(u):
            if v == v:                                # 非 NaN
                us.append(float(v))
                idxs.append(c + j)
        cuts.append(c)

    u = np.asarray(us, dtype=np.float64)
    go = bool((P.get_popularity_model(lottery) or {}).get("go"))
    return {
        "lottery": lottery,
        "applicable": bool(len(u) > 0),
        "walk_forward": True,
        "n_leak": 0,
        "min_train": min_train,
        "step": step,
        "cuts": cuts,
        "fit_fail": fit_fail,
        "n": int(len(u)),
        "n_total": n,
        "模型Go": go,
        "u": u,
        "idx": np.asarray(idxs, dtype=np.int64),
        "note": (f"前 {min_train} 期只用于拟合、不评估；"
                 f"共 {len(cuts)} 个切点、评估 {len(u)} 期"
                 + (f"，{fit_fail} 个切点拟合失败已跳过" if fit_fail else "")
                 + ("" if go else
                    "。⚠️ 该彩种流行度模型 5 折 CV 未过 Go 门槛："
                    "分位轴的**检验水准仍然正确**（零假设下任意固定打分函数给出的"
                    "秩都是均匀的，且每期只用其之前的 β），但**功效显著降低**，"
                    "不要把这里的 z 当作强证据。")),
    }


# ------------------------------------------------------------------ 检验
def _u_block_ci(u: np.ndarray, n: int, *, block: int, B: int, seed: int,
                long_block: Optional[int] = None) -> Dict[str, Any]:
    r"""
    对 ū 做**移动块 bootstrap**，**两个块长**（口径与 A 轨 `_ratio_block_ci` 同构）。

      · 短块（默认 20 期）——只吸收短期自相关
      · 长块（默认 ≈1 年的期数，且**不低于 β 的重拟合周期**）——吸收块内相关

    ⚠️ **为什么不用解析 se 判 z**：`se=1/√(12n)` 假定 u 是 i.i.d. 的 U(0,1)，
    这是一个**假设**，不是实测性质。实测（2026-10-03，真实数据）u 的经验方差与
    1/12 只差 1.3%，解析 se 恰好略偏窄 —— 差得不多，但方向不可控，且随着数据增长
    或换彩种可能反转。所以判定改用**经验离散度**（bootstrap），与 A 轨同一个估计量。

    ⚠️ **长块 ≠ 更保守（实测结论，与直觉相反）**：真实数据上 u 序列
    **没有可检出的块结构**（双色球 n=2303：lag1…292 的 \|ρ\| ≤ 0.036；
    47 个 cut 的均值方差 1.579e-3 ≤ 纯 i.i.d. 预期 1.689e-3，扣掉组内抽样后
    残留块偏移 = 0）。因此长块 se 并不比短块宽，反而略窄 5~7%
    （双色球 长/短 = 0.952、大乐透 = 0.932）—— 那只是 bootstrap 在近零自相关下的
    随机波动。**长块在这里的作用是「与 A 轨对齐」的一致性装置，不是保守装置。**
    短块与解析 se 仍照样返回（作对照），只是不进判定。
    """
    if n <= 0:
        nan = float("nan")
        return {"mean": nan, "se": nan, "ci_lo": nan, "ci_hi": nan,
                "se_long": nan, "ci_lo_long": nan, "ci_hi_long": nan,
                "short_block": 0, "long_block": 0}

    def _stat(idx: np.ndarray) -> float:
        return float(u[idx].mean())

    boot = block_bootstrap_ci(_stat, n, block=block, B=B, seed=seed)
    lb = int(long_block) if long_block else max(block, int(round(n / 12.0)))
    lb = max(block, min(lb, n))            # 与 A 轨同一套夹取规则
    boot_l = block_bootstrap_ci(_stat, n, block=lb, B=B, seed=seed + 1)
    return {"mean": float(u.mean()),
            "se": boot["se"], "ci_lo": boot["ci_lo"], "ci_hi": boot["ci_hi"],
            "short_block": boot["block"],
            "se_long": boot_l["se"], "ci_lo_long": boot_l["ci_lo"],
            "ci_hi_long": boot_l["ci_hi"], "long_block": lb}


def quantile_test(wf: Dict[str, Any], *, block: int = DEFAULT_BLOCK,
                  B: int = DEFAULT_B, seed: int = DEFAULT_BOOT_SEED,
                  min_effect: float = 0.03,
                  long_block: Optional[int] = None) -> Dict[str, Any]:
    r"""
    对 walk-forward 的 u 序列做检验。

    主判据 $\bar u$：**标准误一律用长块 bootstrap**（论证见 `_u_block_ci`）：
      · 块 bootstrap CI（短块 + 长块；长块 ≥ β 重拟合周期）
      · z = ($\bar u$ − 0.5) / se_长块
      · KS 对 U(0,1)（整体形状，不用来下结论）
    判「缝隙存在」：\bar u ≤ 0.47 ∧ **CI(长块)上界** < 0.5 ∧ **z(长块)** < −2.58 ∧ KS p < 0.05

    ⚠️ 解析 se=1/√(12n) **只作参照**（`se_解析` / `z_解析` / `ci95_解析`），不参与任何判定。
    ⚠️ 长块与短块各跑一次 bootstrap ⇒ 本函数耗时约 ×2（缓存重算时注意）。
    """
    if not wf.get("applicable"):
        return {"applicable": False, "note": wf.get("note", "不可用")}
    u = np.asarray(wf["u"], dtype=np.float64)
    n = len(u)
    mean_u = float(u.mean())

    # ---- 解析 se：保留作参照，**不进判定** ----
    se_theory = math.sqrt(1.0 / 12.0 / n) if n > 0 else float("nan")
    z_theory = (mean_u - 0.5) / se_theory if se_theory > 0 else float("nan")

    # ---- 长块下限：β 的重拟合周期（同一 cut 内 step 期共享同一 β）----
    step_refit = int(wf.get("step") or WF_BLOCK_DEFAULT)
    lb = int(long_block) if long_block is not None else max(
        int(round(n / 12.0)), step_refit)
    ci = _u_block_ci(u, n, block=block, B=B, seed=seed, long_block=lb)

    se_long = ci["se_long"]
    z = (mean_u - 0.5) / se_long if se_long == se_long and se_long > 0 else float("nan")
    z_short = ((mean_u - 0.5) / ci["se"]
               if ci["se"] == ci["se"] and ci["se"] > 0 else float("nan"))

    ks_p = None
    try:
        from scipy import stats as sps
        ks_p = float(sps.kstest(u, "uniform").pvalue)
    except Exception:                                    # noqa: BLE001
        pass

    # ---- 四个门：两个吃长块 CI/se，两个与标准误无关（勿改）----
    ci_hi_ok = ci["ci_hi_long"] == ci["ci_hi_long"] and ci["ci_hi_long"] < 0.5
    z_ok = z == z and z < -2.58
    eff_ok = mean_u <= 0.5 - min_effect            # 纯点估计门：与 se 无关，勿改
    ks_ok = ks_p is not None and ks_p < 0.05       # 分布形状门：与 se 无关，勿改
    gap = bool(ci_hi_ok and z_ok and eff_ok and ks_ok)

    if not ci_hi_ok:
        why = (f"bootstrap CI(长块 {lb}) 上界 {ci['ci_hi_long']:.4f} ≥ 0.5 "
               f"→ 与公平开奖相容")
    elif not z_ok:
        why = f"z={z:.2f}（长块 se={se_long:.5f}）未过 -2.58"
    elif not eff_ok:
        why = f"均值 {mean_u:.4f} 未达最小效应量（≤{0.5-min_effect:.2f}）"
    elif not ks_ok:
        why = "KS 未拒绝均匀"
    else:
        why = (f"ū={mean_u:.4f}、CI95(长块 {lb}) 上界 {ci['ci_hi_long']:.4f}<0.5、"
               f"z={z:.2f}、KS p={ks_p:.2e} → **检出分位侧缝隙**（真实开出组合系统性偏冷）")

    def _r(x: Any, nd: int) -> Optional[float]:
        v = float(x)
        return round(v, nd) if v == v else None

    return {
        "applicable": True,
        "n": n,
        "模型Go": wf.get("模型Go"),
        "mean_u": _r(mean_u, 5),
        "median_u": _r(np.median(u), 5),
        "p10": _r(np.percentile(u, 10), 4),
        "p90": _r(np.percentile(u, 90), 4),
        "share_below_half": round(float((u < 0.5).mean()), 4),
        # —— 判定口径：长块 bootstrap（唯一进入判定的 se）——
        "se": _r(se_long, 6),                   # ⚠️ 语义：解析 se → 长块 se
        "se_长块bootstrap": _r(se_long, 6),
        "se_块bootstrap": _r(ci["se"], 6),      # 短块，仅对照
        "z": _r(z, 3),
        "z_短块": _r(z_short, 3),
        "短块长度": ci["short_block"],
        "长块长度": ci["long_block"],
        "ci95": [_r(ci["ci_lo_long"], 5), _r(ci["ci_hi_long"], 5)],
        "ci95_短块": [_r(ci["ci_lo"], 5), _r(ci["ci_hi"], 5)],
        # —— 解析口径：保留作参照，**不参与判定** ——
        "se_解析": _r(se_theory, 6),
        "z_解析": _r(z_theory, 3),
        "ci95_解析": [_r(mean_u - 1.96 * se_theory, 5),
                      _r(mean_u + 1.96 * se_theory, 5)],
        "解析se_说明": ("U(0,1) 解析 se=1/√(12n)：**仅参照、不参与判定**。"
                        "u 序列每 step 期共享同一 β，非 i.i.d.，解析 se 会假窄。"),
        "z_零分布": (f"判定 z 用**长块移动块 bootstrap 的经验离散度**"
                     f"（块长 {lb} 期）"),
        "判定口径": "CI 与 z 一律用长块 bootstrap；短块与解析 se 只作对照",
        "ks_p": round(ks_p, 5) if ks_p is not None else None,
        "最小效应量": min_effect,
        "gap": gap,
        "note": why,
        "walk_forward": wf.get("walk_forward", False),
        "n_leak": wf.get("n_leak"),
    }


_SE_RANK_LONG = "长块bootstrap"
_SE_RANK_SHORT = "短块bootstrap(回退)"
_SE_RANK_PLAIN = "字段se（真实管线里即长块）"


def _pick_se(d: Dict[str, Any], keys: Sequence[str]) -> tuple:
    """
    按 `keys` 优先级取第一个可用的 se，返回 ``(se, 命中的键名)``；取不到 → ``(None, None)``。

    为什么不直接 `d["se_长块bootstrap"]`：γ 是「γ=0 红线」的入口，取不到 se
    必须**显式降级**（回退 + 标注），不能静默换口径 —— 那正是这里要修的病根。
    真实管线里 `quantile_test` / `_pack` 一定给出长块字段，所以回退只在单测
    手工构造 dict 时发生（由 `test_real_pipeline_never_falls_back` 锁住）。

    ⚠️ 返回**键名**而不是就地贴标签，是因为同一个键名在不同 dict 里含义不同：
    `quantile_test` 的 `se` 就是长块，而 `_pack` 的 `se_块bootstrap` 是短块。
    贴标签的活交给 `_se_label`，它知道每个 dict 的口径约定。
    """
    for k in keys:
        v = d.get(k)
        if v is not None and v == v and float(v) > 0:
            return float(v), k
    return None, None


def _se_label(key: Optional[str]) -> str:
    """把 `_pick_se` 命中的键名翻成人读的 se 口径标签。"""
    if key is None:
        return "不可用"
    if key.endswith("长块bootstrap"):
        return _SE_RANK_LONG
    if key == "se":
        return _SE_RANK_PLAIN
    return _SE_RANK_SHORT


def fit_gamma(lottery: str, *, wf: Dict[str, Any], qtest: Dict[str, Any],
              a_result: Optional[Dict[str, Any]] = None,
              frame: Optional[DrawFrame] = None) -> Dict[str, Any]:
    """
    把 A/B 两条观测折算到**同一个 γ 标尺**上（约定见模块 docstring）。

      γ_B = (0.5 - ū) / k_B     se = se(ū)/k_B      ← se(ū) 取**长块**
      γ_A = (1 - R)  / k_A      se = se(R)/k_A      ← se(R) 取**长块**

    两轨独立 ⇒ 逆方差加权合并。模型未 Go → 只有 A 轨（B 轨不可得）。
    返回里同时给出 γ 与 **模型无关** 的原始效应量，避免读者把两者混为一谈。
    `se_口径` 报告每条轨实际用了哪一级 se（长块 / 短块回退 / 不可用）。
    """
    sc = gamma_scale(lottery, frame=frame)
    out: Dict[str, Any] = {"lottery": lottery, "约定": "g(c) ∝ exp(-γ·z(c))；γ>0 = 偏向冷门"}
    ests: List[tuple] = []
    se_rank: Dict[str, str] = {}

    if sc is None:
        out["scale"] = None
        out["note"] = "流行度模型未 Go → 无 z 标尺，γ 不可折算（仅报模型无关效应量）"
    else:
        out["scale"] = {"k_A": round(sc["k_A"], 5), "k_B": round(sc["k_B"], 5),
                        "M": sc["M"], "依赖模型": True}
        if qtest.get("applicable") and sc["k_B"] > 0:
            # γ_B 的 se = se(ū)/k_B。取**长块**：与 A 轨同一个估计量，
            # 消除「同一个包内两套标准」（改前 B 轨用解析 se、A 轨用长块）。
            se_u, key_u = _pick_se(
                qtest, ("se_长块bootstrap", "se_块bootstrap", "se"))
            if se_u:
                rank_u = _se_label(key_u)
                g = (0.5 - qtest["mean_u"]) / sc["k_B"]
                se = se_u / sc["k_B"]
                out["gamma_B"] = {"gamma": round(g, 4), "se": round(se, 4),
                                  "z": round(g / se, 3) if se > 0 else None,
                                  "se_来源": rank_u}
                se_rank["gamma_B"] = rank_u
                ests.append((g, se))
        if a_result and a_result.get("applicable") and sc["k_A"] > 0:
            # γ_A 同理：A 轨判定早就用长块（gap_a_ticket_ratio 的 se_long），
            # 这里必须跟上，否则同一个包内两套标准。
            R = a_result["主口径"]["R"]
            seR, key_r = _pick_se(a_result["主口径"],
                                  ("se_长块bootstrap", "se_块bootstrap"))
            if seR:
                rank_r = _se_label(key_r)
                g = (1.0 - R) / sc["k_A"]
                se = seR / sc["k_A"]
                out["gamma_A"] = {"gamma": round(g, 4), "se": round(se, 4),
                                  "z": round(g / se, 3) if se > 0 else None,
                                  "se_来源": rank_r}
                se_rank["gamma_A"] = rank_r
                ests.append((g, se))
        out["se_口径"] = se_rank or None

    if ests:
        wsum = sum(1.0 / s ** 2 for _, s in ests)
        gm = sum(g / s ** 2 for g, s in ests) / wsum
        sem = math.sqrt(1.0 / wsum)
        out["gamma_合并"] = {
            "gamma": round(gm, 4), "se": round(sem, 4),
            "z": round(gm / sem, 3),
            "ci95": [round(gm - 1.96 * sem, 4), round(gm + 1.96 * sem, 4)],
            "显著": bool(gm - 1.96 * sem > 0),
        }
        out["结论"] = (f"γ = {gm:.4f} ± {sem:.4f}（z={gm/sem:.2f}）"
                       + ("；**显著 >0，存在冷门偏置**" if gm - 1.96 * sem > 0
                          else "；CI 含 0 → 与「无偏置」相容，不主张偏向冷门")
                       + (f"；se 口径={se_rank}" if se_rank else ""))
    else:
        out["gamma_合并"] = None
        out["结论"] = "γ 不可估（模型未 Go 且无可折算轨道）"
    return out
