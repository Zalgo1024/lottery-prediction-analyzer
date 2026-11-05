# -*- coding: utf-8 -*-
"""tests/test_prefix_shadow.py —— 影子验证接线层的契约与牙齿测试。

每道锁都要被**故意破坏一次**才算数：
  ① 回放快照 / 占位批次时间 / 缺开奖时间 / 批次时间晚于开奖，全部不得登记；
  ② 登记必须先于开奖（两层时间），结算不许事后补登记、不许换票、不许改期；
  ③ 只追加不回改，登记与结算两态共存于同一台账；
  ④ 空台账 / 缺判决读数 / 全 ∞ 门槛 ⇒ 一律「未开判」，0 期不是通过；
  ⑤ 分母 = 文件总数，坏文件也留在分母并写原因；
  ⑥ 门槛只从 verdict 取，本模块不另立裸数字；生产路径零 import。
"""
from __future__ import annotations

import importlib.util
import json
import math
import re
from pathlib import Path

import pytest

from ev import order_freeze as OF
from ev import prefix_curve as PC
from ev import prefix_shadow as PS

ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = ("证伪", "证否", "不可预测", "已证明公平", "公平随机", "写死")
LOT = "双色球"
PROD_TIME = "2026-09-14T10:00:00"
DRAW_TIME = "2026-09-15T21:00:00"
SETTLE_TIME = "2026-09-15T22:00:00"


def _snap(*, issue: str = "26127", K: int = 10, n_nature: str = PS.PRODUCTION_NATURE,
          batch_time: str = PROD_TIME, tamper: bool = False):
    tks = [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [i % 16 + 1]} for i in range(K)]
    s = OF.build_snapshot(
        lottery=LOT, issue=issue, tickets=tks,
        strategy_version="test/2026-11-05-A", seed=0, K=K,
        rule_version="ssq-2026-02", code_version=PC.CODE_VERSION,
        batch_time=batch_time, time_nature=n_nature,
        target={"kind": "grade", "grade": "四等"},
        experiment={"块编号": 0, "池种子": 0})
    if tamper:
        s["票序"][0]["红球"] = [9, 9, 9, 9, 9, 9]   # 不动票序哈希 ⇒ 双锁应抓到
    return s


def _verdict(*, cells=None, main_n: int = 3):
    if cells is None:
        cells = [
            {"n": main_n, "门槛": "≥5元", "臂": "existing_coverage", "n*_保守": 2.0},
            {"n": main_n, "门槛": "≥5元", "臂": "tier_coverage", "n*_保守": 3.0},
            {"n": main_n, "门槛": "≥四等", "臂": "existing_coverage",
             "n*_保守": float("inf")},
        ]
    return {"主口径n": main_n, "格": cells}


def _load_report_module():
    spec = importlib.util.spec_from_file_location(
        "prefix_shadow_report_under_test",
        ROOT / "scripts" / "prefix_shadow_report.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------- ① 闸门
def test_replay_snapshot_is_rejected_with_reason():
    """★ 414 份现有快照的性质：绝不能被当成开奖前冻结。"""
    s = _snap(n_nature=OF.build_snapshot.__kwdefaults__.get(
        "time_nature", "回放模拟（非生产批次时间）"))
    g = PS.gate_snapshot(s, draw_time=DRAW_TIME)
    assert g["合格"] is False
    assert any("时间点性质" in r for r in g["原因"])
    assert PS.REPLAY_NATURE in str(g["原因"])


def test_production_nature_is_strict_whitelist():
    assert PS.PRODUCTION_NATURE not in (PS.REPLAY_NATURE, "", None)
    assert PS.REPLAY_NATURE != PS.PRODUCTION_NATURE
    assert PS.gate_snapshot(_snap(), draw_time=DRAW_TIME)["合格"] is True
    # ★ 防漂移：回放性质常量必须等于 build_snapshot 的默认值。若 upstream 改了
    #   默认 time_nature 而这里没跟上，闸门会把新回放快照误当「性质已声明」放行，
    #   等于悄悄打开开奖前冻结的口子。
    default_nature = OF.build_snapshot.__kwdefaults__["time_nature"]
    assert PS.REPLAY_NATURE == default_nature
    assert default_nature != PS.PRODUCTION_NATURE


def test_placeholder_batch_time_is_rejected():
    s = _snap(batch_time="离线回放（未记录生产批次时间）")
    g = PS.gate_snapshot(s, draw_time=DRAW_TIME)
    assert g["合格"] is False
    assert any("批次时间" in r for r in g["原因"])


def test_missing_or_unparsable_draw_time_is_rejected_not_skipped():
    for dt in (None, "", "不是时间"):
        g = PS.gate_snapshot(_snap(), draw_time=dt)
        assert g["合格"] is False
        assert any("开奖时间" in r for r in g["原因"])


@pytest.mark.parametrize("bt", [DRAW_TIME, SETTLE_TIME])
def test_batch_time_not_strictly_before_draw_is_rejected(bt):
    g = PS.gate_snapshot(_snap(batch_time=bt), draw_time=DRAW_TIME)
    assert g["合格"] is False
    assert any("不早于开奖时间" in r for r in g["原因"])


def test_tampered_ticket_order_is_rejected_by_hash_double_lock():
    g = PS.gate_snapshot(_snap(tamper=True), draw_time=DRAW_TIME)
    assert g["合格"] is False
    assert any("校验" in r for r in g["原因"])


def test_register_rejects_n_outside_prereg_table_and_unknown_batch_kind(tmp_path):
    with pytest.raises(PS.PrefixShadowError, match="前缀档表"):
        PS.register_prefix(_snap(), n=2, draw_time=DRAW_TIME,
                           register_time=PROD_TIME, path=tmp_path / "s.jsonl")
    with pytest.raises(PS.PrefixShadowError, match="批次性质"):
        PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                           register_time=PROD_TIME, batch_kind="事后挑的批",
                           path=tmp_path / "s.jsonl")


def test_replay_cannot_be_registered_even_with_valid_draw_time(tmp_path):
    p = tmp_path / "s.jsonl"
    s = _snap(n_nature=PS.REPLAY_NATURE, batch_time="离线回放（未记录生产批次时间）")
    with pytest.raises(PS.PrefixShadowError, match="不具备开奖前冻结登记资格"):
        PS.register_prefix(s, n=3, draw_time=DRAW_TIME,
                           register_time=PROD_TIME, path=p)
    assert not p.exists(), "被拒的登记绝不许留下半行证据"


# ---------------------------------------------------------------- ② 两层时间与登记
def test_register_after_draw_is_rejected_as_post_hoc(tmp_path):
    """★ 批次时间早 ≠ 登记早：开奖后才写台账就是事后补登记。"""
    p = tmp_path / "s.jsonl"
    with pytest.raises(PS.PrefixShadowError, match="事后补登记"):
        PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                           register_time=SETTLE_TIME, path=p)
    assert not p.exists()


def test_register_requires_caller_supplied_time(tmp_path):
    with pytest.raises(PS.PrefixShadowError, match="登记时间"):
        PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                           register_time="", path=tmp_path / "s.jsonl")


def test_register_row_contains_no_draw_readouts(tmp_path):
    """登记行若有开奖读数，就无法自证「先登记、后开奖」。"""
    p = tmp_path / "s.jsonl"
    row = PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                             register_time=PROD_TIME, path=p)
    for bad in ("实得", "开奖号码", "中奖等级", "达标注数", "成本"):
        assert bad not in row, f"登记行混入开奖读数 {bad}"
    assert row["阶段"] == PS.KIND_REGISTER
    assert row["注数"] == 3
    assert row["前缀票序哈希"] == OF.digest(OF.prefix_of(_snap(), 3))


def test_register_is_append_only_same_key_rejected(tmp_path):
    p = tmp_path / "s.jsonl"
    PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                       register_time=PROD_TIME, path=p)
    with pytest.raises(PS.PrefixShadowError, match="拒绝回改"):
        PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                           register_time=PROD_TIME, path=p)
    assert len(PS.load_shadow(p)["行"]) == 1


def test_old_shadow_rows_without_stage_still_dedup(tmp_path):
    """扩展键后旧格式行的去重行为不变（向后兼容）。"""
    p = tmp_path / "s.jsonl"
    rec = {"期号": "26127", "冻结版本": PC.PREREG_VERSION,
           "策略哈希": "abc", "n": 3, "读数": 1}
    assert PC.append_shadow(rec, path=p) is True
    assert PC.append_shadow(dict(rec, 读数=999), path=p) is False
    assert len(PS.load_shadow(p)["行"]) == 1


# ---------------------------------------------------------------- ③ 结算
def test_register_and_settle_coexist_as_two_stages(tmp_path):
    p = tmp_path / "s.jsonl"
    reg = PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                             register_time=PROD_TIME, path=p)
    st = PS.settle_prefix(reg, result={"实得": 10.0, "成本": 6.0, "达标注数": 1},
                          draw_time=DRAW_TIME, settle_time=SETTLE_TIME, path=p)
    rows = PS.load_shadow(p)["行"]
    assert len(rows) == 2 and st["阶段"] == PS.KIND_SETTLE
    assert {r["阶段"] for r in rows} == {PS.KIND_REGISTER, PS.KIND_SETTLE}
    assert rows[0]["票序哈希"] == rows[1]["票序哈希"], "结算不许换票"


def test_settle_without_register_is_rejected(tmp_path):
    p = tmp_path / "s.jsonl"
    reg = PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                             register_time=PROD_TIME, path=p)
    ghost = dict(reg, 期号="99999")
    with pytest.raises(PS.PrefixShadowError, match="不许事后补登记"):
        PS.settle_prefix(ghost, result={"实得": 0.0}, draw_time=DRAW_TIME,
                         settle_time=SETTLE_TIME, path=p)


def test_settle_cannot_swap_ticket_order(tmp_path):
    p = tmp_path / "s.jsonl"
    reg = PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                             register_time=PROD_TIME, path=p)
    with pytest.raises(PS.PrefixShadowError, match="不许换票"):
        PS.settle_prefix(dict(reg, 票序哈希="改过的票序"), result={"实得": 0.0},
                         draw_time=DRAW_TIME, settle_time=SETTLE_TIME, path=p)


def test_settle_before_draw_is_rejected_as_lookahead(tmp_path):
    p = tmp_path / "s.jsonl"
    reg = PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                             register_time=PROD_TIME, path=p)
    with pytest.raises(PS.PrefixShadowError, match="前视"):
        PS.settle_prefix(reg, result={"实得": 0.0}, draw_time=DRAW_TIME,
                         settle_time=PROD_TIME, path=p)


def test_settle_cannot_change_draw_time(tmp_path):
    p = tmp_path / "s.jsonl"
    reg = PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                             register_time=PROD_TIME, path=p)
    with pytest.raises(PS.PrefixShadowError, match="不许改期"):
        PS.settle_prefix(reg, result={"实得": 0.0}, draw_time="2026-09-16T21:00:00",
                         settle_time=SETTLE_TIME, path=p)


def test_settle_is_append_only(tmp_path):
    p = tmp_path / "s.jsonl"
    reg = PS.register_prefix(_snap(), n=3, draw_time=DRAW_TIME,
                             register_time=PROD_TIME, path=p)
    kw = dict(result={"实得": 5.0}, draw_time=DRAW_TIME, settle_time=SETTLE_TIME,
              path=p)
    PS.settle_prefix(reg, **kw)
    with pytest.raises(PS.PrefixShadowError, match="拒绝回改"):
        PS.settle_prefix(reg, **dict(kw, result={"实得": 99999.0}))
    assert len(PS.load_shadow(p)["行"]) == 2


# ---------------------------------------------------------------- ④ 状态判据
def test_empty_ledger_is_not_open_and_not_pass(tmp_path):
    s = PS.shadow_status(verdict=_verdict(), path=tmp_path / "nope.jsonl")
    assert s["状态"] == "未开判" and s["可开判"] is False
    assert s["登记期数"] == 0 and s["结算期数"] == 0
    reasons = " ".join(s["未开判原因"])
    assert "登记 0 期" in reasons and "结算 0 期" in reasons
    assert "独立评估批" in reasons
    assert "缺席" in " ".join(s["说明"])


def test_missing_verdict_is_explicit_not_silent_zero(tmp_path):
    s = PS.shadow_status(verdict={}, path=tmp_path / "nope.jsonl")
    assert s["可开判"] is False
    assert s["门槛"]["可用"] is False
    assert any("缺席" in r or "未提供" in r for r in s["未开判原因"])
    assert s["还需期数"] is None


def test_all_infinite_gate_is_theoretically_unjudgeable():
    g = PS.opening_gate(_verdict(cells=[
        {"n": 3, "门槛": "≥四等", "臂": "a", "n*_保守": float("inf")}]))
    assert g["可用"] is False and g["不可判格数"] == 1
    assert g["门槛期数"] is None
    assert any("理论不可判" in r for r in g["原因"])


def test_gate_comes_from_verdict_not_hardcoded(tmp_path, monkeypatch):
    """门槛必须随 verdict 变；本模块不许另立一套裸数字。"""
    s1 = PS.shadow_status(verdict=_verdict(), path=tmp_path / "n.jsonl")
    big = _verdict(cells=[
        {"n": 3, "门槛": "≥5元", "臂": "a", "n*_保守": 987.4}])
    s2 = PS.shadow_status(verdict=big, path=tmp_path / "n.jsonl")
    assert s1["门槛"]["门槛期数"] == 3
    assert s2["门槛"]["门槛期数"] == 988
    assert s2["还需期数"] > s1["还需期数"]


def test_status_is_open_only_when_eval_batch_reaches_gate(tmp_path):
    p = tmp_path / "s.jsonl"
    v = _verdict()
    for i in range(2):
        snap = _snap(issue=f"2612{i}")
        reg = PS.register_prefix(snap, n=3, draw_time=DRAW_TIME,
                                 register_time=PROD_TIME,
                                 batch_kind=PS.BATCH_EVAL, path=p)
        PS.settle_prefix(reg, result={"实得": float(i)}, draw_time=DRAW_TIME,
                         settle_time=SETTLE_TIME, path=p)
    s = PS.shadow_status(verdict=v, path=p)
    assert s["可开判"] is False, "2 期 < 门槛 3 期"
    assert s["独立评估批"]["结算期数"] == 2
    snap = _snap(issue="26129")
    reg = PS.register_prefix(snap, n=3, draw_time=DRAW_TIME,
                             register_time=PROD_TIME,
                             batch_kind=PS.BATCH_EVAL, path=p)
    PS.settle_prefix(reg, result={"实得": 9.0}, draw_time=DRAW_TIME,
                     settle_time=SETTLE_TIME, path=p)
    s2 = PS.shadow_status(verdict=v, path=p)
    assert s2["可开判"] is True
    assert s2["状态"].startswith("可开判")
    assert "另行判决" in s2["状态"], "开判≠本层代做 BH 判决"


def test_explore_batch_never_counts_as_confirmation(tmp_path):
    p = tmp_path / "s.jsonl"
    for i in range(9):
        snap = _snap(issue=f"2613{i}")
        reg = PS.register_prefix(snap, n=3, draw_time=DRAW_TIME,
                                 register_time=PROD_TIME,
                                 batch_kind=PS.BATCH_EXPLORE, path=p)
        PS.settle_prefix(reg, result={"实得": 0.0}, draw_time=DRAW_TIME,
                         settle_time=SETTLE_TIME, path=p)
    s = PS.shadow_status(verdict=_verdict(), path=p)
    assert s["独立评估批"]["结算期数"] == 0
    assert s["可开判"] is False


def test_bad_rows_are_counted_not_dropped(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text("{坏行\n", encoding="utf-8")
    d = PS.load_shadow(p)
    assert d["坏行数"] == 1 and d["行数"] == 0
    s = PS.shadow_status(verdict=_verdict(), path=p)
    assert any("坏记录" in r for r in s["未开判原因"])


def test_status_never_does_bh_or_writes_detection(tmp_path):
    s = PS.shadow_status(verdict=_verdict(), path=tmp_path / "n.jsonl")
    blob = json.dumps(s, ensure_ascii=False)
    assert "BH" not in json.dumps(s["未开判原因"], ensure_ascii=False)
    assert "检出" not in json.dumps(s["未开判原因"], ensure_ascii=False)
    assert s["门槛"]["有限格数"] == 2
    assert "p_BH" not in blob


# ---------------------------------------------------------------- ⑤ 快照体检分母
def test_audit_keeps_every_file_in_denominator(tmp_path):
    d = tmp_path / "freeze"
    d.mkdir()
    for i in range(3):
        (d / f"ok{i}.json").write_text(
            json.dumps(_snap(issue=f"2614{i}"), ensure_ascii=False), encoding="utf-8")
    (d / "bad.json").write_text("{不是 json\n", encoding="utf-8")
    (d / "replay.json").write_text(
        json.dumps(_snap(issue="26149", n_nature=PS.REPLAY_NATURE,
                         batch_time="离线回放（未记录生产批次时间）"),
                   ensure_ascii=False), encoding="utf-8")
    a = PS.audit_freeze_dir(d)
    assert a["文件总数"] == 5 and a["合格数"] == 3 and a["不合格数"] == 2
    assert len(a["逐份"]) == 5, "不许筛掉后缩分母"
    assert any("解析失败" in " ".join(r["原因"]) for r in a["逐份"])
    assert a["分母口径"].startswith("文件总数")


def test_audit_and_gate_share_one_reason_source(tmp_path, monkeypatch):
    """改白名单两边必须一起变，不许体检与登记各写一份判定。"""
    d = tmp_path / "freeze"
    d.mkdir()
    (d / "a.json").write_text(json.dumps(_snap(), ensure_ascii=False),
                              encoding="utf-8")
    assert PS.audit_freeze_dir(d)["合格数"] == 1
    monkeypatch.setattr(PS, "PRODUCTION_NATURE", "另一种生产标记")
    assert PS.audit_freeze_dir(d)["合格数"] == 0
    assert PS.gate_snapshot(_snap(), draw_time=DRAW_TIME)["合格"] is False


def test_real_freeze_pool_all_replay_and_zero_eligible():
    """★ 现场事实：现有冻结快照池全部是回放模拟，合格 0（缺席≠通过）。

    显式传真实目录：conftest 已把模块级 FREEZE_DIR 重定向到 tmp（隔离生效），
    测试不许依赖被隔离的默认值去读真身。
    """
    a = PS.audit_freeze_dir(ROOT / "logs" / "order_freeze")
    assert a["文件总数"] > 0
    assert a["合格数"] == 0
    assert a["不合格数"] == a["文件总数"]
    assert set(a["时间点性质分布"]) == {PS.REPLAY_NATURE}


# ---------------------------------------------------------------- ⑥ 报告与接线
def test_report_echoes_prereg_and_notes(tmp_path):
    mod = _load_report_module()
    a = PS.audit_freeze_dir(tmp_path / "no_such_dir")
    s = PS.shadow_status(verdict=_verdict(), path=tmp_path / "n.jsonl")
    text = mod.render(a, s, verdict_path=ROOT / "logs" / "v.json",
                      verdict=_verdict())
    for key in ("预注册", "反事实", "非实际购买", "未开判", "分母",
                "开奖前冻结", "接线状态", "还需期数", "不构成证明"):
        assert key in text, f"报告漏了 {key}"


def test_report_tables_have_consistent_column_counts(tmp_path):
    """★ 上一轮实踩：手数 `---` 段数少了会撑破表格。"""
    mod = _load_report_module()
    a = PS.audit_freeze_dir(tmp_path / "no_such_dir")
    s = PS.shadow_status(verdict=_verdict(), path=tmp_path / "n.jsonl")
    lines = mod.render(a, s, verdict_path=ROOT / "logs" / "v.json",
                       verdict=_verdict()).splitlines()
    for i, line in enumerate(lines):
        if re.fullmatch(r"\|(?:---\|)+", line.strip()):
            ncols = len(line.strip().strip("|").split("|"))
            assert len(lines[i - 1].strip().strip("|").split("|")) == ncols
            for j in range(i + 1, len(lines)):
                if not lines[j].strip().startswith("|"):
                    break
                assert len(lines[j].strip().strip("|").split("|")) == ncols


def test_report_cell_never_contains_bare_pipe():
    mod = _load_report_module()
    assert mod._cell("a|b") == "a／b"
    assert mod._sep(3) == "|---|---|---|"
    with pytest.raises(AssertionError):
        mod._table(["a", "b"], [["only-one"]])


def test_report_build_does_not_write_ledger(tmp_path):
    """状态报告只读：跑一次报告不许凭空造出影子台账。"""
    mod = _load_report_module()
    shadow = tmp_path / "shadow.jsonl"
    out = tmp_path / "out.json"
    data = mod.build(verdict_path=ROOT / "logs" / "v.json",
                     freeze_dir=tmp_path / "no_dir", shadow_path=shadow)
    assert not shadow.exists()
    assert data["影子状态"]["可开判"] is False
    assert data["报告"] and data["快照体检"]["文件总数"] == 0
    assert out.parent.exists()


def test_missing_verdict_file_is_reported_not_defaulted(tmp_path):
    mod = _load_report_module()
    v = mod._load_verdict(tmp_path / "missing.json")
    assert "_缺失" in v
    a = PS.audit_freeze_dir(tmp_path / "no_dir")
    s = PS.shadow_status(verdict=v, path=tmp_path / "n.jsonl")
    assert s["可开判"] is False
    text = mod.render(a, s, verdict_path=tmp_path / "missing.json", verdict=v)
    assert "判决读数文件不存在" in text and "缺席≠通过" in text


# ---------------------------------------------------------------- ⑦ 纪律护栏
def test_offline_modules_not_imported_by_production():
    targets = [ROOT / d for d in ("prediction", "web", "pipeline", "data")]
    targets += [ROOT / "run.py", ROOT / "cli.py"]
    for base in targets:
        files = [base] if base.is_file() else list(base.rglob("*.py"))
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            for bad in ("ev.prefix_shadow", "from ev import prefix_shadow"):
                assert bad not in text, f"生产路径 {f.name} 引入了影子接线层：{bad}"


def test_forbidden_words_in_new_modules():
    for name in ("ev/prefix_shadow.py", "scripts/prefix_shadow_report.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for bad in FORBIDDEN:
            assert bad not in text, f"{name} 出现禁词 {bad}"


def test_module_does_not_read_system_time():
    """两层时间都由调用方给，模块内不许读系统时间（便于回放与单测）。"""
    for name in ("ev/prefix_shadow.py",):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "datetime.now(" not in text
        assert "date.today(" not in text


def test_no_hardcoded_production_ticket_count():
    """生产生效注数会漂移：离线模块不许固定任何具体生产注数。"""
    for name in ("ev/prefix_shadow.py", "scripts/prefix_shadow_report.py"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert "K=1000" not in text
        assert "1000 注" not in text


def test_conftest_isolates_new_writable_paths():
    text = (ROOT / "conftest.py").read_text(encoding="utf-8")
    assert '("ev.prefix_shadow", "SHADOW_PATH"' in text
    assert '("ev.prefix_shadow", "FREEZE_DIR"' in text


def test_shadow_key_includes_stage_without_breaking_legacy():
    a = {"期号": "1", "冻结版本": "v", "策略哈希": "h", "n": 3}
    assert PC._shadow_key(a) == PC._shadow_key(dict(a, 阶段=""))
    assert PC._shadow_key(dict(a, 阶段=PS.KIND_REGISTER)) != \
           PC._shadow_key(dict(a, 阶段=PS.KIND_SETTLE))
