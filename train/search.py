"""
参数自动搜索模块（第三档升级 - 用统一评分尺挑最优）

在历史上做"走前验证"（walk-forward）：对每个 (窗口大小, 模型类型, 特征版本) 组合，
用校准评估的统一综合分（命中率 + 概率校准度）打分，自动挑最优。

- 机器学习模型（逻辑回归/随机森林/LightGBM）：逐号码概率，直接算校准
- 统计模型：以"窗口内归一化频率"作为概率代理，同样可校准
- 可复现：固定随机种子
- 两种搜索入口：
  * search_best_config：网格搜索（穷举离散组合），结果写 training/search_best_config_<彩种>_<ver>.json
  * search_best_config_optuna：Optuna 贝叶斯搜索（额外覆盖模型条件超参），
    并对全部试次的「选号 vs 随机」p 值做 BH-FDR 校正 → 输出 q 值与 verdict，
    防止"从一堆配置里挑最好"的赢家诅咒（winner's curse）。结果写
    training/search_best_config_<彩种>_optuna.json
- 两种搜索共用 _evaluate_config 作为唯一评分入口，口径强制一致。
"""

import functools
import json
import logging
import random
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

from config import LOTTERY_CONFIG, TRAINING_DIR
from data.features import (
    build_features,
    FEATURE_VERSION_STANDARD,
    FEATURE_VERSION_RICH,
)
from data.loader import load_lottery
from train.calibration import evaluate_prob_matrix

logger = logging.getLogger(__name__)


def _best_config_path(lottery_name: str, feature_version: int) -> Path:
    """按 彩种 + 特征版本 分开存最优配置，避免不同彩种/特征互相覆盖。"""
    ver = "rich" if feature_version == FEATURE_VERSION_RICH else "standard"
    safe = "".join(ch for ch in lottery_name if ch.isalnum())
    return TRAINING_DIR / f"search_best_config_{safe}_{ver}.json"


def _optuna_path(lottery_name: str) -> Path:
    """Optuna 搜索结果（含 q 值 + verdict）单独落一个文件，不覆盖网格结果。"""
    safe = "".join(ch for ch in lottery_name if ch.isalnum())
    return TRAINING_DIR / f"search_best_config_{safe}_optuna.json"


def _merge_selection_stats(lottery_name: str, res: dict, P: np.ndarray, T: np.ndarray) -> dict:
    """把「选号命中 vs 随机基线」的显著性检验并入评估结果。

    res 来自 evaluate_prob_matrix（仅号码级概率校准指标，看不出"能不能选号赚钱"）。
    这里额外用 train.metrics 的 top-k 选号命中率做一次 vs 随机基线的 z 检验，
    得到 selection_p —— 这才是"这个配置是否真的优于闭眼随机"的证据。
    网格搜索与 Optuna 共用；计算失败时 selection_p=None（不阻断主评分）。
    """
    try:
        from train.metrics import (
            _selection_hits_from_prob_matrix,
            evaluate_selection_vs_random,
        )
        mean_total, _, _ = _selection_hits_from_prob_matrix(lottery_name, P, T)
        n_periods = int(P.shape[0])
        sel = evaluate_selection_vs_random(lottery_name, float(mean_total), n_periods)
        res["selection_expected"] = sel["expected_total_hits"]
        res["selection_observed"] = sel["observed_total_hits"]
        res["selection_advantage"] = sel["total_advantage"]
        res["selection_p"] = sel["p_value"]
        res["selection_note"] = sel["significance_note"]
    except Exception as e:
        logger.warning(f"选号显著性计算失败({lottery_name}): {e}")
        res["selection_p"] = None
        res["selection_note"] = "选号显著性不可用"
    return res


def _bh_fdr(p_values: List[float]) -> List[float]:
    """Benjamini-Hochberg FDR 校正，返回与输入同序的 q 值列表。

    多重比较校正：当从 W 个候选配置里挑"最优"时，单看它的 p 值必然偏乐观
    （赢家诅咒）。BH 把所有候选的 p 一起校正：q_(i) = min_{k>=i} (m/k · p_(k))，
    倒序扫保证单调，cap 到 1。非有限值调用前应先剔除。
    """
    m = len(p_values)
    if m == 0:
        return []
    p = np.asarray(p_values, dtype=float)
    order = np.argsort(p)
    ps = p[order]
    qs = ps * m / np.arange(1, m + 1)
    for i in range(m - 2, -1, -1):  # 倒序保序（BH 阶梯）
        qs[i] = min(qs[i], qs[i + 1])
    qs = np.minimum(qs, 1.0)
    out = np.empty(m)
    out[order] = qs
    return out.tolist()


def _evaluate_config(
    data,
    window: int,
    model_type: str,
    feature_version: int,
    hp: Optional[dict] = None,
    n_splits: int = 5,
) -> Optional[dict]:
    """评估单个 (窗口, 模型, 特征版本[, 模型超参]) 配置的统一入口。

    网格搜索（search_best_config）与 Optuna（search_best_config_optuna）共用，
    保证两种搜索的评分口径完全一致。

    返回 dict：hit_rate / calibration_error / combined_score / n_samples +
    selection_*（选号 vs 随机显著性）；数据不足或评估异常返回 None（由调用方处理）。
    """
    try:
        if model_type == "statistical":
            res = _walk_forward_statistical(data, window, feature_version, n_splits=n_splits)
        else:
            res = _walk_forward_ml(data, window, model_type, feature_version,
                                   n_splits=n_splits, hp=hp)
    except Exception as e:
        logger.warning(f"配置评估失败 window={window} model={model_type} hp={hp}: {e}")
        return None
    return res


def _walk_forward_ml(
    data,
    window_size: int,
    model_type: str,
    feature_version: int,
    n_splits: int = 5,
    hp: Optional[dict] = None,
) -> Optional[dict]:
    """机器学习模型的走前验证，返回统一评估指标。

    ⚠️ 折划分统一走 train.engine.walk_forward_folds（2026-08-31 修复版）：
    训练集只取「比验证折更早」的样本（X 最新在前，索引更大 = 更早），绝不掺入
    X[:val_start]（更新/更晚的数据）。早期版本在此处手写 fold 循环且把更新数据
    并入训练集 → look-ahead bias（用未来预测过去），且 HPO 会稳定选出泄漏最重的
    配置。本函数不再维护第二套 fold 逻辑，与 engine._run_ml_window 口径强制一致。

    hp: 模型条件超参（Optuna 建议），None = 引擎保守默认值。
    """
    from sklearn.preprocessing import StandardScaler

    from train.engine import (
        _train_logistic,
        _train_random_forest,
        _train_lightgbm,
        walk_forward_folds,
    )

    cfg = LOTTERY_CONFIG[data.lottery_name]
    X, y = build_features(data.records, window_size, data.lottery_name, feature_version)
    if X.size == 0:
        return None

    _trainers = {
        "logistic": _train_logistic,
        "random_forest": _train_random_forest,
        "lightgbm": _train_lightgbm,
    }
    trainer = _trainers.get(model_type, _train_random_forest)
    if hp:
        trainer = functools.partial(trainer, hp=hp)

    all_probs = []
    all_true = []
    for train_idx, val_idx in walk_forward_folds(len(X), n_splits):
        Xtr, ytr = X[train_idx], y[train_idx]
        Xv, yv = X[val_idx], y[val_idx]

        scaler = StandardScaler()
        Xtr_s = scaler.fit_transform(Xtr)
        Xv_s = scaler.transform(Xv)

        models = trainer(Xtr_s, ytr)

        prob_rows = []
        for m in models:
            if m is not None:
                prob_rows.append(m.predict_proba(Xv_s)[:, 1])
            else:
                prob_rows.append(np.full(len(Xv), 0.5))
        prob_matrix = np.array(prob_rows).T  # (n_val, n_numbers)
        all_probs.append(prob_matrix)
        all_true.append(yv)

    if not all_probs:
        return None
    P = np.vstack(all_probs)
    T = np.vstack(all_true)
    res = evaluate_prob_matrix(T, P)
    return _merge_selection_stats(data.lottery_name, res, P, T)


def _walk_forward_statistical(
    data, window_size: int, feature_version: int, n_splits: int = 5
) -> Optional[dict]:
    """统计模型的走前验证（窗口频率作概率代理，同样可校准）。

    统计模型没有"训练"步骤，无需折划分 —— 直接对全部 n_samples 个滚动样本评估
    （每个样本：用目标期之前的 window_size 期频率作概率，预测目标期号码）。

    ⚠️ 时间方向（2026-08-31 修正）：records 最新在前。旧实现
    `window = records[i:i+ws]` + `target = records[i+ws]` 是用**更晚**的开奖预测
    **更早**的一期（逆向泄漏）；且当时只评估「最后一折」导致样本量只有 1/5。
    现在 window 取目标期之前（索引更大）的 ws 期，且评估全部样本（与
    engine._train_single_window 的 statistical 全量滚动口径一致）。
    """
    cfg = LOTTERY_CONFIG[data.lottery_name]
    records = data.records
    n_samples = len(records) - window_size
    if n_samples <= 0:
        return None

    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]

    all_probs = []
    all_true = []
    for i in range(n_samples):
        # 目标 = records[i]（较新的一期）；窗口 = 其之前的 window_size 期（更旧，索引更大）
        window = records[i + 1:i + 1 + window_size]
        target = records[i]
        red_freq = {n: 0 for n in range(r_min, r_max + 1)}
        blue_freq = {n: 0 for n in range(b_min, b_max + 1)}
        for rec in window:
            for n in rec.红球:
                red_freq[n] += 1
            for n in rec.蓝球:
                blue_freq[n] += 1
        ws = max(window_size, 1)
        prob_row = [red_freq[n] / ws for n in range(r_min, r_max + 1)]
        prob_row += [blue_freq[n] / ws for n in range(b_min, b_max + 1)]
        true_row = [1 if n in target.红球 else 0 for n in range(r_min, r_max + 1)]
        true_row += [1 if n in target.蓝球 else 0 for n in range(b_min, b_max + 1)]
        all_probs.append(prob_row)
        all_true.append(true_row)

    if not all_probs:
        return None
    P = np.array(all_probs)
    T = np.array(all_true)
    res = evaluate_prob_matrix(T, P)
    return _merge_selection_stats(data.lottery_name, res, P, T)


def search_best_config(
    lottery_name: str,
    windows: Optional[List[int]] = None,
    model_types: Optional[List[str]] = None,
    feature_version: int = FEATURE_VERSION_STANDARD,
    calib_weight: float = 0.3,
    seed: int = 42,
) -> dict:
    """
    搜索最优 (窗口, 模型, 特征版本) 组合。
    返回：
    {
        "best": {window, model_type, feature_version, score, hit_rate, calibration_error},
        "results": [ {window, model_type, feature_version, hit_rate, calibration_error, combined_score}, ... ],
    }
    """
    np.random.seed(seed)
    random.seed(seed)

    data = load_lottery(lottery_name)
    if windows is None:
        windows = [30, 50, 100]
    if model_types is None:
        model_types = ["logistic", "random_forest", "lightgbm", "statistical"]

    results = []
    for w in windows:
        for mt in model_types:
            res = _evaluate_config(data, w, mt, feature_version)
            if res is None:
                results.append({
                    "window": w, "model_type": mt,
                    "feature_version": feature_version,
                    "error": "数据不足或评估失败",
                })
                continue
            row = {
                "window": w,
                "model_type": mt,
                "feature_version": feature_version,
                "hit_rate": res["hit_rate"],
                "calibration_error": res["calibration_error"],
                "combined_score": res["combined_score"],
                "n_samples": res["n_samples"],
            }
            # 并入选号 vs 随机显著性的证据字段（供人工/后续裁判层查阅）
            for sk in ("selection_expected", "selection_observed", "selection_advantage",
                       "selection_p", "selection_note"):
                if sk in res:
                    row[sk] = res[sk]
            results.append(row)
            logger.info(
                f"搜索 window={w} model={mt} ver={feature_version} -> "
                f"命中率={res['hit_rate']:.4f} 校准误差={res['calibration_error']:.4f} "
                f"综合分={res['combined_score']:.4f} "
                f"选号p={res.get('selection_p')}"
            )

    valid = [r for r in results if "combined_score" in r]
    if not valid:
        return {"best": None, "results": results}

    best = max(valid, key=lambda r: r["combined_score"])
    out = {
        "lottery_name": lottery_name,
        "calib_weight": calib_weight,
        "feature_version": feature_version,
        "best": best,
        "results": results,
    }
    # 写出最优配置，供人工/后续自动选用（按彩种+特征版本分文件）
    try:
        path = _best_config_path(lottery_name, feature_version)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        logger.info(f"最优配置已写出: {path}")
    except Exception as e:
        logger.warning(f"写出最优配置失败: {e}")

    return out


def search_best_config_optuna(
    lottery_name: str,
    n_trials: int = 50,
    seed: int = 42,
    calib_weight: float = 0.3,
) -> dict:
    """Optuna 诚实化搜索（P0-2）：在走前验证无泄漏的口径下贝叶斯搜索超参。

    与网格 search_best_config 的区别与诚实性设计：
    1. 搜索空间更大：window ∈ {30,50,100,150}、feature_version ∈ {standard,rich}、
       model_type ∈ {logistic,random_forest,lightgbm,statistical}，且对 ML 模型给
       条件超参（logistic→C；rf→max_depth/n_estimators；lightgbm→num_leaves/
       learning_rate/n_estimators）。
    2. 每个试次都走 _evaluate_config（与网格共用同一评分入口，且全部在
       engine.walk_forward_folds 无泄漏折上评估），同时记录「选号 vs 随机」p 值。
    3. 赢家诅咒防护（BH-FDR）：从 n_trials 个配置里挑"最优"本身就是多重比较，
       单看最优试次的 p 值必然偏乐观。故把**全部完成试次**的 selection_p 做
       Benjamini-Hochberg 校正 → 最优试次得 q_best；verdict 规则：
         q_best < 0.05 且 advantage > 0 → "显著优于随机"（可宣称）
         否则                          → "无显著优势"（不许宣称，哪怕 score 最高）
    4. 结果写 training/search_best_config_<彩种>_optuna.json（不覆盖网格文件）。

    需要 pip install optuna（lazy import，未安装时返回 error dict 而非崩溃）。
    """
    try:
        import optuna
    except ImportError:
        msg = "未安装 optuna，请先执行: E:/Python/python.exe -m pip install optuna"
        logger.error(msg)
        return {"lottery_name": lottery_name, "error": msg}

    import datetime

    optuna.logging.set_verbosity(optuna.logging.WARNING)
    np.random.seed(seed)
    random.seed(seed)

    data = load_lottery(lottery_name)

    _WINDOWS = [30, 50, 100, 150]
    _MODELS = ["logistic", "random_forest", "lightgbm", "statistical"]
    _VERSIONS = [FEATURE_VERSION_STANDARD, FEATURE_VERSION_RICH]
    _VER_LABEL = {
        FEATURE_VERSION_STANDARD: "standard",
        FEATURE_VERSION_RICH: "rich",
    }

    def _objective(trial):
        w = trial.suggest_categorical("window", _WINDOWS)
        mt = trial.suggest_categorical("model_type", _MODELS)
        fv = trial.suggest_categorical("feature_version", _VERSIONS)
        hp = None
        if mt == "logistic":
            hp = {"C": trial.suggest_float("logistic_C", 1e-3, 1e3, log=True)}
        elif mt == "random_forest":
            hp = {
                "max_depth": trial.suggest_int("rf_max_depth", 3, 20),
                "n_estimators": trial.suggest_int("rf_n_estimators", 50, 300, step=50),
            }
        elif mt == "lightgbm":
            hp = {
                "num_leaves": trial.suggest_int("lgb_num_leaves", 8, 96, step=8),
                "learning_rate": trial.suggest_float("lgb_learning_rate", 0.01, 0.3, log=True),
                "n_estimators": trial.suggest_int("lgb_n_estimators", 50, 400, step=50),
            }

        res = _evaluate_config(data, w, mt, fv, hp=hp)
        if res is None:
            # 数据不足/该彩种不受支持：不产生分数，交给 Optuna 标记为失败试次
            raise optuna.exceptions.TrialPruned("评估失败或数据不足")

        trial.set_user_attr("window", int(w))
        trial.set_user_attr("feature_version", _VER_LABEL[fv])
        trial.set_user_attr("model_type", mt)
        if hp:
            trial.set_user_attr("hp", hp)
        trial.set_user_attr("hit_rate", float(res["hit_rate"]))
        trial.set_user_attr("calibration_error", float(res["calibration_error"]))
        trial.set_user_attr("n_samples", int(res["n_samples"]))
        trial.set_user_attr("selection_advantage", res.get("selection_advantage"))
        trial.set_user_attr("selection_p", res.get("selection_p"))
        return float(res["combined_score"])

    study = optuna.create_study(
        direction="maximize",
        sampler=optuna.samplers.TPESampler(seed=seed),
    )
    study.optimize(_objective, n_trials=n_trials)

    completed = [t for t in study.trials if t.state == optuna.trial.TrialState.COMPLETE]
    n_completed = len(completed)
    n_failed = len(study.trials) - n_completed

    if n_completed == 0:
        out = {
            "lottery_name": lottery_name,
            "method": "optuna",
            "n_trials_requested": n_trials,
            "n_trials_completed": 0,
            "n_trials_failed": n_failed,
            "best": None,
            "verdict": "无完成试次（数据不足或该彩种不受搜索支持）",
            "trials": [],
            "note": "Optuna 搜索需要该彩种至少能产出一次有效评估（当前全部 TrialPruned）。",
        }
        return out

    # ---- 赢家诅咒防护：对全部完成试次的 selection_p 做 BH-FDR ----
    p_map = {}  # trial.number -> 原始 selection_p（仅有限值参与校正）
    for t in completed:
        p = t.user_attrs.get("selection_p")
        if isinstance(p, (int, float)) and np.isfinite(p):
            p_map[t.number] = float(p)
    q_map = {}  # trial.number -> q 值
    if p_map:
        numbers = list(p_map.keys())
        qs = _bh_fdr([p_map[n] for n in numbers])
        q_map = dict(zip(numbers, qs))

    best_trial = study.best_trial  # 完成试次中 combined_score 最大
    b_num = best_trial.number
    best_adv = best_trial.user_attrs.get("selection_advantage")
    best_p_raw = p_map.get(b_num)
    best_q = q_map.get(b_num)

    if best_q is not None and best_q < 0.05 and best_adv is not None and best_adv > 0:
        verdict = "显著优于随机（BH-FDR 校正后 q<0.05，可宣称）"
    elif best_q is not None and best_q < 0.05 and best_adv is not None and best_adv < 0:
        verdict = "显著差于随机（BH-FDR 校正后 q<0.05）——综合分最高但选号劣于闭眼随机"
    else:
        verdict = "无显著优势（BH-FDR 校正后，最优配置的选号表现不优于随机基线）"

    def _trial_summary(t):
        return {
            "number": t.number,
            "params": {
                "window": t.user_attrs.get("window"),
                "feature_version": t.user_attrs.get("feature_version"),
                "model_type": t.user_attrs.get("model_type"),
                "hp": t.user_attrs.get("hp"),
            },
            "combined_score": float(t.value),
            "hit_rate": t.user_attrs.get("hit_rate"),
            "calibration_error": t.user_attrs.get("calibration_error"),
            "n_samples": t.user_attrs.get("n_samples"),
            "selection_advantage": t.user_attrs.get("selection_advantage"),
            "selection_p_raw": p_map.get(t.number),
            "selection_q": q_map.get(t.number),
        }

    trials_summary = sorted(
        (_trial_summary(t) for t in completed),
        key=lambda r: r["combined_score"],
        reverse=True,
    )

    out = {
        "lottery_name": lottery_name,
        "method": "optuna",
        "n_trials_requested": n_trials,
        "n_trials_completed": n_completed,
        "n_trials_failed": n_failed,
        "n_p_values_corrected": len(q_map),
        "calib_weight": calib_weight,
        "seed": seed,
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "best": {
            "number": b_num,
            "params": {
                "window": best_trial.user_attrs.get("window"),
                "feature_version": best_trial.user_attrs.get("feature_version"),
                "model_type": best_trial.user_attrs.get("model_type"),
                "hp": best_trial.user_attrs.get("hp"),
            },
            "combined_score": float(best_trial.value),
            "hit_rate": best_trial.user_attrs.get("hit_rate"),
            "calibration_error": best_trial.user_attrs.get("calibration_error"),
            "n_samples": best_trial.user_attrs.get("n_samples"),
            "selection_advantage": best_adv,
            "selection_p_raw": best_p_raw,
            "selection_q": best_q,
        },
        "verdict": verdict,
        "trials": trials_summary,
        "note": (
            "p=每配置「选号命中 vs 随机基线」单尾 z 检验；q=对全部完成试次做 BH-FDR "
            "校正后的值（多重比较/赢家诅咒防护）。所有试次都在 engine.walk_forward_folds "
            "无泄漏折上评估。⚠️ 各试次共享同一段历史数据，q 值对此相关性并非完全鲁棒，"
            "结论仅供参考：q<0.05 才允许宣称显著优于随机。"
        ),
    }

    try:
        path = _optuna_path(lottery_name)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        logger.info(f"Optuna 搜索结果已写出: {path}")
    except Exception as e:
        logger.warning(f"写出 Optuna 搜索结果失败: {e}")

    return out
