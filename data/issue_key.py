# -*- coding: utf-8 -*-
"""data/issue_key.py —— 跨源期号归一化（唯一实现）。

为什么需要它
------------
同一期在不同数据源里期号格式不同，实测至少三套：

    CSV（双色球/大乐透）  `26120`     两位年 + 三位序号
    中彩网 cwl            `2026120`   四位年 + 三位序号
    体彩 sporttery        `07001`     两位年 + 三位序号
    双色球 2003~2009      `3001`      **一位年** + 三位序号（最容易踩）

不归一会导致 `market.db` 的奖级注数与 CSV 财务字段**一行都 join 不上**。
更隐蔽的是「一位年」那一档：曾把 `3001` 的前两位（"30"）当年份判成 2030，
让 979 期被当成「未来」静默丢弃（可见期数 2681 而非 3506）。

只依赖标准库，供 `data/` 与 `ev/` 双向复用（`ev/` 不能反向被 `data/` 依赖）。
"""
from __future__ import annotations

from typing import Any, Tuple


class IssueKeyError(ValueError):
    """期号无法归一化。"""


def issue_key(lottery: str, issue: Any) -> Tuple[int, int]:
    """期号 → (年, 序号)。`lottery` 目前不参与规则，保留以便将来区分。"""
    s = "".join(ch for ch in str(issue or "") if ch.isdigit())
    if not s:
        raise IssueKeyError(f"非法期号: {issue!r}")
    if len(s) >= 7:                       # 2026120
        return int(s[:4]), int(s[4:])
    if len(s) == 5:                       # 26120 / 07001 → 两位年
        yy = int(s[:2])
        return (2000 + yy if yy < 90 else 1900 + yy), int(s[2:])
    if len(s) == 4:
        # ★ 双色球的 2003~2009 是**一位年**：`3001` = 2003 年第 1 期。
        yy = int(s[:1])
        return (2000 + yy if yy < 90 else 1900 + yy), int(s[1:])
    raise IssueKeyError(f"无法识别的期号格式: {issue!r}")
