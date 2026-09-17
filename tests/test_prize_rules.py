"""
奖级判定回归测试（2026-08-31 新增）。

覆盖四套官方判级：
1. 大乐透 _classify_dlt_prize（2023 九级，条件与 ev/rollover.py::_dlt_fixed 一致）
2. 双色球 _classify_prize（六等级）
3. 七星彩 _qxc_prize（任意位置匹配位数）
4. 数字型 _group_winning（直选/组选3/组选6/未中）

只读纯函数测试，不触碰任何数据文件。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.feedback as fb

FAILED = []


def check(name, cond, detail=""):
    if cond:
        print(f"  [OK] {name}")
    else:
        print(f"  [FAIL] {name}  {detail}")
        FAILED.append(name)


def test_dlt_prize():
    print("\n[1] 大乐透九级判级 _classify_dlt_prize")
    cases = [
        ((5, 2), "一等"), ((5, 1), "二等"), ((5, 0), "三等"),
        ((4, 2), "四等"), ((4, 1), "五等"), ((3, 2), "六等"),
        ((4, 0), "七等"), ((3, 1), "八等"), ((2, 2), "八等"),
        ((3, 0), "九等"), ((2, 1), "九等"), ((1, 2), "九等"), ((0, 2), "九等"),
        ((2, 0), "未中"), ((1, 1), "未中"), ((1, 0), "未中"),
        ((0, 1), "未中"), ((0, 0), "未中"),
    ]
    for (r, b), expect in cases:
        got = fb._classify_dlt_prize(r, b)
        check(f"大乐透 {r}+{b} → {expect}", got == expect, f"got {got}")

    # 关键：0+1/1+1 官方不中奖（旧双色球规则会虚报六等）；3+0/1+2/0+2 官方中奖（旧规则漏判）
    check("大乐透 0+1 不得判为六等", fb._classify_dlt_prize(0, 1) != "六等")
    check("大乐透 3+0 中奖(九等)", fb._classify_dlt_prize(3, 0) == "九等")
    check("大乐透 1+2 中奖(九等)", fb._classify_dlt_prize(1, 2) == "九等")


def test_ssq_prize():
    print("\n[2] 双色球六等级判级 _classify_prize")
    cases = [
        ((6, 1), "一等"), ((6, 0), "二等"), ((5, 1), "三等"),
        ((5, 0), "四等"), ((4, 1), "四等"),
        ((4, 0), "五等"), ((3, 1), "五等"),
        ((2, 1), "六等"), ((1, 1), "六等"), ((0, 1), "六等"),
        ((3, 0), "未中"), ((2, 0), "未中"), ((0, 0), "未中"),
    ]
    for (r, b), expect in cases:
        got = fb._classify_prize(r, b)
        check(f"双色球 {r}+{b} → {expect}", got == expect, f"got {got}")


def test_qxc_prize():
    print("\n[3] 七星彩 _qxc_prize（任意位置匹配位数）")
    cases = [
        ((6, 1), "一等奖"), ((6, 0), "二等奖"), ((5, 1), "三等奖"),
        ((5, 0), "四等奖"), ((4, 1), "四等奖"), ((4, 0), "五等奖"),
        ((3, 1), "五等奖"), ((3, 0), "六等奖"), ((2, 1), "六等奖"),
        ((1, 1), "六等奖"), ((0, 1), "六等奖"),
        ((2, 0), None), ((1, 0), None), ((0, 0), None),
    ]
    for (x, y), expect in cases:
        lvl, _ = fb._qxc_prize(_mk_zh(x, y))
        check(f"七星彩 X={x} Y={y} → {expect}", lvl == expect, f"got {lvl}")


def _mk_zh(x_hits, y_hit):
    zh = {f"第{i}位": 0 for i in range(1, 8)}
    for i in range(1, x_hits + 1):
        zh[f"第{i}位"] = 1
    zh["第7位"] = 1 if y_hit else 0
    return {"分区命中": zh}


def test_group_winning():
    print("\n[4] 数字型 _group_winning")
    # 排列3：直选/组选3/组选6/未中
    check("排3 直选 123/123", fb._group_winning("排列3", [1, 2, 3], [1, 2, 3])[0] == "直选")
    check("排3 组选3 112/121", fb._group_winning("排列3", [1, 1, 2], [1, 2, 1])[0] == "组选3")
    check("排3 组选6 123/321", fb._group_winning("排列3", [1, 2, 3], [3, 2, 1])[0] == "组选6")
    check("排3 未中 123/456", fb._group_winning("排列3", [1, 2, 3], [4, 5, 6])[0] is None)
    # 排列5：只设一个奖级（直选），同集合不同序=未中
    check("排5 直选 12345/12345",
          fb._group_winning("排列5", [1, 2, 3, 4, 5], [1, 2, 3, 4, 5])[0] == "直选")
    check("排5 同集合不同序=未中",
          fb._group_winning("排列5", [1, 2, 3, 4, 5], [5, 4, 3, 2, 1])[0] is None)


if __name__ == "__main__":
    test_dlt_prize()
    test_ssq_prize()
    test_qxc_prize()
    test_group_winning()
    print("\n" + "=" * 50)
    if FAILED:
        print(f"[FAIL] 失败 {len(FAILED)} 项: {FAILED}")
        sys.exit(1)
    print("[OK] 四套奖级判级全部通过")
