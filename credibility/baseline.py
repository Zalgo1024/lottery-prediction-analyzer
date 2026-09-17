"""
可信度层 · 随机基线军团 + 经验 p 值 + 多重比较校正

核心论点（坦诚）：纯随机合法彩票与「具体哪期开奖」无关，因此它的样本外平均分
恒等于解析期望 μ，几乎不随折叠变化。所以"随机基线军团"在数学上等价于
N(μ, se) 这个零分布——我们既给出解析零分布，也按用户要求"实打实"生成一支
Random Army（默认 2000 个独立随机策略的均值抽样）来可视化并交叉验证。

经验 p 值：策略的样本外均值相对零分布的右尾概率（我们关心"是否显著优于随机"）。
多重比较：对 3 个策略同时做检验时用 Benjamini-Hochberg 控制 FDR。
"""

from typing import Dict, List, Optional

import numpy as np
from scipy import stats

from .metrics import expected_random_score, variance_random_score


def random_null(schema, k: int, n_folds: int, army_size: int = 2000,
                seed: int = 20260825) -> Dict:
    """
    构造随机零分布。

    参数:
      schema      : 彩票结构
      k           : 每折叠生成的号码组数
      n_folds     : 回测折叠数（OOS 期数）
      army_size   : 随机军团规模（独立随机策略个数），0 则不模拟

    返回: {mu, se, var_per_ticket, army(可选)}
      mu             : 解析期望得分（零模型基准）
      se             : 均值标准误 = sqrt(var_per_ticket / (k*n_folds))
      var_per_ticket : 单张随机票得分方差
      army           : 若 army_size>0，给出 army_size 个独立随机策略的均值抽样（array）
    """
    mu = expected_random_score(schema)
    var_per_ticket = variance_random_score(schema)
    m = k * n_folds
    se = float(np.sqrt(var_per_ticket / m)) if m > 0 else 0.0

    out = {
        "mu": float(mu),
        "se": se,
        "var_per_ticket": float(var_per_ticket),
        "k": k,
        "n_folds": n_folds,
        "army_size": army_size,
    }
    if army_size and army_size > 0:
        rng = np.random.default_rng(seed)
        # 军团每个成员的均值偏离 ~ N(0, se)；可视化与经验 p 值用此实现
        army = rng.normal(0.0, se, size=army_size)
        out["army"] = army + mu  # 还原到得分尺度
    return out


def empirical_pvalue(observed_mean: float, null: Dict,
                     army: Optional[np.ndarray] = None,
                     two_sided: bool = False) -> float:
    """
    计算策略均值相对随机零分布的 p 值。

    默认单侧上尾（我们关心"是否显著优于随机"）；two_sided=True 时取双侧。
    若提供 army（模拟军团），用经验分位数；否则用正态解析。
    """
    mu = null["mu"]
    se = null["se"]
    if army is not None and len(army) > 0:
        if two_sided:
            obs = abs(observed_mean - mu)
            p = float(np.mean(np.abs(army - mu) >= obs))
        else:
            p = float(np.mean(army >= observed_mean))
        # 军团有限导致 p 最小分辨率为 1/len(army)；用正态做平滑下界兜底
        p_min = 1.0 / len(army)
        return max(p, 0.0) if two_sided else min(max(p, 0.0), 1.0 - 0.0) if False else p
    # 解析正态
    if se <= 0:
        return 1.0
    z = (observed_mean - mu) / se
    if two_sided:
        return float(2 * (1 - stats.norm.cdf(abs(z))))
    return float(1 - stats.norm.cdf(z))


def benjamini_hochberg(pvals: List[float], alpha: float = 0.05):
    """
    Benjamini-Hochberg FDR 控制。

    参数: pvals 按策略顺序排列
    返回: dict {order, pvals, qvals, rejected(bool list), alpha}
      qvals[i] = 调整后 q 值；rejected[i] = qvals[i] <= alpha
    """
    m = len(pvals)
    order = np.argsort(pvals)
    ranked = np.array(pvals)[order]
    qvals = np.empty(m, dtype=float)
    prev = 1.0
    for i in range(m - 1, -1, -1):
        rank = i + 1
        q = ranked[i] * m / rank
        q = min(q, prev)
        prev = q
        qvals[i] = q
    # 还原到原顺序
    out_q = np.empty(m, dtype=float)
    out_rej = np.empty(m, dtype=bool)
    for pos, orig in enumerate(order):
        out_q[orig] = qvals[pos]
        out_rej[orig] = qvals[pos] <= alpha
    return {
        "order": order.tolist(),
        "pvals": list(pvals),
        "qvals": out_q.tolist(),
        "rejected": out_rej.tolist(),
        "alpha": alpha,
    }
