# -*- coding: utf-8 -*-
"""ev/profit/solvers.py —— 三个**独立**判决路径 + 判定树。

用户要求「1+2+3 分为不同策略」，所以三者各有独立入口、独立判据、独立文案，
**互不调用**（只有 C 反向裁判 A）：

  A `solve_kappa_star`       反解盈亏平衡所需的倾斜强度 κ*，与已测上界比较
  B `solve_scenario`         把 κ 乘以倍数 0/1/2/3 扫一遍，看「即便更强也不够」
  C `solve_monte_carlo`      抽样估计净盈利概率，用 CI 上界判决

共同前提（不满足就**不出结论**，不是给个近似值）：
  · κ=0 的净EV 必须为负（为正说明模型或数据有 bug）
  · 拥挤度必须可用（否则给不了参与判决）
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

import numpy as np

from ev.profit import evaluate as E
from ev.profit import payouts as P
from ev.profit import pit
from ev.profit import tilt as T
from ev.profit import tax as TX
from ev.profit import crowd_tilt as CT
from ev.profit.rules import rules_for

# κ* 求解用的网格（每点一次精确评估；点越多越慢）
KSTAR_GRID = (0.0, 0.1, 0.2, 0.35, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0)

VERDICT_PARTICIPATE = "条件性可参与"
VERDICT_REJECT = "本期不建议参与"
VERDICT_NO_DATA = "不出结论（数据不足）"

# 判决等级（三档）：完整（乐透 measured_GO）/ 条件性（代理未检验）/ 不出结论
VERDICT_LEVEL_FULL = "完整"
VERDICT_LEVEL_CONDITIONAL = "条件性（代理未检验）"
VERDICT_LEVEL_NONE = "不出结论"


def _ev_single(lottery: str, tickets: Sequence[dict], ctx: P.Ctx, kappa: float,
               tax_basis: str = TX.BASIS_ISSUE) -> Dict[str, Any]:
    m = E.exact_metrics(lottery, tickets, ctx, kappa=kappa, tax_basis=tax_basis)
    return {"κ": float(kappa), "净EV单注": float(m["净EV单注"]),
            "净EV": float(m["净EV"]), "P至少中一注": float(m["P至少中一注"]),
            "中奖注数期望": float(m["中奖注数期望"]), "税前期望": float(m["税前期望"]),
            "期望税": float(m["期望税"]), "税口径": m["税口径"]}


def solve_kappa_star(lottery: str, tickets: Sequence[dict], ctx: P.Ctx,
                     grid: Sequence[float] = KSTAR_GRID,
                     tax_basis: str = TX.BASIS_ISSUE) -> Dict[str, Any]:
    """A：反解净EV 单注 = 0 的 κ（盈亏平衡所需机制强度）。

    单调性不假设：先在网格上算 EV(κ)，判 `单调`；单调则在穿越区间线性插值，
    非单调则取**首个**穿越点并把非单调区间写进备注。
    """
    pts = [_ev_single(lottery, tickets, ctx, k, tax_basis=tax_basis) for k in grid]
    evs = [p["净EV单注"] for p in pts]
    mono = all(evs[i + 1] >= evs[i] - 1e-12 for i in range(len(evs) - 1))

    root: Optional[float] = None
    for i in range(len(evs) - 1):
        a, b = evs[i], evs[i + 1]
        if (a < 0) != (b < 0):
            lo, hi = pts[i]["κ"], pts[i + 1]["κ"]
            fa, fb = a, b
            root = lo + (0 - fa) / (fb - fa) * (hi - lo) if fb != fa else lo
            break

    return {
        "求解器": "kappa_star",
        "曲线": pts,
        "单调": bool(mono),
        "κ*": (round(root, 4) if root is not None else None),
        "κ*是否有解": root is not None,
        "EV@κ=0": pts[0]["净EV单注"],
        "EV@κ_max": pts[-1]["净EV单注"],
        "备注": ("EV(κ) 在网格上非单调，κ* 取首个穿越点，含义为「最低所需强度」。"
                 if not mono else "EV(κ) 在网格上单调不减。"),
    }


def solve_scenario(lottery: str, tickets: Sequence[dict], ctx: P.Ctx,
                   kappa_ref: float, multipliers: Sequence[int] = (0, 1, 2, 3)) -> Dict[str, Any]:
    """B：情景倍数扫描。倍数**只乘 κ**，不乘概率/奖金/μ。

    1× 的锚点是「已测机制上界」而不是我们的估计；2×/3× 是已被数据排除的更强机制，
    保留它们只为展示「即便更强也不够」。
    """
    rows = []
    for m in multipliers:
        k = float(m) * float(kappa_ref)
        d = _ev_single(lottery, tickets, ctx, k)
        rows.append({
            "倍数": int(m), "κ": round(k, 4),
            "相对均匀的概率提升": round(T.cold_boost(T.outer_tilt(lottery), k), 4)
            if T.outer_tilt(lottery) else None,
            **{kk: d[kk] for kk in ("净EV单注", "P至少中一注", "中奖注数期望")},
        })
    return {
        "求解器": "scenario_multiplier",
        "锚点κ_ref": round(float(kappa_ref), 4),
        "情景": rows,
        "口径": "敏感性扫描，不是对机制强度的估计",
        "禁止读法": "m=2 不代表『我们估出了 2 倍』；倍数作用于 tilt 指数 κ，不是概率",
        "不变式": "m=0 必须精确退回组合概率（κ=0 ⇒ 均匀）",
    }


def solve_monte_carlo(lottery: str, tickets: Sequence[dict], ctx: P.Ctx,
                      kappa: float = 0.0, n: int = 20000, seed: int = 0) -> Dict[str, Any]:
    """C：抽样估计净盈利概率（+ 与解析净EV 的裁判比对）。

    存在意义不是算均值（均值可精确算），而是：① 独立第二实现互相当裁判；
    ② 分布类指标在回测里的降本；③ 给「内层独立抽签」这类解析不好写的量兜底。
    """
    mc = E.simulate_net_prob(lottery, tickets, ctx, kappa=kappa, n=n, seed=seed)
    exact = _ev_single(lottery, tickets, ctx, kappa)
    se = float(np.sqrt(max(mc["净盈利概率"] * (1 - mc["净盈利概率"]) / n, 1e-12)))
    return {
        "求解器": "monte_carlo",
        "κ": float(kappa), "样本数": int(n),
        "净盈利概率": mc["净盈利概率"],
        "净盈利概率_标准误": se,
        "净盈利概率_95CI": [max(0.0, mc["净盈利概率"] - 1.96 * se),
                            min(1.0, mc["净盈利概率"] + 1.96 * se)],
        "解析净EV单注": exact["净EV单注"],
        "定位": "回测降本 + 对精确解的偏差校验；决策以解析解为准，本项数字标『估算(MC)』",
    }


# ---------------------------------------------------------------- 判定树

def _decide_core(lottery: str, issue: Any, tickets: Optional[Sequence[dict]] = None,
                 K: int = 20, min_history: int = 1200, allow_mc: bool = False,
                 seed: int = 0) -> Dict[str, Any]:
    """完整判定：数据门 → κ=0 基线 → 拥挤度门 → κ* → 判决。

    tickets=None 时先用 `select.select_profit_tickets` 按净EV 代理选 K 注
    （拿随机票永远解不出 κ* —— 机制偏向冷门只会稀释随机票）。

    ★ 任何一步不满足都输出 `不出结论`/`不建议参与`，**不给近似数字冒充结论**。
    """
    out: Dict[str, Any] = {"彩种": lottery, "期号": str(issue),
                           "税口径": TX.BASIS_ISSUE,
                           "判决等级": VERDICT_LEVEL_NONE}

    # 0. 早期数据门：代理 π 本身不可用（如福彩3D 直选注数自 2022 起全 0）
    early = CT.unavailable_reason(lottery)
    if early is not None:
        out.update({"判决": VERDICT_NO_DATA, "原因": early})
        return out

    # 0b. 构造 PIT 视图（只用开奖前信息）；同时喂给 build_ctx 避免双重加载
    view = pit.as_of(lottery, issue, min_history=min_history)
    ctx = P.build_ctx(lottery, issue, view=view)
    r = ctx.rules
    out["规则版本"] = r.rule_version
    if len(view.history) < min_history:
        out.update({"判决": VERDICT_NO_DATA,
                    "原因": f"开奖/销售历史仅 {len(view.history)} 期，低于门槛 {min_history}"
                            f"（数字型销售额数据仅 2016-07 起有效）⇒ 不出结论"})
        return out
    if ctx.sales_forecast <= 0:
        out.update({"判决": VERDICT_NO_DATA, "原因": "本期销量不可得（历史销售缺失）"})
        return out
    if ctx.total_tickets <= 0:
        out.update({"判决": VERDICT_NO_DATA, "原因": "票量推算为 0"})
        return out
    out["上下文"] = {
        "销量预测": ctx.sales_forecast, "上一期奖池": ctx.pool_before,
        "推算票量": ctx.total_tickets, "浮动池": ctx.float_pool,
        "警告": ctx.warnings,
    }
    out["奖金口径"] = P.note(ctx)

    # 0c. 选号（没给票就先按净EV 代理选）
    if tickets is None:
        from ev.profit import select as SEL
        sel = SEL.select_profit_tickets(lottery, K, ctx, kappa=0.0, seed=seed)
        tickets = sel["票"]
        # 同时保留「票」与「元数据」：情景扫描与台账落账都读 d["选号"]["票"]
        out["选号"] = {"票": sel["票"], **sel["元数据"]}
    out["注数"] = len(tickets)
    if not tickets:
        out.update({"判决": VERDICT_NO_DATA, "原因": "选号结果为空"})
        return out

    # 1. κ=0 基线（必须为负，否则是 bug）
    base = _ev_single(lottery, tickets, ctx, 0.0)
    out["κ0基线"] = base
    if base["净EV单注"] >= 0:
        out.update({"判决": VERDICT_NO_DATA,
                    "原因": f"κ=0 的净EV 为 {base['净EV单注']:+.4f} ≥ 0，与组合精确概率矛盾，"
                            f"按 bug 处理（不出结论）"})
        return out

    # 2. 拥挤度门
    tl = T.outer_tilt(lottery)
    if tl is None:
        out.update({"判决": VERDICT_REJECT, "判决等级": VERDICT_LEVEL_NONE,
                    "原因": "拥挤度打分不可用（模型未 Go 或不支持该彩种）⇒ 无法评估机制倾斜，"
                            "只能拒绝参与"})
        return out

    # 判决等级：乐透 measured_GO = 完整；代理 π / walk_forward = 条件性（代理未检验）
    判决等级 = (VERDICT_LEVEL_FULL if tl.source == "measured_GO"
               else VERDICT_LEVEL_CONDITIONAL)
    out["判决等级"] = 判决等级

    # 3. κ* 与上界（按拥挤度来源分发到正确的上界估计器）
    ks = solve_kappa_star(lottery, tickets, ctx)
    up = (T.kappa_upper_marginal(lottery, tl)
          if tl.source == CT.CROWD_PI_SOURCE else T.kappa_upper(lottery))
    out["κ*求解"] = ks
    out["机制强度上界"] = up

    if not ks["κ*是否有解"]:
        out.update({"判决": VERDICT_REJECT,
                    "原因": f"在 κ∈[0,{T.KAPPA_MAX}] 内净EV 始终为负（κ_max 时 "
                            f"{ks['EV@κ_max']:+.4f} 元/注）⇒ 本期不建议参与"})
        return out

    kstar = ks["κ*"]
    k_up = (up.get("kappa_upper", up.get("kappa")) if up.get("可用") else None)
    out["缺口倍数"] = (round(kstar / k_up, 2) if k_up and k_up > 0 else None)

    if k_up is None or kstar > k_up:
        out.update({"判决": VERDICT_REJECT,
                    "原因": (f"需要 κ*={kstar:.3f} 才能不亏，而已测机制强度上界仅 "
                             f"κ_upper={k_up}（缺口 {out['缺口倍数']}×）⇒ 本期不建议参与"
                             if k_up else
                             f"需要 κ*={kstar:.3f}，但机制强度上界不可得 ⇒ 本期不建议参与")})
        return out

    if 判决等级 == VERDICT_LEVEL_FULL:
        out.update({"判决": VERDICT_PARTICIPATE,
                    "原因": f"κ*={kstar:.3f} ≤ 已测上界 {k_up}，且稳健性检查见下",
                    "注意": "「条件性可参与」仍需通过稳健性检查（β 形状误差、销量 P10/P90、"
                            "池模型两口径、税两口径）后才可作建议；本模型不保证盈利。"})
    else:
        out.update({"判决": VERDICT_PARTICIPATE,
                    "原因": "条件性可参与（拥挤度为**未检验代理**，不得作为投注建议）",
                    "注意": "代理 π 无 Go 门槛、位置独立是结构假设；本结论仅表示在代理口径下"
                            "机制强度上界未被 κ* 否定，不构成投注建议。"})

    if allow_mc:
        out["MC校验"] = solve_monte_carlo(lottery, tickets, ctx, kappa=0.0, n=5000)
    return out


def decide(lottery: str, issue: Any, tickets: Optional[Sequence[dict]] = None,
           K: int = 20, min_history: int = 1200, allow_mc: bool = False,
           seed: int = 0, *, envelope: Any = None,
           spent_this_month: float = 0.0,
           periods_done: Optional[int] = None,
           periods_total: Optional[int] = None) -> Dict[str, Any]:
    """`_decide_core` + 可选的**娱乐预算包络闸门**（v1.8，用户指令一）。

    ★ 默认 `envelope=None` ⇒ 与 v1.7 返回**逐位相同**（不新增任何键），
      这样既有调用点与快照测试不受影响。
    ★ 传入 `envelope` 时只做一件事：把上游判决与包络**取更保守者**
      （见 `ev/budget_envelope.gate_verdict`，只降级不升级）。
      它不重判 EV —— EV 归本判定树，闸门只管「本月还剩多少钱」。
    ★ `periods_done` / `periods_total`（v1.9）：传了才启用**机制①滚动分摊**
      （单期上限 = 剩余额度 ÷ 剩余期数）；不传 ⇒ 静态单期上限，与 v1.8 一致。
    """
    out = _decide_core(lottery, issue, tickets=tickets, K=K,
                       min_history=min_history, allow_mc=allow_mc, seed=seed)
    if envelope is None:
        return out

    from ev import budget_envelope as BE
    gate = BE.gate_verdict(out.get("判决"), envelope, spent_this_month,
                           want_tickets=out.get("注数"),
                           periods_done=periods_done,
                           periods_total=periods_total)
    out["包络"] = gate
    if gate["是否降级"]:
        out["上游判决"] = out.get("判决")
        out["判决"] = gate["最终判决"]
        out["原因"] = gate["原因"]
        out["注数"] = 0
    else:
        out["注数"] = int(gate["注数"])
    return out
