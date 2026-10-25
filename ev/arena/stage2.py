# -*- coding: utf-8 -*-
"""ev/arena/stage2.py —— Stage 2 倍投的对照编排（runner 档位）。

Stage 2 要回答的唯一问题
------------------------
同一笔预算（K 注 = 2K 元）下，**已经由 Stage 1 选出的那副票集**，
是把钱摊成 K 张互异票，还是压成更少票 × 倍数，在净收益侧差多少？

★ 复用 Stage 1 的票集，**不重新选号** —— 否则比较的是「选号 + 分配」两个变量，
     看不出分配本身的作用。

三条必须先摆明的口径（否则整层会被误读）
----------------------------------------
1. **对「达标」目标，倍数是数学空操作。** 达标 = 「至少一注达到 ≥目标奖级」；
   同一张票买 2 倍不会覆盖任何新结局，所以 P(达标) **只由去重票集决定**。
   ⇒ `all_single`（K 张互异票）在达标口径上**弱占优**：只要某个倍投方案
   挤掉了不同的票，它的达标率必然 ≤ `all_single`。这是可证的，不是经验结论。

2. **对「金额」目标，倍投可能有作用。** 实得尾部被放大 ⇒ `P(净额 ≥ 0)`
   未必单调。**这是待检验假设**，本模块只把它量出来，不断言它赚钱。

3. **跨方案一律按每 100 元归一**（`净收益每100元`），
   否则「多花钱」的一侧会赢，比较失去意义。

☆ `all_single` 与 Stage 1 的原臂是同一副票**，所以它是天然的基线：
   Stage 2 的每个方案都和它配对。

三条硬断言（不等即**实现缺陷**，不是「结果不好」）
------------------------------------------------
- `I1 同票集同命中`：`few_double` 与 `incremental_coverage` 的去重票集**逐位相同**
  （都取 `tickets[:N−m]`），差别只在倍数加给谁 ⇒ `hit` 序列必须完全相同。
- `I2 弱占优`：`all_single` 的达标率 ≥ 任何一个倍投方案。严格小于 ⇒ 第 1 条被违反。
- `I3 倍数不进命中`：同一方案的 `hit` 与「把全部倍数改成 1」的 `hit` 必须相同。

这三条比原方案里写的「四目标达标期占比必须完全相同」**更准确**：
原句与实际设计不自洽 —— `few_double` / `incremental_coverage` 按设计要撤掉 m 张票
去重数变成 N−m，达标率按第 1 条就该下降；要求四者完全相等等于要求这四个目标
退化成同一个目标。真正必须守的是「**倍数本身**不改变达标」。

尚未预注册 / 尚未实跑
---------------------
本模块只提供 runner（机制）。**没有**把 Stage 2 的格写进
`config/preregistration.json` —— 那会改动预注册哈希。真正开跑前必须先登记，
且登记动作要在看任何 Stage 2 结果**之前**完成。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ev import tier_allocate as TA
from ev.arena import COST_PER_BET
from ev.arena import replay as RP
from ev.arena import stats as ST

OBJECTIVES = TA.OBJECTIVES                 # ("all_single", "few_double",
N2_RATIOS = TA.N2_RATIOS                   #  "incremental_coverage", "monetary_threshold")

BASELINE = "all_single"                    # 基线 ≡ Stage 1 的原票集（全 1 倍）


# ---------------------------------------------------------------- 方案构造
def build_plan(tickets: Sequence[Dict[str, Any]], n_bets: int,
               objective: str, n2_ratio: float = 0.10,
               weights: Optional[Sequence[float]] = None) -> Dict[str, Any]:
    """把 `n_bets` 注预算按某个分配目标摊到 `tickets` 上。

    返回 `{"objective", "票", "倍数", "去重注数", "总注数", "倍数分布", "说明"}`。
    ★ `tickets` 的顺序有语义：越靠前 = 边际覆盖贡献越大（Stage 1 的选出序）。
    """
    alloc = TA.allocate_multipliers(tickets, n_bets, objective=objective,
                                    n2_ratio=n2_ratio, weights=weights)
    plan = list(alloc["票"])
    return {
        "objective": objective,
        "票": [t for t, _ in plan],
        "倍数": [int(m) for _, m in plan],
        "去重注数": int(alloc["去重注数"]),
        "总注数": int(alloc["总注数"]),
        "倍数分布": dict(alloc["倍数分布"]),
        "说明": alloc["说明"],
    }


# ---------------------------------------------------------------- 不变式
def check_invariants(results: Dict[str, Any]) -> Dict[str, Any]:
    """三条硬断言。返回 `{"通过": bool, "明细": [...]}`。

    ★ 失败意味着**实现缺陷**（比如命中时乘了倍数），不是「这个方案不好」。
    """
    out: Dict[str, Any] = {"通过": True, "明细": [], "违反": []}

    def fail(tag: str, detail: str) -> None:
        out["通过"] = False
        out["违反"].append(tag)
        out["明细"].append(f"[{tag}] {detail}")

    # ---- I3 倍数不进命中：逐个方案与「全 1 倍」比对
    for key, r in results.items():
        ref = r.meta.get("_逐位参考")
        if ref is None:
            continue
        if not np.array_equal(r.hit, ref):
            fail("I3 倍数不进命中",
                 f"{key}: 带倍数的 hit 与全 1 倍的 hit 不一致 "
                 f"（{int((r.hit != ref).sum())}/{len(ref)} 期不同）")

    # ---- I1 同一去重票集 ⇒ 逐位相同
    pair_partners = ("few_double", "incremental_coverage")
    keys = [k for k in results if k.startswith(pair_partners[0])]
    for k in keys:
        other = k.replace(pair_partners[0], pair_partners[1], 1)
        if other in results:
            a, b = results[k], results[other]
            if a.hit.shape != b.hit.shape:
                fail("I1 同票集同命中", f"{k} vs {other}: 期数不同")
            elif not np.array_equal(a.hit, b.hit):
                fail("I1 同票集同命中",
                     f"{k} 与 {other} 的去重票集相同但 hit 不同"
                     f"（{int((a.hit != b.hit).sum())} 期）")
            else:
                out["明细"].append(f"[I1] {k} ≡ {other}：hit 逐位相同 ✓")

    # ---- I2 all_single 弱占优
    base = results.get(BASELINE) or next(
        (v for k, v in results.items() if k.startswith(BASELINE)), None)
    if base is not None:
        br = float(np.mean(base.hit))
        for key, r in results.items():
            if key is base or key.startswith(BASELINE):
                continue
            rr = float(np.mean(r.hit))
            if rr > br + 1e-12:
                fail("I2 all_single弱占优",
                     f"{key} 达标率 {rr:.6f} > {BASELINE} {br:.6f} ⇒ 第 1 条口径被违反")
    if out["通过"]:
        out["明细"].append("[I2] all_single 达标率 ≥ 所有倍投方案 ✓")
    return out


# ---------------------------------------------------------------- 一格
def run_stage2_cell(lottery: str, tickets: Sequence[Dict[str, Any]],
                    records: Sequence[Any], target: Any, *,
                    rule_mode: str = "current",
                    target_issue: Any = None,
                    cost_per_bet: float = COST_PER_BET,
                    n_bets: Optional[int] = None,
                    objectives: Sequence[str] = OBJECTIVES,
                    n2_ratios: Sequence[float] = N2_RATIOS,
                    weights: Optional[Sequence[float]] = None,
                    arm_name: str = "",
                    tax: bool = False,
                    verbose: bool = True) -> Dict[str, Any]:
    """在一副**已固定**的 Stage 1 票集上，跑「4 目标 × n2_ratio」的倍投对照。

    `tickets` = Stage 1 某个臂选出的票（顺序 ＝ 边际贡献从大到小）。
    `n_bets`  = 预算折成的注数（K）；缺省取 `len(tickets)`。

    返回结构化 dict：每个方案的 `行`（可进报告）、`配对`（vs all_single）、
    `不变式`（三条硬断言结果）。
    """
    from ev.arena.runner import money_gate          # 延迟导入：避免 runner↔stage2 循环

    recs = list(records)
    if not recs:
        return {"降级": "无开奖记录"}
    tks = list(tickets)
    if not tks:
        return {"降级": "票集为空"}
    n_bets = int(len(tks) if n_bets is None else n_bets)
    if target_issue is None:
        target_issue = recs[-1].期号

    t0 = time.time()
    grid: List[Tuple[str, Optional[float]]] = []
    for obj in objectives:
        if obj in ("few_double", "incremental_coverage", "monetary_threshold"):
            for r in n2_ratios:
                grid.append((obj, float(r)))
        else:
            grid.append((obj, None))        # all_single 无比值维度

    results: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    for obj, ratio in grid:
        key = obj if ratio is None else f"{obj}@{ratio:.2f}"
        plan = build_plan(tks, n_bets, obj, (ratio or 0.10), weights)
        # ★ I3 参考：同一去重票集、倍数全置 1 ⇒ hit 必须与带倍数版本相同
        ref = RP.replay(lottery, plan["票"], recs, target, rule_mode=rule_mode,
                        target_issue=target_issue, cost_per_bet=cost_per_bet,
                        with_money=False)
        ar = RP.replay(lottery, plan["票"], recs, target, rule_mode=rule_mode,
                       target_issue=target_issue, arm=key,
                       cost_per_bet=cost_per_bet, tax=tax,
                       multipliers=plan["倍数"])
        ar.meta["_逐位参考"] = ref.hit
        results[key] = ar
        row = ar.to_row()
        row.update({"臂": key, "目标": obj, "n2_ratio": ratio,
                    "去重注数": plan["去重注数"], "总注数": plan["总注数"],
                    "倍数分布": plan["倍数分布"], "说明": plan["说明"]})
        rows.append(row)
        if verbose:
            print(f"    · {key}: 去重 {plan['去重注数']}/{n_bets} 张、实投 "
                  f"{plan['总注数']} 注/期 | 达标率 {row['达标率']:.4f} | "
                  f"净收益每100元 {row.get('净收益每100元', float('nan')):.2f}",
                  flush=True)

    inv = check_invariants(results)

    # ---- 配对：每个方案 vs all_single（金额侧）
    pairs: List[Dict[str, Any]] = []
    base = results.get(BASELINE)
    if base is not None and getattr(base, "net", None) is not None:
        for key, o in results.items():
            if key == BASELINE or getattr(o, "net", None) is None:
                continue
            mp = ST.pair(o.net, base.net, issues_a=o.issues, issues_b=base.issues,
                         name_a=key, name_b=BASELINE, metric="money",
                         effect_source="实测（每期净额，元／期）")
            mp = ST.achievable(mp, len(o.issues))
            money_gate(mp, o.money or {})
            # 达标侧：同一去重票集就该同号；不同票集则用差值 + 说明
            mp["达标率_方案"] = float(np.mean(o.hit))
            mp["达标率_基线"] = float(np.mean(base.hit))
            mp["达标Δ"] = mp["达标率_方案"] - mp["达标率_基线"]
            pairs.append(mp)
            for r in rows:
                if r["臂"] == key:
                    r["配对"] = {
                        "实测Δ": mp["实测Δ"], "z_实测": mp["z_实测"],
                        "判决": mp["判决"], "集中度守门": mp["集中度守门"],
                        "达标Δ": mp["达标Δ"],
                    }

    return {
        "彩种": lottery, "臂": arm_name, "目标": str(target),
        "规则口径": rule_mode, "期数": len(recs),
        "期号起": recs[0].期号, "期号止": recs[-1].期号,
        "预算注数": n_bets, "预算元每期": n_bets * float(cost_per_bet),
        "去重票数": len(tks),
        "常数": {"目标": list(objectives), "n2_ratios": list(n2_ratios)},
        "方案": rows, "配对": pairs, "不变式": inv,
        # ★ 原始数据（numpy 序列）。金额侧 `pair` 已做，但**达标侧的配对要等
        #   BH 多重校正**才能判，而 family 的构成（几个方案算一组）是调用方的
        #   事 ⇒ 不在这里擅自校正，把对象交出去。
        "对象": results,
        "耗时秒": round(time.time() - t0, 1),
    }
