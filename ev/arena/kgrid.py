# -*- coding: utf-8 -*-
"""ev/arena/kgrid.py —— 五档 K × 四臂的增量编排（任务 1「单位预算验证」）。

它要回答的那个问题
------------------
> 「中奖增加是因为票多了，还是每元买到的组合覆盖确实改善了？」

这两个来源在单点读数里**完全混在一起**：K 从 50 涨到 500，达标率当然涨。
把它们分开的唯一干净办法是**同一个 K 上比臂**：

    Δ(K) = 覆盖臂达标率(K) − 同 K 随机臂达标率(K)

`Δ(K) ≠ 0` 才是覆盖的贡献；`Δ(K) ≈ 0` 而达标率仍随 K 上升 ⇒ 纯规模效应。

★ 三个必须同时报的量（少一个就会被读歪）
  · `规模效应`：随机臂达标率 vs 理论 `1−(1−p)^K`。偏离超过 2·SE 说明池/cap 口径有问题，
    与策略无关 —— 这一项是**口径体检**，不是结论。
  · `覆盖效应 Δ(K)`：上式。**必须与 K 一起报**，否则读者会默认「越大越好」。
  · `η(K) = Δ(K) / 可争取空间(K)`：把 Δ 归一化到理论上限上。空间本身非单调
    （峰在 `K·p≈1`），所以 η 与 Δ 的排序可能不一致 —— 两个都要给。

不重写 `run_cell`
-----------------
`run_cell` 已含五层硬校验（预注册逐项对齐、同池、`assert_same_periods` 逐位断言、
哨兵 `z>4` 判缺陷、两级集中度守门）。本模块只是**在外面加一圈**：
用 `keep_objects=True` 把 `ArmResult` 与候选池取出来，补跑 `random` 副本，
再用 `tier_metrics` 在同批票上算多门。

★ 基线为什么必须是副本均值
  单个 seed 的 `random` 达标率本身带抽样噪声，直接拿它当分母，
  Δ 里就混进了「基线这一副票恰好运气好/差」。副本均值把这个噪声压下去，
  同时给出 `SD_replica` —— 它是「Δ 是不是噪声」的最低限度的说明材料。

★ 本模块不写 pending、不接调度、不改任何一注的中奖概率。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ev import preregistration as PR
from ev.arena import arms as A
from ev.arena import replay as RP
from ev.arena import runner as RC
from ev.arena import stats as ST
from ev.arena import tier_metrics as TM

__all__ = ["DEV_FRACTION", "REPLICAS", "MAIN_ARMS", "cell_of",
           "run_kgrid_cell", "bh_across_cells", "KGRID_BLOCK"]

# 评估窗纪律（预注册 `格_K网格.评估期`）：前 60% 开发、后 40% 唯一判决窗口
DEV_FRACTION = 0.6
REPLICAS = 5

# 主终点门 + 附加描述门（后两个不进 BH）
DEFAULT_LABELS: Tuple[str, ...] = ("≥10元", "≥四等", "≥三等")

# 进主检验的臂（`prod_mix` 内部 cap≠实验 cap ⇒ 只作外部保真度对照）
MAIN_ARMS: Tuple[str, ...] = ("tier_packing", "union_greedy")
ALL_ARMS: Tuple[str, ...] = ("random", "tier_packing", "union_greedy", "prod_mix")

KGRID_BLOCK = "格_K网格"


# ------------------------------------------------------------------ 格定义
def cell_of(lottery: str, K: int, *, target: Optional[Dict[str, Any]] = None,
            rule_mode: str = "periodic", name: Optional[str] = None,
            drop_rule_versions: Sequence[Any] = ()) -> Dict[str, Any]:
    """造一个 `run_cell` 能吃的格。

    ★ `常量` 里**只能**放 `assert_run_matches` 会收到的那三个键
      （`期窗` / `规则口径` / `剔除规则版本`）：`run_cell` 拿它逐项对齐实跑常量，
      多放一个键就会因为「实跑缺该字段」直接抛。评估窗等信息放在格的**外层**。
    """
    tgt = dict(target or {"kind": "payout", "min_payout": 10.0})
    label = "≥10元" if tgt.get("kind") == "payout" else f"≥{tgt.get('grade')}"
    return {
        "名": name or f"{lottery}-K{int(K)}-{label}",
        "彩种": lottery,
        "K": int(K),
        "预算元每期": int(K) * 2,
        "目标": tgt,
        "常量": {"期窗": "全量",
                 "规则口径": rule_mode,
                 "剔除规则版本": list(drop_rule_versions)},
    }


# ------------------------------------------------------------------ 工具
def _slice(h: np.ndarray, j: np.ndarray, recs: Sequence[Any],
           lo: int, hi: int) -> Tuple[np.ndarray, np.ndarray, List[Any]]:
    return h[:, lo:hi], j[:, lo:hi], list(recs)[lo:hi]


def _hit_series(series: Dict[str, Any]) -> np.ndarray:
    """`gate_series` 的逐期达标 → float 数组（供配对统计用）。"""
    return np.asarray(series["_每期达标"], dtype=float)


def _replica_of_random(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                       target: Any, target_issue: Any, seed: int, cap: int,
                       recs: Sequence[Any], rule_mode: str,
                       const: Dict[str, Any]) -> Any:
    """补跑一副 `random` 副本（与 `run_cell` 内那一副同池、同 cap、不同 seed）。"""
    tks, _meta = A.select_arm("random", lottery, pool, K, target, target_issue,
                              seed=int(seed), cap=int(cap),
                              params=RC._arm_params(const, len(recs)))
    return RP.replay(lottery, tks, recs, target, rule_mode=rule_mode,
                     target_issue=target_issue, arm=f"random#{seed}")


def _decompose(series_arm: Dict[str, Any], base_rate: float, base_sd: float,
               n: int) -> Dict[str, Any]:
    """把「达标率提升」拆成规模效应与覆盖效应两块。"""
    rate = float(series_arm["期占比"])
    delta = rate - base_rate
    cap = float(min(1.0, series_arm["K·p"]))
    space = float(series_arm["可争取空间"])
    se = float(np.sqrt(max(rate * (1 - rate), 0.0) / n)) if n else float("nan")
    return {
        "达标率": rate,
        "基线达标率": base_rate,
        "基线副本SD": base_sd,
        "覆盖效应Δ": delta,
        "相对提升": (delta / base_rate) if base_rate > 0 else None,
        "可争取空间": space,
        "理论上限Kp": cap,
        "归一化η": (delta / space) if space > 1e-12 else None,
        "抽样SE": se,
        # 规模效应：达标率本身随 K 上升，这一档不归功于覆盖
        "规模效应_绝对": base_rate,
        "规模效应_相对纯随机": (base_rate - float(series_arm["随机基线"])),
        "★单期样本": n,
    }


# ------------------------------------------------------------------ 主入口
def run_kgrid_cell(lottery: str, K: int, *,
                   const: Optional[Dict[str, Any]] = None,
                   replicas: int = REPLICAS,
                   arms: Sequence[str] = ALL_ARMS,
                   target: Optional[Dict[str, Any]] = None,
                   rule_mode: str = "periodic",
                   labels: Sequence[str] = DEFAULT_LABELS,
                   verbose: bool = False) -> Dict[str, Any]:
    """跑一格：4 臂 + `random` 的 R 副本 + 多门指标 + 覆盖/规模分解。

    返回可直接进 JSON 的 dict（数组已剔除）。判据仍要等**全部**格子跑完再做 BH ——
    单格里做校正等于把族规模说小了。
    """
    const = dict(const if const is not None else PR.constants())
    cell = cell_of(lottery, K, target=target, rule_mode=rule_mode)
    res = RC.run_cell(cell, const=const, arms=list(arms), verbose=verbose,
                      keep_objects=True)
    if "降级" in res:
        return {"格": cell["名"], "彩种": lottery, "K": int(K), "降级": res["降级"]}

    inner = res.pop("_内部")
    objs = inner["objs"]
    recs: List[Any] = inner["recs"]
    issues: List[Any] = inner["issues"]
    pool = inner["pool"]
    target_issue = inner["target_issue"]
    if "random" not in objs:
        raise RuntimeError(f"{cell['名']} 缺 `random` 臂 ⇒ 没有基线，无法比较")

    n = len(recs)
    lo = int(np.floor(n * DEV_FRACTION))
    cap = int(const["重叠上限"])
    base_seed = int(const["基准seed"])

    # ---- ① random 种子军团（副本 0 = run_cell 那一副）
    #   ★ seed 必须自己记下来：`replay` 的 meta 里没有派生 seed（那是 `select_arm` 的
    #     产物，replay 不知道）⇒ 从 ArmResult.meta 读会一律读到 0，看起来「五个副本同 seed」。
    rep_seeds: List[int] = [int(A.derive_seed(base_seed, "random"))]
    rep_objs: List[Any] = [objs["random"]]
    for r in range(1, int(replicas)):
        sd = int(A.derive_seed(base_seed, f"random#{r}"))
        rep_seeds.append(sd)
        rep_objs.append(_replica_of_random(
            lottery, pool, K, cell["目标"], target_issue,
            sd, cap, recs, rule_mode, const))

    # ---- ② 每门：全窗与评估窗各算一次
    gates: Dict[str, Any] = {}
    for lb in labels:
        # 各副本在同一门上的逐期达标（R, n）—— 后面对所有臂复用，不逐臂重算
        rep_hit = np.array([
            _hit_series(TM.gate_series(lottery, o.h, o.j, recs, lb,
                                       rule_mode=rule_mode,
                                       target_issue=target_issue, K=K))
            for o in rep_objs], dtype=float)
        gates[lb] = {"_副本逐期": rep_hit}

    out_arms: Dict[str, Any] = {}
    # 主终点门（用于把「精确 P」的解析效应量挂到对的门上）
    primary = ("≥10元" if (cell["目标"] or {}).get("kind") == "payout"
               else f"≥{(cell['目标'] or {}).get('grade')}")
    exact_base = (res["臂"].get("random") or {}).get("精确P")
    for name, o in objs.items():
        meta_src = res["臂"].get(name) or {}
        entry: Dict[str, Any] = {
            "臂": name, "完成": True,
            "meta": {k: v for k, v in meta_src.items()
                     if isinstance(v, (int, float, str, bool)) or v is None},
        }
        blocked: List[str] = []
        for lb in labels:
            full = TM.gate_series(lottery, o.h, o.j, recs, lb, rule_mode=rule_mode,
                                  target_issue=target_issue, K=K)
            rep = gates[lb]["_副本逐期"]
            base_rate = float(rep.mean())
            base_sd = (float(rep.mean(axis=1).std(ddof=1)) if rep.shape[0] > 1
                       else float("nan"))
            dec_full = _decompose(full, base_rate, base_sd, n)

            h_e, j_e, recs_e = _slice(o.h, o.j, recs, lo, n)
            eval_series = TM.gate_series(lottery, h_e, j_e, recs_e, lb,
                                         rule_mode=rule_mode,
                                         target_issue=target_issue, K=K)
            rep_e = rep[:, lo:n]
            base_e = float(rep_e.mean())
            base_e_sd = (float(rep_e.mean(axis=1).std(ddof=1)) if rep_e.shape[0] > 1
                         else float("nan"))
            dec_eval = _decompose(eval_series, base_e, base_e_sd, len(recs_e))

            # 配对统计（同口径、同期窗）：用 stats.pair 而不是手算，ρ̂/n* 一并拿到
            # ★ 效应量必须**保守取大**：实测 Δ 只有一副票集，噪声极大；
            #   精确 P 是该票集「真实并集概率」的解析值，与实测同口径（同池同 cap）。
            #   只报实测会把噪声当效应（K=50 实测 Δ 5.2pp vs 精确 2.3pp）。
            ev_issues = [r.期号 for r in recs_e]
            eff = None
            if lb == primary and meta_src.get("精确P") is not None and exact_base is not None:
                eff = float(meta_src["精确P"]) - float(exact_base)
            pr = ST.pair(_hit_series(eval_series), rep_e.mean(axis=0),
                         issues_a=ev_issues, issues_b=ev_issues,
                         effect=eff, name_a=name, name_b="random(R副本均值)",
                         effect_source=("实测+精确（保守取较大 n*）" if eff is not None
                                        else "实测（该门无精确 P，仅实测）"))
            pr = ST.achievable(pr, len(recs_e))
            entry[lb] = {"全窗": dec_full, "评估窗": dec_eval,
                         "配对": {k: v for k, v in pr.items()
                                  if isinstance(v, (int, float, str, bool)) or v is None},
                         "条件集": eval_series["条件集"],
                         # ★ 分版本：大乐透「四等」跨版本条件集不同（`dlt-2019-02`
                         #   没有 (4,1)）⇒ 合并成一个平均是错的口径，必须逐版给数。
                         "分版本": eval_series["分版本"],
                         "精确P": meta_src.get("精确P")}
            bad = TM.sanity_violations({"门": {lb: eval_series}})
            if bad:
                blocked.extend(bad)
                entry[lb]["★层1否决"] = bad
        entry["★cap污染"] = bool(meta_src.get("cap污染"))
        entry["进主检验"] = bool(name in MAIN_ARMS)
        entry["层1否决"] = blocked
        out_arms[name] = entry

    base_entry = {
        "副本数": len(rep_objs),
        "副本seed": rep_seeds,
        "门": {lb: {"副本均值": float(gates[lb]["_副本逐期"].mean()),
                    "副本SD": (float(gates[lb]["_副本逐期"].mean(axis=1).std(ddof=1))
                             if gates[lb]["_副本逐期"].shape[0] > 1 else None),
                    "副本间极差": (float(gates[lb]["_副本逐期"].mean(axis=1).max()
                                    - gates[lb]["_副本逐期"].mean(axis=1).min())
                               if gates[lb]["_副本逐期"].shape[0] > 1 else None)}
              for lb in labels},
    }

    return {
        "格": cell["名"], "彩种": lottery, "K": int(K),
        "预算元每期": int(K) * int(const["单注成本元"]),
        "目标": res.get("目标"), "规则口径": rule_mode,
        "期数": n, "期号起": issues[0], "期号止": issues[-1],
        "开发窗": {"起": 0, "止": lo - 1, "期数": lo},
        "评估窗": {"起": lo, "止": n - 1, "期数": n - lo},
        "单注达标概率": res.get("单注达标概率"),
        "臂": out_arms,
        "随机基线": base_entry,
        "层1否决": any(v.get("层1否决") for v in out_arms.values()),
        "耗时秒": res.get("耗时秒"),
        "诚实边界": TM.HONEST_NOTE,
    }


# ------------------------------------------------------------------ 跨格校正
def bh_across_cells(cells: Sequence[Dict[str, Any]], *,
                    label: str = "≥10元", alpha: float = 0.05,
                    m_expected: Optional[int] = None) -> Dict[str, Any]:
    """把族规模说清楚之后再校正 —— BH 只在**预先声明的族**内做。

    族 = 进主检验的臂 × 档位 × 彩种。单格校正会把 m 说小（阈值放宽），
    所以这一步必须在跑完全部格子之后做一次。

    `m_expected` 用来核对族规模与预注册是否一致（`格_K网格` 写的是
    2 臂 × 5 档 × 2 彩种 = 20）；不一致时**不静默**，在返回里标出来。
    """
    family: List[Tuple[str, str, str]] = []
    pvals: List[float] = []
    for c in cells:
        if "降级" in c:
            continue
        for name, a in (c.get("臂") or {}).items():
            if not a.get("进主检验"):
                continue
            pr = (a.get(label) or {}).get("配对") or {}
            z = pr.get("z_实测")
            if z is None or not np.isfinite(z):
                continue
            family.append((str(c.get("格")), str(name), str(c.get("彩种"))))
            pvals.append(ST.z_to_p_two_sided(float(z)))
    if not pvals:
        return {"族规模": 0, "说明": "没有可校正的配对（全部不可判或无进主检验的臂）"}
    adj = ST.adjust_multiplicity(pvals, method="bh", alpha=alpha)
    rows = [{"格": g, "臂": n_, "彩种": lot, "原始p": p, "校正后p": ap,
             "BH通过": bool(ap <= alpha)}
            for (g, n_, lot), p, ap in zip(family, adj["原始p"], adj["校正后p"])]
    return {
        "门": label,
        "族规模": len(pvals),
        "族规模_预注册": m_expected,
        "★族规模不符": (m_expected is not None and len(pvals) != m_expected),
        "方法": adj.get("方法", "bh"),
        "通过数": int(sum(1 for r in rows if r["BH通过"])),
        "明细": rows,
    }
