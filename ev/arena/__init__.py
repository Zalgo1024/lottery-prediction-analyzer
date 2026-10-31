# -*- coding: utf-8 -*-
"""ev/arena —— 双目标 × 共同预算 的**统一对照场**。

为什么需要这个包
----------------
`ev/tier_backtest.py`、`ev/lowcrowd_backtest.py`、`ev/coverage_backtest.py`
三套回测各自有自己的期窗、池子、seed 派生、成本口径与 cap 口径（见方案 §1.2 的
G1~G5）。把它们的结果放在一起比较时，差异里混着**口径差异**，读不出来策略差异。

本包把「同一批期、同一批池、同一套成本」做成**结构性不变量**：
  · `periods.assert_same_periods` —— 期窗必须逐位相等，不允许求交/取众数/截断；
  · `arms` 的 seed 派生与全局 RNG 隔离；
  · `replay` 的结算只有一条实现，所有臂共用。

诚实边界（与项目其它模块一致）
------------------------------
本包只做**对照测量**。它不改变任何一注的中奖概率，也不产出「买什么号」的建议；
结论一律以「功效是否足以识别差异」表述，不写成「已证明无效」或「已证明有效」。

★ 本轮（2026-10-24）不改生产选号：arena 是**研究入口**，不写 pending，
  不接调度器，不被 `prediction/engine` 调用。
"""
from __future__ import annotations

from config import ticket_price

__all__ = ["types", "periods", "design", "arms", "replay", "stats", "tier_metrics"]

# 单注成本（元）。★ 所有成本计算必须走这里，禁止在别处写裸 2.0。
#   2026-10-30 改为从 config 读：唯一真源 config.LOTTERY_CONFIG[*].payout.cost。
COST_PER_BET = float(ticket_price("双色球"))

# 主终点默认：单期实得 ≥ 10 元
DEFAULT_MIN_PAYOUT = 10.0
