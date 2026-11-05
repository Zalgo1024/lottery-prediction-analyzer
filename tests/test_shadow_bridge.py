# -*- coding: utf-8 -*-
"""tests/test_shadow_bridge.py —— 影子编排层（四包外唯一白名单接入点）。

覆盖四类风险：
  ① 白名单不能被悄悄放宽（改大即红）；
  ② 四包内一旦出现研究模块 / 编排层 import，判定必须能抓到（纯函数负向注入）；
  ③ 编排层自身不变量：不读系统时间、不决定注数；
  ④ 开关关闭 / 配置缺失 ⇒ **零副作用**（不产生快照、不写台账），且不记为通过。
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from ev import order_freeze as OF
from ev import shadow_bridge as SB
from tests import _import_guard as G

# 2026-11-08 是周日 ⇒ 双色球开奖日（draw_days=[1,3,6]，0=周一）
DRAW_DATE = date(2026, 11, 8)
BATCH = "2026-11-08T12:00:00"
REG = "2026-11-08T12:00:05"


def _tickets(k=3):
    return [{"号码": {"红球": [i + 1, 2, 3, 4, 5, 6], "蓝球": [7]},
             "策略": f"s{i}", "置信度": round(1.0 - i * 0.01, 4)}
            for i in range(k)]


def _schedule(tmp_path, **over):
    """合成时刻表：彩种与星期取自真源模板，时刻填一个**非真实**的测试值。"""
    from ev import draw_schedule as DS
    doc = DS.input_template()
    for row in doc["彩种"].values():
        row["开奖时刻"] = "21:15"
    doc.update(over)
    p = tmp_path / "draw_schedule.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


def _freeze(tmp_path, **over):
    kw = dict(lottery="双色球", issue="26128", tickets=_tickets(), K=3,
              target_date=DRAW_DATE, batch_time=BATCH, register_time=REG,
              switch={"启用": True},
              schedule_path=_schedule(tmp_path),
              snapshot_dir=tmp_path / "snap",
              shadow_path=tmp_path / "shadow.jsonl")
    kw.update(over)
    return SB.freeze_after_predict(**kw)


# ---------------------------------------------------------------- ① 白名单
def test_whitelist_is_minimal():
    """白名单必须**恰好**是这些。改大（尤其是把四包加进来）即红。"""
    assert G.ALLOWED_RESEARCH_IMPORTERS == frozenset({"ev/shadow_bridge.py"})
    assert G.ALLOWED_BRIDGE_CALLERS == frozenset({"cli.py", "web/utils.py"})
    assert "ev.shadow_bridge" not in G.RESEARCH_MODULES   # 编排层不是研究模块
    # 编排层自己确实 import 了研究模块 ⇒ 白名单非空、且是它唯一被放行的理由
    src = G.bridge_source()
    assert "from ev import order_freeze" in src
    assert "from ev import prefix_shadow" in src


def test_real_production_paths_have_no_violation():
    bad = G.violations()
    assert bad == [], f"生产路径违例：{bad}"


# ---------------------------------------------------- ② 注入负向（纯函数）
def test_detects_research_import_in_four_packages():
    bad = G.check_text("prediction/engine.py", "from ev import prefix_shadow\n")
    assert bad and bad[0][1] == "ev.prefix_shadow"


def test_detects_bridge_import_in_four_packages():
    bad = G.check_text("data/loader.py", "from ev.shadow_bridge import freeze_after_predict\n")
    assert bad and bad[0][1] == "ev.shadow_bridge"


def test_allows_bridge_import_in_two_entrypoints():
    assert G.check_text("cli.py", "from ev.shadow_bridge import freeze_after_predict") == []
    assert G.check_text("web/utils.py", "from ev.shadow_bridge import freeze_after_predict") == []


def test_detects_research_import_even_in_cli():
    """cli.py 可调用编排层，但**不许**直接碰研究模块。"""
    bad = G.check_text("cli.py", "import ev.order_freeze")
    assert bad and bad[0][1] == "ev.order_freeze"


def test_detects_draw_schedule_import_in_production():
    bad = G.check_text("web/utils.py", "from ev import draw_schedule")
    assert bad and bad[0][1] == "ev.draw_schedule"


# ---------------------------------------------------- ③ 编排层自身不变量
def test_bridge_does_not_read_system_time():
    """用 AST 判读，不用 substring：docstring 里会**引用** `datetime.now()` 作说明，
    子串扫描会把披露文字误判成违规（本轮实踩）。"""
    import ast
    tree = ast.parse(G.bridge_source())
    hits = [n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("now", "today", "utcnow")]
    assert hits == [], f"编排层读了系统时间：{hits}（时间必须由调用方注入）"


def test_bridge_does_not_decide_ticket_count():
    """K 必须来自调用方，不许写死。"""
    src = G.bridge_source()
    assert "K: int" in src or "K=K" in src
    for bad in ("K = 1000", "K=1000", "K = 100", "K = 5"):
        assert bad not in src, f"编排层写死了注数：{bad}"


def test_bridge_uses_production_nature_only():
    """时间点性质只能取 PRODUCTION_NATURE —— 不许用回放模拟冒充生产。"""
    src = G.bridge_source()
    assert "PS.PRODUCTION_NATURE" in src
    assert "回放模拟" not in src


# --------------------------------------------- ④ 开关关闭 / 缺失 ⇒ 零副作用
def test_missing_switch_is_zero_side_effect(tmp_path):
    r = _freeze(tmp_path, switch={"启用": False, "原因": "开关文件不存在：x"})
    assert r["状态"] == "不适用"
    assert "不存在" in r["原因"]
    assert not (tmp_path / "snap").exists()
    assert not (tmp_path / "shadow.jsonl").exists()


def test_switch_off_by_real_path_is_zero_side_effect(tmp_path):
    """不传 switch：走真实 is_enabled()，conftest 已把 CONFIG_PATH 指到 tmp（文件不存在）。"""
    r = _freeze(tmp_path, switch=None)
    assert r["状态"] == "不适用"
    assert not (tmp_path / "snap").exists()


def test_corrupt_switch_is_treated_as_off(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{not json", encoding="utf-8")
    got = SB.is_enabled(p)
    assert got["启用"] is False and "不可用" in got["原因"]


def test_switch_true_but_only_literal_true(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"启用": "true"}), encoding="utf-8")
    assert SB.is_enabled(p)["启用"] is False      # 字符串 "true" 不算开启


# ---------------------------------------------------------------- 成功路径
def test_freeze_and_register_success(tmp_path):
    r = _freeze(tmp_path)
    assert r["状态"] == "已登记", r
    assert r["n"] == 3
    assert r["开奖时间"].startswith("2026-11-08T21:15")
    snaps = list((tmp_path / "snap").glob("*.json"))
    assert len(snaps) == 1
    snap = json.loads(snaps[0].read_text(encoding="utf-8"))
    assert snap["时间点性质"] == "生产出号（开奖前冻结）"
    assert snap["批次时间"] == BATCH
    assert snap["K"] == 3 and len(snap["票序"]) == 3

    lines = [json.loads(x) for x in
             (tmp_path / "shadow.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    assert len(lines) == 1
    row = lines[0]
    assert row["阶段"] == "登记" and row["n"] == 3
    # ★ 登记行不得含任何开奖读数
    for bad in ("开奖号码", "实得", "奖级", "命中"):
        assert bad not in row


def test_batch_time_after_draw_is_rejected_and_writes_nothing(tmp_path):
    r = _freeze(tmp_path, batch_time="2026-11-08T22:00:00",
                register_time="2026-11-08T22:00:00")
    assert r["状态"] == "失败"
    assert "不早于开奖时间" in r["原因"]
    assert not (tmp_path / "snap").exists()
    assert not (tmp_path / "shadow.jsonl").exists()


def test_mixed_timezone_is_rejected_not_raised(tmp_path):
    r = _freeze(tmp_path, batch_time="2026-11-08T12:00:00+08:00")
    assert r["状态"] == "失败"
    assert "时区" in r["原因"] or "口径" in r["原因"]


def test_renumber_gets_distinct_snapshot(tmp_path):
    a = _freeze(tmp_path, renumber=0)
    assert a["状态"] == "已登记", a
    b = _freeze(tmp_path, renumber=1, tickets=_tickets(3)[::-1])
    assert b["状态"] == "已登记", b
    assert a["票序哈希"] != b["票序哈希"]
    assert len(list((tmp_path / "snap").glob("*.json"))) == 2


def test_same_batch_is_idempotent(tmp_path):
    a = _freeze(tmp_path)
    assert a["状态"] == "已登记", a
    b = _freeze(tmp_path)
    assert b["状态"] == "已登记" and "幂等" in b["原因"]
    assert len(list((tmp_path / "snap").glob("*.json"))) == 1
    assert len((tmp_path / "shadow.jsonl").read_text(encoding="utf-8").splitlines()) == 1


def test_k_mismatch_is_failure_not_exception(tmp_path):
    r = _freeze(tmp_path, K=5)                # 票序 3 注、K=5
    assert r["状态"] == "失败" and "K=5" in r["原因"]
    assert not (tmp_path / "snap").exists()


def test_schedule_missing_is_not_pass(tmp_path):
    r = _freeze(tmp_path, schedule_path=tmp_path / "nope.json")
    assert r["状态"] == "失败"
    assert "缺失" in r["原因"]


def test_non_draw_day_is_failure(tmp_path):
    """非开奖日不能构造开奖时间（日历真源在 data/holiday.py）。"""
    r = _freeze(tmp_path, target_date=date(2026, 11, 9))   # 周一：双色球不开奖
    assert r["状态"] == "失败"
    assert "不是" in r["原因"]


def test_never_raises_on_broken_input(tmp_path):
    """任何异常都收敛成「失败」，绝不把异常甩给出号链路。"""
    r = _freeze(tmp_path, batch_time="不是时间")
    assert r["状态"] == "失败"
