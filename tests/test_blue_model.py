# -*- coding: utf-8 -*-
"""红蓝拆分子模型契约测试：蓝球多分类标签、走前评估口径、组合推理、加载优先级。"""
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.schema import DrawRecord


def _mk_records(n=140, seed=7):
    """合成双色球开奖记录：红 6/33、蓝 1/16（确定性伪随机，避免真实数据依赖）。"""
    rng = np.random.RandomState(seed)
    recs = []
    for i in range(n):
        reds = sorted(rng.choice(range(1, 34), size=6, replace=False).tolist())
        blue = int(rng.choice(range(1, 17)))
        recs.append(DrawRecord(
            期号=23001 + i,
            zone_numbers={"红球": reds, "蓝球": [blue]},
        ))
    return recs


@pytest.fixture
def ssq_data(monkeypatch):
    from data.schema import LotteryData
    recs = _mk_records()
    monkeypatch.setattr("train.blue_model.load_lottery",
                        lambda name: LotteryData(lottery_name=name, records=recs))
    return recs


@pytest.fixture
def dlt_data(monkeypatch):
    from data.schema import LotteryData
    rng = np.random.RandomState(11)
    recs = []
    for i in range(140):
        reds = sorted(rng.choice(range(1, 36), size=5, replace=False).tolist())
        blues = sorted(rng.choice(range(1, 13), size=2, replace=False).tolist())
        recs.append(DrawRecord(
            期号=23001 + i,
            zone_numbers={"红球": reds, "蓝球": blues},
        ))
    monkeypatch.setattr("train.blue_model.load_lottery",
                        lambda name: LotteryData(lottery_name=name, records=recs))
    return recs


# ------------------------------------------------------------
# 1. 蓝球多分类标签
# ------------------------------------------------------------

def test_blue_labels_ssq_single_class(ssq_data):
    from train.blue_model import _blue_labels
    labels = _blue_labels("双色球", ssq_data)
    assert set(labels.keys()) == {"main"}
    assert len(labels["main"]) == len(ssq_data)
    # 每个标签都在 0..15，且与记录一致
    for rec, lab in zip(ssq_data, labels["main"]):
        assert 0 <= lab <= 15
        assert lab == rec.蓝球[0] - 1


def test_blue_labels_dlt_two_heads(dlt_data):
    from train.blue_model import _blue_labels
    labels = _blue_labels("大乐透", dlt_data)
    assert set(labels.keys()) == {"low", "high"}          # choose=2：双头，无 main
    for rec, lo, hi in zip(dlt_data, labels["low"], labels["high"]):
        assert lo == rec.蓝球[0] - 1 and hi == rec.蓝球[1] - 1
        assert lo <= hi


def test_blue_split_rejects_non_redblue():
    from train.blue_model import train_blue_split
    with pytest.raises(ValueError):
        train_blue_split("福彩3D")


# ------------------------------------------------------------
# 2. 走前训练 + 分开评估（小样本快速通道）
# ------------------------------------------------------------

def test_train_blue_split_ssq(ssq_data, monkeypatch):
    from train import blue_model as bm
    # 小样本快速通道：2 折、最小训练样本调低
    monkeypatch.setattr(bm, "_N_SPLITS", 2)
    monkeypatch.setattr(bm, "_MIN_TRAIN_SAMPLES", 30)
    res = bm.train_blue_split("双色球", model_type="logistic", window=30, seed=3)
    # 分开评估的关键字段
    assert 0 <= res["red_mean_hits"] <= 6
    assert 0 <= res["blue_mean_hits"] <= 1
    assert abs(res["blue_expected_hits"] - 1 / 16) < 1e-9
    assert abs(res["red_expected_hits"] - 36 / 33) < 1e-9
    assert res["blue_lift"] >= 0
    # 组合评估
    c = res["combined"]
    assert c["n_periods"] > 0
    assert "total_advantage" in c
    # 落盘
    rd = Path(res["record_dir"])
    assert (rd / "model.pkl").exists() and (rd / "report.md").exists()
    cfg = (rd / "config.json")
    assert cfg.exists() and "redblue_split_v1" in cfg.read_text(encoding="utf-8")


def test_train_blue_split_dlt(dlt_data, monkeypatch):
    from train import blue_model as bm
    monkeypatch.setattr(bm, "_N_SPLITS", 2)
    monkeypatch.setattr(bm, "_MIN_TRAIN_SAMPLES", 30)
    res = bm.train_blue_split("大乐透", model_type="logistic", window=30, seed=5)
    # 12 选 2 的随机基线 = 4/12
    assert abs(res["blue_expected_hits"] - 4 / 12) < 1e-9
    assert 0 <= res["blue_mean_hits"] <= 2


# ------------------------------------------------------------
# 3. 组合推理端（_predict_with_ml 识别结构化模型）
# ------------------------------------------------------------

def test_predict_with_ml_supports_split_model(ssq_data):
    """红蓝拆分模型返回扁平概率列表：前 33 位红段、后 16 位蓝段。"""
    from prediction.engine import _predict_with_ml, _build_ml_features
    from data.schema import LotteryData
    from sklearn.linear_model import LogisticRegression

    # 造一个最小可用的结构化模型（权重随机，只验证格式与分派）；
    # 模型必须在真实特征维度上拟合，否则 predict_proba 会因特征数不符报错
    data = LotteryData(lottery_name="双色球", records=ssq_data)
    x = _build_ml_features(data, window_size=30, feature_version=1)
    rng = np.random.RandomState(0)
    X_fit = np.repeat(x, 60, axis=0) + rng.rand(60, x.shape[1]) * 0.05
    red_models = [LogisticRegression().fit(X_fit, rng.randint(0, 2, 60)) for _ in range(33)]
    blue_head = LogisticRegression().fit(X_fit, rng.randint(0, 16, 60))
    payload = {
        "format": "redblue_split_v1",
        "lottery_name": "双色球",
        "red": {"models": red_models, "scaler": None},
        "blue": {"heads": [blue_head]},
        "feature_version": 1,
        "window": 30,
    }
    probs = _predict_with_ml(data, payload, None, window_size=30, feature_version=1)
    assert len(probs) == 33 + 16
    assert all(0 <= p <= 1 for p in probs)


# ------------------------------------------------------------
# 4. 加载优先级（_get_trained_params 优先 blue_split）
# ------------------------------------------------------------

def test_get_trained_params_prefers_blue_split(ssq_data, monkeypatch, tmp_path):
    import prediction.engine as eng
    fake_dir = tmp_path / "training"
    fake_dir.mkdir()
    monkeypatch.setattr(eng, "TRAINING_DIR", fake_dir)
    # 旧格式目录（更新时间戳更小）+ blue_split 目录
    old = fake_dir / "20260101_000000_ssq"
    new = fake_dir / "20260102_000000_ssq_blue_split"
    for d in (old, new):
        d.mkdir()
        (d / "config.json").write_text("{}", encoding="utf-8")
    (old / "model_params.json").write_text("{}", encoding="utf-8")
    (new / "model.pkl").write_bytes(b"x")

    params = eng._get_trained_params("双色球")
    assert params["record_dir"] == str(new)


def test_get_trained_params_falls_back_to_legacy(ssq_data, monkeypatch, tmp_path):
    import prediction.engine as eng
    fake_dir = tmp_path / "training"
    fake_dir.mkdir()
    monkeypatch.setattr(eng, "TRAINING_DIR", fake_dir)
    old = fake_dir / "20260101_000000_ssq"
    old.mkdir()
    (old / "model_params.json").write_text("{}", encoding="utf-8")
    params = eng._get_trained_params("双色球")
    assert params["record_dir"] == str(old)
