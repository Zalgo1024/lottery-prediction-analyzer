# -*- coding: utf-8 -*-
"""ev/arena/runner.py —— 单个「格」的对照编排。

流程
----
1. 期窗（`periods.build_periods`）→ 开奖记录切片；
2. 候选池（`build_pool`，固定 seed）→ **所有臂共用同一池**；
3. 逐臂选号（`arms.select_arm`）→ 逐臂回放（`replay.replay`）；
4. `periods.assert_same_periods` 断言期窗逐位相等；
5. 与基线臂配对统计（`stats.pair`）+ 精确效应量上界（`exact_union_probability`）；
6. 多重比较校正（`stats.adjust_multiplicity`）。

★ 票集全程**固定**（不逐期重选）：本轮的问题是「设计方法能不能改变并集结构」，
  逐期重选会把「重选频率」这个未控制变量混进来。
"""
from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ev import preregistration as PR
from ev import tier_target as TT
from ev.arena import COST_PER_BET
from ev.arena import arms as A
from ev.arena import periods as PD
from ev.arena import replay as RP
from ev.arena import stats as ST
from ev.coverage_backtest import build_pool
from ev.tier_backtest import load_test_recs
from ev.tier_coverage import exact_union_probability

# 纯诊断开关：让 tier_coverage 臂多吐一份逐步贪心轨迹（meta["贪心轨迹"]）。
# ★ 不参与任何判据、不在预注册常量里 ⇒ 打开它不用改哈希，不算改实验契约。
TRACE_SELECTION = True


def _arm_params(const: Dict[str, Any], n_periods: int) -> Dict[str, Any]:
    """逐臂共用的选号参数（MC 规模 / seed 偏移 / 期窗 / 诊断开关）。"""
    return {"m_fit": int(const["MC拟合采样数"]),
            "m_eval": int(const["MC评估采样数"]),
            "eval_seed_offset": int(const["评估seed偏移"]),
            "n_periods": int(n_periods),
            "trace": bool(TRACE_SELECTION)}


def _records_for(lottery: str, issues: Sequence[Any]) -> List[Any]:
    idx = {i: p for p, i in enumerate(issues)}
    recs = [r for r in load_test_recs(lottery) if getattr(r, "期号", None) in idx]
    recs.sort(key=lambda r: idx[r.期号])
    return recs


def money_gate(mp: Dict[str, Any], money: Dict[str, Any]) -> Dict[str, Any]:
    """净收益侧的**两级集中度守门** + 恒等式判据（就地写回 `mp`）。

    级 1（单臂）：`单次大奖占总实得比 > 30%`，或剔除最大一期后净收益符号翻转
                 ⇒ 该臂强制「不可判」。
    级 2（配对）：`|单期占Δ比| > 50%`，即「Δ 的一半以上来自单独一期」
                 ⇒ 同样强制「不可判」。

    ★ 为什么要级 2：级 1 只看「臂自己」，抓不到「基线单独一期中大奖把 Δ 撑起来」。
      主格实测 —— `random` 在 20128 期中一注二等 134,885 元，只占它自己总实得的
      27.5%（级 1 放行），却贡献了 Δ 的 92%（级 2 拦下；剔除后 Δ 从 −41.62
      降到 −3.30 元/期）。缺了级 2，这一格会被报成「基线更好」。

    ★ 恒等式判据：`|z| > 4` 判**实现缺陷**（因为 E[实得] 的期望差恒为 0），
      不报结论 —— 与命中侧的哨兵同一条纪律。
    """
    ratio = float(money.get("单次大奖占总实得比") or 0.0)
    n_all = float(money.get("净收益") or 0.0)
    n_ex = float(money.get("净收益每100元_剔除最大一期") or 0.0)
    share = mp.get("单期占Δ比")
    d_ex = mp.get("Δ_剔除最大配对期")
    if ratio > 0.3:
        mp["集中度守门"] = f"大奖占总实得比 {ratio:.1%} > 30% ⇒ 强制不可判"
        mp["判决"] = "不可判"
    elif (n_all > 0) != (n_ex > 0):
        mp["集中度守门"] = "剔除最大一期后符号翻转 ⇒ 强制不可判"
        mp["判决"] = "不可判"
    elif isinstance(share, float) and math.isfinite(share) and abs(share) > 0.5:
        mp["集中度守门"] = (
            f"配对差被单期支配：{mp.get('最大配对期')} 一期贡献 Δ 的 {share:.0%}"
            f" ⇒ 强制不可判（剔除后 {d_ex:+.2f} 元/期）")
        mp["判决"] = "不可判"
    else:
        mp["集中度守门"] = "通过"
    zz = mp.get("z_实测")
    mp["★恒等式判据"] = (
        "实现缺陷嫌疑（期望差恒为 0）"
        if (zz is not None and math.isfinite(zz) and abs(zz) > 4.0)
        else "正常（与 E[实得] 恒等式相容）")
    return mp


def run_cell(cell: Dict[str, Any], *, const: Optional[Dict[str, Any]] = None,
             arms: Optional[Sequence[str]] = None,
             verbose: bool = True) -> Dict[str, Any]:
    """跑一个格。返回结构化结果 dict（可直接进 JSON 与报告）。"""
    doc = None
    const = dict(const if const is not None else PR.constants())
    lottery = cell["彩种"]
    K = int(cell["K"])
    target = cell["目标"]
    cc = dict(cell.get("常量") or {})
    rule_mode = cc.get("规则口径", "current")
    drop = list(cc.get("剔除规则版本") or ())

    # ★ 实跑常量必须与预注册逐项对齐，不符即抛（不是警告）。
    #   这是三层锁的第二层：配置改了而代码没跟上，会在开跑前就炸掉。
    PR.assert_run_matches({"期窗": "全量", "规则口径": rule_mode,
                           "剔除规则版本": drop}, cell)

    pool_size = int(const["候选池规模"])
    cap = int(const["重叠上限"])
    pool_seed = int(const["候选池seed"])
    base_seed = int(const["基准seed"])

    issues = PD.build_periods(lottery, None, exclude_rule_versions=drop or None)
    recs = _records_for(lottery, issues)
    if not recs:
        return {"格": cell["名"], "降级": "无可回放期"}
    target_issue = recs[-1].期号

    if verbose:
        prof = PD.profile(lottery, issues)
        print(f"[{cell['名']}] 彩种 {lottery} K={K} 预算 {K*2} 元/期 | "
              f"{prof['期数']} 期 {prof['期号起']}~{prof['期号止']} | "
              f"规则版本 {prof['规则版本数']} 个 {list(prof['规则版本分布'])} | "
              f"口径 {rule_mode}", flush=True)

    t0 = time.time()
    pool = build_pool(lottery, pool_size, seed=pool_seed)
    res0 = TT.resolve_conditions(lottery, target, target_issue)
    p1 = TT.single_ticket_probability(lottery, res0["conditions"], target_issue)
    kp = min(1.0, K * p1)
    if verbose:
        print(f"    池 {len(pool)} | 单注 p={p1:.6e} K·p={kp:.4f} | "
              f"达标奖级 {sorted(res0['grades'])} | 规则版本 {res0['rule_version']}", flush=True)

    arm_names = list(arms) if arms is not None else [
        a["名"] for a in (PR.load().get("臂") or [])]
    _doc = PR.load()
    base_name = _doc["基线臂"]
    # ★ 只有 `进主检验: true` 的臂进多重比较。`prod_mix` 内部 cap=4 ≠ 其余臂 cap=3，
    #   把它算进 BH 的 m 里会稀释阈值（m 从 3 变 4），等于用口径不一致的臂
    #   去放宽主检验 —— 它只作外部保真度对照。
    in_main = {a["名"] for a in (_doc.get("臂") or []) if a.get("进主检验")}

    results: Dict[str, Any] = {}
    rows: List[Dict[str, Any]] = []
    objs: Dict[str, Any] = {}
    for name in arm_names:
        t1 = time.time()
        try:
            tks, meta = A.select_arm(name, lottery, pool, K, target, target_issue,
                                     seed=base_seed, cap=cap,
                                     params=_arm_params(const, len(issues)))
        except NotImplementedError as e:
            results[name] = {"臂": name, "状态": "暂不评估", "原因": str(e)}
            if verbose:
                print(f"    · {name}: 暂不评估 —— {e}", flush=True)
            continue
        except Exception as e:                                     # noqa: BLE001
            results[name] = {"臂": name, "状态": "失败", "原因": f"{type(e).__name__}: {e}"}
            if verbose:
                print(f"    · {name}: 失败 —— {e}", flush=True)
            continue

        ar = RP.replay(lottery, tks, recs, target, rule_mode=rule_mode,
                       target_issue=target_issue, arm=name)
        objs[name] = ar
        row = ar.to_row()
        row.update({k: v for k, v in meta.items() if k not in row})
        row["状态"] = "完成"
        row["选号耗时秒"] = round(time.time() - t1, 1)
        try:
            row["精确P"] = exact_union_probability(lottery, tks, target=target,
                                                   issue=target_issue)
        except Exception as e:                                     # noqa: BLE001
            row["精确P"] = None
            row["精确口径失败"] = f"{type(e).__name__}: {e}"
        results[name] = row
        rows.append(row)
        if verbose:
            ex = f"精确P {row['精确P']:.4f}" if row["精确P"] is not None else "精确P n/a"
            print(f"    · {name}: 出票 {len(tks)}/{K} | 达标率 {row['达标率']:.4f} | "
                  f"哨兵 {row['哨兵_单期达标注数']:.3f} 注 | {ex} | "
                  f"cap污染={meta.get('cap污染')} | {row['选号耗时秒']}s", flush=True)

    # ---- 期窗一致性（硬断言，不允许求交/取众数/截断）
    ok_rows = [r for r in rows if r.get("状态") == "完成"]
    for r in ok_rows[1:]:
        PD.assert_same_periods(objs[ok_rows[0]["臂"]].issues, objs[r["臂"]].issues,
                               what=f"{cell['名']}/{r['臂']}")

    # ---- 配对统计
    pairs: List[Dict[str, Any]] = []
    if base_name in objs:
        for name, o in objs.items():
            if name == base_name:
                continue
            pr = ST.pair(o.hit, objs[base_name].hit,
                         issues_a=o.issues, issues_b=objs[base_name].issues,
                         name_a=name, name_b=base_name)
            e_a = results[name].get("精确P")
            e_b = results[base_name].get("精确P")
            if e_a is not None and e_b is not None:
                pr["外部效应量"] = float(e_a) - float(e_b)
                pr["n*_外部效应"] = (((const["Z_alpha"] + const["Z_power"]) * pr["配对差SD"]
                                      / abs(float(e_a) - float(e_b))) ** 2
                                     if abs(float(e_a) - float(e_b)) > 1e-15 else float("inf"))
                pr["n*_保守"] = max(pr["n*_实测效应"], pr["n*_外部效应"])
                pr["效应量口径"] = "实测（保守取实测/精确的较大 n*）"
            pr = ST.achievable(pr, len(issues))
            # ★ 哨兵判据必须是**统计**的：E[达标注数]=K·p 是关于**期望**的恒等式，
            #   实测样本均值必然有抽样差，用 1e-9 这种绝对门限会 100% 误触发。
            d_s = o.nhit.astype(float) - objs[base_name].nhit.astype(float)
            se_s = float(d_s.std(ddof=1)) / math.sqrt(len(issues))
            pr["哨兵差"] = float(d_s.mean())
            pr["哨兵差SE"] = se_s
            pr["哨兵差z"] = (pr["哨兵差"] / se_s) if se_s > 0 else float("nan")
            pr["进主检验"] = bool(name in in_main)
            pr["★哨兵判据"] = ("实现缺陷嫌疑" if abs(pr["哨兵差z"]) > 4.0
                               else "正常（与 E[达标注数]=K·p 恒等式相容）")
            pairs.append(pr)
            results[name]["配对"] = pr

    # ---- 净收益侧配对（2026-10-24 新增，见 ev/arena/payout.py）
    #   ★ 判读纪律：E[单期实得] 与选哪些票**恒等无关**（E[中奖注数]=K·p 的同一恒等式），
    #     所以这里的期望差本来就应该是 0。显著 ≠ 0 先查实现缺陷/分摊效应，
    #     不要读成「策略赚钱」。
    money_pairs: List[Dict[str, Any]] = []
    if base_name in objs and getattr(objs[base_name], "net", None) is not None:
        for name, o in objs.items():
            if name == base_name or getattr(o, "net", None) is None:
                continue
            mp = ST.pair(o.net, objs[base_name].net, issues_a=o.issues,
                         issues_b=objs[base_name].issues,
                         name_a=name, name_b=base_name, metric="money",
                         effect_source="实测（每期净额，元／期）")
            mp = ST.achievable(mp, len(issues))
            mp["进主检验"] = bool(name in in_main)
            mm = o.money or {}
            money_gate(mp, mm)
            for k in ("净收益", "净收益每100元", "净收益每100元_剔除最大一期",
                      "单次大奖占总实得比", "最大回撤", "最大回撤持续期数",
                      "中奖注数", "中奖期数", "≥5元期占比", "≥10元期占比",
                      "逐票兜底期数", "总实得"):
                if k in mm:
                    mp[f"金额_{k}"] = mm[k]
            money_pairs.append(mp)
            results[name]["净收益配对"] = mp
            if verbose:
                print(f"    ¥ {name}: 净额差 {mp['实测Δ']:+.2f} 元/期 "
                      f"(z={mp['z_实测']:+.2f}, CI [{mp['CI95下']:+.2f},{mp['CI95上']:+.2f}]) | "
                      f"净收益 {mm.get('净收益', 0):,.0f} 元 | "
                      f"{mm.get('净收益每100元', 0):.2f} 元/100元 | {mp['判决']}", flush=True)

    # ---- 多重比较校正（只对进主检验的臂）
    adj = None
    main_pairs = [p for p in pairs if p.get("进主检验")]
    if main_pairs:
        pvals = [ST.z_to_p_two_sided(p["z_实测"]) for p in main_pairs]
        adj = ST.adjust_multiplicity(pvals, method="bh", alpha=0.05)
        for p, ap in zip(main_pairs, adj["校正后p"]):
            p["p_BH"] = ap
            p["BH通过"] = bool(ap <= 0.05)

    return {
        "格": cell["名"], "主假设": bool(cell.get("主假设")),
        "彩种": lottery, "K": K, "预算元每期": K * int(const["单注成本元"]),
        "目标": res0["label"], "规则版本": res0["rule_version"],
        "期数": len(issues), "期号起": issues[0], "期号止": issues[-1],
        "规则口径": rule_mode,
        "单注达标概率": p1, "理论上限Kp": kp,
        "随机基线": 1.0 - (1.0 - p1) ** K,
        "常量": const,
        "臂": results, "配对": pairs, "净收益配对": money_pairs,
        "多重比较": adj,
        "耗时秒": round(time.time() - t0, 1),
    }
