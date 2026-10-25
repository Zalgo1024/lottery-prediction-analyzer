# -*- coding: utf-8 -*-
"""反馈文件**持久性**契约（2026-10-24 排列3 数据损坏事故的回归钉）。

事故回顾（实查，不是推演）：
  `training/feedback/排列3_feedback_history.json` 2,354,307 字节，
  前 2 字节 `[]`、接着 368,449 个 NUL、之后才是完整尾部。
  根因两条，必须各自钉死：

  ① **写入非原子**：`open(path, "w")` 先把文件截断为 0 再流式 dump，
     进程中途被杀 → 留下半截/NUL 空洞。
  ② **读取损坏时静默按空**：`_safe_read_json` 返回 `[]`，调用方
     `append_feedback` 紧接着写回 ⇒ 损坏文件被空列表覆盖，抢救窗口为 0。

本文件锁死修复后的两条不变式：
  - 写入失败（dump 抛异常）时**原文件保持不变**，且不留 tmp 垃圾；
  - 读取损坏文件时**原字节被改名隔离**（`.corrupt-<ts>`），而不是被清空。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data import feedback as FB      # noqa: E402


def _tmp_store(tmp_path: Path) -> Path:
    p = tmp_path / "排列3_feedback_history.json"
    return p


# ---------------------------------------------------------------- ① 原子写
def test_write_failure_leaves_original_intact(tmp_path, monkeypatch):
    """★ 核心不变式：写一半炸掉，原文件必须是**旧的完整版本**。

    旧实现 `open(path,"w")` 在这一步已经把文件截断为 0 ⇒ 原数据当场没了。
    """
    p = _tmp_store(tmp_path)
    original = [{"期号": 1, "策略": "A"}, {"期号": 2, "策略": "B"}]
    p.write_text(json.dumps(original, ensure_ascii=False), encoding="utf-8")
    before = p.read_bytes()

    real_dump = FB.json.dump

    def boom(*a, **kw):
        # 先写一点再炸 —— 模拟「流式 dump 中途失败」，这才能真正区分
        # 原子写与截断写：截断写此时文件已经是半截的了。
        f = a[1]
        f.write('[\n  {"期号": 999')
        raise OSError("模拟磁盘写入失败")

    monkeypatch.setattr(FB.json, "dump", boom)
    with pytest.raises(OSError):
        FB._safe_write_json(p, [{"期号": 999}], retries=1)
    monkeypatch.setattr(FB.json, "dump", real_dump)

    assert p.read_bytes() == before, "原文件被破坏 —— 原子写没生效"
    assert json.loads(p.read_text(encoding="utf-8")) == original
    assert not list(tmp_path.glob("*.tmp")), "失败后残留了 tmp 文件"


def test_write_is_atomic_no_partial_visible(tmp_path):
    """写成功时内容完整可解析；不存在「先截断」的中间态。"""
    p = _tmp_store(tmp_path)
    FB._safe_write_json(p, [{"期号": i} for i in range(50)])
    back = json.loads(p.read_text(encoding="utf-8"))
    assert len(back) == 50
    assert not list(tmp_path.glob("*.tmp"))


def test_write_creates_parent_dir(tmp_path):
    p = tmp_path / "sub" / "dir" / "x.json"
    FB._safe_write_json(p, [{"a": 1}])
    assert json.loads(p.read_text(encoding="utf-8")) == [{"a": 1}]


def test_write_rejects_nothing_and_no_nul_bytes(tmp_path):
    """写出的文件不得含 NUL（事故的直接症状就是 NUL 空洞）。"""
    p = _tmp_store(tmp_path)
    FB._safe_write_json(p, [{"说明": "中文测试", "n": 1}])
    assert b"\x00" not in p.read_bytes()


# ---------------------------------------------------------------- ② 损坏隔离
def test_corrupt_file_is_quarantined_not_silently_empty(tmp_path):
    """★ 核心不变式：读到损坏文件时，**原字节必须被保留下来**（改名），
    返回 default 只是为了让主流水线不崩，不得让调用方把空值写回去覆盖。"""
    p = _tmp_store(tmp_path)
    # 复刻真实损坏形态：`[]` + NUL + 完整尾部
    tail = '[\n  {"期号": 26240}\n]'.encode("utf-8")
    p.write_bytes(b"[]" + b"\x00" * 5000 + tail)

    got = FB._safe_read_json(p, default=[])
    assert got == []                          # 不阻塞主流水线
    assert not p.exists(), "损坏文件应已被改名隔离"

    quarantined = list(tmp_path.glob("*.corrupt-*"))
    assert len(quarantined) == 1
    blob = quarantined[0].read_bytes()
    assert blob.count(b"\x00") == 5000, "隔离文件必须保留原字节供抢救"
    assert "26240" in blob.decode("utf-8", errors="ignore")


def test_quarantine_preserves_tail_for_salvage(tmp_path):
    """隔离后的文件必须能被抢救脚本按「NUL 之后的完整尾部」读出来。"""
    import re
    p = _tmp_store(tmp_path)
    tail = '[\n  {\n    "期号": 26241\n  },\n  {\n    "期号": 26242\n  }\n]'
    p.write_bytes(b"[]" + b"\x00" * 3000 + tail.encode("utf-8"))
    FB._safe_read_json(p, default=[])
    q = list(tmp_path.glob("*.corrupt-*"))[0]
    b = q.read_bytes()
    runs = [(m.start(), m.end()) for m in re.finditer(rb"\x00+", b)]
    body = b[runs[-1][1]:]
    m = re.search(rb"\n  \{\n", body)
    salvaged = json.loads((b"[" + body[m.start():]).decode("utf-8"))
    assert [r["期号"] for r in salvaged] == [26241, 26242]


def test_missing_file_returns_default_without_creating_anything(tmp_path):
    p = tmp_path / "nope.json"
    assert FB._safe_read_json(p, default={"x": 1}) == {"x": 1}
    assert not p.exists()
    assert not list(tmp_path.glob("*.corrupt-*"))


def test_valid_file_is_not_quarantined(tmp_path):
    p = _tmp_store(tmp_path)
    p.write_text(json.dumps([{"期号": 7}], ensure_ascii=False), encoding="utf-8")
    assert FB._safe_read_json(p, default=[]) == [{"期号": 7}]
    assert p.exists()
    assert not list(tmp_path.glob("*.corrupt-*"))


# ---------------------------------------------------------------- ③ 端到端
def test_append_does_not_wipe_corrupt_history(tmp_path, monkeypatch):
    """★ 端到端复刻事故链：损坏文件 + append 一条 ⇒ 旧字节不得消失。

    旧实现下这里会把 2000+ 条历史变成 1 条（`[]` + 新记录）。
    """
    p = _tmp_store(tmp_path)
    tail = '[\n  {"期号": 26243}\n]'.encode("utf-8")
    p.write_bytes(b"[]" + b"\x00" * 4000 + tail)
    monkeypatch.setattr(FB, "_history_path", lambda _lot: p)

    FB.append_feedback("排列3", {"期号": 26284, "策略": "A"})

    q = list(tmp_path.glob("*.corrupt-*"))
    assert len(q) == 1, "损坏历史必须先被隔离"
    assert "26243" in q[0].read_bytes().decode("utf-8", errors="ignore")
    # 新文件只有新记录（这是可接受的降级：至少旧字节还在隔离文件里可救）
    assert json.loads(p.read_text(encoding="utf-8")) == [{"期号": 26284, "策略": "A"}]
