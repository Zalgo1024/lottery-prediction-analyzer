# -*- coding: utf-8 -*-
"""出号注数对账 —— 配置生效值 vs 每期实际落盘注数。

三个档位，含义完全不同，不能混着报
--------------------------------
`INTERNAL_INCONSISTENT`  同一期内部自相矛盾（逐注条数 ≠ `批次规模`，或同期出现
                         多个 `批次规模`）。**这是真 bug**，exit 1。
`SHORTFALL`              实际注数 ≠ 请求注数（`_pad_to_groups` 补齐/截断或覆盖压缩
                         造成）。不算 bug，但**随机补齐会稀释策略归因**，必须列出来。
`CURRENT_MISMATCH`       最近一期条数 ≠ 当前配置生效值。多半是「配置已改但下一期
                         还没出号」或「改了没生效」，提示即可。

★ 历史期一律**不**拿当前配置去判：配置会变（本项目 10 天内从 500 变到 50），
  用今天的滑轨判昨天的批次，会把所有历史期都标成「不一致」，噪声淹没信号。
  历史期只判「同期内部一致」，「与配置一致」只判最近一期。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

LOTS = ("双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩", "七乐彩", "快乐8")

INTERNAL = "INTERNAL_INCONSISTENT"
SHORTFALL = "SHORTFALL"
CURRENT = "CURRENT_MISMATCH"


def reconcile(lottery: str) -> Dict[str, Any]:
    from data.feedback import load_feedback_history, load_pending
    from data.ticket_size import effective_count

    try:
        cfg_n = int(effective_count(lottery))
    except Exception:                                        # noqa: BLE001
        cfg_n = None

    records = load_feedback_history(lottery) or []
    groups: Dict[tuple, List[dict]] = defaultdict(list)
    for r in records:
        iss = r.get("期号")
        if iss is None:
            continue
        groups[(str(iss), str(r.get("批次号") or ""))].append(r)

    issues: List[Dict[str, Any]] = []
    for (iss, _batch), g in sorted(groups.items(), key=lambda kv: _num(kv[0][0])):
        n_rows = len(g)
        declared = {x.get("批次规模") for x in g if x.get("批次规模") is not None}
        requested = {x.get("请求注数") for x in g if x.get("请求注数") is not None}
        item = {
            "期号": iss,
            "批次规模": sorted(declared)[0] if len(declared) == 1 else None,
            "逐注条数": n_rows,
            "请求注数": sorted(requested)[0] if len(requested) == 1 else None,
            "档位": None,
        }
        if len(declared) > 1 or (len(declared) == 1 and sorted(declared)[0] != n_rows):
            item["档位"] = INTERNAL
        elif requested and sorted(requested)[0] != n_rows:
            item["档位"] = SHORTFALL
        issues.append(item)

    latest = issues[-1] if issues else None
    cur_flag = bool(latest and cfg_n is not None and latest["逐注条数"] != cfg_n)

    # 下一期 pending（若已生成）—— 这才是「当前配置是否已生效」的即时证据
    pend_n = None
    try:
        p = load_pending(lottery) or []
        if p:
            pend_n = len(p[-1].get("预测号码") or [])
    except Exception:                                        # noqa: BLE001
        pass

    counts = Counter(i["档位"] for i in issues if i["档位"])
    return {
        "彩种": lottery,
        "配置生效值": cfg_n,
        "期数": len(issues),
        "最近期": latest,
        "最近 pending 注数": pend_n,
        "与配置一致": (not cur_flag) if latest else None,
        "档位计数": dict(counts),
        "明细": [i for i in issues if i["档位"]],
        "结论": _verdict(counts, cur_flag, latest, cfg_n, pend_n),
    }


def _verdict(counts, cur_flag, latest, cfg_n, pend_n) -> str:
    if counts.get(INTERNAL):
        return f"发现 {counts[INTERNAL]} 期自相矛盾（逐注条数 ≠ 批次规模）⇒ 需要修数据"
    if cur_flag:
        return (f"最近一期 {latest['期号']} 落盘 {latest['逐注条数']} 注 ≠ 配置 {cfg_n} 注"
                f" ⇒ 配置可能尚未生效（或该期出号早于改动）")
    if pend_n is not None and cfg_n is not None and pend_n != cfg_n:
        return f"待开奖 pending 为 {pend_n} 注 ≠ 配置 {cfg_n} 注 ⇒ 下一期会按错注数出号"
    if counts.get(SHORTFALL):
        return f"{counts[SHORTFALL]} 期存在补齐/截断（不影响一致，但会稀释策略归因）"
    return "未检出不一致"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="出号注数对账")
    ap.add_argument("--lottery", "-l", action="append", choices=list(LOTS))
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    lots = list(LOTS) if args.all else (args.lottery or ["双色球"])
    out = [reconcile(lot) for lot in lots]

    ts = time.strftime("%Y%m%d-%H%M%S")
    log_dir = Path(__file__).resolve().parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / f"ticket_size_reconcile_{ts}.json").write_text(
        json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    md = ["# 出号注数对账", "", f"生成：{ts}", ""]
    md.append("| 彩种 | 配置生效值 | 最近 pending | 期数 | 最近期 | 最近落盘 | 与配置一致 | 结论 |")
    md.append("|---|---|---|---|---|---|---|---|")
    for d in out:
        lp = (d.get("最近期") or {}).get("逐注条数")
        md.append(f"| {d['彩种']} | {d.get('配置生效值')} | {d.get('最近 pending 注数')} | "
                  f"{d['期数']} | {(d.get('最近期') or {}).get('期号')} | {lp} | "
                  f"{d.get('与配置一致')} | {d['结论']} |")
    md.append("")
    bad = [i for d in out for i in d["明细"]]
    if bad:
        md.append("## 需查看的期")
        md.append("")
        md.append("| 彩种 | 期号 | 档位 | 批次规模 | 逐注条数 | 请求注数 |")
        md.append("|---|---|---|---|---|---|")
        for d in out:
            for i in d["明细"]:
                md.append(f"| {d['彩种']} | {i['期号']} | {i['档位']} | "
                          f"{i['批次规模']} | {i['逐注条数']} | {i['请求注数']} |")
    (log_dir / f"ticket_size_reconcile_{ts}.md").write_text("\n".join(md), encoding="utf-8")

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print("\n".join(md))

    # 只有「自相矛盾」才 exit 1 —— 补齐与「配置未生效」是提示，不是故障。
    return 1 if any(d["档位计数"].get(INTERNAL) for d in out) else 0


def _num(x: Any) -> float:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return 0.0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
