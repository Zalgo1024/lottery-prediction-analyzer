# -*- coding: utf-8 -*-
"""ev/profit/outcomes.py —— 结局枚举与命中直方图（性能核心）。

为什么这样做
------------
`Σ_ω` 的 ω 是**完整开奖结局**：双色球 1772 万、大乐透 2142 万、七星彩 1500 万。
逐注去比较（M×K）会爆内存也会爆时间（实测朴素广播 19.7 s/块）。

关键观察：奖级只取决于 **(外层命中数 h, 内层命中类 j)**，而 h 可以由
「外层存在向量 / 逐位 one-hot」的一次矩阵乘得到：

    hit(M, K) = E_outer(M, d) @ E_ticket(K, d)^T        # 双色球 d=33，数字型 d=位宽×10

于是每块只需建一次直方图 `H[M, h] = #{c : 命中 h 个}`，之后每期每 κ 只做 7 次点积。
实测（双色球，K=200）：建 H 约 1.6 s/块，之后每期毫秒级。
"""
from __future__ import annotations

from functools import lru_cache
from itertools import combinations, product
from typing import Dict, List, Sequence, Tuple

import numpy as np

from ev.profit.rules import LotteryRules, rules_for

CHUNK = 200_000


# ---------------------------------------------------------------- 枚举

@lru_cache(maxsize=8)
def outer_universe(lottery: str) -> np.ndarray:
    """外层全部结局，形状 (M_outer, width)，int16。

    · 双/大（组合型）：全部 C(n, w) 个升序组合
    · 七星彩/数字型（排列型）：全部 n^w 个位串
    结果按固定顺序缓存（同一彩种只枚举一次）。
    """
    r = rules_for(lottery)
    lo, hi = r.outer_range
    w = r.outer_width
    if r.outer_choose:
        return np.array(list(combinations(range(lo, hi + 1), w)), dtype=np.int16)
    # 排列型：全部 n^w 个位串，形状必须是 (n^w, w) —— 曾误建 1 维导致 IndexError
    base = hi - lo + 1
    out = np.empty((base ** w, w), dtype=np.int16)
    # 逐位展开（字典序：最高位变化最慢），避免 list(product(...)) 的巨量 Python 对象
    idx = np.arange(out.shape[0], dtype=np.int64)
    for pos in range(w):
        div = base ** (w - 1 - pos)
        out[:, pos] = (idx // div) % base + lo
    return out


# ---------------------------------------------------------------- 编码

def _encode_outer(r: LotteryRules, rows: np.ndarray) -> np.ndarray:
    """外层结局 → 用于「命中数 = 点积」的编码矩阵。

    组合型 → 存在向量（长度 = 取值个数）
    排列型 → 逐位 one-hot（长度 = 位宽 × base）
    """
    rows = np.asarray(rows, dtype=np.int16)
    m = rows.shape[0]
    lo, hi = r.outer_range
    base = hi - lo + 1
    if r.outer_choose:
        E = np.zeros((m, base), dtype=np.uint8)
        E[np.arange(m)[:, None], rows - lo] = 1
        return E
    w = r.outer_width
    E = np.zeros((m, w * base), dtype=np.uint8)
    E[np.arange(m)[:, None], np.arange(w) * base + (rows - lo)] = 1
    return E


@lru_cache(maxsize=8)
def outer_encoding(lottery: str) -> np.ndarray:
    """外层全集的编码矩阵（缓存；双色球 36 MB / 七星彩 60 MB / 大乐透 11 MB）。"""
    return _encode_outer(rules_for(lottery), outer_universe(lottery))


# ---------------------------------------------------------------- 票面 → 数组

def outer_key(r: LotteryRules) -> str:
    """外层在票面 dict 里的键名（单一真源；select 构造票面也从这里取）。"""
    return {"红球": "红球", "前区": "红球", "前6位": "号码", "全部位数": "号码"}[r.outer_label]


def _outer_digits(r: LotteryRules, ticket: dict) -> List[int]:
    """从票面提取**外层位**（按 outer_label 分型）。

    ★ 七星彩的 outer_label="前6位" 而票面 "号码" 是 7 位（第 7 位是内层），
      必须切前 6 位；数字型 "全部位数" 取整串。位数对不对直接决定
      `hit_histogram` 的编码矩阵形状（曾因多取一位而 IndexError）。
    """
    if r.outer_label == "前6位":
        return [int(x) for x in ticket["号码"][: r.outer_width]]
    return [int(x) for x in ticket[outer_key(r)]]


def tickets_to_outer(lottery: str, tickets: Sequence[dict]) -> np.ndarray:
    """把票面列表转成外层矩阵 (K, width)。

    ★ 分型保序：
      · 组合型（outer_choose=True，双/大）：**排序**（组合无序，排序是规范化）
      · 排列型（outer_choose=False，七星彩前6位/数字型）：**保序**（位序即信息；
        排序会把「123…」与「321…」混为一谈，命中数虚高——奖级按同位命中数判）
    """
    r = rules_for(lottery)
    if r.outer_choose:
        rows = [sorted(_outer_digits(r, t)) for t in tickets]
    else:
        rows = [_outer_digits(r, t) for t in tickets]
    return np.asarray(rows, dtype=np.int16)


def tickets_to_inner(lottery: str, tickets: Sequence[dict]) -> np.ndarray:
    """把票面列表转成**内层命中类**（每注一个 int）。

    双色球：蓝球值本身（后面按 == 判定命中）
    大乐透：后区两个数（按交集大小判 j）
    七星彩：第 7 位数字
    数字型：无内层，返回全 0
    """
    r = rules_for(lottery)
    if r.inner_size == 1:
        return np.zeros(len(tickets), dtype=np.int16)
    if r.name == "双色球":
        return np.asarray([int(t["蓝球"][0]) for t in tickets], dtype=np.int16)
    if r.name == "大乐透":
        return np.asarray([list(int(x) for x in t["蓝球"]) for t in tickets], dtype=np.int16)
    if r.name == "七星彩":
        return np.asarray([int(t["号码"][6]) for t in tickets], dtype=np.int16)
    raise ValueError(f"未实现的内层转换: {r.name}")


# ---------------------------------------------------------------- 命中直方图

def hit_histogram(lottery: str, tickets: Sequence[dict],
                  chunk: int = CHUNK) -> np.ndarray:
    """H[ρ, h] = 有多少注票与结局 ρ 的外层命中 h 个。形状 (M_outer, width+1)。

    统计量：外层命中数 h 是唯一进入奖级的量（内层由 `inner_hits` 单独处理）。
    """
    r = rules_for(lottery)
    E_all = outer_encoding(lottery)
    E_tk = _encode_outer(r, tickets_to_outer(lottery, tickets)).T.copy()  # (d, K)
    K = E_tk.shape[1]
    width = r.outer_width
    M = E_all.shape[0]
    H = np.zeros((M, width + 1), dtype=np.int32)

    for s in range(0, M, chunk):
        e = min(s + chunk, M)
        # BLAS 加速：uint8 GEMM → 每行是该结局对 K 注的命中数
        hits = (E_all[s:e].astype(np.float32) @ E_tk.astype(np.float32)).astype(np.int16)
        for h in range(width + 1):
            H[s:e, h] = (hits == h).sum(axis=1)
        del hits
    return H


def inner_multiplicity(r: LotteryRules) -> np.ndarray:
    """内层「命中类 → 该类包含多少个具体状态」的数组（长度 = 类别数）。"""
    jmax = max(r.inner_multiplicity) if r.inner_multiplicity else 0
    m = np.zeros(jmax + 1, dtype=np.float64)
    for j, c in r.inner_multiplicity.items():
        m[j] = c
    return m


def inner_class_prob(r: LotteryRules) -> np.ndarray:
    """内层命中类的边际概率（均匀假设；tilt 不含内层分量，见 tilt.py 说明）。"""
    return inner_multiplicity(r) / float(r.inner_size)
