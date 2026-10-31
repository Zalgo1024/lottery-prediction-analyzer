# -*- coding: utf-8 -*-
"""按**当期规则版本**为历史逐注记录回填成本 / 奖金 / 盈亏字段。

为什么必须按「当期」而不是「现行」规则
------------------------------------
同名奖级在不同规则版本下含义会变。大乐透「四等」是最典型的一例：

    2019 版  4+2 才够四等（p = 1.01e-5）
    2014 版  4+1 就是四等（p = 3.53e-4）
    2026 版  4+1 就是四等（p = 1.50e-4）

同一个词，门槛差 35 倍。若用现行版本的固定额去结算 2019 年的记录，会得出
「奖级对、金额错」——数字合法、量级离谱，比报错难查（历史上踩过一次返奖率
163.8% 的事故，正确值 27.75%）。

★ 本脚本**不改动任何号码、命中、valid_prediction、来源、记录类型**，只写金额字段。
★ 默认 dry-run；`--apply` 前会自动备份成 `.bak-<时间戳>`。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOTS = ("双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩", "七乐彩", "快乐8")

# 只写这些字段（其余一律不动）
MONEY_FIELDS = ("单价", "成本", "税前奖金", "实得", "净盈亏",
                "金额口径", "金额未知", "模拟盈亏")

_NON_WIN = {"", "未中", "未中奖", "None"}


def norm_grade(grade: Any) -> str:
    """奖级名归一化：「六等奖」→「六等」。

    ★ 同一个奖级在项目里有两套写法 —— 七星彩历史记录写「六等奖 / 五等奖」，
      而 `rules.grades` 的键是「六等 / 五等」。不归一化时这些记录会落到
      「未知奖级」分支，金额被当成拿不到 ⇒ 那 158 注的奖金凭空消失，
      账本却仍然"有数字"，属于最难发现的一类静默丢失。
      （与 `data/schema.normalize_zone_names` 同类的坑，只是换了个字段。）
    """
    s = str(grade if grade is not None else "").strip()
    if len(s) > 1 and s.endswith("奖"):
        s = s[:-1]
    return s


def _history_path(lottery: str) -> Optional[Path]:
    from data.feedback import FEEDBACK_DIR
    p = FEEDBACK_DIR / f"{lottery}_feedback_history.json"
    return p if p.exists() else None


def amount_of(lottery: str, issue: Any, grade: Any) -> Tuple[Optional[float], str]:
    """该期该奖级的**单注税前奖金** → (金额 或 None, 口径)。

    `None` 表示「拿不到」，调用方必须标成 `金额未知`，**不得按 0 计入**：
    按 0 会让合计系统性偏低，按上限会虚报，两种都会让「净盈亏」变成错数。
    """
    if grade is None or str(grade) in _NON_WIN:
        return 0.0, "未中"

    from ev.profit.rules import rules_for

    try:
        rules = rules_for(lottery, issue)
    except Exception as e:                                    # noqa: BLE001
        return None, f"规则版本不可解析({type(e).__name__})"

    rule = (getattr(rules, "grades", None) or {}).get(norm_grade(grade))
    if rule is None:
        return None, "未知奖级"

    if getattr(rule, "kind", None) == "fixed":
        # 同版本时优先取当期实测（派奖期会是「基本 + 派奖」，固定额会漏掉派奖部分）；
        # 取不到才回落规则表固定额。跨版本不走这条路 —— 那时同名奖级含义已变。
        try:
            from data.prize_amounts import actual_per_bet
            amt, note = actual_per_bet(lottery, issue, norm_grade(grade), rule.fixed)
        except Exception:                                     # noqa: BLE001
            amt, note = None, "规则表"
        if amt is not None:
            return float(amt), note
        return float(rule.fixed), "规则表"

    # 浮动奖：必须取当期实测，取不到就是未知
    try:
        from data.prize_amounts import actual_per_bet
        amt, note = actual_per_bet(lottery, issue, norm_grade(grade), None)
    except Exception:                                         # noqa: BLE001
        return None, "浮动奖查询失败"
    if amt is None:
        return None, "浮动奖无当期数据"
    return float(amt), note


def backfill(lottery: str, *, apply: bool = False, force: bool = False) -> Dict[str, Any]:
    from config import ticket_price

    p = _history_path(lottery)
    if p is None:
        return {"彩种": lottery, "跳过": "无历史文件"}

    records = json.loads(p.read_text(encoding="utf-8"))
    price = float(ticket_price(lottery))

    filled = skipped = unknown = 0
    notes: Counter = Counter()
    versions: Counter = Counter()
    total_cost = total_gross = 0.0
    touched = 0

    for r in records:
        if not isinstance(r, dict):
            continue
        already = r.get("实得") is not None and r.get("金额口径")
        if already and not force:
            skipped += 1
            continue

        issue = r.get("期号")
        grade = r.get("中奖等级")
        amt, note = amount_of(lottery, issue, grade)
        notes[note] += 1

        try:
            from ev.profit.rules import rules_for
            versions[rules_for(lottery, issue).rule_version] += 1
        except Exception:                                     # noqa: BLE001
            versions["(不可解析)"] += 1

        before = json.dumps({k: r.get(k) for k in MONEY_FIELDS}, ensure_ascii=False, sort_keys=True)

        r["单价"] = price
        r["成本"] = round(price, 2)
        if amt is None:
            r["税前奖金"] = None
            r["实得"] = None
            r["净盈亏"] = None
            r["金额口径"] = note
            r["金额未知"] = True
            unknown += 1
        else:
            r["税前奖金"] = round(float(amt), 2)
            r["实得"] = round(float(amt), 2)   # 逐注层面不扣税：税按期合计征
            r["净盈亏"] = round(float(amt) - price, 2)
            r["金额口径"] = note
            r["金额未知"] = False
            total_cost += price
            total_gross += float(amt)
            filled += 1

        # 旧策略评分字段接到新金额上（此前从未填充过）
        r["模拟盈亏"] = r["净盈亏"]

        after = json.dumps({k: r.get(k) for k in MONEY_FIELDS}, ensure_ascii=False, sort_keys=True)
        if before != after:
            touched += 1

    if apply and touched:
        ts = time.strftime("%Y%m%d-%H%M%S")
        bak = p.with_suffix(p.suffix + f".bak-money-{ts}")
        bak.write_bytes(p.read_bytes())
        from data.feedback import _safe_write_json
        _safe_write_json(p, records)

    return {
        "彩种": lottery,
        "总记录": len(records),
        "将回填": touched,
        "已有跳过": skipped,
        "有金额": filled,
        "金额未知": unknown,
        "成本合计": round(total_cost, 2),
        "税前奖金合计": round(total_gross, 2),
        "净盈亏合计": round(total_gross - total_cost, 2),
        "口径分布": dict(notes.most_common()),
        "规则版本分布": dict(versions.most_common()),
        "已写入": bool(apply and touched),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="按当期规则版本回填历史金额")
    ap.add_argument("--lottery", "-l", action="append", choices=list(LOTS))
    ap.add_argument("--all", action="store_true", help="全部彩种")
    ap.add_argument("--apply", action="store_true", help="真正写入（默认只 dry-run）")
    ap.add_argument("--force", action="store_true", help="已回填的也重算")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    lots = list(LOTS) if args.all else (args.lottery or ["双色球"])
    out = [backfill(lot, apply=args.apply, force=args.force) for lot in lots]

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"{'彩种':<8}{'总记录':>7}{'回填':>7}{'有金额':>8}{'未知':>7}"
          f"{'成本':>10}{'奖金':>12}{'净盈亏':>12}")
    print("-" * 78)
    for d in out:
        if "跳过" in d:
            print(f"{d['彩种']:<8}{d['跳过']}")
            continue
        print(f"{d['彩种']:<8}{d['总记录']:>7}{d['将回填']:>7}{d['有金额']:>8}"
              f"{d['金额未知']:>7}{d['成本合计']:>10.0f}{d['税前奖金合计']:>12.0f}"
              f"{d['净盈亏合计']:>12.0f}")
    print()
    for d in out:
        if "跳过" in d:
            continue
        if d["金额未知"]:
            print(f"★ {d['彩种']}：{d['金额未知']} 注金额未知（不计入合计）"
                  f" → {d['口径分布']}")
        if d["规则版本分布"]:
            print(f"  {d['彩种']} 规则版本：{d['规则版本分布']}")
    if not args.apply:
        print("\n（dry-run，未写入。确认无误后加 --apply）")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
