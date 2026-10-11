# -*- coding: utf-8 -*-
r"""C 轨 · 位级 / 分区结构偏差（**不单独判缝隙**）。

## 它回答什么、不回答什么

回答：**开奖号的形状**在统计上有没有偏离"均匀随机"。
不回答：有没有机制缝隙。

理由：结构偏差的成因太多（规则变更、数据源缺陷、摇奖设备磨损、多重比较假阳性），
单靠它无法把"机制筛选"与"数据伪影"分开。它在本体系里的角色是
**A/A′/B 轨的强化与排查线索**：
  · A/A′/B 都没检出，而 C 检出某处结构偏离 → 优先怀疑数据/规则，不是机制；
  · A/A′/B 检出了，而 C 也指出偏离集中在某个分区 → 才值得深挖。
所以返回里 `gap` **恒为 False**，并由 `_C_NOTE` 明确写死。

## 口径（三处关键约定）

1. **零分布必须是"均匀组合抽样"，不是"理论均匀"**。
   · 数字型某一位：均匀 0..size-1。
   · 乐透型红球形态：均匀 `C(range, choose)` 组合下的**形态分布**——
     不是均匀分布（例：6 个 1..33 的红球，和值本身就近似正态）。
   故一律用蒙特卡洛零分布：χ² 用 `rng.multinomial(total, 均匀p, size=B)`（与
   `credibility/data_quality.py:89-119` 的
   `rng.integers(...)+bincount` 同分布，但**不必展开 total 个观测**，快 2~3 个数量级）；
   形态用 MC 抽组合量出 null 均值/se 再算 z。
2. **七星彩第 7 位只用现行段（≥ 2020-10-11）**。后区由 0-9 扩为 0-14，
   混段会在相邻自相关上造出 p≈1e-10 的假异常（`discovery/randomness_audit.py` P2-1 已定位）。
   ⚠️ 注意：A′ 轨是**整段**切（概率标尺整体换了），C 轨只切第 7 位——
   两者口径不同，不冲突（A′ 判全局注数，C 判分区边际）。
3. **族内 BH + 实质效应门控 + 分段同质闸门**。族 = `(彩种, 特征类)`。
   统计显著在大样本下极易由微小偏差触发（双色球红球累计 2 万次观测），
   必须同时要求效应量实质：位级 Cramér's V ≥ 0.10；红球形态 |z| ≥ 3
   **且分段同质**（前半 vs 后半的差 z < 3）。
   分段闸门不是装饰：实测大乐透全样本 `和值` z=+4.15 看着像实质偏离，
   拆开一看是 2007–2013 段（97.2）与 2014 起（88.5）的分段断裂 —— 数据问题。
4. **红球形态 z 的 MC 精度必须随 n 缩放**（`RED_NULL_MC_PER_DRAW`）。
   MC 零均值自身误差 ⇒ z 的种子噪声 = √(n/mc)；固定 mc 会在数据增长后
   把真值 z≈2.9 顶过门槛而误报（2026-10-11 大乐透「连号对数」实测 3.008）。
   与位级分支的「p 分辨率地板」是同类的自检，缺一不可。
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from ev.mechanism.common import load_draw_frame
from ev.mechanism.sales_grade import QXC_RULE_CUTOFF

MC_B_DEFAULT = 2000
MC_COMBOS_DEFAULT = 200_000
V_MIN = 0.10            # 位级 Cramér's V 实质门槛
Z_MIN = 3.0             # 红球形态 |z| 实质门槛

# 红球形态 z 的 MC 精度闸门（与位级分支的「p 分辨率地板」对称，缺它就会误报）。
# z = (观测均值 − MC 零分布均值)/se，其中 se = null_sd/√n；
# 而 MC 零均值**自身**的抽样误差 = null_sd/√mc
#   ⇒ **z 的 MC 种子噪声 = √(n/mc)**，随数据增长 n↑ 而固定 mc 时噪声↑。
# 实测（2026-10-11）：大乐透 n≈2928、mc=6e4 ⇒ 噪声 = √(2928/6e4) = 0.221，
# 足以把真值 z=2.88（解析期望 0.5714 口径）顶过 |z|≥3 门槛，
# 把「连号对数」误报成「实质结构偏离」。故令 mc ≥ RED_NULL_MC_PER_DRAW × n
#   ⇒ 噪声 ≤ 1/√400 = 0.05，与门槛 3.0 留出余量。
RED_NULL_MC_PER_DRAW = 400      # 每个观测配 400 个 MC 组合
RED_NULL_MC_MAX = 4_000_000     # 内存/耗时上限（n≤4000 时用不到）

_C_NOTE = ("C 轨**不单独判缝隙**：结构偏差可能来自规则变更/数据源缺陷/摇奖设备/"
           "多重比较假阳性，无法与「机制筛选」区分。它只作 A/A′/B 轨的强化与排查线索。")

# 红球形态特征名（顺序与 _red_features 的列一一对应）
RED_FEATURES: Tuple[str, ...] = (
    "和值", "跨度", "连号对数", "奇数个数", "大号个数", "尾数重复", "空三分区数",
)


# ------------------------------------------------------------------ 数字型位级
def _zone_counts(values: np.ndarray, size: int, lo: int) -> np.ndarray:
    """把取值序列归一到 0..size-1 再计数（不归一会出现空 bin 0 制造虚假大 χ²）。"""
    idx = values.astype(np.int64) - int(lo)
    idx = idx[(idx >= 0) & (idx < size)]
    return np.bincount(idx, minlength=size)[:size]


def _mc_chi2_p(obs_chi2: float, total: int, size: int, *, B: int,
               seed: int) -> Tuple[float, float]:
    """
    MC 校准的均匀性 χ² p 值。

    零分布用 `rng.multinomial(total, 均匀, size=B)` 直接抽**计数向量**——
    与逐观测 `rng.integers` + bincount 完全同分布，但代价只与 `B×size` 有关
    （`credibility/data_quality.py` 用的是展开 `B×total` 个观测，慢得多）。
    """
    expected = total / float(size)
    rng = np.random.default_rng(seed)
    sim = rng.multinomial(total, np.full(size, 1.0 / size), size=B).astype(np.float64)
    sim_chi2 = ((sim - expected) ** 2 / expected).sum(axis=1)
    ge = int(np.count_nonzero(sim_chi2 >= obs_chi2))
    p = (1 + ge) / (B + 1.0)
    return float(p), float(sim_chi2.mean())


def _zone_uniformity_tests(lottery: str, schema, *, B: int,
                           seed: int) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """逐分区的均匀性 χ²（MC 校准）+ Cramér's V。乐透型红球跳过（走形态族）。"""
    from data.loader import load_lottery
    data = load_lottery(lottery)
    chron = sorted(data.records, key=lambda r: r.期号 or 0)
    from data.schema import record_zone_numbers

    tests: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {"跳过分区": []}
    for zi, zone in enumerate(schema.zones):
        # 乐透型红球 = 「无序 + choose>1」→ 频率边际意义弱（每期 6 个号强相关），走形态族
        if (not zone.ordered) and getattr(zone, "choose", 1) > 1:
            meta["跳过分区"].append(f"{zone.name}（无序多选 → 走形态族）")
            continue
        base = chron
        seg_note = "全段"
        if lottery == "七星彩" and zone.max == 14:
            base = [r for r in chron if r.开奖日期 and r.开奖日期 >= QXC_RULE_CUTOFF]
            seg_note = f"现行段（≥{QXC_RULE_CUTOFF.isoformat()}）"
        vals: List[int] = []
        for rec in base:
            vals.extend(record_zone_numbers(rec, zone))
        if not vals:
            meta["跳过分区"].append(f"{zone.name}（无数据）")
            continue
        arr = np.asarray(vals)
        size = int(zone.size)
        counts = _zone_counts(arr, size, zone.min)
        total = int(counts.sum())
        if total <= size:
            meta["跳过分区"].append(f"{zone.name}（观测 {total} ≤ 类别 {size}）")
            continue
        exp = total / float(size)
        obs = float(((counts - exp) ** 2 / exp).sum())
        v = float(math.sqrt(obs / (total * (size - 1)))) if size > 1 else 0.0
        p, mc_mean = _mc_chi2_p(obs, total, size, B=B, seed=int(seed) + zi)
        tests.append({
            "族": f"{lottery}|位级",
            "检验": "均匀性χ²(MC)",
            "特征": zone.name,
            "n": total,
            "统计量": round(obs, 3),
            "零分布均值": round(mc_mean, 3),
            "p": round(p, 5),
            "效应量": round(v, 5),
            "效应量名": "CramérV",
            "实质门槛": f"V≥{V_MIN}",
            "段": seg_note,
            "最大相对偏差": round(float(np.max(np.abs(counts - exp) / exp)), 4),
            "明细": (f"size={size}, MC-B={B}, 观测最多号 "
                     f"{int(np.argmax(counts)) + zone.min}({int(counts.max())}次), "
                     f"最少号 {int(np.argmin(counts)) + zone.min}"
                     f"({int(counts.min())}次)"),
        })
    return tests, meta


# ------------------------------------------------------------------ 红球形态
def _red_features(vals: np.ndarray, lo: int, hi: int, choose: int) -> np.ndarray:
    """(n, 7) 形态特征，列序见 `RED_FEATURES`。`vals` 需已按行升序。"""
    s = vals.astype(np.float64)
    m = len(s)
    f = np.empty((m, 7), dtype=np.float64)
    f[:, 0] = s.sum(axis=1)
    f[:, 1] = s[:, -1] - s[:, 0]
    f[:, 2] = (np.diff(s, axis=1) == 1).sum(axis=1)
    f[:, 3] = (np.mod(s, 2) == 1).sum(axis=1)
    f[:, 4] = (s > (lo + hi) / 2.0).sum(axis=1)
    last = np.sort(np.mod(vals.astype(np.int64), 10), axis=1)
    distinct = 1 + (np.diff(last, axis=1) != 0).sum(axis=1) if choose > 1 else np.ones(m)
    f[:, 5] = choose - distinct
    width = (hi - lo + 1) / 3.0
    third = np.clip(((s - lo) / width).astype(np.int64), 0, 2)
    third_sorted = np.sort(third, axis=1)
    nonempty = (1 + (np.diff(third_sorted, axis=1) != 0).sum(axis=1)) if choose > 1 \
        else np.ones(m, dtype=np.int64)
    f[:, 6] = 3 - nonempty
    return f


def _mc_uniform_combos(lo: int, hi: int, choose: int, *, mc: int,
                       seed: int) -> np.ndarray:
    """均匀 C(hi-lo+1, choose) 的 MC 组合（已按行升序），内存分块。"""
    rng = np.random.default_rng(seed)
    span = hi - lo + 1
    out = np.empty((mc, choose), dtype=np.int16)
    done = 0
    while done < mc:
        b = int(min(20_000, mc - done))
        r = rng.random((b, span))
        idx = np.argpartition(r, choose, axis=1)[:, :choose]
        out[done:done + b] = np.sort(idx + lo, axis=1).astype(np.int16)
        done += b
    return out


def _red_shape_tests(lottery: str, *, mc: int, seed: int) -> List[Dict[str, Any]]:
    """
    红球形态 z（零分布 = 均匀 C(range, choose) 的形态分布，MC 抽样）。

    ⚠️ **分段同质闸门**（这是本函数存在的最重要理由之一）：
    2026-10-03 实测发现大乐透 `和值` 的全样本 z=+4.15、`大号个数` z=+4.36，
    看着像"实质结构偏离"。但逐年一查：**2007–2013 段 和值均值 97.2，
    2014 起 88.5，理论值 90.0** —— 是**分段不一致**，不是机制。
    （2014-05-05 起大乐透确实改了奖级/奖金规则，但**号码范围自 2007 上市起一直是
      前区 01-35**，所以这个断裂既不能由号码范围解释、也不能由奖级变更解释，
      属数据分段问题。已记入 `logs/`。）
    所以每个形态特征都同时给出 `分段差_z`（前半 vs 后半），
    **只有 |分段差_z| < 3 的特征才有资格成为"实质偏离"**；
    否则 标 `分段不一致`，只作数据线索。
    """
    from data.schema import get_schema
    schema = get_schema(lottery)
    red = schema.zones[0]
    if red.ordered or getattr(red, "choose", 1) <= 1:
        return []
    fr = load_draw_frame(lottery)
    if fr is None:
        return []
    obs = _red_features(fr.reds, red.min, red.max, int(red.choose))
    n = len(obs)
    # MC 组合数随 n 缩放：保证 z 的 MC 种子噪声 √(n/mc) ≤ 1/√RED_NULL_MC_PER_DRAW
    # （见模块顶部常量注释；固定 mc 会在数据增长后误报「实质偏离」）。
    mc_eff = int(min(RED_NULL_MC_MAX,
                     max(int(mc), RED_NULL_MC_PER_DRAW * max(n, 1))))
    null_vals = _mc_uniform_combos(red.min, red.max, int(red.choose),
                                   mc=mc_eff, seed=seed)
    null_f = _red_features(null_vals, red.min, red.max, int(red.choose))
    h = n // 2
    third = n // 3
    from scipy import stats as sps
    out: List[Dict[str, Any]] = []
    for j, name in enumerate(RED_FEATURES):
        om = float(obs[:, j].mean())
        nm = float(null_f[:, j].mean())
        nsd = float(null_f[:, j].std(ddof=1))
        se = nsd / math.sqrt(n) if nsd > 0 else float("nan")
        z = (om - nm) / se if se == se and se > 0 else float("nan")
        p = float(2 * (1 - sps.norm.cdf(abs(z)))) if z == z else float("nan")
        # 分段同质：前半 vs 后半（独立近似）
        m1, m2 = float(obs[:h, j].mean()), float(obs[h:, j].mean())
        s1 = nsd / math.sqrt(max(h, 1))
        s2 = nsd / math.sqrt(max(n - h, 1))
        dse = math.sqrt(s1 ** 2 + s2 ** 2)
        dz = (m2 - m1) / dse if dse > 0 else float("nan")
        homogeneous = bool(abs(dz) < 3.0) if dz == dz else False
        # 近 1/3 段单独算 z（仅供读者判断"现在的开奖像不像均匀"）
        m3 = float(obs[-third:, j].mean()) if third > 0 else float("nan")
        s3 = nsd / math.sqrt(max(third, 1))
        z3 = (m3 - nm) / s3 if s3 > 0 else float("nan")
        out.append({
            "族": f"{lottery}|红球形态",
            "检验": "形态z(MC均匀组合)",
            "特征": name,
            "n": n,
            "MC组合数": mc_eff,
            "统计量": round(z, 4),
            "观测均值": round(om, 4),
            "零分布均值": round(nm, 4),
            "零分布se": round(se, 6),
            "p": round(p, 5) if p == p else None,
            "效应量": round(abs(om - nm) / nm, 5) if nm else None,
            "效应量名": "相对偏移",
            "实质门槛": f"|z|≥{Z_MIN}",
            "分段": {"前段均值": round(m1, 4), "后段均值": round(m2, 4),
                     "分段差_z": round(dz, 3) if dz == dz else None,
                     "同质": homogeneous},
            "近1/3段": {"n": third, "均值": round(m3, 4) if m3 == m3 else None,
                        "z": round(z3, 3) if z3 == z3 else None},
            "明细": (f"MC 组合数={mc_eff}（≥{RED_NULL_MC_PER_DRAW}×n，"
                     f"z 的 MC 噪声≈{math.sqrt(n / mc_eff):.3f}），null sd={nsd:.4f}"),
        })
    return out


# ------------------------------------------------------------------ 主报告
def structural_bias_report(lottery: str, *, B: int = MC_B_DEFAULT,
                           mc: int = MC_COMBOS_DEFAULT, alpha: float = 0.05,
                           seed: int = 20261003) -> Dict[str, Any]:
    """
    C 轨报告。**永远**返回带 `applicable` 的 dict；`gap` 恒为 False（见 `_C_NOTE`）。
    """
    from data.schema import get_schema
    from credibility.baseline import benjamini_hochberg

    try:
        schema = get_schema(lottery)
    except Exception as e:                                # noqa: BLE001
        return {"lottery": lottery, "applicable": False,
                "note": f"schema 不可用：{e!r}", "gap": False}

    tests, meta = _zone_uniformity_tests(lottery, schema, B=B, seed=seed)
    tests += _red_shape_tests(lottery, mc=mc, seed=seed + 991)
    if not tests:
        return {"lottery": lottery, "applicable": False, "gap": False,
                "note": f"{lottery} 无可做的 C 轨检验", "gap说明": _C_NOTE,
                "排查": meta}

    # 族内 BH
    fams: Dict[str, List[Dict[str, Any]]] = {}
    for t in tests:
        fams.setdefault(t["族"], []).append(t)
    # MC p 的分辨率地板与 BH 最严临界值（供口径披露，见返回里的「口径」）
    p_floor = 1.0 / (float(B) + 1.0)
    m_max = max((len(v) for v in fams.values()), default=1)
    bh_crit_strictest = alpha / max(1, m_max)
    fam_summary: Dict[str, Any] = {}
    for fam, items in fams.items():
        bh = benjamini_hochberg([t["p"] for t in items], alpha=alpha)
        for t, q, rej in zip(items, bh["qvals"], bh["rejected"]):
            t["q(FDR)"] = round(float(q), 5)
            t["统计显著"] = bool(rej)
            if t["效应量名"] == "CramérV":
                t["实质显著"] = bool(rej and (t["效应量"] or 0) >= V_MIN)
            else:
                # 红球形态必须**同时**过分段同质闸门，否则只算数据线索
                homo = bool((t.get("分段") or {}).get("同质", False))
                t["实质显著"] = bool(rej and abs(t["统计量"] or 0) >= Z_MIN
                                     and homo)
                t["分段不一致"] = bool(rej and abs(t["统计量"] or 0) >= Z_MIN
                                       and not homo)
        fam_summary[fam] = {
            "m": len(items),
            "统计显著": sum(1 for t in items if t["统计显著"]),
            "实质显著": sum(1 for t in items if t["实质显著"]),
            "分段不一致": sum(1 for t in items if t.get("分段不一致")),
        }

    practical = [t for t in tests if t["实质显著"]]
    inhomog = [t for t in tests if t.get("分段不一致")]
    only_stat = [t for t in tests if t["统计显著"] and not t["实质显著"]
                 and not t.get("分段不一致")]
    if practical:
        note = ("存在**实质结构偏离**：" +
                "、".join(f"{t['族']}/{t['特征']}" for t in practical[:6]) +
                ("…" if len(practical) > 6 else "") +
                "。⚠️ C 轨不能区分「机制筛选」与「规则变更/数据伪影/设备」，"
                "请先排查数据与规则，再谈机制。")
    elif inhomog:
        note = ("**全样本显著但分段不一致**：" +
                "、".join(f"{t['特征']}(前 {t['分段']['前段均值']} → 后 "
                          f"{t['分段']['后段均值']}, z={t['分段']['分段差_z']})"
                          for t in inhomog[:4]) +
                f" → 判为**数据/规则分段问题**，不是机制。")
    elif only_stat:
        note = (f"有 {len(only_stat)} 项统计显著但效应量未达实质门槛"
                f"（位级 V≥{V_MIN} / 形态 |z|≥{Z_MIN}）→ 视为大样本下的抽样噪声。")
    else:
        note = "未发现任何实质结构偏离，全部检验与「均匀随机」相容。"

    # 红球形态 z 的 MC 精度披露（与位级分支的「p 分辨率地板」对称）：
    # 噪声 = √(n/mc)。mc 随 n 缩放后应 ≤ 0.05；一旦被 RED_NULL_MC_MAX 截顶或
    # 有人调小 mc，这里会显式翻 False 说明本族判定可能被 MC 噪声污染。
    _shape = [t for t in tests if t["族"].endswith("红球形态")]
    _mc_red = _shape[0].get("MC组合数") if _shape else None
    _z_noise = (round(math.sqrt(_shape[0]["n"] / _mc_red), 5)
                if _shape and _mc_red else None)

    return {
        "lottery": lottery,
        "applicable": True,
        "n检验": len(tests),
        "族": fam_summary,
        "检验": tests,
        "实质偏离": [{"族": t["族"], "特征": t["特征"], "统计量": t["统计量"],
                      "效应量": t["效应量"], "q": t["q(FDR)"]} for t in practical],
        "分段不一致": [{"族": t["族"], "特征": t["特征"], "统计量": t["统计量"],
                        "分段": t["分段"]} for t in inhomog],
        "统计显著但效应微小": [{"族": t["族"], "特征": t["特征"], "q": t["q(FDR)"]}
                              for t in only_stat],
        "gap": False,
        "gap说明": _C_NOTE,
        "结论": note,
        "排查": meta,
        "口径": {
            "零分布": "MC（数字型用 rng.multinomial 直接抽计数；红球形态用均匀 C(range,choose) 抽样）",
            "MC-B": B, "MC组合数": mc, "alpha": alpha,
            "位级实质门槛": f"Cramér's V ≥ {V_MIN}",
            "形态实质门槛": f"|z| ≥ {Z_MIN} **且** 分段同质（|前半-后半| 的 z < 3）",
            "七星彩第7位段": ("仅现行段 ≥ " + QXC_RULE_CUTOFF.isoformat()
                              if lottery == "七星彩" else None),
            "红球形态零分布": "均匀组合下的形态分布（**不是**均匀分布）",
            "分段同质闸门": ("红球形态必须过：全样本显著但前半/后半段均值断裂的，"
                             "一律判为数据/规则分段问题、不参与判定"
                             "（触发这次闸门的实测见「排查」与历史记录）"),
            # —— MC p 的分辨率披露 ——
            # p = (1+ge)/(B+1) 有下限 1/(B+1)。低于它就无法与更大的 B 区分。
            # 默认 B=2000 ⇒ 地板 5.0e-4，而族内 BH 最严临界值 = alpha/m_max
            # （m≈3~8 ⇒ 6.3e-3 ~ 1.7e-2），地板比它低一个数量级以上 ⇒
            # **对任何判定无影响**，所以不加大 B（会拖慢 5 倍）。
            # 一旦数据更新导致 m 变大或 B 变小，这里会显式翻成 False 报警。
            "p分辨率地板": round(p_floor, 10),
            "族内最大检验数": m_max,
            "BH最严临界值": round(bh_crit_strictest, 10),
            "地板足够": bool(p_floor < bh_crit_strictest),
            # —— 红球形态 z 的 MC 精度披露 ——
            # z 的 MC 种子噪声 = √(n/mc)。mc 随 n 缩放（≥400×n）后噪声 ≤0.05，
            # 远低于门槛 3.0 ⇒ 对判定无影响。被 RED_NULL_MC_MAX 截顶时会翻 False。
            "红球形态MC规则": (f"MC 组合数 = max(mc, {RED_NULL_MC_PER_DRAW}×n)，"
                               f"使 z 的 MC 噪声 ≤ "
                               f"{1.0 / math.sqrt(RED_NULL_MC_PER_DRAW):.3f}"),
            "形态z的MC噪声": _z_noise,
            "形态z地板足够": bool(_z_noise is not None and _z_noise < 0.06),
        },
    }


__all__ = ["structural_bias_report", "RED_FEATURES", "MC_B_DEFAULT",
           "MC_COMBOS_DEFAULT", "V_MIN", "Z_MIN"]
