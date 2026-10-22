# -*- coding: utf-8 -*-
"""tests/test_profit_settle.py —— 开奖后自动结算（影子台账回填）。

锁定的不变量：
1. decide 修复后 d["选号"]["票"] 含真实号码（此前被吞成元数据，导致台账号码为空）。
2. settle_issue 对照实际开奖判奖：命中全对的票判为一等、实得>0、净收益=税前−税−成本。
3. settle 只回填不新增行（幂等）；settle_pending 二次调用新结算期数为 0。
4. 无实际开奖数据的期次 settle 安全跳过（不报错、不写结算）。
"""
import pytest

from data.schema import get_schema, normalize_zone_names
from ev.profit import solvers, ledger, settle as ST

# 数据里确实存在、且带完整开奖信息（含一等奖奖金）的期（近期期仅有销售额无奖级明细）
ISSUE = "9154"
# decide 走完整判定树，需要 ≥1200 期历史 ⇒ 用近期期（9154 是 2009 期，历史不足）
DECIDE_ISSUE = "26120"


@pytest.fixture
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "PROFIT_DIR", tmp_path)
    return tmp_path


def _actual_zones(lottery, issue):
    rec = ST._actual_record(lottery, issue)
    return normalize_zone_names(rec.zone_numbers, get_schema(lottery))


def test_decide_keeps_tickets():
    d = solvers.decide("双色球", DECIDE_ISSUE, K=5)
    sel = d.get("选号") or {}
    assert isinstance(sel.get("票"), list) and len(sel["票"]) > 0


def test_grade_full_match_is_first_prize(tmp_ledger):
    lottery = "双色球"
    schema = get_schema(lottery)
    az = normalize_zone_names({"红球": [1, 2, 3, 4, 5, 6], "蓝球": [16]}, schema)
    ticket = {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [16]}
    g, pay = ST._grade(lottery, ticket, az, schema, ISSUE)
    assert g == "一等"
    assert pay > 0


def test_settle_matches_actual_draw(tmp_ledger):
    lottery = "双色球"
    az = _actual_zones(lottery, ISSUE)
    red = az.get("红球")
    blue = az.get("蓝球")
    # 构造一张与实际开奖完全一致的票 → 必中一等
    ticket = {"红球": list(red), "蓝球": list(blue)}
    ledger.record(lottery, ISSUE, {"判决": "本期不建议参与", "注数": 1}, [ticket])
    res = ST.settle_issue(lottery, ISSUE)
    assert res["ok"] is True
    assert res["中奖注数"] == 1
    assert res["奖级分布"].get("一等") == 1
    assert res["税前奖金"] > 0
    assert res["净收益"] == round(res["税前奖金"] - res["税"] - res["成本"], 2)
    # 回填不新增行：summary 期数=1
    assert ledger.summary(lottery)["期数"] == 1


def test_settle_no_draw_data_skips(tmp_ledger):
    lottery = "双色球"
    ledger.record(lottery, "0000000", {"判决": "x", "注数": 1},
                 [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}])
    res = ST.settle_issue(lottery, "0000000")
    assert res["ok"] is False
    assert "实际开奖数据" in res["原因"]
    # 未回填
    assert ledger.summary(lottery)["已结算"] == 0


def test_settle_pending_idempotent(tmp_ledger):
    lottery = "双色球"
    az = _actual_zones(lottery, ISSUE)
    ticket = {"红球": list(az["红球"]), "蓝球": list(az["蓝球"])}
    ledger.record(lottery, ISSUE, {"判决": "x", "注数": 1}, [ticket])
    r1 = ST.settle_pending(lottery)
    assert r1["新结算期数"] == 1
    r2 = ST.settle_pending(lottery)
    assert r2["新结算期数"] == 0
    assert r2["已结算期数"] == 1
