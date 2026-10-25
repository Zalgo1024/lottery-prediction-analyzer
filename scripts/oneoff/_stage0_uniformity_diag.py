# -*- coding: utf-8 -*-
"""大乐透红球 χ²=90.33(df=34, p<0.0001) 的来源诊断。

三种互斥可能
------------
A. **数据缺陷**：某些期号的红球字段缺失/错位/重复 ⇒ 计数被污染。
B. **规则/池变更**：某段历史的前区池不是 1..35（或抽取数不同）⇒ 边际本就不该均匀。
C. **真实偏离**：号码边际频率确实不均 ⇒ 是机制信号，但需先排除 A/B。

判据
----
· 按规则版本分段各算 χ²：若只有一段爆表 ⇒ 倾向 A/B（该段口径问题）
· 各段内部一致偏离（同号同方向）⇒ 倾向 C
· 检查每期红球个数是否恒为 5、是否有越界号、是否有重复号 ⇒ A 的直接证据
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter, defaultdict

import numpy as np

sys.path.insert(0, r"E:\707")

from ev.tier_backtest import load_test_recs
from ev.profit.rules import rules_for

LOT = "大乐透"
NRED = 35
KRED = 5


def _chi2_sf(x: float, df: int) -> float:
    try:
        from scipy.stats import chi2
        return float(chi2.sf(x, df))
    except Exception:                                   # noqa: BLE001
        z = ((x / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
        return 0.5 * math.erfc(z / math.sqrt(2))


def _chi2(obs: np.ndarray) -> tuple:
    exp = obs.sum() / len(obs)
    c = float(((obs - exp) ** 2 / exp).sum())
    return c, len(obs) - 1, _chi2_sf(c, len(obs) - 1)


def main() -> None:
    seq = load_test_recs(LOT)
    print(f"{LOT} 共 {len(seq)} 期；前区池 1..{NRED}，每期抽 {KRED}")
    print()

    # ---- A 的直接证据：每期红球个数 / 越界 / 重复
    bad_len, bad_range, bad_dup = [], [], []
    for rec in seq:
        v = list(rec.红球 or [])
        if len(v) != KRED:
            bad_len.append((str(rec.期号), len(v)))
        xs = [int(x) for x in v]
        if any(x < 1 or x > NRED for x in xs):
            bad_range.append((str(rec.期号), xs))
        if len(set(xs)) != len(xs):
            bad_dup.append((str(rec.期号), xs))
    print(f"A. 数据完整性：个数≠{KRED} 的期 {len(bad_len)} 个 | "
          f"越界号 {len(bad_range)} 个 | 期内重复号 {len(bad_dup)} 个")
    if bad_len:
        print(f"   个数异常样例 {bad_len[:5]}")
    if bad_range:
        print(f"   越界样例 {bad_range[:5]}")
    print()

    # ---- 全量频率
    cnt = Counter()
    for rec in seq:
        for x in rec.红球:
            cnt[int(x)] += 1
    obs = np.array([cnt.get(i + 1, 0) for i in range(NRED)], dtype=float)
    exp = obs.sum() / NRED
    sd = math.sqrt(exp)
    z = (obs - exp) / sd
    print(f"全量：期望 {exp:.1f}/号，范围 {int(obs.min())}~{int(obs.max())}")
    print("偏离 |z|>2 的号码：")
    for i in range(NRED):
        if abs(z[i]) > 2:
            print(f"   {i+1:>2} 号：{int(obs[i]):>4} 次  z={z[i]:+.2f}")
    print()

    # ---- B：按规则版本分段
    by_ver = defaultdict(list)
    for rec in seq:
        by_ver[rules_for(LOT, rec.期号).rule_version].append(rec)
    print("B. 按规则版本分段：")
    print(f"   {'版本':<16}{'期数':>6}{'χ²':>9}{'df':>5}{'p':>10}{'检出':>6}"
          f"{'最小':>6}{'最大':>6}")
    seg_obs = {}
    for ver, recs in sorted(by_ver.items()):
        c2 = Counter()
        for rec in recs:
            for x in rec.红球:
                c2[int(x)] += 1
        o = np.array([c2.get(i + 1, 0) for i in range(NRED)], dtype=float)
        seg_obs[ver] = o
        c, df, p = _chi2(o)
        print(f"   {ver:<17}{len(recs):>6}{c:>9.2f}{df:>5}{p:>10.4f}"
              f"{'是' if p < 0.0125 else '否':>6}{int(o.min()):>6}{int(o.max()):>6}")
    print()

    # ---- C：各段偏离方向是否一致（同号同方向 ⇒ 倾向真实偏离）
    print("C. 各段偏离方向一致性（两两段之间的 z 向量相关）：")
    vers = sorted(seg_obs)
    zs = {}
    for ver in vers:
        o = seg_obs[ver]
        e = o.sum() / NRED
        zs[ver] = (o - e) / math.sqrt(e)
    for i in range(len(vers)):
        for j in range(i + 1, len(vers)):
            r = float(np.corrcoef(zs[vers[i]], zs[vers[j]])[0, 1])
            print(f"   {vers[i]:<16} vs {vers[j]:<16} ρ={r:+.3f}")
    print()

    # ---- 按年份
    by_year = defaultdict(list)
    for rec in seq:
        by_year[str(rec.期号)[:4] if len(str(rec.期号)) > 4 else "早期"].append(rec)
    print("按年份（看偏离是否贯穿全程）：")
    print(f"   {'年':<7}{'期数':>6}{'χ²':>9}{'df':>5}{'p':>10}")
    for y in sorted(by_year):
        recs = by_year[y]
        c3 = Counter()
        for rec in recs:
            for x in rec.红球:
                c3[int(x)] += 1
        o = np.array([c3.get(i + 1, 0) for i in range(NRED)], dtype=float)
        c, df, p = _chi2(o)
        flag = "  ←p<0.05" if p < 0.05 else ""
        print(f"   {y:<8}{len(recs):>6}{c:>9.2f}{df:>5}{p:>10.4f}{flag}")
    print()
    print("判读：若 χ² 只在个别年份爆表 ⇒ 数据/规则问题（A/B）；")
    print("      若各年份都轻微同向 ⇒ 真实偏离（C），但需独立样本复核。")

    out = r"E:\707\output\stage0_uniformity_diag.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"完整性": {"个数异常": len(bad_len), "越界": len(bad_range),
                              "期内重复": len(bad_dup)},
                   "全量z": {str(i + 1): float(z[i]) for i in range(NRED)},
                   "按版本": {v: seg_obs[v].tolist() for v in vers}},
                  f, ensure_ascii=False, indent=1)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
