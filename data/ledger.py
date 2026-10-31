# -*- coding: utf-8 -*-
"""逐期账本 —— 每期的成本、返奖、净盈亏、累计盈亏与最大回撤。

为什么另立文件，而不是塞进 `training/feedback/<彩种>_feedback_history.json`
--------------------------------------------------------------------------
两个理由，第二个是关键：

1. 逐注文件是「读全量 → 追加 → 回写」的巨文件（双色球已 2267 条且持续增长），
   每期再往里加聚合行会让它继续膨胀，而且聚合与明细混在一个数组里，读的人
   分不清哪条是「一注」哪条是「一期」。
2. **空期在逐注文件里无法表达**。逐注文件的每一行都对应一张真实的票，所以
   「这期开奖了但没出号」「这期停发」根本没有容身之处。而这两类期**必须进分母** ——
   只数「有号码的期」当分母，等价于把没中奖的期从样本里删掉，是所有彩票类
   统计里最常见、也最容易被自己骗过去的一种偏差。

   所以账本的第一条纪律：**每期都要有一行**，包括成本为 0 的空期。

口径（写死在字段名里，避免下游再解释一遍）
------------------------------------------
* `成本 = 实际注数 × 单价`，单价唯一真源 `config.ticket_price()`（缺配置回退 2.0，
  不回退 0 —— 回退 0 会让净盈亏等于实得，是最坏的一种「看起来正常」）。
* `税前奖金` 是**税前单注奖金之和**，不是实得、更不是净收益。
* `金额未知注数` > 0 时，**那些注不参与求和**：`prize_payout` 对浮动奖级拿不到
  当期派奖数据时返回 `金额=None`，当 0 计入会让合计系统性偏低、按上限计入会虚报。
  唯一正确做法是不参与求和 + 单独计数 + 界面披露。
* `净盈亏 = 实得 − 成本`；`实得 = 税前奖金 − 税`（税走 `ev/profit/tax.py`）。
* 最大回撤按「累计净盈亏」的峰值到谷值算，与 `ev/arena/payout.py::money_series` 同口径。

★ 本模块**不产生任何投注行为**，只读取已发生的记录并记账。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Sequence

from config import BASE_DIR

logger = logging.getLogger(__name__)

LEDGER_DIR = BASE_DIR / "training" / "ledger"

# 一行账本的字段（顺序即落盘顺序，便于人工 diff）
ROW_FIELDS = (
    "期号", "开奖日期", "参与", "参与原因", "批次号", "出号时刻",
    "请求注数", "实际注数", "补齐", "单价", "倍数合计",
    "成本", "税前奖金", "税", "实得", "净盈亏",
    "累计净盈亏", "峰值", "回撤", "最大回撤", "最大回撤持续期数",
    "中奖注数", "中奖期", "金额未知注数", "口径计数",
    "中奖注数_ge10", "中奖期_ge10", "ge10_口径",
    "规则版本", "预测版本", "写入时间",
)


def ledger_path(lottery: str, *, base: Optional[Any] = None) -> Any:
    d = base if base is not None else LEDGER_DIR
    return d / f"{lottery}_ledger.jsonl"


def price_of(lottery: str) -> float:
    from config import ticket_price
    return float(ticket_price(lottery))


# ------------------------------------------------------------------ 构造
def build_rows(lottery: str, *, records: Optional[Sequence[dict]] = None) -> List[dict]:
    """从逐注历史构造「有出号」的期行。**不做空期补全**（见 `fill_empty`）。"""
    if records is None:
        from data.feedback import load_feedback_history
        records = load_feedback_history(lottery)

    groups: Dict[tuple, List[dict]] = {}
    for r in records or []:
        iss = r.get("期号")
        if iss is None:
            continue
        key = (str(iss), str(r.get("批次号") or ""))
        groups.setdefault(key, []).append(r)

    price = price_of(lottery)
    rows: List[dict] = []
    for (iss, batch), g in groups.items():
        head = g[0]
        n = len(g)
        declared = head.get("批次规模")
        try:
            n_declared = int(declared) if declared is not None else n
        except (TypeError, ValueError):
            n_declared = n

        unknown = 0
        gross = 0.0
        hits = 0
        hit10 = 0
        grades: Dict[str, int] = {}
        for r in g:
            lvl = r.get("中奖等级")
            if lvl:
                grades[str(lvl)] = grades.get(str(lvl), 0) + 1
                if str(lvl) not in ("未中", "未中奖", ""):
                    hits += 1
            if r.get("金额未知"):
                unknown += 1
                continue
            v = r.get("税前奖金")
            if v is None:
                unknown += 1
                continue
            try:
                amount = float(v)
            except (TypeError, ValueError):
                unknown += 1
                continue
            gross += amount
            # ★ 逐票口径：单注税前奖金 ≥10 元。**不是**「该期全部票之和 ≥10 元」——
            #   后者随注数机械上升（K 越大越必然超过 10 元），拿来比 K 档是错的。
            if amount >= 10.0:
                hit10 += 1

        rows.append({
            "期号": iss,
            "开奖日期": head.get("开奖日期"),
            "参与": True,
            "参与原因": "已出号",
            "批次号": batch or None,
            "出号时刻": head.get("批次时间"),
            "请求注数": head.get("请求注数", n_declared),
            "实际注数": n,
            "补齐": head.get("补齐"),
            "单价": price,
            "倍数合计": n,          # 全单倍时倍数合计 == 注数；有倍投记录后由记录给出
            "成本": round(price * n, 2),
            "税前奖金": round(gross, 2),
            "中奖注数": hits,
            "中奖期": hits > 0,
            "金额未知注数": unknown,
            "口径计数": grades,
            "中奖注数_ge10": hit10,
            "中奖期_ge10": hit10 > 0,
            "ge10_口径": "逐票：该期至少一注税前奖金≥10元",
            "规则版本": head.get("规则版本"),
            "预测版本": head.get("预测版本"),
        })
    rows.sort(key=lambda r: _num(r.get("期号")))
    return rows


def fill_empty(rows: List[dict], lottery: str, *,
               issues: Optional[Sequence[Any]] = None) -> List[dict]:
    """把「已开奖但账本里没有」的期补成 `参与=False` 的空行。

    ★ 这是本模块存在的核心理由：分母必须包含没出号的期。空行的成本为 0、
      实得为 0、净盈亏为 0，但**它在样本里**，所以「中奖期占比」不会被抬高。
    """
    if issues is None:
        from data.loader import load_lottery
        issues = [getattr(r, "期号", None) for r in load_lottery(lottery).records]
    have = {str(r.get("期号")) for r in rows}
    out = list(rows)
    for iss in issues:
        if iss is None or str(iss) in have:
            continue
        out.append({
            "期号": iss, "开奖日期": None, "参与": False,
            "参与原因": "未出号（该期无预测批次）",
            "批次号": None, "出号时刻": None,
            "请求注数": 0, "实际注数": 0, "补齐": None,
            "单价": price_of(lottery), "倍数合计": 0,
            "成本": 0.0, "税前奖金": 0.0, "税": 0.0, "实得": 0.0, "净盈亏": 0.0,
            "中奖注数": 0, "中奖期": False, "金额未知注数": 0, "口径计数": {},
            "中奖注数_ge10": 0, "中奖期_ge10": False,
            "ge10_口径": "逐票：该期至少一注税前奖金≥10元",
            "规则版本": None, "预测版本": None,
        })
    out.sort(key=lambda r: _num(r.get("期号")))
    return out


def recompute(rows: List[dict]) -> List[dict]:
    """就地重算税、净盈亏、累计、峰值与回撤（**幂等**，可反复跑）。"""
    from ev.profit.tax import BASIS_ISSUE, tax_on

    cum = 0.0
    peak = 0.0
    max_dd = 0.0
    dd_len = 0
    cur_len = 0
    for r in rows:
        gross = float(r.get("税前奖金") or 0.0)
        cost = float(r.get("成本") or 0.0)
        tax = float(tax_on(gross, BASIS_ISSUE)) if gross > 0 else 0.0
        net = gross - tax - cost
        r["税前奖金"] = round(gross, 2)
        r["成本"] = round(cost, 2)
        r["税"] = round(tax, 2)
        r["实得"] = round(gross - tax, 2)
        r["净盈亏"] = round(net, 2)

        cum += net
        r["累计净盈亏"] = round(cum, 2)
        peak = max(peak, cum)
        r["峰值"] = round(peak, 2)
        dd = peak - cum
        r["回撤"] = round(dd, 2)
        if dd > 1e-9:
            cur_len += 1
            if dd > max_dd + 1e-9:
                max_dd = dd
                dd_len = cur_len
        else:
            cur_len = 0
        r["最大回撤"] = round(max_dd, 2)
        r["最大回撤持续期数"] = dd_len
    return rows


def rebuild(lottery: str, *, periods: Optional[int] = None,
            records: Optional[Sequence[dict]] = None) -> dict:
    """重建某彩种账本（构造 → 补空期 → 重算 → 落盘）。返回摘要。"""
    rows = build_rows(lottery, records=records)
    rows = fill_empty(rows, lottery)
    if periods:
        rows = rows[-int(periods):]
    rows = recompute(rows)
    _write(lottery, rows)
    return summary_of(rows, lottery=lottery)


# ------------------------------------------------------------------ 落盘 / 读取
def _write(lottery: str, rows: Sequence[dict]) -> None:
    p = ledger_path(lottery)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(_ordered(r), ensure_ascii=False) + "\n")
        f.flush()
    import os
    os.replace(tmp, p)


def _ordered(r: dict) -> dict:
    out = {k: r.get(k) for k in ROW_FIELDS}
    for k, v in r.items():          # 保留额外字段（向前兼容）
        out.setdefault(k, v)
    return out


def load(lottery: str) -> List[dict]:
    p = ledger_path(lottery)
    if not p.exists():
        return []
    rows = []
    with p.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


# ------------------------------------------------------------------ 汇总
def summary_of(rows: Sequence[dict], *, lottery: str = "") -> dict:
    """账本汇总。**分母三数必须并列给出**，这是防止「只报中奖期」的硬约束。"""
    n = len(rows)
    participated = sum(1 for r in rows if r.get("参与"))
    cost = sum(float(r.get("成本") or 0.0) for r in rows)
    gross = sum(float(r.get("税前奖金") or 0.0) for r in rows)
    tax = sum(float(r.get("税") or 0.0) for r in rows)
    net = sum(float(r.get("净盈亏") or 0.0) for r in rows)
    hits = sum(int(r.get("中奖注数") or 0) for r in rows)
    hit_periods = sum(1 for r in rows if r.get("中奖期"))
    hit10 = sum(int(r.get("中奖注数_ge10") or 0) for r in rows)
    hit10_periods = sum(1 for r in rows if r.get("中奖期_ge10"))
    unknown = sum(int(r.get("金额未知注数") or 0) for r in rows)
    max_dd = max((float(r.get("最大回撤") or 0.0) for r in rows), default=0.0)

    return {
        "彩种": lottery,
        "开奖期数": n,                    # 窗口内真实开奖期数（含未出号期）
        "参与期数": participated,
        "未参与期数": n - participated,
        "成本": round(cost, 2),
        "税前奖金": round(gross, 2),
        "税": round(tax, 2),
        "实得": round(gross - tax, 2),
        "净盈亏": round(net, 2),
        "净收益率": (net / cost) if cost > 0 else None,
        "中奖注数": hits,
        "中奖期数": hit_periods,
        "中奖期占比": (hit_periods / participated) if participated else None,
        "金额未知注数": unknown,
        "最大回撤": round(max_dd, 2),
        # ★ 次级指标：保留但不得当主指标
        "中奖注数_ge10": hit10,
        "中奖期数_ge10": hit10_periods,
        "中奖期占比_ge10": (hit10_periods / participated) if participated else None,
        "备注": {
            "净收益率": "净盈亏 ÷ 成本，是决策指标；负数=亏损",
            "中奖期占比": "分母是**参与期数**，不是全部开奖期数，也不是中奖注数",
            "金额未知": f"{unknown} 注的奖金未能取到，已排除在求和之外（不是按 0 计）",
            "中奖期数_ge10": "**次级指标，不作成功标准**：逐票口径（该期至少一注税前≥10元）；"
                            "它随注数 K 机械上升，不可跨 K 比较；花 800 中 20 元仍是亏损，"
                            "判断只能看净收益率与最大回撤",
        },
    }


def summary(lottery: str, *, periods: Optional[int] = None) -> dict:
    rows = load(lottery)
    if not rows:
        rows = recompute(fill_empty(build_rows(lottery), lottery))
    if periods:
        rows = rows[-int(periods):]
    return summary_of(rows, lottery=lottery)


def _num(x: Any) -> float:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return 0.0
