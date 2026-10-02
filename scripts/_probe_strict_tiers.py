# -*- coding: utf-8 -*-
"""严格分档：被判 valid_prediction=False 的记录，按「批次生成日 vs 开奖日」三分。

- 生成日 <  开奖日  → 铁证：开奖前就出好号了，绝不是马后炮（误判）
- 生成日 == 开奖日  → 存疑：当天生成，可能在开奖(21:15)前后，无法判定
- 生成日 >  开奖日  → 确认马后炮（开奖后才生成）

只看有批次时间可解析的记录。（只读）
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


G = Counter()
detail = {}
for lot in LOTS:
    hist = load_feedback_history(lot)
    tr = [r for r in hist if not r.get("valid_prediction", True)]
    b = {"铁证误判": [], "同一日存疑": [], "确认马后炮": [], "无时间戳": []}
    for r in tr:
        bt = _d(r.get("批次时间") or r.get("生成时间") or r.get("记录时间"))
        ad = _d(r.get("开奖日期"))
        if not bt or not ad:
            b["无时间戳"].append(r)
        elif bt < ad:
            b["铁证误判"].append(r)
        elif bt == ad:
            b["同一日存疑"].append(r)
        else:
            b["确认马后炮"].append(r)
    detail[lot] = b
    print(f"{lot:6s} 训练标签共 {len(tr):4d} 条 → 铁证误判 {len(b['铁证误判']):4d} | "
          f"同日存疑 {len(b['同一日存疑']):3d} | 确认马后炮 {len(b['确认马后炮']):3d} | 无时间戳 {len(b['无时间戳']):3d}")
    for k in ("铁证误判", "确认马后炮"):
        G[k] += len(b[k])
    G["训练标签总数"] += len(tr)
    G["同日存疑"] += len(b["同一日存疑"])
    G["无时间戳"] += len(b["无时间戳"])

print()
print("汇总:", dict(G))
print()
print("★ 铁证误判的批次清单（按批次聚合）：")
for lot in LOTS:
    grp = {}
    for r in detail[lot]["铁证误判"]:
        grp.setdefault(r.get("批次号") or "(无批次号)", []).append(r)
    for bid, rs in grp.items():
        r0 = rs[0]
        print(f"   {lot:5s} 期号={str(r0.get('期号')):<8} 生成={r0.get('批次时间')} "
              f"记录预测日期={r0.get('预测日期')} 实际开奖={r0.get('开奖日期')} {len(rs):3d} 条 批次={bid}")
print()
print("★ 确认马后炮的批次清单：")
for lot in LOTS:
    grp = {}
    for r in detail[lot]["确认马后炮"]:
        grp.setdefault(r.get("批次号") or "(无批次号)", []).append(r)
    for bid, rs in grp.items():
        r0 = rs[0]
        print(f"   {lot:5s} 期号={str(r0.get('期号')):<8} 生成={r0.get('批次时间')} "
              f"记录预测日期={r0.get('预测日期')} 实际开奖={r0.get('开奖日期')} {len(rs):3d} 条 来源={r0.get('来源')} 批次={bid}")
