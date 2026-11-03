# -*- coding: utf-8 -*-
"""loader 泛型分支：一个区读多个号（七乐彩 7+1、快乐8 20）。

★ 原实现每个 zone 只读**一个** `号码{k}` 单值列，所以七乐彩的 7 个基本号和
  快乐8 的 20 个开奖号都读不进来。改造后按 `zone.draw_count` 累计列偏移；
  既有 6 彩种 draw_count 恒为 1，行为不变（由 test_schema_draw_zones 锁）。
"""
from __future__ import annotations

import pandas as pd
import pytest

from config import LOTTERY_CONFIG
from data import loader as L
from data import schema as S


@pytest.fixture()
def tmp_lottery(tmp_path, monkeypatch):
    """造一个走泛型分支的临时彩种，并把数据目录指向 tmp。"""
    monkeypatch.setattr(L, "LOTTERY_DATA_DIR", tmp_path)
    monkeypatch.setattr(L, "CLEANED_FILE_PATHS", {})
    S._SCHEMA_CACHE.clear()
    yield tmp_path
    S._SCHEMA_CACHE.clear()
    L.invalidate_lottery_cache()


def _write(tmp_path, name, df):
    p = tmp_path / f"{name}历史数据.csv"
    df.to_csv(p, index=False, encoding="utf-8-sig")
    return p


def test_loads_seven_basic_plus_one_special(tmp_lottery, monkeypatch):
    monkeypatch.setitem(LOTTERY_CONFIG, "测七乐彩", {
        "zones": [
            {"name": "基本号", "min": 1, "max": 30, "choose": 7},
            {"name": "特别号", "min": 1, "max": 30, "choose": 0, "draw": 1, "draw_only": True},
        ],
        "payout": {"cost": 2},
    })
    _write(tmp_lottery, "测七乐彩", pd.DataFrame([
        {"期号": 2026001, "开奖日期": "2026-01-02",
         **{f"号码{i}": v for i, v in enumerate([2, 5, 10, 15, 22, 23, 28, 3], 1)}},
    ]))
    d = L.load_lottery("测七乐彩")
    assert len(d.records) == 1
    r = d.records[0]
    assert r.zone_numbers["基本号"] == [2, 5, 10, 15, 22, 23, 28]
    assert r.zone_numbers["特别号"] == [3]


def test_loads_twenty_draw_numbers(tmp_lottery, monkeypatch):
    monkeypatch.setitem(LOTTERY_CONFIG, "测快乐8", {
        "zones": [{"name": "基本号", "min": 1, "max": 80, "choose": 10, "draw": 20}],
        "plays": {"选十": 10},
        "default_play": "选十",
        "payout": {"cost": 2},
    })
    nums = list(range(1, 21))
    _write(tmp_lottery, "测快乐8", pd.DataFrame([
        {"期号": 2026001, "开奖日期": "2026-01-02",
         **{f"号码{i}": v for i, v in enumerate(nums, 1)}},
    ]))
    d = L.load_lottery("测快乐8")
    assert d.records[0].zone_numbers["基本号"] == nums


def test_short_row_warns_but_still_loads(tmp_lottery, monkeypatch, caplog):
    """少几个号不能让整批静默通过 —— 必须留痕。

    少 3 个号会让命中数偏低，而「命中偏低」与「这期真的没中」在下游完全同形。
    """
    monkeypatch.setitem(LOTTERY_CONFIG, "测短号", {
        "zones": [{"name": "基本号", "min": 1, "max": 80, "choose": 10, "draw": 20}],
        "payout": {"cost": 2},
    })
    row = {"期号": 2026001, "开奖日期": "2026-01-02"}
    row.update({f"号码{i}": v for i, v in enumerate(range(1, 18), 1)})   # 只有 17 个
    _write(tmp_lottery, "测短号", pd.DataFrame([row]))

    with caplog.at_level("WARNING"):
        d = L.load_lottery("测短号")
    assert len(d.records[0].zone_numbers["基本号"]) == 17
    assert any("应有 20 个号" in m for m in caplog.messages)


def test_numeric_lottery_still_reads_one_number_per_zone():
    """既有数字型（5 个单值区）读取结果不能变。

    这条不挂 tmp_lottery：它要读**真实**的 lottery_data/排列5历史数据.csv，
    验证泛型分支在 draw_count 恒为 1 时行为与改造前一致。
    """
    d = L.load_lottery("排列5")
    r = d.records[0]
    assert len(r.zone_numbers) == 5
    assert all(len(v) == 1 for v in r.zone_numbers.values())
