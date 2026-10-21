# -*- coding: utf-8 -*-
"""关键取证：被判 valid_prediction=False 的记录里，有多少其实「生成时间早于开奖日期」。

若生成时间 < 开奖日期，说明该批次是开奖前就出好的号 → 不该被判为马后炮训练。
（只读，不写任何数据）
"""
import sys
from collections import Counter
from datetime import date, datetime

sys.path.insert(0, r"E:\707")
from data.feedback import load_feedback_history  # noqa: E402

LOTS = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]


def _d(s):
    try:
        return datetime.fromisoformat(str(s)).date()
    except Exception:
        try:
            return date.fromisoformat(str(s)[:10])
        except Exception:
            return None


grand = Counter()
mis = []
for lot in LOTS:
    hist = load_feedback_history(lot)
    tot = len(hist)
    tr = [r for r in hist if not r.get("valid_prediction", True)]
    before = []      # 生成时间 <= 开奖日期 → 其实不是马后炮
    batches = {}
    for r in tr:
        bt, ad = _d(r.get("批次时间") or r.get("评估时间")), _d(r.get("开奖日期"))
        if bt and ad and bt <= ad:
            before.append(r)
            batches.setdefault(r.get("批次号", "?"), []).append(r)
    grand["总记录"] += tot
    grand["训练类"] += len(tr)
    grand["其中生成早于开奖"] += len(before)
    grand["真预测"] += tot - len(tr)
    print(f"{lot:6s} 总 {tot:5d} | 真预测 {tot-len(tr):5d} | 训练类 {len(tr):4d} | "
          f"★其中「批次生成时间<=开奖日期」{len(before):4d} 条，涉及 {len(batches)} 个批次")
    for b, rs in list(batches.items())[:6]:
        r0 = rs[0]
        mis.append((lot, b, r0.get("批次时间"), r0.get("预测日期"), r0.get("开奖日期"), r0.get("期号"), len(rs)))
    # 训练类的两条来源各有多少
    print("        来源分布:", dict(Counter(r.get("来源") for r in tr)))

print()
print("=" * 78)
print("汇总:", dict(grand))
print()
print("★ 疑似被误判的批次（生成时间早于开奖日，却被标为训练）：")
for m in mis:
    print("   ", m)
print(f"   共 {len(mis)} 个批次 / {grand['其中生成早于开奖']} 条记录")
