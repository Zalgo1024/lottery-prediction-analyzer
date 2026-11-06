# -*- coding: utf-8 -*-
"""tests/test_draw_evidence.py —— 方案 B 的开奖证据层。

最关键的判据是**方向**：声明值（直播开始）是开奖时间的**下界**，
「首次可观测时刻」是**上界**。搞反方向会把「批次时间晚于实际开奖」判成合格，
所以这里有专门的负向用例（`test_evidence_earlier_than_declared_kills_lower_bound`）。
"""
from __future__ import annotations

import ast
import json

import pytest

from ev import draw_evidence as DE

DECLARED = "2026-11-08T21:15:00"


def _rec(**over):
    r = {
        "版本": DE.EVIDENCE_VERSION,
        "彩种": "双色球",
        "期号": "26128",
        "开奖日期": "2026-11-08",
        "来源": "https://example.invalid/draw",
        "抓取时间": "2026-11-08T21:20:00",
        "首次可观测时刻": "2026-11-08T21:20:00",
        "开奖号码": {"原始CSV行": ["26128", "1", "2", "3", "4", "5", "6", "7"]},
    }
    r.update(over)
    return r


# ---------------------------------------------------------------- 校验
def test_valid_record_passes():
    assert DE.validate_record(_rec())["彩种"] == "双色球"


@pytest.mark.parametrize("field", DE.REQUIRED)
def test_missing_required_field_raises(field):
    r = _rec()
    r.pop(field)
    with pytest.raises(DE.DrawEvidenceError, match="缺字段"):
        DE.validate_record(r)


def test_version_mismatch_raises():
    with pytest.raises(DE.DrawEvidenceError, match="版本不符"):
        DE.validate_record(_rec(版本="1999-01-01"))


def test_bad_time_raises():
    with pytest.raises(DE.DrawEvidenceError, match="不可解析"):
        DE.validate_record(_rec(抓取时间="昨天"))


def test_mixed_timezone_raises():
    with pytest.raises(DE.DrawEvidenceError, match="时区"):
        DE.validate_record(_rec(抓取时间="2026-11-08T21:20:00+08:00"))


def test_numbers_must_be_container():
    with pytest.raises(DE.DrawEvidenceError, match="开奖号码"):
        DE.validate_record(_rec(开奖号码="010203040506"))


# ---------------------------------------------------------------- 追加 / 读取
def test_append_is_idempotent_on_same_key(tmp_path):
    p = tmp_path / "ev.jsonl"
    assert DE.append_evidence(_rec(), path=p) is True
    assert DE.append_evidence(_rec(), path=p) is False      # 同键 ⇒ 不回改
    assert len(p.read_text(encoding="utf-8").splitlines()) == 1


def test_different_source_or_issue_coexist(tmp_path):
    p = tmp_path / "ev.jsonl"
    DE.append_evidence(_rec(), path=p)
    DE.append_evidence(_rec(来源="https://other.invalid/x"), path=p)
    DE.append_evidence(_rec(期号="26129"), path=p)
    assert len(p.read_text(encoding="utf-8").splitlines()) == 3


def test_append_rejects_invalid_record(tmp_path):
    p = tmp_path / "ev.jsonl"
    with pytest.raises(DE.DrawEvidenceError):
        DE.append_evidence(_rec(首次可观测时刻=""), path=p)
    assert not p.exists()


def test_load_counts_bad_lines(tmp_path):
    p = tmp_path / "ev.jsonl"
    DE.append_evidence(_rec(), path=p)
    with p.open("a", encoding="utf-8") as f:
        f.write("{broken json\n")
        f.write(json.dumps({"版本": DE.EVIDENCE_VERSION}, ensure_ascii=False) + "\n")
    doc = DE.load_evidence(path=p)
    assert doc["行数"] == 1 and doc["坏行数"] == 2


def test_for_issue_filters(tmp_path):
    p = tmp_path / "ev.jsonl"
    DE.append_evidence(_rec(), path=p)
    DE.append_evidence(_rec(期号="26129"), path=p)
    DE.append_evidence(_rec(彩种="大乐透"), path=p)
    got = DE.for_issue("双色球", "26128", path=p)
    assert len(got) == 1 and got[0]["期号"] == "26128"


def test_missing_file_is_not_pass(tmp_path):
    doc = DE.load_evidence(path=tmp_path / "nope.jsonl")
    assert doc["存在"] is False and doc["行数"] == 0


# ------------------------------------------- 下界推导（方向是本文件的核心）
def test_no_evidence_uses_declared_as_lower_bound():
    got = DE.derive_draw_lower_bound(DECLARED, [])
    assert got["可用"] is True and got["矛盾"] is False
    assert got["下界"] == DECLARED


def test_evidence_after_declared_keeps_lower_bound():
    """实测结果晚于直播开始（正常情况）⇒ 声明值仍是有效下界。"""
    got = DE.derive_draw_lower_bound(DECLARED, [_rec()])   # 21:20 > 21:15
    assert got["可用"] is True and got["矛盾"] is False
    assert got["下界"] == DECLARED


def test_evidence_earlier_than_declared_kills_lower_bound():
    """★ 关键负向：结果在声明时刻前就可见 ⇒ 声明值偏高、不再是下界 ⇒ 必须判不可用。

    如果这里判「可用」，就会把「批次时间晚于实际开奖」的票当成合格证据。
    """
    got = DE.derive_draw_lower_bound(
        DECLARED, [_rec(首次可观测时刻="2026-11-08T21:05:00")])
    assert got["可用"] is False and got["矛盾"] is True
    assert any("不再是下界" in r for r in got["原因"])
    assert any("核对官方公告" in n for n in got["披露"])


def test_lower_bound_never_takes_evidence_time():
    """证据时刻永远不能当下界：即使它更晚，下界仍是声明值。"""
    got = DE.derive_draw_lower_bound(DECLARED, [_rec(首次可观测时刻="2026-11-08T23:00:00")])
    assert got["下界"] == DECLARED


def test_missing_declared_is_unavailable():
    got = DE.derive_draw_lower_bound(None, [_rec()])
    assert got["可用"] is False and "不 fail-open" in "".join(got["原因"])


def test_unparseable_declared_is_unavailable():
    got = DE.derive_draw_lower_bound("21:15", [_rec()])
    assert got["可用"] is False


def test_timezone_mixing_is_unavailable():
    got = DE.derive_draw_lower_bound(
        DECLARED, [_rec(首次可观测时刻="2026-11-08T21:20:00+08:00")])
    assert got["可用"] is False
    assert any("时区" in r for r in got["原因"])


def test_bad_evidence_time_is_listed_not_crashing():
    got = DE.derive_draw_lower_bound(DECLARED, [_rec(首次可观测时刻="21:20")])
    assert got["可用"] is True                    # 坏证据被跳过并列进明细
    assert any("错误" in d for d in got["明细"])


def test_disclosure_always_explains_direction():
    for got in (DE.derive_draw_lower_bound(DECLARED, []),
                DE.derive_draw_lower_bound(DECLARED, [_rec()])):
        assert any("下界" in n and "上界" in n for n in got["披露"])


# ---------------------------------------------------------------- 不变量
def test_module_does_not_read_system_time():
    src = (DE.BASE_DIR / "ev" / "draw_evidence.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    hits = [n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("now", "today", "utcnow")]
    assert hits == [], f"证据层读了系统时间：{hits}（时间必须由采集器注入）"


# ------------------------------------------------ 跨期交叉校验（让高频采集真正进判据）
def _builder(date_iso):
    """测试替身：按证据自己的开奖日期组装该期声明下界。"""
    return f"{date_iso}T21:15:00"


def test_for_lottery_returns_all_issues(tmp_path):
    p = tmp_path / "e.jsonl"
    DE.append_evidence(_rec(期号="26127", 开奖日期="2026-11-05"), path=p)
    DE.append_evidence(_rec(期号="26128"), path=p)
    DE.append_evidence(_rec(彩种="大乐透", 期号="26126"), path=p)
    assert {r["期号"] for r in DE.for_lottery("双色球", path=p)} == {"26127", "26128"}
    assert len(DE.for_lottery("大乐透", path=p)) == 1


@pytest.mark.parametrize("raw,want", [
    ("2026-11-08", "2026-11-08"),
    ("5/11/2026", "2026-11-05"),
    ("2026-1-5", "2026-01-05"),   # strptime 允许非补零，归一后仍是合法 ISO
    ("不是日期", None),
    ("", None),
    (None, None),
])
def test_normalize_draw_date(raw, want):
    assert DE.normalize_draw_date(raw) == want


def test_cross_check_empty_pool_is_explicitly_disclosed():
    got = DE.cross_check_schedule([], _builder)
    assert got["可用"] is True and got["参与校验"] == 0
    assert got["未参与"] and "没有任何证据行" in got["未参与"][0]["原因"]
    assert any("不等于声明时刻已被验证正确" in n for n in got["披露"])


def test_cross_check_no_contradiction_keeps_schedule_usable():
    got = DE.cross_check_schedule([_rec()], _builder)
    assert got["可用"] is True and got["矛盾"] is False
    assert got["参与校验"] == 1 and got["明细"][0]["结论"] == "不矛盾"


def test_cross_check_contradiction_blocks_registration():
    """★ 牙齿：历史期结果在声明值之前就可见 ⇒ 整张时刻表不再可信。"""
    got = DE.cross_check_schedule(
        [_rec(首次可观测时刻="2026-11-08T21:10:00")], _builder)
    assert got["可用"] is False and got["矛盾"] is True
    assert got["原因"] and "不再构成下界" in got["原因"][0]
    assert any("不再构成下界" in n for n in got["披露"])


def test_cross_check_compares_each_row_to_its_own_date():
    """跨日不能误报：昨天的期今天才采到，其绝对时刻仍晚于「昨天」的声明值。"""
    rows = [
        _rec(期号="26127", 开奖日期="2026-11-05",
             首次可观测时刻="2026-11-06T09:00:00"),
        _rec(期号="26128", 开奖日期="2026-11-08",
             首次可观测时刻="2026-11-08T21:20:00"),
    ]
    got = DE.cross_check_schedule(rows, _builder)
    assert got["参与校验"] == 2 and got["可用"] is True


def test_cross_check_skipped_rows_have_reasons():
    rows = [
        _rec(期号="A", 开奖日期="5/11/2026",
             首次可观测时刻="不是时间"),
        _rec(期号="B", 开奖日期="???",
             首次可观测时刻="2026-11-08T21:20:00"),
    ]
    got = DE.cross_check_schedule(rows, _builder)
    assert got["参与校验"] == 0
    assert len(got["未参与"]) == 2
    assert all("原因" in r for r in got["未参与"])


def test_cross_check_builder_failure_is_disclosed():
    def _broken(_d):
        raise ValueError("该日不是开奖日")

    got = DE.cross_check_schedule([_rec()], _broken)
    assert got["参与校验"] == 0 and got["可用"] is True
    assert "该期声明时刻不可构造" in got["未参与"][0]["原因"]


def test_cross_check_timezone_mix_is_skipped_not_crash():
    got = DE.cross_check_schedule(
        [_rec()], lambda d: f"{d}T21:15:00+08:00")
    assert got["参与校验"] == 0
    assert "时区口径混用" in got["未参与"][0]["原因"]


def test_schedule_config_discloses_both_directions():
    """配置里的方向曾被写反：声明值对开奖事件是下界、对批次时间是上界。"""
    doc = json.loads((DE.BASE_DIR / "config" / "draw_schedule.json").read_text(
        encoding="utf-8"))
    text = doc["口径"]
    assert "开奖事件时间是下界" in text
    assert "批次时间是上界" in text
    assert "开奖事件的上界" in text
