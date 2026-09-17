"""
可信度层 · 编排与报告

把四个支柱（OOS 回测 / 随机基线军团 / 数据质量 / 反大众）汇总成一份诚实结论：
  - 任何策略是否「显著优于纯随机」（含 FDR 校正）？
  - 数据本身是否足够随机、完整、可信？
  - 反大众（仅双/大）是否带来可量化的降低分摊风险？

不输出"中奖承诺"，只输出可被证伪的指标。
"""

from typing import Dict, List

from data.schema import get_schema

from .backtest import oos_backtest
from .baseline import random_null, empirical_pvalue, benjamini_hochberg
from .data_quality import data_quality_report
from .anti_crowd import anti_crowd_report
from .bayes import bayes_calibration

_STRATEGY_NAMES = ["高频策略", "遗漏值策略", "区间均衡策略"]


def run_credibility(lottery_name: str, k: int = 8, min_train: int = 100,
                    army_size: int = 2000, fdr_alpha: float = 0.05,
                    bayes_tau: float = None, bayes_p_edge: float = 0.25,
                    no_bayes: bool = False) -> Dict:
    schema = get_schema(lottery_name)
    bt = oos_backtest(lottery_name, k=k, min_train=min_train)
    n_folds = bt["n_folds"]
    null = random_null(schema, k, n_folds, army_size=army_size)
    army = null.get("army")

    strategy_results = []
    pvals = []
    for name in _STRATEGY_NAMES:
        obs = bt["means"][name]
        mu = null["mu"]
        se = null["se"]
        p_upper = empirical_pvalue(obs, null, army=army, two_sided=False)
        p_two = empirical_pvalue(obs, null, army=army, two_sided=True)
        rel = (obs - mu) / mu if mu else 0.0
        # 经济/实质显著性：匹配数相对提升需达到一定量级才可能在头奖概率上有意义
        practically = abs(rel) >= 0.02
        strategy_results.append({
            "策略": name,
            "样本外均分": round(obs, 4),
            "随机基准μ": round(mu, 4),
            "差值(obs-μ)": round(obs - mu, 4),
            "相对效应": round(rel, 4),
            "标准误SE": round(se, 5),
            "p_优于随机(单尾)": round(p_upper, 4),
            "p_双侧": round(p_two, 4),
            "经济显著": bool(practically),
        })
        pvals.append(p_upper)

    bh = benjamini_hochberg(pvals, alpha=fdr_alpha)
    for i, sr in enumerate(strategy_results):
        sr["FDR校正q"] = round(bh["qvals"][i], 4)
        sr["显著优于随机"] = bool(bh["rejected"][i])

    # 第⑤支柱：贝叶斯校准（怀疑先验下的 edge 后验概率）
    bayes_enabled = not no_bayes
    bayes_summary = None
    if bayes_enabled:
        mu = null["mu"]
        se = null["se"]
        bayes_summary = {
            "mu_random": round(mu, 6),
            "se": round(se, 6),
            "prior_tau": None,
            "prior_p_edge": bayes_p_edge,
            "results": [],
        }
        for sr in strategy_results:
            cal = bayes_calibration(sr["样本外均分"], se, mu,
                                    prior_tau=bayes_tau, prior_p_edge=bayes_p_edge)
            sr["贝叶斯BF10"] = round(cal["bf10"], 3)
            sr["贝叶斯后验P(正edge)"] = round(cal["posterior_p_positive_edge"], 3)
            if bayes_summary["prior_tau"] is None:
                bayes_summary["prior_tau"] = round(cal["prior_tau"], 6)
            bayes_summary["results"].append({
                "策略": sr["策略"],
                "BF10": round(cal["bf10"], 3),
                "后验P(edge)": round(cal["posterior_p_edge"], 3),
                "后验P(正edge)": round(cal["posterior_p_positive_edge"], 3),
            })

    dq = data_quality_report(lottery_name, fdr_alpha=fdr_alpha)
    ac = anti_crowd_report(lottery_name)

    any_stat = any(sr["显著优于随机"] for sr in strategy_results)
    any_pract = any(sr["显著优于随机"] and sr["经济显著"] for sr in strategy_results)
    borderline = [sr["策略"] for sr in strategy_results if sr["显著优于随机"] and not sr["经济显著"]]
    pract_names = [sr["策略"] for sr in strategy_results if sr["显著优于随机"] and sr["经济显著"]]

    # 跨支柱交叉：若"显著策略"与"数据质量异常分区"重叠，则"优势"实由异常驱动
    anomaly_zones = [t["分区"] for t in dq["tests"]
                     if t.get("实质显著", False)
                     and t["检验"] in ("相邻自相关", "均匀性χ²(MC)")]

    if any_pract and anomaly_zones:
        verdict = (f"表面「实质量级」信号（{('、'.join(pract_names))}）实由数据/摇奖异常驱动"
                   f"（显著偏离随机的分区：{('、'.join(anomaly_zones))}）。这是「开奖质量监控」"
                   f"应标记的问题，不构成可投注优势——该「优势」源于异常相邻相关性，"
                   f"而非可预测的号码规律。")
    elif any_pract:
        verdict = ("是 —— 有策略在 FDR 校正后既统计显著、相对效应也达到实质量级，"
                   "需优先排查数据泄露 / 取数穿越，再考虑是否为真实弱信号。")
    elif any_stat:
        verdict = (f"边界 —— {('、'.join(borderline))} 在 FDR 校正后呈统计显著(q<{fdr_alpha})，"
                   "但相对效应<2%（对头奖概率≈1/千万级无实质影响），且大样本下边界 p 值极可能是"
                   "多重比较噪声。不视为可行动信号，仅作监控。")
    else:
        verdict = ("否 —— 在严格样本外、经 FDR 校正后，没有任何策略显著优于纯随机。"
                   "这与「公平随机开奖不可被稳定预测」一致；当前系统的价值在于纪律化投注与诚实自检，"
                   "而非提高中奖率。")

    return {
        "lottery_name": lottery_name,
        "n_records": bt["n_records"],
        "n_folds": n_folds,
        "min_train": min_train,
        "k_per_fold": k,
        "random_baseline_backtest": round(bt["means"]["随机基线"], 4),
        "random_mu_analytic": round(null["mu"], 4),
        "strategy_results": strategy_results,
        "fdr_alpha": fdr_alpha,
        "any_statistically_beats": any_stat,
        "any_practically_beats": any_pract,
        "borderline_strategies": borderline,
        "verdict": verdict,
        "bayes_enabled": bayes_enabled,
        "bayes": bayes_summary,
        "data_quality": dq,
        "anti_crowd": ac,
    }


def _print_credibility(r: Dict):
    print("=" * 64)
    print(f"  可信度层报告 · {r['lottery_name']}")
    print("=" * 64)
    print(f"  历史期数={r['n_records']}  样本外折叠={r['n_folds']}  "
          f"每折叠票数={r['k_per_fold']}  min_train={r['min_train']}")
    print(f"  随机基线(回测均分)={r['random_baseline_backtest']}  "
          f"随机基准μ(解析)={r['random_mu_analytic']}  "
          f"（二者应一致，验证零模型正确）")
    print("-" * 64)
    print("  策略            均分      μ       差值     相对效    SE       p(单尾)  q(FDR)  显著")
    for sr in r["strategy_results"]:
        print(f"  {sr['策略']:<6}  {sr['样本外均分']:>8}  {sr['随机基准μ']:>7}  "
              f"{sr['差值(obs-μ)']:>+8}  {sr['相对效应']:>+7}  {sr['标准误SE']:>8}  "
              f"{sr['p_优于随机(单尾)']:>7}  {sr['FDR校正q']:>6}  "
              f"{'★' if sr['显著优于随机'] else '·'}")
    print("-" * 64)
    print(f"  结论：{r['verdict']}")
    print()
    # 第⑤支柱：贝叶斯校准区块
    if r.get("bayes_enabled") and r.get("bayes"):
        b = r["bayes"]
        print("  [贝叶斯校准 · 第⑤支柱]")
        print(f"    随机μ={b['mu_random']}  SE={b['se']}  "
              f"先验τ={b['prior_tau']}  P(H1)先验={b['prior_p_edge']}")
        print(f"    {'策略':<8}{'BF10':>9}{'后验P(edge)':>14}{'后验P(正edge)':>15}")
        for c in b["results"]:
            print(f"    {c['策略']:<8}{c['BF10']:>9.3f}{c['后验P(edge)']:>14.3f}"
                  f"{c['后验P(正edge)']:>15.3f}")
        bayes_edge = [c['策略'] for c in b["results"] if c['后验P(正edge)'] >= 0.8]
        if bayes_edge:
            print(f"    贝叶斯支持 edge：{('、'.join(bayes_edge))}（须与③异常监控交叉复核）")
        else:
            print(f"    贝叶斯：无策略后验 P(edge) 明显推高，数据未支持'真有 edge'")
        print()
    dq = r["data_quality"]
    print(f"  [数据质量] 评分={dq['trust_score']}/100  评级={dq['grade']}")
    print(f"            检验数={dq['n_tests']}  显著(校正后)={dq['n_rejected']}  "
          f"重复={dq['duplicates']}  期号断裂={dq['issue_gaps']}  "
          f"日期大间隔={dq['big_date_gaps']}(最大{dq['max_date_gap_days']}天)")
    for note in dq["notes"]:
        print(f"            · {note}")
    print()
    ac = r["anti_crowd"]
    if not ac.get("applicable", False):
        print(f"  [反大众] 不适用 —— {ac.get('note','')}")
    else:
        print(f"  [反大众] 有效样本={ac.get('n_with_data')}  热门组合期数={ac.get('n_popular')}")
        print(f"            热门中位归一化={ac.get('popular_median_norm')}  "
              f"其他中位={ac.get('nonpopular_median_norm')}  p(MW)={ac.get('mw_p')}")
        print(f"            {ac.get('interpretation','')}")


def run_cli(lottery_name: str, k: int = 8, min_train: int = 100,
            army_size: int = 2000, fdr_alpha: float = 0.05,
            bayes_tau: float = None, bayes_p_edge: float = 0.25,
            no_bayes: bool = False):
    r = run_credibility(lottery_name, k=k, min_train=min_train,
                        army_size=army_size, fdr_alpha=fdr_alpha,
                        bayes_tau=bayes_tau, bayes_p_edge=bayes_p_edge,
                        no_bayes=no_bayes)
    _print_credibility(r)
    return r
