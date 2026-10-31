# -*- coding: utf-8 -*-
"""留证台账的不可覆盖性（2026-10-31 任务 2 的牙齿）。

这组测试锁的是「历史是追加出来的，不是编辑出来的」：
链完整时 `verify()` 必须说完整；**任何**非追加改动（改一行、删一行）都必须被抓到
并指出断在第几行。

★ 反向守护同样重要：`verify()` 在台账不存在时必须说「尚无留证事件」，
  而不是伪造一个「完整」的结论 —— 空文件不是证据。
"""

from __future__ import annotations

import json

import pytest

from data import attest as AT


def _ev(i: int) -> dict:
    return {"事件": "测试", "期号": f"10{i}", "备注": f"第{i}条"}


def test_append_then_verify_ok():
    for i in range(3):
        AT.append("排列5", _ev(i))
    v = AT.verify("排列5")
    assert v["完整"] is True
    assert v["行数"] == 3


def test_tamper_middle_line_is_detected():
    """★★ 改掉中间一行 ⇒ 必须报断点，且行号对得上。"""
    for i in range(3):
        AT.append("排列5", _ev(i))
    p = AT.attest_path("排列5")
    lines = p.read_text(encoding="utf-8").splitlines()
    doc = json.loads(lines[1])
    doc["备注"] = "被我改过了"
    lines[1] = json.dumps(doc, ensure_ascii=False)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")

    v = AT.verify("排列5")
    assert v["完整"] is False
    assert v["断点"] == 2
    assert "哈希" in v["原因"]


def test_deleting_a_line_breaks_the_chain():
    """★ 删掉中间一行 ⇒ 后一行的「上一行哈希」对不上。删行是最常见的掩盖手法。"""
    for i in range(3):
        AT.append("排列5", _ev(i))
    p = AT.attest_path("排列5")
    lines = p.read_text(encoding="utf-8").splitlines()
    p.write_text("\n".join([lines[0], lines[2]]) + "\n", encoding="utf-8")

    v = AT.verify("排列5")
    assert v["完整"] is False
    assert v["断点"] == 2


def test_missing_ledger_is_not_a_fake_pass():
    """空台账必须说「尚无留证事件」，不能返回一个让人误以为「验过了」的完整。"""
    v = AT.verify("从没走过的彩种")
    assert v["行数"] == 0
    assert "尚无留证事件" in v["说明"]


def test_no_mutation_api_is_exposed():
    """★ 结构上不提供修改入口 —— 想改只能再 append 一条，让改动本身成为事件。"""
    for name in ("update", "delete", "remove", "rewrite", "edit"):
        assert not hasattr(AT, name), f"留证模块不应暴露 {name}()"


def test_config_hash_changes_when_file_changes(tmp_path):
    """★★ 配置哈希是这次事故的解药：文件被删/被改，哈希就要变。"""
    from data.version import file_hash

    p = tmp_path / "ticket_size.json"
    p.write_text('{"mode":"dynamic"}', encoding="utf-8")
    h1 = file_hash(p)
    p.write_text('{"mode":"fixed"}', encoding="utf-8")
    h2 = file_hash(p)
    assert h1 and h2 and h1 != h2
    p.unlink()
    assert file_hash(p) is None, "文件不存在必须返回 None，不能返回空串冒充『没问题』"


def test_snapshot_records_config_hash_and_presence():
    ev = AT.snapshot_event("排列5", issue="1", batch="b1")
    assert "配置哈希" in ev and "配置存在" in ev
    assert ev["配置存在"] is True
    assert ev["配置哈希"]
    assert ev["预测版本"] and "-" in ev["预测版本"]
