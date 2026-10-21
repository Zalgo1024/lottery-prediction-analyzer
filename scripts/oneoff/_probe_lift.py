# -*- coding: utf-8 -*-
"""探查：pending 文件里评估后的记录是否保留、每注是否带 置信度。"""
import json
from pathlib import Path

FB = Path(r"E:\707\training\feedback")
for p in sorted(FB.glob("*_pending.json")):
    data = json.loads(p.read_text(encoding="utf-8"))
    total = len(data)
    by_status = {}
    with_conf = 0
    with_batch = 0
    for r in data:
        by_status[r.get("状态")] = by_status.get(r.get("状态"), 0) + 1
        for ps in (r.get("预测号码") or []):
            if isinstance(ps, dict) and ps.get("置信度") is not None:
                with_conf += 1
                break
        if r.get("批次号"):
            with_batch += 1
    print(f"{p.name}: {total} 条 状态={by_status} 带批次号={with_batch} 含置信度={with_conf}")

for p in sorted(FB.glob("*_feedback_history.json")):
    data = json.loads(p.read_text(encoding="utf-8"))
    has_batch = sum(1 for r in data if r.get("批次号"))
    has_seq = sum(1 for r in data if r.get("批次内序号"))
    wins = sum(1 for r in data if (r.get("中奖等级") or "未中") != "未中")
    print(f"{p.name}: {len(data)} 条 批次号={has_batch} 批内序号={has_seq} 中奖={wins}")
