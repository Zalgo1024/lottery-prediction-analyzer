# -*- coding: utf-8 -*-
"""生产四包 import 护栏的**单一真源**（三条前缀护栏共用）。

背景（2026-11-06 接线）
-----------------------
影子验证要在生产出号后写「开奖前冻结」证据，因此必须有被显式放行的接入点。
原则不变：**生产四包内不得出现研究 / 验证模块的 import 与调用**；
变化的是「允许谁去碰」——从「永远禁止」改为「收敛到唯一编排层 + 两个出号入口」。

两档白名单（改大即红，见 tests/test_prefix_*.py 的 `test_whitelist_is_minimal`）
----------------------------------------------------------------------------
1. `RESEARCH_MODULES`（研究 / 验证模块）**只允许**出现在 `ev/shadow_bridge.py`；
2. `ev.shadow_bridge`（编排层）**只允许**被 `cli.py` 与 `web/utils.py` 调用。

为什么不能只靠「三处各写一遍 substring」：三份清单会漂移，改一处漏两处，
而漂移方向通常是「不小心放宽」。所以判定收敛到本模块的 `check_text()`，
三条护栏各自独立调用它（仍保留三处独立断言，真源被削弱时会红）。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Tuple, Iterator

ROOT = Path(__file__).resolve().parents[1]

SCAN_DIRS = ("prediction", "web", "pipeline", "data")
SCAN_FILES = ("run.py", "cli.py")

# 研究 / 验证模块：只允许编排层 import
RESEARCH_MODULES = (
    "ev.prefix_curve",
    "ev.order_freeze",
    "ev.prefix_shadow",
    "ev.prefix_real",
    "ev.draw_schedule",
    # 方案 B 证据层与结算编排（2026-11-06）：同样只准编排层碰。
    "ev.draw_evidence",
    "ev.shadow_settle",
)

# 编排层：只允许被两个出号入口调用
BRIDGE_MODULE = "ev.shadow_bridge"

# ★ 白名单必须**恰好**是这些（不许多）。改这里要同时改 test_whitelist_is_minimal。
ALLOWED_RESEARCH_IMPORTERS = frozenset({"ev/shadow_bridge.py"})
ALLOWED_BRIDGE_CALLERS = frozenset({"cli.py", "web/utils.py"})


def markers(mod: str) -> Tuple[str, str]:
    """同一模块的两种常见写法：`ev.x` 与 `from ev import x`。

    ★ 只查 `ev.x` 会漏掉 `from ev import x` —— 早期三条护栏各自手写两种写法，
      合并成真源时如果只留一种，护栏会静默变松（本轮实测踩到）。
    """
    tail = mod.split(".", 1)[1] if "." in mod else mod
    return (mod, "from ev import " + tail)


def check_text(rel: str, text: str) -> List[Tuple[str, str, str]]:
    """纯函数：判定单份源码是否违例。返回 [(相对路径, 模块, 原因)]。

    抽成纯函数是为了能对「注入的坏文本」做负向测试 —— 不必真的改生产文件。
    """
    out: List[Tuple[str, str, str]] = []
    for mod in RESEARCH_MODULES:
        if rel in ALLOWED_RESEARCH_IMPORTERS:
            continue
        if any(m in text for m in markers(mod)):
            out.append((rel, mod,
                        "研究/验证模块只允许出现在编排层 ev/shadow_bridge.py"))
    if rel not in ALLOWED_BRIDGE_CALLERS:
        if any(m in text for m in markers(BRIDGE_MODULE)):
            out.append((rel, BRIDGE_MODULE,
                        "编排层只允许被出号入口 cli.py / web/utils.py 调用"))
    return out


def scan_files() -> Iterator[Path]:
    for d in SCAN_DIRS:
        base = ROOT / d
        if base.is_dir():
            yield from sorted(base.rglob("*.py"))
    for f in SCAN_FILES:
        p = ROOT / f
        if p.is_file():
            yield p


def violations() -> List[Tuple[str, str, str]]:
    """扫描真实生产路径，返回全部违例。空列表 = 通过。"""
    out: List[Tuple[str, str, str]] = []
    for p in scan_files():
        rel = p.relative_to(ROOT).as_posix()
        text = p.read_text(encoding="utf-8", errors="replace")
        out.extend(check_text(rel, text))
    return out


def bridge_source() -> str:
    return (ROOT / "ev" / "shadow_bridge.py").read_text(encoding="utf-8")
