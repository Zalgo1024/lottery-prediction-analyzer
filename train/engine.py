"""
训练引擎
滚动窗口回测 + 早停机制 + 多窗口对比 + 损失函数 + ML 模型方案
"""

import json
import logging
import math
import os
import pickle
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
from sklearn.ensemble import RandomForestClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.preprocessing import StandardScaler

from config import LOTTERY_CONFIG, TRAIN_DEFAULTS, TRAINING_DIR
from data.loader import load_lottery
from data.schema import LotteryData, schema_from_cfg, record_zone_numbers, is_redblue
from data.features import (
    build_features,
    FEATURE_VERSION_STANDARD,
    FEATURE_VERSION_RICH,
)
from pipeline.statistics import frequency_analysis, missing_value_analysis, hot_cold_ranking
from train.metrics import (
    compute_selection_hits,
    evaluate_selection_vs_random,
    _selection_hits_from_prob_matrix,
)

logger = logging.getLogger(__name__)

# 走前验证中「训练集」的最小样本数。
# 修掉 look-ahead bias 后训练集只能取 X[val_end:]（更早的数据，因为 X 是最新在前），
# 最后一折的训练集会为空、靠后的折也可能过少，低于此阈值直接跳过该折。
# train/search.py 复用同一常量，保证两条 CV 路径行为一致。
_MIN_TRAIN_SAMPLES = 50


def walk_forward_folds(n_samples: int, n_splits: int = 5,
                       min_train: int = _MIN_TRAIN_SAMPLES):
    """生成走前验证的折划分，返回 (train_idx, val_idx) 索引数组。

    ⚠️ 时间方向（2026-08-31 修复）：样本矩阵 X 是**最新在前**（索引越小 = 时间越晚），
    因此「比验证折更早」的数据对应**更大的索引**。训练集一律取 [val_end, n_samples)，
    即只用它验证折更早的历史，绝不掺入 X[:val_start]（更晚的数据）。

    不变量（tests/test_no_lookahead.py 锁定）：
        train_idx.min() > val_idx.max()   —— 训练样本全部早于验证样本

    原实现为 `concatenate([X[:val_start], X[val_end:]])`，把验证折之后（更晚）的数据
    也并入训练集 → 用未来预测过去（look-ahead bias）。
    """
    if n_samples <= 0 or n_splits <= 0:
        return
    fold_size = max(n_samples // n_splits, 1)
    for k in range(n_splits):
        val_start = k * fold_size
        val_end = (k + 1) * fold_size if k < n_splits - 1 else n_samples
        if val_start >= val_end:
            continue
        train_idx = np.arange(val_end, n_samples)
        val_idx = np.arange(val_start, val_end)
        if len(train_idx) < min_train or len(val_idx) == 0:
            continue
        yield train_idx, val_idx


def _build_features(data: LotteryData, window_size: int) -> Tuple[np.ndarray, np.ndarray]:
    """
    为 ML 模型构建特征矩阵和标签（阶段0起直接复用 data.features.build_features，
    保证训练与预测端特征完全一致，消除旧版重复实现）。
    """
    return build_features(data.records, window_size, data.lottery_name, FEATURE_VERSION_STANDARD)


def _train_logistic(X_train, y_train, hp=None):
    """训练逻辑回归模型

    hp: 可选超参 dict（来自 Optuna 条件建议）。为 None 时用保守默认值，
    保证 train / 网格搜索路径行为与历史一致。
    """
    hp = hp or {}
    n_features = X_train.shape[1]
    n_outputs = y_train.shape[1]

    models = []
    for i in range(n_outputs):
        y_i = y_train[:, i]
        if len(set(y_i)) < 2:
            models.append(None)
            continue
        m = LogisticRegression(
            max_iter=1000,
            solver="lbfgs",
            class_weight="balanced",  # 处理类别不平衡
            C=hp.get("C", 1.0),
        )
        m.fit(X_train, y_i)
        models.append(m)
    return models


def _train_random_forest(X_train, y_train, hp=None):
    """训练随机森林模型

    hp: 可选超参 dict（来自 Optuna 条件建议），为 None 时用保守默认值。
    """
    hp = hp or {}
    n_features = X_train.shape[1]
    n_outputs = y_train.shape[1]

    models = []
    for i in range(n_outputs):
        y_i = y_train[:, i]
        if len(set(y_i)) < 2:
            models.append(None)
            continue
        m = RandomForestClassifier(
            n_estimators=hp.get("n_estimators", 100),
            max_depth=hp.get("max_depth", 10),
            random_state=42,
            class_weight="balanced",  # 处理类别不平衡
        )
        m.fit(X_train, y_i)
        models.append(m)
    return models


def _train_lightgbm(X_train, y_train, hp=None):
    """训练 LightGBM 模型（最强证伪基线，见《算法升级路线图》P0-3）。

    hp: 可选超参 dict（来自 Optuna 条件建议，覆盖 num_leaves / learning_rate /
    n_estimators），为 None 时用保守固定值（num_leaves=31 / lr=0.05 / 200 树）。
    LGBMClassifier 与 sklearn API 一致（fit / predict / predict_proba / pickle），
    因此保存 model.pkl、_get_trained_params 加载、_predict_with_ml 预测均天然兼容，
    预测端无需改动（红蓝彩种 trained 模式即可使用）。
    """
    from lightgbm import LGBMClassifier

    hp = hp or {}
    n_outputs = y_train.shape[1]
    models = []
    for i in range(n_outputs):
        y_i = y_train[:, i]
        if len(set(y_i)) < 2:
            models.append(None)
            continue
        m = LGBMClassifier(
            n_estimators=hp.get("n_estimators", 200),
            num_leaves=hp.get("num_leaves", 31),
            learning_rate=hp.get("learning_rate", 0.05),
            random_state=42,
            class_weight="balanced",
            verbosity=-1,
        )
        m.fit(X_train, y_i)
        models.append(m)
    return models


def _compute_loss(
    pred_numbers: List[int],
    actual_numbers: List[int],
    red_range: Tuple,
    blue_range: Tuple,
    missing_values: dict,
    alpha: float = 0.6,
    beta: float = 0.4,
) -> float:
    """
    自定义损失函数
    Loss = α × 区间偏差 + β × 遗漏值偏差
    - 区间偏差：预测号码与真实号码的最小距离之和（红蓝球独立归一化）
    - 遗漏值偏差：预测号码的遗漏值偏离理想均匀遗漏的程度
    """
    if not pred_numbers or not actual_numbers:
        return 1.0

    r_min, r_max = red_range
    b_min, b_max = blue_range
    r_span = r_max - r_min + 1
    b_span = b_max - b_min + 1

    # 分离红蓝球
    red_pred = [n for n in pred_numbers if r_min <= n <= r_max]
    blue_pred = [n for n in pred_numbers if b_min <= n <= b_max]
    red_actual = [n for n in actual_numbers if r_min <= n <= r_max]
    blue_actual = [n for n in actual_numbers if b_min <= n <= b_max]

    # 1. 区间偏差（红蓝独立归一化）
    red_interval = 0
    for pn in red_pred:
        distances = [abs(pn - an) for an in red_actual]
        red_interval += min(distances) if distances else 0
    red_interval = red_interval / max(len(red_pred), 1) / r_span

    blue_interval = 0
    for pn in blue_pred:
        distances = [abs(pn - an) for an in blue_actual]
        blue_interval += min(distances) if distances else 0
    blue_interval = blue_interval / max(len(blue_pred), 1) / b_span

    interval_loss = (red_interval + blue_interval) / 2

    # 2. 遗漏值偏差
    all_missing = list(missing_values.values()) if missing_values else []
    if not all_missing:
        return alpha * interval_loss + beta * 0.5

    ideal_missing = sum(all_missing) / len(all_missing)
    missing_dev = 0
    for n in pred_numbers:
        mv = missing_values.get(n, ideal_missing)
        dev = abs(mv - ideal_missing) / max(ideal_missing, 1)
        missing_dev += min(dev, 1.0)
    missing_dev = missing_dev / max(len(pred_numbers), 1)
    missing_loss = missing_dev  # 偏差越大，损失越大

    return alpha * interval_loss + beta * missing_loss


def train(
    lottery_name: str,
    iterations: int = None,
    window: int = None,
    window_list: List[int] = None,
    model_type: str = "statistical",
    patience: int = None,
    features: str = "standard",
    seed: int = 42,
) -> dict:
    """
    训练主入口

    参数：
    - lottery_name: 彩票类型
    - iterations: 迭代次数（早停生效时可能提前终止）
    - window: 默认滚动窗口大小
    - window_list: 多窗口对比列表
    - model_type: statistical | logistic | random_forest
    - patience: 早停容忍轮数
    """
    if iterations is None:
        iterations = TRAIN_DEFAULTS["iterations"]
    if window is None:
        window = TRAIN_DEFAULTS["window"]
    if window_list is None:
        window_list = TRAIN_DEFAULTS["window_list"]
    if patience is None:
        patience = TRAIN_DEFAULTS["patience"]

    alpha = TRAIN_DEFAULTS["loss_alpha"]
    beta = TRAIN_DEFAULTS["loss_beta"]

    # 特征版本：standard=原特征；rich=丰富特征(动量/冷热斜率/共现)
    feature_version = FEATURE_VERSION_RICH if features == "rich" else FEATURE_VERSION_STANDARD

    logger.info(f"开始训练: {lottery_name}, model={model_type}, iterations={iterations}, window_list={window_list}, features={features}")

    # 固定随机种子，保证训练可复现（ML 交叉验证 / 采样不再随运行漂移）
    np.random.seed(seed)

    data = load_lottery(lottery_name)
    cfg = LOTTERY_CONFIG[lottery_name]

    # 阶段2：训练引擎已泛化——红/蓝乐透型与数字型（排列5/福彩3D/排列3/七星彩）
    # 统一按 Schema.zones 训练；数字型走逐区统计 + 逐区标签的通用分支。

    # 数据集版本化：记录本次训练用的是本地哪一期之前的数据，便于复现与对比
    issues = [r.期号 for r in data.records if r.期号]
    data_version = {
        "max_issue": max(issues) if issues else None,
        "total_records": data.total_records,
        "训练时间": datetime.now().isoformat(),
        "seed": seed,
    }

    # 1. 多窗口对比训练
    window_results = {}
    for w in window_list:
        logger.info(f"  训练窗口 size={w}")
        wr = _train_single_window(
            data, w, iterations, patience, model_type, alpha, beta,
            feature_version=feature_version,
        )
        window_results[str(w)] = wr

    # 2. 找到最优窗口
    # 优先按「选号命中优势 over 随机」选窗口；若所有窗口均无显著优势，
    # 则回退到旧的 best_loss，避免窗口选择被无信号的 loss 误导。
    def _window_advantage(k):
        wr = window_results[k]
        if "error" in wr:
            return float("-inf")
        sev = wr.get("selection_eval")
        if sev is None:
            return float("-inf")
        return sev.get("total_advantage", float("-inf"))

    valid_windows = [k for k in window_results.keys() if "error" not in window_results[k]]
    if valid_windows:
        best_adv_window = max(valid_windows, key=_window_advantage)
        best_adv = _window_advantage(best_adv_window)
        # 只要最好窗口有非负优势，就按优势选；否则回退 loss
        if best_adv >= 1e-9:
            best_window = best_adv_window
        else:
            best_window = min(valid_windows, key=lambda k: window_results[k]["best_loss"])
    else:
        best_window = list(window_results.keys())[0]
    best_loss = window_results[best_window]["best_loss"]

    # 3. 基础统计参数（只用训练集，排除验证集，消除数据泄露）
    # 训练集 = 排除最后 max(window_list) 期的数据（验证集）
    validation_size = max(window_list) if window_list else window
    train_records = data.records[validation_size:] if len(data.records) > validation_size else data.records
    train_data = LotteryData(
        lottery_name=data.lottery_name,
        records=train_records,
    )
    freqs = frequency_analysis(train_data)
    missing = missing_value_analysis(train_data)
    ranking = hot_cold_ranking(train_data, top_n=10)

    # 基础参数：按分区名生成 <区名>频率 / <区名>遗漏（红/蓝与数字型统一）
    base_params = {}
    for zname in freqs.keys():
        base_params[f"{zname}频率"] = {str(k): v for k, v in freqs[zname]["freq"].items()}
    for zname in missing.keys():
        base_params[f"{zname}遗漏"] = {str(k): v for k, v in missing[zname].items()}

    result = {
        "lottery_name": lottery_name,
        "model_type": model_type,
        "feature_version": feature_version,
        "training_time": datetime.now().isoformat(),
        "total_records": data.total_records,
        "multi_window_results": window_results,
        "best_window": best_window,
        "best_loss": best_loss,
        "data_version": data_version,
        "calibration": window_results.get(str(best_window), {}).get("calibration"),
        "基础参数": base_params,
        "config": {
            "iterations": iterations,
            "window_list": window_list,
            "model_type": model_type,
            "patience": patience,
            "alpha": alpha,
            "beta": beta,
            "feature_version": feature_version,
            "data_version": data_version,
        },
    }

    # 保存训练记录（模型 + 报告 + 图表在同一目录）
    record_dir = save_training_record(result)
    from train.reporter import save_report
    save_report(result, record_dir)
    result["record_dir"] = str(record_dir)

    # 训练入反馈闭环：用刚训练好的模型生成一次对下一次开奖的预测，
    # 写入 pending 并标记为「训练」来源，使训练模型的真实命中可参与对比与权重统计。
    # ⚠️ 不覆盖生产预测：训练预测与生产 fresh 预测的 (目标期号, 档位) 完全相同
    # （都是「一般」档），若直接落库会把生产预测挤掉、换成一条 valid=False 的训练记录。
    # 因此先 record_pending=False 生成，检测同期是否已有生产预测，有则跳过写入。
    try:
        from prediction.engine import predict as _predict
        from data.feedback import load_pending, record_pending_prediction, _next_issue
        _pred = _predict(lottery_name, groups=5, mode="trained", record_pending=False)
        if _pred and _pred.get("预测号码"):
            target_issue = _pred.get("目标期号") or _next_issue(lottery_name)
            _pred["目标期号"] = target_issue
            pending = load_pending(lottery_name)
            has_prod = any(
                p.get("目标期号") == target_issue and p.get("来源") not in ("train", "训练")
                for p in pending
            )
            if has_prod:
                result["闭环预测"] = {
                    "目标期号": target_issue,
                    "号码组数": _pred.get("号码组数"),
                    "已写入pending": False,
                    "原因": "同期已有生产预测，训练预测不覆盖",
                }
                logger.info(f"训练闭环预测跳过写入：{lottery_name} 期{target_issue} 已有生产预测")
            else:
                _pred["来源"] = "训练"
                _pred["训练来源目录"] = str(record_dir)
                record_pending_prediction(lottery_name, _pred)
                result["闭环预测"] = {
                    "目标期号": target_issue,
                    "号码组数": _pred.get("号码组数"),
                    "已写入pending": True,
                }
    except Exception as e:
        logger.warning(f"训练结果入反馈闭环失败（不影响训练本身）: {e}")

    return result


def _train_single_window(
    data: LotteryData,
    window_size: int,
    max_iterations: int,
    patience: int,
    model_type: str,
    alpha: float,
    beta: float,
    feature_version: int = FEATURE_VERSION_STANDARD,
) -> dict:
    """单个窗口大小的训练"""
    cfg = LOTTERY_CONFIG[data.lottery_name]
    schema = schema_from_cfg(cfg)
    # 阶段0地基：区结构统一从 schema 读取（双色球/大乐透零破坏）
    # 数字型（排列5/福彩3D/排列3/七星彩）无红/蓝双区，走逐区通用分支
    if not is_redblue(data.lottery_name):
        return _train_single_window_digital(
            data, window_size, max_iterations, patience, model_type, alpha, beta, schema, feature_version
        )
    red_zone = schema.red_zone
    blue_zone = schema.blue_zone
    r_min, r_max = red_zone.range_tuple()
    b_min, b_max = blue_zone.range_tuple()
    records = data.records
    n_samples = len(records) - window_size

    if n_samples <= 0:
        return {"error": f"数据量不足，需要至少 {window_size + 1} 期"}

    # 统计模型：一次性滚动验证（消除伪迭代）
    if model_type == "statistical":
        losses = []
        hit_rates = []
        red_hit_rates = []
        blue_hit_rates = []
        selection_total_hits = []  # 选号总命中（逐期）

        for i in range(n_samples):
            window = records[i:i + window_size]
            actual = records[i + window_size]

            # 统计窗口内频率
            red_freq = {}
            blue_freq = {}
            for rec in window:
                for n in rec.红球:
                    red_freq[n] = red_freq.get(n, 0) + 1
                for n in rec.蓝球:
                    blue_freq[n] = blue_freq.get(n, 0) + 1

            # 计算窗口内的遗漏值
            red_missing = {}
            for n in range(r_min, r_max + 1):
                gap = 0
                for rec in window:
                    if n in rec.红球:
                        break
                    gap += 1
                red_missing[n] = gap
            blue_missing = {}
            for n in range(b_min, b_max + 1):
                gap = 0
                for rec in window:
                    if n in rec.蓝球:
                        break
                    gap += 1
                blue_missing[n] = gap
            # 保持旧版合并顺序（红先蓝后），避免改动 _compute_loss 的数值行为
            missing_values = {**red_missing, **blue_missing}

            # 预测：选频率最高的号码
            sorted_red = sorted(red_freq.items(), key=lambda x: x[1], reverse=True)
            sorted_blue = sorted(blue_freq.items(), key=lambda x: x[1], reverse=True)
            pred_red = [n for n, _ in sorted_red[:red_zone.choose]]
            pred_blue = [n for n, _ in sorted_blue[:blue_zone.choose]]

            loss = _compute_loss(
                pred_red + pred_blue,
                actual.红球 + actual.蓝球,
                red_zone.range_tuple(), blue_zone.range_tuple(),
                missing_values,
                alpha, beta,
            )
            losses.append(loss)

            # 命中率（红蓝独立）
            red_hits = len(set(pred_red) & set(actual.红球))
            blue_hits = len(set(pred_blue) & set(actual.蓝球))
            red_hit_rates.append(red_hits / red_zone.choose)
            blue_hit_rates.append(blue_hits / blue_zone.choose)
            total_hits = (red_hits + blue_hits) / (red_zone.choose + blue_zone.choose)
            hit_rates.append(total_hits)

            # 选号命中率（与随机基线对比的真实指标）
            sel_hits = compute_selection_hits(
                data.lottery_name,
                {red_zone.name: pred_red, blue_zone.name: pred_blue},
                actual,
            )
            selection_total_hits.append(sel_hits["total_hits"])

        best_loss = float(np.mean(losses)) if losses else 1.0
        best_hit_rate = float(np.mean(hit_rates)) if hit_rates else 0.0
        best_red_hit_rate = float(np.mean(red_hit_rates)) if red_hit_rates else 0.0
        best_blue_hit_rate = float(np.mean(blue_hit_rates)) if blue_hit_rates else 0.0

        # 选号命中 vs 随机基线
        mean_sel_hits = float(np.mean(selection_total_hits)) if selection_total_hits else 0.0
        selection_eval = evaluate_selection_vs_random(
            data.lottery_name, mean_sel_hits, len(selection_total_hits)
        )

        # loss_history 改为逐期损失序列（按样本序号）
        loss_history = [
            {"iteration": i, "loss": l, "hit_rate": h, "selection_total_hits": s}
            for i, (l, h, s) in enumerate(zip(losses, hit_rates, selection_total_hits))
        ]
        # 限制 history 长度（最多 200 点，均匀采样）
        if len(loss_history) > 200:
            step = len(loss_history) // 200
            loss_history = loss_history[::step][:200]

        early_stopped_at = n_samples  # 表示全部跑完

        result = {
            "window_size": window_size,
            "best_loss": best_loss,
            "best_hit_rate": best_hit_rate,
            "best_hit_rate_red": best_red_hit_rate,
            "best_hit_rate_blue": best_blue_hit_rate,
            "selection_total_hits": mean_sel_hits,
            "selection_eval": selection_eval,
            "best_iteration": 0,
            "early_stopped_at": early_stopped_at,
            "loss_history": loss_history,
        }
        return result

    elif model_type in ("logistic", "random_forest", "lightgbm"):
        # ML 模型：红/蓝与数字型共用泛型 5-fold 滚动交叉验证（按 schema.zones 分段标签）
        return _run_ml_window(data, window_size, model_type, schema, feature_version)


def _zone_hit_rates(pred: np.ndarray, true: np.ndarray, schema) -> List[float]:
    """按 schema.zones 顺序分段，逐区计算 (pred==true) 平均命中率。
    红/蓝双区等价于原「红球命中率 / 蓝球命中率」；数字型按各分区展开。"""
    out = []
    offset = 0
    for zone in schema.zones:
        if pred.shape[1] >= offset + zone.size and true.shape[1] >= offset + zone.size:
            p = pred[:, offset:offset + zone.size]
            t = true[:, offset:offset + zone.size]
            out.append(float(np.mean(p == t)))
        offset += zone.size
    return out


def _run_ml_window(data, window_size, model_type, schema, feature_version) -> dict:
    """泛型 ML 窗口训练（红/蓝与数字型共用）：5-fold 滚动交叉验证 + 逐区命中率"""
    X, y = build_features(data.records, window_size, data.lottery_name, feature_version)
    if X.size == 0:
        return {"error": f"数据不足，window_size={window_size} 过大"}

    n_splits = 5
    cv_losses, cv_hit_rates, cv_red_hit_rates, cv_blue_hit_rates = [], [], [], []
    final_models, final_scaler = None, None
    final_train_size = -1  # 记录 final_models 对应折的训练样本数
    all_probs, all_true = [], []

    for train_idx, val_idx in walk_forward_folds(len(X), n_splits):
        # 严格 expanding window：只用「比验证折更早」的数据训练（见 walk_forward_folds 文档）。
        X_train, y_train = X[train_idx], y[train_idx]
        X_val, y_val = X[val_idx], y[val_idx]

        # 标准化
        scaler = StandardScaler()
        X_train_scaled = scaler.fit_transform(X_train)
        X_val_scaled = scaler.transform(X_val)

        if model_type == "logistic":
            models = _train_logistic(X_train_scaled, y_train)
        elif model_type == "lightgbm":
            models = _train_lightgbm(X_train_scaled, y_train)
        else:
            models = _train_random_forest(X_train_scaled, y_train)

        valid_models = [m for m in models if m is not None]
        if not valid_models:
            continue

        # 收集每号概率与真实标签（供统一评分尺的校准评估）
        prob_rows = []
        for m in models:
            if m is not None:
                prob_rows.append(m.predict_proba(X_val_scaled)[:, 1])
            else:
                prob_rows.append(np.full(len(X_val), 0.5))
        prob_matrix = np.array(prob_rows).T  # (n_val, n_numbers)
        all_probs.append(prob_matrix)
        all_true.append(y_val)

        # 验证
        val_predictions = []
        for m in models:
            if m is not None:
                val_predictions.append(m.predict(X_val_scaled))
            else:
                val_predictions.append(np.zeros(len(X_val)))
        val_predictions = np.array(val_predictions).T
        # 总命中率
        hit_rate = float(np.mean(val_predictions == y_val))
        # 逐区命中率（红/蓝双区等价于原红/蓝独立命中率，数字型按各分区）
        zone_hit = _zone_hit_rates(val_predictions, y_val, schema)
        red_hit_rate = zone_hit[0] if len(zone_hit) >= 1 else hit_rate
        blue_hit_rate = zone_hit[1] if len(zone_hit) >= 2 else hit_rate

        cv_losses.append(1 - hit_rate)
        cv_hit_rates.append(hit_rate)
        cv_red_hit_rates.append(red_hit_rate)
        cv_blue_hit_rates.append(blue_hit_rate)

        # 保存模型供预测使用：取**训练样本最多**的那一折。
        # 原实现取「最后一折」，但那是泄漏版结构——最后一折的训练集掺了 X[:val_start]（更晚的数据）。
        # 修复后每一折的训练集都是 val_end 之后的更早数据，其中折 0 的训练集最大（= 除最新一折外的全量），
        # 参数估计最稳，故改为按训练样本数取最大者。
        if len(train_idx) > final_train_size:
            final_models = models
            final_scaler = scaler
            final_train_size = len(train_idx)

    if not cv_losses:
        return {"error": "交叉验证失败，所有 fold 均无有效模型"}

    # 校准评估（统一评分尺）：用各折概率与真实标签计算命中率/校准误差/综合分
    calibration = None
    selection_eval = None
    selection_total_hits = None
    if all_probs:
        try:
            from train.calibration import evaluate_prob_matrix
            P = np.vstack(all_probs)
            T = np.vstack(all_true)
            calibration = evaluate_prob_matrix(T, P)

            # 选号命中率 vs 随机基线（用概率矩阵按 top-k 选号）
            mean_total, mean_red, mean_blue = _selection_hits_from_prob_matrix(
                data.lottery_name, P, T
            )
            selection_total_hits = mean_total
            selection_eval = evaluate_selection_vs_random(
                data.lottery_name, mean_total, P.shape[0]
            )
            selection_eval["observed_red_hits"] = mean_red
            selection_eval["observed_blue_hits"] = mean_blue
        except Exception as e:
            logger.warning(f"校准/选号评估失败: {e}")

    # 取各折平均值
    best_loss = float(np.mean(cv_losses))
    hit_rate = float(np.mean(cv_hit_rates))
    red_hit_rate = float(np.mean(cv_red_hit_rates))
    blue_hit_rate = float(np.mean(cv_blue_hit_rates))
    # loss_history 记录各折的损失
    loss_history = [
        {"iteration": k, "loss": l, "hit_rate": h}
        for k, (l, h) in enumerate(zip(cv_losses, cv_hit_rates))
    ]
    early_stopped_at = n_splits

    result = {
        "window_size": window_size,
        "feature_version": feature_version,
        "calibration": calibration,
        "best_loss": best_loss,
        "best_hit_rate": hit_rate,
        "best_hit_rate_red": red_hit_rate,
        "best_hit_rate_blue": blue_hit_rate,
        "selection_total_hits": selection_total_hits,
        "selection_eval": selection_eval,
        "best_iteration": 0,
        "early_stopped_at": early_stopped_at,
        "loss_history": loss_history[:50],
        "cv_folds": n_splits,
    }

    # ML 模型：返回模型对象和 scaler 供保存（使用最后一折的模型）
    if final_models is not None:
        result["ml_models"] = final_models
        result["scaler"] = final_scaler

    return result


def _compute_loss_digital(schema, pred_by_zone, actual_by_zone, missing_by_zone, alpha=0.6, beta=0.4) -> float:
    """
    数字型损失：逐区区间偏差均值 + 逐区遗漏偏差均值（区间按各区分段归一化）。
    数字型各分区范围常重叠（如排列5 每区都是 0-9），不能像红/蓝那样按数值区间切分，
    故改为按分区独立计算后取平均。
    """
    if not pred_by_zone:
        return 1.0

    zone_interval, zone_missing = [], []
    for zone in schema.zones:
        lo, hi = zone.range_tuple()
        span = hi - lo + 1
        pred_nums = pred_by_zone.get(zone.name, [])
        actual_nums = actual_by_zone.get(zone.name, [])
        # 1. 区间偏差（按本区分段归一化）
        iv = 0.0
        for pn in pred_nums:
            dists = [abs(pn - an) for an in actual_nums]
            iv += min(dists) if dists else 0
        zone_interval.append(iv / max(len(pred_nums), 1) / span)
        # 2. 遗漏偏差
        miss = missing_by_zone.get(zone.name, {})
        if miss:
            ideal = sum(miss.values()) / len(miss)
            md = 0.0
            for n in pred_nums:
                mv = miss.get(n, ideal)
                md += min(abs(mv - ideal) / max(ideal, 1), 1.0)
            zone_missing.append(md / max(len(pred_nums), 1))

    interval_loss = float(np.mean(zone_interval)) if zone_interval else 0.0
    missing_loss = float(np.mean(zone_missing)) if zone_missing else 0.5
    return alpha * interval_loss + beta * missing_loss


def _train_single_window_digital(
    data, window_size, max_iterations, patience, model_type, alpha, beta, schema, feature_version=FEATURE_VERSION_STANDARD
) -> dict:
    """数字型单窗口训练：逐区频率/遗漏/预测 + 泛型损失；ML 复用 _run_ml_window"""
    records = data.records
    n_samples = len(records) - window_size
    if n_samples <= 0:
        return {"error": f"数据量不足，需要至少 {window_size + 1} 期"}

    if model_type == "statistical":
        losses, hit_rates, zone_hit_acc = [], [], []
        selection_total_hits = []
        total_choose = schema.total_choose
        for i in range(n_samples):
            window = records[i:i + window_size]
            actual = records[i + window_size]
            pred_by_zone, actual_by_zone, missing_by_zone = {}, {}, {}
            for zone in schema.zones:
                lo, hi = zone.range_tuple()
                zfreq = {}
                zmiss = {n: 0 for n in range(lo, hi + 1)}
                for rec in window:
                    nums = record_zone_numbers(rec, zone)
                    for n in nums:
                        zfreq[n] = zfreq.get(n, 0) + 1
                    for n in range(lo, hi + 1):
                        if n in nums:
                            break
                        zmiss[n] += 1
                pred_by_zone[zone.name] = [
                    n for n, _ in sorted(zfreq.items(), key=lambda x: x[1], reverse=True)[: zone.choose]
                ]
                actual_by_zone[zone.name] = record_zone_numbers(actual, zone)
                missing_by_zone[zone.name] = zmiss
            loss = _compute_loss_digital(schema, pred_by_zone, actual_by_zone, missing_by_zone, alpha, beta)
            losses.append(loss)
            # 逐区命中
            total_hits = 0
            for zone in schema.zones:
                h = len(set(pred_by_zone[zone.name]) & set(actual_by_zone[zone.name]))
                total_hits += h
                zone_hit_acc.append(h / zone.choose)
            hit_rates.append(total_hits / total_choose)

            # 选号命中率（与随机基线对比的真实指标）
            sel_hits = compute_selection_hits(
                data.lottery_name,
                pred_by_zone,
                actual,
            )
            selection_total_hits.append(sel_hits["total_hits"])

        best_loss = float(np.mean(losses)) if losses else 1.0
        best_hit_rate = float(np.mean(hit_rates)) if hit_rates else 0.0
        zone_hit = float(np.mean(zone_hit_acc)) if zone_hit_acc else 0.0

        # 选号命中 vs 随机基线
        mean_sel_hits = float(np.mean(selection_total_hits)) if selection_total_hits else 0.0
        selection_eval = evaluate_selection_vs_random(
            data.lottery_name, mean_sel_hits, len(selection_total_hits)
        )

        # loss_history 改为逐期损失序列（按样本序号）
        loss_history = [
            {"iteration": i, "loss": l, "hit_rate": h, "selection_total_hits": s}
            for i, (l, h, s) in enumerate(zip(losses, hit_rates, selection_total_hits))
        ]
        # 限制 history 长度（最多 200 点，均匀采样）
        if len(loss_history) > 200:
            step = len(loss_history) // 200
            loss_history = loss_history[::step][:200]

        early_stopped_at = n_samples  # 表示全部跑完

        result = {
            "window_size": window_size,
            "best_loss": best_loss,
            "best_hit_rate": best_hit_rate,
            "best_hit_rate_red": zone_hit,   # 兼容前端字段：数字型取各分区平均命中率
            "best_hit_rate_blue": zone_hit,
            "selection_total_hits": mean_sel_hits,
            "selection_eval": selection_eval,
            "best_iteration": 0,
            "early_stopped_at": early_stopped_at,
            "loss_history": loss_history,
        }
        return result

    elif model_type in ("logistic", "random_forest", "lightgbm"):
        return _run_ml_window(data, window_size, model_type, schema, feature_version)
    else:
        return {"error": f"未知 model_type: {model_type}"}


def save_training_record(result: dict) -> Path:
    """保存训练记录到 training/ 目录"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = result["lottery_name"]
    code = LOTTERY_CONFIG[name]["name_en"]
    record_dir = TRAINING_DIR / f"{timestamp}_{code}"
    record_dir.mkdir(parents=True, exist_ok=True)

    # 保存参数
    with open(record_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(result["config"], f, ensure_ascii=False, indent=2)

    # 保存 ML 模型权重（用于预测引擎加载）
    best_w = result.get("best_window", "50")
    window_results = result.get("multi_window_results", {})
    if window_results and best_w in window_results:
        wr = window_results[str(best_w)]
        if "ml_models" in wr:
            with open(record_dir / "model.pkl", "wb") as f:
                pickle.dump(wr["ml_models"], f)
            if "scaler" in wr:
                with open(record_dir / "scaler.pkl", "wb") as f:
                    pickle.dump(wr["scaler"], f)
            logger.info(f"ML 模型已保存: {record_dir / 'model.pkl'}")

    # statistical 模式：保存统计参数包（供 trained 模式加载）
    if result["model_type"] == "statistical":
        base = result["基础参数"]
        zone_names = [k[:-2] for k in base.keys() if k.endswith("频率")]
        wr = window_results.get(str(best_w), {}) if (window_results and best_w in window_results) else {}
        stat_pack = {
            "model_type": "statistical",
            "best_window": result["best_window"],
            "best_loss": result["best_loss"],
            "best_hit_rate": wr.get("best_hit_rate", 0),
            # 泛型分区参数包（数字型按区名存储；红/蓝双区范围重叠不适用合并 dict）
            "zone_freqs": {z: base[f"{z}频率"] for z in zone_names},
            "zone_missing": {z: base[f"{z}遗漏"] for z in zone_names},
        }
        # 向后兼容：红/蓝双区保留旧字段名（predict 端 statistical 分支读取）
        if "红球频率" in base:
            stat_pack["freqs"] = base["红球频率"]
            stat_pack["missing"] = base["红球遗漏"]
            stat_pack["blue_freqs"] = base["蓝球频率"]
            stat_pack["blue_missing"] = base["蓝球遗漏"]
        with open(record_dir / "stat_pack.json", "w", encoding="utf-8") as f:
            json.dump(stat_pack, f, ensure_ascii=False)
        logger.info(f"统计参数包已保存: {record_dir / 'stat_pack.json'}")

    # 保存模型参数（JSON 可读）
    best_wr = window_results.get(str(best_w), {}) if (window_results and best_w in window_results) else {}
    model_params = {
        "model_type": result["model_type"],
        "best_window": result["best_window"],
        "best_loss": result["best_loss"],
        "selection_total_hits": best_wr.get("selection_total_hits"),
        "selection_eval": best_wr.get("selection_eval"),
        "基础参数": result["基础参数"],
    }
    with open(record_dir / "model_params.json", "w", encoding="utf-8") as f:
        json.dump(model_params, f, ensure_ascii=False, indent=2)

    # 生成可读报告
    report = _generate_report(result)
    with open(record_dir / "report.md", "w", encoding="utf-8") as f:
        f.write(report)

    logger.info(f"训练记录已保存: {record_dir}")
    return record_dir


def _generate_report(result: dict) -> str:
    """生成训练报告 Markdown"""
    lines = [
        f"# {result['lottery_name']} 训练报告",
        f"",
        f"- **训练时间**: {result['training_time']}",
        f"- **模型类型**: {result['model_type']}",
        f"- **总期数**: {result['total_records']}",
        f"- **最优窗口**: {result['best_window']} 期（损失={result['best_loss']:.4f}）",
        f"",
        f"## 选号命中率 vs 随机基线（关键指标）",
        f"",
        f"> 注意：旧版『命中率』是号码级准确率（把多数不开的号码猜对也算命中），",
        f"> 天然高达 ~85%，极具误导性。以下『选号命中』才是按规则买一组号码时的真实命中数。",
        f"",
        f"| 窗口 | 选号平均命中 | 随机基线期望 | 优势 (Δ) | 评估期数 | p 值 | 显著性 |",
        f"|------|-------------|-------------|---------|---------|------|--------|",
    ]
    for w, wr in result["multi_window_results"].items():
        if "error" in wr:
            lines.append(f"| {w} | ERROR | - | - | - | - | {wr.get('error', '')} |")
        else:
            sev = wr.get("selection_eval")
            if sev:
                obs = sev["observed_total_hits"]
                exp = sev["expected_total_hits"]
                adv = sev["total_advantage"]
                n = sev["n_periods"]
                p = sev["p_value"]
                sig = "✅ 显著优于随机" if sev.get("significant_05") and adv > 0 else (
                    "❌ 显著差于随机" if sev.get("significant_05") and adv < 0 else "➖ 与随机无显著差异"
                )
                lines.append(
                    f"| {w} | {obs:.3f} | {exp:.3f} | {adv:+.3f} | {n} | {p:.4f} | {sig} |"
                )
            else:
                lines.append(f"| {w} | - | - | - | - | - | 未计算 |")

    lines.extend([
        f"",
        f"## 多窗口对比",
        f"",
        f"| 窗口 | 最优损失 | 命中率 | 早停位置 |",
        f"|------|---------|--------|---------|",
    ])
    for w, wr in result["multi_window_results"].items():
        if "error" in wr:
            lines.append(f"| {w} | ERROR | - | {wr.get('error', '')} |")
        else:
            lines.append(f"| {w} | {wr['best_loss']:.4f} | {wr['best_hit_rate']*100:.2f}% | 迭代 {wr.get('early_stopped_at', 'N/A')} |")

    lines.extend([
        f"",
        f"## 参数配置",
        f"",
    ])
    for k, v in result["config"].items():
        lines.append(f"- **{k}**: {v}")

    lines.extend([
        f"",
        f"## 基础统计参数",
        f"",
    ])
    # 各分区频率 Top10（红/蓝双区与数字型分区统一处理）
    for zname in result["基础参数"]:
        if zname.endswith("频率"):
            zfreq = result["基础参数"][zname]
            sorted_z = sorted(zfreq.items(), key=lambda x: x[1], reverse=True)[:10]
            lines.append(f"### {zname} Top10")
            for k, v in sorted_z:
                lines.append(f"- 号码 {k}: {v} 次")

    return "\n".join(lines)
