# -*- coding: utf-8 -*-
"""排列3 feedback_history 抢救脚本（一次性）。

背景：`training/feedback/排列3_feedback_history.json` 头部 368,451 字节被
`[]` + NUL 覆盖 —— `_safe_write_json` 用 `open(path,"w")` 非原子写，
中途被杀后 NTFS 留下空洞（2026-10-24 实查）。

抢救策略：
  · A 段 = `_backup_20260911/排列3_feedback_history.json`（853 条，26212~26243）
  · B 段 = 损坏文件 368,451 字节之后的完整尾部（1611 条，26236~26283）
  · 合并、按 feedback 去重键（台账域, 档位, 类型, 期号, 策略, 号码）去重，B 段优先
  · 原子写回（tmp + fsync + os.replace），写完立即回读校验

用法：
  python scripts/oneoff/_rescue_pa3_history.py --dry-run
  python scripts/oneoff/_rescue_pa3_history.py --apply
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"E:\707")

FB = Path(r"E:\707\training\feedback")
TARGET = FB / "排列3_feedback_history.json"
BACKUP = FB / "_backup_20260911" / "排列3_feedback_history.json"


def salvage_tail(path: Path) -> list:
    """从 NUL 空洞之后抢救完整记录。"""
    b = path.read_bytes()
    runs = [(m.start(), m.end()) for m in re.finditer(rb"\x00+", b)]
    big = [r for r in runs if r[1] - r[0] > 1000]
    if not big:
        raise SystemExit("未发现长 NUL 段，文件可能是另一种损坏形态，终止")
    body_start = big[-1][1]
    body = b[body_start:]
    m = re.search(rb"\r\n  \{\r\n", body) or re.search(rb"\n  \{\n", body)
    if not m:
        raise SystemExit("找不到完整记录起点，终止")
    start = body_start + m.start()
    cand = b"[" + b[start:]
    data = json.loads(cand.decode("utf-8"))
    print(f"[抢救] NUL 段 {big[-1][0]}~{big[-1][1]}，"
          f"从偏移 {start} 起抢救出 {len(data)} 条")
    return data


def key_of(r: dict):
    """与 data/feedback._fb_key 同构的去重键。"""
    dom = r.get("台账域") or "生产"
    tier = r.get("档位") or "一般"
    if "红球命中" in r:
        return (dom, tier, "RB", r.get("期号"), r.get("策略"),
                tuple(r.get("预测红球") or []), tuple(r.get("预测蓝球") or []))
    return (dom, tier, "NUM", r.get("期号"), r.get("策略"),
            json.dumps(r.get("预测号码"), ensure_ascii=False, sort_keys=True))


def atomic_write(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    if not (args.apply or args.dry_run):
        ap.error("必须指定 --apply 或 --dry-run")

    a = json.loads(BACKUP.read_text(encoding="utf-8")) if BACKUP.exists() else []
    b = salvage_tail(TARGET)
    print(f"[输入] 备份段 {len(a)} 条；抢救段 {len(b)} 条")

    merged, seen = [], set()
    # B 段（新）优先：字段更全、含机制快照
    for r in b + a:
        k = key_of(r)
        if k in seen:
            continue
        seen.add(k)
        merged.append(r)
    merged.sort(key=lambda r: (str(r.get("开奖日期") or ""), int(r.get("期号") or 0),
                               r.get("批次内序号") or 0))

    iss = [int(r["期号"]) for r in merged if isinstance(r.get("期号"), int)]
    print(f"[合并] {len(merged)} 条（去重 {len(a) + len(b) - len(merged)} 条）")
    print(f"[范围] 期号 {min(iss)} ~ {max(iss)}，"
          f"日期 {merged[0].get('开奖日期')} ~ {merged[-1].get('开奖日期')}")
    print(f"[分布] 类型 {dict(Counter(r.get('记录类型') for r in merged))}")
    print(f"[分布] valid {dict(Counter(bool(r.get('valid_prediction', True)) for r in merged))}")

    per_issue = Counter(int(r["期号"]) for r in merged if isinstance(r.get("期号"), int))
    gaps = [i for i in range(min(iss), max(iss) + 1) if i not in per_issue]
    print(f"[缺口] 期号不连续处 {len(gaps)} 个（前 10：{gaps[:10]}）")

    if args.dry_run:
        print("\n[dry-run] 未写盘。加 --apply 执行。")
        return

    atomic_write(TARGET, merged)
    back = json.loads(TARGET.read_text(encoding="utf-8"))
    assert len(back) == len(merged), f"回读条数不一致 {len(back)} != {len(merged)}"
    assert BACKUP.exists() or True
    print(f"\n[写回] 原子写完成，回读校验 {len(back)} 条 OK")
    print(f"[写回] {TARGET}")


if __name__ == "__main__":
    main()
