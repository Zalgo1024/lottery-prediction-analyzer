# -*- coding: utf-8 -*-
"""tests/test_prefix_real.py —— 真实生产票序前缀读数的契约与牙齿测试。

四条最容易「看起来做了、其实没做」的地方：
  ① 票序必须按出号时写下的 `批次内序号` 还原，不能依赖 JSON 里的记录顺序；
  ② 同期多批只取最早批，不许事后挑表现最好的批；
  ③ 每项排除都写原因，分母三数并列，K<n 的期不许静默剔除；
  ④ 样本量不足 ⇒ 不做 BH、不写「检出」，「≥四等」不适用必须显式标类型。
"""
from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from ev import prefix_curve as PC
from ev import prefix_real as PR

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("证伪", "证否", "不可预测", "已证明公平", "公平随机", "写死")
LOT = "双色球"
AXIS = [f"2610{i}" for i in range(1, 6)]          # 5 期
BT = "2026-09-14T10:00:00"
DD = "2026-09-15"


def _rec(issue: str, seq: int, pay: float = 0.0, grade: str = "未中", *,
         batch: str = "", batch_time: str = BT, draw_date: str = DD,
         **kw):
    r = {
        "期号": issue, "批次号": batch or f"B-{issue}", "批次内序号": seq,
        "批次时间": batch_time, "开奖日期": draw_date,
        "预测红球": [1, 2, 3, 4, 5, 6], "预测蓝球": [7],
        "实得": pay, "中奖等级": grade, "单价": 2.0,
        "valid_prediction": True, "金额未知": False,
        "来源": "predict", "台账域": "生产",
        "评估时间": "2026-09-15T21:00:00",
    }
    r.update(kw)
    return r


def _batch(issue: str, pays, *, batch: str = "", grades=None,
           shuffle: bool = False, **kw):
    grades = grades or ["未中"] * len(pays)
    rows = [_rec(issue, i + 1, p, grades[i], batch=batch, **kw)
            for i, p in enumerate(pays)]
    if shuffle:
        rows = [rows[i] for i in (2, 0, 4, 1, 3)]   # JSON 顺序 ≠ 出号顺序
    return rows


def _run(records, *, axis=AXIS, seeds=(0, 1), **kw):
    return PR.run_prefix_real(LOT, records=records, axis=axis,
                              seeds=seeds, **kw)


def _report_mod():
    spec = importlib.util.spec_from_file_location(
        "_prefix_real_report", ROOT / "scripts" / "prefix_real_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- 常量与真源
def test_prereg_constants_come_from_prefix_curve():
    """主口径与档表不许另立第二真源。"""
    assert PR.PRIMARY_N == PC.PRIMARY_N == 3
    assert PR.PREFIX_KS == PC.PREFIX_KS == (1, 3, 5, 10)
    assert PR.GRADE_GATE == "四等"


def test_grade_rank_normalizes_two_writings():
    assert PR.grade_rank("六等奖") == PR.grade_rank("六等") == 5
    assert PR.grade_rank("四等") == 3
    for name in ("未中", "直选", "组选3", "选十中零", "", None):
        assert PR.grade_rank(name) is None, f"{name} 不该被当成等级序号"


def test_grade_capable_uses_rules_not_hardcoded_list(monkeypatch):
    src = (ROOT / "ev" / "prefix_real.py").read_text(encoding="utf-8")
    assert "rules_for(" in src and "_GRADE_ORDER" in src
    assert PR.grade_capable(LOT)[0] is True
    assert PR.grade_capable("排列5")[0] is False
    assert "不适用" in PR.grade_capable("排列5")[1] or "玩法名" in PR.grade_capable("排列5")[1]


# ---------------------------------------------------------------- 票序还原
def test_order_uses_explicit_seq_not_json_order():
    """故意打乱 JSON 顺序：只有按 `批次内序号` 还原，第一注才是那张中奖票。"""
    pays = [0, 0, 0, 0, 0]
    rows = _batch("26101", pays, shuffle=True)
    rows[1]["实得"] = 5.0          # seq=1 的注（被打乱到第 2 个位置）
    orders = PR.load_real_orders(LOT, records=rows, axis=["26101"])
    tks = orders["参与"]["26101"]["票序"]
    assert [t["序号"] for t in tks] == [1, 2, 3, 4, 5]
    assert tks[0]["实得"] == 5.0
    r = _run(rows, axis=["26101"])
    assert r["曲线"]["1"]["≥5元期率"] == 1.0
    assert r["曲线"]["3"]["≥5元期率"] == 1.0


def test_derived_batches_are_excluded_with_reason():
    rows = _batch("26101", [0] * 5)
    old = []
    for i, r in enumerate(_batch("26102", [0] * 5)):
        r.pop("批次号")
        r.pop("批次内序号")
        r["评估时间"] = f"2026-09-16T21:00:0{i}"
        old.append(r)
    out = PR.load_real_orders(LOT, records=rows + old,
                              axis=["26101", "26102"])
    assert out["排除计数"]["derived批数"] >= 1
    assert out["排除计数"]["通过批数"] == 1
    assert "26102" not in out["参与"]
    assert any("推算" in e["原因"] for e in out["排除明细"])


def test_broken_seq_excludes_whole_batch_with_reason():
    rows = _batch("26101", [0] * 3)
    rows[2]["批次内序号"] = 2        # 1,2,2 ⇒ 重复
    out = PR.load_real_orders(LOT, records=rows, axis=["26101"])
    assert out["排除计数"]["序号不完整批数"] == 1
    assert out["排除计数"]["通过批数"] == 0
    assert any("不连续" in e["原因"] for e in out["排除明细"])


def test_future_batch_time_is_excluded_not_passed():
    rows = _batch("26101", [0] * 5, batch_time="2026-09-16T10:00:00")
    out = PR.load_real_orders(LOT, records=rows, axis=["26101"])
    assert out["排除计数"]["前视不通过批数"] == 1
    assert out["参与"] == {}
    assert any("前视" in e["原因"] for e in out["排除明细"])


def test_missing_time_is_not_silently_passed():
    rows = _batch("26101", [0] * 3)
    for r in rows:
        r.pop("批次时间")
    out = PR.load_real_orders(LOT, records=rows, axis=["26101"])
    assert out["排除计数"]["前视不通过批数"] == 1
    assert any("无法判定" in e["原因"] for e in out["排除明细"])


def test_same_issue_multiple_batches_takes_earliest_not_best():
    """★ 第 2 批故意中奖更多：挑它就会高估前缀读数。"""
    early = _batch("26101", [0, 0, 0, 0, 0], batch="B1-26101",
                   batch_time="2026-09-14T10:00:00")
    late = _batch("26101", [200, 200, 200, 200, 200], batch="B2-26101",
                  batch_time="2026-09-14T11:00:00",
                  grades=["四等"] * 5)
    out = PR.load_real_orders(LOT, records=early + late, axis=["26101"])
    assert out["排除计数"]["重出批数"] == 1
    assert out["排除计数"]["通过批数"] == 1
    assert out["参与"]["26101"]["批次号"] == "B1-26101"
    assert sum(t["实得"] for t in out["参与"]["26101"]["票序"]) == 0.0
    assert any("不许事后挑" in e["原因"] for e in out["排除明细"])


# ---------------------------------------------------------------- 分母纪律
def test_denominator_three_numbers_and_unparticipated_rows():
    recs = _batch("26101", [0] * 5) + _batch("26103", [0] * 5) + _batch("26105", [0] * 5)
    r = _run(recs)
    d = r["分母"]
    assert d["开奖期数"] == 5 and d["参与期数"] == 3 and d["未参与期数"] == 2
    assert d["开奖轴可用"] is True
    assert len(r["逐期"]["3"]) == 5, "每个开奖期都必须建行"
    na = [x for x in r["逐期"]["3"] if not x["可用"]]
    assert len(na) == 2 and all(x["不适用原因"] for x in na)


def test_k_less_than_n_keeps_row_and_three_rate_denominators():
    """K=3 的期在 n=10 档标「不适用」，三个分母并列给出不同读数。"""
    recs = _batch("26101", [5, 0, 0]) + _batch("26102", [5] * 12) + _batch("26103", [0] * 12)
    r = _run(recs, axis=["26101", "26102", "26103"])
    c = r["曲线"]["10"]
    assert c["不适用期数"] == 1 and c["可用期数"] == 2
    assert any("K=3 < n=10" in s for s in c["不适用原因"])
    assert c["≥5元期率"] == 0.5                  # 可用期分母 2 ⇒ 1/2
    assert c["≥5元期率_参与期分母"] == pytest.approx(1 / 3)   # 3 ⇒ 1/3
    assert c["≥5元期率_全期轴分母"] == pytest.approx(1 / 3)   # 3 ⇒ 1/3
    rows = r["逐期"]["10"]
    assert rows[0]["可用"] == 0 and "K=3" in rows[0]["不适用原因"]
    # 换成 n=1 时该期可用，命中率随分母口径变化 ⇒ 证明分母没被缩掉
    c1 = r["曲线"]["1"]
    assert c1["可用期数"] == 3 and c1["≥5元期率"] == pytest.approx(2 / 3)


def test_grade_not_applicable_is_explicit_not_zero(monkeypatch):
    monkeypatch.setattr(PR, "grade_capable",
                        lambda lot: (False, "玩法名不是等级序号"))
    r = _run(_batch("26101", [0] * 5))
    c = r["曲线"]["3"]
    assert c["≥四等期率"] is None
    assert c["≥四等期率_参与期分母"] is None
    assert c["≥四等状态"].startswith("不适用") and "玩法名" in c["≥四等状态"]
    assert r["数据体检"]["等级门槛"]["适用"] is False


# ---------------------------------------------------------------- 结算口径
def test_unknown_amount_and_invalid_not_summed_but_counted():
    recs = _batch("26101", [0] * 5)
    recs[0]["金额未知"] = True
    recs[0]["实得"] = 100.0
    recs[1]["valid_prediction"] = False
    recs[1]["实得"] = 100.0
    recs[2]["实得"] = 5.0
    r = _run(recs, axis=["26101"])
    c = r["曲线"]["3"]
    assert c["实得合计"] == 5.0
    assert c["金额未知注数"] == 1 and c["无效注数"] == 1
    assert c["≥5元期率"] == 1.0


def test_missing_unit_price_defaults_to_two_and_is_counted():
    recs = _batch("26101", [0] * 5)
    recs[0].pop("单价")
    r = _run(recs, axis=["26101"])
    c = r["曲线"]["3"]
    assert c["成本合计"] == 6.0
    assert c["单价缺失注数"] == 1


def test_net_excludes_whole_period_payout_not_top_ticket():
    """一期两注各 50（期实得 100）、另一期一注 60 ⇒ 最大单期=100，不是最大单注=60。"""
    recs = (_batch("26101", [50, 50, 0, 0, 0]) +
            _batch("26102", [60, 0, 0, 0, 0]))
    r = _run(recs, axis=["26101", "26102"])
    c = r["曲线"]["3"]
    assert c["单期最大实得"] == 100.0
    assert c["实得合计"] == 160.0
    assert c["净收益_剔除单期最大实得"] == round(160.0 - 100.0 - 12.0, 2)


# ---------------------------------------------------------------- 判决与稳定性
def test_whole_batch_is_control_and_marked_not_purchased():
    """整批 K 注只作对照，且逐期 K 不同；绝不能被当成实际购买口径。"""
    r = _run(_batch("26101", [5, 0, 0, 0, 0, 0, 0, 0, 0, 0]), axis=["26101"])
    wb = r["曲线"][PR.WHOLE_BATCH_KEY]
    assert wb["非实际购买"] is True and r["曲线"]["3"]["非实际购买"] is False
    assert wb["成本合计"] == 20.0, "整批按当期实际 K=10 注 × 2 元结算"
    assert "非实际购买" in r["口径说明"]["整批对照"]
    assert PR.WHOLE_BATCH_KEY not in r["顺序对照"], "整批档不进顺序对照"


def test_window_stability_explicitly_unmeasured_when_too_few_periods():
    r = _run(_batch("26101", [0] * 5) + _batch("26102", [0] * 5),
             axis=["26101", "26102"])
    for n in r["前缀档"]:
        s = r["稳定性"][str(n)]
        assert s["状态"].startswith("未测"), "期数不足时写 0 等于把没测写成稳定"
        assert s["原因"] and "前后半差" not in s
    assert r["稳定性"]["3"]["可用期数"] == 2
    assert r["稳定性"]["10"]["可用期数"] == 0, "K=5 < n=10 的期仍然建行但不算可用"


def test_window_stability_measured_with_enough_periods():
    axis = [f"26{i:03d}" for i in range(20)]
    recs = []
    for i, issue in enumerate(axis):
        recs += _batch(issue, [5 if i % 2 == 0 else 0] * 5)
    r = _run(recs, axis=axis)
    s = r["稳定性"]["3"]
    assert s["状态"].startswith("已测")
    assert "前后半差" in s and "切分" in s and s["可用期数"] == 20
    assert "不进 BH" in s["状态"]


def test_noise_and_concentration_are_disclosed_for_every_arm():
    r = _run(_batch("26101", [5, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
             axis=["26101"], seeds=(0, 1, 2))
    for n in r["前缀档"]:
        for arm in r["顺序对照"][str(n)].values():
            assert "噪声判读" in arm and arm["噪声判读"]
            assert "单期占Δ比_跨seed最大" in arm


def test_noise_text_has_no_pipe_that_breaks_markdown_tables():
    """★ markdown 单元格里的裸 `|` 会被当列分隔符，撑破整张表。"""
    r = _run(_batch("26101", [5, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
             axis=["26101"], seeds=(0, 1))
    for n in r["前缀档"]:
        for arm in r["顺序对照"][str(n)].items():
            assert "|" not in arm[1]["噪声判读"]


def test_report_markdown_tables_have_consistent_columns():
    r = _run(_batch("26101", [5, 0, 0, 0, 0, 0, 0, 0, 0, 0]),
             axis=["26101"], seeds=(0, 1))
    text = _report_mod().render([r], main_lottery=LOT)
    block: list[str] = []
    blocks: list[list[str]] = []
    for line in text.splitlines():
        if line.startswith("|"):
            block.append(line)
        elif block:
            blocks.append(block)
            block = []
    if block:
        blocks.append(block)
    assert blocks, "报告里没找到任何表格"
    for b in blocks:
        counts = {line.count("|") for line in b}
        assert len(counts) == 1, f"表格列数不一致（有单元格含裸竖线）：{b}"


def test_report_shows_not_applicable_stability_and_conclusion(monkeypatch):
    monkeypatch.setattr(PR, "grade_capable",
                        lambda lot: (False, "玩法名不是等级序号"))
    r = _run(_batch("26101", [5, 0, 0, 0, 0]))
    text = _report_mod().render([r], main_lottery=LOT)
    for key in ("稳定性", "描述性结论", "噪声判读", PR.WHOLE_BATCH_KEY,
                "★ 是", "不是统计判决", "整批对照"):
        assert key in text, f"报告漏了 {key}"
    assert "| 不适用 |" in text, "「≥四等」不适用不能在表里写成「—」"
    assert "不可分辨" in text or "仍非统计判决" in text


def test_no_statistical_verdict_and_no_bh():
    r = _run(_batch("26101", [0] * 5) + _batch("26102", [5] * 5))
    assert "未做统计判决" in r["判决"]["状态"]
    assert "检出" not in r["判决"]["状态"]
    assert "BH" in r["判决"]["原因"] and "不做 BH" in r["判决"]["原因"]
    assert "功效" in r["判决"]["原因"]


def test_order_comparison_is_deterministic_and_not_a_verdict():
    recs = _batch("26101", [5, 0, 0, 0, 0, 0, 0, 0, 0, 0])
    a = _run(recs, axis=["26101"], seeds=(0, 1, 2))
    b = _run(recs, axis=["26101"], seeds=(0, 1, 2))
    assert a["顺序对照"] == b["顺序对照"]
    for arm in a["顺序对照"]["3"].values():
        assert "判决" not in arm["状态"]
        assert "描述性" in arm["逐seed"][0]["状态"] or "未测" in arm["逐seed"][0]["状态"]
    assert "顺序是否承载信息" in a["口径说明"]["顺序对照"]
    assert "不主张策略有效" in a["口径说明"]["先验零"] or "不主张选号优势" in a["口径说明"]["先验零"]


# ---------------------------------------------------------------- 报告与边界
def test_report_contains_all_disclosures():
    recs = _batch("26101", [5, 0, 0, 0, 0]) + _batch("26102", [0] * 5)
    r = _run(recs)
    text = _report_mod().render([r], main_lottery=LOT)
    for key in ("真实生产票序", "冻结性局限", "结算时", "日级精度",
                "数据体检", "分母", "主口径", "顺序对照", "功效限制",
                "未接未来开奖", "不适用", "全期轴分母", "边界"):
        assert key in text, f"报告漏了 {key}"
    assert "不可直接比较读数" in text


def test_report_lottery_list_comes_from_files_not_literal():
    src = (ROOT / "scripts" / "prefix_real_report.py").read_text(encoding="utf-8")
    assert "glob(" in src and "available_lotteries" in src
    from config import LOTTERY_CONFIG
    names = set(LOTTERY_CONFIG)
    for m in re.finditer(r"\[([^\[\]]*)\]", src):
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
        assert len([q for q in quoted if q in names]) < 2, "报告脚本手写了一份彩种清单"


def test_offline_module_not_imported_by_production():
    targets = [ROOT / d for d in ("prediction", "web", "pipeline", "data")]
    targets += [ROOT / "run.py", ROOT / "cli.py"]
    for base in targets:
        files = [base] if base.is_file() else list(base.rglob("*.py"))
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            for bad in ("ev.prefix_real", "from ev import prefix_real"):
                assert bad not in text, f"生产路径 {f.name} 引入了离线研究模块：{bad}"


def test_no_forbidden_narrative_words_in_new_modules():
    # 只扫产物文件：测试文件自己持有 FORBIDDEN 常量表，扫它会自命中。
    for rel in ("ev/prefix_real.py", "scripts/prefix_real_report.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        for w in FORBIDDEN:
            assert w not in text, f"{rel} 含禁词 {w}"
