# -*- coding: utf-8 -*-
"""从真实开奖数据重建 config.MARKET_CLOSURE（休市日历）。

用法：
    E:/Python/python.exe scripts/_rebuild_market_closure.py            # 打印表 + 与现值 diff
    E:/Python/python.exe scripts/_rebuild_market_closure.py --write    # 直接写回 config.py

原理：
    排列3/排列5/福彩3D 是「每天开奖」彩种，若某天三者在本地 CSV 里同时缺记录，
    该天即为全市场休市日（不受单彩种数据缺失干扰）。把连续休市日聚合成窗口，
    即得官方休市公告的实际范围。

每年官方休市公告发布后跑一次本脚本，把新窗口补进 config.MARKET_CLOSURE，
再把对应年份从推算兜底切到精确表即可（tests/test_market_closure.py 会自动校验）。
"""
import re
import sys
from datetime import date, timedelta

sys.path.insert(0, r"E:\707")
from config import MARKET_CLOSURE  # noqa: E402
from data.loader import load_lottery  # noqa: E402

DAILY_LOTS = ["排列3", "排列5", "福彩3D"]


def rebuild():
    """返回 {年份: [(月, 日, 天数), ...]}"""
    actual = {}
    for lot in DAILY_LOTS:
        recs = [r for r in load_lottery(lot).records if r.开奖日期]
        actual[lot] = {r.开奖日期 for r in recs}

    lo = max(min(actual[l]) for l in DAILY_LOTS)
    hi = min(max(actual[l]) for l in DAILY_LOTS)

    missing, d = [], lo
    while d <= hi:
        if all(d not in actual[l] for l in DAILY_LOTS):
            missing.append(d)
        d += timedelta(days=1)

    spans = []
    if missing:
        s = p = missing[0]
        for x in missing[1:]:
            if (x - p).days == 1:
                p = x
            else:
                spans.append((s, p))
                s = p = x
        spans.append((s, p))

    table = {}
    for s_, e_ in spans:
        table.setdefault(s_.year, []).append((s_.month, s_.day, (e_ - s_).days + 1))
    print(f"# 数据区间 {lo} ~ {hi}，反推休市日 {len(missing)} 天 → {len(spans)} 个窗口")
    return table


def as_literal(table):
    lines = ["MARKET_CLOSURE = {"]
    for y in sorted(table):
        items = ", ".join(f"({m}, {d}, {n})" for m, d, n in table[y])
        lines.append(f"    {y}: [{items}],")
    lines.append("}")
    return "\n".join(lines)


def main():
    table = rebuild()
    print()
    print(as_literal(table))
    print()

    cur = {y: [tuple(x) for x in v] for y, v in MARKET_CLOSURE.items()}
    only_new = {y: v for y, v in table.items() if y not in cur}
    changed = {y: (cur[y], table[y]) for y in table if y in cur and cur[y] != table[y]}
    only_cur = {y: v for y, v in cur.items() if y not in table}

    print("=== 与 config.MARKET_CLOSURE 的差异 ===")
    print(f"  数据里有、表里没有的年份: {sorted(only_new) or '无'}")
    for y, v in only_new.items():
        print(f"      {y}: {v}")
    print(f"  窗口不一致的年份: {sorted(changed) or '无'}")
    for y, (a, b) in changed.items():
        print(f"      {y}: 表={a}  数据={b}")
    print(f"  表里有、数据无覆盖的年份: {sorted(only_cur) or '无'}  "
          f"（数据起点通常是 2004-11，早期年份属正常保留）")

    if "--write" in sys.argv:
        path = r"E:\707\config.py"
        src = open(path, encoding="utf-8").read()
        new_block = as_literal(table)
        src2, n = re.subn(
            r"MARKET_CLOSURE = \{.*?\n\}", new_block, src, count=1, flags=re.S)
        if n != 1:
            print("\n[ABORT] 未能定位 config.py 里的 MARKET_CLOSURE 块，未写入")
            return 1
        with open(path, "w", encoding="utf-8") as f:
            f.write(src2)
        print(f"\n[OK] 已写回 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
