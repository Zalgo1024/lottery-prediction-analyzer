"""pytest 全局夹具：把「会落盘到 config/」的模块存储路径重定向到临时目录。

背景（2026-09-18 实踩，务必保留本夹具）：
  `data/push_notify.py` 的 `push_health_summary` / `push_recovery_summary` /
  `push_pipeline_result` 在推送后会走 `_mark_pushed()` → `save_config()`，
  落盘到 `config/push_notify.json`（真身配置：渠道 + webhook token）。
  新写的「自检推送」用例当时只传了 cfg、**没有重定向 STORE_PATH**，
  结果测试把真身配置覆盖成了测试值 —— 用户真实的企业微信 webhook token 被写成了 "k"。

本夹具对**所有**用例 autouse 生效，作为最后一道保险：任何测试都不再可能
写到真实的 `config/`。各测试内部的 `_TmpStore` 仍可继续用（会保存/恢复本夹具
给出的临时路径，互不冲突）。
"""

import pytest

_TARGETS = (
    ("data.push_notify", "STORE_PATH", "push_notify.json"),
    # 跨进程节流状态文件（2026-10-24 新增）。不加这一条，用例会把真实
    # config/.push_pace.json 写成"刚刚发过"，让**下一次真实推送白等 3.2 秒**；
    # 反过来，真实文件里的旧时间戳也会让用例莫名其妙变慢。
    ("data.push_notify", "PACE_PATH", ".push_pace.json"),
    ("data.ticket_size", "STORE_PATH", "ticket_size.json"),
    ("web.training_loop", "STORE_PATH", "training_loop.json"),
    # ev/mechanism/gap_cache.py 的磁盘缓存目录（测量结果 JSON）。
    # 不加这一条，用例一旦走 measure_gap(use_cache=True) 就会把真实 cache/mechanism 写脏。
    ("ev.mechanism.gap_cache", "CACHE_DIR", "mechanism"),
    # 离线体检日报的「今天跑过没」状态（web/health_digest.py）。
    # 不加这一条，用例调用 maybe_run() 会把真实 config/digest_state.json 写成今天已跑，
    # 导致当晚真实日报被静默跳过。
    ("web.health_digest", "STATE_PATH", "digest_state.json"),
    ("web.health_digest", "DIGEST_DIR", "automation"),
)


@pytest.fixture(autouse=True)
def _isolate_config_stores(tmp_path):
    saved = []
    for mod_name, attr, filename in _TARGETS:
        try:
            mod = __import__(mod_name, fromlist=["*"])
        except Exception:            # 模块不可导入时跳过，不因夹具让测试报错
            continue
        if not hasattr(mod, attr):
            continue
        saved.append((mod, attr, getattr(mod, attr)))
        setattr(mod, attr, tmp_path / filename)

    yield

    for mod, attr, old in saved:
        setattr(mod, attr, old)


# ---------------------------------------------------------------- 环境依赖守卫
# 立项（2026-10-26）：CI 自有历史 17/17 全红，40 个失败（36 FAILED + 4 ERROR）
# 逐条查过 traceback，根因只有三类，**没有一条是代码缺陷**：
#   ① market.db 不入库（旁路补偿库，删即回滚）—— 14 例 + 4 例 robustness 夹具；
#   ② training/hypotheses 登记册是发现流水线的生成物，克隆里没有 —— 2 例；
#   ③ statsmodels 是不在 requirements 的可选科学栈（GLM 拟合），CI 没装 —— 19 例；
#   ④ Windows 中文字体只有本机有，Linux 上 matplotlib 缺字形 —— 2 例。
# 处理：三个标记 + 本钩子。本机（全环境）照跑；CI / 全新克隆 / 无数据环境
# **自动跳过**，且理由逐项点名缺什么——不做笼统的"环境不支持"。
# ★ 纪律：新用例按同样根因归类打标；不得把"懒得造数据"的用例标成 realdata
#   （那是用跳过掩盖可构造的测试）。分类台账见 docs/CI环境依赖隔离_结论.md。

import importlib.util
from pathlib import Path
from typing import List, Optional

_REPO_ROOT = Path(__file__).resolve().parent


def _market_db_exists() -> bool:
    """与 data/prize_amounts._db_path 同源（repo 相对），不认硬编码绝对路径。"""
    return (_REPO_ROOT / "market" / "market.db").exists()


def _statsmodels_installed() -> bool:
    return importlib.util.find_spec("statsmodels") is not None


def _hypothesis_registry_populated() -> bool:
    """至少一个彩种的登记册 JSON 在（import 时 mkdir 会造出空目录，不能只看目录）。"""
    d = _REPO_ROOT / "training" / "hypotheses"
    return d.is_dir() and any(d.glob("*_hypotheses.json"))


HAS_MARKET_DB = _market_db_exists()
HAS_STATSMODELS = _statsmodels_installed()
HAS_REGISTRY = _hypothesis_registry_populated()

_CJK_FONT: Optional[bool] = None


def _cjk_font_available() -> bool:
    """matplotlib 能否解析到中文字体（与 data/push_image._FONTS 前三项同源）。"""
    global _CJK_FONT
    if _CJK_FONT is None:
        try:
            from matplotlib import font_manager
            have = {f.name for f in font_manager.fontManager.ttflist}
            _CJK_FONT = bool(have & {"Microsoft YaHei", "SimHei", "SimSun"})
        except Exception:
            _CJK_FONT = False
    return _CJK_FONT


def _missing_env() -> dict:
    """逐标记给出缺失组件列表（空列表 = 环境满足，照跑）。"""
    return {
        "realdata": ([] if HAS_MARKET_DB else ["market/market.db"])
                    + ([] if HAS_REGISTRY else ["training/hypotheses 登记册（发现流水线生成物）"]),
        "statsmodels": [] if HAS_STATSMODELS else ["statsmodels"],
        "winfont": ([] if _cjk_font_available()
                    else ["中文字体（Microsoft YaHei / SimHei / SimSun）"]),
    }


def pytest_collection_modifyitems(config, items):
    """环境不满足的标记用例自动跳过；未标记的用例永不受影响。"""
    checks = _missing_env()
    for item in items:
        for mark, gone in checks.items():
            if mark in item.keywords and gone:
                item.add_marker(pytest.mark.skip(
                    reason=f"{mark} 环境不满足：缺少 {'、'.join(gone)}"
                           f"（CI/全新克隆/无数据环境自动跳过，本机全量照跑）"))
