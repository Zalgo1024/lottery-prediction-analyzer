"""
ev/attribution.py —— 中奖归因（哪一批、哪个策略、哪个档位在出奖）

## 回答的问题

用户问：「每次中奖的都是那一批次里面算出来的吗？」

答案在数据链路里是确定的：预测生成时立即写入 pending（**预先锁定**，带
目标期号 / 预测日期 / 来源 / 策略 / 档位），开奖后 `evaluate_pending_predictions`
只拿当时存下的号码对比开奖结果，**不会重新生成号码**。所以每条反馈记录都能溯源到
"哪一批、哪个策略、哪个档位"。本模块只是把这个溯源信息聚合出来。

## 聚合口径

- 键 = `(来源, 策略, 档位)`；`来源` ∈ {predict, train}，`档位` 由 `feedback._tier_of`
  统一（缺失/非法一律归「一般」，旧记录向后兼容）；
- 奖级字段名**双轨**：乐透型写 `中奖等级`（一等…六等），数字型写 `中奖玩法`
  （直选/组选3/组选6）。⚠️ 历史上只探测其一会把双色球/大乐透算成 0 中，这里两者都认；
- 奖金走 `data.prize_table.prize_payout(...)["金额"]`（唯一权威口径）；浮动奖级在
  无当期数据时返回 `None` → 计入 `奖金未知注数`，不硬造数字。

## 诚实边界

归因只是**描述性统计**，不构成"某个策略更会选号"的证据：命中率差异未做多重比较
校正，样本量也远不足以检验三等以上的差异（见 MEMORY.md 样本量死结）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from config import LOTTERY_CONFIG
from data import feedback as fb
from data.prize_table import prize_payout

logger = logging.getLogger(__name__)

WIN_GRADE_KEYS = ("中奖玩法", "中奖等级")
_NULL_GRADES = {"", "未中", "none", "null", "0", "-", "无", "—"}

DEFAULT_LOOKBACK = 2000

HONEST_NOTE = (
    "归因只统计「开奖后按预先锁定的 pending 号码」结算的历史记录，不改变任何预测逻辑；"
    "分组命中率差异不构成「某策略更会选号」的证据（未做多重比较校正，样本量不足）"
)


# ------------------------------------------------------------
# 字段解析
# ------------------------------------------------------------

def win_grade(rec: Dict[str, Any]) -> Optional[str]:
    """取中奖奖级；未中返回 None。同时兼容 `中奖玩法`（数字型）与 `中奖等级`（乐透型）。"""
    if not isinstance(rec, dict):
        return None
    for k in WIN_GRADE_KEYS:
        v = rec.get(k)
        if v is None:
            continue
        s = str(v).strip()
        if s and s.lower() not in _NULL_GRADES:
            return s
    return None


def is_win(rec: Dict[str, Any]) -> bool:
    return win_grade(rec) is not None


def nominal_prize(lottery: str, rec: Dict[str, Any]) -> Tuple[float, bool]:
    """单注名义奖金 `(金额, 是否未知)`。浮动奖级无当期数据 → (0.0, True)。"""
    grade = win_grade(rec)
    if not grade:
        return 0.0, False
    try:
        info = prize_payout(lottery, grade, rec.get("期号") or rec.get("目标期号"))
    except Exception as e:  # pragma: no cover - 防御
        logger.warning(f"prize_payout 失败 {lottery}/{grade}: {e}")
        return 0.0, True
    amt = info.get("金额")
    if amt is None:
        return 0.0, True
    return float(amt), False


# ------------------------------------------------------------
# 聚合
# ------------------------------------------------------------

def _aggregate(rows: List[Dict[str, Any]], lottery: str) -> Dict[str, Any]:
    n = len(rows)
    wins = [r for r in rows if is_win(r)]
    levels: Dict[str, int] = {}
    prize_sum = 0.0
    unknown = 0
    for r in wins:
        g = win_grade(r)
        levels[g] = levels.get(g, 0) + 1
        amt, unk = nominal_prize(lottery, r)
        prize_sum += amt
        if unk:
            unknown += 1
    return {
        "注数": n,
        "中奖注数": len(wins),
        "命中率": round(len(wins) / n, 4) if n else 0.0,
        "奖级分布": levels,
        "名义奖金合计": round(prize_sum, 2),
        "奖金未知注数": unknown,
    }


def _as_int_issue(v: Any) -> int:
    try:
        return int(str(v))
    except (TypeError, ValueError):
        return -1


def latest_batch_coverage(lottery: str) -> Dict[str, Any]:
    """最新一期 pending 批次的撞号覆盖 KPI（名义注数 vs 剪枝后等效注数）。"""
    try:
        from ev.coverage import prune_predicted_sets

        pend = [p for p in fb.load_pending(lottery) if p.get("状态") == "pending"]
        if not pend:
            return {}
        latest = max(pend, key=lambda p: _as_int_issue(p.get("目标期号")))
        tickets = latest.get("预测号码") or []
        if not tickets:
            return {}
        _kept, kpi = prune_predicted_sets(tickets, lottery, nominal_n=len(tickets),
                                         keep_per_strategy=1, enabled=True)
        kpi["目标期号"] = latest.get("目标期号")
        kpi["预测日期"] = latest.get("预测日期")
        return kpi
    except Exception as e:  # pragma: no cover - 防御
        logger.warning(f"最新批次覆盖 KPI 失败 {lottery}: {e}")
        return {"错误": str(e)}


def attribution_for_lottery(lottery: str, lookback: Optional[int] = DEFAULT_LOOKBACK) -> Dict[str, Any]:
    """单彩种归因：按 (来源, 策略, 档位) 分组。"""
    try:
        hist = fb.load_feedback_history(lottery, lookback)
    except Exception as e:  # pragma: no cover - 防御
        return {"彩种": lottery, "error": str(e), "分组": []}
    hist = [r for r in hist if isinstance(r, dict)]

    groups: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = {}
    for r in hist:
        key = (str(r.get("来源") or "predict"),
               str(r.get("策略") or "未知"),
               fb._tier_of(r))
        groups.setdefault(key, []).append(r)

    rows: List[Dict[str, Any]] = []
    tn = tw = tu = 0
    tp = 0.0
    for (src, strat, tier), recs in groups.items():
        agg = _aggregate(recs, lottery)
        rows.append({"来源": src, "策略": strat, "档位": tier, **agg})
        tn += agg["注数"]
        tw += agg["中奖注数"]
        tp += agg["名义奖金合计"]
        tu += agg["奖金未知注数"]
    rows.sort(key=lambda x: (-x["注数"], x["来源"], x["策略"], x["档位"]))

    return {
        "彩种": lottery,
        "总注数": tn,
        "总中奖注数": tw,
        "总命中率": round(tw / tn, 4) if tn else 0.0,
        "总名义奖金": round(tp, 2),
        "奖金未知注数": tu,
        "分组": rows,
        "当前批次覆盖": latest_batch_coverage(lottery),
        "回看上限": lookback,
        "诚实声明": HONEST_NOTE,
    }


def attribution_global(lookback: Optional[int] = DEFAULT_LOOKBACK) -> Dict[str, Any]:
    """六彩种汇总：全局 (来源,策略,档位) 合并表 + 各彩种总计。"""
    merged: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    per: Dict[str, Dict[str, Any]] = {}
    tn = tw = 0
    tp = 0.0

    for lot in LOTTERY_CONFIG:
        a = attribution_for_lottery(lot, lookback)
        per[lot] = {k: a.get(k) for k in ("总注数", "总中奖注数", "总命中率", "总名义奖金", "奖金未知注数")}
        tn += a.get("总注数", 0)
        tw += a.get("总中奖注数", 0)
        tp += a.get("总名义奖金", 0.0)
        for grp in a.get("分组", []):
            key = (grp["来源"], grp["策略"], grp["档位"])
            m = merged.setdefault(key, {"注数": 0, "中奖注数": 0, "名义奖金合计": 0.0,
                                        "奖级分布": {}, "奖金未知注数": 0, "彩种注数": {}})
            m["注数"] += grp["注数"]
            m["中奖注数"] += grp["中奖注数"]
            m["名义奖金合计"] += grp["名义奖金合计"]
            m["奖金未知注数"] += grp["奖金未知注数"]
            m["彩种注数"][lot] = m["彩种注数"].get(lot, 0) + grp["注数"]
            for lv, c in (grp.get("奖级分布") or {}).items():
                m["奖级分布"][lv] = m["奖级分布"].get(lv, 0) + c

    gsum = []
    for (src, strat, tier), m in merged.items():
        n = m["注数"]
        gsum.append({
            "来源": src, "策略": strat, "档位": tier,
            "注数": n, "中奖注数": m["中奖注数"],
            "命中率": round(m["中奖注数"] / n, 4) if n else 0.0,
            "奖级分布": m["奖级分布"],
            "名义奖金合计": round(m["名义奖金合计"], 2),
            "奖金未知注数": m["奖金未知注数"],
            # 该合并行内各彩种的注数构成（按注数降序），回答"这一行到底是哪些彩种"
            "彩种注数": dict(sorted(m["彩种注数"].items(), key=lambda kv: -kv[1])),
        })
    gsum.sort(key=lambda x: (-x["注数"], x["来源"], x["策略"], x["档位"]))

    return {
        "全局汇总": gsum,
        "各彩种": per,
        "总注数": tn,
        "总中奖注数": tw,
        "总命中率": round(tw / tn, 4) if tn else 0.0,
        "总名义奖金": round(tp, 2),
        "回看条数": lookback,
        "诚实声明": HONEST_NOTE,
    }


if __name__ == "__main__":  # pragma: no cover - 手工速查
    import json
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    lot = sys.argv[1] if len(sys.argv) > 1 else None
    data = attribution_for_lottery(lot) if lot else attribution_global()
    print(json.dumps(data, ensure_ascii=False, indent=1))
