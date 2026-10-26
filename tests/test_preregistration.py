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
    assert doc["版本"] == "1.7"
    assert doc["锁定日"] == "2026-10-26"
    # 集中度守门必须有**两级**：单臂大奖占比 + 配对级单期支配
    rules = "".join(doc["停止规则"])
    assert "单次大奖占总实得比" in rules
    assert "配对级" in rules and "贡献的 Δ 占比" in rules
    # v1.5：≥四等奖 前瞻终点块必须在（契约件，不是可选装饰）
    assert "前瞻终点_四等奖" in doc
    # v1.6：一二三等奖 前瞻终点块必须在
    assert "前瞻终点_一二三等奖" in doc
    # v1.7：双轨组合优化块必须在
    assert "双轨_组合优化" in doc


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
    # 变更日志里该条必须自证「发布时已观测过历史窗 + 不覆盖」
    # ★ 同样搜全部条目：v1.6 追加后末条是 v1.6，盯末条会让 v1.5 的契约静默脱检
    entry = next(e for e in doc["变更日志"] if "v1.5" in e and "四等" in e)
    assert "已经看过" in entry and "不覆盖" in entry and "探索性" in entry


def test_v16_forward_top3_registration():
    """v1.6：一二三等奖 只做**前瞻**登记 —— 确认力不覆盖历史窗。"""
    doc = PR.load()
    blk = doc["前瞻终点_一二三等奖"]
    assert blk["彩种"] == ["双色球", "大乐透"]
    assert blk["K"] == 200 and blk["预算元每期"] == 400
    # 三个 grade 型目标，≥语义（≥一等=仅一等；≥二等=含一等；≥三等=含一二等）
    assert blk["目标"] == [{"kind": "grade", "grade": "一等"},
                           {"kind": "grade", "grade": "二等"},
                           {"kind": "grade", "grade": "三等"}]
    assert blk["预期判决"] == "不可判"
    # 双口径都必须报；★ 本目标下 periodic 与 current 的门本身就不同（≥三等 三套条件集）
    mode = blk["常量"]["规则口径"]
    assert "current" in mode and "periodic" in mode
    assert blk["常量"]["期窗"].startswith("前瞻")
    # 前瞻块同样不进 Stage 1 的 `格`
    for c in doc["格"]:
        assert c.get("目标", {}).get("grade") not in ("一等", "二等", "三等")
    assert sum(1 for c in doc["格"] if c.get("主假设")) == 1
    # 变更日志必须自证：发布时未看过该目标的独立读数、但看过金额桶、不覆盖历史窗
    entry = next(e for e in doc["变更日志"] if "v1.6" in e)
    assert "尚未产生" in entry and "已经看过" in entry and "不覆盖" in entry
    # 预期依据必须写进块内（不许只活在 changelog 里）
    assert "期望命中" in blk["_预期依据"]


def test_v17_dual_track_registration():
    """v1.7：双轨组合优化 —— 轨道 A 四格、轨道 B 情景网格、倍投末层。"""
    doc = PR.load()
    blk = doc["双轨_组合优化"]
    a, b, c = blk["轨道A"], blk["轨道B"], blk["倍投层"]

    # ---- 轨道 A：4 格 = 2 彩种 × (≥四等, ≥三等)
    cells = {(g["彩种"], g["目标"]["grade"]): g for g in a["格"]}
    assert set(cells) == {("双色球", "四等"), ("双色球", "三等"),
                          ("大乐透", "四等"), ("大乐透", "三等")}
    assert {g["彩种"]: (g["K"], g["预算元每期"]) for g in a["格"]} == {
        "双色球": (200, 400), "大乐透": (38, 76)}
    # ★ 大乐透 38 注是新预算（76 元），与 Stage 1 的 K=200/400 元描述性格不可比
    assert any(g["彩种"] == "大乐透" and g["K"] == 38 for g in a["格"])

    # ---- 臂：新臂必须进主检验；prod_mix 因 cap≠3 必须**不**进
    assert "tier_packing" in a["臂"] and "tier_packing" in a["进主检验"]
    assert "union_greedy" in a["进主检验"] and "tier_coverage" in a["进主检验"]
    assert "prod_mix" not in a["进主检验"]
    assert a["对比臂"] == "random"
    # 四项指标：命中期比例 / 达标注数期望 / 每百元达标期数 / 精确 P
    ind = "".join(a["指标四项"])
    for kw in ("命中期比例", "达标注数期望", "每百元达标期数", "精确 P"):
        assert kw in ind

    # ---- 评估期纪律：判决只能用未参与调参的那一段
    assert "开发窗" in a["评估期"] and "评估窗" in a["评估期"]
    assert "60" in a["评估期"]["开发窗"] and "40" in a["评估期"]["评估窗"]
    assert "唯一判决窗口" in a["评估期"]["评估窗"]
    assert "BH" in a["验收判据"] and "n*_保守" in a["验收判据"]
    assert "评估窗" in a["验收判据"]
    # ★ 层 1 不得替代层 2：验收判据必须显式否掉这种替代
    assert "不能" in a["验收判据"] or "不得" in a["验收判据"]

    # ---- 分层披露：两层确认力必须在块内自证
    assert "已经看过" in blk["_登记性质"] and "尚未产生" in blk["_登记性质"]
    assert "已看过" in a["_层1披露"]

    # ---- 层 1 的核心事实：K·p 必须 <1（这正是可争取空间塌缩的根源）
    for k, v in a["层1_构造性上界"].items():
        if k.startswith("_"):
            continue
        assert v["K·p"] < 1.0, f"{k} 的 K·p={v['K·p']} ≥ 1，覆盖空间不再是 O((K·p)²) 量级"
        # 上限不得低于随机基线（否则恒等式被改坏）
        assert v["理论上限"] >= v["随机独立基线"] - 1e-9

    # ---- 轨道 B：情景网格 + 三条判据 + 预期不参与
    assert 0.0 in b["情景网格"]["κ档"]
    assert max(b["情景网格"]["κ档"]) > b["分摊模型"]["κ_实测上界"]   # 必须探到不可测区间
    assert len(b["情景网格"]["奖池预测误差"]) >= 3
    assert set(b["情景网格"]["规则版本"]) == {"current", "periodic"}
    assert len(b["判据"]) == 3
    assert "不参与" in b["_判决"] and "不参与" in b["_预期"]
    # κ* ≫ κ_upper 是「不参与」的量化依据，不许只写结论
    assert b["分摊模型"]["κ*需求"] > b["分摊模型"]["κ_实测上界"]

    # ---- 倍投层：只在轨道 B 通过后启动，且必须固定票集同预算
    assert "可参与" in c["定位"]
    entry = "".join(c["准入判据"])
    assert "同一票集" in entry or "同一副票集" in entry
    assert "总预算" in entry and "相等" in entry

    # ---- 变更日志双披露
    cl = next(e for e in doc["变更日志"] if "v1.7" in e)
    assert "尚未产生" in cl and "已经实测过" in cl
    assert "不具确认力" in cl
    assert "层 1" in cl and "层 2" in cl


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
