# -*- coding: utf-8 -*-
"""ev/arena/payout.py —— 净收益侧的**向量化**结算（逐票奖级 → 金额 → 每期实得）。

为什么不能直接复用 `grade_ticket` 逐票
------------------------------------
`grade_ticket` 是逐票算的，实测 1.8 s/期（双色球 K=200）⇒ 3,518 期 × 5 臂 ≈ 8.8 小时。
但金额侧真正的成本是「**每期每奖级**取一次单注实得」，与票数无关 —— 只要把它压成
一张 (h, j) → 金额 的小表（7×3 ~ 6×3），就能对 (K, n) 命中矩阵做**一次**花式索引。

    h, j ≈ replay 已有的 (K, n) 命中矩阵
    pay  = amount_grid[h, j]        # (K, n)，每票每期实得
    每期实得 = pay.sum(axis=0)

★ 快路必须有审计对账：`scripts/oneoff/_arena_money_check.py` 用 `evaluate_period`
  逐期对账「每期实得」，要求**逐期逐分相等**（不是「平均相等」）。

三个口径陷阱（都写在这里，因为选错不会报错，只会算错）
----------------------------------------------------
1. **金额必须跟随「判级所用规则版本」，不是当期版本（`current` 模式）**
   大乐透 5 个规则版本的固定奖金额不同（2007 年四等 500 元 / 2026 年四等 200 元）。
   `current` 模式判级用最新规则，若金额还按当期版本发钱，就会出现
   「判成 2026 四等、发 2007 的钱」。故 `amount_grid(rule_issue=...)` 显式分离：
     · `rule_issue` = 判级版本（决定 grade_table 与固定奖兜底额）
     · `issue`      = 当期期号（决定查库实测与派奖）
2. **浮动奖的兜底路径是票依赖的**
   `_settle_by_rules` 浮动奖取值顺序是「库里当期实测 → `payout_eff`(票)」。
   前者与票无关（可进网格），后者依赖票面号码（不能进网格）。
   ⇒ 无实测的浮动奖级 ⇒ 该期标记 `需逐票兜底` 并**回退逐票 `grade_ticket`**，计数上报。
   **绝不**静默用 0 或名义值顶替。
3. **主口径税前**
   `evaluate_period.总实得` 是税前。税后口径单列（`tax=True`），因为 K 大时同期
   合并计税会频繁越过 1 万元阈值，金额侧变成阶跃噪声（`ev/profit/tax.py` 已固化）。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ev.arena import COST_PER_BET
from ev.profit.rules import rules_for

RULE_MODES = ("periodic", "current")


# ---------------------------------------------------------------- 规则与金额
def _rules_safe(lottery: str, issue: Any) -> Tuple[Any, str]:
    """按期号取规则版本；期号不可解析时回退现行版并**显式标注**。

    ★ 只捕 `IssueKeyError`（合成期号，如测试里的 "TEST"）。
      真实早期期号的 `RuleVersionError` 必须向上抛 —— 拿新规则结算旧期号
      是错的，宁可炸掉也不要算出一个「看着正常」的数。
    """
    from data.issue_key import IssueKeyError
    try:
        return rules_for(lottery, issue), "规则表"
    except IssueKeyError:
        return rules_for(lottery), "规则表(期号不可解析→现行版)"


def grade_amounts(lottery: str, issue: Any, *, rule_issue: Any = None
                  ) -> Tuple[Dict[str, float], Dict[str, str], List[str]]:
    """该期「奖级 → 单注实得」+「奖级 → 口径 note」+ 需要逐票兜底的浮动奖级。

    `rule_issue`：判级所用规则版本的期号（`current` 模式传目标期号）。
                  决定 `grade_table` 与固定奖兜底额；**不**影响查库期号。
    `issue`     ：当期期号，决定查哪个期的实测/派奖。

    ★★ 判级版本 ≠ 当期版本时（大乐透 `current` 口径），**固定奖一律取判级版本的
       固定额、不按名字查库**。理由：大乐透 5 个规则版本里同名奖级的含金量完全
       不同 —— 2019 规则 (2,1) 是九等 5 元，2026 规则 (2,1) 是七等 5 元；若判级
       用 2026 表、金额按名字查 2019 期的「七等」（=100 元），固定奖会整体放大
       约 6 倍（实测：大乐透 current 口径返奖率 163.8%）。浮动奖（一等/二等）
       跨版本含义稳定，仍取当期实测。
    """
    from data import prize_amounts as PA

    grading = rule_issue if rule_issue is not None else issue
    r, _basis = _rules_safe(lottery, grading)
    mixed = False
    if rule_issue is not None and str(rule_issue) != str(issue):
        try:
            mixed = _rules_safe(lottery, issue)[0].rule_version != r.rule_version
        except Exception:
            mixed = True

    amt: Dict[str, float] = {}
    notes: Dict[str, str] = {}
    fallback: List[str] = []
    for grade, rule in r.grades.items():
        rule_amt = (float(rule.fixed) if (rule.kind == "fixed" and rule.fixed is not None)
                    else None)
        if mixed and rule_amt is not None:
            # 同名奖级跨版本已变义 ⇒ 只用判级版本的固定额
            amt[grade] = rule_amt
            notes[grade] = "规则表(判级版本)"
            continue
        got, note = PA.actual_per_bet(lottery, issue, grade, rule_amt)
        if got is not None:
            amt[grade] = float(got)
            notes[grade] = note
        elif rule_amt is not None:
            # 库里没这期数据 ⇒ 用**判级版本**的固定奖额（不是当期版本的）
            amt[grade] = rule_amt
            notes[grade] = "规则表"
        else:
            fallback.append(grade)
            notes[grade] = "payout_eff(票依赖)"
    return amt, notes, fallback


def amount_grid(lottery: str, issue: Any, *, rule_issue: Any = None
                ) -> Tuple[np.ndarray, Dict[str, Any]]:
    """`(h, j) → 单注实得` 金额网格 `(w_out+1, m_in+1)` + 审计信息。

    grid[h, j] = 该 (外层命中 h, 内层命中 j) 对应的奖级单注实得；未中为 0。

    ★ 返回的 meta 里有两个「待定」信号：
      · `需逐票兜底`：**无实测的浮动奖级名单**（这些奖级的金额依赖票面）；
      · `待定格`：布尔网格，标出哪些 (h, j) 格的金额还没定。
      调用方必须用后者判断「**本期真的命中它了吗**」——
      早期期段浮动奖普遍没有实测值，但一个期里没中一等/三等时，那一格
      根本用不到 ⇒ 不该整期回退逐票（否则 52% 的期都退化成 1.8 s/期）。
    """
    r, basis = _rules_safe(lottery, rule_issue if rule_issue is not None else issue)
    amt, notes, fallback = grade_amounts(lottery, issue, rule_issue=rule_issue)
    w_out = int(r.outer_width)
    m_in = max(jj for (_h, jj) in r.grade_table)
    grid = np.zeros((w_out + 1, m_in + 1), dtype=np.float64)
    pending = np.zeros((w_out + 1, m_in + 1), dtype=bool)
    names: List[List[str]] = [["" for _ in range(m_in + 1)] for _ in range(w_out + 1)]
    fb = set(fallback)
    for (hh, jj), gname in r.grade_table.items():
        if hh <= w_out and jj <= m_in:
            grid[hh, jj] = float(amt.get(gname, 0.0))
            names[hh][jj] = gname
            if gname in fb:
                pending[hh, jj] = True
    return grid, {"规则版本": r.rule_version, "期号基准": basis,
                  "奖级金额": amt, "口径": notes, "需逐票兜底": fallback,
                  "待定格": pending, "奖级网格": names}


# ---------------------------------------------------------------- 命中矩阵
def hit_matrices(lottery: str, tickets: Sequence[Dict[str, Any]],
                 records: Sequence[Any], issue: Any) -> Tuple[np.ndarray, np.ndarray]:
    """`(K, n)` 的外层 / 内层命中数矩阵（与 `replay` 同一口径）。"""
    from ev.arena.design import _onehot
    from ev.arena.replay import draw_vectors
    from ev.tier_coverage import _inner_pool_size

    r = rules_for(lottery, issue)
    m_in = max(jj for (_h, jj) in r.grade_table)
    n_in = _inner_pool_size(r, m_in)
    n_out = int(r.outer_range[1])
    A = _onehot(tickets, "红球", n_out)
    Bm = _onehot(tickets, "蓝球", n_in)
    R, B = draw_vectors(lottery, list(records), n_out, n_in)
    return (A.astype(np.int16) @ R.T.astype(np.int16),
            Bm.astype(np.int16) @ B.T.astype(np.int16))


# ---------------------------------------------------------------- 主入口
def money_series(lottery: str, tickets: Sequence[Dict[str, Any]],
                 records: Sequence[Any], *, rule_mode: str = "periodic",
                 target_issue: Any = None, tax: bool = False,
                 h: Optional[np.ndarray] = None, j: Optional[np.ndarray] = None,
                 cost_per_bet: float = COST_PER_BET,
                 multipliers: Optional[Sequence[int]] = None) -> Dict[str, Any]:
    """一组**固定**票集在真实开奖序列上的净收益序列（向量化）。

    `multipliers`（Stage 2 倍投，P7 的结算内核）：每张票下注几倍，长度 == 票数、
    取值 ≥ 1，默认全 1。**倍投只改金额与成本，不改命中结构** ——
    达标与否由 `replay` 的 `ok.any(axis=0)` 决定，与倍数无关（P7 的铁律断言点）。

    返回 dict（字段与方案 §3.3 的固定结构对齐）：
      `序列`（每期净额，长度恒 == 期数）、`每期实得`、`每期成本`、
      `净收益`、`总实得`、`成本`、`净收益每100元`、`净收益每100元_剔除最大一期`、
      `单次大奖占总实得比`、`最大回撤`、`最大回撤持续期数`、`中奖注数`、
      `中奖期数`、`≥5元期占比`、`≥10元期占比`、`逐票兜底期数`、`结算口径明细`。

    ★ 成本一律 `Σ 每期实际注数 × cost_per_bet`（倍投时实际注数 = Σ倍数）；
      **不用** `n*K*2` 的估算式。

    ★★★ `≥5元期占比` / `≥10元期占比` 的分母是**该期全部票的实得之和**（`gross`），
      **不是**「该期是否存在**一注**达到该金额」。两者是不同的量，且前者会随
      票数 K **机械上升**：

          双色球 K=50（100 元/期）实测 `≥10元期占比` = **1.0000**，
          而同一批票「至少一注 ≥10 元」的期占比只有 **0.3833**。

      原因很简单：50 注期里几乎不可能一期总实得低于 10 元。所以
      **这两个字段不能用来比较「覆盖选号有没有改善」，也不能跨 K 比较**。
      要「至少一注达到某门」的期占比必须用 `tier_metrics.gate_series`
      （逐票判定 `nhit > 0`）。本字段保留原义（每期总体回本率），
      供 `tier_backtest` / `lowcrowd_backtest` 等历史模块继续使用。
    """
    if rule_mode not in RULE_MODES:
        raise ValueError(f"rule_mode 必须是 {RULE_MODES} 之一，收到 {rule_mode!r}")
    from data import prize_amounts as PA

    recs = list(records)
    if not recs:
        raise ValueError("开奖记录为空")
    tks = list(tickets)
    if not tks:
        raise ValueError("票集为空")
    if target_issue is None:
        target_issue = recs[-1].期号

    n = len(recs)
    K = len(tks)
    if h is None or j is None:
        h, j = hit_matrices(lottery, tks, recs, target_issue)

    if multipliers is None:
        mult = np.ones(K, dtype=np.int64)
    else:
        mult = np.asarray(list(multipliers), dtype=np.int64)
        if mult.size != K:
            raise ValueError(f"倍数向量长度 {mult.size} != 票数 {K}")
        if int(mult.min()) < 1:
            raise ValueError("倍数必须 ≥ 1（倍投不会把某注的下注数降到 0）")

    # 预载：把 n 个期号的 prize 行一次性读进内存（否则每期每奖级开一次连接）
    PA.preload(lottery, [r.期号 for r in recs])

    pay = np.zeros((K, n), dtype=np.float64)
    fallback_periods: List[Any] = []
    note_tally: Dict[str, int] = {}
    for p, rec in enumerate(recs):
        rule_issue = target_issue if rule_mode == "current" else rec.期号
        grid, meta = amount_grid(lottery, rec.期号, rule_issue=rule_issue)
        for note in meta["口径"].values():
            note_tally[note] = note_tally.get(note, 0) + 1
        # ★ 只有当本期**真的命中**了金额未定的奖级时，才需要逐票兜底。
        #   早期期段的浮动奖普遍没有实测值，但绝大多数期根本没中一等/三等 ⇒
        #   按「该格是否被命中」判断，回退次数从 52% 的期降到 0~个位数。
        if meta["需逐票兜底"] and meta["待定格"][h[:, p], j[:, p]].any():
            fallback_periods.append(rec.期号)
            continue
        pay[:, p] = grid[h[:, p], j[:, p]]

    # 浮动奖无实测 ⇒ 金额依赖票面（payout_eff），只能逐票。正常数据里应为 0 期。
    if fallback_periods:
        from ev.coverage_backtest import grade_ticket
        cache: Dict[str, Any] = {}
        idx = {rec.期号: p for p, rec in enumerate(recs)}
        for iss in fallback_periods:
            p = idx[iss]
            ri = target_issue if rule_mode == "current" else iss
            for k, t in enumerate(tks):
                _tier, amt, _note = grade_ticket(lottery, t, recs[p], iss, cache,
                                                 rule_issue=ri)
                pay[k, p] = float(amt or 0.0)

    gross = (pay * mult[:, None]).sum(axis=0)     # 每期实得（税前，含倍数）
    nbet = np.full(n, int(mult.sum()), dtype=np.int64)
    cost = nbet.astype(np.float64) * float(cost_per_bet)
    net = gross - cost

    tax_amt = np.zeros(n, dtype=np.float64)
    if tax:
        from ev.profit.tax import tax_on
        tax_amt = np.array([tax_on(float(g)) for g in gross], dtype=np.float64)

    total_gross = float(gross.sum())
    total_cost = float(cost.sum())
    total_net = float(net.sum())

    # ---- 集中度守门（方案 §8.1）：剔除最大的那一期后还成不成立
    if n > 1:
        i_max = int(np.argmax(gross))
        net_ex = np.delete(net, i_max)
        cost_ex = float(cost.sum() - cost[i_max])
    else:
        i_max = 0
        net_ex = net
        cost_ex = total_cost
    net_ex_sum = float(net_ex.sum())

    from ev.tier_backtest import _max_drawdown
    mdd, mdd_len = _max_drawdown([float(x) for x in net])

    def per100(x: float, c: float) -> float:
        return (x / c * 100.0) if c > 0 else float("nan")

    return {
        "彩种": lottery,
        "规则口径": rule_mode,
        "期数": n,
        "注数每期": K,
        "总注数每期": int(mult.sum()),
        "倍数分布": {int(m): int((mult == m).sum()) for m in sorted({int(x) for x in mult})},
        "序列": net,
        "每期实得": gross,
        "每期成本": cost,
        "净收益": total_net,
        "总实得": total_gross,
        "成本": total_cost,
        "税后净收益": float((gross - tax_amt - cost).sum()),
        "税": float(tax_amt.sum()),
        "税口径": "同期合并计（>1万元按20%）" if tax else "税前（主口径）",
        "净收益每100元": per100(total_net, total_cost),
        "净收益每100元_剔除最大一期": per100(net_ex_sum, cost_ex),
        "单次大奖占总实得比": (float(gross.max()) / total_gross) if total_gross > 0 else 0.0,
        "最大回撤": mdd,
        "最大回撤持续期数": mdd_len,
        "中奖注数": int(((pay > 0) * mult[:, None]).sum()),
        "中奖期数": int((gross > 0).sum()),
        "≥5元期占比": float((gross >= 5).mean()),
        "≥10元期占比": float((gross >= 10).mean()),
        "每期实得最大": float(gross.max()),
        "每期实得最小": float(gross.min()),
        "逐票兜底期数": len(fallback_periods),
        "逐票兜底期号": [str(x) for x in fallback_periods[:20]],
        "结算口径明细": dict(sorted(note_tally.items(), key=lambda kv: -kv[1])),
        "剔除最大一期期号": str(recs[i_max].期号),
    }


__all__ = ["RULE_MODES", "grade_amounts", "amount_grid", "hit_matrices",
           "money_series"]
