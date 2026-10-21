# -*- coding: utf-8 -*-
"""补两件事：(1) 无时间戳那批的 (预测日期-开奖日期) 特征；(2) 来源=train 的正主长啥样；
(3) 随机锚点台账是否与 feedback 完全隔离。（只读）"""
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


gap_all, gap_src_train, notime = Counter(), Counter(), Counter()
for lot in LOTS:
    for r in load_feedback_history(lot):
        if r.get("valid_prediction", True):
            continue
        pd_, ad = _d(r.get("预测日期")), _d(r.get("开奖日期"))
        bt = _d(r.get("批次时间"))
        gap = (pd_ - ad).days if (pd_ and ad) else None
        gap_all[gap] += 1
        if r.get("来源") in ("train", "训练"):
            gap_src_train[gap] += 1
            notime["train来源样例 " + "|".join(
                f"{k}={r.get(k)}" for k in ("期号", "预测日期", "开奖日期", "批次号", "批次时间"))] += 1
        if not bt:
            notime[f"无时间戳 gap={gap}"] += 1

print("全部训练标签记录的 (预测日期 - 开奖日期) 天数分布:", dict(sorted(gap_all.items(), key=lambda x: str(x[0]))))
print("其中 来源=train 的同一分布:", dict(sorted(gap_src_train.items(), key=lambda x: str(x[0]))))
print()
print("无时间戳样本特征（前 12）:")
for k, v in list(notime.items())[:12]:
    print(f"   {v:4d} × {k}")
print()
print("来源=train 具体样例（前 8）:")
cnt = 0
for lot in LOTS:
    for r in load_feedback_history(lot):
        if not r.get("valid_prediction", True) and r.get("来源") in ("train", "训练"):
            print(f"   {lot:5s}", {k: r.get(k) for k in ("期号", "预测日期", "开奖日期", "批次号", "批次时间", "策略")})
            cnt += 1
            if cnt >= 8:
                break
    if cnt >= 8:
        break

print()
print("=== 随机锚点台账位置（应与 feedback 完全隔离）===")
from pathlib import Path  # noqa: E402
for p in [r"E:\707\training\anchor_trials", r"E:\707\training\feedback", r"E:\707\config\training_loop.json"]:
    pp = Path(p)
    if pp.is_dir():
        fs = list(pp.glob("*"))[:8]
        print(f"   [DIR] {p}  文件 {len(list(pp.glob('*')))} 个, 例: {[f.name for f in fs]}")
    elif pp.is_file():
        print(f"   [FILE] {p}  {pp.stat().st_size} bytes")
    else:
        print(f"   [缺失] {p}")
