# -*- coding: utf-8 -*-
"""tests/test_stress_rolling.py —— 跨期滚动压力测试的牙齿护栏。

锁四件事：
  ① 分母三数并列，未参与期（前视不通过 / 逐期读数异常）仍建行进分母并写原因；
  ② 每期 PIT 前视检查由 stress_test 校验，滚动层不许覆盖或补写；
  ③ 退化按**连续段**合并，累计情景数 = 逐期之和（故意只取最后一期会红）；
  ④ 只做描述性汇总：不做 BH / 统计判决 / 不接生产 / 不改下注量 / 不写盘。
"""
from __future__ import annotations

from pathlib import Path

from ev.profit import stress as ST


def _reading(verdict: str, adv: float, *, pit: bool = True,
             n_scen: int = 2, ret: float = 0.4) -> dict:
    return {"判决": verdict, "基准优势_元每注": adv, "前视检查": pit,
            "情景": [{"保留率": ret} for _ in range(n_scen)],
            "判据版本": ST.STRESS_VERSION}


def _runner(mapping):
    return lambda lot, issue, **kw: mapping[str(issue)]


# ------------------------------------------------------------------ 分母
def test_denominator_three_numbers_with_reasons():
    """三数并列；未参与期逐条写原因，不许静默从分母里消失。"""
    rows = {"1": _reading("稳定", 0.08), "2": _reading("退化", 0.02, pit=False),
            "3": _reading("脆弱", 0.05)}
    r = ST.rolling_stress_test("双色球", ["1", "2", "3"], runner=_runner(rows))
    assert r["期数"] == {"总数": 3, "参与": 2, "未参与": 1}
    miss = [x for x in r["逐期"] if not x["参与"]]
    assert len(miss) == 1 and "前视检查未通过" in miss[0]["原因"]
    assert miss[0]["判决"] is None and miss[0]["基准优势_元每注"] is None
    assert sum(r["判决计数"].values()) == 2


def test_runner_failure_still_builds_a_row():
    """空期 / 逐期读数异常 ⇒ 建行、标未参与、写原因（空期建行进分母）。"""
    def _boom(lot, issue, **kw):
        raise RuntimeError("no data")

    r = ST.rolling_stress_test("双色球", [None, "x"], runner=_boom)
    assert r["期数"] == {"总数": 2, "参与": 0, "未参与": 2}
    assert all("逐期读数不可用" in x["原因"] for x in r["逐期"])
    assert r["累计情景数"] == 0 and r["判决计数"] == {}


def test_empty_issue_list_is_zero_not_pass():
    r = ST.rolling_stress_test("双色球", [], runner=lambda *a, **k: _reading("稳定", 0.1))
    assert r["期数"] == {"总数": 0, "参与": 0, "未参与": 0}
    assert r["累计情景数"] == 0 and r["连续退化段数"] == 0


# ------------------------------------------------------------------ 口径
def test_cumulative_scenarios_equals_sum_of_periods():
    """★ 牙齿：把累计改成「只取最后一期」会在这里红。"""
    rows = {"1": _reading("稳定", 0.08, n_scen=2),
            "2": _reading("脆弱", 0.05, n_scen=3),
            "3": _reading("稳定", 0.09, n_scen=4)}
    r = ST.rolling_stress_test("双色球", ["1", "2", "3"], runner=_runner(rows))
    assert [x["情景数"] for x in r["逐期"]] == [2, 3, 4]
    assert r["累计情景数"] == 9


def test_consecutive_degradation_merges_into_one_segment():
    """连续段合并：退化退化→1 段；被「稳定」或未参与期打断后另起一段。"""
    rows = {"1": _reading("退化", -0.01), "2": _reading("退化", -0.02),
            "3": _reading("稳定", 0.08), "4": _reading("退化", -0.03),
            "5": _reading("退化", -0.04, pit=False), "6": _reading("退化", -0.05)}
    r = ST.rolling_stress_test("双色球", list("123456"), runner=_runner(rows))
    assert r["连续退化段数"] == 3, r["逐期"]
    assert r["判决计数"].get("退化") == 4


def test_pit_failure_never_enters_verdict_or_worst_retention():
    """★ 滚动层不许把前视不通过的读数补进判据（覆盖 PIT 即红）。"""
    rows = {"1": _reading("退化", 0.9, pit=False, n_scen=5, ret=-5.0),
            "2": _reading("稳定", 0.08, n_scen=1, ret=0.7)}
    r = ST.rolling_stress_test("双色球", ["1", "2"], runner=_runner(rows))
    assert r["基准优势序列"] == [0.08]
    assert r["最差保留率"] == 0.7
    assert r["累计情景数"] == 1


def test_worst_retention_handles_missing_values():
    rows = {"1": _reading("稳定", 0.08, n_scen=0),
            "2": _reading("脆弱", 0.05, n_scen=1, ret=0.2)}
    r = ST.rolling_stress_test("双色球", ["1", "2"], runner=_runner(rows))
    assert r["逐期"][0]["最差保留率"] is None
    assert r["最差保留率"] == 0.2


# ------------------------------------------------------------------ 边界
def test_versions_come_from_single_source():
    r = ST.rolling_stress_test("双色球", ["1"],
                               runner=lambda *a, **k: _reading("稳定", 0.1))
    assert r["判据版本"] == ST.STRESS_VERSION
    assert r["滚动版本"] == ST.ROLLING_VERSION
    assert ST.ROLLING_VERSION != ST.STRESS_VERSION


def test_rolling_is_descriptive_only():
    """不许把描述性汇总写成统计判决：无 BH、无「检出/未检出」结论。"""
    text = "\n".join(ST.rolling_stress_test(
        "双色球", ["1"], runner=lambda *a, **k: _reading("稳定", 0.1))["说明"])
    assert "不做多重校正" in text and "不做统计判决" in text
    assert "未开判" in text
    for bad in ("检出", "未检出", "BH 通过", "p值"):
        assert bad not in text, f"滚动层出现了统计判决措辞：{bad}"


def test_module_has_no_production_side_effects():
    """离线研究：不写盘、不接生产闸门、不改下注量。"""
    src = Path(ST.__file__).read_text(encoding="utf-8")
    for bad in ("record_spend", "resolve_groups", "save_pending", "write_text(",
                "open(", "ticket_size"):
        assert bad not in src, f"压力测试模块出现生产副作用：{bad}"


def test_production_paths_do_not_call_rolling():
    """滚动入口只属离线研究，生产四包 + 两个入口不许调用它。"""
    root = Path(ST.__file__).resolve().parents[2]
    targets = [root / d for d in ("prediction", "web", "pipeline", "data")]
    targets += [root / "run.py", root / "cli.py"]
    for base in targets:
        files = [base] if base.is_file() else list(base.rglob("*.py"))
        for f in files:
            assert "rolling_stress_test" not in f.read_text(encoding="utf-8", errors="replace"), \
                f"生产路径调用了跨期滚动：{f.name}"
