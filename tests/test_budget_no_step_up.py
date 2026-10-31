# -*- coding: utf-8 -*-
"""tests/test_budget_no_step_up.py —— 「自动路径永不加注」的静态锁（任务 2）。

为什么需要一整份文件锁这一件事
------------------------------
2026-10-24 之前，动态策略里有一条「命中率偏离 → 回补 +2 注」。它让双色球的注数
单向爬到上限，用户手动调小后下一期又被推回，表现为「**怎么调都固定在最大值**」，
而日志一路正常、报表照长 —— 没有任何异常可抓。

那不是一个可以靠 review 防住的 bug，而是一条**规则本身错**：
`E[中奖注数] = K·p`，加注按比例放大注数，单注中奖概率恒定 ⇒ 命中率不会回升。
所以这里不测「算得对不对」，只测「**方向对不对**」：
  · 属性网格：任意 (彩种, 当前注数, 命中率, KPI, 批次数) 组合下 `next ≤ current`；
  · `锁定=true` 时自动路径不得改配置；
  · 零引用的上调常量必须已删（AST 扫描，防「死灰复燃」）；
  · 预算闸门接线后，`--groups 10000` 也不得突破单期上限。
"""
from __future__ import annotations

import ast
import itertools
import json
from pathlib import Path

import pytest

import config
import ev.budget_gate as BG
from data import ticket_size as TS

_REPO = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clear_cache():
    BG._LIMIT_CACHE.update({"key": None, "doc": None})
    yield


# ---------------------------------------------------------------- 属性网格
def test_next_never_exceeds_current_over_full_grid():
    kpis = [
        None,
        {"压缩前": 100, "质量淘汰": 10, "重复淘汰": 10, "重叠淘汰": 10},   # 低冗余
        {"压缩前": 100, "质量淘汰": 30, "重复淘汰": 20, "重叠淘汰": 20},   # 高冗余
        {"压缩前": 100, "质量淘汰": 60, "重复淘汰": 30, "重叠淘汰": 10},
    ]
    currents = [1, 5, 29, 38, 50, 100, 200, 400, 500, 1000]
    hits = [0.0, 0.01, 0.2, 0.9]
    batches = [1, 20, 500]
    for lot, cur, kpi, hr, nb in itertools.product(
            config.LOTTERY_CONFIG, currents, kpis, hits, batches):
        out = TS.decide_next_count(lot, current=cur, last_kpi=kpi,
                                   hit_rate=hr, expected_rate=0.05,
                                   hit_n=cur, n_batches=nb)
        assert out["next"] <= cur, (lot, cur, kpi, hr, nb, out)


def test_step_up_assertion_has_teeth():
    """牙齿：真出现上调时断言必须炸，否则上面那条网格是假绿。"""
    with pytest.raises(AssertionError, match="永远不加注|永不加注"):
        TS._assert_no_step_up(10, 12, "测试用的人造上调")


def test_high_hit_rate_does_not_raise_count():
    """命中率极低 + 批次数充足，也不许触发加注（这正是当年那条错规则）。"""
    out = TS.decide_next_count("双色球", current=50, last_kpi=None,
                               hit_rate=0.0, expected_rate=0.05,
                               hit_n=50, n_batches=500)
    assert out["next"] == 50
    assert out["诊断"] == ""          # 无 KPI 时 z 不计算，诊断只有样本不足/记录两种


# ---------------------------------------------------------------- 锁定
def test_locked_config_is_not_touched_by_auto():
    cfg = TS.sanitize(TS.load_config())
    if not cfg.get("锁定"):
        pytest.skip("本机配置未锁定，跳过（锁定态由 config/ticket_size.json 决定）")
    before = json.loads(json.dumps(cfg, ensure_ascii=False))
    after = TS.apply_dynamic_result("双色球", 999, "测试：自动路径尝试上调", force=False)
    assert after["dynamic"]["双色球"] == before["dynamic"]["双色球"]
    assert after["current"]["双色球"] == before["current"]["双色球"]


# ---------------------------------------------------------------- 死常量
def test_dead_step_up_constants_are_gone():
    tree = ast.parse((_REPO / "data" / "ticket_size.py").read_text(encoding="utf-8"))
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            tgts = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in tgts:
                if isinstance(t, ast.Name):
                    names.add(t.id)
    for dead in ("DYNAMIC_STEP_UP", "DYNAMIC_ANCHOR_FLOOR_RATIO"):
        assert dead not in names, (
            f"{dead} 是零引用的死常量（原「命中率偏离 → 上调」路径）。"
            f"★ 留着它迟早有人重新接上 —— 要恢复加注必须先改 tests/test_budget_no_step_up.py")


# ---------------------------------------------------------------- 闸门接线
def test_groups_10000_cannot_break_single_period_cap():
    BG.LIMIT_PATH.write_text(json.dumps(
        {"版本": 1, "启用": True, "月度总额": 100000, "单期上限": 200},
        ensure_ascii=False), encoding="utf-8")
    cap_tickets = int(200 // config.ticket_price("双色球"))
    got = config.resolve_groups("双色球", 10000, source="manual")
    assert got == cap_tickets


def test_corrupt_ledger_does_not_raise_and_stays_within_baseline():
    """台账写成非法 JSON：不许抛，且结果 ≤ 基线（fail-closed 而不是 fail-open）。"""
    BG.LIMIT_PATH.write_text(json.dumps(
        {"版本": 1, "启用": True, "月度总额": 5000, "单期上限": 5000},
        ensure_ascii=False), encoding="utf-8")
    BG.SPEND_PATH.write_text("{坏账", encoding="utf-8")
    base = config.resolve_groups("双色球")
    got = config.resolve_groups("双色球", 9999)
    assert isinstance(got, int) and 0 <= got <= max(base, 1)


def test_research_mode_never_touches_ledger():
    BG.LIMIT_PATH.write_text(json.dumps(
        {"版本": 1, "启用": True, "月度总额": 100, "单期上限": 100},
        ensure_ascii=False), encoding="utf-8")
    before = BG.spent_this_month()
    assert config.resolve_groups("双色球", 5000, source="research") == 5000
    assert BG.spent_this_month() == before
