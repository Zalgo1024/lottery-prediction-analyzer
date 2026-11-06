# -*- coding: utf-8 -*-
"""tests/test_budget_period_cap.py —— 单期上限 C2/C3 口径的契约与牙齿测试。

锁的东西：
  ① 判据文件（入库、锁定）与坏文件保守回落；C3 默认不阻断；
  ② 分组三种口径（逐批 / 单彩种同期 / 同日）+ 分母纪律（未知不静默丢）；
  ③ C2 两档：真实约束违反才进真实问题；同期合计越过上限只报不罚；
  ④ C3 两种决定都在纯函数里实现，生产默认 ①；
  ⑤ 体检第 7 节与重出接口回包确实带这份读数（不是只写在文档里）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import ev.budget_gate as BG
import ev.budget_period_cap as PC

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("证伪", "证否", "不可预测", "已证明公平", "公平随机", "写死")

CAP = 20000.0
LIM = {"月度总额": 480000.0, "单期上限": CAP, "锁定": True}


def _rec(lot, issue, n, *, rn=0, day="2026-11-07", amount=None):
    r = {"月份": "2026-11", "彩种": lot, "目标期号": issue, "注数": n,
         "单价": 2.0, "档位": "一般", "台账域": "生产",
         "时间": f"{day}T01:00:00"}
    if rn:
        r["重出批号"] = rn
    r["金额"] = (n * 2.0) if amount is None else amount
    return r


def _check(records, **kw):
    kw.setdefault("records", records)
    kw.setdefault("limit", dict(LIM))
    kw.setdefault("decisions", PC.DEFAULT_DECISIONS)
    return PC.check(**kw)


# ---------------------------------------------------------------- ① 判据
def test_real_decision_file_is_locked_and_c3_does_not_block():
    d = PC.load_decisions()
    assert d["锁定"] is True
    assert "单彩种" in d["C1"]["决定"]
    assert "补检查" in d["C2"]["决定"]
    assert d["C3"]["约束重出批"] is False
    assert PC.c3_blocks(d) is False
    assert not d.get("_缺失") and not d.get("_损坏")


def test_decision_file_is_whitelisted_for_git():
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!config/budget_period_cap.json" in text


def test_missing_or_corrupt_decision_falls_back_conservatively(tmp_path):
    a = PC.load_decisions(tmp_path / "missing.json")
    assert a["_缺失"] and PC.c3_blocks(a) is False
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    b = PC.load_decisions(bad)
    assert b["_损坏"] and PC.c3_blocks(b) is False
    obj = tmp_path / "obj.json"
    obj.write_text("[]", encoding="utf-8")
    assert PC.load_decisions(obj)["_损坏"]


def test_c3_blocks_only_on_explicit_true():
    assert PC.c3_blocks({"C3": {"约束重出批": True}}) is True
    assert PC.c3_blocks({"C3": {"约束重出批": "true"}}) is False   # 字符串不算授权
    assert PC.c3_blocks({"C3": None}) is False
    assert PC.c3_blocks({}) is False


# ---------------------------------------------------------------- ② 分组与分母
def test_group_records_three_groupings_and_regenerate_batches():
    rows = [_rec("双色球", "26128", 1000),
            _rec("大乐透", "26127", 1000),
            _rec("大乐透", "26127", 1000, rn=1)]
    g = PC.group_records(rows)
    assert len(g["逐批"]) == 3
    assert len(g["单彩种同期"]) == 2
    dlt = [i for i in g["单彩种同期"] if i["彩种"] == "大乐透"][0]
    assert dlt["批次数"] == 2 and dlt["重出批数"] == 1
    assert dlt["金额"] == 4000.0 and dlt["注数"] == 2000
    day = g["同日"][0]
    assert day["彩种数"] == 2 and day["批次数"] == 3
    assert day["金额"] == 6000.0
    assert g["记录数"] == 3 and g["参与求和条数"] == 3


def test_unknown_amount_and_date_are_counted_not_dropped():
    rows = [_rec("排列3", "26296", 100, amount=None),
            {**_rec("排列3", "26296", 100, amount=None), "金额": None, "单价": None},
            {**_rec("排列3", "26296", 200, amount=None), "时间": ""}]
    g = PC.group_records(rows)
    assert g["记录数"] == 3
    assert g["金额未知条数"] == 1
    assert g["日期未记录条数"] == 1
    assert g["参与求和条数"] == 2
    assert "未记录日期" in [d["日期"] for d in g["同日"]]
    assert g["原因分布"], "未知金额必须写原因"


def test_group_records_tolerates_non_dict_rows():
    g = PC.group_records([None, "x", _rec("福彩3D", "2026296", 5)])
    assert g["记录数"] == 3
    assert sum(b["条数"] for b in g["逐批"]) == 3


# ---------------------------------------------------------------- ③ C2 两档
def test_hard_constraint_violation_is_a_real_problem():
    rows = [_rec("双色球", "26128", 10001, amount=20002.0)]
    r = _check(rows)
    assert r["适用"] is True
    assert r["真实约束"]["违反批数"] == 1
    assert r["真实问题"] and "单期上限" in r["真实问题"][0]
    assert r["真实约束"]["违反明细"][0]["超出元"] == 2.0


def test_same_day_aggregate_over_cap_is_only_disclosure_never_a_problem():
    """★ C1① 下跨彩种合计不受单期上限约束 ⇒ 越过也绝不能计入真实问题。"""
    rows = [_rec(lot, f"26{i}", 10000, amount=20000.0)
            for i, lot in enumerate(("双色球", "大乐透", "七星彩",
                                     "排列5", "七乐彩", "快乐8"))]
    r = _check(rows)
    assert r["真实约束"]["违反批数"] == 0
    assert r["真实问题"] == []
    assert r["同期合计对照"]["同日最大合计元"] == 120000.0
    assert r["同期合计对照"]["同日越过单期上限的天数"] == 1
    assert r["对照告警"] and "不是违规" in r["对照告警"][0]


def test_gate_disabled_is_not_applicable_not_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(BG, "enabled_limit", lambda **kw: None)
    r = PC.check(records=[], decisions=PC.DEFAULT_DECISIONS)
    assert r["适用"] is False
    assert "不记为通过" in r["原因"]
    assert r["真实问题"] == []


def test_check_never_throws_on_broken_input(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(BG, "enabled_limit", _boom)
    r = PC.check(records=[], decisions=PC.DEFAULT_DECISIONS)
    assert r["适用"] is False and "不记为通过" in r["原因"]


def test_check_exposes_denominator_discipline():
    r = _check([_rec("排列3", "26296", 100)])
    t = r["台账"]
    assert t["记录数"] == t["参与求和条数"] + t["金额未知条数"]
    assert "分母" in t["分母口径"]
    assert r["披露"] and any("不构成证明" in n for n in r["披露"])


def test_c3_readout_lists_regenerate_periods():
    rows = [_rec("大乐透", "26127", 1000),
            _rec("大乐透", "26127", 1000, rn=1),
            _rec("大乐透", "26127", 1000, rn=2)]
    r = _check(rows)
    c3 = r["C3读数"]
    assert c3["约束重出批"] is False
    assert c3["重出批次数"] == 2 and c3["涉及期数"] == 1
    assert c3["逐期累计"][0]["批次数"] == 3
    assert c3["逐期累计"][0]["相对单期上限倍数"] == 0.3


# ---------------------------------------------------------------- ④ C3 两分支
def test_regenerate_gate_c3_1_never_blocks_even_when_far_over_cap():
    rows = [_rec("大乐透", "26127", 10000, rn=i) for i in range(5)]
    g = PC.regenerate_gate("大乐透", "26127", 注数=10000, records=rows,
                           limit=LIM, decisions=PC.DEFAULT_DECISIONS)
    assert g["拒绝"] is False and g["约束重出批"] is False
    assert g["本期已有批数"] == 5
    assert g["同期累计元"] == 100000.0
    assert g["预计本批元"] == 20000.0
    assert g["重出后累计元"] == 120000.0
    assert g["相对单期上限倍数"] == 6.0
    assert "不拒绝重出" in g["原因"]


C3_2 = {"C3": {"决定": "② 受同期累计约束", "约束重出批": True}}


def test_regenerate_gate_c3_2_blocks_when_cumulative_exceeds_cap():
    rows = [_rec("大乐透", "26127", 10000, rn=0)]
    g = PC.regenerate_gate("大乐透", "26127", 注数=1000, records=rows,
                           limit=LIM, decisions=C3_2)
    assert g["拒绝"] is True and "拒绝本次重出" in g["原因"]


def test_regenerate_gate_c3_2_allows_when_under_cap():
    g = PC.regenerate_gate("大乐透", "26127", 注数=100, records=[],
                           limit=LIM, decisions=C3_2)
    assert g["拒绝"] is False


def test_regenerate_gate_c3_2_without_cap_is_conservative(monkeypatch):
    monkeypatch.setattr(BG, "enabled_limit", lambda **kw: None)
    g = PC.regenerate_gate("大乐透", "26127", 注数=100, records=[],
                           decisions=C3_2)
    assert g["拒绝"] is False and "缺席≠通过" in g["原因"]


def test_regenerate_gate_unknown_amount_is_disclosed():
    rows = [{**_rec("排列3", "26296", 100), "金额": None, "单价": None}]
    g = PC.regenerate_gate("排列3", "26296", 注数=10, records=rows,
                           limit=LIM, decisions=PC.DEFAULT_DECISIONS)
    assert g["金额未知条数"] == 1 and g["同期累计元"] == 0.0


# ---------------------------------------------------------------- ⑤ 接线
def test_health_check_section7_is_wired():
    import scripts.health_check as HC
    src = (ROOT / "scripts" / "health_check.py").read_text(encoding="utf-8")
    assert "check_period_cap" in src and "## 7. 单期上限口径" in src
    assert "真实问题" in src and "单期上限口径" in src
    assert callable(HC.check_period_cap)


def test_regenerate_route_exposes_cap_notice():
    src = (ROOT / "web" / "routes" / "predict.py").read_text(encoding="utf-8")
    assert "regenerate_gate" in src and '"单期上限口径": cap_notice' in src


def test_new_module_and_decision_have_no_forbidden_words():
    for rel in ("ev/budget_period_cap.py", "config/budget_period_cap.json"):
        text = json.loads((ROOT / rel).read_text(encoding="utf-8")) if rel.endswith(".json") \
            else (ROOT / rel).read_text(encoding="utf-8")
        s = json.dumps(text, ensure_ascii=False) if not isinstance(text, str) else text
        for bad in FORBIDDEN:
            assert bad not in s, f"{rel} 出现禁词 {bad}"
