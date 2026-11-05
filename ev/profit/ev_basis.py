# -*- coding: utf-8 -*-
"""ev/profit/ev_basis.py —— EV 绝对水平的口径标注（单一真源，离线研究）。

背景（`ev/profit/select.py::ev_proxy` 2026-11-04 修复）
------------------------------------------------------
旧实现把内层概率乘了两遍：`p_j[j]` 已经含过一次概率，外层又乘了一次。
实测同一口径下 monkeypatch 复原：仅固定奖 `0.0934 → 0.4863`、含浮动奖
`0.2457 → 1.1754` ⇒ 旧读数的**绝对水平只有真实的约 1/5**（19.2% / 20.9%）。

★ 为什么这个 bug 藏了很久：同彩种内每票的内层结构相同 ⇒ **排序不变**，
  所有「比大小」测试全绿；只有绝对值对账才抓得到。所以：
  · 历史读数的**判据方向与排序**仍可比；
  · 历史读数的**绝对金额（元/注、净EV、κ* 的绝对刻度）**不可与修复后直接比较。

本模块只提供口径元数据，不改任何计算、不接生产选号、不改下注量。
读数落盘的唯一出口是 `web/health_digest.py::refresh_strategy_robustness()`，
由它调用 `annotate()` 把口径写进策略鲁棒性缓存；`strategy_robustness()`
读取时透传，老缓存缺字段时显式写「未标注 + 原因」（缺席≠通过）。
"""
from __future__ import annotations

from typing import Any, Dict

CURRENT_EV_BASIS = "ev_proxy_fixed_2026-11-04"
LEGACY_EV_BASIS = "legacy_inner_probability_double_counted"

FIX_NOTE = (
    "2026-11-04 修复 ev/profit/select.py::ev_proxy：旧实现把内层概率乘了两遍，"
    "绝对 EV 水平只有真实的约 1/5（实测固定奖 19.2%、含浮动奖 20.9%）。"
    "同彩种内层结构相同 ⇒ 排序不变，比大小测试全绿，只有绝对值对账抓得到。"
)
LEGACY_CAVEAT = (
    "2026-11-04 之前的历史读数 / 缓存 / 日志基于旧 EV 水平："
    "判据方向与排序可比，绝对金额（元每注、净EV、κ* 绝对刻度）不可直接比较。"
)
UNANNOTATED_REASON = "未标注（老缓存，原因：生成于口径标注接入前）"


def annotate(doc: Dict[str, Any]) -> Dict[str, Any]:
    """给读数文档加口径元数据，返回**新 dict**（不改入参，方便纯函数对账）。"""
    out = dict(doc)
    out["EV口径"] = CURRENT_EV_BASIS
    out["EV口径说明"] = FIX_NOTE
    out["历史读数说明"] = LEGACY_CAVEAT
    return out


def legacy_label() -> Dict[str, str]:
    """旧口径标签：供未来回溯历史读数时显式标出，不参与任何当前计算。"""
    return {"EV口径": LEGACY_EV_BASIS, "历史读数说明": LEGACY_CAVEAT}
