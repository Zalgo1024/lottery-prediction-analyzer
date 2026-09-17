"""
ev/crowd.py —— 公众投注分布估计（限号暴露的地基）

核心思想（与裁判层结论一致）：
- 公众偏好不能从「开奖号码频率」学（开奖号码是均匀的，裁判层已证伪"冷热号"）。
- 只能从「中奖注数」学：某期开出号码的直选中奖注数 = 公众对该号码的购买量。
- 位级加性模型：log(注数+1) ≈ Σ_位 β[位][数字] + c（最小二乘，最近 lookback 期）。
- 号码 x 的拥挤度 = exp(score_x)，归一化后：该号被买注数 ≈ 总注数 × softmax 权重。

适用范围（v1）：
- 排列3 / 福彩3D：用「直选中奖注数」拟合（每个开出号 0~几万注，信号充足）。
- 排列5：用「一等奖注数」拟合（每期几十~几百注，可拟合）。
- 七星彩：一等奖注数几乎全为 0/1/2，信号过稀疏 → 返回 None（engine 降级为名义 EV）。
"""
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
HIST_DIR = BASE_DIR / "lottery_data"


def _load_history(name: str) -> pd.DataFrame:
    """读历史开奖：lottery_data/{name}历史数据.csv → DataFrame(期号, 号码列表)"""
    cands = [
        HIST_DIR / f"{name}历史数据_cleaned.csv",
        HIST_DIR / f"{name}历史数据.csv",
    ]
    for p in cands:
        if p.exists():
            df = pd.read_csv(p, encoding="utf-8-sig", dtype={"期号": str})
            num_cols = [c for c in df.columns if c.startswith("号码")]
            if not num_cols:
                num_cols = [c for c in df.columns if c.startswith("红球") or c.startswith("第")]
            df["_nums"] = df[num_cols].astype(str).apply(
                lambda r: [int(x) for x in r if str(x).strip().lstrip("-").isdigit()], axis=1)
            return df
    raise FileNotFoundError(f"未找到 {name} 历史开奖数据: {HIST_DIR}")


def _load_sales(name: str):
    from data.fetch_sales import load_sales
    return load_sales(name)


def _signal_col(name: str) -> str:
    """中奖注数信号列：3D类=直选注数，排列5=一等奖注数，七星彩=一等奖注数(>0期1470，可拟合)"""
    if name in ("排列3", "福彩3D"):
        return "直选注数"
    if name in ("排列5", "七星彩"):
        return "一等奖注数"
    return None


def _zones(name: str):
    """每位数字范围：七星彩第7位 0-14，其余 0-9"""
    if name == "七星彩":
        return [10, 10, 10, 10, 10, 10, 15]
    return [10] * (5 if name == "排列5" else 3)


class CrowdModel:
    """位级加性热度模型"""

    def __init__(self, name: str, betas: np.ndarray, intercept: float,
                 zones: list, sigma: float):
        self.name = name
        self.betas = betas          # [位, 数字]
        self.intercept = intercept
        self.zones = zones
        self.sigma = sigma          # 拟合残差 std（log 尺度）

    def score(self, nums) -> float:
        s = self.intercept
        for i, d in enumerate(nums):
            if i < len(self.zones) and 0 <= d < self.zones[i]:
                s += self.betas[i, d]
        return s

    def score_vec(self, nums_arr: np.ndarray) -> np.ndarray:
        """向量化 score：(N, n_pos) → (N,)"""
        s = np.full(len(nums_arr), self.intercept, dtype=float)
        for i in range(nums_arr.shape[1]):
            col = nums_arr[:, i]
            mask = (col >= 0) & (col < self.zones[i])
            if mask.any():
                s[mask] += self.betas[i, col[mask]]
        return s

    def expected_sales(self, nums, total_tickets: float) -> float:
        """
        该号码被买注数估计：总注数 × exp(score) / Z
        Z 对 3D/排列5/七星彩 均用解析式（位独立 → prod(Σ exp(β))）。
        """
        z = self._partition_function()
        return total_tickets * np.exp(self.score(nums)) / z

    def _partition_function(self) -> float:
        # 位独立 → Z = exp(intercept) × Π_位 (Σ_d exp(β[位][d]))
        per_pos = np.exp(self.betas[:, : max(self.zones)]).sum(axis=1)
        per_pos = per_pos[: len(self.zones)]
        return float(np.exp(self.intercept) * np.prod(per_pos))

    def crowding_quantile(self, nums) -> float:
        """
        拥挤度分位（0~1）：该号码的 exp(score) 在所有号码中的分位。
        >0.8 = 大众号（撞号/限号风险高），<0.2 = 冷门号。
        3D 精确枚举 1000；排列5 抽样 5 万；七星彩 抽样 2 万（向量化）。
        """
        if self.name in ("排列3", "福彩3D"):
            sample = np.array([[i // 100 % 10, i // 10 % 10, i % 10] for i in range(1000)])
        elif self.name == "排列5":
            rng = np.random.default_rng(42)
            sample = rng.integers(0, 10, size=(50000, 5))
        else:  # 七星彩
            rng = np.random.default_rng(42)
            sample = np.hstack([rng.integers(0, 10, size=(20000, 6)),
                                rng.integers(0, 15, size=(20000, 1))])
        scores = self.score_vec(sample)
        q = (scores <= self.score(nums)).mean()
        return float(q)


def fit_crowd_model(name: str, lookback: int = 1000) -> CrowdModel:
    """
    拟合位级热度模型。
    数据：最近 lookback 期 (开奖号码, 中奖注数信号)。
    返回 CrowdModel；信号不足（七星彩）返回 None。
    """
    sig_col = _signal_col(name)
    if sig_col is None:
        return None
    hist = _load_history(name)
    sales = _load_sales(name)
    if sales is None or sales.empty:
        return None

    zones = _zones(name)
    n_pos = len(zones)

    # 对齐：按期号 join
    m = sales[["期号", sig_col]].merge(hist[["期号", "_nums"]], on="期号", how="inner")
    m = m.dropna(subset=[sig_col])
    m = m[m[sig_col] > 0]
    if len(m) < 200:
        logger.warning(f"[crowd] {name} 有效样本仅 {len(m)}，不拟合")
        return None
    # 优先最近 lookback 期；样本不足则自动用全量
    if len(m) > lookback:
        m = m.tail(lookback)
    if len(m) < 200:
        m = sales[["期号", sig_col]].merge(hist[["期号", "_nums"]], on="期号", how="inner")
        m = m.dropna(subset=[sig_col])
        m = m[m[sig_col] > 0]

    # 特征矩阵：每期一行，[位×数字] 独热 + 截距
    n_feat = sum(zones)
    X = np.zeros((len(m), n_feat + 1))
    y = np.log(m[sig_col].to_numpy(dtype=float) + 1.0)
    for r, nums in enumerate(m["_nums"]):
        for i, d in enumerate(nums[:n_pos]):
            off = sum(zones[:i])
            if 0 <= d < zones[i]:
                X[r, off + d] = 1.0
    X[:, -1] = 1.0
    # 全零行（号码不足位）会导致特征丢失：验证至少有一列非零
    if X[:, :-1].sum() == 0:
        logger.warning(f"[crowd] {name} 号码列解析为空，不拟合")
        return None

    beta_full, *_ = np.linalg.lstsq(X, y, rcond=None)
    betas = np.zeros((n_pos, max(zones)))
    for i in range(n_pos):
        off = sum(zones[:i])
        betas[i, :zones[i]] = beta_full[off:off + zones[i]]
    intercept = float(beta_full[-1])
    resid = y - X @ beta_full
    sigma = float(np.std(resid))

    logger.info(f"[crowd] {name} 拟合完成: 样本={len(m)} sigma={sigma:.3f} "
                f"β幅度={np.abs(betas).max():.3f}")
    return CrowdModel(name, betas, intercept, zones, sigma)


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    for name in (sys.argv[1:] or ["排列3", "福彩3D", "排列5", "七星彩"]):
        model = fit_crowd_model(name)
        if model is None:
            print(f"[{name}] 无模型（信号不足/无销售数据）→ 限号EV 降级为名义EV")
            continue
        # 找最冷/最热号码展示
        if name in ("排列3", "福彩3D"):
            cands = [(i, [i // 100 % 10, i // 10 % 10, i % 10]) for i in range(1000)]
        else:
            cands = [(i, [i // 10000 % 10, i // 1000 % 10, i // 100 % 10, i // 10 % 10, i % 10]) for i in range(100000)]
        scored = sorted(cands, key=lambda t: model.score(t[1]))
        cold, hot = scored[0], scored[-1]
        print(f"[{name}] 最冷门号={cold[1]} score={model.score(cold[1]):.3f} "
              f"拥挤分位={model.crowding_quantile(cold[1]):.2f}")
        print(f"        最热门号={hot[1]} score={model.score(hot[1]):.3f} "
              f"拥挤分位={model.crowding_quantile(hot[1]):.2f}")
