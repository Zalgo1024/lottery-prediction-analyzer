# -*- coding: utf-8 -*-
r"""D 轨 · 派奖期 vs 平常期（用户已确认：用它代理「特别时期」，地理维度不做）。

## 它检验什么

用户的核心假设里有一条**分段**版本：
「平常期是筛出全国购买最少的组合；**特别时期**则在此之上，让特定地区中奖
以筹备特定需求资金。」
地理维度在本数据集里不可得（没有按省的销量/中奖分布），所以按事前确认的口径，
用**官方派奖期**做「特别时期」的代理。

检验：派奖期的 R（注数比）是否**显著低于**同规则段的平常期。
  · 派奖期 R 显著更低 → 「特别时期机制不同」获得支持；
  · 无差异 → 只说明**在本次可用样本量下未检出**，不是"证明没有"。

## 数据与功效（必须如实说）

唯一有派奖列的彩种是**七星彩**（`七星彩销售奖级数据.csv` 的 12 列派奖字段）：
  · 派奖期仅 **21 期**（2021-08-27~2021-09-14 共 9 期、2023-08-11~2023-09-05 共 12 期）
  · 同规则段（≥2020-10-11）且有销售额的平常期 **880 期**
  · 21 期 vs 880 期 ⇒ **功效极低**：即便真有偏置，本样本也几乎不可能检出。
    所以 D 轨的输出以「功效不足、未检出」为主，**不得**被读成"特别时期与平常期相同"。

福彩3D / 排列3 / 排列5 的 CSV 无派奖列；双色球 / 大乐透的历史 CSV 亦无派奖列
⇒ 一律显式 `applicable: False`，**不硬凑代理**。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np

from ev.mechanism import sales_grade as SG
from ev.mechanism.common import DEFAULT_B, DEFAULT_BLOCK, DEFAULT_BOOT_SEED

# 「特别时期」的其它已知形态（本数据无法覆盖，写出来是为了不让人以为漏检）
_OTHER_SPECIAL = (
    "年度大派奖（双色球/大乐透偶有）——历史 CSV 无派奖列，无法识别",
    "奖池异常滚存期——可由 `总投注额/奖池奖金` 近似，但已超出「派奖期代理」口径，未纳入",
    "地理/省份维度——数据集不含按省销量与中奖分布，用户已确认不做",
)


def _ratio_ci(num: np.ndarray, den: np.ndarray, *, block: int, B: int,
              seed: int) -> Dict[str, float]:
    from ev.mechanism.gap_a_ticket_ratio import _ratio_block_ci
    return _ratio_block_ci(num, den, block=block, B=B, seed=seed)


def _detect_limit(levels: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """两组的 se 合成后，能检出的最小 |ΔR| 量级（≈ 2·se_差）。"""
    for r in levels:
        a, b = r.get("派奖期", {}), r.get("平常期", {})
        if a.get("usable") and b.get("usable"):
            se = math.sqrt((a.get("se_long") or 0) ** 2 + (b.get("se_long") or 0) ** 2)
            return {"奖级": r["奖级"], "se_差": round(se, 4),
                    "可检出|ΔR|下限": round(2 * se, 4)}
    return None


def special_period_report(lottery: str = "七星彩", *, B: int = DEFAULT_B,
                          block: int = DEFAULT_BLOCK,
                          seed: int = DEFAULT_BOOT_SEED,
                          alpha: float = 0.05) -> Dict[str, Any]:
    """D 轨报告。永远返回带 `applicable` 的 dict。"""
    out: Dict[str, Any] = {"lottery": lottery, "轨": "D（派奖期 vs 平常期）"}

    if lottery not in SG.SALES_CSV:
        out["applicable"] = False
        out["note"] = (f"{lottery} 无「销售奖级数据.csv」→ D 轨不适用"
                       f"（本轨不硬凑代理）。")
        out["未覆盖的特别时期形态"] = list(_OTHER_SPECIAL)
        return out

    fr = SG.load_digital_frame(lottery)
    if fr is None:
        out["applicable"] = False
        out["note"] = "销售奖级 CSV 缺失"
        return out

    ap = SG.award_periods(fr)
    if not ap.get("applicable"):
        out["applicable"] = False
        out["note"] = ap.get("note", "无派奖列")
        out["未覆盖的特别时期形态"] = list(_OTHER_SPECIAL)
        return out

    mask = fr.usable_mask()
    aw = ap["mask"] & mask
    nm = (~ap["mask"]) & mask
    n_aw, n_nm = int(aw.sum()), int(nm.sum())
    if n_aw < 5:
        out["applicable"] = False
        out["note"] = (f"派奖期（同规则段且有销售额）只有 {n_aw} 期，"
                       f"连点估计都不可靠 → D 轨不适用。")
        out["未覆盖的特别时期形态"] = list(_OTHER_SPECIAL)
        return out

    from ev.payout import grade_probabilities
    p_all = grade_probabilities(lottery)

    levels: List[Dict[str, Any]] = []
    for cat in SG.A_PRIME_CATEGORY.get(lottery, ()):
        if cat not in fr.notes:
            continue
        pk = SG.prob_key(lottery, cat)
        p = p_all.get(pk)
        if p is None:
            continue
        y = fr.notes[cat]
        row: Dict[str, Any] = {"奖级": cat, "p": p}
        for tag, m in (("派奖期", aw), ("平常期", nm)):
            ok = m & (~np.isnan(y))
            if int(ok.sum()) < 5:
                row[tag] = {"n": int(ok.sum()), "usable": False}
                continue
            num = np.nan_to_num(y[ok], nan=0.0)
            den = fr.sales[ok] / 2.0 * p
            rr = _ratio_ci(num, den, block=min(block, max(2, len(num) // 3)),
                           B=B, seed=seed + (1 if tag == "派奖期" else 2))
            row[tag] = {"n": int(ok.sum()), "usable": True,
                        "R": round(rr["R"], 5),
                        "se_long": round(rr["se_long"], 5)
                        if rr["se_long"] == rr["se_long"] else None,
                        "ci95": [round(rr["ci_lo_long"], 5),
                                 round(rr["ci_hi_long"], 5)]}
        a, b = row.get("派奖期", {}), row.get("平常期", {})
        if a.get("usable") and b.get("usable"):
            se = math.sqrt((a["se_long"] or 0) ** 2 + (b["se_long"] or 0) ** 2)
            d = a["R"] - b["R"]
            row["差_派奖减平常"] = round(d, 5)
            row["差_se"] = round(se, 5)
            row["差_z"] = round(d / se, 3) if se > 0 else None
            from scipy import stats as sps
            row["差_p"] = (round(float(2 * (1 - sps.norm.cdf(abs(d / se)))), 5)
                           if se > 0 else None)
            row["判_派奖更低"] = bool(se > 0 and d / se < -1.96)
        levels.append(row)

    flags = [r for r in levels if r.get("判_派奖更低")]
    if flags:
        verdict = ("派奖期的注数比**显著低于**平常期（" +
                   "、".join(f"{r['奖级']} Δz={r['差_z']}" for r in flags) +
                   "）→ 支持「特别时期机制不同」。⚠️ 派奖期样本仅 "
                   f"{n_aw} 期，务必用独立数据复核后再据以行动。")
    else:
        verdict = (f"派奖期（{n_aw} 期）与平常期（{n_nm} 期）各奖级 R 的差"
                   f"均未达 |z|≥1.96 → **在本次可用样本量下未检出差异**。"
                   f"⚠️ 派奖期只有 {n_aw} 期，**功效极低**，"
                   f"这**不能**读成「特别时期与平常期相同」。")

    return {
        "lottery": lottery,
        "轨": "D（派奖期 vs 平常期）",
        "applicable": True,
        "代理口径": ("用官方**派奖期**代理「特别时期」（任一「*派奖注数>0」）；"
                     "地理维度不做——数据不含按省销量/中奖分布（用户已确认）"),
        "对照组": "同规则段（≥" + SG.QXC_RULE_CUTOFF.isoformat() + "）内的非派奖期",
        "n_派奖期": n_aw, "n_平常期": n_nm,
        "派奖期期号": ap.get("期号"),
        "派奖期日期": ap.get("日期"),
        "各奖级": levels,
        "结论": verdict,
        "功效": {
            "n_派奖期": n_aw, "n_平常期": n_nm,
            "检出下限": _detect_limit(levels),
            "说明": (f"派奖期仅 {n_aw} 期 ⇒ 对 R 的偏置检出下限远高于平常期；"
                     f"小于该量级的真实偏置本样本检不出，"
                     f"所以「未检出差异」不能读成「没有差异」。"),
        },
        "未覆盖的特别时期形态": list(_OTHER_SPECIAL),
        "alpha": alpha,
        "gap": bool(flags),
        "gap说明": ("D 轨的 `gap` 只表示「派奖期 vs 平常期存在差异」，"
                    "它**不是**「存在机制缝隙」的判据；判缝隙见 A/A′/B 轨。"),
    }


__all__ = ["special_period_report"]
