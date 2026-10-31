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
    assert doc["版本"] == "1.10"
    assert doc["锁定日"] == "2026-10-30"
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
    # v1.8：三条用户指令各自的契约件都必须在（不是可选装饰）
    assert "预算约束_奖级目标" in doc
    assert "δ情景" in doc
    assert "预算包络" in doc
    # v1.9：≥四等 门二次评估（两条轨道都登记，且在跑数之前）
    assert "四等门_二次评估" in doc
    # v1.10：五档 K 网格（单位预算验证）
    assert "格_K网格" in doc


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


def test_v18_budget_targets_registration():
    """v1.8 指令三：奖级目标**按实际预算**重做 —— 6 格、BH m=18、层 1 否决力。"""
    doc = PR.load()
    blk = doc["预算约束_奖级目标"]

    # ---- 6 格 = 2 彩种 × 3 目标（≥10元 是**金额门**，不是奖级门）
    want = {("双色球", "四等"), ("双色球", "三等"), ("大乐透", "四等"),
            ("大乐透", "三等"), ("双色球", "payout"), ("大乐透", "payout")}
    got = {(g["彩种"], g["目标"].get("grade", g["目标"]["kind"])) for g in blk["格"]}
    assert got == want
    # 预算必须钉在生产实际值上：200 注 = 400 元/期、38 注 = 76 元/期
    assert {(g["彩种"], g["K"], g["预算元每期"]) for g in blk["格"]} == {
        ("双色球", 200, 400), ("大乐透", 38, 76)}
    # ★ 大乐透 K=38 是本条的核心：200 注的成绩不得套过来
    assert any("38" in g["名"] for g in blk["格"])

    # ---- 臂与判据
    assert blk["进主检验"] == ["tier_packing", "union_greedy", "tier_coverage"]
    assert "prod_mix" in blk["臂"] and "prod_mix" not in blk["进主检验"]
    assert blk["对比臂"] == "random"
    # BH 族规模 = 3 臂 × 6 格 = 18，必须写进判据而不是靠代码记住
    assert "18" in blk["验收判据"] and "BH" in blk["验收判据"]
    # 净收益必须**独立于**命中率单独报（用户明确要求）
    assert any("净收益" in x for x in blk["指标四项"])
    assert "开发窗" in blk["评估期"] and "唯一判决窗口" in blk["评估期"]["评估窗"]

    # ---- 层 1 否决力：前置登记，不是事后决定
    assert "否决" in blk["_层1否决力"] and "确认" in blk["_层1否决力"]
    # 「不具确认力」必须显式写出来，否则层 1 会被当成证据
    assert "已实测" in blk["_层1披露"] and "跑数之前" in blk["_层1披露"]
    assert "只能作上界" in blk["层1_构造性上界"]["_性质"]

    # ---- 层 1 数值自洽（这六个格子是「跑数前就能定判决」的算术依据）
    l1 = blk["层1_构造性上界"]["格"]
    assert set(l1) == {g["名"] for g in blk["格"]}
    kof = {g["名"]: g["K"] for g in blk["格"]}
    for name, v in l1.items():
        K = kof[name]
        # 理论上限 = min(1, K·p)
        assert v["理论上限"] == pytest.approx(min(1.0, K * v["单注p"]), abs=1e-9), name
        # 无放回抽样 ⇒ 独立基线 ≤ 随机臂精确 P ≤ 理论上限
        assert v["随机独立基线"] <= v["随机臂精确P"] + 1e-12, name
        assert v["随机臂精确P"] <= v["理论上限"] + 1e-12, name
        # 可争取空间 = 上限 − 随机臂精确 P（这是「优化器能用武之地」的全部）
        assert v["可争取空间"] == pytest.approx(
            v["理论上限"] - v["随机臂精确P"], abs=1e-9), name
        assert v["可争取空间_相对"] == pytest.approx(
            v["可争取空间"] / v["理论上限"], abs=1e-6), name

    # ---- 判决在跑数前就被算术决定了：三格空间为 0 / 1e-6 量级
    for name, v in l1.items():
        if "≥三等" in name:
            assert v["可争取空间"] == 0.0, f"{name} 空间应为 0（随机已达上限）"
        if name == "大乐透-K38-≥四等":
            assert v["可争取空间"] < 1e-5, "38 注 ≥四等 的空间必须是 1e-6 量级"
    exp = blk["_按目标的预期"]
    for name in ("双色球-K200-≥三等", "大乐透-K38-≥三等", "大乐透-K38-≥四等"):
        assert ("未能识别" in exp[name]) or ("不可能" in exp[name]), name
    for name in ("双色球-K200-≥10元", "大乐透-K38-≥10元"):
        assert "未能识别" not in exp[name], f"{name} 是真有空间的目标，不许预先放弃"


def test_v18_delta_scenario_registration():
    """v1.8 指令二：δ 情景区间 + v1.7 口径错误的正式修订。"""
    doc = PR.load()
    blk = doc["δ情景"]
    # ★ 主口径必须写明 δ 只作用于一等；这是 v1.7 那个 +6.31 的来源被否掉的根因
    assert "只作用于一等" in "".join(blk.keys())
    assert "二等奖" in blk["_v1.7 的口径错误（本次修订的原因）"]
    assert "143" in blk["_v1.7 的口径错误（本次修订的原因）"]
    # 旧口径必须同时报，但标成模型外推（不得当实投依据）
    assert "模型外推" in blk["旧口径_δ作用于全部浮动奖"]
    # 网格必须是**区间**：δ 至少 5 档、奖池误差至少 3 档
    assert len(blk["δ情景网格"]["δ档"]) >= 5
    assert 0.0 in blk["δ情景网格"]["δ档"] and 1.0 in blk["δ情景网格"]["δ档"]
    assert len(blk["δ情景网格"]["奖池误差"]) >= 3
    # δ 是假设，不是已测规律 —— 这句话必须留在契约里
    assert "假设" in blk["δ情景网格"]["_说明"]
    # 保守情景 = δ=0 + 负的奖池误差 + 税后，三者叠加
    cons = blk["保守情景判据"]
    assert "δ=0" in cons and "30%" in cons and "税后" in cons
    # 结构性上限必须声明「与奖池规模无关」
    assert "与奖池规模无关" in "".join(blk.keys())


def test_v18_budget_envelope_registration():
    """v1.8 指令一：把「少亏」做成纪律 —— 定额/上限/亏完即停/默认不参与。"""
    doc = PR.load()
    blk = doc["预算包络"]
    rules = "".join(blk["三条硬纪律"])
    assert "倍投" in rules                 # ① 不追损
    assert "加预算" in rules               # ① 不因更「看好」而加
    assert "每月" in rules and "单期上限" in rules and "亏完即停" in rules
    assert "不参与" in rules               # ③ 可以输出不参与
    # 单向闸门：只降级不升级，这条是安全属性，必须显式
    rel = blk["_与既有模块的关系"]
    assert "只允许" in rel and "永远不允许" in rel
    assert "ev/budget_envelope.py" in blk["_定位"]


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


# ---------------------------------------------------------------- v1.9
def test_v19_fourth_prize_reassessment_registration():
    """v1.9：≥四等 门的两条轨道必须先把自由度钉死，再谈结论。"""
    b = PR.load()["四等门_二次评估"]
    t1 = b["轨道1_K扩展"]
    t2 = b["轨道2_月度合并"]
    # ① K 网格必须包含「K·p=1 的临界点」，否则判不了「跨 1 是不是真杠杆」
    assert 2251 in t1["K网格"]
    assert t1["重叠上限"] == 3
    assert "40" in str(t1["候选池口径"])
    # ② 月内上限必须是逐期上限的合并式，不是 13K 注的并集
    assert "min(1,K·p)" in t2["月内上限公式"]
    assert t2["期数每月"] == 13
    # ③ 层 1 数字自洽：上限 ≥ 随机基线，且空间 = 上限 − 基线
    for r in t2["格"]:
        assert r["月内理论上限"] >= r["月内随机基线"] - 1e-12, r["彩种"]
        assert r["月内可争取空间"] == pytest.approx(
            r["月内理论上限"] - r["月内随机基线"], abs=1e-12)
    # ④ 跨 1 所需注数必须与单注 p 自洽（ceiling(1/p)）
    for c in t1["跨1所需"]:
        assert c["K跨1所需"] == int(-(-1 // c["单注p"])), c["彩种"]
        assert c["K跨1所需"] * c["单注p"] >= 1.0


def test_v19_does_not_claim_confirmatory_power():
    """两条轨道都在层 1 内判决 ⇒ 不许出现「已证实/已证明」这类确认性措辞。"""
    b = PR.load()["四等门_二次评估"]
    blob = json.dumps(b, ensure_ascii=False)
    assert "不产生新的确认性声明" in blob
    for bad in ("证伪", "证否", "已证明", "不可预测"):
        assert bad not in blob, bad


# ---------------------------------------------------------------- v1.10
def test_v110_kgrid_registration():
    """v1.10：单位预算验证的五档 K 网格 —— 四臂、主终点、附加门纪律。"""
    b = PR.load()["格_K网格"]
    # ① 五档 K 必须是用户指定那五档，且预算 = K×2（单价真源）
    assert b["K网格"] == [50, 100, 200, 400, 500]
    assert b["预算元每期"] == {str(K): K * 2 for K in b["K网格"]}
    # ② 四臂齐全；prod_mix 因 cap 污染必须**不**进主检验
    assert b["臂"] == ["random", "tier_packing", "union_greedy", "prod_mix"]
    assert "prod_mix" not in b["进主检验"]
    assert "cap污染" in b["_prod_mix"]
    assert b["对比臂"] == "random"
    # ③ 基线必须给副本离散度（单副本基线会把抽样噪声当 Δ）
    assert "SD_replica" in b["基线臂口径"]
    # ④ 主终点唯一；附加门**不进 BH**、且不许合成总分
    assert "≥ 10" in b["主终点"] or "≥10" in b["主终点"]
    for g in b["附加指标"]:
        assert g["进BH"] is False
        assert "描述性" in g["性质"]
    assert "不做一个加权总分" in b["_附加指标纪律"]
    # ⑤ 评估窗纪律：判决只能用后 40%
    assert "60" in b["评估期"]["开发窗"] and "40" in b["评估期"]["评估窗"]
    assert "唯一判决窗口" in b["评估期"]["评估窗"]
    # ⑥ 四条判据，且第 3 条是层 1 否决（提高判据数会静默削弱否决力）
    assert [c["编号"] for c in b["判据"]] == [1, 2, 3, 4]
    assert "实现缺陷" in b["判据"][2]["判决"]
    assert "层 1" in b["验收判据"] and "不能" in b["验收判据"]
    # ⑦ 层 1 数字自洽：空间 = 上限 − 基线，且六格 × 五档全在
    l1 = b["层1_构造性上界"]
    for key, v in l1.items():
        if key.startswith("_"):
            continue
        assert set(v["格"]) == set(b["预算元每期"]), key
        for K, row in v["格"].items():
            p = v["单注p"]
            assert row["理论上限"] == pytest.approx(min(1.0, int(K) * p), abs=1e-12), (key, K)
            assert row["可争取空间"] == pytest.approx(
                row["理论上限"] - row["随机独立基线"], abs=1e-12), (key, K)
            assert row["可争取空间"] >= -1e-12, (key, K)
    # ⑧ 空间**非单调**：峰在 K·p≈1 处，K 越大不等于空间越大
    #    （双色球 ≥10元 峰在 K=100，大乐透在 K=200；K=500 已过峰 ⇒ 空间收窄）
    ssq10 = l1["双色球-≥10元"]["格"]
    dlt10 = l1["大乐透-≥10元"]["格"]
    assert ssq10["100"]["可争取空间"] > ssq10["500"]["可争取空间"] * 5, "双色球空间应随 K 过峰后收窄"
    assert dlt10["200"]["可争取空间"] > dlt10["50"]["可争取空间"] * 3
    assert 0.05 < ssq10["50"]["可争取空间"] < 0.10        # 实测 0.0726，不是 0.0043
    # ≥三等 空间是 O((K·p)²) 量级（p≈1e-5 ⇒ 最大约 1.3e-5），实质为 0
    for key in ("双色球-≥三等", "大乐透-≥三等"):
        for K, row in l1[key]["格"].items():
            assert row["可争取空间"] < 2e-5, (key, K, row["可争取空间"])
    # 空间非单调这一事实必须写进块内，否则会被读成「K 越大越值」
    assert "非单调" in b["_预期"] and "K·p≈1" in b["_空间非单调提醒"]
    # ⑨ 变更日志自证事前登记（v1.9/v1.10 是 dict 条目，v1.4 起是字符串条目 ⇒ 两种都要认）
    entry = next(e for e in PR.load()["变更日志"]
                 if isinstance(e, dict) and e.get("版本") == "1.10")
    assert "事前登记" in entry["说明"] and "尚未" in entry["说明"]
    # ⑩ 新增块不得动 Stage 1 的主假设唯一性
    assert sum(1 for c in PR.load()["格"] if c.get("主假设")) == 1
