# -*- coding: utf-8 -*-
"""看「当前 pending 的 目标期号/预测日期 是否自洽」+ predict_target 现算值（只读）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"E:\707")
from data.feedback import FEEDBACK_DIR, load_pending, predict_target, _target_draw_date  # noqa: E402

for lot in ["双色球", "福彩3D", "排列5", "七星彩"]:
    print("=" * 78)
    for p in load_pending(lot):
        print(f"{lot} pending: 目标期号={p.get('目标期号')} 预测日期={p.get('预测日期')} "
              f"生成时间={p.get('生成时间')} 来源={p.get('来源')} 注数={len(p.get('预测号码', []))} "
              f"批次号={p.get('批次号')}")
    iss, day = predict_target(lot)
    print(f"  >> predict_target 现算 = ({iss}, {day})")
    if iss is not None:
        print(f"  >> _target_draw_date({lot}, {iss}) = {_target_draw_date(lot, iss)}")
    print()

# 直接看原始 json 里最新一条 pending 的完整字段
for lot in ["双色球", "福彩3D"]:
    f = Path(FEEDBACK_DIR) / f"{lot}_pending.json"
    data = json.loads(f.read_text(encoding="utf-8"))
    arr = data if isinstance(data, list) else data.get("pending", data.get("预测", []))
    print(f"--- {f.name}: 顶层类型={type(data).__name__}, 条数={len(arr)}")
    if arr:
        print("    最新条 keys:", sorted(arr[-1].keys()))
        print("    最新条:", json.dumps({k: v for k, v in arr[-1].items() if k != "预测号码"}, ensure_ascii=False)[:600])
