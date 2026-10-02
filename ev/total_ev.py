"""
ev/total_ev.py —— 总 EV 折算对照表（策略升级 D）

回答的问题：「冷门策略到底把总 EV 提升了几个百分点？」——把反事实回测给出的
头奖实得比（1.484×，历史中位）折算到**完整单注 EV**，逐奖级拆开：

  总EV = −2 + P(一等)×头奖单注实得 + Σ固定小奖 P×赔付

三口径（对头奖单注实得分别取）：
  ① 名义      —— P×名义参考（FLOATING_REF 一等 1000 万），无分薄无封顶挤压
  ② λ 均摊    —— min(cap, 池可分池/λ)，λ=N×P(一等)：随机号面对的平均分摊
                 （= ev/rollover.py 的头奖口径，本表 λ 列与其总EV 精确对齐）
  ③ 冷/热门口径 —— E[min(cap, 池可分池/(1+X))]，X~Poisson(μ_q)，
                 μ_q = N×P(一等)×exp(β·f(q 分位组合))（流行度模型）

诚实定位（写死，防误读）：
- 不预测号码、不改变任何奖级的中奖概率（概率是组合常数）；
- 冷门口径只回答「同样概率下中了能拿多少」；
- ΔEV 的前提是流行度模型历史回测成立（1.484× 为历史中位，非承诺）；
- 封顶 1000 万下总 EV 恒为负，本表是口径对照工具不是收益工具。
- 二等奖浮动但未纳入本表（与 rollover 口径一致），逐注口径见 payout_eff。

适用性：
  双色球  ✅ 全口径（WP1 流行度 GO）
  大乐透  ⚠️ WP1 OOS 未达门槛（诚实否决）→ 只出 名义/λ 均摊 两列
  七星彩  ❌ 机制不同（按位匹配浮动头奖），见 ev.engine.net_ev 的「限号EV」
  数字型  ❌ 固定赔率撞号不分薄，EV 恒定，无口径可对照
"""
import logging
from typing import Any, Dict, Optional

from ev.payout import _SSQ_DLT_CAP

logger = logging.getLogger(__name__)

_HONEST_NOTE = (
    "不预测号码、不改任何奖级中奖概率（概率是组合常数）；冷门口径只回答"
    "「同样概率下中了能拿多少」；ΔEV 的前提是流行度模型历史回测成立"
    "（1.484× 为历史中位，非承诺）；封顶 1000 万下总 EV 恒为负，"
    "本表是口径对照工具不是收益工具。"
)

_LOTTO = ("双色球", "大乐透")


def _latest_row(lottery: str, issue: Optional[str]):
    """从 rollover 的历史 CSV 取一期（延迟 import，防环）。"""
    from ev.rollover import _load_history
    df = _load_history(lottery)
    if issue is None:
        return df.iloc[0]
    hit = df[df["期号"] == str(issue)]
    return hit.iloc[0] if not hit.empty else None


def _cold_hot_mu(lottery: str, n_tickets: float, p1: float,
                 q_cold: float, q_hot: float):
    """返回 (μ_cold, μ_hot, 降级原因|None)。模型不可用/未 Go → (None, None, 原因)。"""
    try:
        from ev.popularity import cold_hot_combo, features_matrix, get_popularity_model
        import numpy as np

        params = get_popularity_model(lottery)
        if not params.get("go"):
            return None, None, "流行度模型未达 Go 门槛 → 冷/热门口径不具解释力（诚实否决），仅 λ 均摊口径"
        ch = cold_hot_combo(lottery, q_cold, q_hot)
        if ch is None:
            return None, None, "冷/热参考组合不可得 → 仅 λ 均摊口径"
        combo_c, combo_h = ch
        F = features_matrix(np.array([combo_c, combo_h], dtype=np.int16))
        z = (F - params["m_"]) / params["s_"]
        k = np.exp(z @ params["beta"])
        mu_cold = float(n_tickets * p1 * k[0])
        mu_hot = float(n_tickets * p1 * k[1])
        if not (mu_cold > 0 and mu_hot > 0):
            return None, None, "μ 估计非正（数据异常）→ 仅 λ 均摊口径"
        return mu_cold, mu_hot, None
    except Exception as e:  # 降级不许静默：原因进 note
        logger.debug(f"[total_ev] 冷/热 μ 估计失败 {lottery}: {e}")
        return None, None, f"流行度模型不可用({type(e).__name__}) → 仅 λ 均摊口径"


def prize_split_table(lottery: str, issue: Optional[str] = None,
                      pool: Optional[float] = None, n_tickets: Optional[float] = None,
                      q_cold: float = 0.1, q_hot: float = 0.9,
                      cap: float = _SSQ_DLT_CAP, pool_share: float = 0.7) -> Dict[str, Any]:
    """
    逐奖级三口径对照表。

    pool / n_tickets 显式传入时直接用（测试/外部数据）；缺省从 rollover 历史
    CSV 取最新期（或 issue 指定期）。
    """
    if lottery == "七星彩":
        return {"彩种": lottery, "applicable": False,
                "note": "七星彩机制不同（按位匹配浮动头奖），请用 `cli.py ev 七星彩` 的限号EV口径（ev.engine.net_ev）",
                "诚实边界": _HONEST_NOTE}
    if lottery not in _LOTTO:
        return {"彩种": lottery, "applicable": False,
                "note": "固定赔率彩种：撞号不分薄、单注 EV 恒定，无号码相关口径可对照",
                "诚实边界": _HONEST_NOTE}

    from ev.rollover import _FIXED_PRIZES, _P1, fixed_prize_ev

    row = None
    if pool is None or n_tickets is None:
        row = _latest_row(lottery, issue)
        if row is None:
            return {"彩种": lottery, "applicable": False,
                    "note": f"未找到期号 {issue}", "诚实边界": _HONEST_NOTE}
        if pool is None:
            pool = float(row["奖池奖金"]) if row.get("奖池奖金") == row.get("奖池奖金") else 0.0
        if n_tickets is None:
            sales = float(row["总投注额"]) if row.get("总投注额") == row.get("总投注额") else 0.0
            n_tickets = sales / 2.0 if sales else 0.0

    p1 = _P1[lottery]
    lam = n_tickets * p1
    head_pool = pool * pool_share if pool > 0 else 0.0

    # λ 均摊口径（与 ev/rollover.rollover_ev 的头奖单注估计精确同式）
    lam_unit = min(cap, head_pool / max(lam, 1e-9)) if head_pool > 0 else 0.0

    mu_cold, mu_hot, degrade = _cold_hot_mu(lottery, n_tickets, p1, q_cold, q_hot)
    from ev.payout import pool_share_expected
    cold_unit = (pool_share_expected(head_pool, mu_cold, cap)
                 if (mu_cold is not None and head_pool > 0) else None)
    hot_unit = (pool_share_expected(head_pool, mu_hot, cap)
                if (mu_hot is not None and head_pool > 0) else None)

    # 名义参考（一等）
    from data.prize_table import FLOATING_REF
    nominal_1 = FLOATING_REF.get(lottery, {}).get("一等") or cap

    rows: list = [{
        "奖级": "一等", "概率": p1, "名义单注": nominal_1, "浮动": True,
        "λ均摊单注": round(lam_unit, 2),
        "冷门口径单注": round(cold_unit, 2) if cold_unit is not None else None,
        "热门口径单注": round(hot_unit, 2) if hot_unit is not None else None,
        "贡献_λ": p1 * lam_unit,
        "贡献_冷门": p1 * cold_unit if cold_unit is not None else None,
        "贡献_热门": p1 * hot_unit if hot_unit is not None else None,
        "备注": (f"μ_冷={mu_cold:.2f}/μ_热={mu_hot:.2f}（N×p1×exp(β·f(q分位))）"
                 if mu_cold is not None else "冷/热口径不可用"),
    }]
    for grade, (payout, p) in _FIXED_PRIZES[lottery].items():
        rows.append({
            "奖级": grade, "概率": p, "名义单注": payout, "浮动": False,
            "λ均摊单注": payout, "冷门口径单注": payout, "热门口径单注": payout,
            "贡献_λ": payout * p, "贡献_冷门": payout * p, "贡献_热门": payout * p,
            "备注": "固定奖，不随选号口径变化（期望线性性）",
        })

    ev_nominal = -2.0 + nominal_1 * p1 + fixed_prize_ev(lottery)
    ev_lambda = -2.0 + sum(r["贡献_λ"] for r in rows)
    ev_cold = (-2.0 + sum(r["贡献_冷门"] for r in rows)) if cold_unit is not None else None
    ev_hot = (-2.0 + sum(r["贡献_热门"] for r in rows)) if hot_unit is not None else None

    return {
        "彩种": lottery,
        "期号": str(row["期号"]) if row is not None else None,
        "奖池": round(pool, 2),
        "总注数": int(n_tickets),
        "口径说明": ("总EV = −2 + P(一等)×头奖单注实得 + Σ固定小奖；头奖单注实得按"
                     "名义 / λ均摊 / 冷门口径 三种口径对照（二等奖浮动未纳入，见 payout_eff）"),
        "分摊口径": ("μ_冷门/μ_热门(流行度GO)" if mu_cold is not None else "λ均摊(回退)"),
        "逐奖级表": rows,
        "EV汇总": {
            "名义": round(ev_nominal, 6),
            "λ均摊": round(ev_lambda, 6),
            "冷门口径": round(ev_cold, 6) if ev_cold is not None else None,
            "热门口径": round(ev_hot, 6) if ev_hot is not None else None,
            "ΔEV_冷门_vs_热门": (round(ev_cold - ev_hot, 6)
                                if ev_cold is not None and ev_hot is not None else None),
            "ΔEV_冷门_vs_λ": (round(ev_cold - ev_lambda, 6)
                              if ev_cold is not None else None),
        },
        "历史提升倍数参考": None,   # 由 total_ev_compare 回填（反事实回测较重，本函数不触发）
        "诚实边界": _HONEST_NOTE,
        "applicable": True,
        **({"note": degrade} if degrade else {}),
    }


def total_ev_compare(lottery: str, q_cold: float = 0.1, q_hot: float = 0.9) -> Dict[str, Any]:
    """
    最新期对照表 + 反事实回测旁证 + 分档结论（CLI/Web 对外入口）。
    """
    out = prize_split_table(lottery, q_cold=q_cold, q_hot=q_hot)
    if not out.get("applicable"):
        return out

    try:
        from ev.popularity import counterfactual_backtest
        bt = counterfactual_backtest(lottery, q_cold, q_hot)
        if bt.get("go"):
            out["历史提升倍数参考"] = bt.get("实得提升倍数_中位")
    except Exception as e:
        logger.debug(f"[total_ev] 反事实回测旁证失败 {lottery}: {e}")

    evs = out["EV汇总"]
    d_lam = evs.get("ΔEV_冷门_vs_λ")
    if d_lam is None:
        out["分档结论"] = "冷门口径不可用（流行度模型未 Go），仅 λ 均摊口径"
    elif d_lam <= 0:
        out["分档结论"] = "冷门口径未优于 λ 均摊（本期池/模型组合下无口径优势）"
    elif abs(d_lam) < 0.01:
        out["分档结论"] = f"冷门口径优势 {d_lam:+.4f} 元/注（分级）——方差管理工具，非收益工具"
    elif abs(d_lam) < 0.1:
        out["分档结论"] = f"冷门口径优势 {d_lam:+.4f} 元/注（角级）——总 EV 仍为负"
    else:
        out["分档结论"] = f"冷门口径优势 {d_lam:+.4f} 元/注——总 EV 仍为负，不构成买入理由"
    return out
