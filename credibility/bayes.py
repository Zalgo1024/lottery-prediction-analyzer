"""
可信度层 · 第⑤支柱：贝叶斯校准（Bayesian Calibration）

你的 GPT 思路里这一条是四支柱（OOS 回测 / 随机基线军团 / 数据质量 / 反大众）的"另一半"：
  频率学派 p 值 + FDR 回答"比纯随机好多少、显著吗"，但它容易被大样本吓到，
  也只给"拒绝/不拒绝"的二元结论。贝叶斯校准补上：

    > 给一个「怀疑先验」（即"大概率没 edge"），看数据能把
    > "策略真有正 edge（θ > μ_random）"的后验概率推到多高。

模型（诚实、可与 ② 的频率结论并列印证）：
    δ = θ − μ_random         （θ = 策略真实样本外均值）
    H0: δ = 0                （无 edge，点零假设）
    H1: δ ~ N(0, τ²)         （怀疑先验：以"无 edge"为中心、标准差 τ 的对称扩散）
    先验 P(H1) = p_edge       （默认 0.25 ⇒ "大概率没 edge"，P(H0)=0.75）

输出：
    BF10                  —— 证据比（H1 相对 H0 的边际似然比；>1 支持 edge）
    posterior_p_edge      —— P(H1 | 数据) = 贝叶斯后验"真有 edge"概率
    posterior_p_positive  —— 连续模型下 P(θ > μ_random | 数据)（直接回答"正 edge"）
    prior_tau / prior_p_edge —— 用到的先验设置（可复现、可审视）

设计要点：
  - 与 ② 共用零模型 μ 与 SE（来自 baseline.random_null），不重复造轮子。
  - τ 默认 = 1% × |μ|（"怀疑但留口子"的尺度）：只有当数据真有 edge 时才把后验推高。
  - 完全确定性、无 RNG，结果可复现。
  - 本支柱只做诚实度量，不改任何预测逻辑。
"""

from typing import Dict, List, Optional

import numpy as np
from scipy import stats


def bayes_calibration(observed_mean: float, se: float, mu_random: float,
                      prior_tau: Optional[float] = None,
                      prior_p_edge: float = 0.25) -> Dict:
    """
    单策略的贝叶斯 edge 校准。

    参数:
      observed_mean : 策略样本外均值（来自 oos_backtest）
      se            : 该均值的抽样标准误（= baseline.random_null 的 se，对所有策略相同）
      mu_random     : 随机零分布均值（baseline.random_null 的 mu）
      prior_tau     : 怀疑先验标准差；None ⇒ 默认 0.01 × |mu_random|
      prior_p_edge  : 先验 P(H1)（"真有 edge"的先验概率），默认 0.25

    返回: 含 delta、se、bf10、log_bf10、posterior_p_edge、posterior_p_positive、
          prior_tau、prior_p_edge、mu_random 的 dict。
    """
    if se is None or se <= 0:
        se = 1e-9
    if prior_tau is None or prior_tau <= 0:
        prior_tau = 0.01 * abs(mu_random) if mu_random else 0.01

    delta = observed_mean - mu_random
    s2 = se * se
    tau2 = prior_tau * prior_tau

    # 边际似然：H1 下 observed ~ N(0, s2+tau2)；H0 下 observed ~ N(0, s2)
    m1 = stats.norm.logpdf(delta, 0.0, np.sqrt(s2 + tau2))
    m0 = stats.norm.logpdf(delta, 0.0, se)
    log_bf10 = float(m1 - m0)
    bf10 = float(np.exp(log_bf10))

    p1 = float(np.clip(prior_p_edge, 1e-6, 1 - 1e-6))
    p0 = 1.0 - p1
    posterior_p_edge = bf10 * p1 / (bf10 * p1 + p0)

    # 连续后验（在 H1 下）：δ | 数据 ~ N(post_mean, post_var)
    post_var = 1.0 / (1.0 / s2 + 1.0 / tau2)
    post_mean = post_var * (delta / s2)  # 先验均值 0
    posterior_p_positive = float(1.0 - stats.norm.cdf(0.0, post_mean, np.sqrt(post_var)))

    # 头条指标：P(真有正 edge | 数据) = P(δ>0 | 数据)
    #   在"H0:δ=0（P(δ>0)=0）"与"H1:δ~N(0,τ²)"两段模型下，
    #   P(δ>0|数据) = P(H1|数据) × P(δ>0|H1,数据) = posterior_p_edge × posterior_p_positive
    posterior_p_positive_edge = float(posterior_p_edge * posterior_p_positive)

    return {
        "delta": float(delta),
        "se": float(se),
        "bf10": bf10,
        "log_bf10": log_bf10,
        "posterior_p_edge": float(posterior_p_edge),
        "posterior_p_positive": posterior_p_positive,
        "posterior_p_positive_edge": posterior_p_positive_edge,
        "prior_tau": float(prior_tau),
        "prior_p_edge": float(prior_p_edge),
        "mu_random": float(mu_random),
    }


def bayes_report(lottery_name: str, k: int = 8, min_train: int = 100,
                 prior_tau: Optional[float] = None,
                 prior_p_edge: float = 0.25) -> Dict:
    """
    跑第⑤支柱：对三策略分别做贝叶斯校准，返回结构化结果。

    复用 oos_backtest + random_null（零模型 μ / SE 对所有策略相同）。
    """
    from .backtest import oos_backtest
    from .baseline import random_null

    from data.schema import get_schema

    bt = oos_backtest(lottery_name, k=k, min_train=min_train)
    n_folds = bt["n_folds"]
    schema = get_schema(lottery_name)
    null = random_null(schema, k, n_folds, army_size=0)
    mu = null["mu"]
    se = null["se"]

    strategy_names = ["高频策略", "遗漏值策略", "区间均衡策略"]
    results = []
    for name in strategy_names:
        obs = bt["means"][name]
        cal = bayes_calibration(obs, se, mu, prior_tau=prior_tau, prior_p_edge=prior_p_edge)
        cal["策略"] = name
        cal["样本外均分"] = round(obs, 4)
        results.append(cal)

    return {
        "lottery_name": lottery_name,
        "n_records": bt["n_records"],
        "n_folds": n_folds,
        "k_per_fold": k,
        "min_train": min_train,
        "mu_random": round(mu, 6),
        "se": round(se, 6),
        "prior_tau": round(results[0]["prior_tau"], 6),
        "prior_p_edge": prior_p_edge,
        "results": results,
    }


def print_bayes(r: Dict):
    print("=" * 64)
    print(f"  可信度层 · 第⑤支柱 贝叶斯校准 · {r['lottery_name']}")
    print("=" * 64)
    print(f"  样本外折叠={r['n_folds']}  每折叠票数={r['k_per_fold']}  "
          f"min_train={r['min_train']}")
    print(f"  随机基准μ={r['mu_random']}  SE={r['se']}  "
          f"先验τ={r['prior_tau']}  P(H1)先验={r['prior_p_edge']}")
    print("-" * 64)
    print(f"  {'策略':<8}{'均分':>9}{'δ':>9}{'BF10':>10}"
          f"{'后验P(edge)':>14}{'后验P(正edge)':>15}")
    for cal in r["results"]:
        print(f"  {cal['策略']:<8}{cal['样本外均分']:>9}{cal['delta']:>+9.4f}"
              f"{cal['bf10']:>10.3f}{cal['posterior_p_edge']:>14.3f}"
              f"{cal['posterior_p_positive_edge']:>15.3f}")
    print("-" * 64)
    # 简短交叉提示
    supported = [c["策略"] for c in r["results"] if c["posterior_p_positive_edge"] >= 0.8]
    if supported:
        print(f"  贝叶斯结论：{('、'.join(supported))} 后验 P(edge)≥0.8，"
              f"频率/异常支柱需交叉复核是否构成可投注信号。")
    else:
        print(f"  贝叶斯结论：无策略后验 P(edge) 明显推高（均≈先验），"
              f"数据未提供'真有 edge'的证据，与'公平随机不可稳定预测'一致。")
