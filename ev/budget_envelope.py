# -*- coding: utf-8 -*-
"""ev/budget_envelope.py —— 娱乐预算包络：把「少亏」做成可执行的纪律。

为什么要单独一个模块
--------------------
`ev/bankroll.py` 管的是「本金 × 2% + 止损线 + 连亏熔断」，`ev/stake.py` 把
EV≤0 落成 0 注。两者都没有回答用户在**还没出现正净 EV** 的阶段真正需要的三件事：

  ① 固定每月娱乐预算 + 固定单期上限，**亏完即停**（不跨月预支、不追加）；
  ② 拒绝任何「因为前面亏了/这期更看好」的加注请求（追损与加预算）；
  ③ 允许并**默认**输出「不参与」。

三条都不是 EV 计算，而是纪律。纪律必须可执行（有额度、有账本、有拒绝），
否则「我会控制预算」只是句话。

★ 单向闸门（安全属性）
  `gate_verdict()` 只允许把「参与」**降级**成「不参与」，
  **永远不允许**把「不参与」升级成「参与」。
  这条不对称是刻意的：漏判一次参与只是少亏，误判一次参与是真亏钱。

★ 本模块**不改变任何一注的中奖概率**，只封顶投入。
  按恒等式 `E[中奖注数] = K·p`，加注不改变单注期望，只放大方差——
  所以「加注」在这里永远不是改进，只是加速。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

_BASE_DIR = Path(__file__).resolve().parent.parent

# 运行时账本（「本月已投」）。放 config/ 是刻意：.gitignore 的 config/*.json
# 规则会把它排除，账本属于本机运行状态，不该进仓库、更不该被推送。
LEDGER_PATH = _BASE_DIR / "config" / "budget_ledger.json"

VERDICT_JOIN = "参与"
VERDICT_SKIP = "不参与"
VERDICT_STOP = "停（本月额度耗尽）"

# 数字型（每日开奖）与乐透型（每周三期）的期数不同，月度定额必须分开折算
PERIODS_PER_MONTH = {
    "双色球": 13, "大乐透": 13, "七星彩": 13,
    "排列3": 30, "福彩3D": 30, "排列5": 30,
}

HONEST_NOTE = (
    "本包络只**封顶投入**，不改变任何一注的中奖概率，也不改变长期期望。"
    "彩票资金按对应游戏规则分为奖金、发行费与公益金，只有奖金部分构成返奖来源，"
    "故长期返奖率显著低于 1；「经常中小奖」说明的是同一笔奖金被切成更多小份，"
    "不等于长期回本。在正净 EV 被样本外验证之前，默认判决是「不参与」。"
    "口径：单注奖金超 1 万元部分按 20% 缴税（见 ev/profit/tax）。"
)


# ------------------------------------------------------------------ 包络
@dataclass(frozen=True)
class Envelope:
    """一期一彩种的预算包络。`per_period_cap` 是单期封顶（元），不是建议投入。"""

    monthly_budget: float
    per_period_cap: float
    cost_per_ticket: float = 2.0
    baseline_tickets: int = 0          # 现行生产注数；任何「加注」都被拒
    lottery: str = ""
    标签: str = ""

    def __post_init__(self) -> None:
        if self.monthly_budget < 0:
            raise ValueError(f"月度定额不能为负：{self.monthly_budget}")
        if self.per_period_cap < 0:
            raise ValueError(f"单期上限不能为负：{self.per_period_cap}")
        if self.cost_per_ticket <= 0:
            raise ValueError(f"单注成本必须为正：{self.cost_per_ticket}")
        if self.per_period_cap > self.monthly_budget + 1e-9:
            raise ValueError(
                f"单期上限 {self.per_period_cap} 大于月度定额 {self.monthly_budget}："
                f"一期就能吃掉整月额度，包络失去意义")

    @property
    def cap_tickets(self) -> int:
        """单期上限折算成注数（向下取整：买不起一注就是 0 注）。"""
        return int(self.per_period_cap // self.cost_per_ticket)


def periods_per_month(lottery: str) -> int:
    """每月开奖期数（乐透型每周 3 期 ⇒ 13 期/月；数字型日开 ⇒ 30 期/月）。"""
    return int(PERIODS_PER_MONTH.get(lottery, 13))


def default_envelope(lottery: str, *, cost_per_ticket: float = 2.0) -> Envelope:
    """按**现行生产注数**折算的默认包络。

    ★ 这是「当前配置的封顶值」，不是建议投入。生产注数从
      `data.ticket_size.effective_count` 取（单一真源），不在这里抄一份。
      读不到配置时退回 1 注（宁可过小，不可过大）。
    """
    try:
        from data import ticket_size as TS
        n = int(TS.effective_count(lottery))
    except Exception:                                        # noqa: BLE001
        n = 1
    n = max(1, n)
    per_period = n * float(cost_per_ticket)
    return Envelope(monthly_budget=per_period * periods_per_month(lottery),
                    per_period_cap=per_period,
                    cost_per_ticket=float(cost_per_ticket),
                    baseline_tickets=n,
                    lottery=lottery,
                    标签="按现行生产注数折算（封顶值，非建议投入）")


def default_envelopes() -> Dict[str, Envelope]:
    """全部彩种的默认包络（结论文档的「当前配置合计」用它算，不手抄）。"""
    return {lot: default_envelope(lot) for lot in PERIODS_PER_MONTH}


# ------------------------------------------------------------------ 折算
def allocate(env: Envelope, spent_this_month: float = 0.0,
             want_tickets: Optional[int] = None) -> Dict[str, Any]:
    """把「剩余额度 + 单期上限 + 想买的注数」折成一个可执行注数。

    ★ 不做「取偶」：数字型生产注数本来就是奇数（3D 5 注、排5 5 注），
      强行取偶会凭空删掉一注，等于偷偷改了预算口径。
    """
    spent = max(0.0, float(spent_this_month))
    remaining = max(0.0, env.monthly_budget - spent)
    by_money = int(remaining // env.cost_per_ticket)
    by_cap = env.cap_tickets
    want = env.baseline_tickets if want_tickets is None else max(0, int(want_tickets))
    n = min(by_money, by_cap, want) if want else 0

    reasons: List[str] = []
    if by_money == 0:
        reasons.append(f"本月额度已耗尽（已投 {spent:.0f} / 定额 {env.monthly_budget:.0f} 元）")
    elif n == by_money:
        reasons.append(f"受剩余额度约束（剩 {remaining:.0f} 元）")
    if n == by_cap and by_cap < min(by_money, want):
        reasons.append(f"受单期上限约束（{env.per_period_cap:.0f} 元 = {by_cap} 注）")
    if want_tickets is not None and want_tickets > env.baseline_tickets:
        reasons.append(f"「想买 {want_tickets} 注」超过基线 {env.baseline_tickets} 注 ⇒ 按基线执行")
    if not reasons:
        reasons.append("在额度与单期上限内")

    return {
        "注数": int(n),
        "单价元": env.cost_per_ticket,
        "本期成本元": round(n * env.cost_per_ticket, 2),
        "剩余额度": round(remaining, 2),
        "额度注数": by_money,
        "单期上限注数": by_cap,
        "基线注数": env.baseline_tickets,
        "原因": reasons,
        "诚实边界": HONEST_NOTE,
    }


def chase_guard(env: Envelope, want_tickets: int, *,
                recent_pnl: float = 0.0) -> Dict[str, Any]:
    """拒绝「追损」与「因为看好而加注」。

    规则（不区分理由，一律拒）：任何超过基线注数的请求都被压回基线。
    理由不是保守，而是算术：加注不改变单注中奖概率，`E[中奖注数]=K·p` 线性放大，
    `E[净回款]` 同样线性放大——投得多只是把同一个负 EV 做大。
    """
    want = max(0, int(want_tickets))
    base = env.baseline_tickets
    refused = want > base
    notes: List[str] = []
    if refused:
        if recent_pnl < 0:
            notes.append(f"前期累计盈亏 {recent_pnl:+.2f} 元为负 ⇒ 拒绝追损加注")
        notes.append(f"请求 {want} 注 > 基线 {base} 注 ⇒ 压回 {base} 注")
        notes.append("加注不改变单注概率，只把同一个期望放大（E[中奖注数]=K·p）")
    else:
        notes.append("未请求加注")
    return {
        "允许注数": min(want, base),
        "是否拒绝加注": refused,
        "说明": notes,
        "诚实边界": HONEST_NOTE,
    }


# ------------------------------------------------------------------ 判决
def decide(env: Envelope, spent_this_month: float = 0.0,
           net_ev_per_ticket: Optional[float] = None,
           want_tickets: Optional[int] = None) -> Dict[str, Any]:
    """三级判决：净 EV≤0（或不可得）⇒ 不参与；额度耗尽 ⇒ 停；否则参与。

    ★ 净 EV **不可得**时按「不参与」处理 —— 默认值必须落在不亏钱的一侧。
    """
    spent = max(0.0, float(spent_this_month))
    alloc = allocate(env, spent, want_tickets)

    if net_ev_per_ticket is None:
        return {"判决": VERDICT_SKIP, "注数": 0, "原因": "净 EV 不可得 ⇒ 按默认不参与",
                "包络": alloc, "诚实边界": HONEST_NOTE}
    if float(net_ev_per_ticket) <= 0:
        return {"判决": VERDICT_SKIP, "注数": 0,
                "原因": f"净 EV = {float(net_ev_per_ticket):+.4f} 元/注 ≤ 0 ⇒ 0 注",
                "包络": alloc, "诚实边界": HONEST_NOTE}
    if alloc["注数"] == 0:
        return {"判决": VERDICT_STOP, "注数": 0,
                "原因": "；".join(alloc["原因"]), "包络": alloc, "诚实边界": HONEST_NOTE}
    return {"判决": VERDICT_JOIN, "注数": int(alloc["注数"]),
            "原因": "；".join(alloc["原因"]), "包络": alloc, "诚实边界": HONEST_NOTE}


def _upstream_participates(verdict: Any) -> bool:
    """上游判决是否表示「参与」。宁可判成不参与（见模块头的单向性说明）。"""
    if verdict is None:
        return False
    s = str(verdict).strip()
    if not s:
        return False
    if ("不建议" in s) or ("不参与" in s) or ("不出结论" in s) or s.startswith("停"):
        return False
    return "参与" in s


def gate_verdict(upstream: Any, env: Envelope, spent_this_month: float = 0.0,
                 *, net_ev_per_ticket: Optional[float] = None,
                 want_tickets: Optional[int] = None) -> Dict[str, Any]:
    """单向闸门：把上游判决与本包络**取更保守者**。

    · 上游已判「不参与 / 不出结论」⇒ 原样返回，注数 0，**不升级**；
    · 上游判「参与」⇒ 再过包络，只可能被降级，不可能被放宽。

    ★ 这是**预算闸门**，不是 EV 闸门：
      `net_ev_per_ticket` 为 None 时**不重判 EV**（EV 归上游），只查额度与单期上限。
      刻意如此——上游「参与」的净 EV 是按 κ* 交点算的（≈0），拿它来过闸
      会把每一条「参与」都无意义地降级，那不是保守，那是把闸门变成橡皮章。
      只有调用方**显式**给出净 EV 时，才叠加 EV≤0 ⇒ 不参与。
    """
    upstream_s = "不出结论" if upstream is None else str(upstream)
    if not _upstream_participates(upstream_s):
        return {"最终判决": upstream_s, "上游判决": upstream_s, "注数": 0,
                "是否降级": False, "原因": "上游已判不参与/不出结论；闸门只降不升，不做任何升级",
                "诚实边界": HONEST_NOTE}

    alloc = allocate(env, spent_this_month, want_tickets)
    if net_ev_per_ticket is not None and float(net_ev_per_ticket) <= 0:
        return {"最终判决": VERDICT_SKIP, "上游判决": upstream_s, "注数": 0,
                "是否降级": True,
                "原因": f"包络闸门降级：净 EV = {float(net_ev_per_ticket):+.4f} 元/注 ≤ 0 ⇒ 0 注",
                "包络": alloc, "诚实边界": HONEST_NOTE}
    if alloc["注数"] == 0:
        return {"最终判决": VERDICT_STOP, "上游判决": upstream_s, "注数": 0,
                "是否降级": True,
                "原因": "包络闸门降级：" + "；".join(alloc["原因"]),
                "包络": alloc, "诚实边界": HONEST_NOTE}
    # 通过：保留上游措辞（如「条件性可参与」），只补注数与包络
    return {"最终判决": upstream_s, "上游判决": upstream_s, "注数": int(alloc["注数"]),
            "是否降级": False, "原因": "；".join(alloc["原因"]),
            "包络": alloc, "诚实边界": HONEST_NOTE}


def monthly_report(env: Envelope, *, periods_per_month_n: Optional[int] = None,
                   return_rate: float = 0.5245) -> Dict[str, Any]:
    """一个月的固定投入会变成什么（用来把「少亏」量化成数字）。

    `return_rate` 默认取双色球税后返奖率量级；不同彩种不同，**不要跨彩种套用**。
    """
    ppm = int(periods_per_month_n or periods_per_month(env.lottery))
    spendable_periods = int(env.monthly_budget // env.per_period_cap) if env.per_period_cap > 0 else 0
    periods = min(ppm, spendable_periods)
    spend = periods * env.per_period_cap
    back = spend * float(return_rate)
    return {
        "彩种": env.lottery, "月度定额元": env.monthly_budget,
        "单期上限元": env.per_period_cap, "每月可执行期数": periods,
        "月计划投入元": round(spend, 2),
        "期望回款元": round(back, 2),
        "期望净亏元": round(back - spend, 2),
        "返奖率口径": float(return_rate),
        "诚实边界": HONEST_NOTE,
    }


# ------------------------------------------------------------------ 账本
def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    """原子写（tmp + fsync + os.replace）。★ 绝不先截断原文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=1)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def _month_key(now: Optional[datetime] = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m")


class Ledger:
    """月度投入账本。`add()` 是唯一写入口，跨月自动归零。

    ★ `path` 必须在调用时读模块级 `LEDGER_PATH`（不能用默认参数绑定），
      否则 conftest 的重定向对这个类不生效，测试会写真实账本。
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path is not None else Path(LEDGER_PATH)

    def read(self, now: Optional[datetime] = None) -> Dict[str, Any]:
        key = _month_key(now)
        if not self.path.exists():
            return {"月份": key, "已投": 0.0, "记录": []}
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:                                    # noqa: BLE001
            # 坏文件不改写、不静默当 0 —— 直接把异常暴露给调用方
            raise
        if str(doc.get("月份")) != key:                       # 跨月：账面归零（不改历史）
            return {"月份": key, "已投": 0.0, "记录": [], "_上月": {
                "月份": doc.get("月份"), "已投": doc.get("已投")}}
        return {"月份": key, "已投": float(doc.get("已投", 0.0)),
                "记录": list(doc.get("记录", []))}

    def spent(self, now: Optional[datetime] = None) -> float:
        return float(self.read(now)["已投"])

    def add(self, amount: float, *, now: Optional[datetime] = None,
            note: str = "") -> Dict[str, Any]:
        cur = self.read(now)
        cur["已投"] = round(float(cur["已投"]) + float(amount), 2)
        cur["记录"].append({"时间": (now or datetime.now()).isoformat(timespec="seconds"),
                            "金额": round(float(amount), 2), "备注": note})
        cur.pop("_上月", None)
        _atomic_write_json(self.path, cur)
        return cur

    def reset(self, *, now: Optional[datetime] = None) -> Dict[str, Any]:
        doc = {"月份": _month_key(now), "已投": 0.0, "记录": []}
        _atomic_write_json(self.path, doc)
        return doc


__all__ = [
    "Envelope", "periods_per_month", "default_envelope", "default_envelopes",
    "allocate", "chase_guard", "decide", "gate_verdict", "monthly_report",
    "Ledger", "HONEST_NOTE", "PERIODS_PER_MONTH", "LEDGER_PATH",
    "VERDICT_JOIN", "VERDICT_SKIP", "VERDICT_STOP",
]
