# -*- coding: utf-8 -*-
"""tests/test_env_guard.py —— 环境依赖守卫（conftest 的 pytest_collection_modifyitems）
的牙齿测试。

立项背景（2026-10-26）：CI 17/17 全红全是环境依赖（market.db 不入库 /
statsmodels 可选栈 / 中文字体 / 登记册生成物），没有一条是代码缺陷。守卫把这类
用例在 CI/全新克隆里自动跳过、在本机全环境照跑。本文件锁四件事：

1. 三个标记已在 pytest.ini 登记（--strict-markers 之外的双保险，防声明被删）；
2. 环境探测与事实自洽（路径解析坏了要能红——探测函数是守卫的眼睛）；
3. 跳过决策正确：缺什么跳什么、**未标记用例永不受影响**、环境满足时一律不跳；
4. 三个标记都真的有用例在用（防「守卫健在但没人打标」的真空状态）。

★ 变异验证：把钩子条件取反 / 从 checks 删一个标记 / 让探测函数恒真，
   本文件第 3、4 组会红（与 test_tier_review 同一纪律）。
"""
from __future__ import annotations

import configparser
import importlib.util
from pathlib import Path

import pytest

import conftest as C

_REPO_ROOT = Path(__file__).resolve().parents[1]
_TESTS_DIR = _REPO_ROOT / "tests"


# ---------------------------------------------------------------- 假 item
class _FakeItem:
    """钩子只用到 `.keywords` 与 `.add_marker`，造个最小桩，不污染真实收集结果。"""

    def __init__(self, *keywords: str):
        self.keywords = set(keywords)
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker)


def _skip_reason(item: _FakeItem) -> str:
    for m in item.markers:
        if m.name == "skip":
            return str(m.kwargs.get("reason", ""))
    return ""


# ---------------------------------------------------------------- 1. 标记登记
def test_markers_registered_in_pytest_ini():
    """三个环境标记必须登记在 pytest.ini（--strict-markers 的配套声明）。"""
    cp = configparser.ConfigParser()
    cp.read(_REPO_ROOT / "pytest.ini", encoding="utf-8")
    declared = cp.get("pytest", "markers")
    for mark in ("realdata", "statsmodels", "winfont"):
        assert mark in declared, f"pytest.ini 的 markers 里没有 {mark}（声明被删？）"


def test_markers_are_actually_used():
    """每个标记至少要有一个用例在用，否则守卫是真空的。"""
    for mark in ("realdata", "statsmodels", "winfont"):
        users = [p.name for p in _TESTS_DIR.glob("test_*.py")
                 if f"pytest.mark.{mark}" in p.read_text(encoding="utf-8")]
        assert users, f"没有任何用例打 {mark} 标记——守卫在保护不存在的东西"


# ---------------------------------------------------------------- 2. 探测自洽
def test_env_flags_agree_with_reality():
    """探测常量必须与文件系统/导入系统的真实状态一致（路径解析坏了要能红）。"""
    assert C.HAS_MARKET_DB == (_REPO_ROOT / "market" / "market.db").exists()
    assert C.HAS_STATSMODELS == (importlib.util.find_spec("statsmodels") is not None)
    reg = _REPO_ROOT / "training" / "hypotheses"
    expect_reg = reg.is_dir() and any(reg.glob("*_hypotheses.json"))
    assert C.HAS_REGISTRY == expect_reg
    assert isinstance(C._cjk_font_available(), bool)


# ---------------------------------------------------------------- 3. 跳过决策
def test_missing_component_skips_only_its_own_mark(monkeypatch):
    """缺 market.db ⇒ 只跳 realdata；statsmodels/winfont/无标记用例不受影响。"""
    monkeypatch.setattr(C, "HAS_MARKET_DB", False)
    items = [_FakeItem("realdata"), _FakeItem("statsmodels"),
             _FakeItem("winfont"), _FakeItem()]
    C.pytest_collection_modifyitems(None, items)
    assert "market/market.db" in _skip_reason(items[0])
    assert _skip_reason(items[1]) == ""      # 别的标记不连带跳
    assert _skip_reason(items[2]) == ""
    assert _skip_reason(items[3]) == ""      # 未标记用例永不受影响


def test_missing_statsmodels_skips_only_statsmodels(monkeypatch):
    monkeypatch.setattr(C, "HAS_STATSMODELS", False)
    items = [_FakeItem("realdata"), _FakeItem("statsmodels"), _FakeItem()]
    C.pytest_collection_modifyitems(None, items)
    assert _skip_reason(items[0]) == ""
    assert "statsmodels" in _skip_reason(items[1])
    assert _skip_reason(items[2]) == ""


def test_missing_font_skips_only_winfont(monkeypatch):
    monkeypatch.setattr(C, "_CJK_FONT", False)
    items = [_FakeItem("winfont"), _FakeItem("realdata"), _FakeItem()]
    C.pytest_collection_modifyitems(None, items)
    assert "中文字体" in _skip_reason(items[0])
    assert _skip_reason(items[1]) == ""
    assert _skip_reason(items[2]) == ""


def test_missing_registry_also_skips_realdata(monkeypatch):
    """登记册缺失同样走 realdata（理由里要点名，不能只说"缺数据"）。"""
    monkeypatch.setattr(C, "HAS_REGISTRY", False)
    items = [_FakeItem("realdata")]
    C.pytest_collection_modifyitems(None, items)
    reason = _skip_reason(items[0])
    assert "登记册" in reason


def test_complete_env_skips_nothing(monkeypatch):
    """环境全满足时，任何标记用例都不得被跳过（本机全量的前提）。"""
    monkeypatch.setattr(C, "HAS_MARKET_DB", True)
    monkeypatch.setattr(C, "HAS_STATSMODELS", True)
    monkeypatch.setattr(C, "HAS_REGISTRY", True)
    monkeypatch.setattr(C, "_CJK_FONT", True)
    items = [_FakeItem("realdata"), _FakeItem("statsmodels"), _FakeItem("winfont")]
    C.pytest_collection_modifyitems(None, items)
    assert all(not it.markers for it in items)


def test_skip_reason_names_component_and_policy(monkeypatch):
    """跳过理由必须同时说清「缺什么」和「为什么跳过而不是失败」。"""
    monkeypatch.setattr(C, "HAS_MARKET_DB", False)
    monkeypatch.setattr(C, "HAS_REGISTRY", False)
    item = _FakeItem("realdata")
    C.pytest_collection_modifyitems(None, [item])
    reason = _skip_reason(item)
    assert "market/market.db" in reason and "登记册" in reason
    assert "本机全量照跑" in reason
