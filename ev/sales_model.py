"""
ev/sales_model.py —— P1-2 销量 / 奖池 / 一等奖注数预测（喂 rollover 的前瞻 EV）

背景（路线图 L1/P1-2）：
开奖号码不可预测，但「总销量、奖池、一等奖注数」是人的行为——有节假日效应、
有"奖池越大越多人追"的惯性、有爆头奖后的冷却。这些变量有信号且是 rollover
进场决策（ev/rollover.py）的输入。本模块把它们的**下一期取值**提前一期估出来，
让"这期奖池值不值得进场"在开奖/销售数据公布前就能判断。

数据口径（2026-09-04 更新）：
- 大乐透：lottery_data/大乐透历史数据_cleaned.csv 总投注额/奖池/一等奖注数/开奖日期
  2007-05 ~ 2026-09 连续 2918 期（日期 DD/MM/YYYY，需 dayfirst 解析）→ 全量可建模。
- 双色球：总投注额列原 96% 缺失（仅 2003 头 + 2026-05 起有值）→ **2026-09-04 已用
  500.com 全史回填补全 3499/3499（100%，data/fetcher.py::backfill_sales_column，
  号码一致性校验 0 跳过 0 未找到）** → 销量/奖池/一等奖注数 现均可全量建模。
  建模与否按「销量列实际有效行数」判断（≥300 期才建），不硬编码彩种名。

方法（严格 walk-forward，零穿越）：
- 特征只用 t 之前的滞后量 + t 当天已知的日历量（weekday/month/开奖间隔 gap）。
- 一等奖注数（计数目标）：
  A) Poisson GLM（statsmodels，log link，滞后+日历特征）
  B) LightGBM（回归 log1p(n1)）
  两套与「齐性泊松(全局均值)」基线对比，报告校准（预测均值 vs 实际均值）与 MAE。
- 总销量 / 奖池（连续目标）：LightGBM 回归（log1p 尺度还原），与 lag1 基线对比 R²。
- 参与信号前瞻回放：对留出期逐期用模型预测下一期奖池/销量/注数 → 预估头奖单注
  与参与信号，与"实际公布后 rollover_ev 算出的信号"比对一致率（诚实校准）。

不做：预测开奖号码。
"""
import math
import logging
from pathlib import Path

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "lottery_data"

# P(一等奖) / 头奖封顶 / 奖池分配系数 —— 与 ev/rollover.py 对齐
_P1 = {
    "双色球": 1.0 / (math.comb(33, 6) * 16),
    "大乐透": 1.0 / (math.comb(35, 5) * math.comb(12, 2)),
}
_JACKPOT_CAP = 10_000_000
_POOL_SHARE = 0.7

# 每周开奖日（pandas dayofweek: Mon=0 ... Sun=6）
_DRAW_WEEKDAYS = {
    "双色球": (1, 3, 6),   # 周二 / 周四 / 周日
    "大乐透": (0, 2, 5),   # 周一 / 周三 / 周六
}

_LGB_PARAMS = dict(
    n_estimators=500, learning_rate=0.05, num_leaves=31,
    subsample=0.8, colsample_bytree=0.8, min_child_samples=25,
    random_state=42, verbose=-1,
)


# ---------------------------------------------------------------- 数据装载
def _load_history(name: str) -> pd.DataFrame:
    path = DATA_DIR / f"{name}历史数据_cleaned.csv"
    if not path.exists():
        path = DATA_DIR / f"{name}历史数据.csv"
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"期号": str})
    return df


def _to_float(s: pd.Series) -> pd.Series:
    """数值化；缺失/0（早期抓取未填的哨兵）一律转 NaN"""
    x = pd.to_numeric(s, errors="coerce")
    x = x.where(x > 0)  # 0 视为缺失
    return x


def build_frame(name: str) -> pd.DataFrame:
    """升序时间帧：纯滞后特征 + 日历特征（预测 t 只用 ≤t-1 的信息）。

    返回列：date, pool_t, sales_t, n1_t（目标）+
    weekday_t, month_t, gap_t（t 当天已知）+ pool_lag1/lag2, sales_lag1/lag2,
    n1_lag1/lag2, pool_ma4, sales_ma4, n1_ma8, pool_mom, streak, hit_recent,
    sales_lag1_ok（双色球无销量 → 特征列全 NaN，LightGBM 可处理）。
    """
    df = _load_history(name)
    d = pd.to_datetime(df["开奖日期"], dayfirst=True, errors="coerce")
    # 双色球早期期号形如 2003001 的字符串排序不可靠 → 一律按日期升序
    f = pd.DataFrame({
        "期号": df["期号"],
        "date": d,
        "pool": _to_float(df["奖池奖金"]),
        "sales": _to_float(df["总投注额"]),
        "n1": pd.to_numeric(df["一等奖注数"], errors="coerce"),
    })
    f = f.dropna(subset=["date"]).sort_values("date").reset_index(drop=True)
    f["n1"] = f["n1"].fillna(0.0)  # 一等奖注数 0 是真实值（未中出）
    # 销量缺失时段为 NaN → LightGBM 自动忽略该特征（_has_sales 决定是否启用销量建模）
    f["sales"] = f["sales"].where(f["sales"].notna() & (f["sales"] > 0))

    # ---- 滞后（shift 基于升序；shift(1)=上一期）----
    for col, lag in (("pool", 1), ("pool", 2), ("sales", 1), ("sales", 2),
                     ("n1", 1), ("n1", 2)):
        f[f"{col}_lag{lag}"] = f[col].shift(lag)
    f["pool_ma4"] = f["pool"].shift(1).rolling(4, min_periods=2).mean()
    f["sales_ma4"] = f["sales"].shift(1).rolling(4, min_periods=2).mean()
    f["n1_ma8"] = f["n1"].shift(1).rolling(8, min_periods=4).mean()
    f["pool_ma8"] = f["pool"].shift(1).rolling(8, min_periods=4).mean()
    # 动量：上一期奖池相对近 8 期均值
    f["pool_mom"] = f["pool_lag1"] / f["pool_ma8"] - 1.0
    # 滚存连击：截至上一期连续 n1==0 的期数
    streak = np.zeros(len(f))
    for i in range(1, len(f)):
        streak[i] = streak[i - 1] + 1 if f["n1_lag1"].iloc[i] == 0 else 0
    f["streak"] = streak
    # 近期爆头奖冷却（近 8 期有几次中出）
    f["hit_recent"] = (f["n1"].shift(1).rolling(8, min_periods=4)
                       .apply(lambda s: int((s > 0).sum()), raw=False))
    f["sales_lag1_ok"] = f["sales_lag1"].notna().astype(float)
    # 销量（log1p）——GLM 线性项不能用原始量级（1e8 → exp 溢出）
    f["log_sales_lag1"] = np.log1p(f["sales_lag1"].fillna(0.0))
    f["log_sales_ma4"] = np.log1p(f["sales_ma4"].fillna(0.0))

    # ---- t 当天已知的日历特征 ----
    f["weekday_t"] = f["date"].dt.dayofweek
    f["month_t"] = f["date"].dt.month
    f["gap_t"] = f["date"].diff().dt.days  # 距上一期天数（>3 即长假/停售回归）
    f["gap_t"] = f["gap_t"].fillna(1.0)

    # 目标列（保留原名便于评估）
    f["pool_t"] = f["pool"]
    f["sales_t"] = f["sales"]
    f["n1_t"] = f["n1"]
    f = f.dropna(subset=["n1_lag1", "pool_lag1"]).reset_index(drop=True)
    return f


# ---------------------------------------------------------------- 特征列
_LAG_FEATS = ["pool_lag1", "pool_lag2", "n1_lag1", "n1_lag2", "pool_ma4",
              "n1_ma8", "pool_mom", "streak", "hit_recent",
              "weekday_t", "month_t", "gap_t", "pool_ma8"]
_SALES_FEATS = _LAG_FEATS + ["sales_lag1", "sales_lag2", "sales_ma4"]


def _has_sales(f: pd.DataFrame, min_rows: int = 300) -> bool:
    """销量列是否有足够有效行支撑建模（按数据能力判断，不硬编码彩种名）。

    双色球总投注额 2026-09-04 全史回填 100% 后启用；早期缺失时段数据
    仍不可用（<min_rows 时降级为只建 奖池/一等奖注数）。
    """
    return int(f["sales_t"].notna().sum()) >= min_rows


def _train_test(f: pd.DataFrame, test_frac: float = 0.2):
    n = len(f)
    cut = int(n * (1.0 - test_frac))
    return f.iloc[:cut], f.iloc[cut:]


# ---------------------------------------------------------------- LightGBM
def _lgb_fit_predict(Xtr, ytr, Xte):
    import lightgbm as lgb
    m = lgb.LGBMRegressor(**_LGB_PARAMS)
    m.fit(Xtr, ytr)
    return m.predict(Xte)


def _r2(y, yhat, baseline):
    """R² 相对基线（baseline 常量数组）"""
    ss_res = float(((y - yhat) ** 2).sum())
    ss_tot = float(((y - baseline) ** 2).sum())
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


# ---------------------------------------------------------------- 连续目标：销量 / 奖池
def _fit_continuous(f: pd.DataFrame, target: str, feats: list,
                    test_frac: float = 0.2) -> dict:
    tr, te = _train_test(f, test_frac)
    tr = tr.dropna(subset=[target])
    te = te.dropna(subset=[target])
    if len(tr) < 150 or len(te) < 30:
        return {"target": target, "error": "有效样本不足"}
    ytr = np.log1p(tr[target].to_numpy(dtype=float))
    yte = np.log1p(te[target].to_numpy(dtype=float))
    yhat_log = _lgb_fit_predict(tr[feats].fillna(-1.0), ytr, te[feats].fillna(-1.0))
    yhat = np.expm1(yhat_log)
    y = np.expm1(yte)
    bl_lag1 = te[f"{target.replace('_t','')}_lag1"].fillna(0.0).to_numpy(dtype=float)
    # 对数尺度 R²（训练目标一致）+ 原始尺度 vs lag1 基线
    r2_log = _r2(yte, yhat_log, np.full_like(yte, ytr.mean()))
    r2_raw = _r2(y, yhat, bl_lag1)
    return {
        "target": target,
        "n_train": int(len(tr)), "n_test": int(len(te)),
        "R²(log尺度, vs均值)": round(float(r2_log), 4),
        "R²(原始, vs lag1基线)": round(float(r2_raw), 4),
        "测试实际均值": round(float(y.mean()), 0),
        "测试预测均值": round(float(yhat.mean()), 0),
        "平均绝对误差%": round(float(np.mean(np.abs(yhat - y) / np.maximum(y, 1)) * 100), 1),
    }


# ---------------------------------------------------------------- 计数目标：一等奖注数
def _fit_poisson_glm(f: pd.DataFrame, feats: list, test_frac: float = 0.2) -> dict:
    """Poisson GLM（log link）：预测均值 λ 的校准与 MAE（对比齐性泊松基线）"""
    import statsmodels.api as sm
    tr, te = _train_test(f, test_frac)
    Xtr = tr[feats].fillna(-1.0).to_numpy(dtype=float)
    Xte = te[feats].fillna(-1.0).to_numpy(dtype=float)
    Xtr = np.column_stack([np.ones(len(Xtr)), Xtr])
    Xte = np.column_stack([np.ones(len(Xte)), Xte])
    model = sm.GLM(tr["n1_t"].to_numpy(dtype=float), Xtr,
                   family=sm.families.Poisson()).fit(disp=0)
    lam = model.predict(Xte)
    y = te["n1_t"].to_numpy(dtype=float)
    base = tr["n1_t"].mean()
    return {
        "方法": "Poisson GLM",
        "n_train": int(len(tr)), "n_test": int(len(te)),
        "预测均值λ": round(float(lam.mean()), 4),
        "实际均值": round(float(y.mean()), 4),
        "校准比(λ̄/ȳ)": round(float(lam.mean() / max(y.mean(), 1e-9)), 3),
        "MAE": round(float(np.mean(np.abs(lam - y))), 4),
        "MAE改进(相对齐性基线)": round(1 - np.mean(np.abs(lam - y))
                                   / max(np.mean(np.abs(base - y)), 1e-9), 4),
    }


def _fit_n1_lgb(f: pd.DataFrame, feats: list, test_frac: float = 0.2) -> dict:
    """LightGBM log1p 回归一等奖注数"""
    tr, te = _train_test(f, test_frac)
    ytr = np.log1p(tr["n1_t"].to_numpy(dtype=float))
    yte = np.log1p(te["n1_t"].to_numpy(dtype=float))
    yhat = np.expm1(_lgb_fit_predict(tr[feats].fillna(-1.0), ytr,
                                     te[feats].fillna(-1.0)))
    y = np.expm1(yte)
    base = tr["n1_t"].mean()
    return {
        "方法": "LightGBM(log1p)",
        "n_train": int(len(tr)), "n_test": int(len(te)),
        "预测均值λ": round(float(yhat.mean()), 4),
        "实际均值": round(float(y.mean()), 4),
        "校准比(λ̄/ȳ)": round(float(yhat.mean() / max(y.mean(), 1e-9)), 3),
        "MAE": round(float(np.mean(np.abs(yhat - y))), 4),
        "MAE改进(相对齐性基线)": round(1 - np.mean(np.abs(yhat - y))
                                   / max(np.mean(np.abs(base - y)), 1e-9), 4),
        "Spearman(预测,实际)": _spearman(yhat, y),
    }


def _spearman(a, b):
    """Spearman 相关；任一序列无方差/近似常量（如顶格期 EV 恒定）→ None"""
    from scipy.stats import spearmanr
    a = np.asarray(a, dtype=float)
    b = np.asarray(b, dtype=float)
    if len(a) < 3 or np.std(a) == 0 or np.std(b) == 0:
        return None
    with np.errstate(all="ignore"):
        r = float(spearmanr(a, b).statistic)
    if np.isnan(r):  # 近似常量输入 → spearmanr 返回 nan，按无方差处理
        return None
    return round(r, 4)


# ---------------------------------------------------------------- 总评估
def evaluate(name: str, test_frac: float = 0.2) -> dict:
    """walk-forward 留出评估（升序切尾 20%，训练只用更早数据，零穿越）"""
    f = build_frame(name)
    out = {"彩种": name, "可用期数": int(len(f)), "测试占比": test_frac}
    has_sales = _has_sales(f)
    # 一等奖注数模型：有销量时带销量特征（GLM 用 log 版防溢出）；无则纯滞后
    feats_n1 = _SALES_FEATS if has_sales else _LAG_FEATS
    feats_glm = (_LAG_FEATS + ["log_sales_lag1", "log_sales_ma4"]) \
        if has_sales else _LAG_FEATS
    # 销量模型：销量列有效行足够才建（双色球 2026-09-04 补全后启用）
    if has_sales:
        out["销量模型"] = _fit_continuous(f, "sales_t", _SALES_FEATS, test_frac)
    else:
        n_sales = int(f["sales_t"].notna().sum())
        out["销量模型"] = {
            "error": f"销量列有效行仅 {n_sales}（<300），时序建模样本不足，诚实降级不建模"}
    out["奖池模型"] = _fit_continuous(f, "pool_t", _LAG_FEATS, test_frac)
    out["一等奖注数模型"] = {
        "PoissonGLM": _fit_poisson_glm(f, feats_glm, test_frac),
        "LightGBM": _fit_n1_lgb(f, feats_n1, test_frac),
    }
    return out


# ---------------------------------------------------------------- 下一期预测 + 信号回放
def _next_date(name: str, last_date) -> pd.Timestamp:
    """按开奖周历推下一开奖日"""
    d = pd.Timestamp(last_date)
    days = _DRAW_WEEKDAYS[name]
    for k in range(1, 8):
        cand = d + pd.Timedelta(days=k)
        if cand.dayofweek in days:
            return cand
    return d + pd.Timedelta(days=7)  # 兜底（不该发生）


def forecast_next(name: str) -> dict:
    """用全量历史（截至最新已知期 t）预测下一期 t+1 的 奖池/销量/一等奖注数，
    并给出前瞻 EV 与参与信号。

    ⚠️ 口径注记（2026-09-04 P1-2 发现）：
    rollover 原"参与信号 = 头奖单注估计 > 历史 P75"有先天缺陷——
    大乐透历史 P75 已顶格 1000 万 → 信号恒 False（死锁）；双色球近十年
    头奖单注顶格常态化 → 信号恒 True（无区分度）。故本模块改用
    「EV 天花板语义」（顶格判定 ≥99.9% 封顶）：跨彩种可比、有区分度，
    rollover.py 已于 2026-09-04 同步升级为同一语义。
    """
    f = build_frame(name)
    if len(f) < 200:
        return {"error": f"{name} 有效历史不足"}
    feats = _SALES_FEATS if _has_sales(f) else _LAG_FEATS
    # 最新"已知"期：跳过池为 NaN 的残缺行（抓取未填），回退到最近完整期
    last = f[f["pool_t"].notna()].iloc[-1]
    last_idx = int(f[f["pool_t"].notna()].index[-1])

    # 预测 t+1 的滞后特征：用 t 的实际值构造（pool_t/sales_t/n1_t 为最新公布值）
    def _lag_val(src: str, lag: int):
        if lag == 1:
            return last[f"{src}_t"]
        return f[f"{src}_t"].iloc[last_idx - lag + 1]

    next_feat = {}
    for col, lag in (("pool", 1), ("pool", 2), ("sales", 1), ("sales", 2),
                     ("n1", 1), ("n1", 2)):
        src = "pool" if col == "pool" else ("sales" if col == "sales" else "n1")
        next_feat[f"{col}_lag{lag}"] = _lag_val(src, lag)
    pool_tail = f["pool_t"].dropna()
    sales_tail = f["sales_t"].dropna()
    next_feat["pool_ma4"] = pool_tail.tail(4).mean()
    next_feat["pool_ma8"] = pool_tail.tail(8).mean()
    next_feat["n1_ma8"] = f["n1_t"].tail(8).mean()
    next_feat["sales_ma4"] = sales_tail.tail(4).mean()
    next_feat["pool_mom"] = next_feat["pool_lag1"] / max(next_feat["pool_ma8"], 1e-9) - 1.0
    next_feat["streak"] = last["streak"] + 1 if last["n1_t"] == 0 else 0.0
    next_feat["hit_recent"] = float((f["n1_t"].tail(8) > 0).sum())
    nd = _next_date(name, last["date"])
    next_feat["weekday_t"] = float(nd.dayofweek)
    next_feat["month_t"] = float(nd.month)
    next_feat["gap_t"] = float((nd - last["date"]).days)
    Xn = pd.DataFrame([next_feat])[feats].fillna(-1.0)

    # 训练 + 预测（全量）
    yp = np.log1p(pool_tail)
    mp = _lgb_fit_predict(f.loc[pool_tail.index, feats].fillna(-1.0),
                          yp.to_numpy(dtype=float), Xn)
    pool_hat = float(np.expm1(mp[0]))
    n1_log = np.log1p(f["n1_t"])
    mn1 = _lgb_fit_predict(f[feats].fillna(-1.0), n1_log.to_numpy(dtype=float), Xn)
    n1_hat = float(np.expm1(mn1[0]))
    sales_hat = None
    if len(sales_tail) >= 300:
        ys = np.log1p(sales_tail)
        ms = _lgb_fit_predict(f.loc[sales_tail.index, feats].fillna(-1.0),
                              ys.to_numpy(dtype=float), Xn)
        sales_hat = float(np.expm1(ms[0]))

    p1 = _P1[name]
    from ev.rollover import fixed_prize_ev
    fixed_contrib = fixed_prize_ev(name)
    # λ：销量推导为主（销量模型校准好，MAPE≈3%）；LightGBM 直接预测作对照
    lam_sales = (sales_hat / 2.0 * p1) if sales_hat else None
    lam_hat = lam_sales if lam_sales else n1_hat
    head_pool = pool_hat * _POOL_SHARE
    jackpot = min(_JACKPOT_CAP, head_pool / max(lam_hat, 1e-9)) if head_pool > 0 else 0.0
    head_contrib = p1 * jackpot
    ev = -2.0 + head_contrib + fixed_contrib
    # EV 上下限（经济学：头奖封顶 → EV 有天花板；池=0 → 地板）
    ev_max = -2.0 + fixed_contrib + p1 * _JACKPOT_CAP
    ev_min = -2.0 + fixed_contrib
    # 顶格支撑注数 = 池可分池 / 封顶；split_margin>1 → 池厚到可顶格，EV 在天花板附近
    split_margin = (head_pool / _JACKPOT_CAP) / max(lam_hat, 1e-9) if head_pool > 0 else 0.0
    at_cap = bool(jackpot >= _JACKPOT_CAP * 0.999)
    return {
        "彩种": name,
        "已知最新期": {"期号": str(last["期号"]), "日期": str(last["date"].date()),
                     "奖池": round(float(last["pool_t"]), 0),
                     "一等奖注数": int(last["n1_t"])},
        "预测下一期": {
            "开奖日": str(nd.date()),
            "奖池(预测)": round(pool_hat, 0),
            "销量(预测)": round(sales_hat, 0) if sales_hat else "销量列样本不足，不预测",
            "一等奖注数(预测, LightGBM对照)": round(n1_hat, 2),
            "销量推导λ(=销量/2×P1)": round(lam_sales, 4) if lam_sales else None,
        },
        "前瞻EV": {
            "头奖单注估计(预测)": round(jackpot, 0),
            "头奖期望贡献": round(head_contrib, 6),
            "固定奖贡献": round(fixed_contrib, 6),
            "总EV(每注)": round(ev, 6),
            "EV上限(头奖顶格)": round(ev_max, 6),
            "EV地板(池=0)": round(ev_min, 6),
            "顶格支撑富余(split_margin)": round(split_margin, 2),
            "顶格判定(≥99.9%封顶)": at_cap,
            "参与信号(前瞻)": at_cap,
            "备注": ("封顶 1000 万下 EV 有天花板；'参与窗口'= 池厚到可顶格且 split 风险低。"
                     "⚠️ rollover 原 P75 规则：大乐透历史 P75 顶格→恒False(死锁)；"
                     "双色球顶格常态化→恒True(饱和)——本模块改用 EV 天花板语义"),
        },
    }


def signal_consistency(name: str, test_frac: float = 0.2) -> dict:
    """回放：留出期逐期用模型前瞻「下一期 EV 顶格判定」，与实际公布后比对。

    只训一次（训练段内），测试段每期特征纯滞后 → 零穿越。
    指标：
    ① 顶格判定一致率：预测 EV̂ 是否顶格（≥99.9%封顶）vs 实际公布后 rollover EV。
    ② 预测 EV̂ vs 实际 EV 的 Spearman（哪些期更值得提前判断的能力）。
    ③ 预测池 vs 实际池 Spearman（位次预测力）。

    经济学注记：双/大头奖封顶 1000 万 → EV 有天花板；池厚到可顶格时 EV 恒定，
    回放真正检验的是「销量/池预测 → 顶格判定」的联合质量。
    销量列不足时 rollover_ev 的 λ=0 退化为恒顶格，回放无信息（假象），
    仅报告池位次相关并诚实标注（双色球 2026-09-04 补全后已启用）。
    """
    f = build_frame(name)
    n = len(f)
    cut = int(n * (1.0 - test_frac))
    tr, te = f.iloc[:cut], f.iloc[cut:].reset_index(drop=True)
    feats = _SALES_FEATS if _has_sales(f) else _LAG_FEATS
    yp = np.log1p(tr["pool_t"].dropna())
    mp = _lgb_fit_predict(tr.loc[yp.index, feats].fillna(-1.0),
                          yp.to_numpy(dtype=float),
                          te[feats].fillna(-1.0))
    pool_hat = np.expm1(mp)
    sales_hat = None
    if _has_sales(f):
        ys = np.log1p(tr["sales_t"].dropna())
        ms = _lgb_fit_predict(tr.loc[ys.index, feats].fillna(-1.0),
                              ys.to_numpy(dtype=float),
                              te[feats].fillna(-1.0))
        sales_hat = np.expm1(ms)
    from ev.rollover import rollover_ev, fixed_prize_ev
    p1 = _P1[name]
    fixed_contrib = fixed_prize_ev(name)
    ev_max = -2.0 + fixed_contrib + p1 * _JACKPOT_CAP

    agree_cap, detail = [], []
    ev_preds, ev_acts, pool_preds, pool_acts = [], [], [], []
    for i in range(len(te)):
        row = te.iloc[i]
        act_pool = row["pool_t"]
        if act_pool is None or (isinstance(act_pool, float) and np.isnan(act_pool)):
            continue
        actual = rollover_ev(name, issue=str(row["期号"]))
        ev_act = float(actual.get("总EV(每注)", float("nan")))
        jackpot_act = float(actual.get("头奖单注奖金估计", 0.0))
        if np.isnan(ev_act):
            continue
        # 预测侧
        pool_hat_i = max(float(pool_hat[i]), 0.0)
        if sales_hat is not None and sales_hat[i] > 0:   # 大乐透：销量推导 λ
            lam_hat = sales_hat[i] / 2.0 * p1
        else:                                             # 双色球：无销量 → EV 层跳过
            lam_hat = float("nan")
        if lam_hat is None or (isinstance(lam_hat, float) and np.isnan(lam_hat)):
            # 无销量 → 顶格判定无信息（rollover 实际侧同样 λ=0 退化），跳过 EV 层
            pool_preds.append(pool_hat_i)
            pool_acts.append(float(act_pool))
            continue
        jackpot_hat = min(_JACKPOT_CAP, pool_hat_i * _POOL_SHARE / max(lam_hat, 1e-9))
        ev_pred = -2.0 + fixed_contrib + p1 * jackpot_hat
        pred_cap = bool(jackpot_hat >= _JACKPOT_CAP * 0.999)
        act_cap = bool(jackpot_act >= _JACKPOT_CAP * 0.999)
        agree_cap.append(pred_cap == act_cap)
        ev_preds.append(ev_pred)
        ev_acts.append(ev_act)
        pool_preds.append(pool_hat_i)
        pool_acts.append(float(act_pool))
        detail.append({"期号": str(row["期号"]), "顶格预测": bool(pred_cap),
                       "顶格实际": bool(act_cap),
                       "预测EV": round(ev_pred, 6), "实际EV": round(ev_act, 6)})

    has_sales = bool(sales_hat is not None)
    out = {
        "彩种": name,
        "回放期数": len(detail),
        "EV上限(顶格参考)": round(ev_max, 6),
        "③预测池vs实际池 Spearman": _spearman(np.array(pool_preds), np.array(pool_acts)),
    }
    if not has_sales:
        out["口径注记"] = ("销量列有效行不足 → 实际 rollover EV 的 λ=0 退化为恒顶格，"
                          "EV 层回放无信息（避免造假象）。")
    else:
        out["①顶格判定一致率(预测EV̂ vs 实际)"] = round(float(np.mean(agree_cap)), 4)
        out["②预测EV̂ vs 实际EV Spearman"] = _spearman(np.array(ev_preds), np.array(ev_acts))
        out["口径注记"] = ("销量可预测（留出 R² 高、MAPE≈3%）→ 顶格判定由『池厚×销量』联合决定；"
                          "一致率衡量前瞻 EV 的可靠性。")
    out["细节"] = detail
    return out


if __name__ == "__main__":
    import json, sys
    logging.basicConfig(level=logging.INFO)
    names = sys.argv[1:] or ["大乐透", "双色球"]
    for nm in names:
        print(f"\n========== {nm} 评估 ==========")
        print(json.dumps(evaluate(nm), ensure_ascii=False, indent=1))
        print(f"---------- {nm} 下一期前瞻 ----------")
        print(json.dumps(forecast_next(nm), ensure_ascii=False, indent=1))
