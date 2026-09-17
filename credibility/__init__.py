"""
可信度层（Credibility Layer）

把"猜号"问题重新定义为"诚实度量"问题：
  1. 严格样本外(OOS)滚动回测 —— 只用历史生成号码，用未来开奖验证，杜绝穿越。
  2. 随机基线军团 + 经验 p 值 + 多重比较校正(FDR) —— 任何策略都要和"纯随机"比。
  3. 数据质量评分 + 异常=开奖质量监控 —— χ² 均匀性 / 游程检验 / 重复缺失检测 + FDR。
  4. 反大众投注模型 —— 用真实「一等奖注数」检验"热门号码组合"是否更易多人平分头奖。

设计原则：完全独立于现有预测逻辑（prediction/engine.py），只读历史开奖数据，
不改写任何已有模块；输出的是"诚实指标"，不是"中奖承诺"。
"""

from .metrics import (
    STRATEGY_MAP,
    match_score,
    expected_random_score,
    variance_random_score,
    generate_ticket,
)
from .backtest import oos_backtest
from .baseline import random_null, empirical_pvalue, benjamini_hochberg
from .data_quality import data_quality_report
from .anti_crowd import anti_crowd_report
from .bayes import bayes_calibration, bayes_report, print_bayes
from .report import run_credibility, run_cli

__all__ = [
    "STRATEGY_MAP",
    "match_score",
    "expected_random_score",
    "variance_random_score",
    "generate_ticket",
    "oos_backtest",
    "random_null",
    "empirical_pvalue",
    "benjamini_hochberg",
    "data_quality_report",
    "anti_crowd_report",
    "bayes_calibration",
    "bayes_report",
    "print_bayes",
    "run_credibility",
    "run_cli",
]
