"""
ev/selection.py —— 按「实得奖金」选号（NetPayout WP2）

目标函数（方案文档 §5-WP2）：
    score(t) = Σ_g P_理论(g|t) × payout_eff(g, t, issue) − cost
    因 P_理论 与号码无关，score 的差异 **100% 来自奖金侧**（分薄/奖池），
    模型在数学上不可能"作弊"声称更准。

适用性（诚实边界）：
    双色球          ✅ 流行度模型 GO（WP1）→ 号码相关 EV（向量化批量打分）
    七星彩          ✅ crowd v2 撞号分薄 → 号码相关 EV
    大乐透          ⚠️ WP1 判 No-Go（OOS 不稳健）→ **跳过打分**直接随机出号
                      （EV 恒定时排序是伪信息，做排序反而暗示虚假优选）
    排列3/3D/排列5  ❌ 固定赔率撞号不分薄 → 选号不改变期望，明确拒绝
                      （如需反大众形态采样请用 `cli.py ev <lottery> --sample N`）

性能设计：
    双色球候选打分走 ev.popularity.batch_mu + ev.payout.pool_share_expected_batch
    的向量化路径（2 万候选 < 1 秒），只有最终选中的 n 注才逐注调 payout_eff
    取完整奖级明细——逐注口径与批量口径有单测对齐（tests/test_payout_eff.py）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_OVERLAP_CAP = {"双色球": 4, "大乐透": 3, "七星彩": 3, "七乐彩": 3, "快乐8": 4}
_FIXED_ODDS = ("排列3", "福彩3D", "排列5")

# 单注 EV 与选号无关、且不在 ev.rulebook 口径内的彩种 → 拒绝打分的机制说明。
# 快乐8：39 档全部是「规则名义 + 限赔封顶」，每注概率只取决于玩法，与选哪些号无关。
# 七乐彩：一~三等浮动，但本仓没有七乐彩的拥挤度模型 ⇒ 分薄乘数无从估计，
#         不能用「猜一个」冒充"冷门更值"。两者都只能给恒定 EV。
_CONSTANT_EV = {
    "快乐8": ("快乐8：10 玩法 / 39 档均为规则名义值+限赔封顶，单注概率只取决于玩法，"
              "与选哪些号无关 ⇒ EV 恒定，无号码相关收益"),
    "七乐彩": ("七乐彩：一~三等浮动奖，但本仓无七乐彩拥挤度（分薄）模型 ⇒ "
               "号码相关项无法估计，只能按恒定 EV 处理；不做伪排序"),
}


def _rand_tickets(lottery: str, m: int, rng: np.random.Generator) -> List[Dict[str, Any]]:
    """随机合法候选号码。"""
    out = []
    if lottery == "双色球":
        for _ in range(m):
            reds = sorted(rng.choice(np.arange(1, 34), size=6, replace=False).tolist())
            out.append({"红球": reds, "蓝球": [int(rng.integers(1, 17))]})
    elif lottery == "大乐透":
        for _ in range(m):
            front = sorted(rng.choice(np.arange(1, 36), size=5, replace=False).tolist())
            out.append({"红球": front, "蓝球": sorted(rng.choice(np.arange(1, 13), size=2, replace=False).tolist())})
    elif lottery == "七星彩":
        for _ in range(m):
            out.append({"第1位..第7位": None,
                        "号码": rng.integers(0, 10, size=6).tolist() + [int(rng.integers(0, 15))]})
    elif lottery == "七乐彩":
        for _ in range(m):
            basic = sorted(int(x) for x in
                          rng.choice(np.arange(1, 31), size=7, replace=False).tolist())
            out.append({"号码": {"基本号": basic, "特别号": []}})
    return out


def _overlap(a, b, lottery: str) -> int:
    if lottery in ("双色球", "大乐透"):
        return len(set(a["红球"]) & set(b["红球"]))
    if lottery == "七乐彩":
        za = (a.get("号码") or {}).get("基本号") or []
        zb = (b.get("号码") or {}).get("基本号") or []
        return len(set(za) & set(zb))
    return sum(1 for x, y in zip(a["号码"], b["号码"]) if x == y)


def _greedy_pick(scored: List, n: int, cap: int, lottery: str) -> List[Dict[str, Any]]:
    """scored: [(key, t), ...] 已按 key 降序 → 贪心 + 低重叠约束。"""
    picked, used = [], []
    for key, t in scored:
        if len(picked) >= n:
            break
        if all(_overlap(t, u, lottery) <= cap for u in used):
            picked.append({"号码": t, "排序键": float(key)})
            used.append(t)
    return picked


def _portfolio_metrics(picked: List[Dict[str, Any]], lottery: str) -> Dict[str, Any]:
    """
    组合方差指标（Step F）：衡量选中注集的分散程度。

    有效独立注数 = m² / ΣΣ oᵢⱼ，其中 oᵢⱼ 按主区号码数归一、自重叠 oᵢᵢ=1：
      - 注注相同 → oᵢⱼ=1 → m_eff=1（一注 repeated m 遍，方差无分散）
      - 完全不重叠 → 非 对角 oᵢⱼ=0 → m_eff=m（独立度最高）
    诚实边界：低重叠只降低「中奖结果之间的相关性」（方差），不改变总期望——
    EV 的线性性与号码选择无关。
    """
    ts = [p["号码"] for p in picked]
    m = len(ts)
    if m == 0:
        return {"注数": 0, "平均重叠": None, "最大重叠": None,
                "有效独立注数": None, "口径": "无选中注"}
    if lottery in ("双色球", "大乐透"):
        size = len(ts[0]["红球"])
    elif lottery == "七乐彩":
        size = len((ts[0].get("号码") or {}).get("基本号") or [])
    else:
        size = len(ts[0]["号码"])
    if m < 2:
        return {"注数": m, "平均重叠": None, "最大重叠": None,
                "有效独立注数": float(m),
                "口径": "单注无组合方差可言（m_eff=m）"}
    pair_overlaps = []
    total = 0.0
    for i in range(m):
        for j in range(m):
            o = size if i == j else _overlap(ts[i], ts[j], lottery)
            total += o / size
            if i < j:
                pair_overlaps.append(o)
    m_eff = (m * m) / total if total > 0 else float(m)
    return {
        "注数": m,
        "平均重叠": round(sum(pair_overlaps) / len(pair_overlaps), 2),
        "最大重叠": max(pair_overlaps),
        "有效独立注数": round(m_eff, 2),
        "口径": ("oᵢⱼ 按主区号码数（双/大=红球数，七星彩=7 位）归一，自重叠=1；"
                 "m_eff∈[1, m]，重叠越低组合方差越小——只降方差不提EV"),
    }


def _select_redblue_vectorized(lottery: str, cands: List[Dict[str, Any]],
                               issue: Optional[str], n: int, cap: int,
                               rng: np.random.Generator,
                               mechanism_tilt: bool = True,
                               gamma: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """
    双色球 GO 路径：向量化批量打分（μ → E[min(cap,池/(1+X))]）→ 贪心低重叠。
    返回 {"机制","选中":[...]}（选中含 payout_eff 完整明细）；不可用返回 None（调用方回退）。

    mechanism_tilt: P4 机制倾斜开关。**γ=0 时 `select_in_gap` 返回 base 本身**
    （`key is eff1`）⇒ 排序输入逐位不变 ⇒ 出号与今天完全一致。
    gamma: 显式传入 γ（测试/回放用）；None = 走 `ev.gap.mechanism_gamma` 红线。
    """
    try:
        from ev.payout import (_grade_pool, _resolve_issue, grade_probabilities,
                               pool_share_expected_batch, payout_eff)
        from ev.popularity import batch_mu, get_popularity_model

        params = get_popularity_model(lottery)
        if not params.get("go"):
            return None
        issue = _resolve_issue(lottery, issue)
        gp = _grade_pool(lottery, issue, "一等")
        if not gp:
            return None
        pool, N = gp
        p1 = grade_probabilities(lottery)["一等"]

        R = np.array([t["红球"] for t in cands], dtype=np.int16)
        mus = batch_mu(lottery, R, N, p1, params)
        eff1 = pool_share_expected_batch(pool, mus)      # 一等奖实得（号码相关）
        ok = np.isfinite(eff1)
        if ok.sum() < n:
            return None

        # ---- P4 机制倾斜：mult(c) = exp(-γ·z(c))，与开奖倾向同向 ----
        # ★ γ=0 时 select_in_gap 直接返回 eff1 本身（不是乘 1.0 的拷贝）
        #   ⇒ argsort 的输入逐位相同 ⇒ 出号与未倾斜时完全一致。
        tilt = None
        if mechanism_tilt:
            from ev.mechanism import gap_select as GS
            tilt = GS.select_in_gap(lottery, R, eff1, gamma=gamma)
            key = tilt["key"]
        else:
            key = eff1

        order = np.argsort(-np.where(ok, key, -np.inf))
        scored = [(float(key[i]), cands[i]) for i in order[: min(len(order), 10 * n + 200)] if ok[i]]
        picked = _greedy_pick(scored, n, cap, lottery)

        # 倾斜后 `排序键` 已含乘数 ⇒ 展示字段必须回到**未倾斜**的 eff1，
        # 否则「一等奖实得」会被机制乘数污染（量纲虽然均值为 1，但单注不同）。
        idx_of = {id(c): i for i, c in enumerate(cands)}
        out = []
        for p in picked:
            t = p["号码"]
            r = payout_eff(lottery, t, issue=issue)
            jz = next((x for x in r.get("奖级明细", []) if x["奖级"] == "一等"), None)
            i0 = idx_of.get(id(t))
            base1 = float(eff1[i0]) if i0 is not None else float(p["排序键"])
            out.append({
                "号码": t,
                "净EV": float(r.get("净EV", float("nan"))),
                "一等奖实得": (float(jz["实得单注"]) if jz and jz.get("实得单注") else round(base1, 2)),
                "分薄乘数": r.get("分薄", {}).get("乘数"),
            })
        mechanism = (f"{lottery}：一等奖实得=E[min(1000万封顶, 池/(1+X))]，X~Poisson(μ(组合))，"
                     "μ 由 WP1 流行度模型给出 → 冷门组合 EV 高于热门（不改变中奖概率）；"
                     "批量向量化打分，与逐注 payout_eff 口径对齐")
        if tilt is not None and tilt.get("倾斜"):
            mechanism += ("；P4 机制倾斜已激活（乘数 exp(-γ·z) 与开奖倾向同向，"
                          "改的是中奖概率权重）")
        # ⚠️ 只带 `状态`（纯标量）；`tilt["key"]` 是每候选一个 float 的大数组，
        #    塞进返回会被 P5 的 pending 快照整份写盘。
        return {"机制": mechanism, "选中": out, "期号": issue,
                "机制倾斜": (tilt or {}).get("状态")}
    except Exception as e:
        logger.warning(f"[selection] 向量化路径失败 {lottery}: {e}")
        return None


def control_tickets(lottery: str, cands: List[Dict[str, Any]], n: int,
                    seed: Optional[int] = None,
                    cap: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    阴性对照组：从**同一候选池**随机取 n 注，施加同样的低重叠约束，不做任何打分排序。

    ⚠️ 对照的严格性全靠这一点：A/B 两组来自同一个候选池、同样的重叠约束，
    **唯一差异就是"有没有按一等奖实得排序"**。若换成另生成一批随机号，
    则比较的是"两批随机样本"，而非"排序这件事有没有用"。
    """
    rng = np.random.default_rng(seed)
    cap = cap if cap is not None else _OVERLAP_CAP.get(lottery, 3)
    picked: List[Dict[str, Any]] = []
    for i in rng.permutation(len(cands)):
        if len(picked) >= n:
            break
        t = cands[i]
        if all(_overlap(t, u, lottery) <= cap for u in picked):
            picked.append(t)
    return picked


def _blue_interleaved(pool: List[Dict[str, Any]], lottery: str,
                     rng: np.random.Generator) -> List[Dict[str, Any]]:
    """把候选池按后区(蓝球)分组后轮转交织：使前 K 张覆盖尽可能多的不同后区组合。

    双色球(16 蓝) / 大乐透(66 蓝) 的 5 元奖级(六等/九等)只要求命中蓝球，
    故「分散后区」直接扩大「至少中 5 元」覆盖的不中奖结果集。
    """
    from collections import defaultdict
    groups: Dict[tuple, List[Dict[str, Any]]] = defaultdict(list)
    for t in pool:
        bk = tuple(t.get("蓝球", []))
        groups[bk].append(t)
    keys = list(groups.keys())
    rng.shuffle(keys)
    out: List[Dict[str, Any]] = []
    while len(out) < len(pool):
        progressed = False
        for k in keys:
            if groups[k]:
                out.append(groups[k].pop(0))
                progressed = True
        if not progressed:
            break
    return out


def _qxc_interleaved(pool: List[Dict[str, Any]],
                    rng: np.random.Generator) -> List[Dict[str, Any]]:
    """七星彩：按后区(第7位, 0-14)分组轮转交织，分散「后区命中」这一 5 元(六等)通道。"""
    from collections import defaultdict
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for t in pool:
        nums = t.get("号码") or []
        bk = nums[6] if len(nums) > 6 else None
        groups[bk].append(t)
    keys = list(groups.keys())
    rng.shuffle(keys)
    out: List[Dict[str, Any]] = []
    while len(out) < len(pool):
        progressed = False
        for k in keys:
            if groups[k]:
                out.append(groups[k].pop(0))
                progressed = True
        if not progressed:
            break
    return out


def _digit_interleaved(pool: List[Dict[str, Any]],
                      rng: np.random.Generator) -> List[Dict[str, Any]]:
    """数字型占位：按首位分组轮转（奖级≠5元，优先级置后，仅做位置多样性）。"""
    from collections import defaultdict
    groups: Dict[Any, List[Dict[str, Any]]] = defaultdict(list)
    for t in pool:
        nums = t.get("号码") or []
        bk = nums[0] if nums else None
        groups[bk].append(t)
    keys = list(groups.keys())
    rng.shuffle(keys)
    out: List[Dict[str, Any]] = []
    while len(out) < len(pool):
        progressed = False
        for k in keys:
            if groups[k]:
                out.append(groups[k].pop(0))
                progressed = True
        if not progressed:
            break
    return out


def select_coverage(lottery: str, pool: List[Dict[str, Any]], K: int,
                   target_tier: int = 5, seed: Optional[int] = None,
                   cap: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    覆盖分散选号（Phase 1 奖级覆盖策略）：从**同一候选池**挑 K 注，针对目标奖级的
    「不中奖结果集」做分散，再施加与现有选号相同的低重叠约束。

    ⚠️ 诚实边界（与项目一致）：各组合等可能 ⇒ 本函数**不改变**单注中奖概率 / 平均
    中奖次数；它只改变「K 注之间中奖结果的重叠结构」——在固定 K 下「至少中一注」的
    概率可能提高（也可能不改变，须回测），同时「多注同中」的可能性会改变。

    做法（复用 _greedy_pick / _overlap，唯一差异 = 排序目标）：
      - 双/大 目标=5元(六等/九等，命中蓝球即≥5元)：先按后区分组轮转交织，使前 K 张
        覆盖更多不同后区；再 _greedy_pick 施加前区低重叠。
      - 七星彩 目标=5元(六等=任意≥1位命中 或 后区命中)：按后区分组轮转，分散后区通道。
      - 数字型：奖级≠5元，优先级置后，仅做首位多样性占位。
    更高奖级由 _greedy_pick 的前区/位低重叠自然覆盖（K 注分散在号码空间）。

    返回 [ticket, ...]（ticket 为原候选池中的字典，可直接喂回放/判级）。
    `cap` 为 None 时取 `_OVERLAP_CAP`（旧行为）；对照回测必须显式传生产口径 3
    （`ev.lowcrowd_backtest.PROD_OVERLAP_CAP`），否则 cap 差异会混进选号差异。
    """
    rng = np.random.default_rng(seed)
    if cap is None:
        cap = _OVERLAP_CAP.get(lottery, 3)
    if lottery in ("双色球", "大乐透"):
        ordered = _blue_interleaved(pool, lottery, rng)
    elif lottery == "七星彩":
        ordered = _qxc_interleaved(pool, rng)
    else:
        ordered = _digit_interleaved(pool, rng)
    scored = [(0.0, t) for t in ordered]   # 主分散排序已由 *_interleaved 完成
    picked = _greedy_pick(scored, K, cap, lottery)
    return [p["号码"] for p in picked]


def select_tickets(lottery: str, n: int = 5, pool_size: int = 20000,
                   seed: Optional[int] = None, issue: str = None,
                   candidates: Optional[List[Dict[str, Any]]] = None,
                   mechanism_tilt: bool = True,
                   gamma: Optional[float] = None) -> Dict[str, Any]:
    """
    返回
      {
        "彩种", "期号", "候选数", "选中数",
        "限号修正": str,       # 七星彩=已前置（排序键=限号EV）；双/大=不适用（浮动奖池制）
        "机制": str,           # 本彩种 EV 与号码的关系（诚实标注）
        "选中": [{"号码", "净EV", "一等奖实得", "分薄乘数"}, ...],   # 按实得降序
        "组合方差指标": {...}  # 平均/最大重叠 + 有效独立注数（只降方差不提EV）
      }
    七星彩选中行额外含 限号暴露概率 / μ（同号期望注数）。
    固定赔率彩种返回 {"彩种", "机制", "拒绝": True, "限号修正", "原因": ...}

    candidates: 外部传入的候选池（A/B 对照实验时用同一池，保证对照严格）。
    mechanism_tilt: P4 机制倾斜开关（True=开启；γ=0 时自动逐位退化，与关闭等价）。
    gamma: 显式指定 γ（回放/测试用）；None = 走 `ev.gap.mechanism_gamma` 红线。
    """
    if lottery in _FIXED_ODDS:
        return {
            "彩种": lottery,
            "机制": "固定赔率：撞号不分薄每注赔付，单注 EV 恒定",
            "拒绝": True,
            "限号修正": "不适用（固定赔率，无分薄/限号EV概念）",
            "原因": ("选号不改变期望——任何'高奖金号码'的说法对固定赔率彩种都不成立。"
                     "如需反大众形态采样（纯形态偏好）请用 `cli.py ev " + lottery + " --sample N`"),
        }

    if lottery in _CONSTANT_EV:
        # ★ 2026-11-02（快乐8/七乐彩接入）：本函数的逐注打分依赖 ev.engine.net_ev
        #   → ev.rulebook（只收录 4 个数字型彩种）。这两个新彩种的 EV 只能走
        #   ev.profit（rules_for + grade_probabilities 的超几何/限赔模型）。
        #   与其让它们落到 net_ev 抛「rulebook 未收录彩种」，不如显式拒绝并说清
        #   为什么这里**不需要**打分：它们的单注 EV 与选哪些号无关。
        return {
            "彩种": lottery,
            "机制": _CONSTANT_EV[lottery],
            "拒绝": True,
            "限号修正": "不适用（EV 与号码无关）",
            "原因": ("本函数的排序键来自 ev.engine.net_ev（名义 EV / 限号 EV），"
                     "其口径只覆盖数字型彩种。该彩种的单注 EV 与选号无关 ⇒ 排序无信息量，"
                     "故这里不做选号打分。要算实得/返奖请用 ev.profit（rules_for / "
                     "grade_probabilities）或 data.prize_amounts 的实测金额链。"),
        }

    rng = np.random.default_rng(seed)
    cands = candidates if candidates is not None else _rand_tickets(lottery, pool_size, rng)
    cap = _OVERLAP_CAP.get(lottery, 3)

    # ---- 双色球：优先走向量化批量打分（GO 时） ----
    if lottery == "双色球":
        fast = _select_redblue_vectorized(lottery, cands, issue, n, cap, rng,
                                          mechanism_tilt=mechanism_tilt, gamma=gamma)
        if fast is not None:
            return {
                "彩种": lottery,
                "期号": fast["期号"],
                "候选数": len(cands),
                "选中数": len(fast["选中"]),
                "重叠上限": cap,
                "限号修正": "不适用（浮动奖池制，无固定赔付限额）",
                "机制": fast["机制"],
                "选中": fast["选中"],
                "组合方差指标": _portfolio_metrics(fast["选中"], lottery),
                "机制倾斜": fast.get("机制倾斜"),
            }
        # 模型不可用/数据缺失 → 回退逐注 payout_eff（EV 仍号码相关或恒定）

    # ---- 大乐透 NO-GO：跳过打分（EV 恒定 → 排序是伪信息），诚实随机出号 ----
    if lottery == "大乐透":
        from ev.payout import _resolve_issue
        from ev.popularity import get_popularity_model
        go = bool(get_popularity_model(lottery).get("go"))
        if not go:
            picked = []
            for t in cands[:n]:
                picked.append({
                    "号码": t,
                    "净EV": None,
                    "一等奖实得": None,
                    "分薄乘数": None,
                })
            return {
                "彩种": lottery,
                "期号": _resolve_issue(lottery, issue),
                "候选数": len(cands),
                "选中数": len(picked),
                "重叠上限": cap,
                "限号修正": "不适用（浮动奖池制，无固定赔付限额）",
                "机制": ("大乐透：WP1 流行度模型 OOS 未达门槛（诚实否决）→ EV 恒定，"
                         "排序无信息量，故不做打分、直接随机出号（无号码相关收益）"),
                "选中": picked,
                "组合方差指标": _portfolio_metrics(picked, lottery),
            }
        # 万一未来模型转 GO，走与双色球相同的向量化路径
        fast = _select_redblue_vectorized(lottery, cands, issue, n, cap, rng,
                                          mechanism_tilt=mechanism_tilt, gamma=gamma)
        if fast is not None:
            return {
                "彩种": lottery, "期号": fast["期号"], "候选数": len(cands),
                "选中数": len(fast["选中"]), "重叠上限": cap,
                "限号修正": "不适用（浮动奖池制，无固定赔付限额）",
                "机制": fast["机制"], "选中": fast["选中"],
                "组合方差指标": _portfolio_metrics(fast["选中"], lottery),
                "机制倾斜": fast.get("机制倾斜"),
            }

    # ---- 逐注打分路径（七星彩 crowd v2；双/大回退） ----
    scored = []
    detail: Dict[int, Dict[str, Any]] = {}   # id(票) → payout_eff 明细（仅双/大逐注路径）
    if lottery in ("双色球", "大乐透"):
        from ev.payout import payout_eff
        for t in cands:
            r = payout_eff(lottery, t, issue=issue)
            if "error" in r:
                continue
            # 主排序键 = 一等奖实得（号码相关项）；净EV 作展示
            jz_row = next((x for x in r["奖级明细"] if x["奖级"] == "一等"), None)
            key = float(jz_row["实得单注"]) if (jz_row and jz_row["实得单注"]) else 0.0
            scored.append((key, t))
            detail[id(t)] = r
        mechanism = ("双色球：一等奖实得=E[min(1000万封顶, 池/(1+X))]，X~Poisson(μ(组合))，"
                     "μ 由 WP1 流行度模型给出 → 冷门组合 EV 高于热门（不改变中奖概率）"
                     if lottery == "双色球" else
                     "大乐透：EV 恒定，本结果仅为随机形态采样，无号码相关收益")
    else:  # 七星彩
        from ev.engine import net_ev
        for t in cands:
            nums = t["号码"]
            r = net_ev(lottery, nums, issue=issue)
            ev = r.get("限号EV")
            if ev is None:
                continue
            scored.append((float(ev) + 2.0, t))  # key=头奖贡献+固定部分
            # Step B：限号明细前置存储（排序键本身已是限号EV → 无需事后修正）
            detail[id(t)] = {"限号暴露概率": r.get("限号暴露概率"),
                             "μ": (r.get("详情") or {}).get("同号期望注数μ")}
        mechanism = ("七星彩：浮动头奖撞号分薄（crowd v2），冷门组合 EV 高于热门（不改变中奖概率）")

    scored.sort(key=lambda x: x[0], reverse=True)
    picked_raw = _greedy_pick(scored, n, cap, lottery)

    # 回填展示字段（逐注路径只在最终选中时保留明细）
    # ⚠️ _greedy_pick 返回的是 {"号码","排序键"} 字典列表，不能按 (key, t) 解包
    #    （历史 bug：解包拿到的是 dict 的键名 → round("号码", 2) TypeError）。
    picked = []
    for item in picked_raw:
        t, key = item["号码"], float(item["排序键"])
        row = {"号码": t, "净EV": None, "一等奖实得": None, "分薄乘数": None}
        d = detail.get(id(t))
        if lottery in ("双色球", "大乐透") and d is not None:
            row["净EV"] = d.get("净EV")
            row["一等奖实得"] = round(key, 2)
            row["分薄乘数"] = (d.get("分薄") or {}).get("乘数")
        else:                                   # 七星彩：key = 头奖 EV 贡献 + 2
            row["净EV"] = round(key - 2.0, 4)
            if d is not None:                   # Step B：限号明细回填
                row["限号暴露概率"] = d.get("限号暴露概率")
                row["μ"] = d.get("μ")
        picked.append(row)

    from ev.payout import _resolve_issue
    return {
        "彩种": lottery,
        "期号": _resolve_issue(lottery, issue),
        "候选数": len(scored),
        "选中数": len(picked),
        "重叠上限": cap,
        "限号修正": ("已前置（排序键=限号EV）" if lottery == "七星彩"
                     else "不适用（浮动奖池制，无固定赔付限额）"),
        "机制": mechanism,
        "选中": picked,
        "组合方差指标": _portfolio_metrics(picked, lottery),
    }


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(errors="replace")
    lot = sys.argv[1] if len(sys.argv) > 1 else "双色球"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    print(json.dumps(select_tickets(lot, n=n), ensure_ascii=False, indent=1))
