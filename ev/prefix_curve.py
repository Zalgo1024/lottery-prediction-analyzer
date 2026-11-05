# -*- coding: utf-8 -*-
"""ev/prefix_curve.py —— 前缀 K 曲线回放（离线研究，**不接生产**）。

目的不是「找前几注优势」，而是**口径翻译**：项目看板 / 台账 / 回测全按
「系统整批 K 注」结算，而实际购买通常只取排序清单的前几注。K=1000 时
`≥5 元期率` 已饱和（≈1）且成本是前 3 注的 300 倍以上 ⇒ 那些数字既没有
区分力，也不代表真实投入。本模块把同一批冻结票序切成 `[:1] [:3] [:5] [:10] [:K]`
逐期重算，给出「实际买前 n 注」口径的读数。

★ 预先声明（跑数据之前固定，跑完不许回来改）
    · 主口径 n=3 是**用户授权的假设值，不是实测购买记录**；
    · 无逐期真实购买记录 ⇒ 全部结果挂「反事实」标签；
    · 「并非每次都买」不建模挑期行为（建模就等于事后挑），主判据仍按期按
      前 n 注结算；未参与维度只作披露，不许剔除后缩小分母；
    · 判据族规模 / α / 评估窗比例预先固定在 `PREREGISTRATION`，BH 只在评估窗做。

★ 与生产的关系：本模块 + `ev/order_freeze.py` 都不被生产出号 / 结算 / 看板
  import；不改 `ticket_size.json`、不改预算闸门、不改推送、不改下注量。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from ev.order_freeze import ORDER_FREEZE_VERSION

#: B：判决层的 n* 增补解析上界锚定（原 A 只用实测 Δ 反推，偏乐观）；
#:    并禁止 K 落在前缀档表内。判据变更 ⇒ 版本 bump，旧快照不回改、不重排。
PREREG_VERSION = "2026-11-05-B"
CODE_VERSION = PREREG_VERSION

#: ★ 主口径：用户授权的假设值。改它必须同时 bump `PREREG_VERSION` 并全量重跑。
PRIMARY_N = 3
PREFIX_KS = (1, 3, 5, 10)
ARMS = ("random", "existing_coverage", "tier_coverage")
BASELINE_ARM = "random"
TARGET_DEFAULT = {"kind": "grade", "grade": "四等"}
#: ★ 两个门槛的 p 差几个数量级（≥5元≈6% / ≥四等≈4.4e-4），理论上界必须分开算。
#:   共用同一个 p 会把 ≥5元 的可争取空间算成几乎 0，是口径错而不是结论。
PAYOUT5_TARGET = {"kind": "payout", "min_payout": 5.0}
METRICS = (("≥5元", "≥5元"), ("达标", "≥四等"))

SHADOW_PATH = Path(__file__).resolve().parent.parent / "logs" / "prefix_shadow.jsonl"
SNAPSHOT_DIR = Path(__file__).resolve().parent.parent / "logs" / "order_freeze"

PREREGISTRATION: Dict[str, Any] = {
    "版本": PREREG_VERSION,
    "锁定日": "2026-11-05",
    "性质": "独立探索性登记；不并入 config/preregistration.json 的主实验契约",
    "口径": "反事实：假设每期只买冻结票序的前 3 注；不是实测购买记录",
    "主口径n": PRIMARY_N,
    "前缀档": list(PREFIX_KS),
    "门槛": ["≥5元", "≥四等"],
    "臂": list(ARMS),
    "基线臂": BASELINE_ARM,
    "族规模": {"前缀档": len(PREFIX_KS), "门槛": 2,
               "臂对": len(ARMS) - 1,
               "检验数": len(PREFIX_KS) * 2 * (len(ARMS) - 1)},
    "名义α": 0.05,
    "探索窗比例": 0.6,
    "判据": ("评估窗（后 40%）逐期配对差 → BH(m=族规模) → 且可判性为「可判」才写「检出」；"
             "n* 用解析上界与实测 Δ 保守取大"),
    "效应量锚定": ("外部效应量 = theoretical_head(p_门槛, n)，即该档可争取空间的解析上界；"
                   "n=1 时恒为 0 ⇒ n*=∞ ⇒ 该档不可判。只用实测 Δ 反推 n* 会被单期噪声"
                   "与搜索批抬高，属偏乐观口径。两个门槛的 p 分开取，不共用。"),
    "停止规则": "未来开奖只追加、不回改历史；探索窗读数只作描述，不进 BH",
    "确认力": ("登记时尚未产生任何前缀回放读数；Step 0 的解析上界只作上界与诊断，"
               "不具确认力。历史窗一律挂探索性标签。"),
}

COUNTERFACTUAL_NOTE = (
    "★ 反事实：假设每期都买了冻结票序的前 n 注（主口径 n=3，用户授权的假设值，"
    "非实测）。项目没有逐期真实购买记录，「并非每次都买」这一维不建模挑期行为，"
    "只作披露；主判据仍按期结算，未参与期不许剔除后缩小分母。")
NOT_PURCHASED_NOTE = (
    "整批 K 注是系统出号口径，**非实际购买**：成本按 K×2 元计，与真实投入不对应；"
    "此列只作对照，不进主判据。")
TERMINOLOGY_NOTE = (
    "「未检出」只表示已测维度在该样本量下未见差异，不构成证明；"
    "非显著不等于无效果。")
PRIOR_ZERO_NOTE = (
    "固定奖层面 E[中奖注数]=n·p 与选哪些票无关 ⇒ 两臂期望回款相同；"
    "检出差异时先查实现缺陷 / 浮动奖分摊，不主张策略有效。")


def theoretical_head(p: float, n: int) -> float:
    """`min(1, n·p) − iid 随机基线`：该前缀档可争取空间的理论上界。

    n=1 时恒为 0；小 n 时是 `O((n·p)²)` 的二阶小量。★ 这是上界，不是实测收益。
    """
    n = int(n)
    # ★ n=1 时 min(1,p) − (1−(1−p)) 在浮点下会残留 ~1e-17，不是严格 0。
    #   但这一档的空间在数学上恒为 0；留着残差会让 n* 误得有限值、「理论不可判」漏计。
    if n <= 0 or n == 1:
        return 0.0
    p = float(p)
    iid = 1.0 - (1.0 - p) ** n
    return max(0.0, min(1.0, n * p) - iid)


def window_split(n_periods: int, *, ratio: Optional[float] = None) -> Dict[str, Any]:
    """评估窗切分的**单一真源**：判决层与稳定性层都走这里，不许两处各算一次。

    探索窗 = 前 60%（只描述、不进 BH）；评估窗对半切作时间窗稳定性（描述性）。
    ★ 前后半各需 ≥3 期，不足时显式标「未测」——写 0 等于把没测写成稳定。
    """
    r = float(PREREGISTRATION["探索窗比例"] if ratio is None else ratio)
    cut = int(n_periods * r)
    eval_len = int(n_periods) - cut
    half = eval_len // 2
    return {"探索窗期数": cut, "评估窗期数": eval_len,
            "前半": half, "后半": eval_len - half,
            "可测": bool(half >= 3 and eval_len - half >= 3)}


def _bounds(p: float, n: int) -> Dict[str, float]:
    return {"单注p": p, "n·p": round(n * p, 8),
            "理论上限": round(min(1.0, n * p), 8),
            "iid随机基线": round(1.0 - (1.0 - p) ** n, 8),
            "可争取空间上界": round(theoretical_head(p, n), 8)}


def _summarize(seq: Dict[str, List[Any]], n: int, K: int,
               p5: float, p_tier: float) -> Dict[str, Any]:
    periods = len(seq["期号"])
    if periods == 0:
        return {"期数": 0, "n": n}
    cost = float(sum(seq["成本"]))
    total = float(sum(seq["实得"]))
    ge5 = sum(seq["≥5元"]) / periods
    tier = sum(seq["达标"]) / periods
    return {
        "n": n,
        "期数": periods,
        "成本": round(cost, 2),
        "总实得": round(total, 2),
        "净收益": round(total - cost, 2),
        "单期平均净收益": round((total - cost) / periods, 4),
        "≥5元期率": round(ge5, 6),
        "≥四等期率": round(tier, 6),
        "单期平均中奖注数": round(sum(seq["中奖注数"]) / periods, 6),
        "剔除最大一期后净收益": round(total - cost - max(seq["实得"]), 2),
        # ★ 分门槛：两个 p 差几个数量级，合成一个「n·p」会把读数锚错量。
        "单注p": {"≥5元": p5, "≥四等": p_tier},
        "上界": {"≥5元": _bounds(p5, n), "≥四等": _bounds(p_tier, n)},
        "饱和": bool(ge5 >= 0.999 or tier >= 0.999),
        "非实际购买": bool(n == K),
        "饱和说明": ("期率≈1 ⇒ 该档对选号无区分力，不得据此判优劣"
                     if (ge5 >= 0.999 or tier >= 0.999) else None),
    }


def run_prefix_curve(lottery: str, *, K: int,
                     target: Any = None,
                     prefix_ks: Optional[Sequence[int]] = None,
                     arms: Sequence[str] = ARMS,
                     periods: Optional[int] = None,
                     seeds: Sequence[int] = (0,),
                     pool_size: int = 2000,
                     cap: Optional[int] = None,
                     reselect_every: int = 25,
                     snapshot_dir: Optional[Path] = None,
                     batch_time: str = "离线回放（未记录生产批次时间）") -> Dict[str, Any]:
    """同一冻结票序 → 逐前缀档逐期重算。返回曲线 + 逐期序列 + 分母证据。"""
    from ev import tier_backtest as TB
    from ev import tier_target as TT
    from ev.coverage_backtest import build_pool, evaluate_period
    from ev.lowcrowd_backtest import PROD_OVERLAP_CAP
    from ev.order_freeze import build_snapshot, prefix_of, save_snapshot

    K = int(K)
    ks = tuple(int(x) for x in (prefix_ks if prefix_ks is not None else PREFIX_KS))
    if K < max(ks):
        raise ValueError(f"K={K} 小于最大前缀档 {max(ks)}：前缀必须落在整批之内")
    if K in ks:
        raise ValueError(
            f"K={K} 落在前缀档 {ks} 内：该档会同时被当作主口径与整批对照，"
            "「非实际购买」标记失去意义；请换一个不在档表内的 K")
    all_ks = tuple(dict.fromkeys(list(ks) + [K]))
    arms = tuple(arms)
    seeds = tuple(int(s) for s in seeds)
    cap = int(PROD_OVERLAP_CAP if cap is None else cap)
    target = TARGET_DEFAULT if target is None else target

    recs = TB.load_test_recs(lottery, periods)
    pre = dict(PREREGISTRATION)
    pre["seeds"] = list(seeds)
    if not recs:
        return {"降级": "无开奖记录", "预注册": pre, "口径说明": {
            "反事实": COUNTERFACTUAL_NOTE, "整批对照": NOT_PURCHASED_NOTE}}

    latest = TT.resolve_conditions(lottery, target, recs[-1].期号)
    rule_version = latest["rule_version"]
    p_tier = float(TT.single_ticket_probability(
        lottery, latest["conditions"], recs[-1].期号))
    pay5 = TT.resolve_conditions(lottery, PAYOUT5_TARGET, recs[-1].期号)
    p5 = float(TT.single_ticket_probability(
        lottery, pay5["conditions"], recs[-1].期号))
    issues = [r.期号 for r in recs]

    curves: Dict[str, Any] = {}
    series: Dict[str, Any] = {}
    snapshots: List[str] = []
    for s in seeds:
        sk = str(s)
        curves[sk], series[sk] = {}, {}
        for arm in arms:
            curves[sk][arm] = {}
            series[sk][arm] = {str(n): {"期号": [], "≥5元": [], "达标": [],
                                        "净收益": [], "实得": [], "成本": [],
                                        "中奖注数": []} for n in all_ks}
        gcache: Dict[str, Any] = {}
        cache: Dict[str, Any] = {}
        for blk, start in enumerate(range(0, len(recs), int(reselect_every))):
            chunk = recs[start:start + int(reselect_every)]
            pool = build_pool(lottery, int(pool_size), seed=s * 100003 + blk)
            for arm in arms:
                tks, _meta = TB._select(arm, lottery, pool, K, target,
                                        chunk[0].期号, s * 1009 + blk, cap)
                if len(tks) != K:
                    raise ValueError(
                        f"臂 {arm} 选出 {len(tks)} 注 != K={K}；同预算口径被污染")
                # ★ 块编号 / 池种子 / 选号种子都影响票序，必须进策略哈希：
                #   曾出现「360 期第 8 块」与「120 期第 0 块」起始期号相同、
                #   seed/K/策略全同却票序不同，撞同一个快照键被拒。
                exp = {"块": blk, "重选间隔": int(reselect_every),
                       "候选池规模": int(pool_size), "重叠上限": cap,
                       "池种子": s * 100003 + blk,
                       "选号种子": s * 1009 + blk}
                snap = build_snapshot(
                    lottery=lottery, issue=chunk[0].期号, tickets=tks,
                    strategy_version=f"{arm}/{ORDER_FREEZE_VERSION}",
                    seed=s, K=K, rule_version=rule_version,
                    code_version=CODE_VERSION, batch_time=batch_time,
                    target=target, experiment=exp,
                    extra=dict(exp, K=K))
                snapshots.append(str(save_snapshot(snap, directory=snapshot_dir)))
                for n in all_ks:
                    pref = prefix_of(snap, n)          # ★ 纯切片，绝不重排
                    row = series[sk][arm][str(n)]
                    for rec in chunk:
                        ev = evaluate_period(
                            lottery, pref, rec, rec.期号, cache,
                            target_grades=TB._grades_for(
                                rec.期号, lottery, target, gcache))
                        payout = float(ev["总实得"])
                        cost = len(pref) * 2.0
                        row["期号"].append(rec.期号)
                        row["≥5元"].append(int(bool(ev["≥5元"])))
                        row["达标"].append(int(bool(ev.get("达标"))))
                        row["实得"].append(payout)
                        row["成本"].append(cost)
                        row["净收益"].append(round(payout - cost, 2))
                        row["中奖注数"].append(int(ev["命中注数"]))
        for arm in arms:
            for n in all_ks:
                curves[sk][arm][str(n)] = _summarize(
                    series[sk][arm][str(n)], n, K, p5, p_tier)

    # 稳定性：跨 seed 全距 + 评估窗前后半差（**均为描述性，不进 BH**）。
    # ★ 缺席≠通过：单 seed 不能写「全距 0」；窗口不足 6 期不能写「差 0」。
    w = window_split(len(recs))
    cut, eval_len, half = w["探索窗期数"], w["评估窗期数"], w["前半"]
    stability: Dict[str, Any] = {}
    for arm in arms:
        for n in all_ks:
            key = f"{arm}/n{n}"
            item: Dict[str, Any] = {"seeds": list(seeds)}
            if len(seeds) < 2:
                item["状态"] = "未测（仅 1 个 seed）"
                item["原因"] = ("跨 seed 全距需要 ≥2 个独立种子；"
                                "单 seed 下 0 只表示没测，不表示稳定")
            else:
                vals = [curves[str(s)][arm][str(n)]["≥5元期率"] for s in seeds]
                tiers = [curves[str(s)][arm][str(n)]["≥四等期率"] for s in seeds]
                item["状态"] = "已测"
                item["≥5元期率_跨seed全距"] = round(max(vals) - min(vals), 6)
                item["≥四等期率_跨seed全距"] = round(max(tiers) - min(tiers), 6)
            # 时间窗维度：评估窗对半，逐 seed 算 |前半 − 后半| 后取最大。
            if not w["可测"]:
                item["窗口状态"] = "未测（评估窗不足 6 期）"
                item["窗口原因"] = ("前后半各需 ≥3 期；不足时写 0 "
                                    "等于把没测写成稳定")
            else:
                d5: List[float] = []
                dt: List[float] = []
                for s in seeds:
                    row = series[str(s)][arm][str(n)]
                    g5, gt = row["≥5元"], row["达标"]
                    d5.append(abs(sum(g5[cut:cut + half]) / half
                                  - sum(g5[cut + half:]) / (eval_len - half)))
                    dt.append(abs(sum(gt[cut:cut + half]) / half
                                  - sum(gt[cut + half:]) / (eval_len - half)))
                item["窗口状态"] = "已测（描述性，不进 BH）"
                item["≥5元期率_评估窗前后半差_跨seed最大"] = round(max(d5), 6)
                item["≥四等期率_评估窗前后半差_跨seed最大"] = round(max(dt), 6)
                item["窗口"] = {"探索窗期数": cut, "评估窗期数": eval_len,
                                "前半": half, "后半": eval_len - half}
            stability[key] = item

    n_periods = len(recs)
    return {
        "预注册": pre,
        "彩种": lottery, "K": K, "目标": latest["label"],
        "单注达标概率": p_tier, "单注p": {"≥5元": p5, "≥四等": p_tier},
        "规则版本": rule_version,
        "臂": list(arms), "前缀档": list(ks), "全档": list(all_ks),
        "seeds": list(seeds), "期号表": issues,
        "分母": {
            "开奖期数": n_periods,
            "参与期数": n_periods,
            "未参与期数": 0,
            "未参与说明": ("无逐期真实购买记录 ⇒ 不建模「挑哪期买」；"
                           "主判据按每期都买前 n 注的反事实结算，"
                           "未参与维度只作披露，不进主判据、不缩分母。"),
        },
        "口径说明": {
            "反事实": COUNTERFACTUAL_NOTE,
            "整批对照": NOT_PURCHASED_NOTE,
            "术语": TERMINOLOGY_NOTE,
            "先验零": PRIOR_ZERO_NOTE,
            "上界提示": ("`上界` 按门槛分列：≥5元 与 ≥四等 的单注 p 差几个数量级，"
                         "合成一个 n·p 会把读数锚错量。解析上界不是实测收益；"
                         "报前缀读数必须同时报 n 与该门槛的 n·p。"),
            "机械上升提示": ("`≥5元期率` / `≥四等期率` 随 n 上升是 1−(1−p)^n 的"
                             "必然结果，不是策略效果；只能同 n 对照随机臂。"),
        },
        "冻结快照": snapshots,
        "曲线": curves, "逐期": series, "稳定性": stability,
    }


def verdict_curve(result: Dict[str, Any], *, seed: Optional[int] = None,
                  alpha: Optional[float] = None) -> Dict[str, Any]:
    """评估窗上的唯一判决：逐期配对 → BH(声明族规模) → 可判性 + 集中度守门。"""
    import numpy as np
    from ev.arena.stats import (achievable, adjust_multiplicity, pair,
                                z_to_p_two_sided)

    if result.get("降级"):
        return {"降级": result["降级"], "判决": "无数据"}
    pre = result["预注册"]
    a = float(pre["名义α"] if alpha is None else alpha)
    sk = str(pre["seeds"][0] if seed is None else seed)
    issues = result["期号表"]
    cut = window_split(len(issues), ratio=pre["探索窗比例"])["探索窗期数"]
    eval_issues = issues[cut:]
    if len(eval_issues) < 3:
        return {"降级": "评估窗不足 3 期", "判决": "无数据",
                "探索窗期数": cut, "评估窗期数": len(eval_issues)}
    seqs = result["逐期"][sk]
    baseline = pre["基线臂"]
    others = [x for x in result["臂"] if x != baseline]
    # ★ 解析上界按门槛分列。缺 p 时显式标「未锚定」，不许静默退回只用实测 Δ。
    ps = result.get("单注p") or {}
    p_for = {"≥5元": ps.get("≥5元"),
             "≥四等": ps.get("≥四等", result.get("单注达标概率"))}
    cells: List[Dict[str, Any]] = []
    pvals: List[float] = []
    for n in pre["前缀档"]:
        for key, label in METRICS:
            p = p_for.get(label)
            head = theoretical_head(p, n) if p is not None else None
            for arm in others:
                av = np.asarray(seqs[arm][str(n)][key][cut:], dtype=float)
                bv = np.asarray(seqs[baseline][str(n)][key][cut:], dtype=float)
                row = pair(av, bv, issues_a=eval_issues, issues_b=eval_issues,
                           metric="binary", name_a=arm, name_b=baseline,
                           effect=head,
                           effect_source=("解析上界/实测保守取大" if head is not None
                                          else "未锚定（缺单注 p）"))
                achievable(row, len(eval_issues))
                # ★ pair() 在 sd=0 时对任何非零 effect 都返回 n*=1（「零方差⇒一期够」）。
                #   但「两臂逐期全同且全 0」是**无信息**，不是「效应可测」：窄门槛短窗
                #   零命中时给 n*=1 会把没测读成「一期就能判」。此处显式归一为 ∞。
                no_info = (float(row["配对差SD"]) == 0.0
                           and abs(float(row["实测Δ"])) <= 1e-15)
                n_ext = float("inf") if no_info else row.get("n*_外部效应")
                cells.append({
                    "n": n, "门槛": label, "臂": arm, "配对": row,
                    "p": z_to_p_two_sided(row["z_实测"]),
                    "空间上界": head,
                    "n*_实测效应": row["n*_实测效应"],
                    "n*_上界锚定": n_ext,
                    "n*_保守": row["n*_保守"],
                    "无信息": bool(no_info),
                    "锚定状态": ("已锚定" if head is not None
                                 else "未锚定（缺单注 p；缺席≠通过）"),
                })
                pvals.append(cells[-1]["p"])
    declared = int(pre["族规模"]["检验数"])
    if len(pvals) != declared:
        raise ValueError(
            f"★ 族规模不符：预注册声明 {declared}，实跑 {len(pvals)}；"
            "不许事后扩充检验族")
    adj = adjust_multiplicity(pvals, method="bh", alpha=a)
    for c, padj, thr in zip(cells, adj["校正后p"], adj["阈值"]):
        c["p_BH"] = padj
        c["阈值"] = thr
        # 集中度：净收益侧若 Δ 由单独一期撑起，不报可判
        n, arm = c["n"], c["臂"]
        money = pair(
            np.asarray(seqs[arm][str(n)]["净收益"][cut:], dtype=float),
            np.asarray(seqs[baseline][str(n)]["净收益"][cut:], dtype=float),
            issues_a=eval_issues, issues_b=eval_issues,
            metric="money", name_a=arm, name_b=baseline)
        share = money.get("单期占Δ比")
        c["集中度"] = {"单期占Δ比": share,
                       "剔除最大配对期Δ": money.get("Δ_剔除最大配对期")}
        if share is not None and abs(float(share)) >= 1.0:
            c["配对"]["判决"] = "不可判（Δ 由单期撑起）"
        c["判决"] = ("检出" if (padj <= a and c["配对"]["判决"] == "可判")
                     else "未检出")
    return {
        "预注册版本": pre["版本"], "主口径n": pre["主口径n"],
        "探索窗期数": cut, "评估窗期数": len(eval_issues),
        "族规模": declared, "名义α": a, "BH": adj,
        "格": cells,
        "理论不可判档数": sum(c["空间上界"] == 0 for c in cells
                              if c["空间上界"] is not None),
        "未锚定档数": sum(c["锚定状态"] != "已锚定" for c in cells),
        "效应量锚定说明": pre["效应量锚定"],
        "检出数": sum(c["判决"] == "检出" for c in cells),
        "结论": ("未检出稳定差异；仅表示已测维度在该样本量下未见差异，不构成证明"
                 if not any(c["判决"] == "检出" for c in cells)
                 else "存在通过 BH 与可判性守门的档，需逐档复核口径"),
        "术语": TERMINOLOGY_NOTE,
    }


def _shadow_key(record: Dict[str, Any]) -> tuple:
    """影子台账去重键。

    ★ 2026-11-05 扩展：加入 `阶段`（登记 / 结算）。方案骨架第 ④ 步要求
      「开奖前登记冻结票序 → 开奖后只追加结算」，两态必须能共存于同一台账；
      旧记录没有该字段 ⇒ 归一为空串，**旧口径去重行为完全不变**。
    """
    return (str(record.get("期号")), str(record.get("冻结版本")),
            str(record.get("策略哈希")), str(record.get("n")),
            str(record.get("阶段") or ""))


def append_shadow(record: Dict[str, Any], *, path: Optional[Path] = None) -> bool:
    """影子台账 append-only：同一键已存在就拒绝回改，返回是否新写入。

    离线研究通道是单进程、不接生产出号/结算，故不加跨进程锁；这与企微推送
    那种「同进程多线程 + 多进程」的锁纪律不是同一场景。
    """
    p = Path(path) if path is not None else SHADOW_PATH
    key = _shadow_key(record)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                old = json.loads(line)
            except json.JSONDecodeError:
                continue
            if _shadow_key(old) == key:
                return False                 # ★ 只追加、不回改
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return True
