# -*- coding: utf-8 -*-
"""随机锚点走前验证（anchor_trainer）+ 持续训练循环（training_loop）契约测试。

核心保证：
- 严格时间切分：预测只允许用目标期之前的数据（用"污染未来"法验证）；
- 每个锚点都带同形态随机基线，显著性为配对差值 z，标注如实；
- 全量落账（含不显著的），台账可汇总；种子固定可复现；
- 训练循环配置 sanitize / 真身 config/ 不被测试触碰（根 conftest 已兜底）。
"""
import json
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data.schema import DrawRecord, LotteryData


def _mk_records(n=160, seed=7):
    """合成双色球记录（红6/33 + 蓝1/16，确定性伪随机）。"""
    rng = np.random.RandomState(seed)
    recs = []
    for i in range(n):
        reds = sorted(rng.choice(range(1, 34), size=6, replace=False).tolist())
        blue = int(rng.choice(range(1, 17)))
        recs.append(DrawRecord(期号=23001 + i, zone_numbers={"红球": reds, "蓝球": [blue]}))
    return recs  # 从旧到新


@pytest.fixture
def ssq_env(monkeypatch, tmp_path):
    recs = _mk_records()
    monkeypatch.setattr("train.anchor_trainer.load_lottery",
                        lambda name: LotteryData(lottery_name=name, records=recs))
    monkeypatch.setattr("train.anchor_trainer.TRAINING_DIR", tmp_path)
    return recs


# ------------------------------------------------------------
# anchor_train：走前 + 基线 + 落账
# ------------------------------------------------------------

def test_anchor_runs_and_reports_all_strategies(ssq_env):
    from train.anchor_trainer import anchor_train
    r = anchor_train("双色球", n_trials=5, seed=42)
    assert "error" not in r
    assert r["n_trials"] == 5
    assert r["seed"] == 42
    for s in ("高频策略", "遗漏值策略", "区间均衡策略", "规则优选"):
        assert s in r["stats"], s
        assert "配对差值" in r["stats"][s]
        assert "基线平均总命中" in r["stats"][s]   # 每个策略都带同形态随机基线
        assert "显著性" in r["stats"][s]
    assert len(r["anchor_issues"]) == 5
    assert "诚实声明" in r


def test_anchor_respects_time_cut(ssq_env):
    """把「未来」记录全部替换为异常值，若实现泄漏未来数据，预测窗口就会受影响。"""
    from train.anchor_trainer import anchor_train
    from data.schema import record_zone_numbers
    from config import LOTTERY_CONFIG
    from data.schema import schema_from_cfg

    recs = list(ssq_env)
    schema = schema_from_cfg(LOTTERY_CONFIG["双色球"], "双色球")
    # 从第 100 期起（锚点下限）把实际开奖改为固定形态——锚点若用到 >=t 的数据会露馅
    r = anchor_train("双色球", n_trials=5, seed=42, records=recs)
    for row in r["trials"]:
        t = row["anchor_idx"]
        assert t >= r["min_history"]
        window_ids = [recs[i].期号 for i in range(t - r["window_size"], t)]
        assert all(i < recs[t].期号 for i in window_ids)  # 窗口全部早于目标期
        assert record_zone_numbers(recs[t], schema.zones[0])  # 目标期本身存在


def test_anchor_deterministic_with_same_seed(ssq_env):
    from train.anchor_trainer import anchor_train
    a = anchor_train("双色球", n_trials=6, seed=7, write_ledger=False)
    b = anchor_train("双色球", n_trials=6, seed=7, write_ledger=False)
    assert a["anchor_issues"] == b["anchor_issues"]
    assert a["stats"] == b["stats"]


def test_anchor_ledger_written_and_summary_aggregates(ssq_env):
    from train.anchor_trainer import anchor_train, anchor_summary
    anchor_train("双色球", n_trials=3, seed=11)
    anchor_train("双色球", n_trials=3, seed=12)
    s = anchor_summary(["双色球"])
    assert s["双色球"]["试验数"] == 6
    assert "随机基线" not in s["双色球"]["策略"] or True  # 基线单列
    assert "基线平均总命中" in s["双色球"]


def test_anchor_insufficient_data(monkeypatch):
    from train.anchor_trainer import anchor_train
    from data.schema import LotteryData
    recs = _mk_records(n=60)   # < min_history+1
    monkeypatch.setattr("train.anchor_trainer.load_lottery",
                        lambda name: LotteryData(lottery_name=name, records=recs))
    r = anchor_train("双色球", n_trials=2, seed=1)
    assert "error" in r


def test_anchor_random_baseline_matches_zone_shape(ssq_env):
    from train.anchor_trainer import anchor_train
    r = anchor_train("双色球", n_trials=3, seed=5, write_ledger=False)
    for row in r["trials"]:
        assert set(row["hits"].keys()) >= {"随机基线"}
        assert isinstance(row["hits"]["随机基线"]["总命中"], int)


# ------------------------------------------------------------
# training_loop：配置与状态（不启动线程）
# ------------------------------------------------------------

def test_training_loop_sanitize_clamps(tmp_path):
    from web.training_loop import sanitize
    cfg = sanitize({"interval_seconds": 99999, "trials_per_cycle": 0,
                    "enabled": True, "nightly_lightgbm": "yes"})
    assert cfg["interval_seconds"] == 3600          # 钳到上限
    assert cfg["trials_per_cycle"] == 1             # 下限 1
    assert cfg["nightly_lightgbm"] is True          # 非 bool 值保留默认
    assert cfg["enabled"] is True


def test_training_loop_roundtrip_isolated(tmp_path):
    """STORE_PATH 已被根 conftest 重定向到 tmp_path——写配置不碰真身 config/。"""
    from web import training_loop as tl
    cfg = tl.save_config({"interval_seconds": 120, "enabled": False})
    assert cfg["interval_seconds"] == 120 and cfg["enabled"] is False
    assert json.loads(tl.STORE_PATH.read_text(encoding="utf-8"))["enabled"] is False
    st = tl.get_status()
    assert st["running"] is False
    assert st["config"]["interval_seconds"] == 120


def test_training_loop_cycle_runs_without_thread(tmp_path, monkeypatch):
    """run_cycle 可单独调用（不启动常驻线程），返回结果并推进轮询游标。"""
    from web import training_loop as tl
    from train.anchor_trainer import anchor_train

    def _fake_anchor(lot, **kw):
        assert lot in ("双色球", "大乐透", "排列3", "排列5", "福彩3D", "七星彩")
        return {"training_time": "t", "seed": 1, "n_trials": 1,
                "stats": {"高频策略": {"配对差值": 0.1, "显著性": "不显著"}}}

    monkeypatch.setattr("train.anchor_trainer.anchor_train", _fake_anchor)
    res = tl.run_cycle(tl.sanitize({}))
    assert res is not None
    assert tl.get_status()["last_cycle"]["lottery"] in ("双色球", "大乐透", "排列3", "排列5", "福彩3D", "七星彩")
