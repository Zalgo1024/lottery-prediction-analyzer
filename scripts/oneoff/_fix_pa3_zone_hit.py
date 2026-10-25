# -*- coding: utf-8 -*-
"""一次性修复：把 feedback 历史里存错的 `分区命中` / `总命中` 按号码重算。

背景（2026-10-25）：
    `scripts/audit_records.py` 报「排列3 分区命中不符 @历史#28 期26215」。
    查证：该记录是**旧格式** —— `预测号码` 用 `百位/十位/个位` 命名，
    而 `分区命中` 用 `第1位/第2位/第3位`，当年结算时两边位置没对上，
    于是 百位9 对 第1位9（本该命中）被记成 0。

    判决：这是**存值错误**，不是审计误报。`canon()` 已能把旧名规范化，
    所以 `预测号码` 校验通过、只有 `分区命中` 对不上。

做法：只重算 `分区命中` / `总命中` 两个字段，其余原样不动；
      `中奖等级` 不在此脚本职责内（排列3 直选需三位全中，本例仍是未中）。

用法：
    PYTHONUTF8=1 E:/Python/python.exe -u scripts/oneoff/_fix_pa3_zone_hit.py --dry-run
    PYTHONUTF8=1 E:/Python/python.exe -u scripts/oneoff/_fix_pa3_zone_hit.py --apply
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from audit_records import canon, recompute_hits  # noqa: E402
from config import LOTTERY_CONFIG  # noqa: E402
from data.schema import schema_from_cfg  # noqa: E402

FB_DIR = ROOT / "training" / "feedback"


def _norm(r: dict) -> dict:
    """记录里的预测号码：新旧两种键名都收。"""
    z = r.get("预测号码") or {}
    if isinstance(z, dict) and z:
        return z
    return {}


def fix(lottery: str, apply: bool) -> int:
    path = FB_DIR / f"{lottery}_feedback_history.json"
    if not path.exists():
        print(f"[跳过] {path} 不存在")
        return 0
    records = json.loads(path.read_text(encoding="utf-8"))
    schema = schema_from_cfg(LOTTERY_CONFIG[lottery], lottery)
    is_rb = lottery in ("双色球", "大乐透")
    if is_rb:
        print("[跳过] 本脚本只处理数字型（红蓝型的命中字段另有口径）")
        return 0

    changed = []
    for i, r in enumerate(records):
        pred = canon(lottery, _norm(r))
        act = canon(lottery, r.get("实际号码") or {})
        if not pred or not act:
            continue
        exp = dict(recompute_hits(pred, act, schema))
        stored = canon(lottery, r.get("分区命中") or {})
        exp_total = sum(int(v or 0) for v in exp.values())
        if stored == exp and r.get("总命中") == exp_total:
            continue
        changed.append({
            "idx": i,
            "期号": r.get("期号"),
            "旧分区命中": r.get("分区命中"),
            "新分区命中": exp,
            "旧总命中": r.get("总命中"),
            "新总命中": exp_total,
        })
        r["分区命中"] = exp
        r["总命中"] = exp_total
        r["_修复"] = f"分区命中重算@{datetime.now():%Y-%m-%d %H:%M:%S}"

    print(f"{lottery}: 记录 {len(records)} 条，需修 {len(changed)} 条")
    for c in changed:
        print(f"  历史#{c['idx']} 期{c['期号']}  分区命中 {c['旧分区命中']} -> {c['新分区命中']}"
              f"  总命中 {c['旧总命中']} -> {c['新总命中']}")

    if not apply:
        print("（dry-run，未写盘）")
        return len(changed)
    if not changed:
        return 0

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = path.with_name(f"{path.name}.bak-{stamp}")
    shutil.copy2(path, bak)
    from data.feedback import _safe_write_json
    _safe_write_json(path, records)
    back = json.loads(path.read_text(encoding="utf-8"))
    assert len(back) == len(records), "回读条数不一致"
    print(f"已写回 {path}（备份 {bak.name}），回读校验通过")
    return len(changed)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lottery", default="排列3")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    apply = a.apply and not a.dry_run
    n = fix(a.lottery, apply)
    return 0 if (n >= 0) else 1


if __name__ == "__main__":
    raise SystemExit(main())
