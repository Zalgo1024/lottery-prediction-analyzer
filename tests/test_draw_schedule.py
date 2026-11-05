# -*- coding: utf-8 -*-
"""ev/draw_schedule.py 的牙齿测试（方案 A 前置准备件，全离线、零生产调用点）。

夹具里的 "12:34:56" 是**合成时刻**，仅用于验证解析与组装逻辑，
不代表任何真实开奖时刻，也不写进任何生产配置。
"""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from config import LOTTERY_CONFIG
from ev import draw_schedule as DS

ROOT = Path(__file__).resolve().parent.parent
FAKE_CLOCK = "12:34:56"


def _full_doc():
    """完整但全为合成值的时刻表（彩种与星期仍走真源）。"""
    doc = DS.input_template()
    for name in doc["彩种"]:
        doc["彩种"][name]["开奖时刻"] = FAKE_CLOCK
    return doc


def _write(tmp_path, doc):
    p = tmp_path / "schedule.json"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    return p


# ---------- 模板：不代拍数值、单一真源 ----------

def test_template_covers_every_lottery_from_single_source():
    tpl = DS.input_template()
    assert set(tpl["彩种"]) == set(LOTTERY_CONFIG)
    assert tpl["核验状态"] == DS.UNVERIFIED
    assert tpl["来源"] is None
    assert "声明值" in tpl["声明"]


def test_template_weekdays_match_single_source():
    tpl = DS.input_template()
    for name, cfg in LOTTERY_CONFIG.items():
        assert tpl["彩种"][name]["开奖星期"] == list(cfg["draw_days"])
        assert tpl["彩种"][name]["代码"] == cfg.get("name_en")


def test_template_leaves_clocks_blank():
    """助手不得代填权威时刻；模板 8 格全空。"""
    tpl = DS.input_template()
    assert len(tpl["彩种"]) > 0
    assert all(row["开奖时刻"] is None for row in tpl["彩种"].values())


def test_render_template_is_json_and_blank():
    doc = json.loads(DS.render_template())
    assert all(row["开奖时刻"] is None for row in doc["彩种"].values())


def test_blank_template_is_not_usable():
    """缺席≠通过：空模板必须给足分母与逐条原因，不能报可用。"""
    v = DS.validate_schedule(DS.input_template())
    assert v["可用"] is False
    assert v["覆盖"]["总数"] == len(LOTTERY_CONFIG)
    assert v["覆盖"]["参与"] == 0
    assert v["覆盖"]["未参与"] == v["覆盖"]["总数"]
    assert len(v["未参与"]) == v["覆盖"]["总数"]
    assert all("未参与，原因=" in row["原因"] for row in v["未参与"])


# ---------- 加载：不 fail-open ----------

def test_load_missing_file_raises(tmp_path):
    with pytest.raises(DS.ScheduleError, match="缺失"):
        DS.load_schedule(tmp_path / "nope.json")


def test_load_bad_json_raises(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(DS.ScheduleError, match="解析失败"):
        DS.load_schedule(p)


def test_load_non_object_raises(tmp_path):
    p = _write(tmp_path, [1, 2, 3])
    with pytest.raises(DS.ScheduleError, match="顶层必须是对象"):
        DS.load_schedule(p)


def test_load_full_file_roundtrip(tmp_path):
    p = _write(tmp_path, _full_doc())
    v = DS.validate_schedule(DS.load_schedule(p))
    assert v["可用"] is True


# ---------- 校验：分母纪律与真源 ----------

def test_full_schedule_three_denominators():
    v = DS.validate_schedule(_full_doc())
    assert v["可用"] is True
    assert v["覆盖"] == {"总数": len(LOTTERY_CONFIG),
                        "参与": len(LOTTERY_CONFIG), "未参与": 0}
    assert v["未参与"] == []
    assert v["核验状态"] == DS.UNVERIFIED


def test_absent_lottery_written_as_not_participating():
    """删掉一个彩种：仍进分母，显式写原因，不判通过。"""
    doc = _full_doc()
    victim = next(iter(doc["彩种"]))
    del doc["彩种"][victim]
    v = DS.validate_schedule(doc)
    assert v["可用"] is False
    assert v["覆盖"]["总数"] == len(LOTTERY_CONFIG)
    assert v["覆盖"]["未参与"] == 1
    assert v["未参与"][0]["彩种"] == victim
    assert "未参与，原因=" in v["未参与"][0]["原因"]
    assert v["彩种"][victim]["参与"] is False


def test_weekdays_cannot_be_handwritten():
    """改一个彩种的星期 ⇒ 单一真源比对抓到，落「未参与」而非静默放行。"""
    doc = _full_doc()
    victim = next(iter(doc["彩种"]))
    doc["彩种"][victim]["开奖星期"] = [0]
    v = DS.validate_schedule(doc)
    assert v["可用"] is False
    assert "config.LOTTERY_CONFIG" in v["彩种"][victim]["原因"]


def test_code_mismatch_is_rejected():
    doc = _full_doc()
    victim = next(iter(doc["彩种"]))
    doc["彩种"][victim]["代码"] = "made_up_code"
    v = DS.validate_schedule(doc)
    assert v["彩种"][victim]["参与"] is False
    assert "name_en" in v["彩种"][victim]["原因"]


def test_unknown_lottery_raises():
    doc = _full_doc()
    doc["彩种"]["不存在的彩种"] = {"开奖时刻": FAKE_CLOCK}
    with pytest.raises(DS.ScheduleError, match="未知彩种"):
        DS.validate_schedule(doc)


def test_day_level_date_cannot_be_clock():
    """日级日期会被 fromisoformat 解析成当日零点，不能拿来充开奖时刻。"""
    doc = _full_doc()
    victim = next(iter(doc["彩种"]))
    doc["彩种"][victim]["开奖时刻"] = "2026-11-05"
    v = DS.validate_schedule(doc)
    assert v["彩种"][victim]["参与"] is False
    assert "不能充数" in v["彩种"][victim]["原因"]


def test_clock_format_is_strict():
    bad = ["25:00", "21:60", "21:00:00:00", "上午九点", "", None, 2100, "21:00:60"]
    doc = _full_doc()
    victim = next(iter(doc["彩种"]))
    for value in bad:
        doc["彩种"][victim]["开奖时刻"] = value
        v = DS.validate_schedule(doc)
        assert v["彩种"][victim]["参与"] is False, value


def test_verification_state_required():
    doc = _full_doc()
    del doc["核验状态"]
    with pytest.raises(DS.ScheduleError, match="核验状态"):
        DS.validate_schedule(doc)


def test_verified_without_source_raises():
    doc = _full_doc()
    doc["核验状态"] = DS.VERIFIED
    doc["来源"] = None
    with pytest.raises(DS.ScheduleError, match="来源"):
        DS.validate_schedule(doc)


def test_unverified_state_is_explicitly_declared():
    """未经外部源核验允许作过渡基准，但必须显式披露、不写成证明。"""
    v = DS.validate_schedule(_full_doc())
    assert v["核验状态"] == DS.UNVERIFIED
    assert "不构成" in v["声明"]


def test_timezone_must_be_asia_shanghai():
    doc = _full_doc()
    doc["时区"] = "UTC"
    with pytest.raises(DS.ScheduleError, match="时区口径不符"):
        DS.validate_schedule(doc)


# ---------- 时间组装 ----------

def test_build_draw_time_rejects_non_draw_day():
    doc = _full_doc()
    with pytest.raises(DS.ScheduleError, match="不是"):
        DS.build_draw_time("双色球", date(2026, 11, 2), doc)  # 周一，非双色球开奖日


def test_build_draw_time_rejects_absent_lottery():
    doc = _full_doc()
    victim = next(iter(doc["彩种"]))
    del doc["彩种"][victim]
    with pytest.raises(DS.ScheduleError, match="未参与"):
        DS.build_draw_time(victim, date(2026, 11, 3), doc)


def test_build_draw_time_naive_iso():
    doc = _full_doc()
    got = DS.build_draw_time("双色球", date(2026, 11, 3), doc)
    assert got == f"2026-11-03T{FAKE_CLOCK}"
    assert "+" not in got


def test_build_draw_time_aware_iso():
    doc = _full_doc()
    got = DS.build_draw_time("双色球", "2026-11-03", doc, aware=True)
    assert got == f"2026-11-03T{FAKE_CLOCK}+08:00"


def test_build_draw_time_rejects_datetime_as_date():
    doc = _full_doc()
    with pytest.raises(DS.ScheduleError, match="datetime"):
        DS.build_draw_time("双色球", datetime(2026, 11, 3, 12, 34, 56), doc)


# ---------- 时区口径一致性（naive / aware 混用会抛 TypeError） ----------

def test_check_time_basis_uniform_naive():
    out = DS.check_time_basis("2026-11-03T12:00:00", "2026-11-03T12:01:00",
                              "2026-11-03T12:34:56")
    assert out["一致"] is True and out["口径"] == "naive" and out["数量"] == 3


def test_check_time_basis_uniform_aware():
    out = DS.check_time_basis("2026-11-03T12:00:00+08:00",
                              "2026-11-03T12:34:56+08:00")
    assert out["口径"] == "aware"


def test_check_time_basis_mixed_raises():
    with pytest.raises(DS.ScheduleError, match="TypeError"):
        DS.check_time_basis("2026-11-03T12:00:00", "2026-11-03T12:34:56+08:00")


def test_check_time_basis_unparseable_raises():
    with pytest.raises(DS.ScheduleError, match="不可解析"):
        DS.check_time_basis("离线回放（未记录生产批次时间）")


# ---------- 静态护栏：零生产调用点、无副作用 ----------

def test_module_does_not_read_system_time():
    src = Path(DS.__file__).read_text(encoding="utf-8")
    for bad in ("datetime.now(", "date.today(", "time.time("):
        assert bad not in src, f"准备模块不得读取系统当前时刻：{bad}"


def test_module_has_no_hardcoded_real_config_path():
    src = Path(DS.__file__).read_text(encoding="utf-8")
    assert "config/draw_schedule.json" not in src
    assert "logs/prefix_shadow.jsonl" not in src


def test_module_not_registered_in_ev_init():
    src = (ROOT / "ev" / "__init__.py").read_text(encoding="utf-8")
    assert "draw_schedule" not in src


def test_no_production_import_of_preparation_module():
    """本阶段守护：准备件零生产调用点。

    接线提交时须与三条 import 护栏改写一并调整（同一提交、同批测试）。
    """
    targets = [ROOT / d for d in ("prediction", "web", "pipeline", "data")]
    targets += [ROOT / "run.py", ROOT / "cli.py"]
    for base in targets:
        files = [base] if base.is_file() else list(base.rglob("*.py"))
        for f in files:
            text = f.read_text(encoding="utf-8", errors="replace")
            assert "ev.draw_schedule" not in text, f"生产路径引入了准备件：{f}"
            assert "from ev import draw_schedule" not in text, f"生产路径引入了准备件：{f}"


def test_no_forbidden_narrative_words_in_module():
    src = Path(DS.__file__).read_text(encoding="utf-8")
    for bad in ("不可预测", "已证明公平", "公平随机", "证否"):
        assert bad not in src
