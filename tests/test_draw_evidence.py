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
