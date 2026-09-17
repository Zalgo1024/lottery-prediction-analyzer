# -*- coding: utf-8 -*-
"""红蓝拆分子模型（双色球/大乐透专用）：红球、蓝球分开训练、分开评估、最后组合。

动机
----
现有 ML 训练把红/蓝球统一成「逐号码 0/1 出现」的二分类堆（一个号码一个模型），
蓝球只在最后采样时与红球拼在一起。本模块把蓝球单独拿出来做成**真正的多分类**：

- 双色球：蓝球 16 选 1 → **16 分类单标签**（输入窗口特征，输出「本期开哪个蓝球」）；
- 大乐透：蓝球 12 选 2 → 两个 12 分类头（低号 head 学 min(蓝球)，高号 head 学 max(蓝球)），
  预测时按两 head 概率之和取 top-2。

红球保持逐号二分类（33/35 选 6/5 的组合结构不适合单标签多分类）。

流程
----
1. 特征复用 `data.features.build_features`（与主训练完全一致，保证可进 trained 模式）；
2. 走前验证（复用 `walk_forward_folds`，训练集恒早于验证折，无 look-ahead）；
3. 每折分别评估：
   - 红球：按二分类概率 top-k 选号 → 平均命中数 vs 随机基线 k²/N；
   - 蓝球：多分类 argmax/top-2 → 平均命中数 vs 随机基线 k²/M，并对比「窗口频率」朴素基线；
   - 组合：红+蓝拼成整票，`compute_selection_hits` vs 随机基线；
4. 取训练样本最大的一折的模型落盘 `training/<ts>_<code>_blue_split/`，
   model.pkl 为结构化 dict（format=redblue_split_v1），`_get_trained_params` 会优先
   加载它，`_predict_with_ml` 识别后红蓝分别出概率再组合——即「最后组合」。

诚实层
------
蓝球 16 选 1 的随机基线是 1/16=6.25%，任何「命中率高于基线」都必须同时报告样本量
与二项波动；本模块输出的是描述性对比，不构成任何下注证据。机制结论（期望线性性）
不因换模型结构而改变。
"""
from __future__ import annotations

import json
import logging
import pickle
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np

from config import LOTTERY_CONFIG, TRAINING_DIR
from data.loader import load_lottery
from data.schema import LotteryData, is_redblue, schema_from_cfg
from data.features import (
    build_features,
    FEATURE_VERSION_STANDARD,
    FEATURE_VERSION_RICH,
)
from train.engine import (
    _MIN_TRAIN_SAMPLES,
    _train_lightgbm,
    _train_logistic,
    _train_random_forest,
    walk_forward_folds,
)
from train.metrics import (
    compute_selection_hits,
    evaluate_selection_vs_random,
)

logger = logging.getLogger(__name__)

#: model.pkl 的结构化格式标记（_predict_with_ml 据此分派）
BLUE_SPLIT_FORMAT = "redblue_split_v1"

_N_SPLITS = 5

_BLUE_MODEL_TYPES = ("logistic", "random_forest", "lightgbm")


# ============================================================
# 数据集构建
# ============================================================

def _blue_labels(lottery_name: str, records: list) -> Dict[str, np.ndarray]:
    """蓝球多分类标签。

    双色球（choose=1）：`main` = 唯一蓝球在 1..16 中的下标（0-based）。
    大乐透（choose=2）：`low`/`high` = 两个蓝球排序后的低位/高位下标。
    """
    schema = schema_from_cfg(LOTTERY_CONFIG[lottery_name])
    blue_zone = schema.blue_zone
    b_min = blue_zone.min
    choose = blue_zone.choose
    main: List[int] = []
    low: List[int] = []
    high: List[int] = []
    for rec in records:
        blues = sorted(rec.蓝球)
        if choose == 1:
            main.append(blues[0] - b_min)
        else:
            low.append(blues[0] - b_min)
            high.append(blues[1] - b_min)
    if choose == 1:
        return {"main": np.array(main)}
    return {"low": np.array(low), "high": np.array(high)}


def _train_blue_mc(X_train, labels: np.ndarray, model_type: str, hp=None):
    """训练一个蓝球多分类头（类别数由数据决定，缺类时 classes_ 会少）。"""
    if model_type == "lightgbm":
        from lightgbm import LGBMClassifier
        m = LGBMClassifier(
            n_estimators=(hp or {}).get("n_estimators", 200),
            num_leaves=(hp or {}).get("num_leaves", 31),
            learning_rate=(hp or {}).get("learning_rate", 0.05),
            random_state=42,
            class_weight="balanced",
            verbosity=-1,
        )
    elif model_type == "random_forest":
        from sklearn.ensemble import RandomForestClassifier
        m = RandomForestClassifier(
            n_estimators=(hp or {}).get("n_estimators", 200),
            max_depth=(hp or {}).get("max_depth", 12),
            random_state=42,
            class_weight="balanced",
        )
    else:
        from sklearn.linear_model import LogisticRegression
        m = LogisticRegression(
            max_iter=2000, solver="lbfgs",
            class_weight="balanced", C=(hp or {}).get("C", 1.0),
        )
    m.fit(X_train, labels)
    return m


def _mc_number_probs(model, X: np.ndarray, blue_size: int) -> np.ndarray:
    """把多分类头的 predict_proba 摊回「蓝球号码下标 → 概率」向量。

    训练折里没出现过的类不在 classes_ 里，记 0 概率（采样端已有 +ε 兜底）。
    """
    probs = np.zeros((X.shape[0], blue_size), dtype=float)
    proba = model.predict_proba(X)
    for j, cls in enumerate(model.classes_):
        idx = int(cls)
        if 0 <= idx < blue_size:
            probs[:, idx] = proba[:, j]
    return probs


def _blue_pick(model_heads: List, X: np.ndarray, blue_size: int,
               blue_choose: int) -> Tuple[np.ndarray, np.ndarray]:
    """由多分类头得到 (每期选中的蓝球下标数组, 每号概率矩阵)。

    choose=1：单头 argmax；choose=2：两头概率相加后取 top-2。
    返回 picks 形状 (n_periods, blue_choose)，prob 矩阵形状 (n_periods, blue_size)。
    """
    head_probs = [_mc_number_probs(m, X, blue_size) for m in model_heads]
    if len(head_probs) == 1:
        prob = head_probs[0]
    else:
        prob = sum(head_probs) / len(head_probs)
    picks = np.argsort(-prob, axis=1)[:, :blue_choose]
    return picks, prob


# ============================================================
# 走前验证训练 + 评估
# ============================================================

def _fold_train(X_train, y_red_train, blue_labels_train, model_type):
    """一折内分别训练红球（逐号二分类）与蓝球（多分类）。"""
    red_models = {"logistic": _train_logistic,
                  "random_forest": _train_random_forest,
                  "lightgbm": _train_lightgbm}[model_type](X_train, y_red_train)
    blue_heads = [_train_blue_mc(X_train, v, model_type)
                  for v in blue_labels_train.values()]
    return red_models, blue_heads


def train_blue_split(
    lottery_name: str,
    model_type: str = "lightgbm",
    window: int = None,
    features: str = "standard",
    seed: int = 42,
) -> dict:
    """红蓝拆分训练主入口（仅双色球/大乐透）。返回结果 dict 并落盘训练记录。"""
    if lottery_name not in LOTTERY_CONFIG or not is_redblue(lottery_name):
        raise ValueError(f"红蓝拆分子模型仅支持双色球/大乐透，收到：{lottery_name}")
    if model_type not in _BLUE_MODEL_TYPES:
        raise ValueError(f"model_type 须为 {_BLUE_MODEL_TYPES} 之一，收到：{model_type}")

    from config import TRAIN_DEFAULTS
    if window is None:
        window = TRAIN_DEFAULTS["window"]
    feature_version = FEATURE_VERSION_RICH if features == "rich" else FEATURE_VERSION_STANDARD
    np.random.seed(seed)

    data = load_lottery(lottery_name)
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg)
    red_zone, blue_zone = schema.red_zone, schema.blue_zone
    r_size, b_size = red_zone.size, blue_zone.size
    records = data.records

    X, y = build_features(records, window, lottery_name, feature_version)
    if X.size == 0:
        raise ValueError(f"数据不足，window={window} 过大")
    y_red = y[:, :r_size]
    blue_labels = _blue_labels(lottery_name, records[window:])

    issues = [r.期号 for r in records if r.期号]
    data_version = {"max_issue": max(issues) if issues else None,
                    "total_records": data.total_records,
                    "训练时间": datetime.now().isoformat(), "seed": seed}

    folds_out = []
    final = None
    final_train_size = -1
    for train_idx, val_idx in walk_forward_folds(len(X), _N_SPLITS, _MIN_TRAIN_SAMPLES):
        X_train, X_val = X[train_idx], X[val_idx]
        scaler = None
        from sklearn.preprocessing import StandardScaler
        scaler = StandardScaler()
        X_train_s = scaler.fit_transform(X_train)
        X_val_s = scaler.transform(X_val)

        bl_train = {k: v[train_idx] for k, v in blue_labels.items()}
        red_models, blue_heads = _fold_train(X_train_s, y_red[train_idx], bl_train, model_type)

        # ---- 红球：二分类概率 top-k ----
        red_probs = np.zeros((len(X_val), r_size))
        for i, m in enumerate(red_models):
            if m is not None:
                red_probs[:, i] = m.predict_proba(X_val_s)[:, 1]
        red_picks = np.argsort(-red_probs, axis=1)[:, :red_zone.choose]

        # ---- 蓝球：多分类 top-k + 朴素频率基线 ----
        blue_picks, blue_prob = _blue_pick(blue_heads, X_val_s, b_size, blue_zone.choose)
        freq_blue_hits = []
        for j in val_idx:
            win = records[j + window - window: j + window]
            bf = {}
            for rec in win:
                for n in rec.蓝球:
                    bf[n] = bf.get(n, 0) + 1
            top = [n for n, _ in sorted(bf.items(), key=lambda x: x[1], reverse=True)[:blue_zone.choose]]
            freq_blue_hits.append([n - blue_zone.min for n in top])

        # ---- 逐期评估 ----
        red_hits_l, blue_hits_l, blue_freq_hits_l, sel_hits_l = [], [], [], []
        for row, vp in enumerate(val_idx):
            actual = records[vp + window]
            pr = sorted(int(n) + red_zone.min for n in red_picks[row])
            pb = sorted(int(n) + blue_zone.min for n in blue_picks[row])
            pf = sorted(int(n) + blue_zone.min for n in freq_blue_hits[row])
            red_hits_l.append(len(set(pr) & set(actual.红球)))
            blue_hits_l.append(len(set(pb) & set(actual.蓝球)))
            blue_freq_hits_l.append(len(set(pf) & set(actual.蓝球)))
            sel = compute_selection_hits(lottery_name, {"红球": pr, "蓝球": pb}, actual)
            sel_hits_l.append(sel["total_hits"])

        red_hits_l, blue_hits_l = np.array(red_hits_l), np.array(blue_hits_l)
        sel_hits_l = np.array(sel_hits_l)
        mean_sel = float(np.mean(sel_hits_l)) if len(sel_hits_l) else 0.0
        fold = {
            "n_val": int(len(val_idx)),
            "red_mean_hits": float(np.mean(red_hits_l)),
            "blue_mean_hits": float(np.mean(blue_hits_l)),
            "blue_freq_mean_hits": float(np.mean(blue_freq_hits_l)) if blue_freq_hits_l else 0.0,
            "blue_freq_lift": float(np.mean(blue_freq_hits_l) / (blue_zone.choose ** 2 / b_size))
                if blue_freq_hits_l else 0.0,
            "combined_selection_eval": evaluate_selection_vs_random(lottery_name, mean_sel, len(sel_hits_l)),
        }
        folds_out.append(fold)

        if len(train_idx) > final_train_size:
            final = {"red_models": red_models, "blue_heads": blue_heads,
                     "scaler": scaler, "train_size": int(len(train_idx))}
            final_train_size = len(train_idx)

    if not folds_out or final is None:
        raise ValueError("走前验证失败：没有有效折（数据量不足？）")

    # ---- 汇总 ----
    def _avg(key: str) -> float:
        return float(np.mean([f[key] for f in folds_out]))

    combined = {
        "observed_total_hits": float(np.mean([f["combined_selection_eval"]["observed_total_hits"] for f in folds_out])),
        "expected_total_hits": float(np.mean([f["combined_selection_eval"]["expected_total_hits"] for f in folds_out])),
        "total_advantage": float(np.mean([f["combined_selection_eval"]["total_advantage"] for f in folds_out])),
        "n_periods": int(sum(f["combined_selection_eval"]["n_periods"] for f in folds_out)),
    }
    exp_blue = blue_zone.choose ** 2 / b_size
    result = {
        "lottery_name": lottery_name,
        "model_type": model_type,
        "feature_version": feature_version,
        "window": window,
        "data_version": data_version,
        "folds": folds_out,
        "red_choose": red_zone.choose,
        "blue_choose": blue_zone.choose,
        "blue_size": b_size,
        "red_mean_hits": _avg("red_mean_hits"),
        "red_expected_hits": red_zone.choose ** 2 / r_size,
        "blue_mean_hits": _avg("blue_mean_hits"),
        "blue_expected_hits": exp_blue,
        "blue_freq_mean_hits": _avg("blue_freq_mean_hits"),
        "blue_freq_lift": _avg("blue_freq_lift"),
        "blue_lift": _avg("blue_mean_hits") / exp_blue if exp_blue > 0 else 0.0,
        "combined": combined,
    }

    record_dir = _save_record(result, final)
    result["record_dir"] = str(record_dir)
    return result


# ============================================================
# 落盘（结构化 model.pkl，供 _predict_with_ml 识别组合）
# ============================================================

def _save_record(result: dict, final: dict) -> Path:
    code = LOTTERY_CONFIG[result["lottery_name"]]["name_en"]
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    record_dir = TRAINING_DIR / f"{ts}_{code}_blue_split"
    record_dir.mkdir(parents=True, exist_ok=True)

    config = {
        "model_type": result["model_type"],
        "window": result["window"],
        "feature_version": result["feature_version"],
        "format": BLUE_SPLIT_FORMAT,
        "data_version": result["data_version"],
    }
    with open(record_dir / "config.json", "w", encoding="utf-8") as f:
        json.dump(config, f, ensure_ascii=False, indent=2)

    payload = {
        "format": BLUE_SPLIT_FORMAT,
        "lottery_name": result["lottery_name"],
        "red": {"models": final["red_models"], "scaler": final["scaler"]},
        "blue": {"heads": final["blue_heads"]},
        "feature_version": result["feature_version"],
        "window": result["window"],
    }
    with open(record_dir / "model.pkl", "wb") as f:
        pickle.dump(payload, f)
    with open(record_dir / "scaler.pkl", "wb") as f:
        pickle.dump(final["scaler"], f)

    with open(record_dir / "metrics.json", "w", encoding="utf-8") as f:
        json.dump({k: v for k, v in result.items() if k != "record_dir"},
                  f, ensure_ascii=False, indent=2, default=str)
    with open(record_dir / "report.md", "w", encoding="utf-8") as f:
        f.write(_generate_report(result))
    logger.info(f"红蓝拆分子模型已保存: {record_dir}")
    return record_dir


def _generate_report(result: dict) -> str:
    lz = result["lottery_name"]
    lines = [
        f"# {lz} 红蓝拆分子模型报告",
        "",
        f"- 训练时间：{result['data_version']['训练时间']}",
        f"- 模型：红球=逐号二分类，蓝球=多分类子模型；model_type={result['model_type']}",
        f"- 窗口={result['window']}，特征版本={result['feature_version']}，数据截至第 {result['data_version']['max_issue']} 期",
        "",
        "## 分开评估（走前验证，5 折平均）",
        "",
        "| 部分 | 模型平均命中 | 随机基线 | Lift |",
        "|---|---|---|---|",
        f"| 红球（top-{result['red_choose']}） | "
        f"{result['red_mean_hits']:.3f} | {result['red_expected_hits']:.3f} | "
        f"{result['red_mean_hits'] / result['red_expected_hits']:.2f} |",
        f"| 蓝球（多分类 top-{result['blue_choose']}） | {result['blue_mean_hits']:.3f} | {result['blue_expected_hits']:.3f} | {result['blue_lift']:.2f} |",
        f"| 蓝球（窗口频率朴素基线） | {result['blue_freq_mean_hits']:.3f} | {result['blue_expected_hits']:.3f} | {result['blue_freq_lift']:.2f} |",
        "",
        "## 组合（红+蓝整票 vs 随机基线）",
        "",
        f"- 观测平均命中：{result['combined']['observed_total_hits']:.3f}",
        f"- 随机期望：{result['combined']['expected_total_hits']:.3f}",
        f"- 优势 Δ：{result['combined']['total_advantage']:+.3f}（n={result['combined']['n_periods']} 期）",
        "",
        "## 各折明细",
        "",
        "| 折 | 验证期数 | 红命中 | 蓝命中(模型) | 蓝命中(频率) | 组合优势 |",
        "|---|---|---|---|---|---|",
    ]
    for i, f_ in enumerate(result["folds"], 1):
        lines.append(
            f"| {i} | {f_['n_val']} | {f_['red_mean_hits']:.3f} | "
            f"{f_['blue_mean_hits']:.3f} | {f_['blue_freq_mean_hits']:.3f} | "
            f"{f_['combined_selection_eval']['total_advantage']:+.3f} |")
    lines += [
        "",
        "## 诚实层",
        "",
        "- 蓝球多分类的随机基线 = k²/M；Lift>1 的波动需结合每折验证期数评估，单折起落不构成信号。",
        "- 组合优势与主训练口径一致地用 `evaluate_selection_vs_random` 计算；不显著 ≡ 与随机一致。",
        "- 蓝球单独建模不会改变长期 EV≈−50% 的机制结论。",
    ]
    return "\n".join(lines)
