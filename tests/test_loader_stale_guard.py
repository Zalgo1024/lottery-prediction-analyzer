"""清洗文件过期防护回归锁（2026-10-09 双色球 26115 卡死事故）。

事故链：Excel 独占清洗输出文件 ⇒ run_clean 写入 PermissionError 被当 warning
吞掉 ⇒ cleaned 永远停在旧期（10-06）⇒ loader 优先读 cleaned ⇒ 评估/预测对
新增期次全盲；且抓 0 条新数据时提前 return，连补清洗都不会触发 ⇒ 死锁 24h+。

本文件锁两道防护：
1) data.loader._resolve_csv_path —— raw 比 cleaned 新时改读 raw；
2) data.fetcher._clean_if_stale —— 抓 0 条的早退路径上补过期清洗，
   且清洗失败不阻塞主流程（返回 False）。
"""
import os
import time

import pytest

import data.loader as loader
import data.fetcher as fetcher


def _touch(path, mtime):
    path.write_text("期号,红球1\n26115,4\n", encoding="utf-8")
    os.utime(path, (mtime, mtime))


@pytest.fixture
def fake_loader_paths(tmp_path, monkeypatch):
    raw = tmp_path / "双色球历史数据.csv"
    cleaned = tmp_path / "双色球历史数据_cleaned.csv"
    # 铁律：patch 目标模块自己的属性（loader 顶层 from config import 了 CLEANED_FILE_PATHS）
    monkeypatch.setattr(loader, "CLEANED_FILE_PATHS", {"双色球": cleaned})
    monkeypatch.setattr(loader, "_file_path", lambda name: raw)
    return raw, cleaned


@pytest.fixture
def fake_fetcher_paths(tmp_path, monkeypatch):
    raw = tmp_path / "raw.csv"
    cleaned = tmp_path / "cleaned.csv"
    monkeypatch.setattr(fetcher, "CLEANED_FILE_PATHS", {"双色球": cleaned})
    return raw, cleaned


# ---------- loader 过期防护 ----------

def test_raw_newer_than_cleaned_reads_raw(fake_loader_paths):
    """cleaned 落后于 raw ⇒ 必须改读 raw（事故主防护）。"""
    raw, cleaned = fake_loader_paths
    now = time.time()
    _touch(cleaned, now - 3600)
    _touch(raw, now)
    assert loader._resolve_csv_path("双色球") == raw


def test_cleaned_fresh_reads_cleaned(fake_loader_paths):
    """cleaned 正常（不旧于 raw）⇒ 维持原行为读 cleaned。"""
    raw, cleaned = fake_loader_paths
    now = time.time()
    _touch(raw, now - 3600)
    _touch(cleaned, now)
    assert loader._resolve_csv_path("双色球") == cleaned


def test_cleaned_missing_reads_raw(fake_loader_paths):
    """cleaned 不存在 ⇒ 回退 raw（原行为）。"""
    raw, cleaned = fake_loader_paths
    _touch(raw, time.time())
    assert loader._resolve_csv_path("双色球") == raw


def test_use_cleaned_false_forces_raw(fake_loader_paths):
    """显式 use_cleaned=False（清洗场景）⇒ 永远读 raw。"""
    raw, cleaned = fake_loader_paths
    now = time.time()
    _touch(raw, now - 3600)
    _touch(cleaned, now)
    assert loader._resolve_csv_path("双色球", use_cleaned=False) == raw


# ---------- fetcher 抓 0 条路径的补清洗 ----------

def test_fetcher_cleans_when_stale(fake_fetcher_paths, monkeypatch):
    """raw 比 cleaned 新 ⇒ 补清洗且只补一次。"""
    raw, cleaned = fake_fetcher_paths
    now = time.time()
    _touch(cleaned, now - 3600)
    _touch(raw, now)
    calls = []
    monkeypatch.setattr("data.cleaner.run_clean", lambda name: calls.append(name))
    assert fetcher._clean_if_stale("双色球", raw) is True
    assert calls == ["双色球"]


def test_fetcher_skips_clean_when_fresh(fake_fetcher_paths, monkeypatch):
    """cleaned 不旧于 raw ⇒ 不触发清洗。"""
    raw, cleaned = fake_fetcher_paths
    now = time.time()
    _touch(raw, now - 3600)
    _touch(cleaned, now)
    calls = []
    monkeypatch.setattr("data.cleaner.run_clean", lambda name: calls.append(name))
    assert fetcher._clean_if_stale("双色球", raw) is False
    assert calls == []


def test_fetcher_skips_when_cleaned_missing(fake_fetcher_paths, monkeypatch):
    """cleaned 不存在（未启用清洗缓存）⇒ 不触发。"""
    raw, cleaned = fake_fetcher_paths
    _touch(raw, time.time())
    calls = []
    monkeypatch.setattr("data.cleaner.run_clean", lambda name: calls.append(name))
    assert fetcher._clean_if_stale("双色球", raw) is False
    assert calls == []


def test_fetcher_clean_failure_swallowed(fake_fetcher_paths, monkeypatch):
    """补清洗失败（如文件仍被占用）⇒ 返回 False，不向主流程抛异常。"""
    raw, cleaned = fake_fetcher_paths
    now = time.time()
    _touch(cleaned, now - 3600)
    _touch(raw, now)

    def boom(name):
        raise PermissionError(13, "另一个程序正在使用此文件")

    monkeypatch.setattr("data.cleaner.run_clean", boom)
    assert fetcher._clean_if_stale("双色球", raw) is False
