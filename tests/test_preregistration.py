# -*- coding: utf-8 -*-
"""tests/test_preregistration.py —— 实验契约的三层锁之一：测试硬编码常量。

★ 这些字面量是**故意**写死的：改 `config/preregistration.json` 会先红测试，
  迫使改的人同时回答「为什么改」，而不是悄悄换一个更漂亮的格子。
"""
from __future__ import annotations

import copy
import json

import pytest

from ev import preregistration as PR


# ---------------------------------------------------------------- 装载
def test_load_ok():
    doc = PR.load()
    # ★ 版本号是硬编码的「锁」：改预注册必须同时改这里，避免静默换版本。
    assert doc["版本"] == "1.5"
    assert doc["锁定日"] == "2026-10-25"
    # 集中度守门必须有**两级**：单臂大奖占比 + 配对级单期支配
    rules = "".join(doc["停止规则"])
    assert "单次大奖占总实得比" in rules
    assert "配对级" in rules and "贡献的 Δ 占比" in rules
    # v1.5：≥四等奖 前瞻终点块必须在（契约件，不是可选装饰）
    assert "前瞻终点_四等奖" in doc


def test_hash_mismatch_raises(tmp_path):
    doc = PR.load()
    doc["版本"] = "9.9"                    # 改内容但不改哈希
    p = tmp_path / "bad.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PR.PreregistrationError, match="哈希不匹配"):
        PR.load(path=p)


def test_missing_file_raises(tmp_path):
    """★ 缺文件**不回退默认值**（对比 data/ticket_size.sanitize 的静默回落）。"""
    with pytest.raises(PR.PreregistrationError, match="不存在"):
        PR.load(path=tmp_path / "nope.json")


def test_missing_required_field_raises(tmp_path):
    doc = PR.load()
    doc.pop("停止规则")
    doc["哈希"] = PR.compute_hash(doc)
    p = tmp_path / "x.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PR.PreregistrationError, match="缺少必需字段"):
        PR.load(path=p)


# ---------------------------------------------------------------- 主格常量（硬编码）
def test_primary_cell_is_exactly_one():
    c = PR.primary_cell()
    assert c["彩种"] == "双色球"
    assert c["K"] == 200
    assert c["预算元每期"] == 400          # = 现生产预算（200 注 × 2 元）
    assert c["目标"]["min_payout"] == 10.0
    assert c["常量"]["规则口径"] == "current"
    assert c["常量"]["剔除规则版本"] == []


def test_only_one_primary_cell():
    assert sum(1 for c in PR.cells() if c.get("主假设")) == 1


def test_two_primaries_raise(tmp_path):
    doc = PR.load()
    for c in doc["格"][:2]:
        c["主假设"] = True
    doc["哈希"] = PR.compute_hash(doc)
    p = tmp_path / "y.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PR.PreregistrationError, match="恰好"):
        PR.primary_cell(doc)


# ---------------------------------------------------------------- 常量
def test_constants():
    c = PR.constants()
    assert c["候选池规模"] == 8000
    assert c["重叠上限"] == 3             # ★ 生产口径，不是 _OVERLAP_CAP['双色球']=4
    assert c["单注成本元"] == 2.0
    assert c["MC评估采样数"] == c["MC拟合采样数"] == 100000
    assert c["评估seed偏移"] != 0         # 优化与评估必须独立流


def test_baseline_and_arms():
    doc = PR.load()
    assert doc["基线臂"] == "random"
    names = {a["名"] for a in doc["臂"]}
    assert names == {"random", "tier_coverage", "union_greedy", "lowcrowd",
                     "prod_mix", "hybrid"}
    # prod_mix 内部 cap=4 ≠ 3 ⇒ 不进主检验
    assert not next(a for a in doc["臂"] if a["名"] == "prod_mix")["进主检验"]
    # v1.4：hybrid（联合目标）进主检验 —— 与其余四臂并列进 BH 校正
    assert next(a for a in doc["臂"] if a["名"] == "hybrid")["进主检验"] is True
    main_names = {a["名"] for a in doc["臂"] if a.get("进主检验")}
    assert main_names == {"random", "tier_coverage", "union_greedy",
                          "lowcrowd", "hybrid"}      # BH 的 m = 4（基线臂 random 不参与）


def test_v14_changelog_registers_hybrid_before_running():
    """v1.4 必须是**事前登记**：变更日志里该条要写明 hybrid、跑之前、未看过结果。

    ★ 刻意搜全部条目而不是只看末条：v1.5 追加后末条是 v1.5，
      若这里还盯「最后一条」，v1.4 的契约会在下一次登记时静默脱检。
    """
    doc = PR.load()
    v14 = [e for e in doc["变更日志"] if "v1.4" in e and "hybrid" in e]
    assert len(v14) == 1
    assert "跑之前" in v14[0]


def test_v15_forward_tier4_registration():
    """v1.5：≥四等奖 只做**前瞻**登记 —— 确认力不覆盖历史窗。"""
    doc = PR.load()
    blk = doc["前瞻终点_四等奖"]
    assert blk["彩种"] == ["双色球", "大乐透"]
    assert blk["K"] == 200 and blk["预算元每期"] == 400
    assert blk["目标"] == {"kind": "grade", "grade": "四等"}
    assert blk["预期判决"] == "不可判"
    # 双口径都必须报（大乐透五版本下 ≥四等 条件集有三套，单口径会造出假等价）
    mode = blk["常量"]["规则口径"]
    assert "current" in mode and "periodic" in mode
    # ★ 牙齿：期窗字段必须自证「只前瞻」——回溯登记在这里无处藏身
    assert blk["常量"]["期窗"].startswith("前瞻")
    # 前瞻块不进 Stage 1 的 `格`（主假设唯一性契约不受影响）
    assert all(c.get("目标") != {"kind": "grade", "grade": "四等"} for c in doc["格"])
    assert sum(1 for c in doc["格"] if c.get("主假设")) == 1
    # 变更日志末条必须自证「发布时已观测过历史窗 + 不覆盖」
    entry = doc["变更日志"][-1]
    assert "v1.5" in entry and "四等" in entry
    assert "已经看过" in entry and "不覆盖" in entry and "探索性" in entry


# ---------------------------------------------------------------- 实跑对齐
def test_assert_run_matches():
    c = PR.primary_cell()
    PR.assert_run_matches({"期窗": "全量", "规则口径": "current"}, c)


def test_assert_run_matches_raises_on_drift():
    c = PR.primary_cell()
    with pytest.raises(PR.PreregistrationError, match="不符"):
        PR.assert_run_matches({"期窗": "全量", "规则口径": "periodic"}, c)


# ---------------------------------------------------------------- Stage 2（单次登记）
def test_stage2_is_registered():
    """Stage 2 必须**已登记**才能产生结论。这一条是 flow 锁，不是数值锁。"""
    doc = PR.load()
    assert "格_Stage2" in doc, "Stage 2 未登记 ⇒ 不得宣称任何 Stage 2 结论"
    s2 = PR.stage2(doc)
    assert s2["彩种"] == "双色球" and s2["K"] == 200
    assert s2["目标"] == {"kind": "payout", "min_payout": 10.0}
    # 方案网格：all_single + 2 个加倍目标 × 3 个比值
    grid = s2["方案网格"]
    assert "all_single" in grid
    assert len([k for k in grid if k.startswith(("few_double", "incremental_coverage"))]) == 6
    # monetary_threshold 在 κ=0 下退化为 all_single ⇒ 显式排除，理由必须留在文件里
    assert "monetary_threshold" not in grid
    assert "monetary_threshold" in s2["_排除项"]


def test_stage2_missing_raises_not_silent(tmp_path):
    """未登记时必须**抛**，不许回退成空规格（否则 runner 照样能跑出数字）。"""
    doc = PR.load()
    doc.pop("格_Stage2")
    doc["哈希"] = PR.compute_hash(doc)
    p = tmp_path / "no_s2.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(PR.PreregistrationError, match="尚未登记"):
        PR.stage2_cells(doc)


def test_stage2_does_not_break_stage1_primary_uniqueness():
    """Stage 2 单列 `格_Stage2` 的原因：保住 Stage 1「主假设恰好 1 个」的契约。"""
    assert sum(1 for c in PR.cells() if c.get("主假设")) == 1
    assert not PR.stage2().get("主假设"), "Stage 2 不设主假设：它量的是代价，不是候选策略"
