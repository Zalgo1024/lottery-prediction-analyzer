"""
ev/payout.py —— 「实得奖金」统一口径（NetPayout 轨道 · WP0）

为什么需要这一层：
    「这一注中了能拿多少钱」在改造前散落在四处，口径互不一致（曾踩坑：排列5 单注
    奖金被硬编码为 1040，导致限号额度高估 ~96 倍）：
      data/prize_table.py  固定奖金额 / 浮动奖「当期实际单注奖金」/ 名义参考
      ev/rulebook.py       数字型各奖级概率与名义赔付
      ev/crowd_model.py    人群选号分布 → 撞号分薄乘数 E[1/(1+X)]
      ev/rollover.py       双/大 奖池滚动 → 头奖单注估计（含封顶）
    本模块把四者收口为一个函数 payout_eff()，任何"以奖金为目标"的代码都必须走它。

核心口径（重要，勿改）：
    实得单注(g) = 名义/当期实际基准(g) × 分薄乘数(ticket, issue)
    实得期望    = Σ_g 理论概率(g) × 实得单注(g)
    净EV        = 实得期望 − 2

    ⚠️ 理论概率(g) 一律取**组合数学精确值**，与"这注号码是什么"无关
       （双色球任一注中一等奖的概率都是 1/17,721,088）。
       因此 payout_eff 在不同号码之间的差异**只能来自奖金侧**（分薄 / 奖池），
       不可能来自"猜得更准"——这是本轨道诚实性的数学保证。

分薄乘数现状（诚实标注）：
    排列3/福彩3D/排列5  固定赔率，撞号不分薄 → 恒 1.0
    七星彩              有 crowd v2 模型 → 模型分薄乘数；无模型 → 均匀 Poisson 基线
    双色球/大乐透       WP1（组合流行度模型）完成前无区分能力 → 恒 1.0，
                        基准直接用「当期实际单注奖金」（已含真实撞号分摊）
"""
from __future__ import annotations

import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

COST = 2.0  # 单注成本（元）

_LOTTERY = ("双色球", "大乐透", "排列3", "福彩3D", "排列5", "七星彩")
_REDBLUE = ("双色球", "大乐透")
_DIGITAL = ("排列3", "福彩3D", "排列5", "七星彩")

# 七星彩名义名义上限（rulebook 同口径）
_QXC_CAP = 5_000_000
_SSQ_DLT_CAP = 10_000_000


# ---------------- 号码归一化 ----------------
def _digits(ticket) -> List[int]:
    """数字型：接受 list/tuple/字符串/"1,2,3" → [1,2,3]"""
    if isinstance(ticket, str):
        s = ticket.strip()
        if s.isdigit():
            return [int(c) for c in s]
        parts = [p for p in s.replace("，", ",").split(",") if p.strip()]
        return [int(p) for p in parts]
    return [int(x) for x in ticket]


def normalize_ticket(lottery: str, ticket) -> Dict[str, Any]:
    """
    统一号码输入 → {"类型": "redblue"/"digital", "红球":[], "蓝球":[], "号码":[]}

    可接受：
      - 乐透型：{"红球":[...], "蓝球":[...]} 或 (reds, blues) 二元组
      - 数字型：[1,2,3] / "123" / "1,2,3" / {"第1位":1,...}（按区名排序）
    """
    if lottery in _REDBLUE:
        if isinstance(ticket, dict):
            reds = [int(x) for x in (ticket.get("红球") or [])]
            blues = [int(x) for x in (ticket.get("蓝球") or [])]
        elif isinstance(ticket, (list, tuple)) and len(ticket) == 2 and \
                isinstance(ticket[0], (list, tuple)):
            reds = [int(x) for x in ticket[0]]
            blues = [int(x) for x in ticket[1]]
        else:
            raise ValueError(f"{lottery} 号码需为 {{'红球':[], '蓝球':[]}} 或 (红球, 蓝球)")
        return {"类型": "redblue", "红球": reds, "蓝球": blues, "号码": reds + blues}

    if lottery in _DIGITAL:
        if isinstance(ticket, dict):
            # 按 第N位 排序取值（兼容旧位名由调用方先 normalize_zone_names）
            keys = sorted(ticket.keys(), key=lambda k: _zone_order(k))
            nums = []
            for k in keys:
                v = ticket[k]
                nums.extend(_digits(v) if isinstance(v, (list, tuple, str)) else [int(v)])
        else:
            nums = _digits(ticket)
        return {"类型": "digital", "红球": [], "蓝球": [], "号码": nums}

    raise ValueError(f"不支持的彩种: {lottery}")


def _zone_order(name: str) -> int:
    """「第3位」→ 3；无法解析的键排到最后（保持原顺序）。"""
    s = str(name)
    digits = "".join(c for c in s if c.isdigit())
    return int(digits) if digits else 999


# ---------------- 理论概率（组合精确值，与号码无关） ----------------
_PROB_CACHE: Dict[str, Dict[str, float]] = {}


def grade_probabilities(lottery: str, ticket=None) -> Dict[str, float]:
    """
    返回 {奖级: 概率}（按彩种缓存；概率是组合常数，重复计算纯浪费）。

    ⚠️ 概率与具体号码无关（任一合法注相同）——这里保留 ticket 形参仅为将来
       支持"复式/多注"等改变概率的玩法，当前忽略。
    """
    if lottery in _PROB_CACHE:
        return _PROB_CACHE[lottery]
    probs = _grade_probabilities_impl(lottery)
    _PROB_CACHE[lottery] = probs
    return probs


def _grade_probabilities_impl(lottery: str) -> Dict[str, float]:
    c = math.comb
    if lottery == "双色球":
        den = c(33, 6) * 16                          # 17,721,088
        f = lambda k: c(6, k) * c(27, 6 - k)          # 红球命中 k 个
        pb_hit, pb_miss = 1, 15                       # 蓝球命中/未中的"份数"（分母 16）
        p = lambda k, b: f(k) * b / den               # b=1 蓝中，b=15 蓝不中
        return {
            "一等": p(6, pb_hit),
            "二等": p(6, pb_miss),
            "三等": p(5, pb_hit),
            "四等": p(5, pb_miss) + p(4, pb_hit),
            "五等": p(4, pb_miss) + p(3, pb_hit),
            "六等": p(2, pb_hit) + p(1, pb_hit) + p(0, pb_hit),
        }
    if lottery == "大乐透":
        df_, db_ = c(35, 5), c(12, 2)
        F = lambda k: c(5, k) * c(30, 5 - k)          # 前区命中 k
        B = lambda j: c(2, j) * c(10, 2 - j)          # 后区命中 j
        p = lambda k, j: F(k) * B(j) / (df_ * db_)
        return {
            "一等": p(5, 2),
            "二等": p(5, 1),
            "三等": p(5, 0),
            "四等": p(4, 2),
            "五等": p(4, 1),
            "六等": p(3, 2),
            "七等": p(4, 0),
            "八等": p(3, 1) + p(2, 2),
            "九等": p(3, 0) + p(2, 1) + p(1, 2) + p(0, 2),
        }
    if lottery in ("排列3", "福彩3D"):
        return {"直选": 1 / 1000}
    if lottery == "排列5":
        return {"直选": 1 / 100000}
    if lottery == "七星彩":
        d = 15_000_000
        return {"一等": 1 / d, "二等": 14 / d, "三等": 54 / d,
                "四等": 1971 / d, "五等": 31590 / d, "六等": 1188270 / d}
    raise ValueError(f"不支持的彩种: {lottery}")


# ---------------- 分薄乘数 ----------------
def _poisson_share(lam: float) -> float:
    """E[1/(1+X)], X~Poisson(λ) = (1-e^{-λ})/λ；λ→0 时 →1（独享）。"""
    if lam <= 0:
        return 1.0
    if lam < 1e-9:
        return 1.0
    return (1.0 - math.exp(-lam)) / lam


def _record_settled(rec: Any) -> bool:
    """该期是否**已开奖结算**（奖金/销售额可读）。

    数据源存在滞后：最新一期常「只有开奖号码，奖金/奖池/销售额全为 0」，
    必须与「已结算」区分开（2026-09-13 双色球 26106 即此情形）。

    判定只用**开奖后才会产生**的字段（销售额、二等及以上奖金），
    不要求一等奖注数 > 0——某期无人中头奖属正常，该期仍已结算；
    也**不用奖池奖金**——部分源会在开奖前就给出「当前奖池（面向下一期）」。
    """
    for col in ("总投注额", "销售额", "二等奖奖金", "一等奖奖金"):
        try:
            v = getattr(rec, col, None)
        except Exception:  # pragma: no cover - 防御
            continue
        if v is None:
            continue
        try:
            if float(v) > 0:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _resolve_issue(lottery: str, issue: Any = None) -> Optional[str]:
    """
    解析期号：显式传入原样返回；None → 取该彩种**已结算**的最新一期。

    ⚠️ 必须解析出「有当期数据」的期号，否则浮动奖级读不到「当期实际单注奖金」，
       会一路降级到名义参考 → 双/大二等直接算成「未知」而系统性低估 EV。

    源滞后时（最新一期刚出号码、奖金列未回填）**必须继续往前找**，
    否则整条 EV 链在开奖当晚到次日回填前都是错的。
    """
    if issue is not None and str(issue).strip():
        return str(issue)
    try:
        if lottery in _REDBLUE:
            from data.loader import load_lottery
            recs = load_lottery(lottery).records
            if not recs:
                return None
            for rec in recs:  # 新→旧，取第一个已结算的
                try:
                    if _record_settled(rec):
                        return str(rec.期号)
                except Exception:  # pragma: no cover - 防御
                    continue
            return str(recs[0].期号)  # 全表都查不到结算数据 → 保持原行为

        from data.fetch_sales import load_sales
        df = load_sales(lottery)
        if df is not None and not df.empty:
            return str(df["期号"].iloc[-1])
    except Exception as e:
        logger.debug(f"[payout] 解析 {lottery} 最新期号失败: {e}")
    return None


def _total_tickets(lottery: str, issue: Any = None) -> Optional[float]:
    """当期总注数（销售额/2 或 总投注额/2）；取不到返回 None。"""
    try:
        if lottery in _REDBLUE:
            from data.loader import load_lottery
            data = load_lottery(lottery)
            if not data.records:
                return None
            if issue is None:
                rec = data.records[0]
            else:
                rec = next((r for r in data.records if str(r.期号) == str(issue)), None)
                if rec is None:
                    return None
            v = getattr(rec, "总投注额", None)
            return float(v) / 2.0 if v else None
        from data.fetch_sales import load_sales
        df = load_sales(lottery)
        if df is None or df.empty:
            return None
        if "销售额" not in df.columns:
            return None
        if issue is None:
            v = df["销售额"].iloc[-1]
        else:
            hit = df[df["期号"] == str(issue)]
            if hit.empty:
                return None
            v = hit["销售额"].iloc[-1]
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v / 2.0 if v == v and v > 0 else None
    except Exception as e:  # 数据不可用 → 诚实返回 None
        logger.debug(f"[payout] 取 {lottery} 期{issue} 总注数失败: {e}")
        return None


def share_multiplier(lottery: str, ticket, issue: Any = None,
                     total_tickets: Optional[float] = None) -> Tuple[float, str]:
    """
    返回 (分薄乘数, 来源说明)。1.0 = 独享/不分薄；越小 = 被越多同号者分摊。

    当前能力边界（诚实）：
      - 排列3/3D/排列5：固定赔率 → 恒 1.0
      - 七星彩：crowd v2 → 号码相关；无模型 → 均匀 Poisson 基线（所有注相同）
      - 双/大：WP1 前 → 恒 1.0（依赖「当期实际单注奖金」里的真实分摊）
    """
    if lottery in ("排列3", "福彩3D", "排列5"):
        return 1.0, "固定赔率：撞号不分薄每注赔付"

    if lottery == "七星彩":
        nums = normalize_ticket(lottery, ticket)["号码"]
        n = total_tickets if total_tickets is not None else _total_tickets(lottery, issue)
        try:
            from ev.engine import _get_crowd
            from ev.crowd_model import CrowdV2
            model = _get_crowd(lottery)
            if isinstance(model, CrowdV2) and n:
                return float(model.share_multiplier(nums, n)), "crowd v2：E[1/(1+X)], X~Poisson(同号期望)"
        except Exception as e:
            logger.debug(f"[payout] 七星彩 crowd 模型不可用: {e}")
        if n:
            lam = n * (1.0 / 15_000_000)
            return _poisson_share(lam), f"均匀泊松基线（λ={lam:.3f}，无 crowd 模型）"
        return 1.0, "无销售数据，无法估同号人数 → 按独享（乐观）"

    if lottery in _REDBLUE:
        return 1.0, ("顶层乘数不适用：浮动奖级基准=当期实际单注奖金（已含真实分摊）；"
                     "号码相关路径见 payout_eff 奖级明细（WP1 流行度模型 GO 的彩种自动启用奖池路径）")

    raise ValueError(f"不支持的彩种: {lottery}")


# ---------------- 基准赔付 ----------------
def _rulebook_payout(lottery: str, grade: str) -> Optional[float]:
    try:
        from ev.rulebook import get_rulebook
        rb = get_rulebook(lottery)
        for k, v in rb.items():
            if k.replace("奖", "") == grade:
                return float(v.get("payout", 0)) or None
    except Exception:
        pass
    return None


def base_payout(lottery: str, grade: str, issue: Any = None) -> Tuple[Optional[float], str]:
    """
    某奖级单注「基准赔付」（未乘分薄）与其来源。
    取值链：当期实际单注奖金 → 奖池滚动估计(双/大一等) → rulebook 名义 → None(未知)
    """
    try:
        from data.prize_table import prize_payout
        info = prize_payout(lottery, grade, issue)
        if info.get("金额"):
            return float(info["金额"]), info.get("来源", "当期实际单注奖金")
    except Exception as e:
        logger.debug(f"[payout] prize_table 查询失败 {lottery}/{grade}: {e}")

    if lottery in _REDBLUE and grade == "一等":
        try:
            from ev.rollover import rollover_ev
            v = rollover_ev(lottery, issue=None if issue is None else str(issue)).get("头奖单注奖金估计")
            if v and float(v) > 0:
                return float(v), "奖池滚动估计(rollover)"
        except Exception as e:
            logger.debug(f"[payout] rollover 估计失败 {lottery}: {e}")

    rb = _rulebook_payout(lottery, grade)
    if rb:
        return float(rb), "名义参考(rulebook)"
    return None, "未知（浮动奖级且无当期数据）"


# ---------------- 双/大：流行度模型下的「奖池×E[1/(1+X)]」路径（WP1/WP2 接线） ----------------
_REC_CACHE: Dict[Tuple[str, str], Any] = {}


def _record_for_issue(lottery: str, issue: Any):
    """按期号取开奖记录（带缓存；selection 批量打分必需，否则每候选全表扫描）。"""
    key = (lottery, str(issue))
    if key in _REC_CACHE:
        return _REC_CACHE[key]
    rec = None
    try:
        from data.loader import load_lottery
        data = load_lottery(lottery)
        rec = next((r for r in data.records if str(r.期号) == str(issue)), None)
    except Exception as e:
        logger.debug(f"[payout] 取 {lottery} 期{issue} 记录失败: {e}")
    _REC_CACHE[key] = rec
    return rec


def _grade_pool(lottery: str, issue: Any, grade: str) -> Optional[Tuple[float, float]]:
    """(可分配池, 当期票量)：池 = {grade}奖奖金 × {grade}奖注数（当期真实值）。"""
    rec = _record_for_issue(lottery, issue)
    if rec is None:
        return None
    try:
        prize = getattr(rec, f"{grade}奖奖金", None)
        winners = getattr(rec, f"{grade}奖注数", None)
        sales = getattr(rec, "总投注额", None)
        if prize and winners and winners > 0 and sales:
            return float(prize) * float(winners), float(sales) / 2.0
    except Exception as e:
        logger.debug(f"[payout] 取 {lottery} 期{issue} {grade}奖池失败: {e}")
    return None


def pool_share_expected(pool: float, mu: float, cap: float = _SSQ_DLT_CAP) -> float:
    """
    E[min(cap, 池/(1+X))]，X~Poisson(mu) —— 「我中头奖时的期望实得」精确式。

    ⚠️ 必须显式建模封顶：奖池巨厚时（池 ≫ cap×μ）几乎所有情形都被 1000 万封顶
       吃掉，冷门优势被压缩——朴素式 池×E[1/(1+X)] 会系统性高估冷门收益。
    """
    if mu <= 0:
        return min(cap, pool)
    from scipy.stats import poisson as _pois
    xmax = int(max(mu + 10.0 * math.sqrt(mu) + 20.0, 60))
    xs = np.arange(0, xmax + 1)
    pmf = _pois.pmf(xs, mu)
    vals = np.minimum(cap, pool / (1.0 + xs))
    return float((pmf * vals).sum())


def pool_share_expected_batch(pool: float, mus: np.ndarray,
                              cap: float = _SSQ_DLT_CAP) -> np.ndarray:
    """
    pool_share_expected 的向量化批量版：一次算一批 μ（选号器 2 万候选必需，
    逐注调 scipy poisson.pmf 是纯浪费）。返回与 mus 同形的实得数组。
    """
    from scipy.stats import poisson as _pois
    mus = np.asarray(mus, dtype=np.float64)
    out = np.full(mus.shape, float(min(cap, pool)))
    valid = np.isfinite(mus) & (mus > 0)
    if not valid.any():
        return out
    mv = mus[valid]
    # 公共网格取全局最大 μ（泊松尾部：|X-max| 超过 10σ 的概率可忽略）
    xmax = int(max(mv.max() + 10.0 * math.sqrt(mv.max()) + 20.0, 60))
    xs = np.arange(0, xmax + 1)
    vals = np.minimum(cap, pool / (1.0 + xs))            # (G,)
    # 分块算 pmf，控内存（2 万 × 2000 网格 ≈ 320MB，分块 4096 行）
    chunk = 4096
    for i in range(0, len(mv), chunk):
        block = mv[i:i + chunk][:, None]                 # (b,1)
        pmf = _pois.pmf(xs[None, :], block)              # (b,G)
        out[np.where(valid)[0][i:i + chunk]] = (pmf * vals[None, :]).sum(axis=1)
    return out


def pool_share_expected_table(pools, mus, cap: float = _SSQ_DLT_CAP) -> np.ndarray:
    """
    「每期各自奖池」的批量版：E[min(cap, pool_i/(1+X))]，X_i~Poisson(mu_i)。
    反事实回测 3292 期 × 2 组合一次性算完（逐期调 scipy 是回测的主要耗时）。
    """
    from scipy.stats import poisson as _pois
    pools = np.asarray(pools, dtype=np.float64)
    mus = np.asarray(mus, dtype=np.float64)
    out = np.minimum(cap, np.where(pools > 0, pools, 0.0))   # mu<=0 → 独享
    valid = np.isfinite(mus) & (mus > 0) & np.isfinite(pools) & (pools > 0)
    if not valid.any():
        return out
    mv = mus[valid]
    xmax = int(max(mv.max() + 10.0 * math.sqrt(mv.max()) + 20.0, 60))
    xs = np.arange(0, xmax + 1)
    pmf = _pois.pmf(xs[None, :], mv[:, None])                # (n, G)
    vals = np.minimum(cap, pools[valid, None] / (1.0 + xs[None, :]))
    out[valid] = (pmf * vals).sum(axis=1)
    return out


def pool_based_eff(lottery: str, reds: List[int], grade: str, p_grade: float,
                   pool: float, N: float) -> Optional[Tuple[float, float, str]]:
    """
    流行度模型路径：实得 = E[min(cap, 池/(1+X))]，X~Poisson(μ)，
    μ = N × p_grade × exp(β·f(组合))（号码相关 → 冷门实得高）。
    返回 (实得单注, 分薄乘数, 来源说明)；模型不可用返回 None。
    """
    try:
        from ev.popularity import get_popularity_model, ticket_mu
        params = get_popularity_model(lottery)
        if not params.get("go"):
            return None
        mu = ticket_mu(lottery, reds, N, p_grade, params)
        if mu <= 0:
            return None
        eff = pool_share_expected(pool, mu)
        share = eff / pool if pool > 0 else 1.0
        return (eff, share,
                f"池{pool:,.0f}×E[min(封顶,1/(1+X))]，X~Poisson(μ={mu:.2f})【流行度模型】")
    except Exception as e:
        logger.debug(f"[payout] 流行度路径失败 {lottery}: {e}")
        return None


# ---------------- 主接口 ----------------
def payout_eff(lottery: str, ticket, issue: Any = None,
               total_tickets: Optional[float] = None) -> Dict[str, Any]:
    """
    实得奖金统一口径。

    参数
      lottery  彩种名
      ticket   号码（见 normalize_ticket）
      issue    期号（缺省=最新一期；用于取「当期实际单注奖金」）
      total_tickets 当期总注数（缺省自动探测）

    返回
      {
        "彩种", "期号", "号码",
        "奖级明细": [{"奖级","概率","名义单注","实得单注","浮动","分薄乘数","来源","贡献"}...],
        "名义期望": float,   # Σ p × 名义（未分薄）
        "实得期望": float,   # Σ p × 实得（已分薄）
        "净EV": float,       # 实得期望 − 2
        "成本": 2.0,
        "分薄": {"乘数":.., "来源":..},
        "备注": [str...]
      }
    """
    if lottery not in _LOTTERY:
        return {"error": f"不支持的彩种: {lottery}（可选 {_LOTTERY}）"}

    try:
        tk = normalize_ticket(lottery, ticket)
    except Exception as e:
        return {"error": f"号码解析失败: {e}"}

    # 期号解析（浮动奖级取「当期实际单注奖金」必需）
    issue = _resolve_issue(lottery, issue)

    probs = grade_probabilities(lottery, tk)
    mult, mult_src = share_multiplier(lottery, ticket, issue, total_tickets)
    notes: List[str] = []

    # 双/大 + 流行度模型 GO → 浮动奖走「奖池×E[1/(1+X)]」号码相关路径
    use_pool_path = {}
    if lottery in _REDBLUE:
        from ev.popularity import get_popularity_model
        if get_popularity_model(lottery).get("go"):
            use_pool_path = {"一等", "二等"}

    rows: List[Dict[str, Any]] = []
    nom_exp = 0.0
    eff_exp = 0.0
    pool_path_hits = []
    for grade, p in probs.items():
        base, src = base_payout(lottery, grade, issue)
        nominal = base if base is not None else None
        # 浮动奖级才乘分薄；固定奖级恒不乘
        floating = src not in ("固定奖金",)
        eff = None
        row_mult = mult
        row_src = src
        if base is not None:
            eff = base * mult if floating else base
        # 号码相关路径：有可分配池时，实得 = 池 × E[1/(1+X)]（冷门高、热门低）
        if grade in use_pool_path and tk["红球"]:
            gp = _grade_pool(lottery, issue, grade)
            if gp:
                r = pool_based_eff(lottery, tk["红球"], grade, p, gp[0], gp[1])
                if r:
                    eff, row_mult, row_src = r[0], r[1], r[2]
                    pool_path_hits.append(grade)
        if base is None:
            notes.append(f"{grade}：{src} → 未计入（低估）")
        rows.append({
            "奖级": grade,
            "概率": p,
            "名义单注": nominal,
            "实得单注": None if eff is None else round(eff, 2),
            "浮动": floating,
            "分薄乘数": round(row_mult, 6),
            "来源": row_src,
            "贡献": None if eff is None else round(p * eff, 8),
        })
        if nominal is not None:
            nom_exp += p * nominal
        if eff is not None:
            eff_exp += p * eff

    if pool_path_hits:
        mult_src = (f"流行度模型路径（{'/'.join(pool_path_hits)}：奖池×E[1/(1+X)]，"
                    f"号码相关；其余奖级仍为 {mult_src}）")

    return {
        "彩种": lottery,
        "期号": issue,
        "号码": tk["号码"],
        "奖级明细": rows,
        "名义期望": round(nom_exp, 6),
        "实得期望": round(eff_exp, 6),
        "净EV": round(eff_exp - COST, 6),
        "成本": COST,
        "分薄": {"乘数": round(mult, 6), "来源": mult_src},
        "备注": notes or ["口径完整"],
    }


def expected_payout(lottery: str, ticket, issue: Any = None,
                    total_tickets: Optional[float] = None) -> float:
    """便捷接口：实得期望（元/注）。出错返回 nan。"""
    r = payout_eff(lottery, ticket, issue, total_tickets)
    if "error" in r:
        return float("nan")
    return float(r["实得期望"])


def net_ev_payout(lottery: str, ticket, issue: Any = None,
                  total_tickets: Optional[float] = None) -> float:
    """便捷接口：净 EV（元/注）。出错返回 nan。"""
    r = payout_eff(lottery, ticket, issue, total_tickets)
    if "error" in r:
        return float("nan")
    return float(r["净EV"])


if __name__ == "__main__":
    import json
    import sys

    demo = [
        ("双色球", {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}, None),
        ("大乐透", {"红球": [1, 2, 3, 4, 5], "蓝球": [6, 7]}, None),
        ("排列5", "12345", None),
        ("福彩3D", "123", None),
        ("七星彩", "1234567", None),
    ]
    names = sys.argv[1:]
    for lot, tk, iss in demo:
        if names and lot not in names:
            continue
        print(json.dumps(payout_eff(lot, tk, iss), ensure_ascii=False, indent=1))
