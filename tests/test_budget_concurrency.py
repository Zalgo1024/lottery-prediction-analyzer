# -*- coding: utf-8 -*-
"""tests/test_budget_concurrency.py —— 预算台账的并发安全与超额检测。

背景（2026-11-06 事故）：09:01:24 与 09:01:27 两笔各 1,102 元，第二笔在额度已归零时
仍按旧账算出 551 注 ⇒ 两个进程基于同一份旧账各算各的，合计超出月度总额。

本文件锁两件**已修**的事：
  1. `record_spend()` 的「读-改-写」现在整体持文件锁 ⇒ 并发写不再互相覆盖、不丢记录；
  2. 超额会被**检测并标记**（`超额元` + `超额说明`），不是静默发生。
★ 未修的是「clamp 读账 → 出号 → record_spend 写账」之间的窗口（TOCTOU），
  那需要原子预占，会改变 clamp 的纯查询语义，已在 docstring 里写明。
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from ev import budget_gate as BG

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def spend(tmp_path):
    return tmp_path / "budget_spend.json"


def test_concurrent_writers_do_not_lose_records(spend):
    """3 个进程各写 5 条不同键 ⇒ 必须 15 条全在（无锁时会互相覆盖）。"""
    script = textwrap.dedent(f"""
        import os, sys
        sys.path.insert(0, r"{ROOT}")
        from pathlib import Path
        from ev import budget_gate as BG
        p = Path(r"{spend}")
        for i in range(5):
            BG.record_spend("双色球", 目标期号=f"P{{os.getpid()}}_{{i}}",
                            注数=1, path=p)
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(3)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err.decode("utf-8", "ignore")[-800:]

    doc = json.loads(spend.read_text(encoding="utf-8"))
    assert len(doc["记录"]) == 15, f"丢记录：只有 {len(doc['记录'])} 条（应 15）"
    assert doc["已投"] == 30.0, doc["已投"]      # 15 条 × 1 注 × 2 元


def test_same_key_stays_idempotent_under_concurrency(spend):
    """同键并发写仍然是**幂等覆盖**，不会因加锁变成重复累加。"""
    script = textwrap.dedent(f"""
        import sys
        sys.path.insert(0, r"{ROOT}")
        from pathlib import Path
        from ev import budget_gate as BG
        p = Path(r"{spend}")
        for _ in range(5):
            BG.record_spend("双色球", 目标期号="SAME", 注数=7, path=p)
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE)
             for _ in range(2)]
    for p in procs:
        _, err = p.communicate(timeout=180)
        assert p.returncode == 0, err.decode("utf-8", "ignore")[-800:]

    doc = json.loads(spend.read_text(encoding="utf-8"))
    assert len(doc["记录"]) == 1, doc["记录"]
    assert doc["已投"] == 14.0                     # 7 注 × 2 元，不是 70


def test_over_budget_is_marked_not_silent(spend, tmp_path, monkeypatch):
    """超额必须被检测并写进台账，不能静默。"""
    limit = tmp_path / "budget_aggregate.json"
    limit.write_text(json.dumps({"启用": True, "月度总额": 100,
                                 "单期上限": 100, "锁定": True},
                                ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(BG, "LIMIT_PATH", limit)

    a = BG.record_spend("双色球", 目标期号="26128", 注数=30, path=spend)   # 60 元
    assert a["超额元"] == 0

    b = BG.record_spend("大乐透", 目标期号="26127", 注数=30, path=spend)   # 累计 120
    assert b["超额元"] == 20, b          # 120 - 100

    doc = json.loads(spend.read_text(encoding="utf-8"))
    assert doc["超额元"] == 20
    assert "TOCTOU" in doc["超额说明"]
    assert doc["已投"] == 120.0


def test_under_budget_has_no_marker(spend, tmp_path, monkeypatch):
    limit = tmp_path / "budget_aggregate.json"
    limit.write_text(json.dumps({"启用": True, "月度总额": 1000,
                                 "单期上限": 1000, "锁定": True},
                                ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(BG, "LIMIT_PATH", limit)

    r = BG.record_spend("双色球", 目标期号="26128", 注数=10, path=spend)
    assert r["超额元"] == 0
    doc = json.loads(spend.read_text(encoding="utf-8"))
    assert "超额元" not in doc


def test_gate_disabled_never_marks_over(spend, tmp_path, monkeypatch):
    """闸门未启用时不存在「超额」概念：不许乱打标记。"""
    limit = tmp_path / "budget_aggregate.json"
    limit.write_text(json.dumps({"启用": False, "月度总额": 1},
                                ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(BG, "LIMIT_PATH", limit)

    r = BG.record_spend("双色球", 目标期号="26128", 注数=999, path=spend)
    assert r["超额元"] == 0
    assert "超额元" not in json.loads(spend.read_text(encoding="utf-8"))


def test_clamp_docstring_states_mitigation_and_residual():
    """docstring 必须**同时**写清「怎么缓解」和「还剩什么没解决」。

    ★ 2026-11-06 更新判据：原来的断言是「必须含『未修』」—— 那是修复前的状态描述。
      现在 TOCTOU 已用原子预占缓解，这条断言会变成「逼着人谎称没修」。
      改为三件事都要写（比原来更严）：
        ① 披露并发窗口本身（TOCTOU / 原子预占）；
        ② 写明已做的缓解（已修）；
        ③ 写明剩余风险（残留）—— 不能让人从「以为没修」跳到「以为全好了」。
    """
    doc = BG.clamp.__doc__ or ""
    assert "TOCTOU" in doc or "原子预占" in doc, "clamp 未披露并发窗口"
    assert "已修" in doc, "已做的缓解没写进 docstring"
    assert "残留" in doc, "剩余风险没写 —— 不能让人以为 100% 解决"
