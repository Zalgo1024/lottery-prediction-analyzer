"""滚动回测（train/rolling_trainer.py）回归测试

重点锁定 2026-09-08 修复的 bug：
通用策略（高频/遗漏值/区间均衡）返回 {"zones": {...}}，而 _evaluate_hit 的红蓝分支
只读 "红球"/"蓝球" 键 → 这三个策略的红/蓝命中被算成恒 0，回测报告系统性失真
（只有返回 {"红球":…, "蓝球":…} 的「规则优选」数值正常，一度看起来"最优"）。
"""

import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from config import LOTTERY_CONFIG  # noqa: E402
from data.loader import load_lottery  # noqa: E402
from data.schema import schema_from_cfg  # noqa: E402
from train.rolling_trainer import (  # noqa: E402
    _evaluate_hit,
    _strategy_balanced,
    _strategy_high_freq,
    _strategy_missing_value,
)

GENERIC_STRATEGIES = [_strategy_high_freq, _strategy_missing_value, _strategy_balanced]


def _mean_hits(lottery_name: str, strategy_fn, periods: int = 40, window: int = 50) -> float:
    """在最近 periods 期上滚动，返回平均总命中"""
    data = load_lottery(lottery_name)
    cfg = LOTTERY_CONFIG[lottery_name]
    schema = schema_from_cfg(cfg, lottery_name)
    records = data.records  # 新→旧
    total = 0
    for i in range(periods):
        window_recs = records[i + 1 : i + 1 + window]
        actual = records[i]
        if len(window_recs) < window:
            break
        pred = strategy_fn(window_recs, cfg, schema)
        hit = _evaluate_hit(pred, actual, schema, True, lottery_name)
        total += hit["总命中"]
    return total / periods


@pytest.mark.parametrize("strategy_fn", GENERIC_STRATEGIES, ids=lambda f: f.__name__)
def test_generic_strategy_hits_are_not_zero(strategy_fn):
    """红蓝彩种：通用策略的平均命中必须显著大于 0（修复前恒为 0）"""
    mean = _mean_hits("双色球", strategy_fn)
    # 随机基线 ≈ 1.153（红 6²/33 + 蓝 1²/16），40 期均值不应低于 0.5
    assert mean > 0.5, f"{strategy_fn.__name__} 平均命中仅 {mean:.3f}，疑似 zones 结构未被读取"


def test_evaluate_hit_accepts_both_shapes():
    """_evaluate_hit 必须同时接受 zones 结构与 红球/蓝球 结构"""
    data = load_lottery("双色球")
    cfg = LOTTERY_CONFIG["双色球"]
    schema = schema_from_cfg(cfg, "双色球")
    actual = data.records[0]
    window = data.records[1:51]

    zones_pred = _strategy_high_freq(window, cfg, schema)  # {"zones": {...}}
    hit_zones = _evaluate_hit(zones_pred, actual, schema, True, "双色球")

    red = sorted(zones_pred["zones"]["红球"])
    blue = sorted(zones_pred["zones"]["蓝球"])
    hit_flat = _evaluate_hit({"红球": red, "蓝球": blue, "策略": "高频策略"},
                             actual, schema, True, "双色球")

    assert hit_zones["红球命中"] == hit_flat["红球命中"]
    assert hit_zones["蓝球命中"] == hit_flat["蓝球命中"]
    assert hit_zones["总命中"] == hit_zones["红球命中"] + hit_zones["蓝球命中"]
