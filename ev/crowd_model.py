"""
ev/crowd_model.py —— 人群选号热度反推（P1-1，统一计数框架，v2）

为什么重写 ev/crowd.py：
- v1 只取单一奖级注数（3D=直选、排5=一等奖）做 log 线性最小二乘，七星彩因
  一等奖注数过稀疏（0/1/2）直接返回 None——而七星彩恰是**唯一有浮动头奖、
  撞号分薄真实存在**的彩种（理论价值最高处却是能力最弱处）。
- v2 用**全部奖级注数**（七星彩六等奖每期 ~80 万注，信息量巨大）做 Poisson MLE：
  假设每注号码按「位置独立分类分布 π_i(d)」抽取，对某期开奖号 z，各奖级中奖
  注数期望 = 当期票量 × q_奖级(z; π)，q 由 π 与奖级命中结构算出（3 位彩种
  直选/组选解析式；排5 全中乘积；七星彩任意位置匹配 DP 卷积）。
  最大化各期观测注数的 Poisson 似然 → 反推 π。

核心假设（诚实声明）：
1. 每注独立 + 位置独立 → π_i(d) 完全刻画"大众爱买什么数字"。
2. 直选/组选3/组选6 是不同票种，各自购票占比 λ（每彩种全局常量，3 位彩种）
   与 π 联合估计；3D 组选列缺失期 → 部分观测仍无偏。
3. 当期票量 N：有销售额则 N=销售额/2；无销售额（排3/排5 早年）的期不参与
   七星彩/排5 拟合（单奖级 profile 无信息），3 位彩种用集中似然 profile。
4. 只反推"哪类组合买的人少"，不反推开奖（开奖与购彩独立）。

产出：pi 热度矩阵 + score/expected_sales/expected_co_winners/share_multiplier
（撞号分薄：大众号中头奖被多人分摊）。
"""

import logging
from pathlib import Path
from typing import List, Optional

import numpy as np
import pandas as pd
from scipy.optimize import minimize

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
HIST_DIR = BASE_DIR / "lottery_data"
_EPS = 1e-12


# ---------------- 彩种规格 ----------------

def _zones(name: str) -> List[int]:
    """每位置数字范围大小：七星彩第7位 0-14（15 个号），其余位置 0-9。"""
    if name == "七星彩":
        return [10, 10, 10, 10, 10, 10, 15]
    return [10] * (5 if name == "排列5" else 3)


def _min_date(name: str) -> Optional[str]:
    """七星彩 2020-10-11 改版为「任意位置匹配」现行奖级表；2004~2020 旧规则
    （连续位数、1800/300/20 结构）的注数含义不同，只取现行口径期。"""
    return "2020-10-13" if name == "七星彩" else None


def _is_3digit(name: str) -> bool:
    return name in ("排列3", "福彩3D")


def _digit_cols(name: str) -> List[str]:
    return [f"号码{i}" for i in range(1, len(_zones(name)) + 1)]


def _tier_cols(name: str) -> List[str]:
    if name == "七星彩":
        return [f"{k}等奖注数" for k in "一二三四五六"]
    if name == "排列5":
        return ["一等奖注数"]
    return ["直选注数", "组选3注数", "组选6注数"]


def _sales_csv(name: str) -> Path:
    fn = {"七星彩": "七星彩销售奖级数据.csv", "排列3": "排列3销售奖级数据.csv",
          "排列5": "排列5销售奖级数据.csv", "福彩3D": "福彩3D销售奖级数据.csv"}[name]
    return HIST_DIR / fn


def _history_csv(name: str) -> Path:
    for cand in (f"{name}历史数据_cleaned.csv", f"{name}历史数据.csv"):
        p = HIST_DIR / cand
        if p.exists():
            return p
    raise FileNotFoundError(f"未找到 {name} 历史开奖数据于 {HIST_DIR}")


# ---------------- 命中概率 q（给定开奖号 z 与 π） ----------------

def _softmax_rows(theta: np.ndarray, sizes: List[int]) -> np.ndarray:
    """theta: 展平 logit（每位置 s 个，含参考位）→ π [n_pos, max(sizes)]。"""
    pi = np.zeros((len(sizes), max(sizes)))
    off = 0
    for i, s in enumerate(sizes):
        row = theta[off:off + s]
        row = row - row.max()
        e = np.exp(row)
        pi[i, :s] = e / (e.sum() + _EPS)
        off += s
    return pi


def _tier_q(name: str, pi: np.ndarray, z: List[int]) -> np.ndarray:
    """给定每位置热度 π 与开奖号 z，算各奖级命中概率 q。

    - 排列3/3D：返回 [直选, 组选3, 组选6]（按开奖号形态；形态不允许时为 0）
    - 排列5：  返回 [一等奖]（5 位全对且顺序一致）
    - 七星彩：返回 [一等..六等]（前 6 位命中数 X 的 DP + 第 7 位命中 Y，
               现行奖级=按 (X,Y) 分区，见下）
    """
    if name in ("排列3", "福彩3D"):
        d0, d1, d2 = z[0], z[1], z[2]
        q_direct = float(pi[0, d0] * pi[1, d1] * pi[2, d2])
        uniq = len(set(z))
        if uniq == 3:  # 全异 → 组选6 形态
            perms = ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0))
            q6 = sum(pi[0, z[a]] * pi[1, z[b]] * pi[2, z[c]] for a, b, c in perms)
            return np.array([q_direct, 0.0, q6])
        if uniq == 2:  # 一对 + 一独 → 组选3 形态
            solo = next(d for d in set(z) if z.count(d) == 1)
            rep = next(d for d in set(z) if z.count(d) == 2)
            q3 = 0.0
            for solo_at in range(3):  # solo 出现在哪个位置
                p = 1.0
                for i in range(3):
                    p *= pi[i, solo] if i == solo_at else pi[i, rep]
                q3 += p
            return np.array([q_direct, q3, 0.0])
        return np.array([q_direct, 0.0, 0.0])  # 三同（豹子）：无组选

    if name == "排列5":
        q = float(np.prod([pi[i, z[i]] for i in range(5)]))
        return np.array([q])

    # 七星彩：前 6 位命中数 X ~ 逐位 Bernoulli(π_i(z_i)) 卷积
    f = np.zeros(7)
    f[0] = 1.0
    for p in (pi[i, z[i]] for i in range(6)):
        nf = np.zeros(7)
        nf[0] = (1 - p) * f[0]
        for x in range(1, 7):
            nf[x] = (1 - p) * f[x] + p * f[x - 1]
        f = nf
    b = float(pi[6, z[6]])
    # 现行奖级分区（2020-10-13 起，任意位置匹配）：
    # 一(T7=6,1) 二=(6,0) 三=(5,1) 四=T5=(5,0)+(4,1) 五=T4=(4,0)+(3,1)
    # 六=(3,0)+(2,1)+(1,1)+(0,1)；未中 = X<=2 且 Y=0
    return np.array([
        f[6] * b,                        # 一等
        f[6] * (1 - b),                  # 二等
        f[5] * b,                        # 三等
        f[5] * (1 - b) + f[4] * b,       # 四等
        f[4] * (1 - b) + f[3] * b,       # 五等
        f[3] * (1 - b) + (f[2] + f[1] + f[0]) * b,  # 六等
    ])


# ---------------- 数据装配 ----------------

def _load_draws(name: str):
    """读历史开奖：(期号, 开奖日期, digits[int])；越界/坏行丢弃。"""
    df = pd.read_csv(_history_csv(name), encoding="utf-8-sig", dtype={"期号": str})
    cols = _digit_cols(name)
    zones = _zones(name)
    out = []
    for _, r in df.iterrows():
        try:
            digits = [int(str(r[c]).strip()) for c in cols]
        except (ValueError, TypeError):
            continue
        if any(not (0 <= d < zones[i]) for i, d in enumerate(digits)):
            continue
        out.append((str(r["期号"]), str(r.get("开奖日期", "")), digits))
    return out


def _load_observations(name: str):
    """装配拟合数据：每期 dict(digits, N 或 None, counts=[(tier_idx, count),...])。

    N = 销售额/2（有销售额）；无销售额 → None。只保留规则口径一致的期。
    """
    zones = _zones(name)
    sales = pd.read_csv(_sales_csv(name), encoding="utf-8-sig", dtype={"期号": str})
    tcols = _tier_cols(name)
    mdate = _min_date(name)
    sales_map = sales.set_index("期号")
    obs = []
    for qh, date, digits in _load_draws(name):
        if mdate and date < mdate:
            continue
        if qh not in sales_map.index:
            continue
        row = sales_map.loc[qh]
        counts = []
        for ti, c in enumerate(tcols):
            v = row.get(c)
            if v is None or (isinstance(v, float) and np.isnan(v)):
                continue
            try:
                v = float(v)
            except (TypeError, ValueError):
                continue
            counts.append((ti, v))
        if not counts:
            continue
        sv = row.get("销售额")
        N = None
        if sv is not None and not (isinstance(sv, float) and np.isnan(sv)):
            N = float(sv) / 2.0
        obs.append({"digits": digits, "N": N, "counts": counts})
    return obs


# ---------------- 向量化似然（按期矩阵，避免逐期 Python 拖慢梯度） ----------------

def _n_logit_free(sizes: List[int]) -> int:
    """自由参数数：每位置 s 个概率 → s-1 个 logit（参考位=0）。"""
    return sum(s - 1 for s in sizes)


def _free_to_full(theta_free: np.ndarray, sizes: List[int]) -> np.ndarray:
    """自由 logit（s-1/位）→ 完整 logit（末位参考=0）。"""
    full = np.zeros(sum(sizes))
    off, off2 = 0, 0
    for s in sizes:
        full[off:off + s - 1] = theta_free[off2:off2 + s - 1]
        off += s
        off2 += s - 1
    return full


def _tier_count(name: str) -> int:
    return 6 if name == "七星彩" else (1 if name == "排列5" else 3)


def _prep_arrays(obs: List[dict], name: str) -> dict:
    """obs → 矩阵：Z(D,n_pos) 每期开奖号、N(D,) 票量(nan=缺)、M/C(D,T) 观测掩码/注数。"""
    D = len(obs)
    T = _tier_count(name)
    Z = np.array([d["digits"] for d in obs], dtype=int)
    N = np.array([d["N"] if d["N"] is not None else np.nan for d in obs], dtype=float)
    M = np.zeros((D, T))
    C = np.zeros((D, T))
    for di, d in enumerate(obs):
        for ti, c in d["counts"]:
            if 0 <= ti < T:
                M[di, ti] = 1.0
                C[di, ti] = c
    return {"Z": Z, "N": N, "M": M, "C": C, "D": D, "T": T}


def _match_probs(pi: np.ndarray, Z: np.ndarray) -> np.ndarray:
    """每期每位命中概率 (D, n_pos)：m[d,i] = π_i(Z[d,i])。"""
    n_pos = Z.shape[1]
    rows = np.broadcast_to(np.arange(n_pos), Z.shape)
    return pi[rows, Z]


def _q_all(name: str, pi: np.ndarray, Z: np.ndarray) -> np.ndarray:
    """批量算每期各奖级命中概率 q (D,T)。"""
    D = len(Z)
    m = _match_probs(pi, Z)
    if name == "排列5":
        return np.prod(m, axis=1, keepdims=True)
    if name in ("排列3", "福彩3D"):
        q_dir = m[:, 0] * m[:, 1] * m[:, 2]
        q3 = np.zeros(D)
        q6 = np.zeros(D)
        for d in range(D):
            z = [int(x) for x in Z[d]]
            u = len(set(z))
            if u == 3:  # 全异 → 组选6：6 种排列累加
                p = 0.0
                for perm in ((0, 1, 2), (0, 2, 1), (1, 0, 2), (1, 2, 0), (2, 0, 1), (2, 1, 0)):
                    p += pi[0, z[perm[0]]] * pi[1, z[perm[1]]] * pi[2, z[perm[2]]]
                q6[d] = p
            elif u == 2:  # 一对+独 → 组选3：solo 位 3 选 1
                rep = next(v for v in set(z) if z.count(v) == 2)
                solo = next(v for v in set(z) if z.count(v) == 1)
                for at in range(3):
                    p = 1.0
                    for i in range(3):
                        p *= pi[i, solo] if i == at else pi[i, rep]
                    q3[d] += p
        return np.column_stack([q_dir, q3, q6])
    # 七星彩：前 6 位命中数 DP（向量化整批）
    f = np.zeros((D, 7))
    f[:, 0] = 1.0
    for pos in range(6):
        p = m[:, pos]
        nf = np.zeros((D, 7))
        nf[:, 0] = (1 - p) * f[:, 0]
        for x in range(1, 7):
            nf[:, x] = (1 - p) * f[:, x] + p * f[:, x - 1]
        f = nf
    b = m[:, 6]
    return np.column_stack([
        f[:, 6] * b,                                  # 一等 (6,1)
        f[:, 6] * (1 - b),                            # 二等 (6,0)
        f[:, 5] * b,                                  # 三等 (5,1)
        f[:, 5] * (1 - b) + f[:, 4] * b,              # 四等 T5
        f[:, 4] * (1 - b) + f[:, 3] * b,              # 五等 T4
        f[:, 3] * (1 - b) + (f[:, 2] + f[:, 1] + f[:, 0]) * b,  # 六等
    ])


def _ll_total(name: str, pi: np.ndarray, prep: dict,
              shares: Optional[np.ndarray] = None) -> float:
    """总 Poisson 对数似然（所有期；有票量→直接项，无票量→集中 profile）。"""
    Z, N, M, C = prep["Z"], prep["N"], prep["M"], prep["C"]
    q = _q_all(name, pi, Z)
    if shares is not None:                      # 3 位彩种票种占比 [直选,组3,组6]
        mu_scale = shares[None, :] * q
    else:
        mu_scale = q
    hasN = np.isfinite(N)
    ll = 0.0
    # —— 有票量：Poisson(c; N·s·q)，逐 (期, 奖级) 求和（掩码掉未观测奖级）——
    if hasN.any():
        mu = N[hasN, None] * mu_scale[hasN]
        msk = M[hasN]
        ct = C[hasN]
        with np.errstate(divide="ignore", invalid="ignore"):
            term = np.where(ct > 0, ct * np.log(np.maximum(mu, _EPS)) - mu, -mu)
        ll += float((term * msk).sum())
    # —— 无票量（排3/排5 早年）：集中似然，N̂ = C_obs / Q_obs ——
    if (~hasN).any():
        sub_m = M[~hasN]
        sub_c = C[~hasN]
        sub_sq = mu_scale[~hasN]
        Cobs = (sub_c * sub_m).sum(axis=1)
        Qobs = (sub_sq * sub_m).sum(axis=1)
        Qobs = np.maximum(Qobs, _EPS)
        with np.errstate(divide="ignore", invalid="ignore"):
            ll += float((Cobs * np.log(Cobs / Qobs) - Cobs).sum())
            # Σ c·log(s·q)（仅 c>0）
            contrib = np.where(sub_c > 0, sub_c * np.log(np.maximum(sub_sq, _EPS)), 0.0)
            ll += float((contrib * sub_m).sum())
    return ll


def _fit(name: str, prep: dict, max_iter: int, shares_init=None):
    """在已装配矩阵上跑 L-BFGS。返回 (pi, shares 或 None, result)。"""
    sizes = _zones(name)
    n_free = _n_logit_free(sizes)
    x0 = np.zeros(n_free)
    if _is_3digit(name):
        lam = np.zeros(2) if shares_init is None else np.log(
            np.maximum(shares_init[1:], _EPS) / max(shares_init[0], _EPS))

        def obj(par):
            tf = par[:n_free]
            pi = _softmax_rows(_free_to_full(tf, sizes), sizes)
            w = np.array([0.0, par[n_free], par[n_free + 1]])
            w = w - np.logaddexp.reduce(w)
            sh = np.exp(w)
            return -_ll_total(name, pi, prep, shares=sh)

        res = minimize(obj, np.concatenate([x0, lam]), method="L-BFGS-B",
                       options={"maxiter": max_iter})
        pi = _softmax_rows(_free_to_full(res.x[:n_free], sizes), sizes)
        w = np.array([0.0, res.x[n_free], res.x[n_free + 1]])
        w = w - np.logaddexp.reduce(w)
        shares = np.exp(w)
    else:
        def obj(free):
            return -_ll_total(name, _softmax_rows(_free_to_full(free, sizes), sizes), prep)

        res = minimize(obj, x0, method="L-BFGS-B", options={"maxiter": max_iter})
        pi = _softmax_rows(_free_to_full(res.x, sizes), sizes)
        shares = None
    return pi, shares, res


def fit_crowd_v2(name: str, lookback: Optional[int] = None,
                 max_iter: int = 400, obs: Optional[list] = None,
                 ) -> Optional["CrowdV2"]:
    """反推每位置公众热度 π（Poisson MLE，L-BFGS 向量化似然）。

    lookback：只取最近 N 期（None=七星彩/排列5 口径内全量；3 位彩种默认 2000，
    兼顾早年组选口径波动与拟合耗时）。obs：注入合成观测（自检用）。
    """
    if name not in ("七星彩", "排列5", "排列3", "福彩3D"):
        logger.warning(f"[crowd_v2] 不支持的彩种: {name}")
        return None
    if obs is None:
        obs = _load_observations(name)
        if name in ("七星彩", "排列5"):
            obs = [d for d in obs if d["N"] is not None]  # 单奖级必须有票量
    if len(obs) < 200:
        logger.warning(f"[crowd_v2] {name} 有效拟合期仅 {len(obs)}，不拟合")
        return None
    if lookback is None and _is_3digit(name):
        lookback = 2000
    if lookback:
        obs = obs[-lookback:]
    if len(obs) < 200:
        logger.warning(f"[crowd_v2] {name} lookback 后仅 {len(obs)}，不拟合")
        return None

    prep = _prep_arrays(obs, name)
    pi, shares, res = _fit(name, prep, max_iter=max_iter)
    if res.nit == 0 and not res.success:
        logger.warning(f"[crowd_v2] {name} L-BFGS 未启动: {res.message}")
        return None
    logger.info(f"[crowd_v2] {name} 拟合 {len(obs)} 期: converged={res.success} "
                f"iter={res.nit} NLL={res.fun:.1f}")
    return CrowdV2(name=name, pi=pi, zones=_zones(name), shares=shares,
                   n_obs=len(obs), converged=bool(res.success),
                   n_iter=int(res.nit), nll=float(res.fun))


class CrowdV2:
    """位置独立热度模型。

    接口兼容 ev/crowd.CrowdModel 的 score / expected_sales / crowding_quantile，
    新增 combo_prob / expected_co_winners / share_multiplier（撞号分薄）/
    coldness / coldest。
    """

    def __init__(self, name: str, pi: np.ndarray, zones: List[int],
                 shares: Optional[np.ndarray] = None, n_obs: int = 0,
                 converged: bool = True, n_iter: int = 0, nll: float = 0.0):
        self.name = name
        self.pi = pi          # [位, 数字] 概率
        self.zones = zones
        self.shares = shares  # 3 位彩种票种占比 [直选, 组选3, 组选6]
        self.n_obs = n_obs
        self.converged = converged
        self.n_iter = n_iter
        self.nll = nll

    # ---- 兼容 v1 接口 ----
    def score(self, nums) -> float:
        """log P(组合) = Σ log π_i(d_i)（越大=越大众）。"""
        s = 0.0
        for i, d in enumerate(nums):
            if i < len(self.zones) and 0 <= d < self.zones[i]:
                s += np.log(self.pi[i, d] + _EPS)
        return float(s)

    def score_vec(self, nums_arr: np.ndarray) -> np.ndarray:
        s = np.zeros(len(nums_arr))
        for i in range(nums_arr.shape[1]):
            if i < len(self.zones):
                col = nums_arr[:, i]
                ok = (col >= 0) & (col < self.zones[i])
                if ok.any():
                    s[ok] += np.log(self.pi[i, col[ok]] + _EPS)
        return s

    def combo_prob(self, nums) -> float:
        """任一注恰为该直选组合的概率 = Ππ_i(d_i)。"""
        p = 1.0
        for i, d in enumerate(nums):
            if i < len(self.zones) and 0 <= d < self.zones[i]:
                p *= float(self.pi[i, d])
        return p

    def expected_sales(self, nums, total_tickets: float) -> float:
        """该直选组合预期被买注数 = 总注数 × Ππ（3 位彩种含直选票占比）。"""
        if self.shares is not None:
            return total_tickets * self.shares[0] * self.combo_prob(nums)
        return total_tickets * self.combo_prob(nums)

    def expected_co_winners(self, nums, total_tickets: float) -> float:
        """预期同号注数（含自己的 1 注）。"""
        return self.expected_sales(nums, total_tickets)

    def share_multiplier(self, nums, total_tickets: float) -> float:
        """撞号分薄乘数 E[1/(1+X)]，X~Poisson(μ)。

        头奖注数 X 近似 Poisson(μ=同号期望)：E[1/(1+X)]=(1−e^{−μ})/μ。
        冷门 μ≈0 → ≈1（独享奖池）；大众 μ 大 → 快速趋 0。
        """
        mu = self.expected_co_winners(nums, total_tickets)
        if mu <= _EPS:
            return 1.0
        return float((1.0 - np.exp(-mu)) / mu)

    def crowding_quantile(self, nums) -> float:
        """拥挤度分位（0~1）：sample 中 score <= 该号占比。>0.8 = 大众号。"""
        rng = np.random.default_rng(42)
        if self.name in ("排列3", "福彩3D"):
            sample = np.array([[i // 100 % 10, i // 10 % 10, i % 10]
                               for i in range(1000)])
        elif self.name == "排列5":
            sample = rng.integers(0, 10, size=(50000, 5))
        else:
            sample = np.hstack([rng.integers(0, 10, size=(20000, 6)),
                                rng.integers(0, 15, size=(20000, 1))])
        scores = self.score_vec(sample)
        return float((scores <= self.score(nums)).mean())

    def coldness(self, nums) -> float:
        """冷门度 = −score（越大越冷门，撞号分薄风险越低）。"""
        return -self.score(nums)

    def coldest(self, k: int = 10):
        """找最冷门 k 个组合（3D 全枚举；排5/七星彩抽样）。"""
        rng = np.random.default_rng(7)
        if self.name in ("排列3", "福彩3D"):
            sample = np.array([[i // 100 % 10, i // 10 % 10, i % 10]
                               for i in range(1000)])
        elif self.name == "排列5":
            sample = rng.integers(0, 10, size=(50000, 5))
        else:
            sample = np.hstack([rng.integers(0, 10, size=(200000, 6)),
                                rng.integers(0, 15, size=(200000, 1))])
        sc = self.score_vec(sample)
        idx = np.argsort(sc)[:k]
        return [(sample[i].tolist(), float(sc[i])) for i in idx]


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    for nm in (sys.argv[1:] or ["排列3", "福彩3D", "排列5", "七星彩"]):
        print(f"\n===== {nm} =====")
        m = fit_crowd_v2(nm)
        if m is None:
            print("无模型（数据不足）")
            continue
        print(f"拟合: {m.n_obs} 期 converged={m.converged} "
              f"iter={m.n_iter} NLL={m.nll:.1f}")
        for i, z in enumerate(m.zones):
            hot = int(np.argmax(m.pi[i]))
            cold = int(np.argmin(m.pi[i]))
            spread = float(m.pi[i].max() - m.pi[i].min())
            print(f"  第{i+1}位: 最热={hot}({m.pi[i, hot]:.4f}) "
                  f"最冷={cold}({m.pi[i, cold]:.4f}) 极差={spread:.4f}")
        print("  最冷门 TOP3:", m.coldest(3))
