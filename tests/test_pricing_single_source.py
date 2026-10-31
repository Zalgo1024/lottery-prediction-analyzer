# -*- coding: utf-8 -*-
"""tests/test_pricing_single_source.py —— 单注票价的**单一真源**。

为什么需要这条锁
----------------
单价（元/注）是「成本 → 净收益 → 每 100 元投入」整条链的分母。它此前有
**四份互不相干的拷贝**：

    ev/rulebook.py::COST = 2
    ev/arena/__init__.py::COST_PER_BET = 2.0
    ev/budget_envelope.py::Envelope.cost_per_ticket = 2.0      （字段默认值）
    ev/bankroll.py::Budget.cost_per_ticket = 2.0               （字段默认值）
    另有 cli.py `plan --cost` 的 argparse 默认值 2.0

改一处不会改另几处 ⇒ 成本口径会**静默漂移**。而成本漂移的后果不是报错，
是「净收益」和「EV」变成两组不可比的数——比崩溃更难查。

本文件锁三件事：
  ① 唯一真源存在且六彩种全覆盖（`config.ticket_price`）；
  ② 四个派生点都等于真源（不是「碰巧都等于 2」的巧合，是同一个值）；
  ③ 非测试源码里**不许再出现**票价字面量（AST 扫描，带白名单豁免）。

★ 为什么不用「都等于 2.0」来断言：六彩种今天的单价确实全是 2 元，
  但一旦某彩种调价（历史上数字型与乐透型就分别调过），
  「都等于 2」会红得毫无意义，「都等于真源」才是要保的性质。
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

import config
from config import LOTTERY_CONFIG, ticket_price

_REPO = Path(__file__).resolve().parent.parent

# 六彩种（schema 的真源在 config.LOTTERY_CONFIG）
_LOTTERIES = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]


# ---------------------------------------------------------------- ① 真源本身
def test_all_lotteries_declare_a_ticket_price():
    """六彩种都必须显式声明 payout.cost —— 缺键走回退不算声明。"""
    for lot in _LOTTERIES:
        assert lot in LOTTERY_CONFIG, f"彩种 {lot} 不在 LOTTERY_CONFIG"
        payout = LOTTERY_CONFIG[lot].get("payout") or {}
        assert "cost" in payout, f"{lot} 缺少 payout.cost（会静默回退到默认单价）"
        assert float(payout["cost"]) > 0, f"{lot} 的 payout.cost 必须为正：{payout['cost']}"


def test_ticket_price_reads_config_per_lottery():
    assert set(_LOTTERIES) <= set(LOTTERY_CONFIG)
    for lot in _LOTTERIES:
        assert ticket_price(lot) == float(LOTTERY_CONFIG[lot]["payout"]["cost"])


def test_ticket_price_never_falls_back_to_zero():
    """未知彩种 / 坏配置回退默认单价，**绝不回退 0**。

    回退 0 会让净收益 = 实得、EV 变成非负 —— 一种「数字合法但错得离谱」，
    比缺值更危险（界面不会报错，只会给出漂亮结论）。
    """
    assert ticket_price("不存在的彩种") == config.DEFAULT_TICKET_PRICE
    assert ticket_price("") == config.DEFAULT_TICKET_PRICE
    assert config.DEFAULT_TICKET_PRICE > 0


def test_ticket_price_rejects_non_positive_and_garbage(monkeypatch):
    monkeypatch.setitem(LOTTERY_CONFIG, "假彩种A", {"payout": {"cost": 0}})
    monkeypatch.setitem(LOTTERY_CONFIG, "假彩种B", {"payout": {"cost": -3}})
    monkeypatch.setitem(LOTTERY_CONFIG, "假彩种C", {"payout": {"cost": "两元"}})
    monkeypatch.setitem(LOTTERY_CONFIG, "假彩种D", {})
    for lot in ("假彩种A", "假彩种B", "假彩种C", "假彩种D"):
        assert ticket_price(lot) == config.DEFAULT_TICKET_PRICE, lot


# ---------------------------------------------------------------- ② 派生点
def test_rulebook_cost_equals_source():
    from ev import rulebook
    for lot in _LOTTERIES:
        assert rulebook.COST == ticket_price(lot), (
            f"ev/rulebook.COST={rulebook.COST} 与 {lot} 单价 {ticket_price(lot)} 不一致")


def test_arena_cost_equals_source():
    from ev import arena
    for lot in _LOTTERIES:
        assert arena.COST_PER_BET == ticket_price(lot)


def test_default_envelope_price_is_per_lottery():
    """包络单价必须**逐彩种**从真源取，不是构造时统一写死 2.0。"""
    from ev import budget_envelope as BE
    for lot in _LOTTERIES:
        env = BE.default_envelope(lot)
        assert env.cost_per_ticket == ticket_price(lot)
        assert env.lottery == lot
        # 单期上限必须能被单价整除成注数且不为 0（回退成 0 注等于静默停发）
        assert env.cap_tickets >= 1


def test_default_envelopes_covers_all_lotteries():
    from ev import budget_envelope as BE
    envs = BE.default_envelopes()
    for lot in _LOTTERIES:
        assert lot in envs, f"default_envelopes() 漏了 {lot}"
        assert envs[lot].cost_per_ticket == ticket_price(lot)


def test_dataclass_defaults_point_at_source():
    """两处 dataclass 字段默认值也必须来自 config，不许各自写 2.0。"""
    from ev import budget_envelope as BE
    from ev import bankroll
    assert BE.Envelope(monthly_budget=100.0, per_period_cap=100.0).cost_per_ticket \
        == config.DEFAULT_TICKET_PRICE
    assert bankroll.Budget().cost_per_ticket == config.DEFAULT_TICKET_PRICE


# ---------------------------------------------------------------- ③ 字面量扫描
# 允许出现票价数字的文件：真源本身 + 测试（测试需要构造明确的期望值）
_ALLOW = {
    "config.py",
    "tests/test_pricing_single_source.py",
    "tests/test_budget_envelope.py",
    "tests/test_budget_dynamic.py",
}

# 允许出现的**路径前缀**：scripts/oneoff/ 是已冻结的一次性实验脚本，
# 里面写的是「当时那次实验的口径」，改它等于篡改实验记录 —— 保持原样。
_ALLOW_PREFIX = ("scripts/oneoff/",)

# 这些名字被赋予数字字面量 = 又多了一份单价拷贝
_PRICE_NAMES = {"COST", "COST_PER_BET", "TICKET_PRICE", "PRICE_PER_TICKET",
                "UNIT_COST", "COST_PER_TICKET"}

_SCAN_DIRS = ["ev", "data", "prediction", "web", "market", "scripts"]


def _source_files():
    def _skip(rel: str) -> bool:
        return rel in _ALLOW or rel.startswith(_ALLOW_PREFIX)

    for name in _SCAN_DIRS:
        d = _REPO / name
        if not d.is_dir():
            continue
        for p in sorted(d.rglob("*.py")):
            rel = p.relative_to(_REPO).as_posix()
            if not _skip(rel):
                yield rel, p
    for p in sorted(_REPO.glob("*.py")):
        if not _skip(p.name):
            yield p.name, p


def _is_number(node) -> bool:
    return isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) \
        and not isinstance(node.value, bool)


def _scan_unbound_price_literals() -> list[str]:
    """找出「把票价写成数字字面量」的每一处。

    两条判据（都是 AST，不做文本匹配 —— 注释里的 2 元不该误报）：
      A. `赋值 / 注解赋值` 的目标名在 `_PRICE_NAMES` 里，且值是数字常量；
      B. 形如 `xxx.cost_per_ticket = <数字>` 的关键字实参或属性赋值。
    """
    bad: list[str] = []
    for rel, path in _source_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):        # 损坏文件由别的测试管
            continue
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets, value = node.targets, node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets, value = [node.target], node.value
            if targets and _is_number(value):
                for t in targets:
                    nm = t.id if isinstance(t, ast.Name) else \
                        (t.attr if isinstance(t, ast.Attribute) else "")
                    # ★ 只做**精确**名字匹配。曾用「小写后比较」，结果把
                    #   `ArmResult.cost = 0.0`（总成本字段默认值，不是票价）
                    #   也判成拷贝 —— 名字相近不等于语义相同。
                    if nm in _PRICE_NAMES:
                        bad.append(f"{rel}:{value.lineno} {nm} = {value.value!r}")
            # B：关键字实参 cost_per_ticket=<数字>
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    if kw.arg == "cost_per_ticket" and _is_number(kw.value):
                        bad.append(f"{rel}:{kw.value.lineno} cost_per_ticket={kw.value.value!r}")
            # B'：函数签名默认值 cost_per_ticket: float = <数字>
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                args = node.args
                pos = list(args.posonlyargs) + list(args.args)
                for a, d in zip(pos[len(pos) - len(args.defaults):], args.defaults):
                    if a.arg == "cost_per_ticket" and _is_number(d):
                        bad.append(f"{rel}:{d.lineno} def {node.name}(cost_per_ticket={d.value!r})")
                for a, d in zip(args.kwonlyargs, args.kw_defaults):
                    if a.arg == "cost_per_ticket" and d is not None and _is_number(d):
                        bad.append(f"{rel}:{d.lineno} def {node.name}(cost_per_ticket={d.value!r})")
    return bad


def test_no_second_copy_of_ticket_price_literal():
    bad = _scan_unbound_price_literals()
    assert not bad, (
        "发现票价字面量的额外拷贝，请改为 config.ticket_price(...)：\n  "
        + "\n  ".join(bad))


def test_scanner_actually_catches_a_copy(tmp_path, monkeypatch):
    """牙齿的牙齿：扫描器必须能抓到一份人造拷贝，否则上面那条是假绿。"""
    d = tmp_path / "ev"
    d.mkdir()
    (d / "fake_mod.py").write_text(
        "COST = 2\n\n\ndef f(cost_per_ticket: float = 2.0):\n    return cost_per_ticket\n",
        encoding="utf-8")
    monkeypatch.setattr("tests.test_pricing_single_source._REPO", tmp_path)
    monkeypatch.setattr("tests.test_pricing_single_source._SCAN_DIRS", ["ev"])
    bad = _scan_unbound_price_literals()
    assert len(bad) >= 2, f"扫描器漏抓：{bad}"
    assert any("COST" in b for b in bad)
    assert any("cost_per_ticket" in b for b in bad)
