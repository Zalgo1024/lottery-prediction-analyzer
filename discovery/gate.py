"""
discovery/gate.py —— 闸门引擎

对登记表里的假设判卷，只有真正穿越四道闸的才 promoted（准入 live 策略集）。

两种探测方式：
1. credibility_gate：调裁判层 run_credibility（OOS回测+随机军团+FDR+贝叶斯），
   对被检验策略逐条判四道闸。适用于「号码策略是否显著优于随机」类假设。
2. ev_edge：用 ev/engine 对比冷门号 vs 热门号的限号EV 在最近窗口的差距一致性，
   适用于「选号是否带来可重复 EV 边」类假设（数字型；固定赔率预期恒为无差别）。

输出：gate_manifest（training/hypotheses/{彩种}_gate_manifest.json）——P0 只产出清单，
不直接改写策略权重（权重仍由 data.feedback 按真实 P/L 驱动，闸门只设准入线）。
"""
import json
import logging
from datetime import datetime
from pathlib import Path

from .hypothesis_registry import (
    HYP_DIR,
    list_hypotheses,
    record_test,
    DEFAULT_GATE,
)

logger = logging.getLogger(__name__)


def _manifest_path(lottery: str) -> Path:
    return HYP_DIR / f"{lottery}_gate_manifest.json"


# ============================================================
# 探测方式一：裁判层四道闸（号码策略类假设）
# ============================================================
def _credibility_gate(lottery: str, hyp: dict, k: int, min_train: int,
                      army_size: int, fdr_alpha: float, bayes_tau: float,
                      bayes_p_edge: float, no_bayes: bool,
                      cred_result: dict = None) -> dict:
    if cred_result is None:
        from credibility.report import run_credibility
        cred_result = run_credibility(lottery, k=k, min_train=min_train, army_size=army_size,
                                      fdr_alpha=fdr_alpha, bayes_tau=bayes_tau,
                                      bayes_p_edge=bayes_p_edge, no_bayes=no_bayes)
    r = cred_result

    strategy = hyp["probe"].get("strategy")
    sr = next((s for s in r["strategy_results"] if s["策略"] == strategy), None)
    if sr is None:
        # 裁判层未测该策略（如用户自创策略名）——诚实降级，不能判"通过"
        return {
            "passed": False,
            "reasons": [f"裁判层未测试策略「{strategy}」（可测: 高频/遗漏/区间），无法判卷"],
            "metrics": {},
            "verdict": r["verdict"],
            "sample_n": r.get("n_records"),
        }

    g = hyp["probe"].get("gate_criteria") or DEFAULT_GATE
    reasons = []

    # 闸1：OOS 击败随机基线军团（FDR 校正后）
    if g.get("oos_vs_random") and not sr.get("显著优于随机"):
        reasons.append(f"未显著优于随机基线军团（q={sr.get('FDR校正q')}）")
    # 闸2：FDR 校正 q 达标
    if sr.get("FDR校正q", 1.0) > g.get("fdr_q_max", 0.05):
        reasons.append(f"FDR q={sr.get('FDR校正q')} > {g.get('fdr_q_max')}")
    # 闸3：相对效应达实质量级
    if abs(sr.get("相对效应", 0.0)) < g.get("practically_min", 0.02):
        reasons.append(f"相对效应 {sr.get('相对效应')} < 实质量级 {g.get('practically_min')}")
    # 闸4：与数据异常分区重叠 → 跨支柱揭穿（该"优势"实由异常驱动）
    if not g.get("allow_anomaly_overlap"):
        anomaly_zones = [
            t["分区"] for t in r["data_quality"]["tests"]
            if t.get("实质显著") and t["检验"] in ("相邻自相关", "均匀性χ²(MC)")
        ]
        if anomaly_zones:
            reasons.append(f"优势实由数据异常驱动（显著偏离随机分区: {'、'.join(anomaly_zones)}）")
    # 闸5（贝叶斯）：后验 P(正edge) 需达阈值
    if not no_bayes and sr.get("贝叶斯后验P(正edge)", 0.0) < g.get("bayes_p_edge_min", 0.80):
        reasons.append(f"贝叶斯后验P(正edge)={sr.get('贝叶斯后验P(正edge)')} < {g.get('bayes_p_edge_min')}")

    metrics = {
        "样本外均分": sr.get("样本外均分"),
        "随机基准μ": sr.get("随机基准μ"),
        "相对效应": sr.get("相对效应"),
        "FDR_q": sr.get("FDR校正q"),
        "贝叶斯后验P(正edge)": sr.get("贝叶斯后验P(正edge)"),
    }
    return {
        "passed": len(reasons) == 0,
        "reasons": reasons,
        "metrics": {k: v for k, v in metrics.items() if v is not None},
        "verdict": r["verdict"],
        "sample_n": r.get("n_records"),
    }


# ============================================================
# 探测方式二：EV 边探测（选号类假设，数字型）
# ============================================================
def _ev_edge_probe(lottery: str, n_issues: int = 100) -> dict:
    """冷门号 vs 热门号限号EV 的窗口差距一致性检验。

    对固定赔率彩种（排3/3D/排5）：限号EV恒定 → 差距恒为 0 → 判"无持续边"（正确结论）。
    七星彩（2026-08-31 起启用）：crowd v2（全奖级注数 Poisson 反推）可用 → 浮动头奖
    撞号分薄产生真实差距，可判卷。模型不可用 → 判"不可判"（诚实降级）。

    ⚠️ 曾直接 import ev.crowd(v1)：七星彩一等奖注数过稀疏 → 判"不支持"，
    且 v1 的 log-加性最小二乘与 engine 的 v2 撞号分薄口径不一致。现统一走
    engine._get_crowd（v2 优先，退化 v1）。
    """
    try:
        from ev.engine import net_ev, _get_crowd
        from data.loader import load_lottery
    except Exception as e:
        return {"applicable": False, "reason": f"依赖不可用: {e}"}
    import numpy as np

    model = _get_crowd(lottery)
    if model is None:
        return {"applicable": False,
                "reason": "crowd模型不可用，无法估计冷/热号拥挤度"}

    # 用模型找出"最冷"与"最热"的候选号（模板，跨期固定）
    if lottery in ("排列3", "福彩3D"):
        cands = [(i, [i // 100 % 10, i // 10 % 10, i % 10]) for i in range(1000)]
    elif lottery == "排列5":
        cands = [(i, [i // 10000 % 10, i // 1000 % 10, i // 100 % 10, i // 10 % 10, i % 10])
                 for i in range(0, 100000, 10)]
    elif lottery == "七星彩":
        # 第7位 0-14：按位随机采样（十万级，v2 下 score=Σlogπ 排序稳定）
        rng = np.random.default_rng(20260831)
        sample = np.hstack([rng.integers(0, 10, size=(100000, 6)),
                            rng.integers(0, 15, size=(100000, 1))])
        cands = [(j, sample[j].tolist()) for j in range(len(sample))]
    else:
        return {"applicable": False, "reason": f"彩种 {lottery} 暂不支持 EV 边探测"}

    cold = min(cands, key=lambda t: model.score(t[1]))[1]
    hot = max(cands, key=lambda t: model.score(t[1]))[1]

    data = load_lottery(lottery)
    issues = [r.期号 for r in data.records[:n_issues] if r.期号]
    gaps = []
    for iss in issues:
        try:
            rc = net_ev(lottery, cold, issue=iss)
            rh = net_ev(lottery, hot, issue=iss)
            ec, eh = rc.get("限号EV"), rh.get("限号EV")
            if isinstance(ec, dict):
                ec = ec.get("直选")
            if isinstance(eh, dict):
                eh = eh.get("直选")
            if ec is None or eh is None:
                continue
            # 七星彩：无当期销售数据的期 net_ev 降级为名义EV（冷热无差），
            # 该 0 差是"数据缺失"而非"冷热无差证据"，须剔除（否则稀释真实差距）
            if lottery == "七星彩" and "分薄乘数(≈1=独享)" not in rc.get("详情", {}):
                continue
            gaps.append(float(ec) - float(eh))
        except Exception:
            continue

    if len(gaps) < 30:
        return {"applicable": False, "reason": f"有效样本 {len(gaps)} < 30，不足以判卷"}

    arr = np.array(gaps)
    mean = float(arr.mean())
    std = float(arr.std()) if arr.std() > 0 else 0.0
    pos_frac = float((arr > 0).mean())
    cohen_d = abs(mean) / std if std > 0 else 0.0
    # 非单期驱动：贡献最大的单期正差距占比
    pos = arr[arr > 0]
    single_dominance = float(pos.max() / pos.sum()) if pos.sum() > 0 else 1.0

    return {
        "applicable": True,
        "metrics": {
            "冷号示例": "".join(map(str, cold)),
            "热号示例": "".join(map(str, hot)),
            "样本期数": len(gaps),
            "平均差距(冷-热)": round(mean, 4),
            "正差距占比": round(pos_frac, 4),
            "Cohen_d": round(cohen_d, 4),
            "单期贡献占比": round(single_dominance, 4),
        },
        "mean_gap": mean,
        "pos_frac": pos_frac,
        "cohen_d": cohen_d,
        "single_dominance": single_dominance,
    }


def _ev_edge_gate(lottery: str, hyp: dict, n_issues: int) -> dict:
    probe = _ev_edge_probe(lottery, n_issues=n_issues)
    if not probe.get("applicable"):
        return {"passed": False, "reasons": [probe.get("reason", "不可判")],
                "metrics": {}, "verdict": "不可判（数据/模型不足，诚实降级）", "sample_n": None}

    m = probe["metrics"]
    reasons = []
    # 闸：平均差距必须显著为正、且非单期驱动、且效应量达标
    if probe["mean_gap"] <= 0:
        reasons.append(f"平均差距 {probe['mean_gap']:.4f} ≤ 0：冷门号并无持续EV优势（固定赔率EV恒定是正确结论）")
    if probe["pos_frac"] < 0.55:
        reasons.append(f"正差距占比 {probe['pos_frac']:.2%} < 55%：差距不具时间一致性")
    if probe["cohen_d"] < 0.2:
        reasons.append(f"效应量 Cohen d={probe['cohen_d']:.3f} < 0.2：差距在噪声内")
    if probe["single_dominance"] > 0.5:
        reasons.append(f"单期贡献 {probe['single_dominance']:.1%} > 50%：优势由单期异常驱动，不可重复")

    return {
        "passed": len(reasons) == 0,
        "reasons": reasons,
        "metrics": m,
        "verdict": "冷门号限号EV持续高于热门号，构成可重复选号边" if not reasons
                   else "未发现可重复的选号EV边",
        "sample_n": m.get("样本期数"),
    }


# ============================================================
# 统一入口
# ============================================================
def run_gate(lottery: str, hyp: dict, k: int = 8, min_train: int = 100,
             army_size: int = 2000, fdr_alpha: float = 0.05,
             bayes_tau: float = None, bayes_p_edge: float = 0.25,
             no_bayes: bool = False, ev_window: int = 100,
             cred_result: dict = None) -> dict:
    """对单条假设判卷，更新登记表状态并返回结果。"""
    probe_type = hyp["probe"].get("type", "credibility_gate")
    if probe_type == "ev_edge":
        res = _ev_edge_gate(lottery, hyp, n_issues=ev_window)
    else:
        res = _credibility_gate(lottery, hyp, k, min_train, army_size,
                                fdr_alpha, bayes_tau, bayes_p_edge, no_bayes,
                                cred_result=cred_result)

    record_test(lottery, hyp["id"], passed=res["passed"],
                metrics=res["metrics"], verdict=res["verdict"],
                reasons=res["reasons"], sample_n=res.get("sample_n"))
    res["hypothesis"] = hyp
    res["status"] = "survived" if res["passed"] else "rejected"
    return res


def gate_all(lottery: str, hyps: list = None, **kw) -> list:
    """对登记表全部假设判卷，写 gate_manifest 清单。

    --all 场景优化：同彩种只跑一次重型裁判（run_credibility），
    所有 credibility_gate 型假设共享，避免每条假设重复 3-4 倍开销。
    """
    hyps = hyps if hyps is not None else list_hypotheses(lottery)
    cred_result = None
    if any(h["probe"].get("type", "credibility_gate") == "credibility_gate" for h in hyps):
        from credibility.report import run_credibility
        cred_result = run_credibility(
            lottery,
            k=kw.get("k", 8), min_train=kw.get("min_train", 100),
            army_size=kw.get("army_size", 2000), fdr_alpha=kw.get("fdr_alpha", 0.05),
            bayes_tau=kw.get("bayes_tau"), bayes_p_edge=kw.get("bayes_p_edge", 0.25),
            no_bayes=kw.get("no_bayes", False))
    results = [run_gate(lottery, h, cred_result=cred_result, **kw) for h in hyps]

    survived = [{
        "id": h["hypothesis"]["id"],
        "name": h["hypothesis"]["name"],
        "strategy": h["hypothesis"]["probe"].get("strategy"),
        "probe": h["hypothesis"]["probe"].get("type"),
    } for h in results if h["passed"]]

    manifest = {
        "lottery": lottery,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "survived": survived,
        "rejected_count": sum(1 for h in results if not h["passed"]),
        "note": "P0 闸门仅产出准入清单；策略权重仍由 data.feedback 按真实P/L驱动，闸门不直接改权重",
    }
    _manifest_path(lottery).write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"闸门完成: {lottery} 存活{len(survived)}/驳回{manifest['rejected_count']} -> {_manifest_path(lottery)}")
    return results
