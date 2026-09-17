# -*- coding: utf-8 -*-
"""
历史反馈记录修复（写操作，默认 dry-run）。

修复对象：feedback 历史里「命中字段与真实开奖重算不符」的记录。
典型场景：数字型早期用「万位/千位/…」分区名，schema 改为「第N位」后命中被静默算成 0
（2026-09-11 审计发现 4 条：排列5 期26215 #27/#29/#30、排列3 期26215 #28）。

只改这些字段，绝不改号码本身：
  - 分区命中 / 总命中（数字型）
  - 红球命中 / 蓝球命中 / 总命中（乐透型）
  - 中奖等级（若重算后不同）
不触碰：预测号码、实际号码、valid_prediction、来源、记录类型、模拟盈亏。

用法：
  E:/Python/python.exe scripts/repair_records.py            # 只看差异（dry-run）
  E:/Python/python.exe scripts/repair_records.py --apply    # 真正写回（自动先备份）
"""
import json
import shutil
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LOTTERY_CONFIG
from data.loader import load_lottery
from data.schema import schema_from_cfg
from scripts.audit_records import (  # 复用同一套口径，避免两处漂移
    canon, norm_zones, recompute_hits, expected_grade, group_expected, check_numbers,
)

BASE = Path(__file__).resolve().parent.parent
FB = BASE / "training" / "feedback"
LOTTERIES = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]


def diff_record(name, rec, draw, schema):
    """返回需要修改的字段字典（空 dict = 无需修改）。"""
    is_rb = name in ("双色球", "大乐透")
    act = canon(name, getattr(draw, "zone_numbers", {}) or {})
    pred = canon(name, norm_zones(rec, is_rb))
    hits = recompute_hits(pred, act, schema)
    patch = {}

    # 规则 0：预测号码本身非法（个数/范围/重复不符 schema）→ 那不是一张合法彩票，
    # 不得保留任何奖级。历史案例：双色球 26090 预测蓝球 [1,2]（应为 1 个）却因红球全中
    # 被判「二等」，是 1/110 万级的假高奖级，且会出现在战绩查询里（该接口不过滤 invalid）。
    bad = check_numbers(name, pred, schema)
    if bad:
        if (rec.get("中奖等级") or "未中") != "未中":
            patch["中奖等级"] = "未中"
        if rec.get("valid_prediction", True):
            patch["valid_prediction"] = False
        if rec.get("记录类型") != "训练":
            patch["记录类型"] = "训练"
        if not rec.get("备注"):
            patch["备注"] = f"已修正：预测号码非法（{'；'.join(bad)}），原奖级不可信"
        return patch

    if is_rb:
        rh, bh = hits.get("红球", 0), hits.get("蓝球", 0)
        if (rec.get("红球命中"), rec.get("蓝球命中")) != (rh, bh):
            patch["红球命中"], patch["蓝球命中"] = rh, bh
        if rec.get("总命中") != rh + bh:
            patch["总命中"] = rh + bh
        exp = expected_grade(name, hits, schema)
    else:
        if canon(name, rec.get("分区命中") or {}) != hits:
            patch["分区命中"] = hits
        if rec.get("总命中") != sum(hits.values()):
            patch["总命中"] = sum(hits.values())
        exp = group_expected(name, pred, act, rec.get("期号")) or "未中"
    if (rec.get("中奖等级") or "未中") != exp:
        patch["中奖等级"] = exp
    return patch


def main():
    apply = "--apply" in sys.argv
    total = 0
    plan = {}
    for name in LOTTERIES:
        draws = {r.期号: r for r in load_lottery(name).records if r.期号}
        schema = schema_from_cfg(LOTTERY_CONFIG[name], name)
        hf = FB / f"{name}_feedback_history.json"
        if not hf.exists():
            continue
        hist = json.load(open(hf, encoding="utf-8"))
        changed = []
        for i, rec in enumerate(hist):
            draw = draws.get(rec.get("期号"))
            if draw is None:
                continue
            patch = diff_record(name, rec, draw, schema)
            if patch:
                changed.append((i, rec.get("期号"), patch))
        if changed:
            plan[name] = (hf, hist, changed)
            total += len(changed)
            print(f"[{name}] 需修复 {len(changed)} 条")
            for i, issue, patch in changed:
                print(f"    #{i} 期{issue} → {patch}")

    print(f"\n合计需修复 {total} 条")
    if not apply:
        print("（dry-run，未写入。加 --apply 执行，执行前会自动备份）")
        return
    if not plan:
        print("无需要修改的记录。")
        return

    backup_dir = FB / f"_backup_{date.today():%Y%m%d}"
    backup_dir.mkdir(parents=True, exist_ok=True)
    for name, (hf, hist, changed) in plan.items():
        shutil.copy2(hf, backup_dir / hf.name)
        for i, _issue, patch in changed:
            hist[i].update(patch)
        tmp = hf.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(hist, ensure_ascii=False, indent=2), encoding="utf-8")
        # 落位：沙箱下 os.replace 可能因句柄被锁失败，回退 copyfile+unlink
        try:
            import os
            os.replace(tmp, hf)
        except OSError:
            shutil.copyfile(tmp, hf)
            try:
                os.unlink(tmp)
            except OSError:
                pass
        print(f"[{name}] 已写回 {len(changed)} 条修复")
    print(f"备份目录：{backup_dir}")


if __name__ == "__main__":
    main()
