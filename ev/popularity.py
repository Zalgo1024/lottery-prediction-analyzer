"""
ev/popularity.py —— 双色球/大乐透 组合流行度模型（NetPayout WP1 + WP3）

定位（与 ev/crowd_model.py 的数字型 v2 对应，补齐乐透型缺口）：
- 不预测开奖（裁判层已证伪），只回答一件事：**哪些组合被更多人买**。
- 数据地基：双/大 CSV 自带 一等奖注数 + 总投注额 →
  某期开出组合 C 的一等奖注数 ≈ 当期票量 × P(一等) × exp(β·特征(C))。
  对数泊松回归反推 β（offset = log(票量)），β 显著 → 形态信号存在。

特征（红球组合，9 维，全部是"人类选号偏好"的先验代理）：
  生日月占比   ≤12 的红球个数占比（月份号）
  生日日占比   ≤31 的红球个数占比（日期号；双色球 32/33、大乐透 32-35 为冷区）
  连号长度     最大连续段长度（截断到 3）
  跨度         (max-min)/33
  和值偏离     sum/(期望和值)
  奇占比       奇数个数占比
  尾数重复数   相同尾数的重复对数
  半区偏斜     |低半区个数 - k/2| / k
  上期重合数   与上一期红球的重合个数（"追热号"人群）

模型与验证（Go/No-Go，方案文档 §5-WP1）：
- 全量拟合：GLM Poisson(log)，offset=log(票量)。
- LRT：full(9特征) vs null(仅截距+offset)，χ²(k)，p<0.01 才算"信号存在"。
- 时序 5 折 CV：expanding window，指标=测试折泊松偏差，full 须稳定优于 null。
- **不显著 → 实得轨道降级为「未发现可量化收益」并公开否决（诚实产出）。**

WP3 反事实回测（counterfactual_backtest）：
- 可分配池 = 一等奖奖金 × 一等奖注数（当期真实值）。
- 反事实：若持有流行度分位 q 的组合且当期命中，期望实得 = 池 × E[1/(1+X)]，
  X~Poisson(μ_q)，μ_q = 票量 × P(一等) × exp(β·f(q分位组合))。
- 报告冷门(q=0.1) vs 热门(q=0.9) 的实得提升倍数中位数 + 预测μ vs 实际中奖注数的 Spearman。

诚实边界（MVP）：
- 蓝球/后区的流行度未建模（双色球蓝球仅 16 个、影响远小于 33 选 6 的红球形态），
  quantile 只对红球组合枚举，蓝球影响并入残差。
"""
from __future__ import annotations

import logging
import math
from itertools import combinations
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 特征名（顺序即矩阵列序，勿改动——β 与之对齐）
FEATURE_NAMES = [
    "生日月占比", "生日日占比", "连号长度", "跨度", "和值偏离",
    "奇占比", "尾数重复数", "半区偏斜", "上期重合数",
]

# 组合枚举缓存：lottery → (combos (M,k) int16, scores_sorted)
_ENUM_CACHE: Dict[str, Tuple[np.ndarray, np.ndarray]] = {}


# ---------------- 特征工程（向量化） ----------------
def features_matrix(R: np.ndarray, prev: Optional[np.ndarray] = None) -> np.ndarray:
    """
    R: (n, k) 红球矩阵（每行一注，无序）。prev: (n, k) 上一期红球（对齐行序）。
    返回 (n, 9) 特征矩阵，列序 = FEATURE_NAMES。
    """
    R = np.asarray(R, dtype=np.int16)
    n_rows, k = R.shape
    Rs = np.sort(R, axis=1)

    f_le12 = (R <= 12).sum(axis=1) / k
    f_le31 = (R <= 31).sum(axis=1) / k

    # 最大连号长度（截断到 3）
    diffs = np.diff(Rs, axis=1)
    is_run = np.concatenate([np.ones((n_rows, 1), dtype=bool), diffs == 1], axis=1)
    max_run = np.zeros(n_rows, dtype=np.int16)
    cur = np.zeros(n_rows, dtype=np.int16)
    for j in range(is_run.shape[1]):
        cur = np.where(is_run[:, j], cur + 1, 1)
        max_run = np.maximum(max_run, cur)
    f_run = np.minimum(max_run, 3).astype(np.float64)

    f_span = (Rs[:, -1] - Rs[:, 0]) / 33.0
    f_sum = R.sum(axis=1) / (k * (k + 1) / 2.0 * (33 / k))  # 归一到 ~1（粗归一即可，回归里再标准化）

    f_odd = (R % 2 == 1).sum(axis=1) / k

    tails = R % 10
    # 尾数重复对数：Σ_t C(cnt_t, 2)
    f_tail = np.zeros(n_rows)
    for t in range(10):
        cnt = (tails == t).sum(axis=1)
        f_tail += cnt * (cnt - 1) / 2.0

    half = k / 2.0
    low_cnt = (R <= 17).sum(axis=1)          # 1-17 低半区（33 个号的分界）
    f_half = np.abs(low_cnt - half) / k

    if prev is not None:
        prev_sorted = np.sort(np.asarray(prev, dtype=np.int16), axis=1)
        # 行级交集大小（k≤6，直接双循环向量化）
        f_prev = np.zeros(n_rows)
        for i in range(k):
            hit = (R == prev_sorted[:, [i]]).any(axis=1)
            f_prev += hit
    else:
        f_prev = np.zeros(n_rows)

    X = np.column_stack([
        f_le12, f_le31, f_run, f_span, f_sum,
        f_odd, f_tail, f_half, f_prev,
    ]).astype(np.float64)
    return X


# ---------------- 数据装载 ----------------
def _load_draw_rows(lottery: str) -> Optional[Dict[str, np.ndarray]]:
    """按时间升序返回 {"reds": (n,k), "prev": (n,k), "y": (n,) 一等奖注数, "N": (n,) 票量, "issues": [...]}"""
    try:
        from data.loader import load_lottery
        data = load_lottery(lottery)
    except Exception as e:
        logger.error(f"[popularity] 读 {lottery} 失败: {e}")
        return None
    recs = sorted(data.records, key=lambda r: r.期号)  # 升序
    schema = None
    from data.schema import get_schema
    schema = get_schema(lottery)
    red_zone = schema.red_zone
    k = red_zone.choose if red_zone else 6

    reds, prev, y, N, issues = [], [], [], [], []
    last_reds = None
    for r in recs:
        red = list(r.红球) if hasattr(r, "红球") else []
        if len(red) != k:
            last_reds = red if len(red) == k else last_reds
            continue
        cnt = getattr(r, "一等奖注数", None)
        sales = getattr(r, "总投注额", None)
        if cnt is None or sales is None or not sales or sales <= 0:
            last_reds = red
            continue
        reds.append(red)
        prev.append(last_reds if last_reds is not None and len(last_reds) == k else red)
        y.append(int(cnt))
        N.append(float(sales) / 2.0)
        issues.append(str(r.期号))
        last_reds = red

    if len(reds) < 200:
        logger.warning(f"[popularity] {lottery} 有效样本仅 {len(reds)}，不足拟合")
        return None
    return {
        "reds": np.array(reds, dtype=np.int16),
        "prev": np.array(prev, dtype=np.int16),
        "y": np.array(y, dtype=np.float64),
        "N": np.array(N, dtype=np.float64),
        "issues": issues,
    }


# ---------------- 拟合与检验 ----------------
def _poisson_deviance(y: np.ndarray, mu: np.ndarray) -> np.ndarray:
    """逐点泊松偏差（y=0 时退化 2μ）。"""
    out = 2.0 * mu
    m = y > 0
    out[m] = 2.0 * (y[m] * np.log(y[m] / mu[m]) - (y[m] - mu[m]))
    return out


def _fit_glm(y, X, offset):
    import statsmodels.api as sm
    return sm.GLM(y, X, family=sm.families.Poisson(), offset=offset).fit()


def fit_popularity(lottery: str) -> Dict[str, Any]:
    """
    拟合 + LRT + 时序 CV。返回报告 dict（含 Go/No-Go 裁决）。
    拟合失败/样本不足 → {"applicable": False, ...}
    """
    rows = _load_draw_rows(lottery)
    if rows is None:
        return {"lottery": lottery, "applicable": False,
                "verdict": "样本不足或数据缺失，无法检验"}

    X_raw = features_matrix(rows["reds"], rows["prev"])
    y = rows["y"]
    offset = np.log(rows["N"])
    n, k_feat = X_raw.shape

    # 标准化（用全量统计量；CV 内部重新标准化）
    mu_, sd_ = X_raw.mean(axis=0), X_raw.std(axis=0)
    sd_[sd_ < 1e-12] = 1.0
    X = (X_raw - mu_) / sd_

    ones = np.ones((n, 1))
    try:
        glm_full = _fit_glm(y, np.column_stack([ones, X]), offset)
        glm_null = _fit_glm(y, ones, offset)
    except Exception as e:
        return {"lottery": lottery, "applicable": False, "verdict": f"GLM 拟合失败: {e}"}

    llf_full, llf_null = glm_full.llf, glm_null.llf
    lrt_stat = 2.0 * (llf_full - llf_null)
    from scipy import stats as sps
    lrt_p = float(sps.chi2.sf(max(lrt_stat, 0.0), k_feat))

    # 时序 5 折 expanding CV
    n_folds = 5
    fold = max(n // (n_folds + 1), 50)
    cv_full, cv_null = [], []
    for i in range(1, n_folds + 1):
        cut = fold * i
        if cut + fold > n:
            break
        tr_X_raw, te_X_raw = X_raw[:cut], X_raw[cut:cut + fold]
        tr_y, te_y = y[:cut], y[cut:cut + fold]
        tr_off, te_off = offset[:cut], offset[cut:cut + fold]
        m_, s_ = tr_X_raw.mean(axis=0), tr_X_raw.std(axis=0)
        s_[s_ < 1e-12] = 1.0
        tr_X = np.column_stack([np.ones(len(tr_y)), (tr_X_raw - m_) / s_])
        te_X = np.column_stack([np.ones(len(te_y)), (te_X_raw - m_) / s_])
        try:
            gf = _fit_glm(tr_y, tr_X, tr_off)
            gn = _fit_glm(tr_y, np.ones((len(tr_y), 1)), tr_off)
            mu_f = gf.predict(te_X, offset=te_off)
            mu_n = gn.predict(np.ones((len(te_y), 1)), offset=te_off)
            cv_full.append(float(_poisson_deviance(te_y, mu_f).mean()))
            cv_null.append(float(_poisson_deviance(te_y, mu_n).mean()))
        except Exception as e:
            logger.warning(f"[popularity] CV 第 {i} 折失败: {e}")

    # 裁决
    cv_better = sum(1 for a, b in zip(cv_full, cv_null) if a < b - 1e-9)
    go = (lrt_p < 0.01) and cv_better >= math.ceil(len(cv_full) * 0.6) and cv_full
    if go:
        verdict = (f"信号存在：LRT p={lrt_p:.2e} 且 {cv_better}/{len(cv_full)} 折 CV 优于零模型 "
                   f"→ 可进入 WP2/WP3")
    else:
        verdict = (f"未达门槛：LRT p={lrt_p:.3g}，CV 优于零模型 {cv_better}/{len(cv_full)} 折 "
                   f"→ 形态信号不显著，实得轨道按方案公开否决")

    return {
        "lottery": lottery,
        "applicable": True,
        "n": n,
        "y_mean": round(float(y.mean()), 3),
        "y_zero_rate": round(float((y == 0).mean()), 3),
        "features": FEATURE_NAMES,
        "beta_raw": [round(float(b), 4) for b in glm_full.params[1:]],
        "beta_p": [round(float(p), 4) for p in glm_full.pvalues[1:]],
        # 未取整数组（get_popularity_model 直接复用，避免二次拟合）
        "beta_full": [float(b) for b in glm_full.params[1:]],
        "feat_mean": [float(v) for v in mu_],
        "feat_std": [float(v) for v in sd_],
        "lrt_stat": round(float(lrt_stat), 2),
        "lrt_df": k_feat,
        "lrt_p": lrt_p,
        "cv_full": [round(v, 4) for v in cv_full],
        "cv_null": [round(v, 4) for v in cv_null],
        "cv_better_folds": cv_better,
        "verdict": verdict,
        "go": bool(go),
    }


# ---------------- 预测：组合流行度分位 ----------------
def _enum_combos(k: int) -> np.ndarray:
    """全部红球组合 (M, k)（双色球 1,107,568 / 大乐透 324,632），int16 省内存。"""
    from data.schema import get_schema
    z = get_schema(_name_by_k(k)).red_zone
    lo, hi = z.range_tuple()
    return np.array(list(combinations(range(lo, hi + 1), k)), dtype=np.int16)


_K2NAME = {6: "双色球", 5: "大乐透"}


def _name_by_k(k: int) -> str:
    if k not in _K2NAME:
        raise ValueError(f"未知红球位数: {k}")
    return _K2NAME[k]


def popularity_quantile(lottery: str, reds: List[int],
                        beta: Optional[np.ndarray] = None,
                        mu_: Optional[np.ndarray] = None,
                        sd_: Optional[np.ndarray] = None) -> float:
    """
    组合流行度分位（0=最冷 1=最热）：对全部红球组合的特征打分后取分位。
    缺省参数时自动用 get_popularity_model 的缓存拟合结果（不再每次重拟合）；
    全组合打分走 _get_scores_cached 三级缓存（进程内/磁盘/现算）。
    蓝球未建模（MVP 边界，见模块 docstring）。
    """
    if beta is None:
        params = get_popularity_model(lottery)
        if not params.get("go"):
            return float("nan")
        beta, mu_, sd_ = params["beta"], params["m_"], params["s_"]
    s_ = sd_ if sd_ is not None else np.ones(len(beta))
    m_ = mu_ if mu_ is not None else np.zeros(len(beta))

    scores_sorted, _, _ = _get_scores_cached(lottery, beta, m_, s_)

    q = features_matrix(np.array([reds], dtype=np.int16))
    own = float((((q - m_) / s_) @ np.asarray(beta, dtype=np.float64))[0])
    return float((scores_sorted <= own).mean())


# 全组合打分缓存：lottery → (参数指纹, 排序后分数)
_SCORE_CACHE: Dict[str, Tuple[str, np.ndarray]] = {}


def _scores_fingerprint(beta, m_, s_) -> str:
    """参数指纹（模型参数变 → 打分缓存失效）。"""
    import hashlib
    h = hashlib.md5()
    for a in (beta, m_, s_):
        h.update(np.round(np.asarray(a, dtype=np.float64), 10).tobytes())
    return h.hexdigest()[:12]


def _get_scores_cached(lottery: str, beta: np.ndarray, m_: np.ndarray, s_: np.ndarray,
                       q_cold: float = 0.1, q_hot: float = 0.9
                       ) -> Tuple[np.ndarray, List[int], List[int]]:
    """
    全组合标准化打分（排序后）+ 冷/热参考组合，三级缓存：
    进程内 → 磁盘 cache/popularity_scores_<lottery>.npz（110 万组合枚举+特征约 3~4s，
    Web 重启后首载不再重算）→ 现算。参数指纹不匹配自动重算。
    """
    fp = _scores_fingerprint(beta, m_, s_)

    hit = _SCORE_CACHE.get(lottery)
    if hit and hit[0] == fp:
        return hit[1], hit[2], hit[3]

    import os
    import json as _json
    npz_path = os.path.join(os.path.dirname(_model_cache_path(lottery)),
                            f"popularity_scores_{lottery}.npz")
    try:
        if os.path.exists(npz_path):
            z = np.load(npz_path, allow_pickle=False)
            if str(z["fp"]) == fp and abs(float(z["q_cold"]) - q_cold) < 1e-9 \
                    and abs(float(z["q_hot"]) - q_hot) < 1e-9:
                scores = z["scores"]
                cold = [int(v) for v in z["cold"]]
                hot = [int(v) for v in z["hot"]]
                _SCORE_CACHE[lottery] = (fp, scores, cold, hot)
                return scores, cold, hot
    except Exception as e:
        logger.debug(f"[popularity] 读打分缓存失败 {lottery}: {e}")

    # 现算：枚举 + 特征 + 打分
    from data.schema import get_schema
    k = get_schema(lottery).red_zone.choose
    if lottery not in _ENUM_CACHE:
        combos = _enum_combos(k)
        S = features_matrix(combos)
        _ENUM_CACHE[lottery] = (combos, S)
        logger.info(f"[popularity] 枚举 {lottery} 组合 {combos.shape[0]} 条完成")
    combos, S = _ENUM_CACHE[lottery]
    scores_all = ((S - m_) / s_) @ np.asarray(beta, dtype=np.float64)
    order = np.argsort(scores_all)
    scores_sorted = scores_all[order]
    cold_combo = combos[order[int(q_cold * (len(order) - 1))]].tolist()
    hot_combo = combos[order[int(q_hot * (len(order) - 1))]].tolist()

    _SCORE_CACHE[lottery] = (fp, scores_sorted, cold_combo, hot_combo)
    try:
        np.savez_compressed(npz_path, fp=fp, scores=scores_sorted,
                            cold=np.asarray(cold_combo, dtype=np.int16),
                            hot=np.asarray(hot_combo, dtype=np.int16),
                            q_cold=q_cold, q_hot=q_hot)
    except Exception as e:
        logger.debug(f"[popularity] 写打分缓存失败 {lottery}: {e}")
    return scores_sorted, cold_combo, hot_combo


# ---------------- 向量化批量 μ（选号器专用，避免逐注 payout_eff） ----------------
def batch_mu(lottery: str, R: np.ndarray, N: float, p_grade: float,
             params: Dict[str, Any]) -> np.ndarray:
    """
    一次算一批组合的期望同奖中奖注数 μ = N × p_grade × exp(β·f(组合))。
    R: (m, k) 红球矩阵；返回 (m,)。参数非法/模型未 Go 时返回全 NaN。
    """
    if not params.get("go"):
        return np.full(len(R), np.nan)
    F = features_matrix(np.asarray(R, dtype=np.int16))
    z = (F - params["m_"]) / params["s_"]
    return float(N) * float(p_grade) * np.exp(z @ params["beta"])


# ---------------- 拟合参数缓存（供 payout/selection 复用） ----------------
_MODEL_CACHE: Dict[str, Dict[str, Any]] = {}


def _model_cache_path(lottery: str):
    """磁盘缓存路径：cache/popularity_model_<lottery>.json（cache/ 已 gitignore）。"""
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    d = os.path.join(root, "cache")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, f"popularity_model_{lottery}.json")


def _load_model_from_disk(lottery: str) -> Optional[Dict[str, Any]]:
    """读磁盘缓存；样本数与当前数据不一致 → 视为过期返回 None（数据更新自动重拟合）。"""
    import json
    import os
    try:
        p = _model_cache_path(lottery)
        if not os.path.exists(p):
            return None
        with open(p, "r", encoding="utf-8") as f:
            obj = json.load(f)
        rows = _load_draw_rows(lottery)
        if rows is None or int(obj.get("n_samples", -1)) != int(len(rows["reds"])):
            return None
        if not obj.get("go"):
            return {"go": False, "report": obj.get("report", {})}
        return {
            "go": bool(obj["go"]),
            "beta": np.asarray(obj["beta"], dtype=np.float64),
            "m_": np.asarray(obj["feat_mean"], dtype=np.float64),
            "s_": np.asarray(obj["feat_std"], dtype=np.float64),
            "report": obj["report"],
        }
    except Exception as e:
        logger.debug(f"[popularity] 读磁盘缓存失败 {lottery}: {e}")
        return None


def _save_model_to_disk(lottery: str, params: Dict[str, Any]) -> None:
    import json
    try:
        obj = {
            "go": bool(params.get("go")),
            "beta": [float(v) for v in params.get("beta", [])],
            "feat_mean": [float(v) for v in params.get("m_", [])],
            "feat_std": [float(v) for v in params.get("s_", [])],
            "n_samples": params.get("report", {}).get("n"),
            "report": params.get("report", {}),
        }
        with open(_model_cache_path(lottery), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False)
    except Exception as e:
        logger.debug(f"[popularity] 写磁盘缓存失败 {lottery}: {e}")


def get_popularity_model(lottery: str) -> Dict[str, Any]:
    """
    拟合参数缓存：{"go": bool, "beta", "m_", "s_", "report"}；未达门槛 go=False。

    两级缓存：进程内 dict → 磁盘 cache/（样本数变化自动过期）。
    复用 fit_popularity 报告里未取整的 β/标准化参数，**不再二次拟合**；
    供 ev/payout（奖池×E[1/(1+X)] 号码相关路径）与 ev/selection 复用。
    """
    if lottery in _MODEL_CACHE:
        return _MODEL_CACHE[lottery]

    disk = _load_model_from_disk(lottery)
    if disk is not None:
        _MODEL_CACHE[lottery] = disk
        return disk

    rep = fit_popularity(lottery)
    out: Dict[str, Any] = {"go": False, "report": rep}
    if rep.get("applicable") and rep.get("go") and rep.get("beta_full"):
        out = {"go": True,
               "beta": np.asarray(rep["beta_full"], dtype=np.float64),
               "m_": np.asarray(rep["feat_mean"], dtype=np.float64),
               "s_": np.asarray(rep["feat_std"], dtype=np.float64),
               "report": rep}
    # GO 与 NO-GO 都落盘（NO-GO 缓存可避免重启后重复拟合否决结论）
    _save_model_to_disk(lottery, out)
    _MODEL_CACHE[lottery] = out
    return out


def ticket_mu(lottery: str, reds, N: float, p_grade: float, params: Dict[str, Any]) -> float:
    """
    某奖级的期望同奖中奖注数 μ = N × p_grade × exp(β·f(组合))。
    二等及以下复用红球形态因子（蓝球未建模的 MVP 近似，见模块 docstring）。
    """
    f = features_matrix(np.array([reds], dtype=np.int16))[0]
    z = (f - params["m_"]) / params["s_"]
    return float(N * p_grade * math.exp(float(z @ params["beta"])))


# ---------------- WP3：历史反事实回测 ----------------
def counterfactual_backtest(lottery: str, q_cold: float = 0.1, q_hot: float = 0.9) -> Dict[str, Any]:
    """
    反事实回测：若当期开出的组合换成「流行度 q 分位的组合」且命中，
    期望实得 = 可分配池 × E[1/(1+X)]，X~Poisson(μ_q)。

    可分配池 = 一等奖奖金 × 一等奖注数（当期真实值，双/大 CSV 自带）。
    μ_q = 票量 × P(一等) × exp(β·f(该分位组合))，β 来自 fit_popularity。
    """
    params = get_popularity_model(lottery)
    if not params.get("go"):
        rep = params.get("report", {})
        return {"lottery": lottery, "applicable": bool(rep.get("applicable")),
                "go": False,
                "note": "流行度模型未达 Go 门槛，反事实回测不具解释力（诚实否决）",
                "lrt_p": rep.get("lrt_p")}

    beta = params["beta"]
    m_, s_ = params["m_"], params["s_"]

    rows = _load_draw_rows(lottery)
    if rows is None:
        return {"lottery": lottery, "applicable": False, "go": False,
                "note": "数据缺失，无法回测"}
    y = rows["y"]

    from ev.payout import grade_probabilities
    p1 = grade_probabilities(lottery)["一等"]

    # 冷/热参考组合：与全组合打分共用三级缓存（参数指纹一致即复用）
    _, cold_combo, hot_combo = _get_scores_cached(lottery, beta, m_, s_, q_cold, q_hot)

    # 逐期反事实（封顶显式建模：池 ≫ cap×μ 时冷门优势被 1000 万封顶压缩）
    # 向量化：f(组合) 与期数无关 → 形态因子 exp(β·f) 只算一次，μ = N × 常数；
    # 各期奖池不同 → 走 pool_share_expected_table 一次性算完。
    from ev.payout import pool_share_expected_table
    from data.loader import load_lottery
    cap = 10_000_000
    data = load_lottery(lottery)
    rec_map = {str(r.期号): r for r in data.records}
    k_c = p1 * math.exp(float(((features_matrix(np.array([cold_combo], dtype=np.int16))[0] - m_) / s_) @ beta))
    k_h = p1 * math.exp(float(((features_matrix(np.array([hot_combo], dtype=np.int16))[0] - m_) / s_) @ beta))

    N_all, yc_all = rows["N"], y
    pools = np.zeros(len(N_all))
    keep = np.zeros(len(N_all), dtype=bool)
    for i, iss in enumerate(rows["issues"]):
        rec = rec_map.get(iss)
        prize = getattr(rec, "一等奖奖金", None) if rec is not None else None
        if prize and yc_all[i] > 0 and N_all[i] > 0:
            pools[i] = float(prize) * float(yc_all[i])
            keep[i] = True

    pools_v = pools[keep]
    mu_c = N_all[keep] * k_c
    mu_h = N_all[keep] * k_h
    eff_cold_arr = pool_share_expected_table(pools_v, mu_c, cap)
    eff_hot_arr = pool_share_expected_table(pools_v, mu_h, cap)
    valid = (mu_c > 0) & (mu_h > 0) & (eff_hot_arr > 0)
    ratios_arr_full = np.where(valid, eff_cold_arr / np.where(eff_hot_arr > 0, eff_hot_arr, 1.0), np.nan)
    ratios = ratios_arr_full[valid].tolist()
    mu_cold_list = mu_c[valid].tolist()
    mu_hot_list = mu_h[valid].tolist()

    # 分段（漂移监控用）：段 = 时间顺序等分，看冷门优势是否随时间变化。
    # 依赖 _load_draw_rows 按期号/日期升序返回（loader 口径）。
    n_seg = 5 if len(N_all) >= 250 else 1
    seg_id = (np.arange(len(N_all)) * n_seg) // max(len(N_all), 1)
    seg_valid = seg_id[keep][valid]
    seg_uplift = []
    for j in range(n_seg):
        m = seg_valid == j
        if int(m.sum()) >= 30:
            seg_uplift.append({
                "段": j + 1,
                "期数": int(m.sum()),
                "倍数_中位": round(float(np.median(ratios_arr_full[valid][m])), 3),
            })

    if not ratios:
        return {"lottery": lottery, "applicable": True, "go": True,
                "note": "无可回测期（缺一等奖奖金或中奖注数）"}

    ratios_arr = np.array(ratios)
    # 预测 μ vs 实际归一化中奖注数（每亿销量）的 Spearman
    # （截距与标准化常数在 log 空间是常数项，不改变秩，故用标准化特征即可）
    X_raw = features_matrix(rows["reds"], rows["prev"])
    Xz = (X_raw - m_) / s_
    mu_pred = rows["N"] * p1 * np.exp(Xz @ beta)
    norm_y = y / (rows["N"] / 1e8)
    from scipy import stats as sps
    sp = sps.spearmanr(mu_pred, norm_y)
    return {
        "lottery": lottery,
        "applicable": True,
        "go": True,
        "n_draws": int(len(ratios_arr)),
        "冷门组合": "".join(f"{d:02d}" for d in cold_combo),
        "热门组合": "".join(f"{d:02d}" for d in hot_combo),
        "μ_冷门_中位": round(float(np.median(mu_cold_list)), 4),
        "μ_热门_中位": round(float(np.median(mu_hot_list)), 4),
        "实得提升倍数_中位": round(float(np.median(ratios_arr)), 3),
        "实得提升倍数_P25": round(float(np.percentile(ratios_arr, 25)), 3),
        "实得提升倍数_P75": round(float(np.percentile(ratios_arr, 75)), 3),
        "spearman_预测μ_vs_实际中奖率": round(float(sp.statistic), 4),
        "spearman_p": round(float(sp.pvalue), 5),
        "分段提升": seg_uplift,
        "结论": (f"持有{q_cold:.0%}分位冷门组合，中头奖时期望实得是热门组合的 "
                 f"{np.median(ratios_arr):.2f} 倍（{len(ratios_arr)} 期反事实；"
                 f"不改变中奖概率本身）"),
    }


# ---------------- 漂移监控：人群选号行为是否随时间变化 ----------------
def drift_report(lottery: str, n_segments: int = 5) -> Dict[str, Any]:
    """
    回答一个问题：**「冷门组合中奖拿更多」这个结论现在还成立吗？**

    人群选号行为会漂移（派奖活动、机选比例上升、新玩法、宣传口径变化），
    β 一旦过时，1.48× 这个数就只是历史平均值而非当下值。本函数给出两个证据：

     ① 分段提升：把历史按时间等分，看冷门/热门实得比是否稳定（直观）
     ② β 前后半段对比：两半分别拟合，联合卡方检验 β 是否发生位移（统计）

    裁决：
      - 稳定   → 维持现有模型（磁盘缓存继续有效，样本数变化会自动过期重拟合）
      - 漂移   → 缩短拟合窗口 / 提高重拟合频率，并如实标注"结论的时间适用范围"

    ⚠️ 这是**维护**动作，不是"让模型变强"——漂移修正只保证结论不过时，
       不会提高提升倍数本身（其天花板由全样本估计给出）。
    """
    params = get_popularity_model(lottery)
    rep = params.get("report", {}) or {}
    if not params.get("go"):
        return {"彩种": lottery, "go": False, "applicable": bool(rep.get("applicable")),
                "裁决": "不适用",
                "建议": "流行度模型未达 Go 门槛，无需监控漂移（本彩种 EV 与号码无关）"}

    from scipy import stats as sps

    rows = _load_draw_rows(lottery)
    if rows is None:
        return {"彩种": lottery, "go": True, "裁决": "数据缺失", "建议": "无法读取历史开奖，跳过漂移检查"}

    y, N = rows["y"], rows["N"]
    X_raw = features_matrix(rows["reds"], rows["prev"])
    m_, s_ = X_raw.mean(axis=0), X_raw.std(axis=0)
    s_[s_ < 1e-12] = 1.0
    Xz = (X_raw - m_) / s_

    # ② β 前后半段对比
    n = len(y)
    half = n // 2
    halves, z_vec = [], None
    if half >= 200:
        fits = []
        for sl in (slice(0, half), slice(half, n)):
            k = sl.stop - sl.start
            g = _fit_glm(y[sl], np.column_stack([np.ones(k), Xz[sl]]), np.log(N[sl]))
            fits.append((np.asarray(g.params[1:], dtype=float),
                         np.asarray(g.bse[1:], dtype=float)))
        b1, se1 = fits[0]
        b2, se2 = fits[1]
        z_vec = (b1 - b2) / np.sqrt(se1 ** 2 + se2 ** 2 + 1e-18)
        halves = [round(float(v), 4) for v in b1], [round(float(v), 4) for v in b2]

    chi2 = p_val = None
    worst_feat, worst_z = None, None
    if z_vec is not None:
        chi2 = float((z_vec ** 2).sum())
        p_val = float(sps.chi2.sf(chi2, len(z_vec)))
        wi = int(np.argmax(np.abs(z_vec)))
        worst_feat = FEATURE_NAMES[wi] if wi < len(FEATURE_NAMES) else f"f{wi}"
        worst_z = round(float(z_vec[wi]), 3)

    # ① 分段提升（复用回测，向量化 ~0.6s）
    cf = counterfactual_backtest(lottery)
    segs = cf.get("分段提升") or []

    # 漂移裁决（分三档：结论层面漂移 / 仅系数位移 / 稳定）
    beta_drift = bool(p_val is not None and p_val < 0.01)
    uplift_drift = False
    tail_med = None
    seg_vals = [s["倍数_中位"] for s in segs]
    if len(seg_vals) >= 3:
        first, last = seg_vals[0], seg_vals[-1]
        uplift_drift = bool(first > 0 and abs(last - first) / first > 0.20)
        tail_med = round(float(np.median(seg_vals[1:])), 3)

    if uplift_drift:
        verdict = "漂移告警（结论层面）"
        advice = ("冷门提升倍数随时间发生 >20% 位移 → 当前倍数应视为历史平均而非当下值；"
                  "建议缩短拟合窗口（如近 1500 期）重新检验后再对外引用。")
    elif beta_drift:
        verdict = "系数位移（结论仍稳定）"
        advice = (f"β 有统计显著位移，但分段提升倍数保持稳定"
                  f"（剔除最早段后中位 {tail_med}×）→ 结论仍适用；"
                  f"早期段偏低多源于早期销量/数据质量差异。保持定期重拟合即可，无需缩短窗口。")
    else:
        verdict = "稳定"
        advice = ("未见显著漂移，现有模型可继续使用。缓存模型已按样本数自动过期，"
                  "新期数据到达后会自行重拟合（无需手动维护）。")

    # 近期校准复核：把「总量漂移(截距)」与「形态效应漂移(β)」分开。
    # scale = N·p1·exp(β·f)（不含截距），y/scale 的期望 = exp(截距)；
    # 用 Σy/Σscale 估计两段各自的截距尺度，比值≈1 表示总量校准未漂移。
    recent_calib = None
    try:
        from ev.payout import grade_probabilities
        p1 = grade_probabilities(lottery)["一等"]
        scale = np.maximum(N * p1 * np.exp(Xz @ np.asarray(params["beta"], dtype=float)), 1e-12)
        cut = max(int(n * 0.75), 1)
        s_recent = float(y[cut:].sum() / scale[cut:].sum())
        s_early = float(y[:cut].sum() / scale[:cut].sum())
        recent_calib = round(s_recent / s_early, 3) if s_early > 0 else None
    except Exception as e:
        logger.debug(f"[popularity] 近期校准复核失败 {lottery}: {e}")

    stale = None
    n_now = int(rep.get("n") or 0)
    if n_now:
        stale = max(0, int(n) - n_now)

    advice = advice + (f"（近期/早期校准比={recent_calib}，1.0 表示总量未漂移）"
                       if recent_calib is not None else "")

    return {
        "彩种": lottery,
        "go": True,
        "样本期数": int(n),
        "缓存模型期数": n_now or None,
        "落后期数": stale,
        "分段提升": segs,
        "β_前半段": halves[0] if halves else None,
        "β_后半段": halves[1] if halves else None,
        "β漂移卡方": None if chi2 is None else round(chi2, 2),
        "β漂移_df": None if z_vec is None else int(len(z_vec)),
        "β漂移_p": None if p_val is None else round(p_val, 5),
        "最漂移特征": worst_feat,
        "最漂移特征_z": worst_z,
        "近期校准比": recent_calib,
        "剔除最早段后倍数中位": tail_med,
        "裁决": verdict,
        "建议": advice,
        "性质说明": "漂移修正=维护（保证结论不过时），不会提高提升倍数本身",
    }


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(errors="replace")
    for lot in (sys.argv[1:] or ["双色球", "大乐透"]):
        print(json.dumps(fit_popularity(lot), ensure_ascii=False, indent=1))
        print(json.dumps(counterfactual_backtest(lot), ensure_ascii=False, indent=1))
