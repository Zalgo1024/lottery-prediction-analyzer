# -*- coding: utf-8 -*-
"""tests/test_prefix_curve.py —— 前缀口径研究的契约与牙齿测试。

三条最容易「看起来做了、其实没做」的地方：
  ① 所有前缀档必须来自**同一份冻结票序**，不是每个 n 重选一次；
  ② 分母必须三数并列，未参与期数不许被静默省略或剔除；
  ③ 族规模必须在跑之前声明，BH 不许事后扩容检验族。
"""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from ev import prefix_curve as PC
from ev import order_freeze as OF

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("证伪", "证否", "不可预测", "已证明公平", "公平随机", "写死")


@pytest.fixture
def replayed(tmp_path, monkeypatch):
    """小样本离线回放：打掉真实选号 / 结算 / 开奖读取，只验契约。"""
    import ev.coverage_backtest as CB
    import ev.tier_backtest as TB
    import ev.tier_target as TT

    recs = [SimpleNamespace(期号=f"26{i:03d}") for i in range(20)]

    def fake_select(arm, lottery, pool, K, target, issue, seed, cap):
        return ([{"序号": i} for i in range(K)], {"策略": arm})

    def fake_eval(lottery, tickets, actual_rec, issue, cache,
                  target_grades=None, *, rule_issue=None):
        hit = int(issue) % 2 == 0
        return {"命中注数": int(hit), "总实得": 5.0 if hit else 0.0,
                "≥5元": hit, "达标": hit, "结算口径": {}}

    monkeypatch.setattr(TB, "load_test_recs", lambda lot, periods=None: recs)
    monkeypatch.setattr(TB, "_grades_for", lambda *a, **k: frozenset({"四等"}))
    monkeypatch.setattr(TB, "_select", fake_select)
    def fake_resolve(lottery, target=None, issue=None, *a, **k):
        kind = (target or {}).get("kind")
        return {"label": "≥四等", "grades": frozenset({"四等"}),
                "rule_version": "2026",
                "conditions": "PAYOUT5" if kind == "payout" else "GRADE4"}

    def fake_p(lottery, conditions, issue=None):
        return 0.06 if conditions == "PAYOUT5" else 0.0004

    monkeypatch.setattr(TT, "resolve_conditions", fake_resolve)
    monkeypatch.setattr(TT, "single_ticket_probability", fake_p)
    monkeypatch.setattr(CB, "build_pool", lambda *a, **k: [])
    monkeypatch.setattr(CB, "evaluate_period", fake_eval)

    out = PC.run_prefix_curve("双色球", K=12, periods=20, seeds=(0,),
                              pool_size=50, reselect_every=10,
                              snapshot_dir=tmp_path / "snap")
    return out


def test_prereg_constants_are_frozen():
    pre = PC.PREREGISTRATION
    assert PC.PRIMARY_N == 3
    assert PC.PREFIX_KS == (1, 3, 5, 10)
    assert pre["族规模"]["检验数"] == 16
    assert pre["名义α"] == 0.05
    assert pre["探索窗比例"] == 0.6
    assert pre["基线臂"] == "random"
    assert pre["版本"] == PC.PREREG_VERSION == "2026-11-05-A"


def test_theoretical_head():
    assert PC.theoretical_head(0.01, 1) == 0.0, "K=1 时空间严格为 0"
    assert PC.theoretical_head(0.01, 0) == 0.0
    h3 = PC.theoretical_head(0.01, 3)
    h10 = PC.theoretical_head(0.01, 10)
    assert 0 < h3 < h10, "小 K 空间是二阶小量但随 K 增长"
    assert PC.theoretical_head(4.4e-4, 3) < 1e-5, "窄门槛在小 K 上空间近乎为零"


def test_replay_denominator_and_counterfactual_notes(replayed):
    d = replayed["分母"]
    assert set(("开奖期数", "参与期数", "未参与期数", "未参与说明")) <= set(d)
    assert d["开奖期数"] == d["参与期数"] == 20
    assert d["未参与期数"] == 0
    assert "不进主判据" in d["未参与说明"] and "不缩分母" in d["未参与说明"]
    notes = replayed["口径说明"]
    assert "反事实" in notes["反事实"] and "非实测" in notes["反事实"]
    assert "非实际购买" in notes["整批对照"]
    assert "不构成证明" in notes["术语"]


def test_all_prefixes_share_one_frozen_order(replayed):
    """2 块 × 3 臂 × 1 seed = 6 份快照；若每个 n 重选应是 30 份。"""
    assert len(replayed["冻结快照"]) == 6
    for p in replayed["冻结快照"]:
        snap = OF.load_snapshot(Path(p))
        assert len(snap["票序"]) == 12
        assert OF.prefix_of(snap, 3) == snap["票序"][:3]


def test_cost_is_actual_prefix_stake_and_whole_batch_is_control(replayed):
    sk = str(replayed["seeds"][0])
    for n in replayed["全档"]:
        for arm in replayed["臂"]:
            seq = replayed["逐期"][sk][arm][str(n)]
            assert all(c == 2 * n for c in seq["成本"]), "成本必须按实际购买注数累加"
            r = replayed["曲线"][sk][arm][str(n)]
            assert r["成本"] == 2 * n * len(seq["期号"])
            assert r["非实际购买"] is bool(n == replayed["K"])
            assert set(("≥5元", "≥四等")) <= set(r["上界"])
            assert "可争取空间上界" in r["上界"]["≥5元"]


def test_bounds_are_split_by_metric(replayed):
    """★ 两个门槛的 p 差几个数量级，合成一个 n·p 会把读数锚错量。"""
    sk = str(replayed["seeds"][0])
    r = replayed["曲线"][sk]["random"]["3"]
    assert r["单注p"]["≥5元"] != r["单注p"]["≥四等"]
    head5 = r["上界"]["≥5元"]["可争取空间上界"]
    head_tier = r["上界"]["≥四等"]["可争取空间上界"]
    assert head5 > head_tier > 0
    assert r["上界"]["≥5元"]["n·p"] == pytest.approx(3 * 0.06)


def test_single_seed_stability_is_marked_unmeasured(replayed):
    """缺席≠通过：单 seed 时全距 0 是「没测」，不许写成「稳定」。"""
    assert len(replayed["seeds"]) == 1
    for v in replayed["稳定性"].values():
        assert v["状态"].startswith("未测")
        assert "原因" in v
        assert "≥5元期率_跨seed全距" not in v


def test_verdict_family_size_is_declared_before_bh(replayed, monkeypatch):
    v = PC.verdict_curve(replayed)
    assert v["族规模"] == 16
    assert v["BH"]["检验数"] == 16
    assert len(v["格"]) == 16
    assert "结论" in v and "未检出" in v["结论"]
    monkeypatch.setitem(PC.PREREGISTRATION["族规模"], "检验数", 99)
    with pytest.raises(ValueError, match="族规模不符"):
        PC.verdict_curve(replayed)


def test_shadow_is_append_only(tmp_path):
    p = tmp_path / "shadow.jsonl"
    rec = {"期号": "26127", "冻结版本": "2026-11-05-A",
           "策略哈希": "abc", "n": 3, "读数": 1}
    assert PC.append_shadow(rec, path=p) is True
    assert PC.append_shadow(dict(rec, 读数=999), path=p) is False
    lines = [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 1 and lines[0]["读数"] == 1
    assert PC.append_shadow(dict(rec, 期号="26128"), path=p) is True
    assert len(p.read_text(encoding="utf-8").splitlines()) == 2


def test_offline_modules_not_imported_by_production():
    """研究通道不许渗进生产出号 / 结算 / 看板。"""
    targets = [ROOT / d for d in ("prediction", "web", "pipeline", "data")]
    targets += [ROOT / "run.py", ROOT / "cli.py"]
    for base in targets:
        files = [base] if base.is_file() else list(base.rglob("*.py"))
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            for bad in ("ev.prefix_curve", "ev.order_freeze",
                        "from ev import prefix_curve", "from ev import order_freeze"):
                assert bad not in text, f"生产路径 {f.name} 引入了离线研究模块：{bad}"


def test_no_forbidden_narrative_words_in_new_modules():
    for name in ("ev/prefix_curve.py", "ev/order_freeze.py",
                 "scripts/prefix_curve_report.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} 出现禁词 {bad}"


def test_tier_k_grid_output_name_carries_target():
    """两个门槛的空间差几个数量级，证据文件名不带目标就会互相覆盖。"""
    text = (ROOT / "scripts" / "tier_k_grid.py").read_text(encoding="utf-8")
    assert "_t{args.target_grade}" in text


def test_report_header_echoes_prereg_and_notes(replayed):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "prefix_curve_report", ROOT / "scripts" / "prefix_curve_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    text = mod.render(replayed, PC.verdict_curve(replayed))
    for key in ("预注册", "反事实", "非实际购买", "分母", "主口径n",
                "整批对照", "评估窗", "结论"):
        assert key in text, f"报告首部漏了 {key}"
