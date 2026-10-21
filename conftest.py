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
