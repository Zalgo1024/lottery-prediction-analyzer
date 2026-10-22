# -*- coding: utf-8 -*-
"""tests/test_profit_tax_pit.py —— 税 / 数据门禁 / 台账 / 隔离。

这几条都是「错了不会报错、只会静默给出错误结论」的地方，必须锁死。
"""
import ast
import pathlib

import pytest

from ev.profit import ledger, pit, tax as TX

ROOT = pathlib.Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- 税

def test_tax_boundary_9999_10000_10001():
    """★ 阈值是「严格大于 10000 才征」，且 1 万的跳崖必须可复现。"""
    assert TX.tax_on(9999.0) == 0.0
    assert TX.tax_on(10000.0) == 0.0            # 恰好 1 万不征
    assert TX.tax_on(10001.0) == pytest.approx(2000.2)

    n9 = TX.net_of(9999.0, 1, 2.0)
    n10 = TX.net_of(10000.0, 1, 2.0)
    n11 = TX.net_of(10001.0, 1, 2.0)
    assert n9["净收益"] == pytest.approx(9997.0)
    assert n10["净收益"] == pytest.approx(9998.0)
    assert n11["净收益"] == pytest.approx(7998.8)
    # 多中 1 元，净收益反而少约 2000 元 —— 这就是为什么 P(净盈利) 不许平滑
    assert n10["净收益"] - n11["净收益"] > 1999


def test_tax_issue_aggregate_vs_per_ticket():
    """合并口径：两注 6000 合计 12000 要征；逐注口径不征。"""
    total = 6000.0 + 6000.0
    assert TX.tax_on(total, basis=TX.BASIS_ISSUE) == pytest.approx(2400.0)
    assert TX.tax_on_ticket(6000.0) == 0.0
    assert TX.tax_on_ticket(6000.0) == 0.0


def test_tax_zero_and_negative():
    assert TX.tax_on(0.0) == 0.0
    assert TX.tax_on(-5.0) == 0.0
    assert TX.tax_on_ticket(-1.0) == 0.0


# ---------------------------------------------------------------- 期号 / 零填充

@pytest.mark.parametrize("issue,expect", [
    ("3001", (2003, 1)),         # 双色球 2003~2009 是**一位年**
    ("9001", (2009, 1)),
    ("10001", (2010, 1)),
    ("26120", (2026, 120)),
    ("2026120", (2026, 120)),    # 中彩网 7 位
    ("07001", (2007, 1)),        # 体彩 5 位
])
def test_issue_key_normalization(issue, expect):
    assert pit.issue_key("双色球", issue) == expect


def test_zero_fill_detected_as_missing():
    """销售额 0 物理上不可能 ⇒ 必须判缺失（整行未回填的哨兵）。"""
    assert pit.is_zero_filled(0.0, 0.0, 0) is True
    assert pit.is_zero_filled(None, None, None) is True
    assert pit.is_zero_filled(3.3e8, 1.0e9, 3) is False


def test_zero_fill_rows_excluded_from_history():
    """真实数据：双色球的零填充期不得进入可见历史。"""
    v = pit.as_of("双色球", "26121")
    assert v.n_observed() > 3000
    for r in v.history:
        assert r.sales > 0, f"零填充行混进了历史: {r.issue}"


def test_as_of_never_exposes_future():
    """★ 前视门禁：可见历史必须全部严格早于目标期。"""
    for lot, iss in (("双色球", "26121"), ("排列5", "26300")):
        v = pit.as_of(lot, iss)
        k = pit.issue_key(lot, iss)
        assert all(r.key < k for r in v.history), lot


def test_pool_before_is_previous_issue_pool():
    """奖池必须是**上一期开奖后余额**，不是本期（本期那个含开奖结果）。"""
    v = pit.as_of("双色球", "26121")
    assert v.prev is not None
    assert v.pool_before() == v.prev.pool_after
    assert v.pool_before() > 0


def test_forecast_sales_uses_only_history():
    v = pit.as_of("双色球", "26121")
    fc = v.forecast_sales(window=8)
    assert fc["n"] == 8 and fc["p10"] <= fc["median"] <= fc["p90"]


# ---------------------------------------------------------------- 台账

@pytest.fixture
def tmp_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger, "PROFIT_DIR", tmp_path)
    return tmp_path


def test_ledger_idempotent_per_issue(tmp_ledger):
    d = {"判决": "本期不建议参与", "注数": 5, "税口径": TX.BASIS_ISSUE,
         "κ0基线": {"净EV单注": -0.9, "P至少中一注": 0.3, "中奖注数期望": 0.5}}
    r1 = ledger.record("双色球", 26121, d)
    r2 = ledger.record("双色球", 26121, d)
    assert r1["覆盖"] is False and r2["覆盖"] is True
    rows = ledger._read("双色球")
    assert len(rows) == 1


def test_ledger_settle_backfills_without_adding_row(tmp_ledger):
    ledger.record("双色球", 26121, {"判决": "x", "注数": 5})
    assert len(ledger._read("双色球")) == 1
    out = ledger.settle("双色球", 26121, gross=12000.0, tax=2400.0, cost=10.0)
    assert out["ok"] is True
    rows = ledger._read("双色球")
    assert len(rows) == 1, "结算必须回填，不许新增行"
    assert rows[0]["结算"]["净收益"] == pytest.approx(12000 - 2400 - 10)


def test_ledger_summary_drawdown(tmp_ledger):
    for i, net in enumerate([-10.0, -5.0, 30.0, -20.0]):
        ledger.record("双色球", 100 + i, {"判决": "x", "注数": 1})
        ledger.settle("双色球", 100 + i, gross=net + 2.0, tax=0.0, cost=2.0)
    s = ledger.summary("双色球")
    assert s["已结算"] == 4
    assert s["净收益"] == pytest.approx(-5.0)
    assert s["最大回撤"] == pytest.approx(20.0)


# ---------------------------------------------------------------- 隔离

def test_profit_package_never_writes_pending():
    """★ 静态锁：ev/profit 的任何一个模块都不许 import record_pending_prediction。"""
    pkg = ROOT / "ev" / "profit"
    offenders = []
    for f in pkg.glob("*.py"):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                for a in node.names:
                    if a.name in ("record_pending_prediction", "_upsert_pending"):
                        offenders.append(f"{f.name}:{a.name}")
            elif isinstance(node, ast.Import):
                for a in node.names:
                    if "feedback" in a.name:
                        offenders.append(f"{f.name}:{a.name}")
    assert not offenders, f"盈利策略不得触碰生产/研究 pending: {offenders}"


def test_ledger_dir_is_gitignored():
    """账面目录必须落在 training/ 下（已 gitignore），不会污染仓库。"""
    assert "training" in str(ledger.path("双色球")).lower()
