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

from pathlib import Path

from ev import order_freeze as OF
from ev import prefix_shadow as PS
from ev import shadow_bridge as SB
from tests import _import_guard as G

ROOT = Path(__file__).resolve().parents[1]
ROOT_RULES = ROOT / "config" / "prefix_shadow_rules.json"

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
              # 默认给一个**不存在**的规则文件 ⇒ 一律探索批，避免用例依赖现场规则
              rules_path=tmp_path / "no_rules.json",
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


# ------------------------------------------- 批次性质（必须事先锁定，不能事后挑）
def test_batch_kind_without_rules_is_explore(tmp_path):
    """缺规则 ⇒ 探索批。保守方向：探索批不参与判决，绝不会被误算成证据。"""
    got = SB.batch_kind_for(BATCH, {"评估批起始日期": None, "_缺失": True,
                                    "_路径": str(tmp_path / "nope.json")})
    assert got["性质"] == PS.BATCH_EXPLORE
    assert "不存在" in got["依据"]


def test_batch_kind_before_start_is_explore():
    got = SB.batch_kind_for("2026-11-20T12:00:00",
                            {"评估批起始日期": "2026-12-01", "版本": "t"})
    assert got["性质"] == PS.BATCH_EXPLORE and "探索期" in got["依据"]


def test_batch_kind_on_or_after_start_is_eval():
    got = SB.batch_kind_for("2026-12-01T00:00:00",
                            {"评估批起始日期": "2026-12-01", "版本": "t"})
    assert got["性质"] == PS.BATCH_EVAL
    got2 = SB.batch_kind_for("2027-01-05T12:00:00",
                             {"评估批起始日期": "2026-12-01", "版本": "t"})
    assert got2["性质"] == PS.BATCH_EVAL


def test_batch_kind_unparseable_start_is_explore():
    """起始日期写坏 ⇒ 探索批（不 fail-open 成评估批）。"""
    got = SB.batch_kind_for(BATCH, {"评估批起始日期": "下个月"})
    assert got["性质"] == PS.BATCH_EXPLORE
    assert "不可解析" in got["依据"]


def test_batch_kind_uses_injected_time_not_system_clock():
    """判定只看注入的 batch_time —— 同一条规则下改时间就能翻转结论。"""
    rules = {"评估批起始日期": "2026-12-01", "版本": "t"}
    assert SB.batch_kind_for("2026-11-30T23:59:59", rules)["性质"] == PS.BATCH_EXPLORE
    assert SB.batch_kind_for("2026-12-01T00:00:00", rules)["性质"] == PS.BATCH_EVAL


def test_real_rules_file_is_locked_and_has_start():
    """现场规则文件必须存在、锁定、且给出起始日期 —— 否则评估批永远攒不起来。"""
    rules = SB.load_rules(ROOT_RULES)
    assert not rules.get("_缺失"), "config/prefix_shadow_rules.json 不存在"
    assert rules.get("锁定") is True
    assert rules.get("评估批起始日期"), "缺「评估批起始日期」⇒ 全按探索批，永不可开判"


def test_freeze_records_batch_kind_in_snapshot_and_ledger(tmp_path):
    """冻结时必须把批次性质同时写进快照备注与台账登记行。"""
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps({"版本": "t", "评估批起始日期": "2026-11-01",
                                 "锁定": True}, ensure_ascii=False), encoding="utf-8")
    r = _freeze(tmp_path, rules_path=rules)
    assert r["状态"] == "已登记", r
    assert r["批次性质"] == PS.BATCH_EVAL
    snap = json.loads(next((tmp_path / "snap").glob("*.json")).read_text(encoding="utf-8"))
    assert snap["备注"]["批次性质"] == PS.BATCH_EVAL
    assert snap["备注"]["批次规则版本"] == "t"
    row = json.loads((tmp_path / "shadow.jsonl").read_text(
        encoding="utf-8").splitlines()[0])
    assert row["批次性质"] == PS.BATCH_EVAL


def test_freeze_without_rules_registers_explore(tmp_path):
    """规则缺失时冻结照常进行，但只能登记为探索批（不冒充评估批）。"""
    r = _freeze(tmp_path, rules_path=tmp_path / "nope.json")
    assert r["状态"] == "已登记", r
    assert r["批次性质"] == PS.BATCH_EXPLORE
    assert "不存在" in r["批次性质依据"]


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


# ------------------------------ 方案 B 历史证据必须真的进判据（不是采了没人读）
def _history_evidence(tmp_path, observed):
    from ev import draw_evidence as DE
    p = tmp_path / "evidence.jsonl"
    DE.append_evidence({
        "版本": DE.EVIDENCE_VERSION,
        "彩种": "双色球", "期号": "26127", "开奖日期": "2026-11-08",
        "来源": "https://example.invalid/history",
        "抓取时间": observed, "首次可观测时刻": observed,
        "开奖号码": {"原始CSV行": ["26127"]},
    }, path=p)
    return p


def test_historical_evidence_contradiction_blocks_registration(tmp_path):
    """★ 牙齿：历史期结果在声明值 21:15 之前可见 ⇒ 时刻表不可信 ⇒ 本期拒登。"""
    p = _history_evidence(tmp_path, "2026-11-08T21:10:00")
    r = _freeze(tmp_path, evidence_path=p)
    assert r["状态"] == "失败" and "矛盾" in r["原因"]
    assert not (tmp_path / "shadow.jsonl").exists(), "拒登时绝不写台账"


def test_historical_evidence_without_contradiction_still_registers(tmp_path):
    """反向：不能因为接了证据就把所有登记都误杀。"""
    p = _history_evidence(tmp_path, "2026-11-08T21:20:00")
    r = _freeze(tmp_path, evidence_path=p)
    assert r["状态"] == "已登记", r
    assert any("历史期证据参与" in d for d in r["披露"])
