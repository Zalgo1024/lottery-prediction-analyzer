# -*- coding: utf-8 -*-
"""四轨汇总与裁决。

裁决语义（与"允许量出缝隙很窄"这条原则一致）：

  乐透型（双色球/大乐透）判定轨 = A（注数比）、B（开出号分位）
  数字型（排列5/七星彩）  判定轨 = A′（注数比，直读销售奖级 CSV）
  排列3 / 福彩3D          判定轨 = 无（销售额覆盖全部玩法，注数比测的是投注份额）

  status = "strong"  判定轨**两轨**都检出   ← 数字型只有一轨，故永远到不了 strong
  status = "weak"    只有一轨检出
  status = "none"    都没检出  ← **这是合法且常见的结论，不是失败**

**C / D 轨一律不参与判定**：
  · C（位级/分区结构偏差）成因太多（规则变更/数据源缺陷/设备/多重比较），
    无法与"机制筛选"区分 → 只作强化与排查线索；
  · D（派奖期 vs 平常期）派奖期样本极少，功效低 → 只报"差异/无差异"，不判缝隙。

方向一致性是硬要求：A 轨要 R<1，B 轨要 ū<0.5，两者都说"偏冷"才叫同向。
γ 只是把两条观测折到同一标尺上的换算（依赖模型），
**判定本身用模型无关的 R 与 ū**。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

from ev.mechanism.common import DIGITAL_LOTTERIES, LOTTO_LOTTERIES, load_draw_frame
from ev.mechanism.gap_a_ticket_ratio import (
    digital_ticket_ratio_test, ticket_ratio_test,
)
from ev.mechanism.gap_b_drawn_quantile import (
    MIN_TRAIN_DEFAULT, WF_BLOCK_DEFAULT, fit_gamma, quantile_test,
    walk_forward_quantiles,
)
from ev.mechanism.gap_c_structural import structural_bias_report
from ev.mechanism.gap_d_special_period import special_period_report
from ev.mechanism import gap_cache

TRACKS_LOTTO: Sequence[str] = ("A", "B", "C", "D")
TRACKS_DIGITAL: Sequence[str] = ("A′", "C", "D")
# C 轨的默认规模（单彩种 <1s；报告与测试都用这个）
C_B = 2000
C_MC = 100_000


def measure_gap(lottery: str, *, tracks: Optional[Sequence[str]] = None,
                use_cache: bool = True, force: bool = False,
                min_train: int = MIN_TRAIN_DEFAULT, step: int = WF_BLOCK_DEFAULT,
                min_effect: float = 0.03,
                c_B: int = C_B, c_mc: int = C_MC) -> Dict[str, Any]:
    """
    对单个彩种跑全部适用轨道并给出裁决。

    `tracks` 可显式指定（如 `("A",)` 只跑 A，秒级、无模型依赖）；
    默认按彩种类型自动选（见模块 docstring）。
    """
    digital = lottery in DIGITAL_LOTTERIES
    if not digital and lottery not in LOTTO_LOTTERIES:
        return {"lottery": lottery, "applicable": False,
                "status": "not-applicable",
                "note": (f"{lottery} 既不在乐透型 {LOTTO_LOTTERIES}，"
                         f"也不在数字型 {DIGITAL_LOTTERIES} 的适用清单内")}
    tr = tuple(tracks) if tracks else (TRACKS_DIGITAL if digital else TRACKS_LOTTO)
    default_tracks = (digital and set(tr) == set(TRACKS_DIGITAL)) or \
                     ((not digital) and set(tr) == set(TRACKS_LOTTO))

    if digital:
        from ev.mechanism import sales_grade as SG
        fr_d = SG.load_digital_frame(lottery)
        n = fr_d.n if fr_d is not None else 0
        last = fr_d.issues[-1] if (fr_d is not None and fr_d.issues) else ""
        fr = None
    else:
        fr = load_draw_frame(lottery)
        if fr is None:
            return {"lottery": lottery, "applicable": False,
                    "status": "not-applicable", "note": "样本不足或数据缺失"}
        n = fr.n
        last = fr.issues[-1]

    sig = gap_cache.make_sig(lottery, n, last,
                             min_train=min_train, step=step,
                             min_effect=min_effect, c_B=c_B, c_mc=c_mc)
    if use_cache and not force and default_tracks:
        hit = gap_cache.load(lottery, sig)
        if hit is not None:
            hit["_from_cache"] = True
            return hit

    out: Dict[str, Any] = {
        "lottery": lottery, "applicable": True, "n": n, "sig": sig,
        "_from_cache": False, "轨类型": "数字型" if digital else "乐透型",
        "track结果": {},
    }

    # ---------------- A / A′ ----------------
    a = None
    if digital:
        ap = digital_ticket_ratio_test(lottery, min_effect=min_effect) if "A′" in tr else None
        if ap is not None:
            out["track结果"]["A′"] = ap
    else:
        a = ticket_ratio_test(lottery, frame=fr, min_effect=min_effect) if "A" in tr else None
        if a is not None:
            out["track结果"]["A"] = a

    # ---------------- B（仅乐透型）----------------
    wf = qtest = None
    if (not digital) and "B" in tr:
        wf = walk_forward_quantiles(lottery, frame=fr, min_train=min_train, step=step)
        qtest = quantile_test(wf, min_effect=min_effect)
        out["track结果"]["B"] = qtest
        out["B过程"] = {k: wf.get(k) for k in
                        ("walk_forward", "n_leak", "min_train", "step",
                         "fit_fail", "n_total", "模型Go", "note", "cuts")}

    if not digital:
        out["gamma"] = fit_gamma(lottery, wf=wf or {}, qtest=qtest or {},
                                 a_result=a, frame=fr)
    else:
        out["gamma"] = {"lottery": lottery, "gamma_合并": None, "scale": None,
                        "note": "数字型无流行度模型 → γ 不可折算（A′ 轨已给出模型无关的 R）"}

    # ---------------- C ----------------
    if "C" in tr:
        try:
            out["track结果"]["C"] = structural_bias_report(lottery, B=c_B, mc=c_mc)
        except Exception as e:                            # noqa: BLE001
            out["track结果"]["C"] = {"lottery": lottery, "applicable": False,
                                     "gap": False, "note": f"C 轨执行失败：{e!r}"}

    # ---------------- D ----------------
    if "D" in tr:
        try:
            out["track结果"]["D"] = special_period_report(lottery)
        except Exception as e:                            # noqa: BLE001
            out["track结果"]["D"] = {"lottery": lottery, "applicable": False,
                                     "gap": False, "note": f"D 轨执行失败：{e!r}"}

    out["裁决"] = _verdict(out)
    if use_cache and default_tracks:
        try:
            gap_cache.save(lottery, out, sig)
        except Exception:                                # noqa: BLE001
            pass
    return out


def _verdict(out: Dict[str, Any]) -> Dict[str, Any]:
    """汇总判定。**只用判定轨（A/A′/B）**，C/D 只作附注。"""
    t = out.get("track结果") or {}
    digital = out.get("轨类型") == "数字型"
    a = t.get("A") or {}
    ap = t.get("A′") or {}
    b = t.get("B") or {}
    c = t.get("C") or {}
    d = t.get("D") or {}

    # 判定轨：乐透型 = A + B；数字型 = A′（只有一轨）
    spots: List[tuple] = []
    if digital:
        spots.append(("A′", bool(ap.get("applicable")), ap))
    else:
        spots.append(("A", bool(a.get("applicable")), a))
        if b:
            spots.append(("B", bool(b.get("applicable")), b))
    usable = [s for s in spots if s[1]]
    labels = [s[0] for s in usable]
    hits = [s[0] for s in usable if _track_gap(s[0], s[2])]

    if not usable:
        status = "no-track"
    elif len(hits) == len(labels) and len(labels) > 1:
        status = "strong"
    elif hits:
        status = "weak"
    else:
        status = "none"

    dirs: List[bool] = []
    for lab, ok, tr in usable:
        if not ok:
            continue
        if lab == "B":
            dirs.append(tr["mean_u"] < 0.5)
        else:
            Rv = (tr.get("主口径") or {}).get("R")
            if Rv is not None:
                dirs.append(Rv < 1.0)
    # 方向一致性只在**有两轨及以上**时才有意义
    consistent: Optional[bool] = (all(dirs) if len(dirs) >= 2 else None)

    main = (ap if digital else a)
    n_judging = main.get("n") if main.get("applicable") else None

    return {
        "status": status,
        "gap_present": bool(hits),
        # 判定轨 = **本次真的判过的轨**。无判定轨的彩种必须是 []，不能回落到
        # 「本应存在的轨」——否则表格里会出现「判定轨 = A′」与「裁决 = 无判定轨」
        # 同行自相矛盾，让读者误以为「排列3 有 A′ 轨但没检出」。
        "判定轨": labels,
        # 应有判定轨 = 按彩种类型本应存在的轨（即使这次不可用）——诊断用。
        "应有判定轨": [s[0] for s in spots],
        "检出轨": hits,
        "A轨": bool((a.get("判定") or {}).get("gap")),
        "A′轨": bool((ap.get("判定") or {}).get("gap")),
        "B轨": bool(b.get("gap")),
        "C轨": bool(c.get("gap")),          # 恒 False（C 不判缝隙）
        "D轨": bool(d.get("gap")),          # 只表示"派奖期有差异"
        "方向一致": consistent,
        "n_判定轨": n_judging,
        "R": (main.get("主口径") or {}).get("R"),
        "R_ci95": (main.get("主口径") or {}).get("ci95"),
        "mean_u": b.get("mean_u") if b.get("applicable") else None,
        "u_ci95": b.get("ci95") if b.get("applicable") else None,
        "gamma": ((out.get("gamma") or {}).get("gamma_合并") or {}).get("gamma"),
        "note": _verdict_note(status, out, labels, hits, consistent, dirs),
    }


def _track_gap(lab: str, tr: Dict[str, Any]) -> bool:
    return bool(tr.get("gap")) if lab == "B" else bool((tr.get("判定") or {}).get("gap"))


def _verdict_note(status: str, out: Dict[str, Any], labels: List[str],
                  hits: List[str], consistent: Optional[bool],
                  dirs: List[bool]) -> str:
    t = out.get("track结果") or {}
    a = t.get("A") or {}
    ap = t.get("A′") or {}
    b = t.get("B") or {}
    c = t.get("C") or {}
    d = t.get("D") or {}
    digital = out.get("轨类型") == "数字型"

    if status == "no-track":
        return ("**没有可用的判定轨** → 既不判「有缝隙」也不判「无缝隙」。"
                "排列3 / 福彩3D 的销售额覆盖全部玩法，注数比只能量出投注结构占比，"
                "本轨对它们不适用；它们的 C 轨结果见下表（只作数据体检）。")

    if status == "strong":
        return ("A、B 两轨均检出且同向：真实开出的组合在**注数侧**与**分位侧**"
                "都比随机更冷。这是「缝隙存在」的证据（仍非承诺，见 γ 的模型依赖）。")
    if status == "weak":
        return (f"仅 {'/'.join(hits)} 轨检出，其余判定轨未过门槛 → 记为**弱证据**，"
                f"不做「存在机制缝隙」的结论。")

    main = ap if digital else a
    Rv = (main.get("主口径") or {}).get("R")
    uv = b.get("mean_u")
    tail = (f"（A′ 轨 R={Rv}；数字型无 B 轨分位轴）" if digital
            else f"（R={Rv}, ū={uv}）")
    extra: List[str] = []
    if consistent is False:
        extra.append("两判定轨**方向不一致**"
                     f"（A 轨说 R{'<1' if dirs[0] else '≥1'}、"
                     f"B 轨说 ū{'<0.5' if dirs[1] else '≥0.5'}）"
                     f"→ 连方向都不稳定，进一步支持「未见缝隙」")
    if c.get("applicable") and c.get("实质偏离"):
        extra.append("C 轨有实质结构偏离（须先排查数据/规则）")
    elif c.get("applicable") and c.get("分段不一致"):
        extra.append("C 轨全样本显著但**分段不一致**（数据/规则问题，非机制）")
    if d.get("applicable") and d.get("gap"):
        extra.append("D 轨派奖期与平常期有差异（样本小，仅供参考）")
    elif d.get("applicable"):
        extra.append("D 轨派奖期样本过小、未检出差异")
    return ("判定轨均未过门槛" + tail + " → **未检出机制缝隙**。"
            "这表示在当前样本量与效应量门控下，开奖注数与真实开出组合的冷门度"
            "都与「公平开奖」相容；**不表示已证明公平**。"
            + ("附注：" + "；".join(extra) if extra else ""))


def measure_gap_global(lotteries: Sequence[str], *,
                       use_cache: bool = True, force: bool = False,
                       alpha: float = 0.05, **kw) -> Dict[str, Any]:
    """
    跨彩种汇总 + 一次 BH 控族错误率。

    ⚠️ 每个彩种**只贡献一个 p**，取它的**主判定轨**（乐透型 = A 轨，数字型 = A′ 轨）。
       不把"两个轨都测"当成"两个独立机会"；C/D 轨不参与 BH。
    """
    from credibility.baseline import benjamini_hochberg

    per: List[Dict[str, Any]] = []
    pairs: List[tuple] = []          # (逐彩种下标, p) —— 只有"有判定轨"的彩种进 BH
    for lot in lotteries:
        r = measure_gap(lot, use_cache=use_cache, force=force, **kw)
        per.append(r)
        p = _judging_pvalue(r)
        r["_判定轨_p"] = p
        if p is not None:
            pairs.append((len(per) - 1, p))

    pvals = [p for _, p in pairs]
    bh = benjamini_hochberg(pvals, alpha=alpha) if pvals else {
        "qvals": [], "rejected": [], "alpha": alpha}
    for r in per:
        r["BH_q"] = None
        r["BH_拒绝"] = False
    for (i, _), q, rej in zip(pairs, bh.get("qvals", []), bh.get("rejected", [])):
        per[i]["BH_q"] = round(float(q), 5) if q == q else None
        per[i]["BH_拒绝"] = bool(rej)

    return {
        "彩种数": len(per),
        "入BH彩种数": len(pairs),
        "alpha": alpha,
        "逐彩种": per,
        "检出彩种": [per[i]["lottery"] for (i, _), rej
                     in zip(pairs, bh.get("rejected", []))
                     if rej and (per[i].get("裁决") or {}).get("gap_present")],
        "note": (f"BH 只在 {len(pairs)} 个**有判定轨**的彩种之间控 FDR"
                 f"（排列3/福彩3D 无判定轨，不入 BH，否则会虚增 m 使校正过松）；"
                 f"每彩种只取其主判定轨的 p（乐透型 = A 轨，数字型 = A′ 轨；C/D 不参与）。"),
    }


def _judging_pvalue(r: Dict[str, Any]) -> Optional[float]:
    """主判定轨的单侧 p（只看"更冷"方向）。模型无关。"""
    from scipy import stats as sps
    t = r.get("track结果") or {}
    key = "A′" if r.get("轨类型") == "数字型" else "A"
    tr = t.get(key) or {}
    if not tr.get("applicable"):
        return None
    R = (tr.get("主口径") or {}).get("R")
    se = (tr.get("主口径") or {}).get("se_长块bootstrap") \
        or (tr.get("主口径") or {}).get("se_块bootstrap")
    if R is None or not se:
        return None
    return float(sps.norm.cdf((R - 1.0) / se))
