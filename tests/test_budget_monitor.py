# -*- coding: utf-8 -*-
"""tests/test_budget_monitor.py —— 预算只读决策表的牙齿护栏。

锁四件事：
  ① 推演公式与真实 `ev.budget_gate.clamp()` 对账（把 min 改成 max / 删掉
     max(0, ...) 都会红），不是「自己跟自己比」；
  ② 额度用尽时逐轮推演必须写「全停发」，不许把 0 期当成通过；
  ③ 脚本**绝不写任何文件**（不写 config、不写台账、不落报告到 logs/）；
  ④ 报告诚实性：闸门不可用时写「不适用/未参与」，表格列数一致，
     档位由参数决定（不预设目标值），满月估算走 draw_days 单一真源。

conftest 的 `_isolate_config_stores` 已把 `ev.budget_gate.LIMIT_PATH /
SPEND_PATH` 与 `data.ticket_size.STORE_PATH` 指向 tmp_path，用例不碰真身。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from config import LOTTERY_CONFIG, ticket_price
from data import ticket_size as TS
from ev import budget_gate as BG
from scripts import budget_monitor as BM


# ------------------------------------------------------------------ 夹具
def _seed(*, monthly: float = 3000.0, cap: float = 2000.0,
          spent: float = 0.0, enabled: bool = True,
          dynamic: int = 50) -> None:
    """往 tmp 写三份状态文件（conftest 已把路径常量重定向到 tmp）。"""
    lim = {"版本": 1, "启用": enabled, "月度总额": monthly,
           "单期上限": cap, "锁定": True}
    Path(BG.LIMIT_PATH).write_text(json.dumps(lim, ensure_ascii=False), encoding="utf-8")
    BG._LIMIT_CACHE.update({"key": None, "doc": None})   # 缓存键含 mtime，写后必须清

    # ★ 期号故意与 clamp() 调用用的 "TEST-1" 错开：同期同键会触发重出批回补
    #   （_rebate_amount 把那笔钱退回可用额度），而本脚本推演按「每轮首出批、
    #   不含回补」走。错开后 rebate=0，两边才是同一口径，见下条差异声明用例。
    rec = {"彩种": "双色球", "目标期号": "OTHER-1", "档位": "一般",
           "台账域": "生产", "注数": 1, "金额": float(spent), "单价": 2.0}
    doc = {"版本": 2, "月份": BG._month_of(), "记录": [rec] if spent else [],
           "已投": float(spent), "停发": []}
    Path(BG.SPEND_PATH).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    lots = list(LOTTERY_CONFIG)
    cfg = {"mode": "dynamic", "fixed": {l: dynamic for l in lots},
           "dynamic": {l: dynamic for l in lots},
           "current": {l: dynamic for l in lots},
           "limits": {l: 1000 for l in lots}, "锁定": True}
    Path(TS.STORE_PATH).write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")


def _report(capsys, *args: str) -> str:
    assert BM.main(list(args)) == 0
    return capsys.readouterr().out


def _table_columns_are_consistent(md: str) -> None:
    """机械核对每张表：分隔行列数 == 表头列数 == 每个数据行列数。"""
    lines = md.splitlines()
    seps = [i for i, l in enumerate(lines)
            if re.fullmatch(r"\|(?:---\|)+", l.strip())]
    assert seps, "报告里一张表格都没有"
    for i in seps:
        n = len(lines[i].strip().strip("|").split("|"))
        assert len(lines[i - 1].strip().strip("|").split("|")) == n, \
            f"第 {i+1} 行表头列数 != 分隔行列数 {n}"
        for j in range(i + 1, len(lines)):
            if not lines[j].strip().startswith("|"):
                break
            assert len(lines[j].strip().strip("|").split("|")) == n, \
                f"第 {j+1} 行数据列数 != {n}"


# ------------------------------------------------------------------ ① 对账
def test_projection_matches_real_clamp():
    """★ 推演公式必须与真实闸门逐点相等。

    故意破坏方式：把 `clamp_projection` 里的 `min(...)` 改成 `max(...)`，
    或删掉 `max(0.0, ...)` ⇒ 下面第一对（额度足够/额度不足）立刻不相等。
    """
    _seed(monthly=3000.0, cap=2000.0, spent=1000.0)
    snap = BM.snapshot()
    assert snap["可用"] is True
    lot = snap["彩种"][0]
    price = snap["单价"][lot]
    cap = snap["单期上限"]
    for want, spent in ((10, 0.0), (5000, 0.0), (10, 2999.0), (5000, 2999.0)):
        # 真实闸门：临时台账里「已投」改成 spent，走 manual 来源（不夹基线）
        _seed(monthly=3000.0, cap=2000.0, spent=spent)
        real = BG.clamp(lot, want, source="manual", 目标期号="TEST-1")["注数"]
        mine = BM.clamp_projection(want, remaining=max(0.0, 3000.0 - spent),
                                   price=price, cap=cap)
        assert mine == real, \
            f"want={want} spent={spent}：推演 {mine} != 真实闸门 {real}"
    # 负剩余不许出负注数（删掉 max(0.0, ...) 会在 2999 那组露出来）
    assert BM.clamp_projection(10, remaining=-100.0, price=price, cap=cap) == 0


def test_rebate_difference_is_explicitly_declared(capsys):
    """真实闸门在「同期同键重出」时回补额度；推演不含回补 ⇒ 差异必须写进报告。

    这不是 bug：推演逐轮都是新一期（首出批），回补不会发生。但若有人删掉报告
    里的口径声明，后来者会把两种口径的读数直接对比，误判闸门算错。
    """
    _seed(monthly=3000.0, cap=2000.0, spent=2999.0)
    lot = list(LOTTERY_CONFIG)[0]
    # 同期同键 ⇒ 真实闸门把 2999 元退回，出号比推演宽松
    with_same_key = {"彩种": lot, "目标期号": "TEST-1", "档位": "一般",
                     "台账域": "生产", "注数": 1, "金额": 2999.0, "单价": 2.0}
    doc = {"版本": 2, "月份": BG._month_of(), "记录": [with_same_key],
           "已投": 2999.0, "停发": []}
    Path(BG.SPEND_PATH).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    real = BG.clamp(lot, 10, source="manual", 目标期号="TEST-1")["注数"]
    mine = BM.clamp_projection(10, remaining=1.0,
                               price=float(ticket_price(lot)), cap=2000.0)
    assert real > mine, "同期回补应当让真实闸门比推演宽松"
    md = _report(capsys, "--rounds", "1")
    assert "不含重出批回补" in md, "报告必须显式声明推演不含回补"


def test_broken_projection_is_detected_by_the_reconciliation():
    """把推演换成「永远放行」的坏实现，对账断言必须抓到（证明护栏有牙）。"""
    _seed(monthly=3000.0, cap=2000.0, spent=2999.0)
    lot = list(LOTTERY_CONFIG)[0]
    real = BG.clamp(lot, 5000, source="manual", 目标期号="TEST-1")["注数"]
    broken = (lambda want, **kw: int(want))
    assert broken(5000) != real, "坏实现没被对账抓到 ⇒ 对账用例失去牙齿"


# ------------------------------------------------------------------ ② 停发
def test_exhausted_budget_every_round_is_stopped(capsys):
    _seed(monthly=3000.0, cap=2000.0, spent=3000.0)
    snap = BM.snapshot()
    rows = BM.project_rounds(snap, rounds=3)
    assert len(rows) == 3
    assert all(r["本轮注数"] == 0 and r["本轮全停发"] for r in rows)
    md = _report(capsys, "--rounds", "3")
    assert "本轮全停发" in md and "不适用" not in md.split("## 3.")[1].split("## 4.")[0]
    assert "停发" in md


def test_partial_remaining_clamps_later_lotteries(capsys):
    """剩余不够一整轮：前段彩种出满、后段被夹到 0，且报告显式写原因。"""
    _seed(monthly=3000.0, cap=2000.0, spent=2900.0)
    md = _report(capsys, "--rounds", "2")
    assert "不够一整轮" in md
    rows = BM.project_rounds(BM.snapshot(), rounds=1)
    per = rows[0]["逐彩种注数"]
    assert per[list(LOTTERY_CONFIG)[0]] > 0
    assert per[list(LOTTERY_CONFIG)[-1]] == 0


# ------------------------------------------------------------------ ③ 只读
def test_script_writes_no_file(tmp_path, capsys):
    """★ 跑完脚本，tmp 目录的文件集合与内容都必须逐字节不变。"""
    _seed(monthly=3000.0, cap=2000.0, spent=1000.0)
    before = {p.name: p.read_bytes() for p in Path(tmp_path).rglob("*") if p.is_file()}
    _report(capsys, "--rounds", "4")
    after = {p.name: p.read_bytes() for p in Path(tmp_path).rglob("*") if p.is_file()}
    assert after == before, "脚本写盘了（只读纪律被破坏）"


def test_module_has_no_hardcoded_production_budget_numbers():
    """额度/台账读数必须从真源读，不许在脚本里落具体绝对值。"""
    text = Path(BM.__file__).read_text(encoding="utf-8")
    for bad in ("61330", "50228", "344000", "480000", "11102", "16000"):
        assert bad not in text, f"脚本里出现生产裸数字 {bad}"


# ------------------------------------------------------------------ ④ 报告诚实性
def test_disabled_gate_reports_not_applicable_without_fabricating_zero(capsys):
    """缺席≠通过：闸门未启用就写「不适用」+「未参与」，不伪造推演表。"""
    _seed(enabled=False)
    snap = BM.snapshot()
    assert snap["可用"] is False
    md = _report(capsys, "--rounds", "3")
    assert "不适用" in md and "未参与" in md and "缺席≠通过" in md
    assert "## 3." not in md, "闸门不可用时不应输出逐轮推演（0 期不是通过）"


def test_report_tables_and_honesty_notes(capsys):
    _seed(monthly=3000.0, cap=2000.0, spent=1000.0)
    md = _report(capsys, "--rounds", "3")
    _table_columns_are_consistent(md)
    for note in ("反事实", "未写 config", "月度累计口径", "不含推荐值", "目标值由使用者拍板"):
        assert note in md, f"报告缺少口径声明：{note}"


def test_multiples_are_parameters_not_preset_targets(capsys):
    """档位由 --multiples 决定；不同参数产生不同行数（不预设目标值）。"""
    _seed(monthly=3000.0, cap=2000.0, spent=1000.0)
    md2 = _report(capsys, "--multiples", "1,2")
    md4 = _report(capsys, "--multiples", "1,1.5,2,3")
    sec2 = md2.split("## 4.")[1].split("## 5.")[0]
    sec4 = md4.split("## 4.")[1].split("## 5.")[0]
    n2 = sum(1 for l in sec2.splitlines() if re.match(r"^\|\s*[\d.]+\s*\|", l))
    n4 = sum(1 for l in sec4.splitlines() if re.match(r"^\|\s*[\d.]+\s*\|", l))
    # 两档都额外追加「满月口径档」⇒ 差 2 行
    assert n4 - n2 == 2, f"档位不是由参数决定：{n2} vs {n4}"
    assert "只作口径对照" in sec4 and "不是推荐值" in sec4


def test_full_month_estimate_tracks_draw_days(capsys):
    """★ 满月估算走 `draw_days` 单一真源：改天数参数会变，逐彩种月期数与周天数一致。"""
    _seed(monthly=3000.0, cap=2000.0, spent=1000.0)
    t30 = BM.decision_table(BM.snapshot(), multiples=(1.0,), days_in_month=30)
    t14 = BM.decision_table(BM.snapshot(), multiples=(1.0,), days_in_month=14)
    assert t30["满月按开奖日历估算"] > t14["满月按开奖日历估算"]
    assert t30["满月每日开上界"] > t14["满月每日开上界"]
    for lot, n in t30["逐彩种月期数"].items():
        weekdays = len((LOTTERY_CONFIG.get(lot) or {}).get("draw_days") or [])
        assert n == round(weekdays * 30 / 7.0), f"{lot} 月期数没走 draw_days"
    assert t30["满月按开奖日历估算"] != 0 and t30["满月每日开上界"] != 0


def test_single_period_cap_binding_is_detected(capsys):
    """单期上限小于生效注数 × 单价 ⇒ 必须报「构成约束」，不许静默。"""
    _seed(monthly=3000.0, cap=20.0, spent=0.0)   # n_cap = 10 注 < 生效 50 注
    snap = BM.snapshot()
    table = BM.decision_table(snap, multiples=(1.0,), days_in_month=30)
    assert table["单期上限构成约束的彩种"], "cap 明显binding却没被检出"
    md = _report(capsys, "--rounds", "2")
    assert "构成约束" in md and "须另行拍板" in md


def test_projection_never_exceeds_want():
    """闸门只降不升：推演注数永远 ≤ 请求注数。"""
    _seed(monthly=3000.0, cap=2000.0, spent=0.0)
    snap = BM.snapshot()
    for lot in snap["彩种"]:
        want = snap["生效注数"][lot]
        for left in (0.0, 1.0, 999.0, 3000.0, 1e9):
            n = BM.clamp_projection(want, remaining=left,
                                    price=snap["单价"][lot], cap=snap["单期上限"])
            assert 0 <= n <= want, f"{lot} left={left} ⇒ {n} 超出 [0,{want}]"


def test_price_uses_single_source():
    """单价取自 config.ticket_price，不许脚本自己另造一份。"""
    _seed()
    snap = BM.snapshot()
    for lot in snap["彩种"]:
        assert snap["单价"][lot] == float(ticket_price(lot))
