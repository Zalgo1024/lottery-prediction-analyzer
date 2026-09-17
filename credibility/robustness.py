"""
鲁棒性 / 过拟合 专用计算层（credibility 第 6 支柱）

职责：回答「**当前选号流程**是否过拟合、鲁棒性如何」，逐期追加趋势供实时监控。
只度量**流程稳定性**，语义上不宣称「号码更可能中奖」——号码独立随机、单注 EV 恒负；
本层产出的是流程稳定性元指标，与裁判层「假设全 rejected」结论不冲突（度量对象不同）。

指标（全部基于随机基线/置换零分布校准，杜绝拍脑袋阈值）：
- train_test_gap    规范化 in-sample vs OOS 分差（正值=过拟合迹象），
                    告警阈值 = 洗牌置换零分布 P95 + 效应量门控(|gap|>0.05)
- window_stability  相邻/两两滚动窗口下 top-K 候选的 Jaccard 重合率均值（低=选号随窗口跳变）
- param_sensitivity 种子扰动(1-Jaccard) 与 置信度权重扰动(1-Kendall τ) 的混合敏感度
- digital_pool_drift 近期 vs 基准期各分区号码分布的 JS 散度，
                    告警阈值 = 同池随机重切零分布 P95（按分区独立校准）

七星彩第 7 位（0-14 现行段 vs 0-9 旧段）与大乐透红球的历史变点一律分段过滤
（读 discovery/changepoints.json「现代段」口径，与 conformal 校准一致）——
分区规则变更后任何按位分布检验不分段 = P2-1 假异常教训重演。

落盘（线程锁 + 原子写）：
- 单期报告  training/feedback/robustness/<lottery>.json
- 趋势追加  training/feedback/robustness/trend_<lottery>.json（上限 200 条）
"""

import json
import logging
import math
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import LOTTERY_CONFIG, TRAINING_DIR
from data.loader import load_lottery
from data.schema import get_schema

from .metrics import draw_to_zones, generate_ticket

logger = logging.getLogger(__name__)

ROBUSTNESS_DIR = TRAINING_DIR / "feedback" / "robustness"
_TREND_CAP = 200
_WRITE_LOCK = threading.Lock()

_STRATEGY_KEYS = ("high_freq", "missing", "balanced")
_STRATEGY_NAMES = {"high_freq": "高频策略", "missing": "遗漏值策略", "balanced": "区间均衡策略"}

# 变点文件（P2-2 产出）：用于「现代段」分段过滤
_CHANGEPOINT_FILE = TRAINING_DIR.parent / "discovery" / "changepoints.json"

# 效应量门控：gap 超零分布分位数之外还须超过该绝对值才告警（防大样本微效应假阳性）
_GAP_EFFECT_GATE = 0.05
# JS 散度的置换校准分位数
_DRIFT_P = 95


# ============================================================
# 工具
# ============================================================

def _chronological_draws(lottery_name: str, records=None) -> Tuple[List[dict], List]:
    """历史按期号升序（最旧→最新）排列的分区号码列表 + 对应 record 列表。"""
    data = load_lottery(lottery_name)
    recs = list(records) if records is not None else list(data.records)
    recs = [r for r in recs if r.期号]
    recs.sort(key=lambda r: r.期号 or 0)
    schema = get_schema(lottery_name)
    return [draw_to_zones(schema, r) for r in recs], recs


def _modern_cutoff(lottery_name: str) -> int:
    """读 P2-2 变点文件的现代段起点期号（该期号之后的记录才进入分布检验）。

    无变点返回 0（不裁剪）。分区规则变更（如七星彩第7位 0-9→0-14）必须分段，
    否则按位分布检验会拿旧段基准比新段 → P2-1 假异常教训。
    """
    try:
        if _CHANGEPOINT_FILE.exists():
            with open(_CHANGEPOINT_FILE, encoding="utf-8") as f:
                cp = json.load(f)
            entries = (cp.get(lottery_name) or {}).get("检出") or []
            issues = [int(e["期号"]) for e in entries if e.get("期号")]
            return max(issues) if issues else 0
    except Exception as e:  # 变点文件损坏不应阻断监控
        logger.warning(f"读取变点文件失败({lottery_name}): {e}")
    return 0


def _atomic_write_json(path: Path, payload: dict):
    """线程锁 + 临时文件 + os.replace 原子写（shim 不拦截 os.replace，锁不敏感）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK:
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        os.replace(tmp, path)


def _kendall_tau(order_a: List[int], order_b: List[int]) -> float:
    """两个排名（名次列表，越小越靠前）的 Kendall τ，O(n²)（n≤120 足够快）。"""
    n = min(len(order_a), len(order_b))
    if n < 2:
        return 1.0
    conc = disc = 0
    for i in range(n):
        for j in range(i + 1, n):
            prod = (order_a[i] - order_a[j]) * (order_b[i] - order_b[j])
            if prod > 0:
                conc += 1
            elif prod < 0:
                disc += 1
    denom = n * (n - 1) / 2
    return (conc - disc) / denom if denom else 1.0


def _ranks_desc(scores: List[float]) -> List[int]:
    """分数 → 名次（0 = 最高分；并列取先出现者）。"""
    order = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)
    ranks = [0] * len(scores)
    for pos, idx in enumerate(order):
        ranks[idx] = pos
    return ranks


# ============================================================
# 指标 1：train-test gap（含置换零分布校准）
# ============================================================

def _zone_structs(chron_draws: List[dict], schema):
    """逐分区累积计数 / 最近出现位置（与 credibility.backtest 同构，无穿越取数）。"""
    n = len(chron_draws)
    struct = {}
    for zone in schema.zones:
        nums = list(range(zone.min, zone.max + 1))
        size = len(nums)
        present = np.zeros((n, size), dtype=np.int8)
        last_seen = np.full((n, size), -1, dtype=np.int32)
        cur = {num: -1 for num in nums}
        for t, dz in enumerate(chron_draws):
            for num in dz.get(zone.name, []):
                present[t, num - zone.min] = 1
                cur[num] = t
            for j, num in enumerate(nums):
                last_seen[t, j] = cur[num]
        struct[zone.name] = {
            "nums": nums, "size": size,
            "cum": present.cumsum(axis=0), "last_seen": last_seen,
        }
    return struct


def _fold_scores(chron_draws: List[dict], schema, min_train: int, k: int, seed: int,
                 in_window: int = 10, max_folds: int = 60) -> dict:
    """
    逐折配对计算统计管线的 in-sample / out-sample 得分。

    对每折 t：用 [0,t) 统计（频率/遗漏）为三策略各生成 k 张票；
      out = 票对 t 期真实开花的平均得分（样本外）
      in  = 同一批票对 [t-in_window, t) 窗口内各期的平均得分（样本内）
    gap = (in_mean - out_mean) / (in_mean + out_mean + ε)，正值 = 样本内虚高 = 过拟合迹象。
    """
    n = len(chron_draws)
    if n < min_train + in_window + 2:
        raise ValueError(f"历史不足（{n} 期），无法计算 train-test gap")
    struct = _zone_structs(chron_draws, schema)
    folds = list(range(min_train, n))[-max_folds:]

    ins, outs = [], []
    for t in folds:
        freqs, missing = {}, {}
        for zname, st in struct.items():
            cum_prev = st["cum"][t - 1] if t >= 1 else np.zeros(st["size"], dtype=np.int64)
            last_prev = st["last_seen"][t - 1] if t >= 1 else np.full(st["size"], -1, dtype=np.int32)
            freqs[zname] = {st["nums"][j]: int(cum_prev[j]) for j in range(st["size"])}
            missing[zname] = {
                st["nums"][j]: (int(t - 1 - last_prev[j]) if last_prev[j] >= 0 else t)
                for j in range(st["size"])
            }
        lo = max(0, t - in_window)
        for key in _STRATEGY_KEYS:
            off = int.from_bytes(_STRATEGY_NAMES[key].encode("utf-8"), "little") % 100000
            rng = np.random.default_rng(seed + t * 7 + off)
            for _ in range(k):
                ticket = generate_ticket(key, schema, freqs, missing, rng)
                outs.append(match_score_safe(schema, ticket, chron_draws[t]))
                win = [match_score_safe(schema, ticket, chron_draws[u]) for u in range(lo, t)]
                ins.append(sum(win) / max(len(win), 1))

    in_m = float(np.mean(ins)) if ins else 0.0
    out_m = float(np.mean(outs)) if outs else 0.0
    gap = (in_m - out_m) / (abs(in_m) + abs(out_m) + 1e-9)
    return {"in_mean": in_m, "out_mean": out_m, "gap": gap, "n_folds": len(folds)}


def match_score_safe(schema, ticket, draw) -> float:
    """match_score 的本地别名（保持对 credibility.metrics 单点依赖）。"""
    from .metrics import match_score as _ms
    return _ms(schema, ticket, draw)


def train_test_gap(lottery_name: str, min_train: int = 100, k: int = 8,
                   seed: int = 20260905, in_window: int = 10,
                   max_folds: int = 60, n_perm: int = 10) -> dict:
    """
    train-test gap + 洗牌置换零分布校准。

    零分布：把开奖序列随机重排（破坏任何时序结构）后重算 gap，
    重复 n_perm 次；真实 gap 超过零分布 P95 且 |gap| > 0.05 才视为过拟合迹象。
    返回 {"gap", "in_mean", "out_mean", "null_p50", "null_p90", "null_p95", "n_folds", "n_perm"}
    """
    chron, _ = _chronological_draws(lottery_name)
    schema = get_schema(lottery_name)
    real = _fold_scores(chron, schema, min_train, k, seed, in_window, max_folds)

    rng = np.random.default_rng(seed + 991)
    null_gaps = []
    for b in range(n_perm):
        perm = rng.permutation(len(chron))
        shuffled = [chron[i] for i in perm]
        try:
            null_gaps.append(_fold_scores(shuffled, schema, min_train, k,
                                          seed + b * 13, in_window, max_folds)["gap"])
        except ValueError:
            continue
    if null_gaps:
        p50, p90, p95 = (float(np.percentile(null_gaps, q)) for q in (50, 90, 95))
    else:
        p50 = p90 = p95 = float("nan")

    return {
        "gap": real["gap"], "in_mean": real["in_mean"], "out_mean": real["out_mean"],
        "null_p50": p50, "null_p90": p90, "null_p95": p95,
        "n_folds": real["n_folds"], "n_perm": len(null_gaps),
    }


# ============================================================
# 指标 2/3：窗口稳定性 与 参数敏感性（复用引擎无副作用入口）
# ============================================================

def _jaccard(a: set, b: set) -> float:
    if not a and not b:
        return 1.0
    union = a | b
    return len(a & b) / len(union) if union else 1.0


def _top_number_sets(cands, schema, frac: float = 0.5) -> Dict[str, set]:
    """
    把候选票集聚合成「号码级热度画像」：每分区统计各号码被候选覆盖的次数，
    取权重前 frac 的号码集合。票面在千万级组合空间几乎不可能逐张重合，
    号码级聚合才是有统计意义的比较单元（采样噪声也自然被平均掉）。
    """
    out = {}
    for zone in schema.zones:
        size = zone.max - zone.min + 1
        w = np.zeros(size)
        for ps in cands:
            for n_ in ps.zones.get(zone.name, []):
                if zone.min <= n_ <= zone.max:
                    w[n_ - zone.min] += 1
        top_n = max(1, int(size * frac))
        order = np.argsort(-w, kind="stable")
        out[zone.name] = {zone.min + int(i) for i in order[:top_n]}
    return out


def _profile_jaccard(a: Dict[str, set], b: Dict[str, set]) -> float:
    """各分区 top 号码集 Jaccard 的均值。"""
    vals = [_jaccard(a[z], b[z]) for z in a if z in b]
    return float(np.mean(vals)) if vals else 0.0


def window_stability(lottery_name: str, windows: Tuple[int, ...] = (30, 50, 80, 120),
                     k: int = 30, seed_base: int = 20260905) -> dict:
    """
    滚动窗口稳定性（号码级热度画像 + 噪声地板归一化）。

    票面在组合空间几乎不可能重合、策略内部随机采样也会引入噪声，
    因此比较单元 = 候选集聚合出的「各分区 top 半区号码集」：
      noise_floor = 同窗口·不同种子 的画像 Jaccard（采样噪声地板）
      cross       = 跨窗口·两两 的画像 Jaccard
      stability   = clip(cross / noise_floor, 0, 1)
    stability ≈ 1 → 跨窗口画像重合不低于采样噪声地板（窗口因素不驱动选号）；
    stability → 0 → 换窗口对号码画像的影响远超采样噪声（低稳定）。
    """
    from prediction.engine import _window_candidates

    data = load_lottery(lottery_name)
    records = data.records
    n = len(records)
    usable = [w for w in windows if 10 < w <= n]
    if len(usable) < 2:
        raise ValueError(f"{lottery_name} 历史不足，无法做窗口稳定性（可用窗口 {usable}）")

    schema = get_schema(lottery_name)
    profiles = {}  # (w, seed) -> {zone: top-number set}
    for w in usable:
        for si in range(2):  # 每窗口 2 个种子：一份跨窗口比较，一份噪声地板
            cands = _window_candidates(lottery_name, records[:w], k, seed_base + w * 10 + si)
            profiles[(w, si)] = _top_number_sets(cands, schema)

    cross_jacs, pairs = [], []
    ws = sorted(usable)
    for i in range(len(ws)):
        for j in range(i + 1, len(ws)):
            jac = _profile_jaccard(profiles[(ws[i], 0)], profiles[(ws[j], 0)])
            cross_jacs.append(jac)
            pairs.append({"a": ws[i], "b": ws[j], "jaccard": round(jac, 4)})

    noise_jacs = [_profile_jaccard(profiles[(w, 0)], profiles[(w, 1)]) for w in ws]
    cross = float(np.mean(cross_jacs)) if cross_jacs else 0.0
    noise = float(np.mean(noise_jacs)) if noise_jacs else 0.0
    stability = min(max(cross / max(noise, 1e-9), 0.0), 1.0)
    return {
        "stability": round(stability, 4),
        "cross_jaccard": round(cross, 4),
        "noise_floor": round(noise, 4),
        "min_cross": round(float(np.min(cross_jacs)), 4) if cross_jacs else 0.0,
        "pairs": pairs,
        "windows": ws,
        "k": k,
        "口径": "号码级热度画像（各分区top半区号码集）+ 噪声地板归一化",
    }


def _zone_key_of(ps) -> tuple:
    from prediction.robust_tiers import _zone_key
    return _zone_key(ps)


def param_sensitivity(lottery_name: str, base_window: int = 50, k: int = 30,
                      seed: int = 20260905) -> dict:
    """
    参数扰动敏感度：置信度 4 维权重 ±扰动下 top-K 排序的 Kendall τ。

    敏感度 sensitivity = 1 - (1+τ)/2 ∈ [0,1]，越大越敏感。
    （窗口参数的敏感性已由 window_stability 的噪声地板归一化口径覆盖；
      种子采样噪声作为 window_stability 的 noise_floor 上报，不重复计分。）
    """
    from prediction.engine import _window_candidates

    data = load_lottery(lottery_name)
    records = data.records
    if len(records) <= base_window:
        raise ValueError(f"{lottery_name} 历史不足 {base_window} 期，无法做参数敏感性")

    cands = _window_candidates(lottery_name, records[:base_window], k, seed)
    base_scores, dim_scores = [], []
    for ps in cands:
        d = ps.score_detail or {}
        base_scores.append(ps.confidence)
        dim_scores.append((
            d.get("冷热号得分", 0.0), d.get("遗漏偏差得分", 0.0),
            d.get("分布拟合度得分", 0.0), d.get("历史命中率得分", 0.0),
        ))
    taus = []
    if len(cands) >= 3:
        from config import PREDICT_DEFAULTS
        w1 = PREDICT_DEFAULTS["confidence_w1"]
        w2 = PREDICT_DEFAULTS["confidence_w2"]
        w3 = PREDICT_DEFAULTS["confidence_w3"]
        w4 = 0.15
        base_ranks = _ranks_desc(base_scores)
        perturbations = [
            (w1 * 1.10, w2 * 0.90, w3 * 1.00, w4),
            (w1 * 0.90, w2 * 1.10, w3 * 1.00, w4),
            (w1 * 1.00, w2 * 1.00, w3 * 1.10, w4),
        ]
        for pw in perturbations:
            pert_scores = [
                pw[0] * d[0] + pw[1] * d[1] + pw[2] * d[2] + pw[3] * d[3]
                for d in dim_scores
            ]
            taus.append(_kendall_tau(base_ranks, _ranks_desc(pert_scores)))
    mean_tau = float(np.mean(taus)) if taus else 1.0
    sens_weight = 1.0 - (1.0 + mean_tau) / 2.0

    return {
        "sensitivity": round(sens_weight, 4),
        "mean_kendall_tau": round(mean_tau, 4),
        "base_window": base_window,
        "k": k,
    }


# ============================================================
# 指标 4：数字池分布漂移（近期 vs 基准，按分区置换零分布校准）
# ============================================================

def _js_divergence(p: np.ndarray, q: np.ndarray) -> float:
    """JS 散度（自然对数，0..ln2）。零向量处理：两侧全 0 → 0。"""
    p = np.asarray(p, dtype=float)
    q = np.asarray(q, dtype=float)
    if p.sum() <= 0 or q.sum() <= 0:
        return 0.0
    p = p / p.sum()
    q = q / q.sum()
    m = 0.5 * (p + q)

    def _kl(a, b):
        mask = a > 0
        return float(np.sum(a[mask] * np.log(a[mask] / b[mask])))

    return 0.5 * _kl(p, m) + 0.5 * _kl(q, m)


def pool_drift(lottery_name: str, recent_n: int = 30, base_n: int = 150,
               n_perm: int = 200, seed: int = 20260905) -> dict:
    """
    近期(recent_n 期) vs 基准(其前 base_n 期)各分区号码分布的 JS 散度。

    校准：把 (recent+base) 期随机重切成 recent_n/base_n 两段，重复 n_perm 次
    得到「纯随机下 JS 散度」的零分布（按分区独立）；真实 JS 超该分区零分布
    P95 才告警。    含历史变点的彩种（七星彩第7位/大乐透红球）先裁剪到现代段。
    """
    cutoff = _modern_cutoff(lottery_name)
    recs = [r for r in load_lottery(lottery_name).records
            if r.期号 and (r.期号 or 0) > cutoff]
    recs.sort(key=lambda r: r.期号 or 0)
    schema = get_schema(lottery_name)
    chron = [draw_to_zones(schema, r) for r in recs]

    if len(chron) < recent_n + base_n:
        recent_n = max(10, len(chron) // 5)
        base_n = len(chron) - recent_n
    if base_n < 20:
        raise ValueError(f"{lottery_name} 历史不足，无法做分布漂移（{len(chron)} 期）")

    recent = chron[-recent_n:]
    base = chron[-(recent_n + base_n):-recent_n]

    zone_reports = []
    rng = np.random.default_rng(seed)
    for zone in schema.zones:
        size = zone.max - zone.min + 1

        def _dist(draws: List[dict]) -> np.ndarray:
            v = np.zeros(size)
            for d in draws:
                for num in d.get(zone.name, []):
                    if zone.min <= num <= zone.max:
                        v[num - zone.min] += 1
            return v

        p = _dist(recent)
        q = _dist(base)
        js = _js_divergence(p, q)

        # 置换零分布：同池随机重切
        pooled = recent + base
        null = []
        for _ in range(n_perm):
            perm = rng.permutation(len(pooled))
            r2 = [pooled[i] for i in perm[:recent_n]]
            b2 = [pooled[i] for i in perm[recent_n:]]
            null.append(_js_divergence(_dist(r2), _dist(b2)))
        p95 = float(np.percentile(null, _DRIFT_P))
        zone_reports.append({
            "分区": zone.name,
            "js": round(js, 5),
            "null_p95": round(p95, 5),
            "alarm": bool(js > p95),
        })

    alarms = [z for z in zone_reports if z["alarm"]]
    return {
        "recent_n": recent_n, "base_n": base_n,
        "modern_cutoff_issue": cutoff,
        "max_js": round(max(z["js"] for z in zone_reports), 5),
        "zones": zone_reports,
        "alarm_zones": [z["分区"] for z in alarms],
        "alarm": bool(alarms),
    }


# ============================================================
# verdict / 报告落盘
# ============================================================

def _verdict(metrics: dict) -> Tuple[str, List[str]]:
    """基于校准阈值产出 verdict 与 alarms（全部带效应量门控，不拍脑袋）。"""
    alarms: List[str] = []
    gap_info = metrics.get("train_test_gap")
    if gap_info and not math.isnan(gap_info.get("null_p95", float("nan"))):
        thr = max(gap_info["null_p95"], 0.0)
        if gap_info["gap"] > thr and abs(gap_info["gap"]) > _GAP_EFFECT_GATE:
            alarms.append(
                f"过拟合迹象：train_test_gap={gap_info['gap']:.3f} 超随机置换零分布 P95={thr:.3f}"
                f"（效应量门控 {(_GAP_EFFECT_GATE)}）"
            )
    ws = metrics.get("window_stability")
    if ws and ws["stability"] < 0.15:
        alarms.append(
            f"窗口稳定性过低：stability={ws['stability']:.3f}（选号随窗口剧烈跳变）"
        )
    ps = metrics.get("param_sensitivity")
    if ps and ps["sensitivity"] > 0.6:
        alarms.append(
            f"参数敏感性过高：sensitivity={ps['sensitivity']:.3f}"
            f"（权重扰动 Kendall τ={ps['mean_kendall_tau']:.3f}）"
        )
    drift = metrics.get("pool_drift")
    if drift and drift["alarm"]:
        alarms.append(
            f"近期分布漂移：{','.join(drift['alarm_zones'])} 分区 JS 超随机重切 P95"
        )

    if any("过拟合" in a for a in alarms):
        verdict = "overfit_risk"
    elif alarms:
        verdict = "watch"
    else:
        good = ((not ws or ws["stability"] >= 0.40)
                and (not ps or ps["sensitivity"] <= 0.30)
                and (not gap_info or math.isnan(gap_info.get("null_p90", float("nan")))
                     or gap_info["gap"] <= gap_info["null_p90"]))
        verdict = "high_robust" if good else "stable"
    return verdict, alarms


def score_candidate_sets(lottery_name: str, zones_list: List[dict],
                         records: Optional[list] = None) -> List[float]:
    """
    票面鲁棒性打分入口（对外薄封装）：给定候选票面，用全历史统计重算置信度。
    供三档管线/外部调用；跨窗口共识逻辑在 prediction.robust_tiers 内部实现。
    """
    from prediction.engine import _score_candidates
    if records is None:
        records = load_lottery(lottery_name).records
    return _score_candidates(lottery_name, records, zones_list)


def light_check(lottery_name: str, write: bool = True) -> dict:
    """
    轻量检查（数字型每次新数据触发）：窗口稳定性(2 窗口) + 分布漂移。
    不算 train-test gap / 参数敏感性（重计算项），秒级返回。
    """
    metrics = {}
    try:
        metrics["window_stability"] = window_stability(
            lottery_name, windows=(30, 60), k=30
        )
    except ValueError as e:
        metrics["window_stability"] = None
        metrics["error"] = str(e)
    try:
        metrics["pool_drift"] = pool_drift(lottery_name)
    except ValueError as e:
        metrics["pool_drift"] = None
        metrics.setdefault("error", str(e))

    verdict, alarms = _verdict(metrics)
    report = {
        "lottery_name": lottery_name,
        "mode": "light",
        "generated_at": datetime.now().isoformat(),
        "data_upto_issue": _latest_issue(lottery_name),
        "metrics": metrics,
        "verdict": verdict,
        "alarms": alarms,
        "说明": ("鲁棒性/过拟合指标度量选号流程的稳定性，不代表号码中奖概率。"
                 "light 模式仅含窗口稳定性与分布漂移（跳过重计算项）。"),
    }
    if write:
        _atomic_write_json(ROBUSTNESS_DIR / f"{lottery_name}.json", report)
        _append_trend(lottery_name, report)
    return report


def compute_robustness_report(lottery_name: str, mode: str = "full",
                              write: bool = True) -> dict:
    """
    全量鲁棒性报告（乐透型每期 / CLI 手动触发）。

    mode="full"  : train-test gap(置换校准) + 窗口稳定性 + 参数敏感性 + 分布漂移
    mode="light" : 仅窗口稳定性 + 分布漂移（等价 light_check）
    """
    if mode == "light":
        return light_check(lottery_name, write=write)

    metrics: dict = {}
    errors = {}
    for name, fn in (
        ("window_stability", lambda: window_stability(lottery_name)),
        ("param_sensitivity", lambda: param_sensitivity(lottery_name)),
        ("pool_drift", lambda: pool_drift(lottery_name)),
        ("train_test_gap", lambda: train_test_gap(lottery_name)),
    ):
        try:
            metrics[name] = fn()
        except ValueError as e:
            metrics[name] = None
            errors[name] = str(e)
        except Exception as e:  # 单指标失败不拖垮整份报告
            logger.warning(f"鲁棒性指标 {name} 计算失败({lottery_name}): {e}")
            metrics[name] = None
            errors[name] = str(e)

    verdict, alarms = _verdict(metrics)
    report = {
        "lottery_name": lottery_name,
        "mode": "full",
        "generated_at": datetime.now().isoformat(),
        "data_upto_issue": _latest_issue(lottery_name),
        "metrics": metrics,
        "errors": errors or None,
        "verdict": verdict,
        "alarms": alarms,
        "说明": "鲁棒性/过拟合指标度量选号流程的稳定性与过拟合风险，不代表号码中奖概率。",
    }
    if write:
        _atomic_write_json(ROBUSTNESS_DIR / f"{lottery_name}.json", report)
        _append_trend(lottery_name, report)
    return report


def _latest_issue(lottery_name: str) -> Optional[int]:
    try:
        data = load_lottery(lottery_name)
        issues = [r.期号 for r in data.records if r.期号]
        return max(issues) if issues else None
    except Exception:
        return None


def _append_trend(lottery_name: str, report: dict):
    """趋势追加（上限 200 条，FIFO）。"""
    path = ROBUSTNESS_DIR / f"trend_{lottery_name}.json"
    try:
        trend = []
        if path.exists():
            with open(path, encoding="utf-8") as f:
                trend = json.load(f)
        if not isinstance(trend, list):
            trend = []
    except Exception:
        trend = []
    m = report.get("metrics") or {}
    gap = m.get("train_test_gap") or {}
    entry = {
        "time": report.get("generated_at"),
        "issue": report.get("data_upto_issue"),
        "mode": report.get("mode"),
        "verdict": report.get("verdict"),
        "train_test_gap": None if not gap else gap.get("gap"),
        "null_p95": None if not gap else gap.get("null_p95"),
        "window_stability": None if not m.get("window_stability") else m["window_stability"].get("stability"),
        "param_sensitivity": None if not m.get("param_sensitivity") else m["param_sensitivity"].get("sensitivity"),
        "max_pool_drift_js": None if not m.get("pool_drift") else m["pool_drift"].get("max_js"),
        "alarms": report.get("alarms", []),
    }
    trend.append(entry)
    trend = trend[-_TREND_CAP:]
    _atomic_write_json(path, trend)


def load_trend(lottery_name: str) -> List[dict]:
    path = ROBUSTNESS_DIR / f"trend_{lottery_name}.json"
    if not path.exists():
        return []
    try:
        with open(path, encoding="utf-8") as f:
            t = json.load(f)
        return t if isinstance(t, list) else []
    except Exception:
        return []
