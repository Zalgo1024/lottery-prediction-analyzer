# -*- coding: utf-8 -*-
"""tests/test_tier_ledger.py —— 影子台账与隔离。

锁死三件事：
1. 策略名在 `NON_LEARNING_STRATEGIES` 里（双保险之一）；
2. `record_pending=True` 直接抛错（永不写生产 pending）；
3. 台账按期幂等、结算只回填不新增行。
"""
import json

import pytest

from ev import tier_ledger as TL


@pytest.fixture()
def ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(TL, "TIER_DIR", tmp_path / "prize_tier")
    return TL


TICKETS = [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]},
           {"红球": [7, 8, 9, 10, 11, 12], "蓝球": [8]}]
META = {"目标": "≥四等", "规则版本": "ssq-2026-02", "单注达标概率": 4.44e-4,
        "理论上限Kp": 0.0889, "覆盖通道数": 44, "通道权重和": 0.0099, "重叠上限": 3}


def test_strategy_is_registered_as_non_learning():
    """策略名必须在 NON_LEARNING_STRATEGIES 里，否则会稀释三策略权重（静默回归）。"""
    from data.feedback import NON_LEARNING_STRATEGIES, RESEARCH_STRATEGIES

    assert TL.STRATEGY in NON_LEARNING_STRATEGIES
    assert RESEARCH_STRATEGIES is NON_LEARNING_STRATEGIES


def test_record_pending_is_forbidden(ledger):
    with pytest.raises(ValueError):
        ledger.record("双色球", "26120", "四等", TICKETS, META, record_pending=True)
    assert ledger.RECORD_PENDING_ALLOWED is False


def test_record_is_idempotent(ledger):
    r1 = ledger.record("双色球", "26120", "四等", TICKETS, META)
    r2 = ledger.record("双色球", "26120", "四等", TICKETS, META)
    assert r1["覆盖"] is False and r2["覆盖"] is True
    rows = ledger._read("双色球")
    assert len(rows) == 1
    assert rows[0]["策略"] == TL.STRATEGY and rows[0]["台账域"] == "研究"
    assert rows[0]["注数"] == 2 and rows[0]["结算"] is None


def test_settle_backfills_without_new_row(ledger):
    ledger.record("双色球", "26120", "四等", TICKETS, META)
    out = ledger.settle("双色球", "26120", gross=210.0, cost=4.0, hit_target=True)
    assert out["ok"] is True
    rows = ledger._read("双色球")
    assert len(rows) == 1, "结算只能回填，不许新增行"
    assert rows[0]["结算"]["净收益"] == pytest.approx(206.0)
    assert rows[0]["结算"]["达标"] is True


def test_settle_unknown_issue(ledger):
    out = ledger.settle("双色球", "99999", gross=0.0, cost=0.0)
    assert out["ok"] is False and "原因" in out


def test_summary(ledger):
    ledger.record("双色球", "26120", "四等", TICKETS, META)
    ledger.record("双色球", "26121", "四等", TICKETS, META)
    ledger.settle("双色球", "26120", gross=210.0, cost=4.0, hit_target=True)
    ledger.settle("双色球", "26121", gross=0.0, cost=4.0, hit_target=False)
    s = ledger.summary("双色球")
    assert s["期数"] == 2 and s["已结算"] == 2
    assert s["达标期数"] == 1 and s["达标期占比"] == 0.5
    assert s["净收益"] == pytest.approx(202.0)
    assert "研究域" in s["说明"]


def test_ledger_file_is_jsonl(ledger):
    ledger.record("双色球", "26120", "四等", TICKETS, META)
    p = ledger.path("双色球")
    lines = [x for x in p.read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["期号"] == "26120"


def test_cli_summary_smoke(monkeypatch, capsys, tmp_path):
    """CLI 接线：`prize-tier --summary` 能跑通（台账为空也要正常返回）。"""
    monkeypatch.setattr(TL, "TIER_DIR", tmp_path / "prize_tier")
    import cli

    monkeypatch.setattr("sys.argv", ["cli.py", "prize-tier", "双色球", "--summary"])
    cli.main()
    out = capsys.readouterr().out
    assert "奖级达标策略" in out
