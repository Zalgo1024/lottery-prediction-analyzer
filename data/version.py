# -*- coding: utf-8 -*-
"""版本冻结 —— 把「这批号码是哪份代码、哪套参数跑出来的」固化成字符串。

为什么需要它
------------
同一句「模型变强了」，在不同代码版本下可能完全不是一回事。没有版本标记时，
复盘只能靠 `批次时间` 去猜当时跑的是哪个 commit —— 而 commit 会 amend、
参数会被就地改掉、配置会被覆盖。事后想复现，只能重跑并祈祷。

`预测版本` 的构成：
    <git short sha>[-dirty]-<策略指纹 8 位>

`-dirty` 是**刻意保留**的：工作区有新改动时，sha 指向的代码并不是实际跑的那份。
把 dirty 藏掉会让「可复现」变成一句空话；宁可承认它不干净，也好过假装干净。

★ `code_sha()` 失败时返回 `"unknown"`，**不回退空串**：空串在报表里会被当成
  「没有这一列」，而 unknown 会让人去查为什么拿不到。
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import subprocess
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
_UNKNOWN = "unknown"


@functools.lru_cache(maxsize=1)
def code_sha() -> str:
    """`<short sha>[-dirty]`；拿不到就是 `unknown`。"""
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=5,
        )
        if sha.returncode != 0:
            return _UNKNOWN
        short = sha.stdout.strip() or _UNKNOWN
        st = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            cwd=str(BASE_DIR), capture_output=True, text=True, timeout=5,
        )
        dirty = bool(st.stdout.strip()) if st.returncode == 0 else False
        return f"{short}-dirty" if dirty else short
    except Exception as e:                                    # noqa: BLE001
        logger.debug(f"取 git sha 失败（不影响出号）：{e}")
        return _UNKNOWN


def strategy_fingerprint(params: Optional[Any] = None) -> str:
    """策略参数指纹（8 位）。排序后序列化 ⇒ 字典顺序不影响结果。"""
    try:
        blob = json.dumps(params if params is not None else {}, ensure_ascii=False,
                          sort_keys=True, default=str)
    except Exception:                                         # noqa: BLE001
        blob = repr(params)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:8]


def prediction_version(params: Optional[Any] = None) -> str:
    return f"{code_sha()}-{strategy_fingerprint(params)}"


def file_hash(path: Optional[Path]) -> Optional[str]:
    """文件内容哈希（前 16 位）。文件不存在返回 `None` —— 不返回空串，也不伪造。"""
    if path is None:
        return None
    try:
        p = Path(path)
        if not p.exists():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    except OSError:
        return None
