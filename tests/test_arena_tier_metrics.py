# -*- coding: utf-8 -*-
"""tests/test_arena_tier_metrics.py —— 多门指标必须与 replay 逐位一致，且分版本。

三条锁（对应计划 P0-3）
-----------------------
① **一致性**：`gate_series("≥10元")` 的每期达标 必须逐位等于 `replay().hit`。
   多门指标是「在 replay 已算好的 (h, j) 上换个查表」，换错一格就是另一回事。
② **分版本**：大乐透混合窗（2019 + 2026）必须出现 **两个** `rule_version` 桶，
   且「四等」的条件集不同 —— 2019 没有 `(4,1)`，2026 有。
   这条挡的是「复用一份 conditions 跨版本套用」，它不报错、只让所有数偏。
③ **硬上界**：`期占比 ≤ min(1, K·p) + 3·SE`。`min(1,K·p)` 是 Bonferroni 上界
   （`P(∪Aᵢ) ≤ ΣP(Aᵢ)`），对同一批票逐位成立 ⇒ 越界就是实现缺陷，不是噪声。
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from ev.arena import replay as RP
from ev.arena import tier_metrics as TM
from ev.coverage_backtest import build_pool
from ev.tier_backtest import load_test_recs

K = 60


# ---------------------------------------------------------------- ① 一致性
def test_series_matches_replay_single_version():
    """双色球只有 1 个规则版本 ⇒ 两口径等价，且必须等于 replay 的 hit。"""
    pool = build_pool("双色球", K, seed=20261030)
    recs = load_test_recs("双色球")[-30:]
    arm = RP.replay("双色球", pool, recs, {"kind": "payout", "min_payout": 10.0},
                    rule_mode="periodic", arm="t", with_money=False)

    s = TM.gate_series("双色球", arm.h, arm.j, recs, "≥10元", rule_mode="periodic")
    assert np.array_equal(s["_每期达标"], arm.hit), "多门指标与 replay 的达标序列不一致"
    assert s["期占比"] == pytest.approx(float(arm.hit.mean()))
    assert s["达标期数"] == int(arm.hit.sum())

    cur = TM.gate_series("双色球", arm.h, arm.j, recs, "≥10元", rule_mode="current")
    assert np.array_equal(cur["_每期达标"], arm.hit)
    assert cur["口径"] != s["口径"]


def test_block_covers_all_gates_on_same_tickets():
    pool = build_pool("双色球", K, seed=7)
    recs = load_test_recs("双色球")[-30:]
    arm = RP.replay("双色球", pool, recs, {"kind": "payout", "min_payout": 10.0},
                    rule_mode="periodic", arm="t", with_money=False)
    block = TM.gate_block("双色球", arm.h, arm.j, recs)

    assert set(block["门"]) == set(TM.GATE_ORDER)
    assert block["K"] == K
    # 门是单调的：三等 ≤ 四等 ≤ ≥10元（双色球 四等=200 元 > 10 元）
    r10 = block["门"]["≥10元"]["期占比"]
    r4 = block["门"]["≥四等"]["期占比"]
    r3 = block["门"]["≥三等"]["期占比"]
    assert r3 <= r4 <= r10 + 1e-12, f"门不单调：≥10元={r10} ≥四等={r4} ≥三等={r3}"


# ---------------------------------------------------------------- ② 分版本
def _mixed_dlt_window():
    """2019 与 2026 各取几期 —— 跨规则版本的**唯一**可靠构造方式。"""
    recs = load_test_recs("大乐透")
    old = [r for r in recs if str(r.期号).startswith("19")][-3:]
    new = [r for r in recs if str(r.期号).startswith("26")][-3:]
    if not old or not new:
        pytest.skip("大乐透数据缺 2019 或 2026 段，无法构造跨版本窗")
    return old + new


def test_mixed_window_splits_by_rule_version():
    recs = _mixed_dlt_window()
    pool = build_pool("大乐透", K, seed=20261030)
    arm = RP.replay("大乐透", pool, recs, {"kind": "grade", "grade": "四等"},
                    rule_mode="periodic", arm="t", with_money=False)

    s = TM.gate_series("大乐透", arm.h, arm.j, recs, "≥四等", rule_mode="periodic")
    vers = sorted(s["条件集"])
    assert len(vers) == 2, f"跨版本窗只切出 {len(vers)} 个规则版本：{vers}"

    old_v, new_v = vers[0], vers[-1]
    old_c = {tuple(c) for c in s["条件集"][old_v]}
    new_c = {tuple(c) for c in s["条件集"][new_v]}
    assert old_c != new_c, f"{old_v} 与 {new_v} 的「四等」条件集居然相同"
    # 记忆里的实测事实：2019 没有 (4,1)，2026 有
    assert (4, 1) not in old_c, f"{old_v} 不应含 (4,1)：{sorted(old_c)}"
    assert (4, 1) in new_c, f"{new_v} 应含 (4,1)：{sorted(new_c)}"
    # 分版本 p 必须不同（否则就是拿一版的 p 套了另一版的期）
    assert s["分版本"][old_v]["p"] != pytest.approx(s["分版本"][new_v]["p"])


def test_cross_version_replay_equals_current_gate():
    """`current` 口径下「当期达标」必须**逐位等于** replay 的 current 模式。"""
    recs = _mixed_dlt_window()
    pool = build_pool("大乐透", K, seed=99)
    target = {"kind": "grade", "grade": "四等"}
    arm = RP.replay("大乐透", pool, recs, target, rule_mode="current",
                    target_issue=recs[-1].期号, arm="t", with_money=False)
    s = TM.gate_series("大乐透", arm.h, arm.j, recs, "≥四等", rule_mode="current",
                       target_issue=recs[-1].期号)
    assert np.array_equal(s["_每期达标"], arm.hit)


# ---------------------------------------------------------------- ③ 硬上界
@pytest.mark.parametrize("lottery", ["双色球", "大乐透"])
def test_rate_never_exceeds_bonferroni_cap(lottery):
    pool = build_pool(lottery, K, seed=20261030)
    recs = load_test_recs(lottery)[-40:]
    arm = RP.replay(lottery, pool, recs, {"kind": "payout", "min_payout": 10.0},
                    rule_mode="periodic", arm="t", with_money=False)
    block = TM.gate_block(lottery, arm.h, arm.j, recs)

    for label, s in block["门"].items():
        cap = min(1.0, s["K·p"])
        assert s["期占比"] <= cap + 3 * s["单期SE"] + 1e-12, (
            f"{lottery} {label}：{s['期占比']:.6f} > 上界 {cap:.6f}")
        assert s["可争取空间"] >= -1e-12, f"{label} 可争取空间为负：{s['可争取空间']}"
        assert s["随机基线"] <= cap + 1e-12
    assert TM.sanity_violations(block) == []


def test_sanity_violations_catches_a_broken_block():
    """人牙的牙：上界检查必须真的能报错，否则上面那条是假绿。"""
    broken = {"门": {"≥10元": {"期占比": 0.99, "K·p": 0.01, "单期SE": 0.001}}}
    bad = TM.sanity_violations(broken)
    assert bad and "硬上界" in bad[0]


# ---------------------------------------------------------------- 契约与解析
def test_to_row_strips_arrays_and_is_jsonable():
    pool = build_pool("双色球", 20, seed=1)
    recs = load_test_recs("双色球")[-10:]
    arm = RP.replay("双色球", pool, recs, {"kind": "payout", "min_payout": 10.0},
                    rule_mode="periodic", arm="t", with_money=False)
    row = TM.to_row(TM.gate_block("双色球", arm.h, arm.j, recs))
    json.dumps(row)                              # 不许有 ndarray / numpy 标量
    for label, s in row["门"].items():
        assert not [k for k in s if k.startswith("_")], f"{label} 泄漏了数组键"
        assert "期占比" in s and "K·p" in s


def test_gate_target_parsing():
    assert TM.gate_target("≥10元") == {"kind": "payout", "min_payout": 10.0}
    assert TM.gate_target("≥四等") == {"kind": "grade", "grade": "四等"}
    assert TM.gate_target("≥4等") == {"kind": "grade", "grade": "四等"}
    assert TM.gate_target("≥四等奖") == {"kind": "grade", "grade": "四等"}
    raw = {"kind": "grade", "grade": "三等"}
    assert TM.gate_target(raw) == raw and TM.gate_target(raw) is not raw
    with pytest.raises(ValueError):
        TM.gate_target("随便一个门")


def test_shape_mismatch_is_rejected():
    """h/j 与期数错位是这类扩展最容易犯的错，必须显式抛。"""
    recs = load_test_recs("双色球")[-10:]
    h = np.zeros((5, 9), dtype=np.int16)
    j = np.zeros((5, 9), dtype=np.int16)
    with pytest.raises(ValueError, match="列数"):
        TM.gate_series("双色球", h, j, recs, "≥10元")
    with pytest.raises(ValueError, match="K="):
        TM.gate_series("双色球", np.zeros((5, 10), np.int16),
                       np.zeros((5, 10), np.int16), recs, "≥10元", K=4)


# ---------------------------------------------------------------- 口径分离（防混用）
def test_period_total_gate_is_not_the_per_ticket_gate():
    """★★ 两个「≥10元」是不同的量：禁止拿 `money_series` 的那一栏当覆盖证据。

    `money_series["≥10元期占比"]` 的分母是**该期全部票实得之和**，
    因此
      ① 恒有 `money ≥ 逐票期占比`（任一注 ≥10 ⇒ 该期总和 ≥10，包含关系）；
      ② 它随 K **机械上升**，K=50（100 元/期）就已能到 1.0，而此时
         逐票口径只有三成多 —— 用它比较覆盖效应必然得出「越大越好」的假结论。

    这条测试锁的是②：同一个 K 上，两个数**必须显著不同**。
    如果哪天有人把它们合并成一个字段，这个断言会立刻红。
    """
    recs = load_test_recs("双色球")
    pool = build_pool("双色球", 60, seed=7)
    arm = RP.replay("双色球", pool, recs, {"kind": "payout", "min_payout": 10.0},
                    rule_mode="periodic", arm="t", with_money=True)
    money = arm.money
    assert money and "≥10元期占比" in money

    g = TM.gate_series("双色球", arm.h, arm.j, recs, "≥10元", K=len(pool))

    # ① 包含关系恒成立
    assert money["≥10元期占比"] >= g["期占比"] - 1e-12

    # ② 同一批票、同一个 K，两个数不是同一个量
    #    （K 足够大时金额侧必然远高于逐票侧；小样本下也不该相等）
    assert money["≥10元期占比"] > g["期占比"] + 0.10, (
        f"两个口径居然接近相等（金额侧 {money['≥10元期占比']:.4f} vs "
        f"逐票侧 {g['期占比']:.4f}）⇒ 先确认 money_series 是不是被误改成了逐票口径")

    # ③ 逐票口径有解析上界，金额侧没有 ⇒ 金额侧可以越过 min(1,K·p) 而无意义
    assert g["期占比"] <= min(1.0, g["K·p"]) + 3 * g["单期SE"] + 1e-12
