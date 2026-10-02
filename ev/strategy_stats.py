"""
ev/strategy_stats.py —— 策略的历史实测命中统计（带随机基线对照）

为什么要有这个模块：
    前端展示"某策略历史上实际命中了几个号"，必须同时给出**随机基线**
    （瞎猜能达到的水平）与**样本量 n**，否则「策略 A 命中 1.2 / 策略 B 命中 0.9」
    会被读成"A 更会选号"，而这个差值在几十~几百注的样本下纯属噪声。

口径（诚实边界，勿改）：
  - 单位 = 注；只统计 `valid_prediction=True` 的**真预测**（排除马后炮/训练/非法票，
    与 data.feedback.get_feedback_summary 同口径）。
  - 策略名经 `normalize_strategy_name` 归一（ML(logistic)/统计训练(W50) → ML策略）。
  - 实测 = 历史记录 `总命中` 的均值；基线 = `compute_random_baseline` 的超几何期望
    （双色球 1.1534 / 大乐透 1.0476 / 排列5 0.5 / 福彩3D·排列3 0.3 / 七星彩 0.6667）。
  - ★**不做任何显著性检验**：同一期的多注共享同一开奖号、彼此不独立，
    把"注数"当 n 会严重高估显著性。只给描述统计 + 样本量，不对差值下结论。
  - 文案一律称「历史实测平均命中」，不叫「概率/胜率」。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

#: 样本量低于此值 → 标记「样本不足」（数值照给，但前端灰显 + 提示差异多为噪声）
MIN_SAMPLE = 30


def strategy_hit_stats(lottery: str, lookback: Optional[int] = None) -> Dict[str, Any]:
    """按策略统计「历史实测命中」并附随机基线。

    返回
      {
        "彩种", "is_redblue", "样本合计", "理论基线", "整体实测",
        "策略": { 策略名: {样本数, 平均总命中, 理论基线, 差值, 中奖次数, 中奖率,
                          样本不足, 中奖等级分布, (红蓝型)平均红球命中/平均蓝球命中/基线红/基线蓝} },
        "口径说明", "诚实边界", ("note": 无数据说明)
      }
    """
    # 延迟导入：避免 ev → data / train 的循环导入
    from data.feedback import load_feedback_history, normalize_strategy_name
    from data.schema import is_redblue
    from train.metrics import compute_random_baseline

    rb = is_redblue(lottery)
    base = compute_random_baseline(lottery)
    exp_total = float(base.get("expected_total_hits") or 0.0)
    exp_red = float(base.get("expected_red_hits") or 0.0) if rb else None
    exp_blue = float(base.get("expected_blue_hits") or 0.0) if rb else None

    out: Dict[str, Any] = {
        "彩种": lottery,
        "is_redblue": rb,
        "样本合计": 0,
        "理论基线": round(exp_total, 4),
        "整体实测": None,
        "策略": {},
        "口径说明": ("实测=该策略历史真预测的『总命中』均值；基线=随机选号的超几何期望"
                     "（红蓝型另给红/蓝分项）。单位：个号码/注。"),
        "诚实边界": ("只统计开奖前锁定的真预测；同一期多注共享同一开奖号、彼此不独立，"
                     "故不做显著性检验——差值基本落在噪声范围内，"
                     "不构成「某策略更会选号」的证据。"),
    }

    history = load_feedback_history(lottery, lookback) or []
    valid = [f for f in history if f.get("valid_prediction", True) and f.get("策略")]
    if not valid:
        out["note"] = "无有效真预测记录（或记录未落盘策略）"
        return out

    all_hits = [float(f.get("总命中", 0) or 0) for f in valid]
    out["样本合计"] = len(valid)
    out["整体实测"] = round(sum(all_hits) / len(all_hits), 4)

    names: List[str] = list(dict.fromkeys(normalize_strategy_name(f["策略"]) for f in valid))
    for name in names:
        recs = [f for f in valid if normalize_strategy_name(f["策略"]) == name]
        if not recs:
            continue
        hits = [float(f.get("总命中", 0) or 0) for f in recs]
        avg = sum(hits) / len(hits)
        won = [f for f in recs if (f.get("中奖等级") or "未中") != "未中"]
        grades: Dict[str, int] = {}
        for f in recs:
            g = f.get("中奖等级") or "未中"
            grades[g] = grades.get(g, 0) + 1

        row: Dict[str, Any] = {
            "样本数": len(recs),
            "平均总命中": round(avg, 4),
            "理论基线": round(exp_total, 4),
            "差值": round(avg - exp_total, 4),
            "中奖次数": len(won),
            "中奖率": round(len(won) / len(recs), 4),
            "样本不足": len(recs) < MIN_SAMPLE,
            "中奖等级分布": grades,
        }
        if rb:
            reds = [float(f.get("红球命中", 0) or 0) for f in recs]
            blues = [float(f.get("蓝球命中", 0) or 0) for f in recs]
            row["平均红球命中"] = round(sum(reds) / len(reds), 4)
            row["平均蓝球命中"] = round(sum(blues) / len(blues), 4)
            row["基线红"] = round(exp_red or 0.0, 4)
            row["基线蓝"] = round(exp_blue or 0.0, 4)
        out["策略"][name] = row

    return out


def batch_strategy_stats(lottery: str, strategies: List[str],
                         lookback: Optional[int] = None) -> Dict[str, Any]:
    """只取 `strategies` 里出现过的策略（一批通常 1~4 种）→ payload 小。

    策略名先归一再匹配（票面是原始名，统计键是归一后的名字）。
    """
    if not strategies:
        return {}
    from data.feedback import normalize_strategy_name

    full = strategy_hit_stats(lottery, lookback)
    pool = full.get("策略") or {}
    wanted = {normalize_strategy_name(s) for s in strategies if s}
    return {k: v for k, v in pool.items() if k in wanted}
