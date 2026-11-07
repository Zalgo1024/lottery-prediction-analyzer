# -*- coding: utf-8 -*-
"""Walk-forward 的口径披露（候选数 / 选择窗 / 评估窗 / 是否重叠）。

为什么需要它（2026-11-07）
-------------------------
外部参考程序用「在最近 35–50 期里从 N 套权重中挑一套」来做配置选择，然后
**在同一段数据上**报告效果。候选数越多、窗口越短，挑出来的「最好」越可能只是噪声，
而它从不报候选数与窗口 —— 读者无从判断这个「最优」有多少是选出来的。

本项目没有「N 套 profile 里挑一套」这一步（`data/ticket_size.decide_next_count`
是 z 检验分支，`ev/popularity_proxy.walk_forward_beta` 是逐块**拟合** β 而非挑选），
所以这里提供的是一个**通用披露工具**：
  · 已有的 walk-forward 用它表明「训练窗 / 评估窗怎么切、有没有候选选择」；
  · 将来若真的引入多候选选择，必须先用它算出「选择噪声是否大于效应」再谈效果。

纯函数，不读盘、不依赖时间。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["SELECTION_NOTE", "disclose"]

SELECTION_NOTE = ("候选数越多、选择窗越短，「最优」越可能只是噪声；"
                  "选择窗与评估窗重叠时，读数是**选择出来的**，不是样本外表现。"
                  "本读数只作披露，不参与判决。")


def disclose(*, train_n: Optional[int] = None, eval_n: Optional[int] = None,
             candidates: Optional[int] = None,
             overlap: Optional[bool] = None,
             note: str = "") -> Dict[str, Any]:
    """把「窗口与候选数」翻译成一句可判断的话。

    参数：
      · `train_n`  选择/拟合用的期数（选择窗长度）
      · `eval_n`   被评估的期数（评估窗长度）
      · `candidates` 参与挑选的候选数；`None` = 该流程**没有**候选选择（拟合/单一路径）
      · `overlap`  选择窗与评估窗是否重叠；`None` = 不适用（没有选择这一步）

    判读三档（阈值是可复核的规则，不是打分）：
      · 有候选 且 (候选数 ≥ 50 或 选择窗 < 50 期) ⇒ 选择噪声可能大于效应
      · 选择窗与评估窗重叠 ⇒ 读数属样本内，不能当样本外表现
      · 其余 ⇒ 披露窗口即可
    """
    out: Dict[str, Any] = {
        "选择窗期数": train_n,
        "评估窗期数": eval_n,
        "候选数": candidates,
        "窗口重叠": overlap,
        "说明": SELECTION_NOTE,
    }

    if candidates is None:
        out["判读"] = ("无候选选择（拟合/单一路径）：不存在「从 N 个里挑最好」的噪声放大；"
                       "仍须注意评估窗是否与拟合窗重叠")
    elif overlap:
        out["判读"] = ("选择窗与评估窗重叠 ⇒ 读数是样本内最优，不能当作样本外表现；"
                       "请改用留出窗或嵌套切分")
    elif (candidates or 0) >= 50 or (train_n is not None and 0 < train_n < 50):
        out["判读"] = (f"候选 {candidates} 套 / 选择窗 {train_n} 期 ⇒ "
                       "选择噪声可能大于效应：先报候选数与窗口，再谈效果")
    else:
        out["判读"] = (f"候选 {candidates} 套 / 选择窗 {train_n} 期 ⇒ "
                       "窗口与候选数在可接受范围（仍需独立评估窗确认）")

    if note:
        out["补充"] = note
    return out
