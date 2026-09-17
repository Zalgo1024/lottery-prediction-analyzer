"""
ev/engine.py —— 单注净 EV（名义 EV + 限号/撞号修正 EV）

⚠️ 机制修正（2026-08-25 官方规则核实，财办综[2008]47号 + 体彩官网）：
- 排列3/福彩3D/排列5 是【固定赔率】：单注奖金固定，多人撞号不减少每注赔付。
- 限号 = 某号码投注数达到动态限额（≈ 销售额×49%/单注奖金）后【停止销售该号码】，
  并随销量上升【滚动放号】——对 2 元小额购彩基本无影响，只约束大额/倍投。
- 因此纯固定赔率彩种【单注 EV 恒定】（直选 -0.96/-1.0 元），选号不改变期望；
  唯一真实的结构性边在【七星彩浮动头奖】：一等奖按注数分薄奖池，
  大众号中头奖被多人分摊 → 单注奖金低；冷门号中头奖 → 独享 → 单注奖金高。

本模块据此建模：
- 排列3/福彩3D/排列5：net_ev = 名义 EV（恒定），附加「限号距离」报告（该号距停售阈值）。
- 七星彩：net_ev = 名义 EV + 撞号修正（当期实际头奖单注奖金 × 拥挤修正系数）。
- 无销售数据/无 crowd 模型 → 降级为名义 EV 并标注。
"""
import logging

import numpy as np

from .rulebook import nominal_ev, get_rulebook
from .crowd import fit_crowd_model, CrowdModel
from .crowd_model import fit_crowd_v2, CrowdV2

logger = logging.getLogger(__name__)

_CROWD_CACHE = {}
_SALES_CACHE = {}


def _get_crowd(name: str):
    """人群模型：优先 v2（全奖级注数 Poisson MLE，七星彩可用）；退化为 v1。

    v1 位级加性最小二乘只吃单一奖级注数，七星彩一等奖过稀疏 → None。
    v2（crowd_model.py）用全部奖级注数反推 π，四类数字彩均可拟合。
    """
    if name not in _CROWD_CACHE:
        m = fit_crowd_v2(name)
        if m is None:
            m = fit_crowd_model(name)
        _CROWD_CACHE[name] = m
    return _CROWD_CACHE[name]


def _load_sales(name: str):
    if name not in _SALES_CACHE:
        from data.fetch_sales import load_sales
        _SALES_CACHE[name] = load_sales(name)
    return _SALES_CACHE[name]


def _resolve_issue(name: str, issue: str):
    """缺省取最新一期；返回当期 DataFrame 行 dict"""
    df = _load_sales(name)
    if df is None or df.empty:
        return None, None
    if issue is None:
        row = df.iloc[-1]
    else:
        hit = df[df["期号"] == str(issue)]
        if hit.empty:
            return None, None
        row = hit.iloc[-1]
    return df, row


def _issue_prize_overrides(name: str, row) -> dict:
    """当期实际单注奖金（覆盖浮动奖级）。
    七星彩一等奖：当期未中出(单注奖金缺失)但奖池充足时 → 用名义上限 500 万；
    二等奖缺失 → 用历史近值/0（标注）。"""
    ov = {}
    if name == "七星彩":
        for k in ("一等奖", "二等奖"):
            col = f"{k}单注奖金"
            v = row.get(col)
            if v is not None and v == v and v > 0:  # notna & >0
                ov[k] = float(v)
        # 一等奖未中出 → 奖池充足则按 500 万名义上限（中奖时可拿满）
        if "一等奖" not in ov:
            pool = row.get("奖池")
            if pool is not None and pool == pool and float(pool) >= 5_000_000:
                ov["一等奖"] = 5_000_000.0
        # 二等奖未中出 → 用最近有值期（跨期近似，标注在备注）
        if "二等奖" not in ov:
            try:
                df = _load_sales(name)
                if df is not None and "二等奖单注奖金" in df.columns:
                    hist = df[df["二等奖单注奖金"] > 0]["二等奖单注奖金"]
                    if not hist.empty:
                        ov["二等奖"] = float(hist.iloc[-1])
            except Exception:
                pass
    return ov


def net_ev(name: str, ticket, issue: str = None, cap_ratio: float = 0.5,
           hot_penalty: float = 9.0) -> dict:
    """
    单注净 EV。
    ticket: [d1, d2, ...] 号码列表（直选玩法；3D 类=3 位，排列5=5 位，七星彩=7 位）。
    issue: 期号（默认最新一期）。
    cap_ratio: 限号赔付能力系数（销售额×cap_ratio 为赔付池，v1 报告用）。
    hot_penalty: 七星彩拥挤修正强度（拥挤分位 q → 头奖单注修正 = /(1+hot_penalty×q)）。
    返回 {"名义EV", "限号EV", "限号暴露概率", "机制", "详情"}
    """
    rb = get_rulebook(name)
    if name == "七星彩":
        return _net_ev_qxc(name, ticket, issue, hot_penalty)
    if name in ("排列3", "福彩3D", "排列5"):
        return _net_ev_fixed(name, ticket, issue, cap_ratio)
    raise ValueError(f"不支持的彩种: {name}")


# ---------------- 七星彩：浮动头奖撞号修正 ----------------
def _net_ev_qxc(name: str, ticket, issue: str, hot_penalty: float) -> dict:
    rb = get_rulebook(name)
    df, row = _resolve_issue(name, issue)
    overrides = _issue_prize_overrides(name, row) if row is not None else None
    nom = nominal_ev(name, prize_payout_override=overrides)

    if row is None:
        return _degraded(name, nom, "无销售数据，按名义EV（默认头奖500万上限）")

    model = _get_crowd(name)

    # —— crowd v2（Poisson 同号期望 → E[1/(1+X)] 期望分薄乘数）——
    if isinstance(model, CrowdV2):
        sv = row.get("销售额")
        if sv is None or (isinstance(sv, float) and np.isnan(sv)):
            return _degraded(name, nom, "有 crowd v2 但缺当期销售额，无法估同号人数，按名义EV")
        total = float(sv) / 2.0
        mu = model.expected_co_winners(ticket, total)
        mult = model.share_multiplier(ticket, total)
        jz = overrides.get("一等奖") if overrides else None
        if jz is None:
            return _degraded(name, nom, "有 crowd v2 但无当期头奖基准奖金，按名义EV")
        jz_adj = jz * mult
        overrides_adj = dict(overrides or {})
        overrides_adj["一等奖"] = jz_adj
        ev_adj = -2 + sum(
            (overrides_adj.get(k, v["payout"]) * v["prob"] for k, v in rb.items()),
        )
        return {
            "名义EV": round(nom["ev_per_ticket"], 4),
            "限号EV": round(ev_adj, 4),
            "限号暴露概率": round(mult, 4),
            "机制": "七星彩浮动头奖撞号分薄：头奖单注=当期基准×E[1/(1+X)], X~Poisson(同号期望)",
            "详情": {
                "头奖单注基准": round(jz, 2),
                "同号期望注数μ": round(mu, 3),
                # ⚠️ 键名与 scan_issues 行键统一为"分薄乘数(≈1=独享)"（防精确匹配踩坑）
                "分薄乘数(≈1=独享)": round(mult, 4),
                "修正后头奖单注": round(jz_adj, 2),
            },
            "备注": (f"μ≈0（冷门号）→ 乘数≈1 独享；μ 大（大众号）→ 期望被多人分摊。"
                     f"当期票量 {total:,.0f} 注（销售额 {sv:,.0f} 元）"),
        }

    # —— 无 crowd 或 v1 模型：原降级 / 拥挤分位启发式 ——
    if model is None:
        # 无 crowd：头奖用当期实际单注奖金（读 CSV），无撞号修正
        actual = overrides.get("一等奖")
        if actual is None:
            return _degraded(name, nom, "有销售数据但无当期头奖奖金，按名义EV")
        ev = -2 + sum(
            (overrides.get(k, v["payout"]) * v["prob"] for k, v in rb.items()),
        )
        return {
            "名义EV": round(ev, 4),
            "限号EV": round(ev, 4),
            "限号暴露概率": None,
            "机制": "七星彩浮动头奖（当期实际单注奖金，无撞号修正）",
            "详情": {"头奖单注奖金": actual, "拥挤修正": 1.0},
            "备注": "crowd 模型不可用（一等奖注数过稀疏），撞号修正关闭",
        }

    # 撞号修正（v1 启发式）：拥挤度 q → 头奖单注奖金 ÷ (1+hot_penalty×q)
    q = model.crowding_quantile(ticket)
    jz = overrides.get("一等奖") if overrides else None
    if jz is None:
        return _degraded(name, nom, "有 crowd 但无当期头奖奖金，按名义EV")
    jz_adj = jz / (1.0 + hot_penalty * q)
    overrides_adj = dict(overrides)
    overrides_adj["一等奖"] = jz_adj
    ev_adj = -2 + sum(
        (overrides_adj.get(k, v["payout"]) * v["prob"] for k, v in rb.items()),
    )
    return {
        "名义EV": round(nom["ev_per_ticket"], 4),
        "限号EV": round(ev_adj, 4),
        "限号暴露概率": round(q, 4),
        "机制": "七星彩浮动头奖撞号修正：头奖单注=当期实际÷(1+9×拥挤分位)",
        "详情": {
            "头奖单注奖金": jz,
            "拥挤分位": round(q, 4),
            "修正后头奖单注": round(jz_adj, 2),
        },
        "备注": "拥挤分位>0.8=大众号（中头奖被分摊），<0.2=冷门号（独享概率高）",
    }


# ---------------- 排列3/福彩3D/排列5：固定赔率 + 限号距离 ----------------
def _net_ev_fixed(name: str, ticket, issue: str, cap_ratio: float) -> dict:
    df, row = _resolve_issue(name, issue)
    nom = nominal_ev(name)
    rb = get_rulebook(name)
    main_prize = "直选"

    if row is None:
        return _degraded(name, nom, "无销售数据，按名义EV")

    # 限号距离：该号估计被买注数 vs 官方动态限额（≈ 销售额×49%/单注奖金）
    sales = float(row["销售额"]) if row.get("销售额") == row.get("销售额") else None
    model = _get_crowd(name)
    limit_note = None
    exposure = None
    if sales and model is not None:
        total = sales / 2.0
        n_x = model.expected_sales(ticket, total)
        # ⚠️ 单注奖金必须按彩种取（3D/排3=1040，排列5=100000），曾硬编码 1040
        # 导致排列5 限号额度被高估 ~96 倍、暴露概率系统性低估（2026-08-31 修复）
        payout_main = rb[main_prize]["payout"]
        limit_49 = max(1.0, sales * 0.49 / payout_main)  # 官方公式主要项
        # 该号估计占限号额度比例：若 n_x 接近 limit → 可能停售
        ratio = n_x / limit_49 if limit_49 else 0.0
        exposure = min(1.0, max(0.0, ratio))
        q = model.crowding_quantile(ticket)
        limit_note = (f"该号估计被买 {n_x:,.0f} 注，限号额度≈{limit_49:,.0f}注 "
                      f"（{ratio*100:.0f}%），拥挤分位 {q:.2f}")
    elif sales:
        limit_note = "无 crowd 模型，限号距离不可估（七星彩之外）"

    return {
        "名义EV": nom["ev_per_ticket"] if isinstance(nom["ev_per_ticket"], float)
                  else nom["ev_per_ticket"][main_prize],
        "限号EV": nom["ev_per_ticket"] if isinstance(nom["ev_per_ticket"], float)
                  else nom["ev_per_ticket"][main_prize],
        "限号暴露概率": exposure,
        "机制": "固定赔率：单注EV恒定，限号=停售（滚动放号），撞号不分薄赔付",
        "详情": {"玩法": main_prize, "限号报告": limit_note},
        "备注": "固定赔率彩种选号不改变单注期望；限号只影响大额/倍投可购买性",
    }


def _degraded(name: str, nom, note: str) -> dict:
    ev = nom["ev_per_ticket"] if isinstance(nom["ev_per_ticket"], float) else None
    if ev is None and name not in ("排列3", "福彩3D", "排列5"):
        ev = None
    return {
        "名义EV": nom["ev_per_ticket"] if isinstance(nom["ev_per_ticket"], float)
                  else (nom["ev_per_ticket"].get("直选") if isinstance(nom["ev_per_ticket"], dict) else None),
        "限号EV": nom["ev_per_ticket"] if isinstance(nom["ev_per_ticket"], float)
                  else (nom["ev_per_ticket"].get("直选") if isinstance(nom["ev_per_ticket"], dict) else None),
        "限号暴露概率": None,
        "机制": "降级：名义EV",
        "详情": {},
        "备注": note,
    }


def scan_issues(name: str, issue: str = None, top_n: int = 10) -> dict:
    """
    扫描全部候选号码的限号EV（排列3/3D: 1000，排列5: 100000 抽样），
    输出「冷门号 TOP」——限号EV 最高（七星彩）或限号距离最远（固定赔率）的号码。
    """
    model = _get_crowd(name)
    if name in ("排列3", "福彩3D"):
        cands = [(i, [i // 100 % 10, i // 10 % 10, i % 10]) for i in range(1000)]
    elif name == "排列5":
        cands = [(i, [i // 10000 % 10, i // 1000 % 10, i // 100 % 10, i // 10 % 10, i % 10])
                 for i in range(0, 100000, 10)]  # 抽样 1/10，约 1 万候选
    elif name == "七星彩":
        # ⚠️ 第7位范围 0-14（非 0-9）：十进制枚举漏掉 10-14，须按位采样。
        # v2 用 coldest(候选池)（20万采样 argsort，覆盖第7位 10-14 冷区）；
        # 非 v2（退化）回退为按位随机采样。
        if isinstance(model, CrowdV2):
            cands = [(j, nums) for j, (nums, _s) in enumerate(model.coldest(max(top_n * 20, 200)))]
        else:
            rng = np.random.default_rng(2026)
            n_cand = 4000
            sample = np.hstack([rng.integers(0, 10, size=(n_cand, 6)),
                                rng.integers(0, 15, size=(n_cand, 1))])
            cands = [(j, sample[j].tolist()) for j in range(n_cand)]
    else:
        return {"error": f"不支持的彩种: {name}"}

    results = []
    # v2 的"暴露概率"=同号分薄乘数 E[1/(1+X)]（≈1 冷门独享）；v1/降级=拥挤分位
    crowd_field = "分薄乘数(≈1=独享)" if isinstance(model, CrowdV2) else "拥挤分位"
    for idx, nums in cands:
        r = net_ev(name, nums, issue=issue)
        ev = r["限号EV"]
        if isinstance(ev, dict):
            ev = ev.get("直选")
        if ev is None:
            continue
        results.append({"号码": "".join(map(str, nums)), "限号EV": round(float(ev), 4),
                        crowd_field: r.get("限号暴露概率"),
                        "备注": r.get("备注", "")})
    results.sort(key=lambda x: x["限号EV"], reverse=True)
    return {"彩种": name, "期号": issue, "TOP冷门(限号EV最高)": results[:top_n]}
