# -*- coding: utf-8 -*-
"""tests/test_arena_hj_passthrough.py —— `ArmResult` 必须透出命中矩阵 `(h, j)`。

为什么需要这条锁
----------------
多门指标（同一批票上同时算 ≥10元 / ≥四等 / ≥三等）原本必须**为每一门各跑一遍
`replay`** 才能拿到命中矩阵 —— 命中矩阵重算 3 倍时间，而且三门的票集本来就
不一样（不同目标选出不同并集），所以也没法用缓存替代。

`replay` 内部算完 `h`、`j` 后原本直接丢弃。透出的边际成本为 0，
但**必须保证两件事**，否则下游会在错误的数据上算出漂亮的数：

  ① 透出的 `(h, j)` 与 `hit` 是同一批数（用 `gate[h, j]` 逐位重建 `hit` 验证）；
  ② 它们**不进** `to_row()` / JSON 契约（MB 级数组塞进报告会撑爆并破坏契约）。

★ 第 ① 条是这类「顺手多返回一个中间量」改动最容易出错的地方：
  改了 `h` 的 dtype 或错位一列，`hit` 看着完全正常，只有下游多门指标悄悄偏。
"""
from __future__ import annotations

import json

import numpy as np

from ev.arena import replay as RP
from ev.arena.design import outcome_gate
from ev.arena.types import ArmResult, arm_result_from
from ev.coverage_backtest import build_pool
from ev.tier_backtest import load_test_recs

TARGET10 = {"kind": "payout", "min_payout": 10.0}
PERIODS = 40
K = 60


def _fixture(lottery="双色球"):
    pool = build_pool(lottery, K, seed=20261030)
    recs = load_test_recs(lottery)[-PERIODS:]
    return lottery, pool, recs


# ---------------------------------------------------------------- ① 形状与语义
def test_replay_exposes_hj_matrices():
    lottery, pool, recs = _fixture()
    arm = RP.replay(lottery, pool, recs, TARGET10, rule_mode="current", arm="t")

    assert arm.h is not None and arm.j is not None, "replay 没有透出 (h, j)"
    assert arm.h.shape == (len(pool), len(recs))
    assert arm.j.shape == (len(pool), len(recs))
    assert arm.h.dtype == np.int16 and arm.j.dtype == np.int16
    # 命中数不可能超过该区要求的个数
    assert int(arm.h.max()) <= 6 and int(arm.h.min()) >= 0
    assert int(arm.j.max()) <= 1 and int(arm.j.min()) >= 0


def test_hj_rebuilds_hit_exactly():
    """`gate[h, j].any(axis=0)` 必须逐位等于 `arm.hit` —— 同一批数的两种读法。"""
    lottery, pool, recs = _fixture()
    arm = RP.replay(lottery, pool, recs, TARGET10, rule_mode="current", arm="t")
    gate, _w, _m, _n = outcome_gate(lottery, TARGET10, recs[-1].期号)

    rebuilt_hit = gate[arm.h, arm.j].any(axis=0)
    rebuilt_nhit = gate[arm.h, arm.j].sum(axis=0)
    assert np.array_equal(rebuilt_hit, arm.hit), "透出的 (h, j) 与 hit 不是同一批数"
    assert np.array_equal(rebuilt_nhit, arm.nhit), "透出的 (h, j) 与 nhit 不一致"


def test_hj_available_without_money_series():
    """`with_money=False`（只跑命中侧）也必须给 (h, j) —— 多门指标不依赖净收益。"""
    lottery, pool, recs = _fixture()
    arm = RP.replay(lottery, pool, recs, TARGET10, rule_mode="current",
                    arm="t", with_money=False)
    assert arm.h is not None and arm.j is not None
    assert arm.money == {}


# ---------------------------------------------------------------- ② 契约不变
def test_row_contract_does_not_leak_arrays():
    """`to_row()` 是进 JSON/报告的标量契约，不许混进 (K, n) 矩阵。"""
    lottery, pool, recs = _fixture()
    arm = RP.replay(lottery, pool, recs, TARGET10, rule_mode="current", arm="t")
    row = arm.to_row()

    for bad in ("h", "j", "hit", "nhit", "payout", "net"):
        assert bad not in row, f"to_row() 泄漏了数组字段 {bad}"
    for v in row.values():
        assert not isinstance(v, np.ndarray), f"to_row() 含 ndarray：{type(v)}"
    json.dumps(row)                     # 不许有 numpy 标量导致的不可序列化


def test_legacy_construction_still_works():
    """不给 (h, j) 也必须能构造 —— 老调用点（例如只看命中率的脚本）不受影响。"""
    arm = ArmResult(臂="x", 彩种="双色球", issues=[1, 2],
                    hit=np.array([True, False]), nhit=np.array([1, 0]))
    assert arm.h is None and arm.j is None
    assert arm.to_row()["达标期数"] == 1


def test_arm_result_from_passes_hj_through():
    hit = np.array([True, False])
    nhit = np.array([2, 0])
    h = np.array([[6, 3], [1, 0]])
    j = np.array([[1, 0], [0, 0]])
    arm = arm_result_from("y", "双色球", [1, 2], hit, nhit,
                          nbet_per_period=[5, 5], h=h, j=j)
    assert np.array_equal(arm.h, h.astype(np.int16))
    assert np.array_equal(arm.j, j.astype(np.int16))
    # 不传时仍是 None，不静默造零矩阵（零矩阵会被下游读成「一张票都没中」）
    arm2 = arm_result_from("z", "双色球", [1, 2], hit, nhit, nbet_per_period=[5, 5])
    assert arm2.h is None and arm2.j is None
