# -*- coding: utf-8 -*-
"""combo/conformal.py —— P3-2 分箱共形（split conformal）：给"买多少注"一个带保证的说法

回答（人话）："买 K 注，下期至少 1 票命中目标的概率有 X% 保证吗？"
  不预测号码，只给**集合级覆盖率保证**，且保证是有限样本、无分布假设的
  （仅需校准期与未来可交换——彩票天然满足：机制未变时逐期独立同分布）。

为什么是共形而不是"直接看历史命中率"：
  历史命中率 k/n 是点估计，直接当保证会撒谎（抽样误差、机制漂移）。
  split conformal 的 +1 构造给出可验证的误保证率上界：
    声称 "P(下期≥1票命中) ≥ X"  仅在  k_cal ≥ (n_cal+1)·X  时才发出；
    此时 P(发出声称 且 下一期没中) ≤ 1-X（有限样本，无分布假设）。

模块还输出 Clopper-Pearson 95% 单侧置信下界（"命中率至少 p_L"的置信表述），
并用**时间靠后的回放尾段**做"不撒谎"门控：声称的档位必须在从未参与校准的
后续真实开奖上达标，否则判定机制可能已变（与 P2-2 变点检测联动——校准窗口
一律从最近一次检出的机制变点之后开始取）。

适用范围：
  - 双色球 / 大乐透（乐透型，组合规则 + 历史校准有语义）→ 完整共形流程；
  - 排列5 / 福彩3D / 排列3 / 七星彩（数字型，官方固定赔率）→ 单注命中概率
    由规则精确定死，共形无校准对象 → 只给精确公式表（1-(1-p)^K / K·p），
    并注明与机制变更无关（但 七星彩第7位 2020-11 变点后按现代规则）。

⚠️ 诚实红线（本模块不撒谎的三条）：
  1. 共形保证档位 G = k_cal/(n_cal+1) < 1 恒成立——分布自由方法无法证明 100%，
     确定性满覆盖（P3-1）是数学事实，另行标注，不冒充共形结果；
  2. "随机 K 注"的解析对照 π（超几何精算）与校准命中率互相印证——若两者长期
     背离说明历史与均匀假设冲突，表格会如实显示而不调和；
  3. 覆盖设计的"红球地板"不是奖级（双色球 3+0 不中奖）——target=any 的奖级
     语义只用官方判级表（data/feedback._classify_prize / _classify_dlt_prize）。
"""
from __future__ import annotations

import math
import random
from bisect import bisect_right
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

# ---------------------------------------------------------------- 核心：共形档位

def certified_level(k_pass: int, n_cal: int) -> float:
    """split conformal（+1 构造）可声称的最高保证档位 G = k_pass/(n_cal+1)。

    语义：声称 "P(下期≥1票命中) ≥ G" 时，误保证率（声称了却没中）≤ 1-G，
    有限样本成立，无需分布假设。G 恒 < 1：分布自由方法给不出 100% 保证。
    """
    if n_cal <= 0:
        return 0.0
    return k_pass / (n_cal + 1)


def claim_supported(k_pass: int, n_cal: int, x: float) -> bool:
    """声称档位 X 是否被校准支持：k_cal ≥ (n_cal+1)·X（1-X 为误保证率上界）。"""
    return k_pass >= (n_cal + 1) * x


def cp_lower(k_pass: int, n_cal: int, conf: float = 0.95) -> float:
    """Clopper-Pearson 单侧下界：P(真实命中率 ≥ p_L) ≥ conf（精确二项，无近似）。"""
    if k_pass <= 0:
        return 0.0
    if k_pass >= n_cal:
        return (1 - conf) ** (1.0 / n_cal) if n_cal > 0 else 0.0
    from scipy.stats import beta
    return float(beta.ppf(1.0 - conf, k_pass, n_cal - k_pass + 1))


def minimal_certified_K(first_hit: Sequence[int], n_pool: int,
                        x: float) -> Optional[int]:
    """在票池前 K 张（first_hit[i] = 命中该期所需的最小票序，∞=未命中）的
    单调结构下，找满足 k_cal(K) ≥ (n_cal+1)·X 的最小 K（校准集全体为 first_hit）。
    返回 None 表示票池上限内无法保证。
    """
    need = math.ceil((len(first_hit) + 1) * x)
    hits = sorted(v for v in first_hit if v is not None)
    if len(hits) < need:
        return None
    return hits[need - 1] if hits[need - 1] <= n_pool else None


# ---------------------------------------------------------------- 号码命中计算

def _tier_matrix(lottery: str) -> np.ndarray:
    """[红命中数+1, 蓝命中数+1] 的"是否中奖(≥5元级)"布尔表（用官方判级）。"""
    from data.feedback import _classify_dlt_prize, _classify_prize
    if lottery == "双色球":
        cls, kr, kb = _classify_prize, 7, 2          # 红0..6 蓝0..1
    else:
        cls, kr, kb = _classify_dlt_prize, 6, 3      # 大乐透 红0..5 蓝0..2
    m = np.zeros((kr, kb), dtype=bool)
    for rh in range(kr):
        for bh in range(kb):
            m[rh, bh] = cls(rh, bh) != "未中"
    return m


def _masks(zone: Optional["Zone"], nums: Sequence[int]) -> int:
    """号码集合 → 位掩码（位偏移 = 号 - zone.min）。zone=None 返回 0。"""
    if zone is None or not nums:
        return 0
    m = 0
    for x in nums:
        m |= 1 << (x - zone.min)
    return m


def _record_redblue(rec, schema) -> Tuple[int, int]:
    """单期红/蓝位掩码。无蓝区（数字型）蓝掩码=0。"""
    rz = schema.red_zone
    dm_r = _masks(rz, rec.zone_numbers.get("红球", []))
    bz = schema.blue_zone
    dm_b = _masks(bz, rec.zone_numbers.get("蓝球", [])) if bz else 0
    return dm_r, dm_b


def _ticket_hit_bool(dm_r: int, dm_b: int, tm_r: int, tm_b: int,
                     target: str, prize_bool: np.ndarray, t: int) -> bool:
    """单票对单期是否命中目标。target: any=官方中奖(≥5元级)；red2/red3=红球地板。"""
    rh = (dm_r & tm_r).bit_count()
    if target == "any":
        bh = (dm_b & tm_b).bit_count()
        return bool(prize_bool[rh, bh])
    return rh >= t


def _first_hit_indices(records, pool, target, prize_bool, t, schema,
                       red_zone, blue_zone) -> List[Optional[int]]:
    """每期记录 → '命中最快票序'（1-based，票池有序；整期未命中=None）。

    单调结构保证：k_cal(K)=#{期: 序≤K} 随 K 单调不减 → minimal K 可精确求。
    """
    tms = [(_masks(red_zone, tr), _masks(blue_zone, tb)) for tr, tb in pool]
    n_t = len(tms)
    out: List[Optional[int]] = []
    for rec in records:
        dm_r, dm_b = _record_redblue(rec, schema)
        hit = None
        for j in range(n_t):
            tm_r, tm_b = tms[j]
            if _ticket_hit_bool(dm_r, dm_b, tm_r, tm_b, target, prize_bool, t):
                hit = j + 1
                break
        out.append(hit)
    return out


# ---------------------------------------------------------------- 策略票池

def _random_pool(lottery: str, kmax: int, seed: int):
    """kmax 张互异均匀随机票（去重，无放回）；策略取前 K 张 → K 单调语义。
    返回 [(红号tuple, 蓝号tuple or None)]，号码 1-based。"""
    rng = random.Random(seed)
    schema = _schema(lottery)
    rz, bz = schema.red_zone, schema.blue_zone
    seen, pool = set(), []
    while len(pool) < kmax:
        tr = tuple(sorted(rng.sample(range(rz.min, rz.max + 1), rz.choose)))
        if bz:
            tb = tuple(sorted(rng.sample(range(bz.min, bz.max + 1), bz.choose)))
        else:
            tb = None
        key = (tr, tb)
        if key not in seen:
            seen.add(key)
            pool.append(key)
    return pool


def _cover_pool(lottery: str, t: int, seed: int = 0):
    """P3-1 覆盖设计满覆盖红区票（红号 tuple；蓝=None——红球地板目标专用）。"""
    from combo.covering import covering_tickets
    schema = _schema(lottery)
    rz = schema.red_zone
    rep = covering_tickets(rz.max - rz.min + 1, rz.choose, t, seed=seed)
    return [(tuple(sorted(x + rz.min for x in tk)), None)
            for tk in rep["tickets"]]


def _schema(lottery: str):
    from data.schema import get_schema
    return get_schema(lottery)


# ---------------------------------------------------------------- 现代段与切窗

def _modern_records(lottery: str, records) -> Tuple[List, dict]:
    """P2-2 变点联动：校准只用最近一次检出变点之后的"现代段"。
    返回 (升序现代段, 说明 dict)。无检出 → 全历史。"""
    recs = sorted(records, key=lambda r: r.期号)
    try:
        import json, os
        cp = json.load(open(os.path.join(os.path.dirname(__file__),
                                         "..", "discovery", "changepoints.json"),
                            encoding="utf-8"))
    except Exception:
        cp = {}
    info = {"检出": [], "说明": "变点文件缺失→按全历史校准"}
    det = (cp.get(lottery) or {}).get("检出") or []
    if not det:
        info = {"检出": [], "说明": "P2-2 变点检测无检出→机制平稳，用全历史"}
        return recs, info
    last = max(det, key=lambda d: d.get("期号", 0))
    th = int(last.get("期号", 0))
    modern = [r for r in recs if r.期号 > th]
    info = {"检出": det,
            "排除截止": f"期号 ≤ {th}（{last.get('日期')}）之前的段已排除",
            "说明": f"校准仅用 {last.get('日期')} 之后现代段 {len(modern)} 期"}
    return (modern if modern else recs), info


# ---------------------------------------------------------------- 主入口 plan

# 数字型：官方固定赔率，单注直选命中概率精确定死（共形无校准对象）
_DIGITAL_DIRECT_P = {
    "福彩3D": 1 / 1000, "排列3": 1 / 1000,
    "排列5": 1 / 100000,
    "七星彩": 1 / (10 ** 6 * 15),     # 前6位0-9 + 第7位0-14（2020-11 后现代规则）
}

_X_GRID = [0.5, 0.8, 0.9, 0.95, 0.99]


def plan(lottery: str, target: str = "any", mode: str = "random",
         K_grid: Optional[List[int]] = None, cal_frac: float = 0.7,
         conf: float = 0.95, seed: int = 0) -> dict:
    """输出"预算 K 注 → 覆盖率保证"对照表（含校准/回放/不撒谎门控）。"""
    from data.loader import load_lottery
    schema = _schema(lottery)

    if lottery in _DIGITAL_DIRECT_P:
        return _digital_plan(lottery)

    if lottery not in ("双色球", "大乐透"):
        return {"applicable": False, "彩种": lottery,
                "reason": f"未知彩种 {lottery}"}

    if mode.startswith("cover") and target == "any":
        return {"applicable": False, "彩种": lottery, "mode": mode,
                "reason": "覆盖设计只保证红球地板（蓝球分配未参与优化），"
                          "target=any（官方奖级）请用 mode=random"}

    t = int(mode[-1]) if mode.startswith("cover") else 0
    prize_bool = _tier_matrix(lottery)
    rz, bz = schema.red_zone, schema.blue_zone

    ld = load_lottery(lottery)
    modern, cp_info = _modern_records(lottery, ld.records)
    n_mod = len(modern)
    if n_mod < 50:
        return {"applicable": False, "彩种": lottery,
                "reason": f"现代段仅 {n_mod} 期，不足以做可靠校准"}

    n_cal = max(10, int(n_mod * cal_frac))
    cal_recs = modern[:n_cal]
    tail_recs = modern[n_cal:]
    n_tail = len(tail_recs)

    if mode == "random":
        kmax = max(K_grid or [1, 2, 5, 10, 20, 30, 50, 75, 100,
                              150, 200, 300])
        pool = _random_pool(lottery, kmax, seed)
        grid = sorted(set(K_grid or [1, 2, 5, 10, 20, 30, 50, 75, 100,
                                     150, 200, 300]))
        grid = [k for k in grid if k <= kmax]
    else:  # cover_t2 / cover_t3
        pool = _cover_pool(lottery, t, seed=seed)
        grid = [len(pool)]

    # 每期最小命中票序（校准段 + 尾段）
    fh_cal = _first_hit_indices(cal_recs, pool, target, prize_bool, t,
                                schema, rz, bz)
    fh_tail = _first_hit_indices(tail_recs, pool, target, prize_bool, t,
                                 schema, rz, bz)
    sc = sorted(v for v in fh_cal if v is not None)

    # 解析对照（仅 random）：单注命中概率 π（超几何精算），
    # 以及 "前K张随机票≈独立" 下的期望命中率 1-(1-π)^K
    analytic = None
    if mode == "random" and target in ("any",):
        pi = _single_ticket_pi(lottery, target, prize_bool, rz, bz)
        analytic = pi

    rows = []
    full_cov = (mode.startswith("cover") and len(pool) <= 5000)
    for K in grid:
        k = bisect_right(sc, K)          # 校准段：序≤K 的期数（单调）
        g = certified_level(k, n_cal)
        pL = cp_lower(k, n_cal, conf)
        # 回放尾段实测（从未参与校准）
        st = sorted(v for v in fh_tail if v is not None)
        kt = bisect_right(st, K)
        replay = kt / n_tail if n_tail else float("nan")
        claims = {f"{int(x * 100)}%": bool(claim_supported(k, n_cal, x))
                  for x in _X_GRID}
        rows.append({
            "K注数": K,
            "校准命中率": round(k / n_cal, 4),
            "共形保证档": round(g, 4),
            f"CP{int(conf*100)}%下界": round(pL, 4),
            "回放实测": round(replay, 4),
            "声称档位": claims,
            "满覆盖确定性": bool(full_cov and k == n_cal),
        })

    # 最小达标 K（共形档位 ≥ X；仅从票池前K张的单调结构精确求）
    minimal = {}
    for x in (0.8, 0.9, 0.95, 0.99):
        mK = minimal_certified_K(fh_cal, len(pool), x)
        minimal[f"{int(x*100)}%"] = mK

    # 不撒谎门控：声称 ≥90/95% 的档位，回放实测必须达标（尾段二项噪声 ~±2%）
    gate = {}
    tol = 0.02
    for row in rows:
        for tag, fired in row["声称档位"].items():
            x = int(tag.rstrip("%")) / 100
            if fired and x >= 0.9:
                key = f"K={row['K注数']} {tag}"
                gate[key] = ("✅ 回放达标" if row["回放实测"] >= x - tol
                             else "❌ 撒谎检测失败：机制可能已变（P2-2 联动）")

    # 确定性满覆盖单独标注：数学事实 100%，非共形结果（G 恒 <1 是方法上限）
    det_note = None
    if mode.startswith("cover"):
        det_note = (f"覆盖设计 t={t}：{'满覆盖' if full_cov else '部分覆盖'}。"
                    f"满覆盖时任意开奖 ≥1 票红≥{t} 是组合数学事实（100%），"
                    f"共形档位上限 n/(n+1) 只是分布自由方法的表述天花板。")
        if lottery == "双色球":
            det_note += (f" ⚠️ 双色球 红≥{t} 地板≠奖级："
                         f"{'2+0' if t==2 else '3+0'} 均不中奖；"
                         f"要保底 5 元(六等) 需红地板与蓝命中联合覆盖"
                         f"（注数≈红覆盖×16）。")
        elif lottery == "大乐透" and t == 2:
            det_note += (" ⚠️ 大乐透 红≥2 地板≠奖级：2+0 未中；"
                         "九等(5元)需 2+1/1+2/0+2 之一，红地板需与后区联合。")
        elif lottery == "大乐透":  # t == 3
            det_note += (" 大乐透 红≥3 地板对应九等 3+0（5元，需后区0命中）"
                         "——乐透型中少见的『红地板=最低奖』情形。")

    note = (f"共形保证档 G = 校准命中数/(校准期数+1)：声称『下期至少 1 票命中"
            f"≥G』时误保证率 ≤ 1-G，有限样本、无分布假设（仅需校准期与未来"
            f"可交换）。G 恒 <1——分布自由方法无法证明 100%。")
    if lottery == "双色球" and target in ("red3",):
        note += " ⚠️ 双色球 3+0 不是奖级：red3 只是红区命中地板。"
    if lottery == "大乐透" and target in ("red3",):
        note += " ⚠️ 大乐透 3+0 属九等奖(5元)：red3 地板可直接对应最低奖。"
    if mode == "random" and analytic is not None:
        note += (f" 解析对照：随机单注中奖概率 π={analytic:.5f}（超几何精算）。"
                 f"⚠️ 勿用 1-(1-π)^K 近似多注曲线：K 注互异去重后，蓝号命中事件"
                 f"在票间互斥（K≤蓝号数时曲线≈K·P(蓝匹配) 近线性），独立近似会"
                 f"低估——这正是由共形校准直接给出真值的原因。")
    if mode == "random" and target.startswith("red"):
        note += (f" ⚠️ red{target[-1]} 是红球命中地板非奖级；"
                 f"red{target[-1]} 目标下随机单注解析 π 见 ev/rulebook。")

    return {
        "applicable": True, "彩种": lottery, "target": target, "mode": mode,
        "现代段": {"期数": n_mod, "校准期": n_cal, "回放期": n_tail,
                  "首期": modern[0].期号, "末期": modern[-1].期号,
                  **cp_info},
        "票池": {"生成": "随机无放回前K张" if mode == "random"
                 else f"覆盖设计C(v,k,{t})满覆盖", "票数": len(pool)},
        "对照表": rows,
        "最小达标K": minimal,
        "不撒谎门控": gate or "无 ≥90% 声称档位需门控",
        "确定性说明": det_note,
        "诚实说明": note,
    }


def _single_ticket_pi(lottery: str, target: str, prize_bool: np.ndarray,
                      rz, bz) -> float:
    """随机单注命中 target 的精确概率（超几何，票-期对称 → 对任意一期恒等）。
    target=any 用官方判级表；redN 走红区超几何（N 由 prize 表外另行给——仅 any）。"""
    v, k = rz.max - rz.min + 1, rz.choose
    bv = bz.max - bz.min + 1
    bk = bz.choose
    total = 0.0
    for rh in range(k + 1):
        w_red = math.comb(k, rh) * math.comb(v - k, k - rh) / math.comb(v, k)
        for bh in range(bk + 1):
            if not prize_bool[rh, bh]:
                continue
            w_blue = (math.comb(bk, bh) * math.comb(bv - bk, bk - bh)
                      / math.comb(bv, bk))
            total += w_red * w_blue
    return total


def _digital_plan(lottery: str) -> dict:
    """数字型：官方固定赔率 → 命中概率精确定死，无共形校准对象。
    直选全组合互斥 → K 张互异票单期命中率 = K·p（K ≤ 全组合数，封顶 1）。"""
    p = _DIGITAL_DIRECT_P[lottery]
    cap = int(round(1 / p))   # round 防 1e-5 类浮点 1/p=99999.999… 截断
    grid = [1, 2, 5, 10, 20, 50, 100, 200, 500, cap]
    rows = []
    for K in grid:
        if K >= cap:
            pr, note = 1.0, "K≥全组合数 → 直选全覆盖，确定性 100%（非共形）"
        else:
            pr = round(K * p, 6)
            note = None
        rows.append({"K注数": K, "直选命中率(精确)": pr,
                     "解析式": f"{K}×{p:.3g}" if note is None else note})
    return {
        "applicable": True, "彩种": lottery, "target": "直选(全对)",
        "mode": "exact-固定赔率",
        "说明": ("数字型单注命中概率由官方固定赔率精确定死（排3/3D 直选 1/1000、"
                 "排列5 1/100000、七星彩 1/15,000,000 现代规则），"
                 "历史数据无校准对象 → 共形无意义，直接给精确公式表。"
                 "七星彩按 2020-11 变点后现代规则（第7位0-14）。"),
        "对照表": rows,
        "诚实说明": ("保证仅对『机制未变』的未来有效；数字型开奖号本身仍逐期"
                     "独立，命中概率表是规则精算值，不承诺任何一期必中。"),
    }
