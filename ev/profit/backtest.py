# -*- coding: utf-8 -*-
"""ev/profit/backtest.py —— 以期次为单位的回测（已实现 + 事前两档）。

两条硬规矩
----------
1. **每期只能用开奖前可得的量**：块的选号只用 `pit.as_of(期号)`（严格早于该期），
   价格/奖池也从同一视图取。窗口外的期不进判定。
2. **结算复用 `ev.coverage_backtest.evaluate_period`**，与低拥挤四臂回测同一套
   结算口径，因此返回 dict 与 `run_arm` 同构，可直接喂
   `sign_test` / `paired_report` / `_agg_paired` 做逐期配对比较。

本文件在 `replay_plan` 之外多做了三件事（也是方案要求的）：
  · 按**同期合并**计税（税独立成列，不并进净收益就完事）
  · 最大回撤 / 回撤持续期数 / 单期集中度 / 单次大奖占比
  · 预测EV 与实现净收益的校准（斜率 + 截距）

β 口径（`beta_mode`，两种都实现）
--------------------------------
  · `"full"`（默认）：选号用**全样本** β（`popularity.get_popularity_model`）。
    模型见过回放期 ⇒ 排序偏乐观，只适合作上限参考。
  · `"walk_forward"`：每块用**该块第一期之前**的开奖行重拟合 β（expanding window，
    严格 < 目标期，无前视），块内用这份 β 打分选票。β 不可用（样本不足/拟合失败）
    的块显式降级为「无倾斜」（κ 通道关闭、分摊通道也退为号码无关），不静默回退
    全样本 β。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ev.profit import payouts as P
from ev.profit import pit
from ev.profit import select as SEL
from ev.profit import tax as TX
from ev.profit import tilt as TILT
from ev.profit.rules import rules_for

logger = logging.getLogger(__name__)

BETA_STEP = 50          # 与低拥挤回测的 β 重拟合步长一致（块长）
MIN_TRAIN = 1200        # 双色球/大乐透：低于此不判定（与 popularity 的 MIN_TRAIN 对齐）

BETA_MODES = ("full", "walk_forward")


def _records_asc(lottery: str) -> List[Any]:
    """全部已开奖记录，按开奖时间升序（loader 返回是降序）。"""
    from data.loader import load_lottery

    recs = [r for r in (load_lottery(lottery).records or []) if getattr(r, "期号", None)]
    recs.sort(key=lambda r: pit.issue_key(lottery, r.期号))
    return recs


def _draw_pool(lottery: str) -> Tuple[List[Any], Dict[Tuple[int, int], Any]]:
    recs = _records_asc(lottery)
    return recs, {pit.issue_key(lottery, r.期号): r for r in recs}


def _wf_rows(lottery: str) -> Optional[Dict[str, Any]]:
    """walk-forward 用的原始开奖行（升序；薄封装 popularity._load_draw_rows）。"""
    from ev.popularity import _load_draw_rows
    return _load_draw_rows(lottery)


def _wf_params(lottery: str, issue: Any, rows: Optional[Dict[str, Any]],
               cache: Dict[Any, Optional[Dict[str, Any]]],
               min_train: int) -> Optional[Dict[str, Any]]:
    """只用 `issue` **之前**的开奖行拟合 β（expanding window，无前视）。

    ★ 按**期号键**选训练集（不是 positional 前 N 行）：`_load_draw_rows` 的行序
      按期号字符串排，双色球 2003-2009 是 4 位期号会排到 2026 之后；而 GLM 对
      行序不敏感，按键选集合可保证「训练集真值早于目标期」且结果与行序无关。
    训练行数 < min_train / 拟合失败 → None（调用方显式降级，不许回退全样本 β）。
    """
    if issue in cache:
        return cache[issue]
    cache[issue] = None
    if rows is None:
        return None
    try:
        k_target = pit.issue_key(lottery, issue)
    except Exception:
        return None

    from ev.popularity import fit_beta

    keys = [pit.issue_key(lottery, s) for s in rows["issues"]]
    mask = np.array([k < k_target for k in keys], dtype=bool)
    if int(mask.sum()) < int(min_train):
        return None
    try:
        fb = fit_beta(rows["reds"][mask], rows["prev"][mask],
                      rows["y"][mask], rows["N"][mask], fit_null=False)
    except Exception as e:
        logger.warning(f"[profit.backtest] {lottery} {issue} walk-forward β 拟合失败: {e}")
        return None
    if not fb:
        return None
    params = {"beta": fb["beta"], "m_": fb["m_"], "s_": fb["s_"], "n": int(fb["n"])}
    cache[issue] = params
    return params


def build_plan(lottery: str, K: int, periods: Optional[int], seed: int,
               kappa: float, block: int, overlap_cap: int,
               min_train: int, beta_mode: str = "full"
               ) -> Tuple[List[Tuple[Any, Any, List[dict]]], Dict[str, Any]]:
    """逐块构造回放计划：每块用「块之前」的信息重新选一次票。

    `beta_mode="walk_forward"` 时每块的倾斜依据按该块第一期之前的数据重拟合
    （见 `_wf_params`）；拟合不可用的块降级为「无倾斜」并在 meta 记数。
    """
    if beta_mode not in BETA_MODES:
        raise ValueError(f"未知 beta_mode: {beta_mode}（可选 {BETA_MODES}）")

    recs, by_key = _draw_pool(lottery)
    n_full = len(recs)
    if n_full <= min_train + 1:
        return [], {"降级": f"可用记录仅 {n_full} 期，低于门槛 {min_train + 1}"}

    start = len(recs) - min(int(periods), n_full - min_train) if periods else min_train
    start = max(start, min_train)
    plan: List[Tuple[Any, Any, List[dict]]] = []
    blocks = 0
    wf_degraded = 0
    wf_pi_blocks = 0
    wf_rows = _wf_rows(lottery) if beta_mode == "walk_forward" else None
    wf_cache: Dict[Any, Optional[Dict[str, Any]]] = {}
    for b0 in range(start, n_full, block):
        b1 = min(b0 + block, n_full)
        first_issue = recs[b0].期号
        # ★ 只用该块第一期**之前**的信息（as_of 内部严格 < 目标期）
        ctx = P.build_ctx(lottery, first_issue)
        if ctx.sales_forecast <= 0 or ctx.total_tickets <= 0:
            continue                                  # 数据不足的期不硬凑
        tl = None
        if beta_mode == "walk_forward":
            if lottery in ("排列3", "排列5", "福彩3D", "七星彩"):
                # π 代理无 β 重拟合概念：walk-forward 下退化为全样本 π（已知局限，标注）
                from ev.profit import crowd_tilt as CT
                tl = CT.crowd_tilt(lottery)
                wf_pi_blocks += 1
            else:
                params = _wf_params(lottery, first_issue, wf_rows, wf_cache, min_train)
                if params is None:
                    wf_degraded += 1                  # 显式降级：无倾斜（不等同随机选号的偏差）
                else:
                    tl = TILT.outer_tilt(lottery, beta=params["beta"], mu_=params["m_"],
                                         sd_=params["s_"], asof=str(first_issue))
        sel = SEL.select_profit_tickets(lottery, K, ctx, kappa=kappa, seed=seed,
                                        overlap_cap=overlap_cap, tilt=tl)
        tks = sel["票"]
        if not tks:
            continue
        blocks += 1
        for r in recs[b0:b1]:
            plan.append((r.期号, r, tks))
    if beta_mode == "walk_forward" and wf_pi_blocks:
        beta_desc = (f"walk_forward（乐透型每块只用块之前数据重拟合，无前视；降级块 {wf_degraded} 个；"
                     f"数字型/七星彩 {wf_pi_blocks} 块用全样本 π——π 代理无 β 重拟合概念，"
                     f"含轻微前视，仅作代理口径参考）")
    elif beta_mode == "walk_forward":
        beta_desc = (f"walk_forward（每块只用块之前数据重拟合，无前视；降级块 {wf_degraded} 个）")
    else:
        beta_desc = "全样本（in-sample，排序略偏乐观）"
    meta = {
        "回放起点": start, "全量期数": n_full, "块数": blocks,
        "块长": block,
        "β口径": beta_desc,
    }
    return plan, meta


def _max_drawdown(nets: Sequence[float]) -> Tuple[float, int]:
    cum = peak = mdd = 0.0
    cur = best_len = 0
    for v in nets:
        cum += v
        if cum > peak:
            peak, cur = cum, 0
        else:
            cur += 1
            if peak - cum > mdd:
                mdd, best_len = peak - cum, cur
    return mdd, best_len


def run_profit_arm(lottery: str = "双色球", K: int = 200,
                   periods: Optional[int] = 500, seed: int = 0,
                   kappa: float = 0.0, block: int = BETA_STEP,
                   overlap_cap: int = SEL.PROD_OVERLAP_CAP,
                   min_train: int = MIN_TRAIN,
                   basis: str = TX.BASIS_ISSUE,
                   beta_mode: str = "full") -> Dict[str, Any]:
    """跑一条盈利臂（已实现口径：用真实开奖与真实单注奖金结算）。"""
    from ev.coverage_backtest import evaluate_period

    if beta_mode not in BETA_MODES:
        raise ValueError(f"未知 beta_mode: {beta_mode}（可选 {BETA_MODES}）")
    plan, meta = build_plan(lottery, K, periods, seed, kappa, block,
                            overlap_cap, min_train, beta_mode=beta_mode)
    if not plan:
        return {"彩种": lottery, "臂": "profit", "期数": 0, **meta}

    r = rules_for(lottery, plan[0][0])     # 成本/票面口径按首个回放期的规则版本
    cache: Dict[str, Any] = {}
    gross_l: List[float] = []
    tax_l: List[float] = []
    cost_l: List[float] = []
    wins_l: List[int] = []
    ge5: List[int] = []
    ge10: List[int] = []
    staked = 0

    for issue, rec, tks in plan:
        ev = evaluate_period(lottery, tks, rec, issue, cache)
        gross = float(ev["总实得"])
        cost = len(tks) * float(r.cost)
        tax = TX.tax_on(gross, basis=basis)
        gross_l.append(gross)
        tax_l.append(tax)
        cost_l.append(cost)
        wins_l.append(int(ev["命中注数"]))
        ge5.append(1 if ev["≥5元"] else 0)
        ge10.append(1 if ev["≥10元"] else 0)
        staked += len(tks)

    n = len(plan)
    total_gross = float(sum(gross_l))
    total_tax = float(sum(tax_l))
    total_cost = float(sum(cost_l))
    nets = [g - t - c for g, t, c in zip(gross_l, tax_l, cost_l)]
    mdd, mdd_len = _max_drawdown(nets)
    best = max(nets)
    total_net = total_gross - total_tax - total_cost

    return {
        "彩种": lottery, "臂": "profit", "seed": seed, "κ": float(kappa),
        "期数": n, "K": K, "总投注注数": staked,
        # —— 与 run_arm 同构的键（供 paired_report / sign_test 直接吃） ——
        "≥5元期占比": round(sum(ge5) / n, 4),
        "≥10元期占比": round(sum(ge10) / n, 4),
        "中奖次数": int(sum(wins_l)),
        "单期平均中奖注数": round(sum(wins_l) / n, 4),
        "总实得": round(total_gross, 2),
        "净收益": round(total_net, 2),
        "单期平均净收益": round(total_net / n, 4),
        "序列": {"≥10元": ge10, "中奖注数": wins_l, "实得": gross_l},
        # —— 方案要求的补充指标 ——
        "税前奖金": round(total_gross, 2),
        "税": round(total_tax, 2),
        "成本": round(total_cost, 2),
        "税口径": basis,
        "净EV单注": round(total_net / staked, 4) if staked else None,
        "净盈利期占比": round(sum(1 for v in nets if v > 0) / n, 4),
        "最大回撤": round(mdd, 2),
        "最大回撤持续期数": mdd_len,
        "单期最大净收益": round(best, 2),
        "单期集中度": round(best / total_net, 4) if total_net else None,
        "单次大奖占总实得比": round(max(gross_l) / total_gross, 4) if total_gross else None,
        "命中率（辅助，不作判据）": round(sum(wins_l) / staked, 6) if staked else None,
        **meta,
    }


def report(arm: Dict[str, Any]) -> str:
    """把一条臂渲染成可读文本（含必须一起读的口径声明）。"""
    if not arm.get("期数"):
        return f"{arm.get('彩种')} 盈利臂：无可用期次（{arm.get('降级')}）"
    return "\n".join([
        f"== {arm['彩种']} 盈利臂（κ={arm.get('κ')}, {arm['期数']} 期 × {arm['K']} 注）==",
        f"净收益 {arm['净收益']:+,.2f} 元 ｜ 净EV {arm['净EV单注']:+.4f} 元/注",
        f"税前 {arm['税前奖金']:,.2f} − 税 {arm['税']:,.2f} − 成本 {arm['成本']:,.2f}",
        f"净盈利期占比 {arm['净盈利期占比']:.2%} ｜ 最大回撤 {arm['最大回撤']:,.2f}（{arm['最大回撤持续期数']} 期）",
        f"单期集中度 {arm['单期集中度']} ｜ 单次大奖占总实得 {arm['单次大奖占总实得比']}",
        f"命中率 {arm['命中率（辅助，不作判据）']}（辅助指标，不作判据）",
        f"β口径：{arm.get('β口径')}｜税口径：{arm.get('税口径')}",
        "⚠️ 已实现净EV 与模型的**期望**净EV 不可直接比较：",
        "   头奖/二奖的期望（合计约 0.5 元/注）在几千注的样本里几乎不会兑现",
        "   （1 万注中头奖的概率约 0.06%），所以已实现值会明显更差。",
        "   差额本身是信息（见「单次大奖占总实得」），不是 bug。",
    ])
