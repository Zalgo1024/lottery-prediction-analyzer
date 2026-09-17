# -*- coding: utf-8 -*-
"""Lift 检验：按出号时的「置信度」把预测注分 5 档，看各档实际命中占比是否真的递增。

回答的问题：出号时给的推荐分/置信度，事后有没有区分力？
    第 5 档（分最高）的命中率 / 全体命中率 = Lift。若置信度有效，
    Lift 应大致单调递增且 Q5 > 1；若 ≈1 或乱序，说明评分只是噪声。

数据口径（诚实层，务必看清再解读）：
- 分数 = 出号时每注写下的 `置信度`（评估时已入账留存；2026-09-14 之前的历史
  记录没有该字段，无法追溯，会在 `覆盖率` 里如实报告，绝不拿现算分数马后炮）。
- 命中 = 中奖等级/中奖玩法 ≠ 未中（任意奖级都算中）。
- 默认只统计 `valid_prediction=True` 的真预测（--include-train 可放开）。

⚠️ 解读红线：这是事后描述性统计。样本几十~几百注时，单档命中率的二项波动
极大（SE≈√(p(1-p)/n)），档位间差异不显著 ≡ 随机。本项目机制结论已经证明
「选号改不了中奖率」（期望线性性），本脚本的作用是检验评分体系本身是否含
真实信号——别把某一档的高 Lift 当成可以下注的证据。

用法：
    python scripts/lift_check.py                       # 全彩种，5 分位档
    python scripts/lift_check.py --lottery 双色球      # 单彩种
    python scripts/lift_check.py --bins 4 --include-train
输出：终端表格 + output/lift_report.md
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LOTTERY_CONFIG
import data.feedback as fb


def _score_of(rec: dict) -> Optional[float]:
    v = rec.get("置信度")
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _hit_of(rec: dict) -> int:
    grade = rec.get("中奖等级") or rec.get("中奖玩法") or "未中"
    return 1 if grade != "未中" else 0


def quantile_bins(scores: List[float], bins: int = 5) -> List[float]:
    """分数分位切点（唯一值分位数，同分必须落同一档）。返回升序切点。

    切点语义：score > cut 才进更高档（切点值本身留在低档）。
    同分密集导致切点去重后不足 bins-1 个时，档数自动减少（不硬凑空档）。
    """
    uniq = sorted(set(scores))
    if len(uniq) < bins:
        return []                      # 唯一值太少，退化为单档
    cuts = set()
    for i in range(1, bins):
        pos = len(uniq) * i / bins
        idx = min(int(pos), len(uniq) - 1)
        cuts.add(uniq[idx])
    return sorted(cuts)


def bin_index(score: float, cuts: List[float], bins: int = 5) -> int:
    """score 落在第几档（0-based）；切点值本身留在低档（score > cut 才升级）。"""
    b = sum(1 for c in cuts if score > c)
    return min(b, bins - 1)


def binom_se(p: float, n: int) -> float:
    return (p * (1 - p) / n) ** 0.5 if n > 0 else 0.0


def collect(lottery: str, include_train: bool) -> Tuple[List[dict], int, int]:
    """取该彩种「带置信度」的记录（默认仅真预测）。

    返回 (记录列表, 无分记录数, 全部记录数)。覆盖率 = len(记录)/全部，
    无分的历史记录与被口径排除的训练记录都计入分母，不静默缩小。
    """
    try:
        history = fb.load_feedback_history(lottery, lookback=None)
    except Exception:
        return [], 0, 0
    scored, missing, total = [], 0, 0
    for r in history:
        if not isinstance(r, dict):
            continue
        total += 1
        s = _score_of(r)
        if s is None:
            missing += 1
            continue
        if not include_train and not r.get("valid_prediction", True):
            continue
        scored.append({**r, "_score": s, "_hit": _hit_of(r)})
    return scored, missing, total


def analyze(records: List[dict], bins: int = 5) -> Optional[dict]:
    """分档统计。records 可直接喂原始记录（自动取 分数/命中），也接受
    collect() 产出的带 `_score/_hit` 的记录。"""
    norm = []
    for r in records:
        if not isinstance(r, dict):
            continue
        s = r["_score"] if "_score" in r else _score_of(r)
        if s is None:
            continue
        norm.append({**r, "_score": s,
                     "_hit": r["_hit"] if "_hit" in r else _hit_of(r)})
    if not norm:
        return None
    records = norm
    scores = [r["_score"] for r in records]
    cuts = quantile_bins(scores, bins)
    k = len(cuts) + 1
    groups: List[List[dict]] = [[] for _ in range(k)]
    for r in records:
        groups[bin_index(r["_score"], cuts, k)].append(r)

    total_n = len(records)
    total_hits = sum(r["_hit"] for r in records)
    overall = total_hits / total_n

    rows = []
    for i, g in enumerate(groups):
        n = len(g)
        hits = sum(r["_hit"] for r in g)
        rate = hits / n if n else 0.0
        lo = min((r["_score"] for r in g), default=None)
        hi = max((r["_score"] for r in g), default=None)
        rows.append({
            "档": f"Q{i + 1}",
            "分数区间": f"{lo:.4f}~{hi:.4f}" if n else "—",
            "注数": n,
            "中奖注数": hits,
            "命中率": rate,
            "SE": binom_se(rate, n),
            "Lift": rate / overall if overall > 0 else 0.0,
        })
    return {"rows": rows, "overall": overall, "total_n": total_n,
            "total_hits": total_hits, "cuts": cuts}


def _table_md(rows: List[dict], overall: float) -> str:
    head = "| 档位 | 分数区间 | 注数 | 中奖注数 | 命中率 | ±SE | Lift(倍) |"
    sep = "|---|---|---|---|---|---|---|"
    lines = [head, sep]
    for r in rows:
        lines.append(
            f"| {r['档']} | {r['分数区间']} | {r['注数']} | {r['中奖注数']} "
            f"| {r['命中率']*100:.2f}% | ±{r['SE']*100:.2f}% | {r['Lift']:.2f} |")
    lines.append(f"| 全体 | — | {sum(r['注数'] for r in rows)} "
                 f"| {sum(r['中奖注数'] for r in rows)} "
                 f"| {overall*100:.2f}% | — | 1.00 |")
    return "\n".join(lines)


def _monotonic_note(rows: List[dict]) -> str:
    """档位命中率是否随分数单调递增（忽略注数为 0 的档）。"""
    rates = [r["命中率"] for r in rows if r["注数"] > 0]
    if len(rates) < 2:
        return "样本不足，无法判断单调性"
    mono = all(a <= b for a, b in zip(rates, rates[1:]))
    if mono:
        return "命中率随分数单调递增（描述性观察，不构成显著性证据）"
    q5, q1 = rates[-1], rates[0]
    if len(rates) >= 5 and q5 > 1 and q5 > max(rates[:-1]):
        return "Q5 最高但整体不单调——头部可能有微弱信号，需更多样本确认"
    return "不单调——当前样本下评分无可见区分力（与随机一致）"


def main() -> int:
    ap = argparse.ArgumentParser(description="按置信度分 5 档的 lift 检验")
    ap.add_argument("--lottery", default="全部",
                    help="彩种名或「全部」（默认全部）")
    ap.add_argument("--bins", type=int, default=5, help="分档数（默认 5）")
    ap.add_argument("--include-train", action="store_true",
                    help="包含「开奖后回测·训练」记录（默认仅真预测）")
    ap.add_argument("--out", default="output/lift_report.md",
                    help="markdown 报告输出路径")
    args = ap.parse_args()

    scope = list(LOTTERY_CONFIG) if args.lottery == "全部" else [args.lottery]
    if args.lottery != "全部" and args.lottery not in LOTTERY_CONFIG:
        print(f"未知彩种：{args.lottery}")
        return 1

    print(f"== Lift 检验（按出号时置信度分 {args.bins} 档，"
          f"{'含训练' if args.include_train else '仅真预测'}）==\n")
    md = [f"# Lift 检验报告（按置信度分 {args.bins} 档）",
          f"生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} ｜ "
          f"口径：{'全部记录' if args.include_train else '仅真预测(valid_prediction=True)'}",
          ""]

    any_data = False
    for name in scope:
        scored, missing, total_raw = collect(name, args.include_train)
        if not scored:
            print(f"◆ {name}：共 {total_raw} 条记录，均无置信度字段"
                  f"（2026-09-14 起新评估才会留存），跳过\n")
            md.append(f"## {name}\n\n无带置信度的记录（{total_raw} 条均为历史数据，"
                      "评估时未落盘分数）。新评估积累后重跑即可。\n")
            continue
        any_data = True
        res = analyze(scored, args.bins)
        coverage = len(scored) / total_raw * 100 if total_raw else 0
        title = (f"◆ {name}：带分数 {len(scored)}/{total_raw} 条（覆盖率 {coverage:.1f}%，"
                 f"无分 {missing} 条），整体命中率 {res['overall']*100:.2f}%"
                 f"（{res['total_hits']}/{res['total_n']}）")
        print(title)
        print(_table_md(res["rows"], res["overall"]))
        note = _monotonic_note(res["rows"])
        print(f"  ▸ {note}\n")
        md += [f"## {name}", "", title, "", _table_md(res["rows"], res["overall"]),
               "", f"**结论（描述性）：** {note}", ""]

    md += ["---",
           "### 解读红线（诚实层）",
           "- 命中 = 任意奖级中奖；分数 = 出号时的置信度，事后不可追溯（历史记录无此字段，覆盖率如实报告）。",
           "- 单档命中率的小样本波动极大：SE≈√(p(1-p)/n)，两档差异小于 2×SE 视为不可区分。",
           "- 本表不构成任何「高分档值得加注」的证据；机制结论：长期 EV≈−50%，选号不改中奖率。"]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(md), encoding="utf-8")
    print(f"报告已写入 {out_path}")
    return 0 if any_data else 0


if __name__ == "__main__":
    sys.exit(main())
