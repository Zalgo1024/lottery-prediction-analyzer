# -*- coding: utf-8 -*-
"""ev/mechanism/gap_cache.py —— 测量结果的磁盘缓存（原子写）。

why 要缓存：B 轨 walk-forward 要在几十个切点上重拟合 β 并对 110 万组合重新排序，
双色球一次 ~20s；看板/CLI 每次重算不可接受。

⚠️ `CACHE_DIR` 是模块级变量，**必须**补进 `conftest.py::_TARGETS`，
   否则测试会把真实 cache/ 写脏（与 push_notify / ticket_size / training_loop 同类）。
缓存带 `_sig`（数据范围 **+ 阈值参数** 指纹），数据或阈值一变，缓存自动失效，
不靠人工清理。改口径时还要 bump `_PROTOCOL` —— 两者防的是不同类变化，缺一不可。
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

CACHE_DIR = Path(__file__).resolve().parents[2] / "cache" / "mechanism"

_PROTOCOL = "v4"   # v3→v4：C 轨红球形态的 MC 组合数改为随 n 缩放（≥400×n），
#      z 的 MC 种子噪声由 ~0.22 降到 ≤0.05；大乐透「连号对数」由误报 3.008 回到真值 ~2.88。
#      v2→v3：B 轨判定/z/γ 的标准误由「解析 se / 短块」改为长块 bootstrap，
#      结果数值语义变（se、ci95 字段口径不同）；指纹另加入全部阈值参数（见 make_sig）。


def _path(lottery: str) -> Path:
    return Path(CACHE_DIR) / f"gap_{lottery}.json"


def make_sig(lottery: str, n: int, last_issue: str, *,
             min_train: int, step: int, min_effect: float,
             c_B: int, c_mc: int) -> str:
    """
    指纹 = 协议版本 + 数据范围 + **全部阈值参数**。

    ⚠️ 阈值参数**必须**进指纹，而且这里刻意**不给默认值**（全部 keyword-only 必填）：
       `gap_cache` 不能 import `gap_verdict` 的常量（会循环依赖），若在此处复写
       一份默认值就会漂移；强制调用方显式传，唯一调用方 `measure_gap` 手里正握着
       这五个值。漏传 ⇒ TypeError（响亮失败），而不是静默用旧阈值的结果 ——
       对一个测量工具来说，「调了参数没重算」比报错危险得多。

    浮点用 `:.6g` 归一（0.03 与 0.030000000000000002 必须同指纹）。
    """
    return (f"{_PROTOCOL}|{lottery}|n={n}|last={last_issue}"
            f"|min_train={int(min_train)}|step={int(step)}"
            f"|min_effect={float(min_effect):.6g}"
            f"|c_B={int(c_B)}|c_mc={int(c_mc)}")



def load(lottery: str, sig: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """读缓存。sig 不匹配（或缓存文件损坏/无 sig）→ None，由调用方重算。"""
    p = _path(lottery)
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as fh:
            obj = json.load(fh)
    except Exception:                                    # noqa: BLE001
        return None
    if sig is not None and str(obj.get("_sig")) != str(sig):
        return None
    return obj


def save(lottery: str, obj: Dict[str, Any], sig: Optional[str] = None) -> str:
    """原子写（临时文件 + os.replace）。返回落盘路径。"""
    from ev.mechanism.common import atomic_write_json
    p = _path(lottery)
    payload = dict(obj)
    payload["_sig"] = sig
    payload["_cached_at"] = datetime.now().isoformat(timespec="seconds")
    atomic_write_json(str(p), payload)
    return str(p)


def clear(lottery: Optional[str] = None) -> int:
    """删除缓存（lottery=None 表示全部）。返回删除文件数。"""
    if lottery is not None:
        targets = [_path(lottery)]
    else:
        targets = list(Path(CACHE_DIR).glob("gap_*.json")) if os.path.isdir(CACHE_DIR) else []
    k = 0
    for t in targets:
        try:
            os.remove(t)
            k += 1
        except OSError:
            pass
    return k
