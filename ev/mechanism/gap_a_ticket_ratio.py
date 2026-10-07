# -*- coding: utf-8 -*-
r"""A 轨 · 注数比检验 —— **模型无关**，是"缝隙存在"的主判据。

核心量： R = 实际一等奖注数 / 期望一等奖注数。

为什么 E[R] = 1 与玩家偏好**完全无关**：
  给定某期开出的完整组合 C，该期一等奖注数 = 买了 C 的注数（确定量）。
  若 C 均匀分布，则 E[一等奖注数] = 票量 × P(任一注等于 C) = 票量 · p1。
  这个等式对**任何**玩家选号分布都成立 —— 不需要知道玩家偏不偏好生日号。
  ⇒ R < 1 只可能是"开出的组合被买得比随机更少"。

——
**两个口径，主口径免票量**

  R_sales = Σy1 / Σ(总投注额/2 · p1)        依赖"2 元一注"
  R_calib = (Σy1 / Σy2) · (p2 / p1)         用**二等奖注数反推票量**，免票量
                                            也顺便免掉复式/多倍/追加的记账差异

  实测（2026-10-03）：大乐透的"2 元一注"**不成立**（追加投注使销售额/2 高估
  票量 13.24%），于是 R_sales = 0.86181 看着 z=-4.16 极显著 —— 纯属假象；
  同一批数据的 R_calib = 0.97591（CI 含 1）。两条独立路径给出**同一个**
  追加因子（0.86181 × 1.1324 = 0.9759），互为验证。
  ⇒ 主口径取 R_calib；R_sales 只在票量校准通过时才参与判定。

——
**不能用模型隐含方差当标准误**：双色球经验 se 0.0203 是模型隐含 se 0.0030 的
6.7 倍（实际零注占比 5.99% vs 泊松(8.16) 的 0.03%）。用模型 se 会把"无缝隙"
误判成"有缝隙"。一律用经验离散度 + 块 bootstrap。

——
**A′ 轨（本文件下半部分）**：同一套注数比口径搬到**数字型 / 七星彩**上。
它们的三列数据不在 loader 里，必须直读 `lottery_data/*销售奖级数据.csv`
（见 `ev/mechanism/sales_grade.py` 里记的三个口径陷阱）。
实测：排列5 R=1.031（CI [0.970, 1.099]）、七星彩现行段六个奖级 R =
1.034/1.018/0.973/0.996/0.998/0.991 —— 全部与 1 相容。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from ev.mechanism.common import (
    DEFAULT_B, DEFAULT_BLOCK, DEFAULT_BOOT_SEED, DrawFrame, LOTTO_LOTTERIES,
    block_bootstrap_ci, autocorr, load_draw_frame, prize_probs,
    ticket_calibration,
)

# 判"缝隙存在"的最低效应量：R 至少要低 3% 才算，避免把千分之几的系统误差
# 因为样本大而报成"显著"。
DEFAULT_MIN_EFFECT = 0.03
# 旧版这里还有一个 `_Z95` 常量（零调用者），字面量与 `common.block_bootstrap_ci`
# 里那份重复。已删；标准正态分位的单一真源是 `common.Z95`。


def _ratio_block_ci(num: np.ndarray, den: np.ndarray, *, block: int, B: int,
                    seed: int, long_block: Optional[int] = None) -> Dict[str, Any]:
    """
    对 Σnum/Σden 做块 bootstrap，**两个块长**：

      · 短块（默认 20 期 ≈ 1 个月）——只吸收短期自相关
      · 长块（默认 ≈1 年的期数）——**吸收逐年漂移**

    为什么必须报长块：实测双色球 R 逐年从 1.04 漂到 0.97，
    短块 bootstrap 的 CI 抓不到这种低频成分，会**假窄**（从而把"无缝隙"
    误报成"有缝隙"）。判定一律用长块 CI，短块 CI 只作对照。
    """
    n = len(num)
    if n == 0:
        return {"R": float("nan"), "se": float("nan"), "ci_lo": float("nan"),
                "ci_hi": float("nan"), "se_long": float("nan"),
                "ci_lo_long": float("nan"), "ci_hi_long": float("nan"),
                "long_block": 0}
    R = float(num.sum() / den.sum())

    def _stat(idx: np.ndarray) -> float:
        d = float(den[idx].sum())
        return float(num[idx].sum()) / d if d > 0 else float("nan")

    boot = block_bootstrap_ci(_stat, n, block=block, B=B, seed=seed)
    lb = int(long_block) if long_block else max(block, int(round(n / 12.0)))
    lb = max(block, min(lb, n))
    boot_l = block_bootstrap_ci(_stat, n, block=lb, B=B, seed=seed + 1)
    return {"R": R, "se": boot["se"], "ci_lo": boot["ci_lo"],
            "ci_hi": boot["ci_hi"], "B": boot["B"], "block": boot["block"],
            "se_long": boot_l["se"], "ci_lo_long": boot_l["ci_lo"],
            "ci_hi_long": boot_l["ci_hi"], "long_block": lb}


def _pack(tag: str, d: Dict[str, Any], *, usable: bool, note: str) -> Dict[str, Any]:
    """把口径结果打包成报告友好的形状；**判定用长块 CI**。"""
    R = d.get("R", float("nan"))
    se_long = d.get("se_long", float("nan"))
    se_short = d.get("se", float("nan"))
    se = se_long if (se_long == se_long and se_long > 0) else se_short
    z = (R - 1.0) / se if se and se == se and se > 0 else float("nan")
    ci_lo, ci_hi = d.get("ci_lo_long"), d.get("ci_hi_long")
    return {
        "口径": tag, "R": round(R, 6),
        "se_块bootstrap": round(se_short, 6) if se_short == se_short else None,
        "se_长块bootstrap": round(se_long, 6) if se_long == se_long else None,
        "长块长度": d.get("long_block"),
        "ci95": [round(ci_lo, 5), round(ci_hi, 5)] if ci_lo == ci_lo else None,
        "ci95_短块": [round(d["ci_lo"], 5), round(d["ci_hi"], 5)]
        if d.get("ci_lo") == d.get("ci_lo") else None,
        "z": round(z, 3) if z == z else None,
        "效应量(1-R)": round(1.0 - R, 5) if R == R else None,
        "usable": bool(usable),
        "说明": note,
    }


def _conditional_diagnostic(fr: DrawFrame) -> Optional[Dict[str, Any]]:
    """
    **模型校准诊断**（原方案里的"条件口径"）。

    ⚠️ 这不是检验：R_cond = Σy1 / Σ(票量·p1·e^{β·z}) 在
       q̂_c = p1·e^{βz_c}/c（c 为归一常数）时**恒等于 c**，
       与开奖是否偏置完全无关。实测 E[R_cond] 在偏置 θ∈[0,4] 上恒为
       0.89933、sd=0.000000。
       它回答的是"流行度模型在开出的组合上校准得准不准"，不是"有没有缝隙"。
       故只作诊断输出，**禁止进入判定**。
    """
    from ev import popularity as P
    params = P.get_popularity_model(fr.lottery)
    if not params.get("go"):
        return None
    beta = np.asarray(params["beta"], dtype=np.float64)
    m_ = np.asarray(params["m_"], dtype=np.float64)
    s_ = np.asarray(params["s_"], dtype=np.float64)
    F = P.features_matrix(fr.reds, fr.prev)
    z = P._score_std(F, m_, s_, beta)
    p = prize_probs(fr.lottery)
    N_sales = fr.sales / 2.0
    lam = N_sales * p["一等"] * np.exp(z)
    R = float(fr.y1.sum() / lam.sum())
    return {
        "R_cond": round(R, 6),
        "检验力": "无——恒为常数，只作模型校准诊断，禁止进入判定",
        "含义": ("≈1 表示流行度模型在真实开出的组合上校准良好；"
                 "偏离 1 只说明模型 scale 偏了，不说明开奖有偏置"),
    }


def _drift_test(fr: DrawFrame, years: Dict[str, float],
                year_masks: Dict[str, np.ndarray], den: np.ndarray, *,
                block: int, B: int, seed: int) -> Dict[str, Any]:
    """
    逐年 R 的趋势检验 + 「最早 25% 年 vs 最近 25% 年」对比。

    为什么要这一步：逐年 R 的**极差**永远很大（单年只有 ~150 期，R 的经验 sd ≈
    1.2/sqrt(150) ≈ 0.10），光看极差会把纯噪声读成"趋势"。这里给出趋势的
    Spearman p 与一个**预先定死比例**的早/晚对比，把"看着有漂移"变成可反驳的数字。

    ⚠️ 早/晚对比用的是两段**相互独立**的近似（se 平方和开根），不做联合 bootstrap；
       分段本身也是事后选的对比，p 只能当线索、不能当结论。
    """
    out: Dict[str, Any] = {"n年": len(years)}
    if len(years) < 6:
        out["note"] = "年数 < 6，不做趋势检验（会过度解读）"
        return out
    keys = list(years)
    yv = np.asarray([int(k) for k in keys], dtype=float)
    rv = np.asarray([years[k] for k in keys], dtype=float)
    if float(rv.std()) < 1e-12:
        # 逐年 R 完全恒定 → Spearman 无定义（会返回 nan 并告警），显式跳过
        out["趋势_Spearman_rho"] = None
        out["趋势_p"] = None
        out["note"] = "逐年 R 完全恒定，趋势检验无定义"
    else:
        try:
            from scipy import stats as sps
            rho, p = sps.spearmanr(yv, rv)
            out["趋势_Spearman_rho"] = round(float(rho), 4)
            out["趋势_p"] = round(float(p), 5)
        except Exception:                                # noqa: BLE001
            out["趋势_Spearman_rho"] = None
            out["趋势_p"] = None

    q = max(1, len(keys) // 4)
    ek, lk = keys[:q], keys[-q:]
    me = np.zeros(fr.n, dtype=bool)
    for k in ek:
        me |= year_masks[k]
    ml = np.zeros(fr.n, dtype=bool)
    for k in lk:
        ml |= year_masks[k]
    if me.sum() < 50 or ml.sum() < 50:
        out["note"] = "早/晚段期数不足，跳过分段对比"
        return out
    re_ = _ratio_block_ci(fr.y1[me], den[me], block=block, B=B, seed=seed)
    rl_ = _ratio_block_ci(fr.y1[ml], den[ml], block=block, B=B, seed=seed + 7)
    se_d = math.sqrt(re_["se_long"] ** 2 + rl_["se_long"] ** 2)
    diff = rl_["R"] - re_["R"]
    out["早期"] = {"年": f"{ek[0]}~{ek[-1]}", "期数": int(me.sum()),
                   "R": round(re_["R"], 4),
                   "ci95": [round(re_["ci_lo_long"], 4), round(re_["ci_hi_long"], 4)]}
    out["近期"] = {"年": f"{lk[0]}~{lk[-1]}", "期数": int(ml.sum()),
                   "R": round(rl_["R"], 4),
                   "ci95": [round(rl_["ci_lo_long"], 4), round(rl_["ci_hi_long"], 4)]}
    out["差_近期减早期"] = round(float(diff), 4)
    out["差_se"] = round(float(se_d), 4)
    out["差_z"] = round(float(diff / se_d), 3) if se_d > 0 else None
    out["note"] = ("分段是**事后**选的对比，p 只能当线索。"
                   "若趋势_p 与 差_z 同时显著，才值得另立「机制随时间变化」的检验。")
    return out


def ticket_ratio_test(lottery: str, *, frame: Optional[DrawFrame] = None,
                      block: int = DEFAULT_BLOCK, B: int = DEFAULT_B,
                      seed: int = DEFAULT_BOOT_SEED,
                      min_effect: float = DEFAULT_MIN_EFFECT) -> Dict[str, Any]:
    """
    A 轨主检验。返回 dict（**永远**带 `applicable` 与结论字段，不抛给调用方猜）。
    """
    if lottery not in LOTTO_LOTTERIES:
        return {"lottery": lottery, "applicable": False,
                "note": (f"A 轨需要「一等奖注数/二等奖注数/总投注额」三列；"
                         f"{lottery} 属数字型/七星彩 —— 走 A′ 轨"
                         f"（`digital_ticket_ratio_test`）或 C/D 轨。")}
    fr = frame if frame is not None else load_draw_frame(lottery)
    if fr is None:
        return {"lottery": lottery, "applicable": False,
                "note": "样本不足或数据缺失（需 ≥200 期且三列齐全）"}

    n = fr.n
    p = prize_probs(lottery)
    cal = ticket_calibration(fr, block=block, B=B, seed=seed)

    # 主口径：免票量（分母 = 二等注数反推的票量 × p1）
    den_calib = fr.y2 * (p["一等"] / p["二等"])
    r_calib = _ratio_block_ci(fr.y1, den_calib, block=block, B=B, seed=seed)
    R_calib = _pack(
        "R_calib（免票量，主用）", r_calib, usable=True,
        note="分母用二等注数反推票量，对追加/复式/多倍/记账差异全免疫")

    # 对照口径：销售额/2
    den_sales = (fr.sales / 2.0) * p["一等"]
    r_sales = _ratio_block_ci(fr.y1, den_sales, block=block, B=B, seed=seed)
    R_sales = _pack(
        "R_sales（销售额/2）", r_sales, usable=bool(cal["ok"]),
        note=("票量前提成立，可作交叉验证" if cal["ok"] else
              f"票量前提不成立（factor={cal['factor']:.4f}）→ **不可用于判定**；"
              f"其 R 已内含 {100*(cal['factor']-1):.2f}% 的系统性低估"))

    # 分段（β 稳定性）。⚠️ 某半段 y2 全为 0 ⇒ 该段分母和为 0 ⇒ R 无定义。
    # 注意这**不抛异常**：两个操作数都是 np.float64，numpy 的除零给 inf（分子非零）
    # 或 nan（分子也为 0）。危害是 inf/nan 会经 `atomic_write_json` 写进缓存 ——
    # 而 NaN 不是合法 JSON 字面量，浏览器 `JSON.parse` / `jq` / 严格解析器会失败。
    # 因此这里显式记 None（不可用），并让 halves_ok 把它当"不可判"。
    h = n // 2
    seg: Dict[str, Any] = {}
    seg_NA: List[str] = []
    for tag, sl in (("前半", slice(0, h)), ("后半", slice(h, n))):
        d = float(den_calib[sl].sum())
        if d > 0:
            seg[tag] = round(float(fr.y1[sl].sum()) / d, 5)
        else:
            seg[tag] = None
            seg_NA.append(tag)

    # 逐年 R：漂移诊断。**必须在判定里显式体现** —— 实测双色球前半 1.0416 /
    # 后半 0.9665，全样本 pooled 出的 0.9939 会把一个明显的时间漂移抹平。
    years: Dict[str, float] = {}
    year_masks: Dict[str, np.ndarray] = {}
    for y in sorted({d.year for d in fr.dates}):
        m = np.array([d.year == y for d in fr.dates])
        if int(m.sum()) >= 20 and den_calib[m].sum() > 0:
            years[str(y)] = round(float(fr.y1[m].sum() / den_calib[m].sum()), 4)
            year_masks[str(y)] = m
    drift = round(float(max(years.values()) - min(years.values())), 4) if len(years) >= 2 else None

    # —— 把"看着有漂移"变成数字 ——
    # ① 逐年 R 与年份的 Spearman（对每年 R 的重尾噪声稳健，不做分布假设）
    # ② 最早 25% 年 vs 最近 25% 年 的 R 差 + 独立近似 CI（比例预先定死，不做挑选）
    trend = _drift_test(fr, years, year_masks, den_calib, block=block, B=B, seed=seed)

    # r_t 自相关（y2=0 的期（必然 y1=0）不计入，避免 0/0）
    ok_r = den_calib > 0
    ac = autocorr(fr.y1[ok_r] / den_calib[ok_r])

    # 判定一律用长块 CI（吸收逐年漂移；短块会假窄）
    se_long = r_calib["se_long"]
    ci_hi = r_calib["ci_hi_long"]
    ci_lo = r_calib["ci_lo_long"]
    R = R_calib["R"]
    z = (R - 1.0) / se_long if se_long and se_long == se_long and se_long > 0 else float("nan")
    ci_hi_ok = ci_hi == ci_hi and ci_hi < 1.0
    halves_ok = all(v is not None and v < 1.0 for v in seg.values())
    same_dir = (not R_sales["usable"]) or (r_sales["R"] < 1.0)
    effect_ok = R < 1.0 - min_effect
    gap = bool(ci_hi_ok and halves_ok and same_dir and effect_ok)

    warn = []
    tp = trend.get("趋势_p")
    dz = trend.get("差_z")
    if tp is not None and tp < 0.05 and dz is not None and abs(dz) >= 2.0:
        warn.append(f"逐年 R 有显著时间趋势且端点对比也显著"
                    f"（Spearman ρ={trend.get('趋势_Spearman_rho')}, p={tp}；"
                    f"早期 {trend.get('早期', {}).get('R')} → 近期 "
                    f"{trend.get('近期', {}).get('R')}，差 z={dz}）"
                    f"→ 全样本 pooled R 会被抹平，建议单独检验机制随时间变化。")
    elif tp is not None and tp < 0.05:
        warn.append(f"**线索（未达判定门槛）**：逐年 R 的 Spearman 趋势 p={tp}"
                    f"（ρ={trend.get('趋势_Spearman_rho')}），但预先定死比例的"
                    f"早期 {trend.get('早期', {}).get('R')} → 近期 "
                    f"{trend.get('近期', {}).get('R')} 端点对比只有 z={dz}"
                    f"（|z|<2）→ 幅度不足，不作为「缝隙随时间出现」的证据。")
    elif drift is not None:
        warn.append(f"逐年 R 极差 {drift:.3f}，但趋势不显著（Spearman p={tp}）——"
                    f"单年 R 的经验 sd ≈ 0.10，此量级极差属正常噪声。")
    if not same_dir and R_sales["usable"]:
        warn.append(f"两口径方向不一致（R_calib={R_calib['R']}, R_sales={r_sales['R']}）")

    if not ci_hi_ok:
        note = f"CI95(长块) 上界 {ci_hi:.4f} ≥ 1 → 与公平开奖相容，不判缝隙"
    elif not effect_ok:
        note = f"R={R:.4f} 未达最小效应量（R<{1-min_effect:.2f}）"
    elif not halves_ok and seg_NA:
        # 区分「两段方向不一致」与「某段根本算不出来」——后者是数据问题，
        # 读者若混为一谈会误以为机制不稳。
        note = (f"分段不可用（{'/'.join(seg_NA)} 段二等注数全为 0、分母为 0）"
                f"→ 无法判定前后半段是否同向，不判缝隙")
    elif not halves_ok:
        note = f"前后半段不一致（{seg}）→ 不稳定，不判缝隙"
    elif not same_dir:
        note = "两口径方向不一致"
    else:
        note = (f"R={R:.4f}、CI95(长块) 上界 {ci_hi:.4f}<1、前后半段均<1 "
                f"→ **检出注数侧缝隙**（真实开出组合被买得比随机更少）")

    return {
        "lottery": lottery,
        "applicable": True,
        "n": n,
        "p1": p["一等"],
        "p2": p["二等"],
        "期望总注数": round(float(den_calib.sum()), 3),
        "实际总注数": round(float(fr.y1.sum()), 3),
        "票量校准": cal,
        "主口径": R_calib,
        "对照口径": R_sales,
        "模型校准": _conditional_diagnostic(fr),
        "分段": seg,
        "分段不可用": seg_NA or None,
        "逐年R": years,
        "逐年趋势": trend,
        "漂移极差": drift,
        "r_t自相关": {k: (round(v, 4) if v == v else None) for k, v in ac.items()},
        "判定": {
            "gap": gap,
            "R": round(R, 6),
            "z": round(z, 3) if z == z else None,
            "z_用": "长块 bootstrap se",
            "ci95": [round(ci_lo, 5), round(ci_hi, 5)],
            "ci95_短块": R_calib.get("ci95_短块"),
            "最小效应量": min_effect,
            "警告": warn or None,
            "note": note,
        },
    }


# ==================================================================== A′ 轨
def digital_ticket_ratio_test(lottery: str, *, block: int = DEFAULT_BLOCK,
                              B: int = DEFAULT_B, seed: int = DEFAULT_BOOT_SEED,
                              min_effect: float = DEFAULT_MIN_EFFECT,
                              ) -> Dict[str, Any]:
    """
    A′ 轨 · 数字型 / 七星彩的注数比检验 —— 与 A 轨**同构、同样模型无关**。

      R = Σ(中奖注数) / Σ(票量 · p)，  票量 = 销售额 ÷ 2（数字型全部 2 元一注、无追加）

    为什么 E[R] = 1 依然与玩家偏好无关：某期开出组合 C 的中奖注数就是"买了 C 的注数"，
    C 均匀 ⇒ 期望 = 票量 × p。**与玩家怎么选号无关。**

    只对 `sales_grade.A_PRIME_LOTTERIES`（排列5 / 七星彩）生效；
    排列3 / 福彩3D 显式降级 —— 它们的销售额覆盖全部玩法，`Σ注数/Σ(N·p)` 量的是
    **投注结构占比**而不是缝隙（实测三玩法份额之和 0.9889 ≈ 1），理由写在返回值里。

    七星彩只用**现行规则段**（≥ 2020-10-11）：2020-10-11 起组合空间由 10^7 变 1.5e7，
    全段混算实测给出 一等 R=1.19 / 三等 R=2.92 的假信号。
    """
    from ev.mechanism import sales_grade as SG

    out: Dict[str, Any] = {"lottery": lottery, "轨": "A′（数字型注数比）"}
    if lottery not in SG.A_PRIME_LOTTERIES:
        out["applicable"] = False
        out["note"] = (SG.excluded_a_reason(lottery)
                       or f"{lottery} 不在 A′ 轨适用清单（{SG.A_PRIME_LOTTERIES}）内。")
        return out

    fr = SG.load_digital_frame(lottery)
    if fr is None:
        out["applicable"] = False
        out["note"] = "销售奖级 CSV 缺失或为空"
        return out

    p_all = prize_probs_digital(lottery)
    mask = fr.usable_mask()
    n = int(mask.sum())
    if n < 50:
        out["applicable"] = False
        out["note"] = f"现行段且销售额>0 的期数只有 {n} 期，样本不足"
        return out

    N = fr.sales[mask] / 2.0
    cats = SG.A_PRIME_CATEGORY[lottery]
    levels: Dict[str, Any] = {}
    for cat in cats:
        if cat not in fr.notes:
            levels[cat] = {"usable": False, "note": "该奖级列在本帧缺失"}
            continue
        pk = SG.prob_key(lottery, cat)
        if pk not in p_all:
            levels[cat] = {"usable": False,
                           "note": f"概率表缺键 {pk}（grade_probabilities）"}
            continue
        y = fr.notes[cat][mask]
        ok = ~np.isnan(y)
        if int(ok.sum()) < 50:
            levels[cat] = {"usable": False, "note": "注数列缺失过多"}
            continue
        den = N[ok] * p_all[pk]
        rr = _ratio_block_ci(y[ok], den, block=block, B=B, seed=seed)
        h = len(y[ok]) // 2
        halves = [float(y[ok][:h].sum() / den[:h].sum()),
                  float(y[ok][h:].sum() / den[h:].sum())] if h > 0 else []
        packed = _pack(f"R_{cat}", rr, usable=True,
                       note=f"p={p_all[pk]:.6g}（ev/payout.grade_probabilities[{pk}]）")
        packed["n"] = int(ok.sum())
        packed["分段"] = [round(v, 5) for v in halves]
        levels[cat] = packed

    prim = SG.A_PRIME_PRIMARY[lottery]
    prim_fallback = None
    if not (levels.get(prim) or {}).get("usable"):
        cand = [c for c, lv in levels.items() if lv.get("usable")]
        if not cand:
            out["applicable"] = False
            out["note"] = "所有奖级都不可用（注数列缺失或概率表缺键）"
            out["各奖级"] = levels
            return out
        prim = max(cand, key=lambda c: int(levels[c].get("n") or 0))
        prim_fallback = f"主口径奖级降级：{SG.A_PRIME_PRIMARY[lottery]} 不可用 → 改用 {prim}"
    P = levels.get(prim) or {}
    R = P.get("R", float("nan"))
    se_long = P.get("se_长块bootstrap")
    ci_lo = (P.get("ci95") or [float("nan")])[0]
    ci_hi = (P.get("ci95") or [float("nan"), float("nan")])[1]
    se = se_long if se_long else P.get("se_块bootstrap")
    z = (R - 1.0) / se if se and se > 0 and R == R else float("nan")
    ci_hi_ok = ci_hi == ci_hi and ci_hi < 1.0
    halves_ok = bool(P.get("分段")) and all(v < 1.0 for v in P["分段"])
    effect_ok = R == R and R < 1.0 - min_effect
    gap = bool(ci_hi_ok and halves_ok and effect_ok)

    # 全奖级一致性（七星彩专属）：各奖级独立观测同一机制。
    #   `全奖级低于1` = 全部奖级都偏冷（若真存在"筛出最少人买的组合"，六奖级应同时 <1）
    #   `各奖级R极差` = 交叉验证的离散度；极差小 ⇒ p 表/票量口径/数据都自洽
    dirs = [lv.get("R", float("nan")) for lv in levels.values()
            if lv.get("usable") and lv.get("R") == lv.get("R")]
    all_below = bool(dirs) and all(v < 1.0 for v in dirs)
    spread = (round(float(max(dirs) - min(dirs)), 5) if len(dirs) > 1 else None)

    # 免票量交叉校验：同一期两个奖级的注数比（对"销售额单位/追加"完全免疫）
    calib = _digital_calib(fr, mask, p_all, cats)

    # 派奖率（描述性）：Σ注数×单注奖金 / Σ销售额 —— 设计返奖率量级校验
    payout_rate = _digital_payout_rate(fr, mask)

    if not ci_hi_ok:
        note = f"CI95(长块) 上界 {ci_hi:.4f} ≥ 1 → 与公平开奖相容，不判缝隙"
    elif not effect_ok:
        note = f"R={R:.4f} 未达最小效应量（R<{1-min_effect:.2f}）"
    elif not halves_ok:
        note = f"前后半段不一致（{P.get('分段')}）→ 不稳定，不判缝隙"
    else:
        note = (f"R={R:.4f}、CI95(长块) 上界 {ci_hi:.4f}<1、前后半段均<1 "
                f"→ **检出注数侧缝隙**")

    idx_ok = np.nonzero(mask)[0]
    return {
        "lottery": lottery,
        "轨": "A′（数字型注数比）",
        "applicable": True,
        "n": n,
        "票量口径": "销售额 ÷ 2（数字型全部 2 元一注、无追加）",
        "规则段": (SG.RULE_CUTOFF.get(lottery).isoformat()
                   if lottery in SG.RULE_CUTOFF else "全段（无规则变更）"),
        "期号范围": ([fr.issues[i] for i in (idx_ok[0], idx_ok[-1])]
                     if n else None),
        "各奖级": levels,
        "主口径": P,
        "主口径奖级": prim,
        "主口径降级": prim_fallback,
        "免票量校验": calib,
        "派奖率": payout_rate,
        "全奖级低于1": (all_below if len(dirs) > 1 else None),
        "各奖级R极差": spread,
        "全奖级含义": ("六奖级观测同一张票的同一机制：若真「筛出最少人买的组合」，"
                       "各奖级 R 应同时 <1；`各奖级R极差` 小则说明 p 表/票量口径/数据自洽。"),
        "判定": {
            "gap": gap,
            "R": round(float(R), 6) if R == R else None,
            "z": round(float(z), 3) if z == z else None,
            "z_用": "长块 bootstrap se",
            "ci95": P.get("ci95"),
            "最小效应量": min_effect,
            "note": note,
        },
        "口径限制": (
            "排列3 / 福彩3D **不适用**：" +
            "；".join(f"{k}——{v[:60]}…" for k, v in SG.EXCLUDED_A_REASON.items())
            if lottery in SG.EXCLUDED_A_REASON else
            "销售额÷2 = 该彩种全部票量（单一玩法、无追加）；七星彩六奖级共用同一张票。"),
    }


def prize_probs_digital(lottery: str) -> Dict[str, float]:
    """数字型奖级 → 概率（`ev/payout.grade_probabilities` 为唯一权威）。"""
    from ev.payout import grade_probabilities
    g = grade_probabilities(lottery)
    if not g:
        raise ValueError(f"{lottery} 无奖级概率表")
    return {k: float(v) for k, v in g.items()}


def _digital_calib(fr, mask: np.ndarray, p_all: Dict[str, float],
                   cats) -> Optional[Dict[str, Any]]:
    """
    免票量交叉校验：用**两个奖级的注数比**反推，与 p 的比值对照。
    对「销售额单位 / 追加 / 复式记账」完全免疫，是 A′ 轨唯一的独立旁证。
    单奖级彩种（排列5）→ None，显式说明。
    """
    from ev.mechanism import sales_grade as SG
    usable = [c for c in cats
              if c in fr.notes and not np.isnan(fr.notes[c][mask]).all()]
    if len(usable) < 2:
        return {"applicable": False,
                "note": "只有一个奖级，免票量口径不可用（该口径需要两个奖级反推）"}
    a, b = usable[0], usable[-1]
    pa = p_all.get(SG.prob_key(fr.lottery, a))
    pb = p_all.get(SG.prob_key(fr.lottery, b))
    if pa is None or pb is None:
        return {"applicable": False, "note": "概率表缺键"}
    ya = fr.notes[a][mask]
    yb = fr.notes[b][mask]
    ok = (~np.isnan(ya)) & (~np.isnan(yb)) & (yb > 0)
    if int(ok.sum()) < 50:
        return {"applicable": False, "note": "两奖级同时有值的期数不足"}
    R = float(ya[ok].sum() / yb[ok].sum()) * (pb / pa)
    return {"applicable": True, "A": a, "B": b, "n": int(ok.sum()),
            "R_免票量": round(R, 5),
            "note": (f"R=(Σ{a}注数/Σ{b}注数)·(p_{b}/p_{a})，免票量；"
                     f"与主口径同向且接近 1 ⇒ 销售额单位与 p 表互相印证")}


def _digital_payout_rate(fr, mask: np.ndarray) -> Optional[Dict[str, Any]]:
    """描述性：设计返奖率量级校验（**不参与判定**）。"""
    tot_sales = float(fr.sales[mask].sum())
    if tot_sales <= 0:
        return None
    paid = 0.0
    for cat, y in fr.notes.items():
        pri = fr.prizes.get(cat)
        if pri is None:
            continue
        y = y[mask]
        pri = pri[mask]
        ok = (~np.isnan(y)) & (~np.isnan(pri))
        paid += float((y[ok] * pri[ok]).sum())
    return {"返奖额/销售额": round(paid / tot_sales, 4),
            "含义": "设计返奖率量级校验（排列3/3D ≈ 0.51，排列5/七星彩 ≈ 0.5）；"
                    "**不参与判定**"}

