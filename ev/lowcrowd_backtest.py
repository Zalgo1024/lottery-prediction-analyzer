# -*- coding: utf-8 -*-
"""
ev/lowcrowd_backtest.py —— 低拥挤目标策略：固定注数 / 固定预算 / 多窗口 / 多种子 四臂对照回测

四臂：
  lowcrowd  低拥挤目标策略（门控 gate_q + 覆盖↔冷门加权 λ）
  prod_mix  生产三策略混合（高频/遗漏/区间均衡，按冻结均分权重逐期重放）
  net_ev    现有净 EV 选号（ev.selection.select_tickets）
  random    同池随机对照（ev.selection.control_tickets，同样低重叠约束）

★ 判级不重写：直接复用 ev/coverage_backtest 的 grade_ticket / evaluate_period，
  其底层是 data/feedback 的官方判级单一真源。
★ 浮动奖缓存用带 |issue|tier 键的 _floating_payout_redblue；
  不用 ev.trials._payout_of（它的缓存不带期号，多期回放会串期）。

★ 主终点（事前写死，不许事后挑）
  主终点 = 单期平均中奖注数。各组合等可能 ⇒ 换选号不改变单注中奖概率、
  也不改变平均中奖次数。四臂若有显著差异，说明有实现缺陷或前视泄漏，
  而不是「策略有效」。
  次终点 = ≥10 元期占比（双色球五等：4红 或 3红+1蓝，K=200 时覆盖率约 7%，未饱和）。
  报告项 = ≥5 元期占比（六等只需命中蓝球，K≥50 时 16 个蓝球必被覆盖满 ⇒ 该指标
  在生产注数下恒饱和，对选号无区分力，只作报告不进判定）、净收益。

★ 诚实判定三层
  1) 跨 seed 全距 > 组间差 ⇒ 判「差异在噪声内」，不主张结论；
  2) 块 bootstrap CI（`block_bootstrap_ci` / `paired_report`），块长默认 BETA_STEP=50
     = walk-forward β 的重拟合步长；同块共享 β、共享同一批票 ⇒ 拆得更细就是把自相关
     当成独立样本，区间会假窄；
  3) 逐期配对差符号检验（四臂共享同一批开奖期）。
     ⚠️ 反保守：`net_ev` / `random` / `lowcrowd`（块内）的票集只抽一次、跨期复用，
     有效样本量接近 1 ⇒ p 值会假小，只作参考，不作判定依据。
"""
from __future__ import annotations

import json
import logging
import math
import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

ARMS = ("lowcrowd", "prod_mix", "net_ev", "random")

# ★ 默认口径 = 「轻量档」：裸调 compare() 应该几分钟跑完，而不是半小时以上。
#   重活（文档第 7 节那套 4 窗口 × 5 seed × 20 情景）必须显式 `full=True`。
DEFAULT_WINDOWS: Tuple[Optional[int], ...] = (500,)   # None = 全量
DEFAULT_SEEDS: Tuple[int, ...] = (0, 1, 2)
DEFAULT_LAM_GRID: Tuple[float, ...] = (0.0, 0.5, 1.0)
DEFAULT_GATE_GRID: Tuple[float, ...] = (0.3, 1.0)

# 全量口径（复现已发表结论用；由 compare(full=True) 选用）
TIER1_WINDOWS: Tuple[Optional[int], ...] = (200, 500, 1000, None)
TIER1_SEEDS: Tuple[int, ...] = (0, 1, 2, 3, 4)

# 完整扫描网格（Tier-2 全网格；默认不再用它，避免裸调就跑 20 组情景）
LAM_GRID: Tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
GATE_GRID: Tuple[float, ...] = (0.2, 0.3, 0.5, 1.0)

FROZEN_WEIGHTS = {"高频策略": 1 / 3, "遗漏值策略": 1 / 3, "区间均衡策略": 1 / 3}
PROD_WINDOW = 200     # 生产臂每期回放用的历史窗口期数

# ★ 块 bootstrap 的块长下限 = walk-forward β 的重拟合步长。
#   同一块内的期共享同一个 β、也共享同一批票 ⇒ 期与期之间不独立。
#   块长小于 BETA_STEP 会把这段自相关当成独立样本，CI 假窄（见文档第 4 节口径陷阱 2）。
BETA_STEP = 50
PAIR_METRICS: Tuple[str, ...] = ("中奖注数", "≥10元")   # 主终点 / 次终点，事前写死

# ★ 红球重叠上限必须用**生产口径 3**，不能用 `ev.selection._OVERLAP_CAP["双色球"] = 4`。
#   实测（窗口 500，K=200，λ=0.5 / gate=0.30，同池同种子）：
#     lowcrowd ≥10 元期占比：cap=3 → 0.748，cap=4 → 0.630
#     同池随机 ≥10 元期占比：cap=3 → 0.812，cap=4 → 0.800
#   即 cap 对「按冷序贪心」的低拥挤臂影响极大（放宽重叠 ⇒ 选出的票更聚集 ⇒ 并集概率下降），
#   对随机臂几乎无影响。用 4 跑出来的差距会被 cap 差异污染，不是选号本身的差距。
PROD_OVERLAP_CAP = 3

SATURATION_NOTE = (
    "≥5 元（六等）只要求命中蓝球，而 K≥50 时 16 个蓝球必被覆盖满 ⇒ 该指标在生产注数下"
    "恒饱和、对选号无区分力，只作报告不进判定。"
)

_BETA_CACHE: Dict[Tuple[str, Optional[int]], List[Dict[str, Any]]] = {}


# ---------------------------------------------------------------- 数据准备
def load_replay_recs(lottery: str) -> Tuple[List[Any], Optional[Dict[str, Any]], Dict[str, int]]:
    """取可与 popularity 行对齐的开奖记录（升序）。

    popularity 只对「有一等奖注数且销售额>0」的期建模，故回测期必须取同一集合，
    否则 walk-forward 的切点坐标对不上。
    返回 (recs_asc, rows, issue2pos)。
    """
    from data.loader import load_lottery

    from ev import popularity_proxy as pp
    rows = pp.load_rows(lottery)
    if rows is None:
        return [], None, {}
    issue2pos = {str(i): p for p, i in enumerate(rows["issues"])}
    data = load_lottery(lottery)
    recs = [r for r in (data.records or []) if getattr(r, "期号", None) and str(r.期号) in issue2pos]
    recs.sort(key=lambda r: r.期号)          # ★ loader 返回降序，必须重排升序
    return recs, rows, issue2pos


def beta_blocks(lottery: str, periods: Optional[int] = None,
                min_train: int = 1200, step: int = BETA_STEP) -> List[Dict[str, Any]]:
    """walk-forward β 分块（带进程内缓存；β 与 pool/seed/回放窗口都无关，故可跨网格共享）。

    `periods` 只为兼容旧调用保留，**不参与计算也不参与缓存键** ——
    β 的分块只由 (min_train, step) 与全量行集决定。
    """
    # ★ 缓存键不能带 periods：`walk_forward_beta` 只吃 rows/min_train/step，
    #   β 与「回放窗口」无关。以前用 (lottery, periods) 做键 ⇒ 4 个窗口会把同一批
    #   β 重复拟合 4 次（单次约 3.6s，全量下白烧十几分钟）。
    key = (lottery, int(min_train), int(step))
    if key in _BETA_CACHE:
        return _BETA_CACHE[key]
    from ev import popularity_proxy as pp
    rows = pp.load_rows(lottery)
    blocks = pp.walk_forward_beta(lottery, rows=rows, min_train=min_train, step=step)
    _BETA_CACHE[key] = blocks
    return blocks


# ---------------------------------------------------------------- 各臂选号
def prod_mix_tickets(lottery: str, recs_desc: List[Any], K: int,
                     weights: Optional[Dict[str, float]] = None,
                     seed: int = 0) -> List[Dict[str, Any]]:
    """生产三策略混合，逐期重放（照抄 engine._window_candidates 骨架，换成按权重分配）。

    recs_desc 必须**降序**（records[0] = 最新），因为 pipeline.statistics 的遗漏值
    逻辑是从最新往最旧遍历。
    ⚠️ 三个策略函数内部用全局 random ⇒ 必须 random.seed 才能保证确定性。
    """
    from config import LOTTERY_CONFIG
    from pipeline.statistics import frequency_analysis, missing_value_analysis
    from prediction.engine import (_DataSlice, _compute_historical_hit_rate,
                                   _strategy_balanced, _strategy_high_freq,
                                   _strategy_missing_value)

    cfg = LOTTERY_CONFIG[lottery]
    data = _DataSlice(lottery, list(recs_desc))
    random.seed(seed)
    np.random.seed(seed % (2 ** 32))
    freqs = frequency_analysis(data)
    missing = missing_value_analysis(data)
    hist_rate = _compute_historical_hit_rate(data)

    names = ["高频策略", "遗漏值策略", "区间均衡策略"]
    w = [float((weights or FROZEN_WEIGHTS).get(n, 1 / 3)) for n in names]

    # ★ existing 必须**跨 K 注共享**：生产侧 _dedup_and_record 会把它当累积去重集合，
    #   每注新建一份等于关掉生产的相似去重（红球交集 ≤3）。
    existing: set = set()
    fns = {
        "高频策略": lambda: _strategy_high_freq(data, cfg, existing, freqs, missing),
        "遗漏值策略": lambda: _strategy_missing_value(data, cfg, existing, freqs, missing),
        "区间均衡策略": lambda: _strategy_balanced(data, cfg, existing, freqs, missing),
    }
    # ★ 固定注数对照：策略函数可能因「与已选票红球交集 >3」返回 None（生产侧靠
    #   _pad_to_groups 补齐）。若这里直接少给几注，四臂就不是同注数对照，
    #   净收益与「至少中 N 元」都会被注数差异污染。故按权重继续抽，直到凑满 K。
    rng_seq = random.Random(seed)
    out: List[Dict[str, Any]] = []
    attempts, max_attempts = 0, max(3 * int(K), 30)
    while len(out) < int(K) and attempts < max_attempts:
        name = rng_seq.choices(names, weights=w, k=1)[0]
        attempts += 1
        ps = fns[name]()
        if ps is None:
            continue
        out.append({"红球": sorted(int(x) for x in ps.reds),
                    "蓝球": sorted(int(x) for x in ps.blues)})
    return out


def _netev_tickets(lottery: str, pool: List[Dict[str, Any]], K: int,
                   seed: int, issue: Any) -> List[Dict[str, Any]]:
    from ev.selection import select_tickets
    res = select_tickets(lottery, n=K, seed=seed, issue=issue, candidates=pool)
    if res.get("拒绝"):
        return [row["号码"] for row in res.get("选中", [])]
    return [row["号码"] for row in res.get("选中", [])]


def _random_tickets(lottery: str, pool: List[Dict[str, Any]], K: int,
                    seed: int, cap: Optional[int] = None) -> List[Dict[str, Any]]:
    from ev.selection import control_tickets
    return control_tickets(lottery, pool, K, seed=seed,
                           cap=(cap if cap is not None else PROD_OVERLAP_CAP))


def _ov_cap(lottery: str) -> int:
    """`ev.selection._OVERLAP_CAP` 的实际取值（只用于元数据披露，不改变行为）。"""
    try:
        from ev.selection import _OVERLAP_CAP
        return int(_OVERLAP_CAP.get(lottery, 3))
    except Exception:                                    # noqa: BLE001
        return -1


# ---------------------------------------------------------------- 回放
def replay_plan(lottery: str, plan: List[Tuple[Any, Any, List[Dict[str, Any]]]],
                cache: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """plan = [(期号, 开奖记录, 该期票列表), ...]，按时间升序。"""
    from ev.coverage_backtest import evaluate_period

    cache = cache if cache is not None else {}
    n = len(plan)
    if n == 0:
        return {"期数": 0}
    K = len(plan[0][2])
    wins = 0
    total = 0.0
    ge5 = 0
    ge10 = 0
    staked = 0
    series_ge10: List[int] = []
    series_wins: List[int] = []
    series_pay: List[float] = []
    for issue, rec, tks in plan:
        ev = evaluate_period(lottery, tks, rec, issue, cache)
        wins += int(ev["命中注数"])
        total += float(ev["总实得"])
        staked += len(tks)
        ge5 += 1 if ev["≥5元"] else 0
        ge10 += 1 if ev["≥10元"] else 0
        series_ge10.append(1 if ev["≥10元"] else 0)
        series_wins.append(int(ev["命中注数"]))
        series_pay.append(float(ev["总实得"]))
    # ★ 成本按**实际投注注数**逐期累加，不用 n×K 估算：
    #   某臂少出几注时，n×K 会把它的净收益凭空算差，变成注数差异而非选号差异。
    cost = staked * 2.0
    return {
        "期数": n, "K": K, "总投注注数": staked,
        "≥5元期占比": round(ge5 / n, 4),
        "≥10元期占比": round(ge10 / n, 4),
        "中奖次数": wins,
        "单期平均中奖注数": round(wins / n, 4),
        "总实得": round(total, 2),
        "净收益": round(total - cost, 2),
        "单期平均净收益": round((total - cost) / n, 4),
        "序列": {"≥10元": series_ge10, "中奖注数": series_wins, "实得": series_pay},
    }


def run_arm(lottery: str, arm: str, periods: Optional[int] = 500, K: int = 200,
            seed: int = 0, lam: float = 0.5, gate_q: float = 0.30,
            pool_size: int = 20000, cap: Optional[int] = None,
            prod_window: int = PROD_WINDOW, keep_series: bool = True) -> Dict[str, Any]:
    """跑一条臂。返回指标 dict（含序列，供配对比较）。

    cap: 红球重叠上限。默认取 PROD_OVERLAP_CAP=3（生产口径）。
    ⚠️ net_ev 臂无法指定：`select_tickets` 不暴露 cap，它内部固定用
    `ev.selection._OVERLAP_CAP["双色球"]=4` ⇒ 该臂的重叠口径与其余三臂不同，
    结果里以 `重叠上限` 字段标出，比较时必须知道这一点。
    """
    from ev import lowcrowd_select as ls
    from ev import popularity_proxy as pp
    from ev.coverage_backtest import build_pool

    recs_all, rows, issue2pos = load_replay_recs(lottery)
    if not recs_all:
        return {"臂": arm, "期数": 0, "降级": "无可用开奖记录"}
    n_full = len(recs_all)
    recs = recs_all[-int(periods):] if periods else recs_all
    # ★ walk-forward 的切点坐标是**全量期**索引，须换算到切片后的坐标
    offset = n_full - len(recs)

    pool = build_pool(lottery, pool_size, seed)
    n = len(recs)
    # ★ 四臂必须落在**同一批开奖期**上，否则逐期配对比较（符号检验）会被 zip 静默截断。
    #   · 生产臂需要「目标期之前」的历史窗口 ⇒ 窗口覆盖全量历史时首期无历史可算（warmup）；
    #   · 低拥挤臂的 β 需要 MIN_TRAIN 期训练历史 ⇒ 全量回放时前 MIN_TRAIN 期它根本没有分位。
    #   取两者的**交集起点**：所有臂都从 start 开始，公共窗口之外的期一律不进判定。
    warmup = 1 if offset == 0 else 0
    start = max(int(pp.MIN_TRAIN) - offset, warmup)
    covered_until = start
    cap_use = int(cap) if cap is not None else PROD_OVERLAP_CAP
    plan: List[Tuple[Any, Any, List[Dict[str, Any]]]] = []

    if arm == "lowcrowd":
        # 块级 walk-forward：每块用「块之前」的 β 重新打分并重选；
        # 拟合失败的块分位为 None ⇒ 跳过该块（不许用相邻块 β 顶替，那是前向泄漏）。
        blocks = beta_blocks(lottery, periods)
        meta: Dict[str, Any] = {}
        for b in blocks:
            lo = max(int(b["切点"]) - offset, start)
            hi = min(int(b["止"]) - offset, n)
            if hi <= max(lo, covered_until):
                continue
            lo = max(lo, covered_until)
            u = None
            if b.get("params"):
                u = pp.crowd_quantiles(lottery, pool, beta=b["params"]["beta"],
                                       mu_=b["params"]["m_"], sd_=b["params"]["s_"],
                                       persist=False)
            if u is None or not np.isfinite(u).any():
                continue
            tks, meta = ls.select_lowcrowd(lottery, pool, K, u, lam=lam, gate_q=gate_q,
                                           cap=cap_use,
                                           beta_caliber="walk-forward 块重拟合β")
            if not tks:
                continue
            for r in recs[lo:hi]:
                plan.append((r.期号, r, tks))
            covered_until = hi
        if not plan:
            # 窗口太短（期数 ≤ 最小训练期数）⇒ 无可用 walk-forward 块，
            # 退化为「冻结 β 单次选号 + 固定票集回放」，并在结论里标注。
            u = pp.crowd_quantiles(lottery, pool)
            tks, meta = ls.select_lowcrowd(lottery, pool, K, u, lam=lam, gate_q=gate_q,
                                           cap=cap_use,
                                           beta_caliber="冻结β（窗口过短，无可用 walk-forward 块）")
            if not tks:
                return {"臂": arm, "期数": 0, "降级": "拥挤度代理不可用"}
            for r in recs[start:]:
                plan.append((r.期号, r, tks))
        # 结构指标（取最后一个块的元数据；块间 β 不同 ⇒ 只作量级参考，不作精确比较）
        _meta_tail = {"低拥挤参数": {"λ": lam, "门控": gate_q},
                      "门控生效分位": (meta.get("门控") or {}).get("生效分位"),
                      "门控后候选数": (meta.get("门控") or {}).get("门控后候选数"),
                      "覆盖键": (meta.get("覆盖") or {}).get("选中覆盖键"),
                      "覆盖理论上限": (meta.get("覆盖") or {}).get("理论上限")}
    elif arm == "prod_mix":
        for t in range(start, n):
            lo = max(0, offset + t - int(prod_window))
            win = list(reversed(recs_all[lo:offset + t]))   # ★ 降序交给统计函数
            if not win:
                continue
            tks = prod_mix_tickets(lottery, win, K, FROZEN_WEIGHTS, seed)
            if not tks:
                continue
            plan.append((recs[t].期号, recs[t], tks))
        _meta_tail = {"生产窗口": int(prod_window), "权重": "冻结均分（生产权重文件含未来信息，禁用于回放）"}
    elif arm == "net_ev":
        # 打分基准期取**回放起点之前**那一期，避免「用本期奖池选号又用本期开奖结算」的虚假对照。
        # ★ 全量窗口下 offset=0，取 recs_all[0] 会拿到**最旧**一期（早期奖池数据缺失 ⇒
        #   模型拒用后静默退化成近似随机，实测把 ≥10 元从 0.575 顶到 0.804，那是另一个口径）。
        idx_prev = offset + start - 1
        prev_issue = recs_all[idx_prev].期号 if idx_prev >= 0 else recs_all[0].期号
        tks = _netev_tickets(lottery, pool, K, seed, prev_issue)
        if not tks:
            return {"臂": arm, "期数": 0, "降级": "净 EV 选号返回空"}
        for r in recs[start:]:
            plan.append((r.期号, r, tks))
        # ⚠️ 该臂的重叠上限由 select_tickets 内部固定，无法与本函数的 cap 对齐
        _meta_tail = {"β口径": "冻结β（与生产同口径）",
                      "重叠上限": f"不可调（select_tickets 内部 _OVERLAP_CAP={_ov_cap(lottery)}）"}
    elif arm == "random":
        tks = _random_tickets(lottery, pool, K, seed, cap=cap_use)
        for r in recs[start:]:
            plan.append((r.期号, r, tks))
        _meta_tail = {"β口径": "不涉及"}
    else:
        raise ValueError(f"未知臂: {arm}")

    res = replay_plan(lottery, plan)
    res["臂"] = arm
    res["彩种"] = lottery
    res["seed"] = seed
    # 公共回放窗口：四臂逐期对齐的区间（低拥挤臂的 β 需要 MIN_TRAIN 期历史，
    # 生产臂首期需要历史窗口 ⇒ 取交集，窗口外的期不进判定）
    res["回放起点"] = int(start)
    res["公共窗口期数"] = int(n - start)
    res.setdefault("重叠上限", cap_use)
    _ks = [len(tks) for _, _, tks in plan]
    res["每期注数min"] = min(_ks) if _ks else 0
    res["每期注数max"] = max(_ks) if _ks else 0
    res["λ"] = (round(lam, 3) if arm == "lowcrowd" else None)
    res["门控"] = (round(gate_q, 3) if arm == "lowcrowd" else None)
    res.update(_meta_tail)
    if not keep_series:
        res.pop("序列", None)
    return res


# ---------------------------------------------------------------- 诚实判定
def sign_test(a: Sequence[float], b: Sequence[float]) -> Dict[str, Any]:
    """逐期配对差的符号检验（双侧）。a/b 长度需一致。"""
    from ev.market_backtest import binom_two_sided

    pos = neg = 0
    for x, y in zip(a, b):
        if x > y:
            pos += 1
        elif x < y:
            neg += 1
    m = pos + neg
    return {"正期数": pos, "负期数": neg, "有效期数": m,
            "p值": (round(binom_two_sided(pos, m, 0.5), 4) if m else None)}


def seed_spread(values: Sequence[float]) -> Dict[str, Any]:
    """跨 seed 离散度：均值 ± 标准差 + 全距。"""
    v = [float(x) for x in values]
    if not v:
        return {}
    mean = sum(v) / len(v)
    sd = math.sqrt(sum((x - mean) ** 2 for x in v) / len(v)) if len(v) > 1 else 0.0
    return {"均值": round(mean, 4), "标准差": round(sd, 4),
            "min": round(min(v), 4), "max": round(max(v), 4),
            "全距": round(max(v) - min(v), 4)}


def block_bootstrap_ci(d: Sequence[float], block: int = BETA_STEP,
                       reps: int = 2000, alpha: float = 0.05,
                       seed: int = 0) -> Dict[str, Any]:
    """配对差序列 d 的均值 · **循环**移动块 bootstrap 百分位 CI。

    为什么必须分块：低拥挤臂的 β 每 BETA_STEP 期重拟合一次，同一块内的期共享
    同一个 β、也共享同一批票 ⇒ 期与期之间不独立。逐期独立重抽样会把这段自相关
    当成独立样本，区间假窄（与「口径陷阱 2」同源）。

    为什么用循环块：普通移动块的起点只能取 [0, n-block]，首尾期被抽到的次数
    系统性偏少，n 不大时均值估计有偏；循环块让每一期被抽中的机会均等。
    """
    v = np.asarray([float(x) for x in d], dtype=float)
    n = int(v.size)
    if n < 2:
        return {"配对期数": n, "降级": "期数不足，不估计区间"}
    if int(reps) <= 0:
        return {"配对期数": n, "降级": "重抽样次数为 0"}
    block = max(1, min(int(block), n))
    rng = np.random.default_rng(int(seed))
    nblk = int(math.ceil(n / block))
    offsets = np.arange(block, dtype=np.int64)[None, :]
    means = np.empty(int(reps), dtype=float)
    for i in range(int(reps)):
        starts = rng.integers(0, n, size=nblk)
        idx = (starts[:, None] + offsets) % n
        means[i] = float(v[idx.reshape(-1)][:n].mean())
    lo = float(np.quantile(means, alpha / 2.0))
    hi = float(np.quantile(means, 1.0 - alpha / 2.0))
    zero_in = bool(lo <= 0.0 <= hi)
    return {
        "配对期数": n, "块长": block, "重抽样次数": int(reps),
        "置信水平": round(1.0 - float(alpha), 4),
        "差均值": round(float(v.mean()), 6),
        "CI下限": round(lo, 6), "CI上限": round(hi, 6),
        "含零": zero_in,
        "判定": ("区间含 0 ⇒ 与「两臂无差异」相容，不主张有差异"
                 if zero_in else
                 "区间不含 0；先排查前视泄漏 / 重叠口径 / 每期注数差异，再谈结论"),
    }


def paired_report(res_a: Dict[str, Any], res_b: Dict[str, Any],
                  metric: str = "≥10元", block: int = BETA_STEP,
                  reps: int = 2000, seed: int = 0) -> Dict[str, Any]:
    """两臂在**同一批期**上的配对比较：块 bootstrap CI + 逐期符号检验。

    metric 取 replay_plan「序列」里的键（"中奖注数" / "≥10元" / "实得"）。
    ⚠️ 长度不等时按较短者截断并在结果里标出——截断通常意味着两臂没落在同一窗口。
    """
    sa = ((res_a.get("序列") or {}).get(metric)) or []
    sb = ((res_b.get("序列") or {}).get(metric)) or []
    m = min(len(sa), len(sb))
    if m == 0:
        return {"指标": metric, "配对期数": 0, "降级": "无配对序列"}
    a = [float(x) for x in sa[:m]]
    b = [float(x) for x in sb[:m]]
    return {
        "指标": metric, "配对期数": m, "截断": len(sa) != len(sb),
        "臂A": res_a.get("臂"), "臂B": res_b.get("臂"),
        "A均值": round(sum(a) / m, 6), "B均值": round(sum(b) / m, 6),
        "块bootstrap": block_bootstrap_ci([x - y for x, y in zip(a, b)],
                                          block=block, reps=reps, seed=seed),
        "符号检验": sign_test(a, b),
    }


def _agg_paired(reps: List[Dict[str, Any]]) -> Dict[str, Any]:
    """多种子的配对结果聚合：不看最小 p 值，只数「区间含零」的次数。"""
    ok = [r for r in reps if r.get("配对期数")]
    if not ok:
        return {"配对期数": 0, "运行次数": 0, "降级": "无配对运行"}
    zero = sum(1 for r in ok if (r.get("块bootstrap") or {}).get("含零"))
    return {"运行次数": len(ok), "配对期数": ok[0]["配对期数"],
            "含零次数": zero, "不含零次数": len(ok) - zero,
            "各seed": ok}


def verdict(low: Dict[str, Any], base: Dict[str, Any]) -> str:
    """「组间差 vs 跨 seed 全距」的朴素判定：差小于全距 ⇒ 噪声内。"""
    if not low or not base:
        return "样本不足"
    d = abs(low["均值"] - base["均值"])
    spread = max(low["全距"], base["全距"])
    return ("差异在噪声内（组间差 %.4f ≤ 跨 seed 全距 %.4f），不主张结论"
            % (d, spread)) if d <= spread else (
        "组间差 %.4f 大于跨 seed 全距 %.4f，需进一步核查（先排查前视泄漏与实现缺陷）"
        % (d, spread))


def compare(lottery: str = "双色球", arms: Sequence[str] = ARMS,
            windows: Optional[Sequence[Optional[int]]] = None,
            seeds: Optional[Sequence[int]] = None,
            lam_grid: Optional[Sequence[float]] = None,
            gate_grid: Optional[Sequence[float]] = None,
            K: int = 200, pool_size: int = 8000,
            bootstrap: bool = True, bootstrap_reps: int = 2000,
            block: int = BETA_STEP,
            full: bool = False,
            scenario_window: Optional[int] = None) -> Dict[str, Any]:
    """跑网格并做跨 seed 汇总。

    **默认是「轻量档」**：窗口只跑 500 期、种子 0/1/2、λ×gate 走 3×2=6 组情景，
    裸调约 2~3 分钟。

    `full=True` 切换到**全量口径**（= 文档第 7 节复现命令）：
    窗口 200/500/1000/全量 × 5 个种子 × 完整的 5×4 情景网格，实测 30 分钟以上。
    `full=True` 时显式传入的 windows/seeds/lam_grid/gate_grid 仍然优先。

    `scenario_window`：情景网格用哪个窗口回放。默认取 `windows[-1]`（轻量档下就是 500）。
    以前这里是隐式 `windows[-1]`，一旦默认含「全量」就会把 20 组情景全跑到全量上，
    是那颗「裸调 30 分钟」的雷。

    bootstrap=True 时额外对每条臂与 random 臂做逐期配对（块 bootstrap CI + 符号检验），
    指标只取 PAIR_METRICS（主终点 / 次终点），避免事后挑指标。
    """
    # 用 None 当哨兵（而不是拿常量比相等）：否则 full=True 且调用方显式写了
    # windows=(500,) 时会被误判成「没传」。
    if windows is None:
        windows = TIER1_WINDOWS if full else DEFAULT_WINDOWS
    if seeds is None:
        seeds = TIER1_SEEDS if full else DEFAULT_SEEDS
    if lam_grid is None:
        lam_grid = LAM_GRID if full else DEFAULT_LAM_GRID
    if gate_grid is None:
        gate_grid = GATE_GRID if full else DEFAULT_GATE_GRID
    if scenario_window is None:
        scenario_window = windows[-1] if windows else None

    out: Dict[str, Any] = {"彩种": lottery, "K": K, "注": SATURATION_NOTE,
                           "口径": "全量(full=True)" if full else "轻量(默认)",
                           "情景窗口": scenario_window,
                           "窗口": [], "低拥挤情景": []}
    for w in windows:
        blk: Dict[str, Any] = {"窗口": w, "臂": {}}
        rows_by_arm: Dict[str, List[Dict[str, Any]]] = {}
        for arm in arms:
            if arm == "lowcrowd":
                continue
            rows = [run_arm(lottery, arm, periods=w, K=K, seed=s, pool_size=pool_size)
                    for s in seeds]
            rows_by_arm[arm] = rows
            blk["臂"][arm] = _agg(rows)
        base = rows_by_arm.get("random") or []
        if bootstrap and base:
            blk["配对vs随机"] = {}
            for arm, rows in rows_by_arm.items():
                if arm == "random":
                    continue
                by_metric: Dict[str, Any] = {}
                for metric in PAIR_METRICS:
                    by_metric[metric] = _agg_paired(
                        [paired_report(rows[i], base[i], metric=metric,
                                       block=block, reps=bootstrap_reps, seed=s)
                         for i, s in enumerate(seeds)
                         if i < len(rows) and i < len(base)])
                blk["配对vs随机"][arm] = by_metric
        out["窗口"].append(blk)
    for lam in lam_grid:
        for g in gate_grid:
            # 情景回放窗口显式化：不再隐式跟随 windows[-1]，否则默认含「全量」时
            # 20 组情景会被全部跑到全量上（裸调半小时的真正原因）。
            rows = [run_arm(lottery, "lowcrowd", periods=scenario_window,
                            K=K, seed=s, lam=lam, gate_q=g, pool_size=pool_size)
                    for s in seeds]
            out["低拥挤情景"].append({"λ": lam, "门控": g, **_agg(rows)})
    return out


def _agg(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把多次运行（多种子）聚合成 均值±标准差 + 各指标。"""
    ok = [r for r in rows if r.get("期数")]
    if not ok:
        return {"期数": 0, "降级": ok and "无有效运行" or "全部运行被降级"}
    agg: Dict[str, Any] = {"期数": ok[0].get("期数"), "运行次数": len(ok)}
    for key in ("≥5元期占比", "≥10元期占比", "单期平均中奖注数",
                "总实得", "净收益", "单期平均净收益"):
        agg[key] = seed_spread([r.get(key, 0.0) for r in ok])
    return agg


def to_jsonable(d: Any) -> Any:
    return json.loads(json.dumps(d, ensure_ascii=False, default=str))


if __name__ == "__main__":
    import sys

    # 用法：python -m ev.lowcrowd_backtest [彩种] [--full]
    #   默认走轻量档（约 2~3 分钟）；--full 复现文档第 7 节那套全量口径（30 分钟以上）。
    _args = [a for a in sys.argv[1:] if not a.startswith("--")]
    _full = "--full" in sys.argv[1:]
    lot = _args[0] if _args else "双色球"
    rep = compare(lot, pool_size=8000, seeds=None if _full else (0,), full=_full)
    print(json.dumps(to_jsonable(rep), ensure_ascii=False, indent=1))
