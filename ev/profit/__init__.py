# -*- coding: utf-8 -*-
"""ev/profit —— 盈利门槛策略（净EV 门槛 → 选票 → 不参与判断）。

研究用途：**不预测号码、不保证盈利、不开启自动投注**。
默认只写影子台账（jsonl），永不写 pending。

诚实边界：κ=0（开奖公平）时六彩种单注 EV 全部为负，且缺口需 8~10 倍机制强度才抹平。
任何一期若输出「可参与」，应先按 bug 排查（见 solvers.verdict 的交叉校验）。
"""

__version__ = "0.1.0"

# 策略名（进 data.feedback.NON_LEARNING_STRATEGIES 后自动落「研究」台账域，
# 但因本策略走 jsonl 台账、永不写 pending，实际不占用研究域的槽位）
PROFIT_STRATEGY = "盈利策略"
PROFIT_STRATEGY_SCENARIO = "盈利策略·情景倍数"
PROFIT_STRATEGY_MC = "盈利策略·蒙特卡洛"

ALL_STRATEGIES = (PROFIT_STRATEGY, PROFIT_STRATEGY_SCENARIO, PROFIT_STRATEGY_MC)
