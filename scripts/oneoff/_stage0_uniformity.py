# -*- coding: utf-8 -*-
"""真实开奖号码的边际均匀性检验 —— 判定 design4 那个「实测Δ − 精确Δ」偏移的性质。

逻辑链
------
1. 在**均匀**开奖上，实测频率 == `exact_union_prob`（已在 _stage0_exact2 对账，±1.3σ）；
2. 在**真实**开奖上，实测Δ − 精确Δ = δ ≠ 0（0.13~1.9pp），且**历史前后半同号**；
3. 分半同号说明 δ 不是"期数不足"的抽样噪声，而是**该批开奖序列的固有偏移**；
4. 但 δ 在未来新期是否复现，取决于真实开奖号码的边际分布是否真的偏离均匀：
   · 若偏离是**稳定**的 ⇒ δ 可复用，效应量要按实测口径预注册；
   · 若偏离只是这 N 期的**抽样波动** ⇒ δ 不可复用，精确Δ 才是未来效应。

本文件做 4 个边际频率卡方检验（双色球红/蓝、大乐透红/蓝），
多重比较用 Bonferroni（α=0.05/4=0.0125）。

诚实边界
--------
不检出偏离 **不等于** 开奖是均匀的：χ² 只检边际频率，不检高阶结构
（配对、序列相关、条件依赖都不在检验内）。结论强度按项目四级标准记为 Ⅱ/Ⅲ。
"""
from __future__ import annotations

import json
import math
import os
import sys
from collections import Counter

import numpy as np

sys.path.insert(0, r"E:\707")

from ev.tier_backtest import load_test_recs

ALPHA = 0.05
N_TEST = 4


def _chi2_sf(x: float, df: int) -> float:
    """χ² 上尾概率（用正则化上不完全 gamma；scipy 不可用时退回正态近似）。"""
    try:
        from scipy.stats import chi2
        return float(chi2.sf(x, df))
    except Exception:                                   # noqa: BLE001
        # Wilson–Hilferty 正态近似（df 较大时足够）
        z = ((x / df) ** (1 / 3) - (1 - 2 / (9 * df))) / math.sqrt(2 / (9 * df))
        return 0.5 * math.erfc(z / math.sqrt(2))


def main() -> None:
    rows = []
    for lot, red_key, blue_key, nred, nblue, kred, kblue in (
            ("双色球", "红球", "蓝球", 33, 16, 6, 1),
            ("大乐透", "红球", "蓝球", 35, 12, 5, 2)):
        seq = load_test_recs(lot)
        n = len(seq)
        for tag, key, m, k in (("红球", red_key, nred, kred),
                               ("蓝球", blue_key, nblue, kblue)):
            cnt = Counter()
            for rec in seq:
                v = getattr(rec, key)
                for x in (list(v) if isinstance(v, (list, tuple)) else [v]):
                    if x is not None:
                        cnt[int(x)] += 1
            obs = np.array([cnt.get(i + 1, 0) for i in range(m)], dtype=float)
            tot = obs.sum()
            exp = tot / m
            chi2 = float(((obs - exp) ** 2 / exp).sum())
            df = m - 1
            p = _chi2_sf(chi2, df)
            # 效应量：相对频率的最大偏离（以期望计数的 σ 为单位）
            sd = math.sqrt(exp * (1 - k / m) * n / n) if False else math.sqrt(exp)
            zmax = float(np.abs(obs - exp).max() / sd)
            rows.append(dict(彩种=lot, 部位=tag, 期数=n, 池大小=m, 每期抽=k,
                             总抽取=int(tot), 期望每号=exp,
                             chi2=chi2, df=df, p=p,
                             Bonferroni临界=ALPHA / N_TEST,
                             检出=bool(p < ALPHA / N_TEST),
                             最大偏离z=zmax,
                             最小号=int(obs.min()), 最大号=int(obs.max())))

    print("=" * 124)
    print(f"{'彩种':<6}{'部位':<6}{'期数':>6}{'池':>4}{'总抽取':>8}{'期望/号':>10}"
          f"{'χ²':>10}{'df':>5}{'p':>10}{'临界':>8}{'检出':>6}{'最大偏离z':>10}"
          f"{'最少':>7}{'最多':>7}")
    print("-" * 124)
    for r in rows:
        print(f"{r['彩种']:<6}{r['部位']:<7}{r['期数']:>6}{r['池大小']:>4}"
              f"{r['总抽取']:>8}{r['期望每号']:>10.1f}{r['chi2']:>10.2f}"
              f"{r['df']:>5}{r['p']:>10.4f}{r['Bonferroni临界']:>8.4f}"
              f"{'是' if r['检出'] else '否':>6}{r['最大偏离z']:>10.2f}"
              f"{r['最小号']:>7}{r['最大号']:>7}")
    print("=" * 124)

    n_sig = sum(1 for r in rows if r["检出"])
    print(f"Bonferroni 校正后检出 {n_sig}/{len(rows)} 个部位偏离均匀"
          f"（α={ALPHA}/{N_TEST}={ALPHA/N_TEST:.4f}）")
    print()
    if n_sig == 0:
        print("⇒ 在本检验的分辨率下**未检出**边际频率偏离。")
        print("  这不等于开奖均匀：χ² 只检边际频率，不检配对/序列相关/条件依赖。")
        print("  design4 的 δ 应视为**这批开奖序列的抽样波动**，"
              "未来新期不保证复用 ⇒ 效应量应以精确Δ 为准，实测Δ 作保守下界。")
    else:
        print("⇒ 检出偏离，需进一步判定是否稳定可复用。")

    out = r"E:\707\output\stage0_uniformity.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(rows, f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}")


if __name__ == "__main__":
    main()
