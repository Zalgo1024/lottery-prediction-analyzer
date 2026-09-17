"""
CLI 入口 — 所有子命令集中在此
使用 argparse 构建，后续可套 Web 界面
"""

import argparse
import sys
from datetime import datetime
from pathlib import Path

from config import LOTTERY_CONFIG, TRIALS_ENABLED, resolve_groups, TRIAL_ARMS
from logs.logger import setup_logger, set_module_level

logger = setup_logger("cli")


def main():
    # GBK 控制台/重定向兜底：CLI 输出含 ℹ️✅⚠️ 等非 GBK 字符时
    # 降级为 ? 而不是 UnicodeEncodeError 崩溃（曾致 update 命令 exit=1，
    # 调度日志显示"七星彩每日补抓失败"，实际数据已更新成功）
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(errors="replace")
        except (AttributeError, ValueError):
            pass
    parser = argparse.ArgumentParser(
        prog="lottery",
        description="彩票预测分析系统 — 双色球 & 大乐透",
        allow_abbrev=False,  # 关闭长选项缩写，避免 --retrain 与 --retrain-gated 前缀冲突
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="详细输出（DEBUG 级别日志）",
    )
    subparsers = parser.add_subparsers(dest="command", help="可用子命令")

    # ----- update -----
    update_parser = subparsers.add_parser("update", help="从 500.com 抓取最新开奖数据并更新本地 CSV")
    update_parser.add_argument(
        "lottery",
        nargs="?",
        choices=list(LOTTERY_CONFIG.keys()),
        default=None,
        help="指定彩票类型（留空则全部更新）",
    )

    # ----- auto -----
    auto_parser = subparsers.add_parser("auto", help="自动流水线：抓取→评估→更新权重→预测（开奖后一条龙）")
    auto_parser.add_argument(
        "lottery",
        nargs="?",
        choices=list(LOTTERY_CONFIG.keys()),
        default=None,
        help="指定彩票类型（留空则全部处理）",
    )
    auto_parser.add_argument(
        "--skip-predict",
        action="store_true",
        help="只更新数据+评估，不自动生成下一期预测",
    )
    auto_parser.add_argument(
        "--mode",
        choices=["fresh", "trained", "rule"],
        default="fresh",
        help="自动预测使用的模式（默认 fresh）",
    )
    auto_parser.add_argument(
        "--groups",
        type=int,
        default=None,
        help="每期出号组数（默认按彩种：数字型 5 / 乐透型 100；显式给出则覆盖）",
    )
    auto_parser.add_argument(
        "--retrain",
        action="store_true",
        help="在流水线末尾执行一次模型重训（滚动回测），结果记入运行状态",
    )
    auto_parser.add_argument(
        "--gated-retrain",
        action="store_true",
        help="重训并过闸门：新模型命中率必须显著高于解析随机基准μ才保留，否则丢弃（诚实防幻觉）",
    )
    auto_parser.add_argument(
        "--no-anomaly",
        action="store_true",
        help="跳过自动异常检测步骤（默认会跑 4 法异常检测并告警）",
    )

    # ----- lag -----
    lag_parser = subparsers.add_parser("lag", help="检查各彩种数据滞后天数（白天兜底用：按最近应开奖日 vs 数据最新日期）")
    lag_parser.add_argument(
        "lottery",
        nargs="?",
        choices=list(LOTTERY_CONFIG.keys()),
        default=None,
        help="指定彩票类型（留空则全部）",
    )
    lag_parser.add_argument(
        "--json",
        action="store_true",
        help="输出 JSON（供 auto_scheduled.ps1 白天模式解析）",
    )

    # ----- clean -----
    clean_parser = subparsers.add_parser("clean", help="清洗原始 CSV 数据")
    clean_parser.add_argument(
        "lottery",
        nargs="?",
        choices=list(LOTTERY_CONFIG.keys()),
        default=None,
        help="指定彩票类型（留空则全部清洗）",
    )

    # ----- stats -----
    stats_parser = subparsers.add_parser("stats", help="统计概览（频率、冷热号、遗漏值）")
    stats_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )

    # ----- pipeline -----
    pipe_parser = subparsers.add_parser("pipeline", help="执行计算管道（5 步：概率→期望→分布→风险→风控）")
    pipe_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    pipe_parser.add_argument(
        "--steps",
        default="1-5",
        help="步骤范围，如 1-2 或 3-5（默认 1-5）",
    )
    pipe_parser.add_argument("--all", action="store_true", help="执行全部 5 步")

    # ----- train -----
    train_parser = subparsers.add_parser("train", help="训练模式（滚动窗口回测 + 参数优化 + ML 模型）")
    train_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    train_parser.add_argument(
        "--iterations", type=int, default=None, help="迭代次数（默认 500）"
    )
    train_parser.add_argument(
        "--window", type=int, default=None, help="滚动窗口大小（期数，默认 50）"
    )
    train_parser.add_argument(
        "--window-list",
        type=lambda s: [int(x) for x in s.split(",")],
        default=None,
        help="多窗口对比，逗号分隔，如 30,50,100",
    )
    train_parser.add_argument(
        "--model",
        choices=["statistical", "logistic", "random_forest", "lightgbm"],
        default=None,
        help="权重方案（默认 statistical；logistic/random_forest/lightgbm 需相应依赖）",
    )
    train_parser.add_argument(
        "--features",
        choices=["standard", "rich"],
        default="standard",
        help="特征版本：standard=原特征；rich=丰富特征(动量/冷热斜率/共现)，需配合 retrain 重新训练",
    )
    train_parser.add_argument(
        "--search",
        action="store_true",
        help="先跑参数自动搜索，用统一评分尺挑最优(窗口/模型/特征)，再以最优配置训练",
    )

    # ----- search -----
    search_parser = subparsers.add_parser(
        "search", help="参数自动搜索（用统一评分尺在历史上一期期滑窗验证，挑最优配置）"
    )
    search_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    search_parser.add_argument(
        "--windows",
        type=lambda s: [int(x) for x in s.split(",")],
        default="30,50,100",
        help="候选窗口列表，逗号分隔，如 30,50,100",
    )
    search_parser.add_argument(
        "--models",
        type=lambda s: [x.strip() for x in s.split(",")],
        default="logistic,random_forest,lightgbm,statistical",
        help="候选模型，逗号分隔",
    )
    search_parser.add_argument(
        "--features",
        choices=["standard", "rich"],
        default="standard",
        help="特征版本",
    )
    search_parser.add_argument(
        "--optuna",
        action="store_true",
        help="改用 Optuna 贝叶斯搜索（自动探索窗口/特征/模型 + 模型条件超参；"
             "对全部试次的「选号 vs 随机」p 值做 BH-FDR 校正，输出 q 值与 verdict，"
             "防赢家诅咒。需 pip install optuna）",
    )
    search_parser.add_argument(
        "--trials",
        type=int,
        default=50,
        help="Optuna 试次数（默认 50，仅 --optuna 时生效）",
    )
    search_parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="随机种子（默认 42）",
    )

    # ----- predict -----
    pred_parser = subparsers.add_parser("predict", help="预测模式（生成 1-10 组号码 + 置信度评分 + 报告）")
    pred_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    pred_parser.add_argument(
        "--groups",
        type=int,
        default=None,
        help="输出号码组数（留空则按「出号数量」配置：固定/动态，见看板滑轨；显式给出则覆盖）",
    )
    pred_parser.add_argument(
        "--mode",
        choices=["fresh", "trained", "rule", "robust"],
        default=None,
        help="预测模式：fresh 即时统计 / trained 加载训练参数 / rule 规则优选 / robust 三档鲁棒出号",
    )
    pred_parser.add_argument(
        "--tier",
        choices=["general", "robust", "high", "all"],
        default="robust",
        help="三档档位（仅 --mode robust 生效）：general 一般 / robust 稳健(避雷) / high 高鲁棒 / all 三档全出（默认 robust）",
    )
    pred_parser.add_argument(
        "--windows",
        default="30,50,80,120",
        help="稳健/高鲁棒档的共识窗口，逗号分隔（默认 30,50,80,120）",
    )
    pred_parser.add_argument(
        "--seeds", type=int, default=4,
        help="稳健/高鲁棒档每窗口种子数（默认 4）",
    )

    # ----- history -----
    hist_parser = subparsers.add_parser("history", help="查看历史训练/预测记录")
    hist_parser.add_argument(
        "--days", type=int, default=7, help="显示最近几天的记录（默认 7）"
    )

    # ----- logs -----
    logs_parser = subparsers.add_parser("logs", help="查看日志摘要和最近记录")
    logs_parser.add_argument(
        "--lines", type=int, default=20, help="显示行数（默认 20）"
    )
    logs_parser.add_argument(
        "--level", choices=["DEBUG", "INFO", "WARN", "ERROR"], default="INFO",
        help="过滤级别（默认 INFO）",
    )

    # ----- anomaly -----
    anomaly_parser = subparsers.add_parser("anomaly", help="人为干涉检测（4种方法：卡方/Benford/区间/冷热号突变）")
    anomaly_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    anomaly_parser.add_argument(
        "--threshold",
        choices=["strict", "normal", "loose", "all"],
        default=None,
        help="异常判定严格度（all = 三档对比）",
    )

    # ----- evaluate -----
    eval_parser = subparsers.add_parser("evaluate", help="评估 pending 预测（开奖后对比命中，更新策略权重）")
    eval_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    eval_parser.add_argument(
        "--summary",
        action="store_true",
        help="只查看反馈摘要，不执行评估",
    )
    eval_parser.add_argument(
        "--lookback",
        type=int,
        default=50,
        help="摘要统计的回看期数（默认 50）",
    )

    # ----- credibility -----
    cred_parser = subparsers.add_parser(
        "credibility",
        help="可信度层：OOS回测+随机基线军团+数据质量+反大众（诚实自检，不承诺中奖率）",
    )
    cred_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    cred_parser.add_argument("--k", type=int, default=8, help="每折叠生成票数（默认 8）")
    cred_parser.add_argument("--min-train", type=int, default=100, help="最小训练期数（默认 100）")
    cred_parser.add_argument("--army", type=int, default=2000, help="随机基线军团规模（默认 2000）")
    cred_parser.add_argument("--alpha", type=float, default=0.05, help="FDR 显著性水平（默认 0.05）")
    cred_parser.add_argument("--bayes-tau", type=float, default=None,
                             help="贝叶斯第⑤支柱：怀疑先验标准差τ（默认 0.01×μ）")
    cred_parser.add_argument("--bayes-p-edge", type=float, default=0.25,
                             help="贝叶斯先验 P(真有edge)，默认 0.25（即'大概率没 edge'）")
    cred_parser.add_argument("--no-bayes", action="store_true", help="跳过贝叶斯校准（第⑤支柱）")

    # ----- bayes (独立子命令：仅第⑤支柱) -----
    bayes_parser = subparsers.add_parser(
        "bayes",
        help="可信度层·第⑤支柱：贝叶斯校准（孤立edge后验概率，独立运行）",
    )
    bayes_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    bayes_parser.add_argument("--k", type=int, default=8, help="每折叠生成票数（默认 8）")
    bayes_parser.add_argument("--min-train", type=int, default=100, help="最小训练期数（默认 100）")
    bayes_parser.add_argument("--bayes-tau", type=float, default=None,
                              help="怀疑先验标准差τ（默认 0.01×μ）")
    bayes_parser.add_argument("--bayes-p-edge", type=float, default=0.25,
                              help="先验 P(真有edge)，默认 0.25（即'大概率没 edge'）")

    # ----- plan -----
    plan_parser = subparsers.add_parser(
        "plan",
        help="资金计划（任务2.1+2.2）：Kelly比例 + 资金预算 + 蒙特卡洛模拟",
    )
    plan_parser.add_argument("--ev", type=float, default=-0.96, help="单注EV（元，默认排列3直选-0.96）")
    plan_parser.add_argument("--prob", type=float, default=1/1000, help="中奖概率（默认1/1000）")
    plan_parser.add_argument("--cost", type=float, default=2.0, help="单注成本（默认2元）")
    plan_parser.add_argument("--bankroll", type=float, default=1000.0, help="本金（默认1000元）")
    plan_parser.add_argument("--periods", type=int, default=100, help="蒙特卡洛期数（默认100）")
    plan_parser.add_argument("--sim", action="store_true", help="EV>0 时运行蒙特卡洛模拟")

    # ----- rollover -----
    ro_parser = subparsers.add_parser(
        "rollover",
        help="双/大 Rollover-EV（任务2.3）：奖池滚动窗口判定，头奖期望贡献",
    )
    ro_parser.add_argument("lottery", choices=["双色球", "大乐透"], help="彩票类型")
    ro_parser.add_argument("--issue", default=None, help="期号（默认最新一期）")
    ro_parser.add_argument("--history", type=int, default=0, help="输出最近 N 期滚动趋势（默认仅当期）")

    # ----- ev -----
    ev_parser = subparsers.add_parser(
        "ev",
        help="EV计算（任务1.2）：名义EV/限号EV/限号暴露概率，冷门号TOP",
    )
    ev_parser.add_argument("lottery", choices=["排列5", "福彩3D", "排列3", "七星彩", "双色球", "大乐透"],
                           help="彩票类型")
    ev_parser.add_argument("--ticket", default=None, help="直选号码，如 123（3D类）/ 12345（排列5）/ 1234567（七星彩）")
    ev_parser.add_argument("--issue", default=None, help="期号（默认最新一期）")
    ev_parser.add_argument("--cold", type=int, default=None, help="输出限号EV最高(冷门)TOP N")
    ev_parser.add_argument("--hot", default=None, help="输出指定大众号（如生日号）对比EV")
    ev_parser.add_argument("--sample", type=int, default=None,
                           help="约束采样出号 N 组（反大众+形态约束+低重叠）")
    ev_parser.add_argument("--sum-range", default=None, help="和值范围，如 9-18")
    ev_parser.add_argument("--max-consec", type=int, default=2, help="最大连号长度（默认2）")

    # ----- payout（NetPayout WP0：实得奖金统一口径）-----
    payout_parser = subparsers.add_parser(
        "payout",
        help="实得奖金统一口径（NetPayout WP0）：各奖级概率×实得单注+净EV",
    )
    payout_parser.add_argument("lottery", choices=["排列5", "福彩3D", "排列3", "七星彩", "双色球", "大乐透"],
                               help="彩票类型")
    payout_parser.add_argument("--ticket", default=None,
                               help="数字型号码：123（3D类）/ 12345（排列5）/ 1234567（七星彩）")
    payout_parser.add_argument("--red", default=None, help="乐透型红球（前区），逗号分隔，如 1,2,3,4,5,6")
    payout_parser.add_argument("--blue", default=None, help="乐透型蓝球（后区），逗号分隔，如 7")
    payout_parser.add_argument("--issue", default=None, help="期号（默认最新一期）")

    # ----- select（NetPayout WP2：按实得奖金选号）-----
    sel_parser = subparsers.add_parser(
        "select",
        help="按实得奖金选号（NetPayout WP2）：候选池→净EV排序→低重叠贪心",
    )
    sel_parser.add_argument("lottery", choices=["排列5", "福彩3D", "排列3", "七星彩", "双色球", "大乐透"],
                            help="彩票类型")
    sel_parser.add_argument("--groups", type=int, default=5, help="出号组数（默认 5）")
    sel_parser.add_argument("--pool", type=int, default=20000, help="随机候选池大小（默认 20000）")
    sel_parser.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")
    sel_parser.add_argument("--issue", default=None, help="期号（默认最新一期）")

    # ----- trials（NetPayout WP3：前瞻配对对照实验）-----
    tr_parser = subparsers.add_parser(
        "trials",
        help="前瞻配对对照实验（NetPayout WP3）：A=模型选号 / B=同池随机对照",
    )
    tr_parser.add_argument("lottery", choices=["双色球", "大乐透"], help="彩票类型（仅乐透型参与）")
    tr_parser.add_argument("--record", action="store_true", help="记录本期 A/B 两组号码")
    tr_parser.add_argument("--settle", action="store_true", help="结算已开奖期次的实得奖金")
    tr_parser.add_argument("--report", action="store_true", help="输出对照实验报告（默认）")
    tr_parser.add_argument("--plan", action="store_true", help="功效规划：检出中奖率差异需要多久")
    tr_parser.add_argument("--groups", type=int, default=100, help="每组注数（默认 100）")
    tr_parser.add_argument("--seed", type=int, default=None, help="随机种子（可复现）")

    # ----- drift（NetPayout 维护：人群行为漂移监控）-----
    dr_parser = subparsers.add_parser(
        "drift",
        help="人群选号行为漂移监控：冷门优势是否随时间变化 + β 前后半段对比",
    )
    dr_parser.add_argument("lottery", choices=["双色球", "大乐透"], help="彩票类型")

    # ----- sales -----
    sales_parser = subparsers.add_parser(
        "sales",
        help="抓取数字型彩种销售额+各奖级中奖注数（任务1.1：限号EV数据地基）",
    )
    sales_parser.add_argument(
        "lottery",
        nargs="?",
        default=None,
        help="彩票类型（排列5/福彩3D/排列3/七星彩），缺省=全部",
    )
    sales_parser.add_argument(
        "--all",
        action="store_true",
        help="更新全部数字型彩种",
    )

    # ----- salesmodel (P1-2 销量/池/注数预测，喂 rollover 的前瞻 EV) -----
    sm_parser = subparsers.add_parser(
        "salesmodel",
        help="P1-2 销量/奖池/一等奖注数预测：前瞻 EV 与参与信号（大乐透/双色球）",
    )
    sm_parser.add_argument("lottery", choices=["双色球", "大乐透"], help="彩票类型")
    sm_parser.add_argument("--forecast", action="store_true",
                           help="输出下一期前瞻（奖池/销量/注数/EV/顶格判定）")
    sm_parser.add_argument("--signal", action="store_true",
                           help="输出留出期信号回放（前瞻顶格判定 vs 实际，诚实校准）")
    sm_parser.add_argument("--test-frac", type=float, default=0.2,
                           help="留出占比（默认 0.2，walk-forward 切尾）")

    hyp_parser = subparsers.add_parser(
        "hypothesize",
        help="登记假设（发现闭环入口）：把可证伪的 edge 假设写入登记表，等待闸门判卷",
    )
    hyp_parser.add_argument("lottery", choices=list(LOTTERY_CONFIG.keys()), help="彩票类型")
    hyp_parser.add_argument("--name", default=None, help="假设名（--auto 时忽略）")
    hyp_parser.add_argument("--hypothesis", default=None, help="可证伪的假设内容")
    hyp_parser.add_argument("--strategy", default=None,
                            help="被检验策略（credibility_gate 型须为 高频/遗漏/区间 之一）")
    hyp_parser.add_argument("--target-metric", default=None, help="目标指标（如 OOS均分/限号EV）")
    hyp_parser.add_argument("--probe", choices=["credibility_gate", "ev_edge"],
                            default="credibility_gate", help="探测方式（默认裁判层四道闸）")
    hyp_parser.add_argument("--auto", action="store_true",
                            help="从内置模板批量生成候选假设（按名去重）")

    # ----- gate (发现闭环·P0) -----
    gate_parser = subparsers.add_parser(
        "gate",
        help="闸门判卷：裁判层四道闸 / EV边探测，更新假设状态并写准入清单",
    )
    gate_parser.add_argument("lottery", choices=list(LOTTERY_CONFIG.keys()), help="彩票类型")
    gate_parser.add_argument("--id", default=None, help="假设ID（缺省=判全部）")
    gate_parser.add_argument("--all", action="store_true",
                             help="判全部假设（缺省行为，可省略）")
    gate_parser.add_argument("--k", type=int, default=8, help="每折叠生成票数（默认 8）")
    gate_parser.add_argument("--min-train", type=int, default=100, help="最小训练期数（默认 100）")
    gate_parser.add_argument("--army", type=int, default=2000, help="随机基线军团规模（默认 2000）")
    gate_parser.add_argument("--fdr-alpha", type=float, default=0.05, help="FDR 显著性水平")
    gate_parser.add_argument("--bayes-tau", type=float, default=None,
                             help="贝叶斯怀疑先验标准差τ（默认 0.01×μ）")
    gate_parser.add_argument("--bayes-p-edge", type=float, default=0.25,
                             help="贝叶斯先验 P(真有edge)，默认 0.25")
    gate_parser.add_argument("--no-bayes", action="store_true", help="跳过贝叶斯校准")
    gate_parser.add_argument("--ev-window", type=int, default=100,
                             help="ev_edge 探测回看期数（默认 100）")

    # ----- infotheory (P2-4 信息传递量化) -----
    it_parser = subparsers.add_parser(
        "infotheory",
        help="P2-4 历史→下一期信息传递量化：MI/CMI 置换检验（≈0 是对外最硬结论）",
    )
    it_parser.add_argument("lottery", nargs="?", default=None,
                           help="彩种（缺省=6 彩种摘要）")
    it_parser.add_argument("--lags", type=int, default=5, help="最大滞后（默认 5）")
    it_parser.add_argument("--perm", type=int, default=150, help="置换次数（默认 150）")

    # ----- changepoint (P2-2 变点检测) -----
    cp_parser = subparsers.add_parser(
        "changepoint",
        help="P2-2 单变点扫描：自动找号码/数据口径突变点（供 rolling 切窗参考）",
    )
    cp_parser.add_argument("lottery", nargs="?", default=None,
                           help="彩种（缺省=全部 6 彩种）")
    cp_parser.add_argument("--perm", type=int, default=1000, help="置换次数（默认 1000）")
    cp_parser.add_argument("--min-seg", type=int, default=200, help="最小段长（默认 200）")
    cp_parser.add_argument("--v-gate", type=float, default=0.1,
                           help="Cramér V 效应量门控（默认 0.1）")
    cp_parser.add_argument("--no-write", action="store_true",
                           help="不写 discovery/changepoints.json")

    # ----- evalue (P2-3 e-value 序贯检验 / 任意停时监控) -----
    ev_parser = subparsers.add_parser(
        "evalue",
        help="P2-3 e-value 序贯监控：分区边际公平性任意停时检验（随时可喊停不误报）",
    )
    ev_parser.add_argument("lottery", nargs="?", default=None,
                           help="彩种（缺省=6 彩种族级汇总）")
    ev_parser.add_argument("--alpha", type=float, default=0.05,
                           help="族水平 α（默认 0.05，拒绝阈 T=M/α）")
    ev_parser.add_argument("--warm", type=int, default=200, help="赌注预热期数（默认 200）")
    ev_parser.add_argument("--calibrate", action="store_true",
                           help="合成数据校准：iid 族误报率 + δ 检出能力表")
    ev_parser.add_argument("--no-write", action="store_true",
                           help="不写 discovery/evalues.json")

    # ----- randomness (P2-1 NIST 风格随机性审计) -----
    ra_parser = subparsers.add_parser(
        "randomness",
        help="P2-1 随机性审计：NIST SP800-22 思路（频率/游程/最长游程/序列/累积和/块内频率）",
    )
    ra_parser.add_argument("lottery", nargs="?", default=None,
                           help="彩种（缺省=全部 6 彩种）")
    ra_parser.add_argument("--B", type=int, default=200,
                           help="MC 零分布模拟次数（默认 200）")
    ra_parser.add_argument("--block", type=int, default=500,
                           help="块内频率检验的块长（默认 500）")
    ra_parser.add_argument("--no-write", action="store_true",
                           help="不写 discovery/randomness_audit.json")

    # ----- covering (P3-1 覆盖设计出号) -----
    cv_parser = subparsers.add_parser(
        "covering",
        help="P3-1 覆盖设计：预算 N 注选号使『任意开奖 ≥t 红』保底（纯组合数学，不预测）",
    )
    cv_parser.add_argument("lottery", help="彩种（仅乐透型：双色球/大乐透）")
    cv_parser.add_argument("--t", type=int, default=3, help="保底红球数 t（默认 3）")
    cv_parser.add_argument("--groups", type=int, default=0,
                           help="注数预算（0=自动找满覆盖最小注数）")
    cv_parser.add_argument("--seed", type=int, default=20260905,
                           help="随机种子（默认固定，结果可复现）")
    cv_parser.add_argument("--no-mc", action="store_true",
                           help="跳过随机抽期验证（省时）")

    # ----- conformal (P3-2 共形预测保证) -----
    cf_parser = subparsers.add_parser(
        "conformal",
        help="P3-2 共形保证：预算 K 注 → 单期『≥1票命中』覆盖率保证（不预测号码）",
    )
    cf_parser.add_argument("lottery", help="彩种（双色球/大乐透=共形；数字型=精确公式）")
    cf_parser.add_argument("--target", default="any",
                           choices=["any", "red2", "red3"],
                           help="命中目标：any=官方中奖(≥5元级)；redN=红球地板（默认 any）")
    cf_parser.add_argument("--mode", default="random",
                           choices=["random", "cover_t2", "cover_t3"],
                           help="票策略：random=随机K注；cover_tN=覆盖设计满覆盖（默认 random）")
    cf_parser.add_argument("--K", default="",
                           help="注数网格，逗号分隔（默认 1,2,5,10,20,30,50,75,100,150,200,300）")
    cf_parser.add_argument("--cal-frac", type=float, default=0.7,
                           help="校准段占现代段比例（默认 0.7，其余为回放尾段）")
    cf_parser.add_argument("--seed", type=int, default=20260905,
                           help="随机种子（票池可复现）")
    cf_parser.add_argument("--write", action="store_true",
                           help="结果写入 discovery/conformal.json")

    # ----- robustness (鲁棒性/过拟合专用监控) -----
    rob_parser = subparsers.add_parser(
        "robustness",
        help="鲁棒性/过拟合专用监控：train-test gap(置换校准)/窗口稳定性/参数敏感性/分布漂移",
    )
    rob_parser.add_argument("lottery", choices=list(LOTTERY_CONFIG.keys()),
                            help="彩票类型")
    rob_parser.add_argument("--mode", choices=["full", "light"], default="full",
                            help="full=全套指标(乐透型每期)；light=轻量(数字型高频)")
    rob_parser.add_argument("--write", action="store_true",
                            help="落盘报告并追加趋势（training/feedback/robustness/）")

    # ----- hypotheses (发现闭环·P0) -----
    hyplist_parser = subparsers.add_parser(
        "hypotheses",
        help="列出假设登记表（状态/最近结论/promoted）",
    )
    hyplist_parser.add_argument("lottery", choices=list(LOTTERY_CONFIG.keys()), help="彩票类型")
    hyplist_parser.add_argument("--detail", action="store_true", help="显示全部历史检验记录")

    # ----- rolling -----
    rolling_parser = subparsers.add_parser("rolling", help="滚动预测训练（用历史数据模拟时序预测验证）")
    rolling_parser.add_argument(
        "lottery",
        choices=list(LOTTERY_CONFIG.keys()),
        help="彩票类型",
    )
    rolling_parser.add_argument(
        "--window",
        type=int,
        default=50,
        help="训练窗口大小（默认 50）",
    )
    rolling_parser.add_argument(
        "--eval-periods",
        type=int,
        default=100,
        help="评估期数（默认 100）",
    )

    args = parser.parse_args()

    # 全局 --verbose
    if getattr(args, "verbose", False):
        set_module_level("cli", "DEBUG")
        set_module_level("data", "DEBUG")
        set_module_level("pipeline", "DEBUG")
        set_module_level("train", "DEBUG")
        set_module_level("prediction", "DEBUG")

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    _dispatch(args)


def _dispatch(args):
    if args.command == "lag":
        _cmd_lag(args)
    elif args.command == "auto":
        _cmd_auto(args)
    elif args.command == "update":
        _cmd_update(args)
    elif args.command == "clean":
        _cmd_clean(args)
    elif args.command == "stats":
        _cmd_stats(args)
    elif args.command == "pipeline":
        from pipeline.runner import run_pipeline_cli
        run_pipeline_cli(args)
    elif args.command == "train":
        _cmd_train(args)
    elif args.command == "search":
        _cmd_search(args)
    elif args.command == "predict":
        _cmd_predict(args)
    elif args.command == "history":
        _cmd_history(args)
    elif args.command == "logs":
        _cmd_logs(args)
    elif args.command == "evaluate":
        _cmd_evaluate(args)
    elif args.command == "rolling":
        _cmd_rolling(args)
    elif args.command == "sales":
        _cmd_sales(args)
    elif args.command == "salesmodel":
        _cmd_salesmodel(args)
    elif args.command == "ev":
        _cmd_ev(args)
    elif args.command == "payout":
        _cmd_payout(args)
    elif args.command == "select":
        _cmd_select(args)
    elif args.command == "trials":
        _cmd_trials(args)
    elif args.command == "drift":
        _cmd_drift(args)
    elif args.command == "rollover":
        _cmd_rollover(args)
    elif args.command == "plan":
        _cmd_plan(args)
    elif args.command == "anomaly":
        from prediction.anomaly import run_cli
        run_cli(args.lottery, args.threshold)
    elif args.command == "credibility":
        from credibility.report import run_cli as cred_run_cli
        cred_run_cli(args.lottery, k=args.k, min_train=args.min_train,
                     army_size=args.army, fdr_alpha=args.alpha,
                     bayes_tau=args.bayes_tau, bayes_p_edge=args.bayes_p_edge,
                     no_bayes=args.no_bayes)
    elif args.command == "bayes":
        from credibility.bayes import bayes_report, print_bayes
        r = bayes_report(args.lottery, k=args.k, min_train=args.min_train,
                         prior_tau=args.bayes_tau, prior_p_edge=args.bayes_p_edge)
        print_bayes(r)
        return r
    elif args.command == "hypothesize":
        _cmd_hypothesize(args)
    elif args.command == "gate":
        _cmd_gate(args)
    elif args.command == "hypotheses":
        _cmd_hypotheses(args)
    elif args.command == "infotheory":
        _cmd_infotheory(args)
    elif args.command == "changepoint":
        _cmd_changepoint(args)
    elif args.command == "evalue":
        _cmd_evalue(args)
    elif args.command == "randomness":
        _cmd_randomness(args)
    elif args.command == "covering":
        _cmd_covering(args)
    elif args.command == "conformal":
        _cmd_conformal(args)
    elif args.command == "robustness":
        _cmd_robustness(args)

def _cmd_auto(args):
    """自动流水线：抓取 → 评估 → 异常检测 → 预测 → 期望 →（可选）重训"""
    from data.fetcher import update_lottery_data
    from data.feedback import evaluate_pending_predictions, load_pending, _next_issue
    from data.loader import load_lottery
    from prediction.engine import predict
    from prediction.reporter import save_prediction_record
    from prediction.anomaly import detect_anomalies
    from train.engine import train
    from data.automation_status import record_run
    from datetime import datetime
    from collections import Counter
    import time as _time

    targets = [args.lottery] if args.lottery else list(LOTTERY_CONFIG.keys())
    log_lines = [f"=== auto 流水线 {datetime.now().isoformat()} ==="]
    start_all = _time.time()

    any_failed = False
    last_message = ""

    for name in targets:
        print(f"\n{'='*50}\n>>> {name} 自动流水线\n{'='*50}")
        steps = []
        draw_numbers = {}
        predictions = []
        hit_summary = ""
        anomaly_flags = []
        retrain = None
        t0 = _time.time()

        # ① 抓取最新开奖
        print("[1/6] 抓取最新开奖数据...")
        try:
            update = update_lottery_data(name)
        except Exception as e:
            err = f"抓取异常: {e}"
            print(f"  ❌ {err}")
            log_lines.append(f"[{name}] {err}")
            record_run(name, False, err, steps=[err], duration_sec=_time.time() - t0)
            any_failed = True
            continue
        if "error" in update:
            err = f"抓取失败: {update['error']}"
            print(f"  ❌ {err}")
            log_lines.append(f"[{name}] {err}")
            record_run(name, False, err, steps=[err], duration_sec=_time.time() - t0)
            any_failed = True
            continue
        steps.append(f"抓取: {update['latest_before']}→{update['latest_after']} (+{update['fetched']})")
        print(f"  {steps[-1]}")
        log_lines.append(f"[{name}] {steps[-1]}")

        # 抓取到的开奖号码（供看板展示「到底抓到了什么」）
        try:
            _rec = load_lottery(name).records[0]
            from data.schema import is_redblue
            if is_redblue(name):
                draw_numbers = {
                    "期号": _rec.期号,
                    "红球": _rec.红球,
                    "蓝球": _rec.蓝球,
                    "日期": str(_rec.开奖日期) if _rec.开奖日期 else "",
                }
            else:
                draw_numbers = {
                    "期号": _rec.期号,
                    "号码": getattr(_rec, "zone_numbers", {}) or {},
                    "日期": str(_rec.开奖日期) if _rec.开奖日期 else "",
                }
        except Exception:
            draw_numbers = {}

        # ①b 销售额 + 各奖级中奖注数（数字型彩种；失败降级不阻塞主流水线）
        # ⚠️ 与 web/utils.run_auto_pipeline_core 对齐：早期 cli auto 缺这一步，
        # 导致「只跑 CLI 的机器」上数字型销售数据长期停在旧期（2026-09-11 实查停 9/8）。
        if name in ("排列5", "福彩3D", "排列3", "七星彩"):
            try:
                from data.fetch_sales import update_sales_data
                _u = update_sales_data(name)
                steps.append(f"销售: {_u.get('message', '')} 最新{_u.get('latest_after', '-')}")
                print(f"  {steps[-1]}")
                log_lines.append(f"[{name}] {steps[-1]}")
            except Exception as e:
                steps.append(f"销售更新失败(降级): {e}")
                log_lines.append(f"[{name}] 销售更新失败(降级): {e}")

        # ② 评估 pending 预测（开奖后对比命中，更新策略权重）
        print("[2/6] 评估 pending 预测...")
        eval_result = evaluate_pending_predictions(name)
        steps.append(f"评估: {eval_result['evaluated_count']}条, 新反馈{eval_result['new_feedback_count']}")
        print(f"  已评估 {eval_result['evaluated_count']} 条，新增反馈 {eval_result['new_feedback_count']} 条，仍 pending {eval_result['still_pending']} 条")
        for strat, w in eval_result['updated_weights'].items():
            print(f"    权重 {strat}: {w:.3f}")
        log_lines.append(
            f"[{name}] 评估: {eval_result['evaluated_count']}条/新反馈{eval_result['new_feedback_count']}/"
            f"权重={ {k: round(v,3) for k,v in eval_result['updated_weights'].items()} }"
        )

        # 命中摘要（本期新增「真预测」反馈的等级分布，马后炮训练样本不算预测命中）
        new_recs_all = eval_result.get('new_feedback_records') or []
        new_recs = [r for r in new_recs_all if r.get('valid_prediction', True)]
        if new_recs:
            grades = Counter(r.get('中奖等级', '未中') for r in new_recs)
            non_zero = {k: v for k, v in grades.items() if k != '未中'}
            if non_zero:
                hit_summary = "🎉 命中 " + "，".join(f"{k}×{v}" for k, v in non_zero.items())
            else:
                hit_summary = f"本期新增{len(new_recs)}条预测反馈，均未中奖"
        else:
            hit_summary = "本期无新增预测命中反馈"

        # 结构化命中记录（含真预测/训练回测标识，供前端区分展示）
        hit_records = []
        for r in new_recs_all:
            prize = r.get('中奖等级', '未中')
            if prize == '未中':
                continue
            hit_records.append({
                "prize": prize,
                "count": 1,
                "valid": bool(r.get('valid_prediction', True)),
                "type": r.get('记录类型', '训练' if not r.get('valid_prediction', True) else '预测'),
                "issue": r.get('目标期号'),
                "predict_date": r.get('预测日期'),
                "draw_date": r.get('开奖日期'),
            })

        # ②.5 自动异常检测（4 法）
        if not args.no_anomaly:
            print("[3/6] 自动异常检测...")
            try:
                anom = detect_anomalies(name, threshold="normal", chart=False)
                cnt = anom.get('异常总条数', 0)
                if cnt > 0:
                    for a in anom.get('异常明细', [])[:3]:
                        anomaly_flags.append(
                            f"{a.get('类型', '?')}: {a.get('号码类型', '')}{a.get('号码', '')} 偏离(z={a.get('z_score', 0):.1f})"
                        )
                    steps.append(f"异常检测: 发现{cnt}处异常")
                    log_lines.append(f"[{name}] 异常检测: {cnt}处 -> " + "; ".join(anomaly_flags))
                else:
                    steps.append("异常检测: 无异常")
                    log_lines.append(f"[{name}] 异常检测: 无异常")
            except Exception as e:
                logger.warning(f"异常检测失败: {e}")
                steps.append(f"异常检测失败: {e}")
        else:
            print("[3/6] 跳过异常检测（--no-anomaly）")
            steps.append("跳过异常检测")

        # ④ 生成下一期预测（除非 --skip-predict）
        if args.skip_predict:
            print("[4/6] 跳过预测（--skip-predict）")
            steps.append("跳过预测")
            log_lines.append(f"[{name}] 跳过预测")
        else:
            # 每期出号按彩种分流（数字型 5 / 乐透型 100）：--groups 显式给出时优先
            groups_eff = resolve_groups(name, args.groups)
            # 智能跳过：下一期若已有 pending 预测则不重复生成
            target_issue = _next_issue(name)
            existing = [p for p in load_pending(name) if p.get('目标期号') == target_issue]
            if existing:
                print(f"[4/6] 下一期 {target_issue} 已有预测，跳过（避免重复 pending）")
                steps.append(f"跳过预测(下期{target_issue}已预测)")
                log_lines.append(f"[{name}] 跳过预测(下期{target_issue}已预测)")
                predictions = (existing[0].get('预测号码') or [])[:groups_eff]
            else:
                print(f"[4/6] 生成下一期预测 (mode={args.mode}, groups={groups_eff})...")
                try:
                    pred = predict(
                        lottery_name=name,
                        groups=groups_eff,
                        mode=args.mode,
                    )
                    path = save_prediction_record(pred)
                    predictions = (pred.get('预测号码') or [])[:groups_eff]
                    print(f"  预测日期 {pred['预测日期']}，{pred['号码组数']} 组，已保存 {path}")
                    steps.append(f"预测: {pred['预测日期']} {pred['号码组数']}组")
                    log_lines.append(f"[{name}] 预测: {pred['预测日期']} {pred['号码组数']}组 ({args.mode})")
                except Exception as e:
                    err = f"预测失败: {e}"
                    print(f"  ⚠️ {err}")
                    steps.append(err)
                    log_lines.append(f"[{name}] {err}")
                    any_failed = True

        # ④b 前瞻 A/B 对照实验（NetPayout WP3；与 web/utils 流水线同一实现）
        #    只积累证据、不参与出号 → 失败一律降级，不阻塞主流水线。
        if TRIALS_ENABLED:
            try:
                from ev import trials as _T
                line = _T.pipeline_step(name, n=TRIAL_ARMS)
                if line:
                    steps.append(line)
                    log_lines.append(f"[{name}] {line}")
            except Exception as e:
                steps.append(f"A/B对照失败(降级): {e}")
                log_lines.append(f"[{name}] A/B对照失败(降级): {e}")

        # ⑤ 当前 pending 状态
        pending_count = len(load_pending(name))
        print(f"[5/6] 当前 pending 预测: {pending_count} 条")
        log_lines.append(f"[{name}] pending={pending_count}")

        # ⑥ 计算当期期望值（期望时机引擎）
        try:
            from pipeline.step6_ev import current_period_advice
            ev_advice = current_period_advice(name, load_lottery(name))
            ev_line = f"期望 {ev_advice.get('单注期望', 0):.3f}"
            if ev_advice.get("是否值得买"):
                ev_line += " 🔥 正期望值得买"
            else:
                ev_line += "（常规期）"
            print(f"[6/6] 当期期望: {ev_line}")
            steps.append(ev_line)
            log_lines.append(f"[{name}] {ev_line}")
        except Exception as e:
            logger.warning(f"期望计算失败: {e}")
            steps.append(f"期望计算失败: {e}")

        # ⑦ 可选模型重训（--retrain 裸重训；--gated-retrain 重训+闸门）
        if args.retrain or args.gated_retrain:
            print("[+] 模型重训...")
            try:
                tr = train(name, model_type="statistical", window=50)
                if tr.get('error'):
                    steps.append(f"重训失败: {tr['error']}")
                    log_lines.append(f"[{name}] 重训失败: {tr['error']}")
                else:
                    retrain = {
                        "model": tr.get('model_type', 'statistical'),
                        "window": tr.get('best_window'),
                        "hit_rate": round(tr.get('best_hit_rate', 0) or 0, 4),
                    }
                    steps.append(f"重训: 窗口{retrain['window']} 命中率{retrain['hit_rate']:.2%}")
                    log_lines.append(f"[{name}] 重训: {retrain}")
                    if args.gated_retrain:
                        gate_line = _gate_retrain(name, retrain)
                        steps.append(gate_line)
                        log_lines.append(f"[{name}] {gate_line}")
                        if "丢弃" in gate_line:
                            retrain = None  # 未过闸，不进入运行记录/看板
            except Exception as e:
                logger.warning(f"重训异常: {e}")
                steps.append(f"重训异常: {e}")

        # 记录本次运行状态（含真实产出，供看板展示）
        duration = _time.time() - t0
        success = not _last_step_failed(steps)
        message = f"最新 {update['latest_after']}，{len(steps)} 步完成" if success else "流水线有失败步骤"
        record_run(
            name, success, message, steps=steps, duration_sec=duration,
            predictions=predictions, draw_numbers=draw_numbers,
            hit_summary=hit_summary, hit_records=hit_records,
            anomaly_flags=anomaly_flags, retrain=retrain,
        )
        # 回写内置调度器状态文件：cli.py auto 直接跑时不会经过 scheduler._launch，
        # 不补这一步，看板/内置调度器会误以为该彩种还停留在旧日期（如 8-17）。
        _update_scheduler_state(name, success, message)
        last_message = message

    # 写流水线日志
    from config import BASE_DIR
    log_dir = BASE_DIR / "logs" / "automation"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / f"auto_{datetime.now().strftime('%Y%m%d')}.log"
    with open(log_file, "a", encoding="utf-8") as f:
        f.write("\n".join(log_lines) + "\n")
    print(f"\n流水线日志: {log_file}")
    if any_failed:
        print(f"\n⚠️ 流水线有失败项，详情见看板「自动化状态」")


def _last_step_failed(steps: list) -> bool:
    """判断流水线是否包含真正的执行失败步骤。

    注意区分：
    - 正常功能结果："异常检测: 发现 N 处异常" 是检测器正常工作，不算失败。
    - 真正失败：抓取/预测/重训/期望计算等环节抛异常或返回错误。
    """
    if not steps:
        return False
    fail_prefixes = (
        "抓取异常:", "抓取失败:",
        "预测失败:", "预测异常:",
        "重训失败:", "重训异常:",
        "期望计算失败:", "期望计算异常:",
        "评估失败:", "评估异常:",
    )
    return any(s.startswith(p) for s in steps for p in fail_prefixes)


def _update_scheduler_state(name: str, success: bool, message: str):
    """cli.py auto 直接跑时，回写 auto_scheduler_state.json。

    让看板与内置调度器看到真实的「今日已跑」，避免状态文件停留在旧日期误导用户；
    同时内置调度器据此判定「今日已跑」不再补跑，避免双跑。
    """
    try:
        from config import BASE_DIR
        import json
        sf = BASE_DIR / "config" / "auto_scheduler_state.json"
        d = {"enabled": True, "state": {}}
        if sf.exists():
            try:
                d = json.loads(sf.read_text(encoding="utf-8"))
            except Exception:
                pass
        st = d.setdefault("state", {}).setdefault(name, {})
        st["last_run"] = datetime.now().isoformat(timespec="seconds")
        st["last_status"] = "done" if success else "failed"
        st["running"] = False
        if message:
            st["last_message"] = message
        d["enabled"] = True
        sf.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _gate_retrain(name: str, retrain: dict) -> str:
    """重训闸门：新模型命中率 vs 裁判层解析随机基准 μ，相对提升≥2% 才保留。

    裁判层已证彩票零期望，此闸门预期几乎总是驳回——这正是诚实设计：
    "训练出新模型"只有在真超随机时才被允许进入运行记录。
    """
    try:
        from credibility.report import run_credibility
        r = run_credibility(name, k=8, min_train=100, army_size=2000,
                            fdr_alpha=0.05, no_bayes=True)
        mu = r.get("random_mu_analytic", 0) or 0
        hr = retrain.get("hit_rate") or 0
        rel = (hr - mu) / mu if mu else 0.0
        passed = rel >= 0.02
        verdict = "✅保留" if passed else "❌丢弃(未显著超随机)"
        tail = "" if passed else "（裁判层结论：彩票零期望，符合预期）"
        return (f"重训闸门: 命中率{hr:.4f} vs 随机μ={mu:.4f} 相对提升{rel:+.1%} → {verdict}{tail}")
    except Exception as e:
        return f"重训闸门异常: {e}"


def _cmd_lag(args):
    """输出各彩种数据滞后天数（白天兜底用）。
    滞后 = 最近一个应当已开奖的日期 − 数据最新开奖日期（>0 即漏跑/数据源延迟）。
    判定与 web/utils.py::_expected_latest_date 保持一致：
      - 每日开奖彩种：当天 21:00 前不算已开奖（否则白天会被误判滞后 1 天）；
      - 非每日：当天 21:30 前今天不算已开奖。
    """
    import json as _json
    from datetime import datetime as _dt, timedelta
    from data.loader import load_lottery

    def _expected(draw_days, now):
        unique = set(int(d) for d in draw_days)
        is_daily = len(unique) == 7
        today = now.date()
        if is_daily:
            cutoff = now.replace(hour=21, minute=0, second=0, microsecond=0)
            return today if now >= cutoff else today - timedelta(days=1)
        for i in range(14):
            cand = today - timedelta(days=i)
            if cand.weekday() in unique:
                if i == 0:
                    cutoff = now.replace(hour=21, minute=30, second=0, microsecond=0)
                    if now >= cutoff:
                        return cand
                    continue
                return cand
        return today

    targets = [args.lottery] if args.lottery else list(LOTTERY_CONFIG.keys())
    out = {}
    now = _dt.now()
    for name in targets:
        lag = -1
        try:
            data = load_lottery(name)
            ld = str(data.records[0].开奖日期).split()[0] if data.records else None
            if ld:
                ld_d = _dt.strptime(ld, "%Y-%m-%d").date()
                exp = _expected(LOTTERY_CONFIG.get(name, {}).get("draw_days", list(range(7))), now)
                lag = max(0, (exp - ld_d).days) if ld_d < exp else 0
        except Exception:
            lag = -1
        out[name] = lag
    if args.json:
        print(_json.dumps(out, ensure_ascii=False))
    else:
        for k, v in out.items():
            tag = "滞后" if v and v > 0 else ("正常" if v == 0 else "未知")
            print(f"{k}: {tag}（{v} 天）")


def _cmd_update(args):
    """从 500.com 抓取最新开奖数据"""
    from data.fetcher import update_lottery_data

    targets = [args.lottery] if args.lottery else list(LOTTERY_CONFIG.keys())
    for name in targets:
        print(f"\n===== 更新 {name} =====")
        result = update_lottery_data(name)
        if "error" in result:
            print(f"❌ {result['error']}")
            continue
        print(f"抓取前最新: {result['latest_before']} → 抓取后: {result['latest_after']} | 新增 {result['fetched']} 期")
        cfg = LOTTERY_CONFIG[name]
        from data.schema import is_redblue
        if is_redblue(name):
            rc, bc = cfg["red_count"], cfg["blue_count"]
            for rec in result.get("new_records", []):
                reds = " ".join(rec[1:1 + rc])
                blues = " ".join(rec[1 + rc:1 + rc + bc])
                print(f"  + 期号 {rec[0]}: 红球 {reds} 蓝球 {blues} ({rec[-1]})")
        else:
            for rec in result.get("new_records", []):
                nums = " ".join(str(x) for x in rec[1:-1]) if len(rec) > 2 else rec[1]
                print(f"  + 期号 {rec[0]}: 号码 {nums} ({rec[-1]})")
        if result.get("message"):
            print(f"ℹ️ {result['message']}")
        if result.get("cleaned"):
            print("✅ 数据已重新清洗")


def _cmd_clean(args):
    """执行数据清洗"""
    from data.cleaner import run_clean

    if args.lottery:
        run_clean(args.lottery)
    else:
        for name in LOTTERY_CONFIG:
            run_clean(name)


def _cmd_stats(args):
    """统计概览"""
    from data.loader import load_lottery
    from pipeline.statistics import summary_stats

    data = load_lottery(args.lottery)
    s = summary_stats(data)
    print(f"===== {args.lottery} 统计概览 =====")
    print(f"总期数: {s['total_records']}")
    for zname, zs in s["zones"].items():
        print(f"\n{zname}:")
        print(f"  理论频率: {zs['理论频率']:.3f}")
        print(f"  实际均值: {zs['实际频率均值']:.1f} (标准差 {zs['频率标准差']:.2f})")
        print(f"  热号Top5: {zs['hot_5']}")
        print(f"  冷号Bottom5: {zs['cold_5']}")
        print(f"  最大遗漏: {zs['最大遗漏']}")


def _cmd_train(args):
    """训练模式"""
    from train.engine import train

    # 若开启 --search，先跑自动搜索挑最优配置，再以最优参数训练
    if args.search:
        from train.search import search_best_config
        print("=== 参数自动搜索（统一评分尺）===")
        search_out = search_best_config(
            args.lottery,
            windows=args.window_list if args.window_list else None,
            model_types=None,
            feature_version=1 if args.features == "standard" else 2,
        )
        best = search_out.get("best")
        if best:
            print(f"最优配置：窗口={best['window']} 模型={best['model_type']} "
                  f"特征版本={best['feature_version']} 综合分={best['combined_score']:.4f}")
            args.window = best["window"]
            args.model = best["model_type"]
            # 特征版本跟随搜索结果
            args.features = "rich" if best["feature_version"] == 2 else "standard"
        else:
            print("⚠️ 搜索未得到有效结果，退回默认参数训练")

    result = train(
        lottery_name=args.lottery,
        iterations=args.iterations,
        window=args.window,
        window_list=args.window_list,
        model_type=args.model,
        features=args.features,
    )

    print(f"\n训练完成！结果已保存: {result.get('record_dir', 'N/A')}")


def _cmd_search(args):
    """参数自动搜索（网格 或 --optuna 贝叶斯搜索）"""
    feature_version = 1 if args.features == "standard" else 2

    # ----- Optuna 分支（P0-2：贝叶斯搜索 + BH-FDR q 值 + verdict）-----
    if getattr(args, "optuna", False):
        from train.search import search_best_config_optuna

        print(f"\n===== {args.lottery} Optuna 搜索 "
              f"({args.trials} 试次, seed={args.seed}) =====")
        print("评分口径：engine.walk_forward_folds 无泄漏折 + 统一综合分；"
              "对全部试次 selection_p 做 BH-FDR 校正\n")
        out = search_best_config_optuna(
            args.lottery,
            n_trials=args.trials,
            seed=args.seed,
        )
        if out.get("error"):
            print(f"❌ {out['error']}")
            return
        best = out.get("best")
        if best is None:
            print(f"⚠️ {out.get('verdict')}")
            return
        p_raw = best.get("selection_p_raw")
        q = best.get("selection_q")
        print(f"完成试次: {out['n_trials_completed']}/{out['n_trials_requested']} "
              f"(失败 {out['n_trials_failed']}), 参与 FDR 校正 p 值数: {out['n_p_values_corrected']}")
        bp = best["params"]
        print(f"\n🏆 最优(综合分最高): 试次#{best['number']} "
              f"窗口={bp['window']} 特征={bp['feature_version']} "
              f"模型={bp['model_type']}" + (f" 超参={bp['hp']}" if bp.get("hp") else ""))
        print(f"   综合分={best['combined_score']:.4f} "
              f"命中率={best['hit_rate']:.4f} 校准误差={best['calibration_error']:.4f}")
        print(f"   选号vs随机: advantage={best.get('selection_advantage')} "
              f"p={p_raw if p_raw is None else f'{p_raw:.4f}'} "
              f"q(BH-FDR)={q if q is None else f'{q:.4f}'}")
        print(f"📌 verdict: {out.get('verdict')}")
        print("\n各试次排名（前 10）：")
        for t in out.get("trials", [])[:10]:
            tp = t["params"]
            qs = t.get("selection_q")
            print(f"  #{t['number']:>2} 窗口={tp['window']:>3} 特征={tp['feature_version']:<8} "
                  f"模型={tp['model_type']:<13} 综合分={t['combined_score']:.4f} "
                  f"p={t.get('selection_p_raw')} q={qs if qs is None else round(qs, 4)}")
        print(f"\n   已写出 training/search_best_config_{args.lottery}_optuna.json")
        return

    # ----- 网格搜索分支（原逻辑）-----
    from train.search import search_best_config

    windows = args.windows if isinstance(args.windows, list) else None
    # 归一化模型列表（默认是逗号分隔字符串）
    models = args.models
    if isinstance(models, str):
        models = [m.strip() for m in models.split(",") if m.strip()]
    out = search_best_config(
        args.lottery,
        windows=windows,
        model_types=models if models else None,
        feature_version=feature_version,
    )
    best = out.get("best")
    print(f"\n===== {args.lottery} 参数搜索结果 =====")
    for r in out.get("results", []):
        if "combined_score" in r:
            print(f"窗口={r['window']:>3} 模型={r['model_type']:<13} "
                  f"命中率={r['hit_rate']:.4f} 校准误差={r['calibration_error']:.4f} "
                  f"综合分={r['combined_score']:.4f}")
        else:
            print(f"窗口={r.get('window')} 模型={r.get('model_type')} -> {r.get('error')}")
    if best:
        print(f"\n✅ 最优：窗口={best['window']} 模型={best['model_type']} "
              f"特征版本={best['feature_version']} 综合分={best['combined_score']:.4f}")
        print(f"   已写出 training/search_best_config_<彩种>_<standard|rich>.json，可据此 retrain 或作参考")


def _cmd_predict(args):
    """预测模式"""
    from prediction.reporter import save_prediction_record

    if args.mode == "robust":
        _cmd_predict_robust(args)
        return

    from prediction.engine import predict
    from config import resolve_groups
    result = predict(
        lottery_name=args.lottery,
        groups=resolve_groups(args.lottery, args.groups),
        mode=args.mode,
    )

    path = save_prediction_record(result)

    print(f"\n===== {args.lottery} 预测结果 =====")
    print(f"预测日期: {result['预测日期']}")
    print(f"模式: {result['预测模式']}")
    print(f"号码组数: {result['号码组数']}")
    for i, ps in enumerate(result['预测号码'], 1):
        if "红球" in ps:
            red = " ".join(f"{n:02d}" for n in ps['红球'])
            blue = " ".join(f"{n:02d}" for n in ps['蓝球'])
            line = f"  第{i}组: 红球[{red}] 蓝球[{blue}] 置信度{ps['置信度']:.4f}"
        else:
            zone_str = "  ".join(
                f"{z}[{' '.join(f'{n:02d}' for n in nums)}]"
                for z, nums in ps.get('号码', {}).items()
            )
            line = f"  第{i}组: {zone_str} 置信度{ps['置信度']:.4f}"
        if ps.get("备注"):
            line += f"\n        备注: {ps['备注']}"
        print(line)
    if result.get("规则分档"):
        z = result["规则分档"]
        print(f"\n  冷热分档参考:")
        print(f"    红球热: {' '.join(f'{n:02d}' for n in z.get('红球热', []))}")
        print(f"    红球冷: {' '.join(f'{n:02d}' for n in z.get('红球冷', []))}")
        print(f"    蓝球热: {' '.join(f'{n:02d}' for n in z.get('蓝球热', []))}")
        print(f"    蓝球冷: {' '.join(f'{n:02d}' for n in z.get('蓝球冷', []))}")
    print(f"\n已保存: {path}")


def _print_tier_result(label: str, result: dict):
    """打印单档结果（含鲁棒性明细与诚实口径）。"""
    print(f"\n----- {label}档 · {result.get('预测模式')} -----")
    if result.get("提示"):
        print(f"  ⚠️ {result['提示']}")
    print(f"  号码组数: {result['号码组数']}"
          + (f"（目标期号 {result.get('目标期号')}）" if result.get("目标期号") else ""))
    if result.get("共识参数"):
        cp = result["共识参数"]
        print(f"  共识参数: 窗口{cp['windows']} × 种子{cp['seeds']} = {cp['组合数']}组合，池 {cp['候选池大小']} 票面")
    if result.get("高鲁棒门槛"):
        g = result["高鲁棒门槛"]
        extra = "（已放宽）" if g.get("coverage_relaxed") else ""
        print(f"  高鲁棒门槛: 窗口一致性 ≥{g['窗口一致性阈值']:.0%}{extra}")
    for i, ps in enumerate(result["预测号码"][:5], 1):
        zone_str = "  ".join(
            f"{z}[{' '.join(f'{n:02d}' for n in nums)}]"
            for z, nums in ps.get("号码", {}).items()
        ) or f"红[{ps.get('红球')}] 蓝[{ps.get('蓝球')}]"
        rob = ps.get("鲁棒性") or {}
        rob_str = ""
        if rob:
            rob_str = (f" · 前列命中率{rob.get('前列命中率', 0):.0%}"
                       f" 半区命中率{rob.get('半区命中率', 0):.0%}"
                       f" 平均置信度{rob.get('平均置信度', 0):.3f}")
        print(f"  第{i}组: {zone_str} 共识分{ps['置信度']:.4f}{rob_str}")
    if result["号码组数"] > 5:
        print(f"  ...（其余 {result['号码组数'] - 5} 组略）")
    print(f"  诚实口径: {result.get('鲁棒性说明', '')}")


def _cmd_predict_robust(args):
    """三档鲁棒出号（--mode robust）：三种独立生成流程，全部写 pending 参与反馈对比。"""
    from prediction.robust_tiers import predict_tiered
    from prediction.reporter import save_prediction_record

    windows = tuple(int(x) for x in str(args.windows).split(",") if x.strip())
    results = predict_tiered(
        lottery_name=args.lottery,
        groups=args.groups or 50,
        tier=args.tier,
        windows=windows,
        seeds=args.seeds,
    )

    print(f"\n===== {args.lottery} 三档鲁棒出号（tier={args.tier}） =====")
    if args.tier == "all":
        for label, result in results.items():
            _print_tier_result(label, result)
            save_prediction_record(result)
    else:
        result = results
        _print_tier_result(result.get("档位", ""), result)
        save_prediction_record(result)
    print("\n三档均已写入 pending（开奖后各档独立结算；策略权重学习仅由一般档驱动）")


def _cmd_robustness(args):
    """鲁棒性/过拟合专用监控报告。"""
    from credibility.robustness import compute_robustness_report

    report = compute_robustness_report(args.lottery, mode=args.mode, write=args.write)
    m = report.get("metrics") or {}

    print(f"\n===== {args.lottery} 鲁棒性/过拟合监控（mode={report['mode']}） =====")
    print(f"数据截止期号: {report.get('data_upto_issue')} | verdict: {report['verdict']}")

    ws = m.get("window_stability")
    if ws:
        print(f"\n[窗口稳定性] stability={ws['stability']:.3f} "
              f"(跨窗口 Jaccard={ws['cross_jaccard']:.3f} / 噪声地板={ws['noise_floor']:.3f})")
        print("  口径: " + ws.get("口径", ""))
    ps = m.get("param_sensitivity")
    if ps:
        print(f"[参数敏感性] sensitivity={ps['sensitivity']:.3f} "
              f"(权重扰动 Kendall τ={ps['mean_kendall_tau']:.3f})")
    drift = m.get("pool_drift")
    if drift:
        print(f"[分布漂移] max JS={drift['max_js']:.4f} "
              f"现代段期号>{drift['modern_cutoff_issue']} 告警分区: {drift['alarm_zones'] or '无'}")
    g = m.get("train_test_gap")
    if g:
        print(f"[train-test gap] gap={g['gap']:+.4f} (in={g['in_mean']:.4f} / out={g['out_mean']:.4f}) "
              f"零分布 P95={g['null_p95']:+.4f}（{g['n_folds']}折 × {g['n_perm']}次置换校准）")
    if report.get("errors"):
        print(f"[跳过的指标] {report['errors']}")

    if report.get("alarms"):
        print("\n⚠️ 告警:")
        for a in report["alarms"]:
            print(f"  - {a}")
    else:
        print("\n✅ 无告警（各项指标均在随机基线校准阈值内）")

    print(f"\n诚实口径: {report.get('说明')}")
    if args.write:
        from credibility.robustness import ROBUSTNESS_DIR
        print(f"已落盘: {ROBUSTNESS_DIR}\\{args.lottery}.json + trend_{args.lottery}.json")


def _cmd_history(args):
    """查看历史记录（支持 --days 过滤）"""
    from config import TRAINING_DIR
    from datetime import timedelta

    if not TRAINING_DIR.exists():
        print("暂无历史记录")
        return

    all_records = sorted(TRAINING_DIR.iterdir(), reverse=True)
    if not all_records:
        print("暂无历史记录")
        return

    # 按天数过滤
    cutoff = datetime.now() - timedelta(days=args.days)
    filtered = []
    for r in all_records:
        # 从目录名解析日期：YYYYMMDD_HHMMSS_xxx
        try:
            parts = r.name.split("_")
            if len(parts) >= 2:
                record_date = datetime.strptime(parts[0] + parts[1], "%Y%m%d%H%M%S")
                if record_date >= cutoff:
                    filtered.append(r)
        except (ValueError, IndexError):
            filtered.append(r)

    if not filtered:
        print(f"最近 {args.days} 天内无记录")
        return

    print(f"===== 训练/预测历史记录 （最近 {args.days} 天，共 {len(filtered)} 条） =====")
    for r in filtered[:30]:
        name = r.name
        if r.is_dir():
            # 计算目录大小
            try:
                total = sum(f.stat().st_size for f in r.glob("**/*") if f.is_file())
                size_str = f"{total / 1024:.1f} KB"
            except Exception:
                size_str = "?"
        else:
            size_str = f"{r.stat().st_size / 1024:.1f} KB"

        # 识别类型
        type_tag = "TRAIN" if "_predict" not in name else "PREDICT"
        if "anomaly" in name:
            type_tag = "ANOMALY"
        print(f"  [{type_tag:7s}] {name}  ({size_str})")


def _cmd_logs(args):
    """查看日志"""
    from logs.logger import show_recent_logs
    show_recent_logs(lines=args.lines, level=args.level)


def _cmd_evaluate(args):
    """评估 pending 预测（开奖后对比命中）"""
    from data.feedback import (
        evaluate_pending_predictions,
        get_feedback_summary,
        load_pending,
    )

    lottery = args.lottery

    if args.summary:
        # 只查看摘要
        summary = get_feedback_summary(lottery, lookback=args.lookback)
        print(f"===== {lottery} 反馈摘要 =====")
        print(f"历史反馈记录数: {summary['total_records']}")
        print(f"待开奖预测数: {summary['pending_count']}")
        print(f"当前策略权重:")
        for name, w in summary['weights'].items():
            print(f"  {name}: {w:.4f}")
        print()
        print(f"各策略表现（近 {args.lookback} 期）:")
        for name, stats in summary['strategies'].items():
            if stats.get("样本数", 0) == 0:
                print(f"  {name}: 无数据")
                continue
            print(f"  {name}:")
            print(f"    样本数: {stats['样本数']}")
            if "平均红球命中" in stats:
                print(f"    平均红球命中: {stats['平均红球命中']}")
                print(f"    平均蓝球命中: {stats['平均蓝球命中']}")
            elif "平均命中率" in stats:
                print(f"    平均命中率: {stats['平均命中率']*100:.2f}%")
            if "平均模拟盈亏" in stats:
                print(f"    平均模拟盈亏: {stats['平均模拟盈亏']} 元  累计: {stats['累计模拟盈亏']} 元")
            print(f"    中奖率: {stats['中奖率']*100:.2f}%")
            print(f"    中奖等级分布: {stats['中奖等级分布']}")
        return

    # 查看当前 pending 数量
    pending = load_pending(lottery)
    print(f"当前 {lottery} 有 {len(pending)} 条 pending 预测")

    # 执行评估
    result = evaluate_pending_predictions(lottery)

    print()
    print(f"===== 评估结果 =====")
    print(f"已评估: {result['evaluated_count']} 条预测")
    print(f"新增反馈: {result['new_feedback_count']} 条")
    print(f"仍 pending: {result['still_pending']} 条")
    print()
    print(f"策略权重已更新:")
    for name, w in result['updated_weights'].items():
        print(f"  {name}: {w:.4f}")


def _cmd_infotheory(args):
    """P2-4：历史开奖 → 下一期的信息传递量化（MI/CMI，置换检验零分布）"""
    import json as _json
    from discovery.info_theory import lottery_report, run_all

    if args.lottery:
        print(_json.dumps(lottery_report(args.lottery, kmax=args.lags, B=args.perm),
                          ensure_ascii=False, indent=1))
        return
    for rep in run_all(kmax=args.lags, B=args.perm):
        print(f"[{rep['彩种']}] {rep['结论']}")
        for z in rep["分区"]:
            sigs = [(t["量"], t["q(BH)"]) for t in z["检验"] if t["显著"]]
            worst = min((t["q(BH)"] for t in z["检验"]), default=1.0)
            line = f"  {'★' if sigs else ' '} {z['分区']}: 最小q={worst}"
            if sigs:
                line += f"  显著: {sigs}"
            print(line)


def _cmd_changepoint(args):
    """P2-2：单变点扫描（自动找号码/数据口径突变点，供 rolling 切窗参考）"""
    from credibility.changepoint import OUT_JSON, change_point_scan

    names = [args.lottery] if args.lottery \
        else ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]
    for nm in names:
        rep = change_point_scan(nm, B=args.perm, min_seg=args.min_seg,
                                v_gate=args.v_gate, write_json=not args.no_write)
        print(f"[{nm}] {rep['结论']}")
        for r in rep["全部扫描"]:
            if "注" in r:
                print(f"  {r['分区']}: {r['注']}")
                continue
            mark = "★" if r.get("实质变点") else " "
            print(f"  {mark} {r['分区']}: 变点@位置{r['变点位置']}({r['变点日期']},期{r['变点期号']})"
                  f" V={r['CramérV']} p={r['p']} q={r.get('q(BH)')}")
    if not args.no_write:
        print(f"\n各彩种结果已写入 {OUT_JSON}")


def _cmd_evalue(args):
    """P2-3：e-value 序贯监控（分区边际公平性，任意停时有效）"""
    import json as _json
    from discovery.always_valid import (
        OUT_JSON, calibrate_bias, calibrate_null, monitor_lottery, run_all,
    )

    if args.calibrate:
        print(_json.dumps(calibrate_null(), ensure_ascii=False, indent=1))
        for d in (0.03, 0.05, 0.06):
            print(_json.dumps(calibrate_bias(delta=d), ensure_ascii=False, indent=1))
        return

    if args.lottery:
        rep = monitor_lottery(args.lottery, alpha=args.alpha, warm=args.warm)
        print(f"[{rep['彩种']}] {rep['结论']}")
        for z in rep["分区"]:
            if "注" in z:
                print(f"    {z['分区']}: {z['注']}")
                continue
            cross = z.get("首次越阈期号") or z.get("首次越阈(期索引)")
            mark = "★" if cross is not None else " "
            print(f"  {mark} {z['分区']}: e-max={z['组合e-max']}"
                  f"{f' 越阈@{cross}' if cross is not None else ''}")
        hyps = rep.get("覆盖假设", [])
        if hyps:
            print(f"  覆盖登记假设 {len(hyps)} 条（证伪对象=分区公平性，状态见上）")
        return

    rep = run_all(alpha=args.alpha, warm=args.warm, write_json=not args.no_write)
    print(rep["族结论"])
    for r in rep["彩种"]:
        print(f"[{r['彩种']}] {r['结论']}")
        for z in r["分区"]:
            if "注" in z:
                print(f"    {z['分区']}: {z['注']}")
                continue
            cross = z.get("首次越阈期号") or z.get("首次越阈(期索引)")
            mark = "★" if cross is not None else " "
            print(f"  {mark} {z['分区']}: 期数={z['期数(用)']} e-max={z['组合e-max']}"
                  f"{f' 越阈@{cross}' if cross is not None else ''}")
    if not args.no_write:
        print(f"\n族级结果已写入 {OUT_JSON}")


def _cmd_randomness(args):
    """P2-1：NIST 风格随机性审计（频率/游程/最长游程/序列/累积和/块内频率）"""
    from credibility.randomness_audit import OUT_JSON, audit_lottery, run_all

    if args.lottery:
        reps = [audit_lottery(args.lottery, B=args.B, block=args.block)]
    else:
        reps = run_all(B=args.B, block=args.block, write_json=not args.no_write)
    for rep in reps:
        print(f"[{rep['彩种']}] {rep['结论']}")
        for z in rep["分区"]:
            for t in z.get("检验", []):
                mark = "★" if t.get("实质显著") else " "
                line = (f"  {mark} {z['分区']}/{t['检验']}: p={t['p值']}"
                        f" q={t.get('q(BH)')}")
                if t.get("注"):
                    line += f" [{t['注']}]"
                print(line)
    if args.lottery and not args.no_write:
        import json as _json
        from datetime import datetime as _dt
        payload = {"模块": "P2-1 随机性审计（NIST 思路）",
                   "更新于": _dt.now().isoformat(timespec="seconds"),
                   "彩种": reps}
        OUT_JSON.write_text(
            _json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"\n结果已写入 {OUT_JSON}")
    elif not args.lottery and not args.no_write:
        print(f"\n各彩种结果已写入 {OUT_JSON}")


def _cmd_covering(args):
    """P3-1：覆盖设计出号（预算 N 注 → 保底任意开奖 ≥t 红）"""
    from combo.covering import plan

    rep = plan(args.lottery, t=args.t, budget=(args.groups or None),
               seed=args.seed, mc_draws=(0 if args.no_mc else 2000))
    if not rep.get("applicable", True):
        print(f"[{args.lottery}] {rep['reason']}")
        return
    print(f"===== {rep['彩种']} 覆盖设计（红区 t={rep['t']} 保底） =====")
    print(f"  覆盖空间 C({rep['v']},{rep['t']})={rep['覆盖空间']} 个 t-子集")
    print(f"  已覆盖 {rep['已覆盖']}（覆盖率 {rep['覆盖率']:.2%}）"
          f"{'【满覆盖 → 确定性保底】' if rep['满覆盖'] else '【部分覆盖 → 概率性】'}")
    print(f"  注数 {rep['实际注数']}（理论下界 {rep['理论下界注数']}）")
    if rep.get("随机对比"):
        rc = rep["随机对比"]
        print(f"  随机同注数覆盖率 {rc['随机同注数覆盖率']:.2%}"
              f"（覆盖增益 {rc['覆盖增益']:+.2%}）")
    if rep.get("MC验证"):
        mv = rep["MC验证"]
        print(f"  MC {mv['随机抽期']} 期：每期 ≥t 红票数 最差={mv['每期≥t红票数-最差']}"
              f" 平均={mv['每期≥t红票数-平均']} → {mv['结论']}")
    if rep.get("蓝号轮转"):
        print(f"  蓝区：{rep['蓝号轮转']}")
    print(f"  诚实说明：{rep['诚实说明']}")
    # 注单打印：红票 + 轮转蓝号（双色球 1/16；大乐透 2/12 用互质步长取对）
    n_b = {"双色球": 16, "大乐透": 12}.get(rep["彩种"], 0)
    tickets = rep["tickets"]
    print(f"\n  {len(tickets)} 注注单（前 20 注）:")
    for i, tk in enumerate(tickets[:20]):
        if rep["彩种"] == "双色球":
            bl = (i % n_b) + 1
            print(f"    第{i + 1}注: 红[{' '.join(f'{x:02d}' for x in tk)}]"
                  f" 蓝[{bl:02d}]")
        elif rep["彩种"] == "大乐透":
            b1 = (i % 12) + 1
            b2 = ((i * 7) % 12) + 1
            print(f"    第{i + 1}注: 红[{' '.join(f'{x:02d}' for x in tk)}]"
                  f" 蓝[{b1:02d} {b2:02d}]")
        else:
            print(f"    第{i + 1}注: {' '.join(f'{x:02d}' for x in tk)}")
    if len(tickets) > 20:
        print(f"    ……（共 {len(tickets)} 注）")


def _cmd_conformal(args):
    """P3-2：分箱共形——预算 K 注 → 覆盖率保证对照表（含回放不撒谎门控）"""
    import json as _json
    from combo.conformal import plan

    kgrid = ([int(x) for x in args.K.split(",") if x.strip()]
             if args.K else None)
    rep = plan(args.lottery, target=args.target, mode=args.mode,
               K_grid=kgrid, cal_frac=args.cal_frac, seed=args.seed)
    if not rep.get("applicable", True):
        print(f"[{args.lottery}] {rep['reason']}")
        return

    if rep.get("mode", "").startswith("exact"):
        print(f"===== {rep['彩种']} 精确公式表（共形无校准对象） =====")
        print(f"  目标: {rep['target']} | {rep['说明']}")
        print(f"  {'K注数':>10}  {'单期直选命中率':>16}")
        for row in rep["对照表"]:
            pr = row["直选命中率(精确)"]
            print(f"  {row['K注数']:>10,}  {pr:>15.6%}"
                  + (f"  {row['解析式']}" if row.get("解析式") and pr < 1 else ""))
        print(f"  诚实说明：{rep['诚实说明']}")
        return

    ms = rep["现代段"]
    print(f"===== {rep['彩种']} 共形保证（target={rep['target']} "
          f"mode={rep['mode']}） =====")
    print(f"  现代段 {ms['期数']} 期（{ms['首期']}~{ms['末期']}）"
          f"｜校准 {ms['校准期']} 期 + 回放尾段 {ms['回放期']} 期")
    if ms.get("检出"):
        print(f"  P2-2 变点联动：{ms['说明']}")
    print(f"  票池：{rep['票池']['生成']}，共 {rep['票池']['票数']} 票")
    print(f"\n  {'K':>5} {'校准命中率':>9} {'共形保证档G':>10} "
          f"{'CP95%下界':>9} {'回放实测':>8}  {'声称(50/80/90/95/99%)':>22}")
    for row in rep["对照表"]:
        cl = row["声称档位"]
        marks = "".join("✓" if cl[k] else "·" for k in
                        ("50%", "80%", "90%", "95%", "99%"))
        det = " [确定性满覆盖]" if row.get("满覆盖确定性") else ""
        print(f"  {row['K注数']:>5,} {row['校准命中率']:>9.2%} "
              f"{row['共形保证档']:>10.4f} {row['CP95%下界']:>9.2%} "
              f"{row['回放实测']:>8.2%}  {marks}{det}")
    mk = rep["最小达标K"]
    print(f"\n  最小达标K（共形档位≥X%）："
          + "  ".join(f"{k}→{v if v else '超票池'}" for k, v in mk.items()))
    gate = rep["不撒谎门控"]
    if isinstance(gate, dict) and gate:
        bad = [v for v in gate.values() if v.startswith("❌")]
        print(f"\n  不撒谎门控：{len(gate)} 个 ≥90% 声称档位待回放校验"
              + ("，全部 ✅ 达标" if not bad else f"，⚠️ {len(bad)} 个未达标："
                + "；".join(bad)))
    else:
        print(f"\n  不撒谎门控：{gate}")
    if rep.get("确定性说明"):
        print(f"  {rep['确定性说明']}")
    print(f"\n  诚实说明：{rep['诚实说明']}")
    if args.write:
        import os
        out = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "discovery", "conformal.json")
        with open(out, "w", encoding="utf-8") as f:
            _json.dump(rep, f, ensure_ascii=False, indent=1, default=str)
        print(f"\n  已写入 {out}")



def _cmd_hypothesize(args):
    """登记假设（发现闭环入口）"""
    from discovery.hypothesis_registry import add_hypothesis, add_auto_hypotheses

    if args.auto:
        added = add_auto_hypotheses(args.lottery)
        if not added:
            print(f"{args.lottery} 无新假设（模板已全部登记过）")
            return
        print(f"=== {args.lottery} 自动登记 {len(added)} 条候选假设 ===")
        for h in added:
            print(f"  + {h['id']} [{h['probe']['type']}] {h['name']}  (策略={h['probe']['strategy']})")
        print(f"  下一步: python cli.py gate {args.lottery} --all")
        return

    if not (args.name and args.hypothesis and args.strategy):
        print("手工提假设需提供 --name / --hypothesis / --strategy；或 --auto 从模板生成候选")
        return
    h = add_hypothesis(args.lottery, args.name, args.hypothesis, args.strategy,
                       probe_type=args.probe, target_metric=args.target_metric)
    print(f"已登记 {h['id']}: {h['name']} [{h['probe']['type']}]")
    print(f"  假设: {h['hypothesis']}")
    print(f"  策略: {h['probe']['strategy']} | 目标指标: {h['probe']['target_metric']}")
    print(f"  状态: {h['status']}（下一步: python cli.py gate {args.lottery} --id {h['id']}）")


def _cmd_gate(args):
    """闸门判卷"""
    from discovery.gate import gate_all
    from discovery.hypothesis_registry import get_hypothesis, list_hypotheses

    hyps = [get_hypothesis(args.lottery, args.id)] if args.id else list_hypotheses(args.lottery)
    hyps = [h for h in hyps if h]
    if not hyps:
        print(f"{args.lottery} 假设登记表为空，先跑: python cli.py hypothesize {args.lottery} --auto")
        return

    results = gate_all(
        args.lottery, hyps,
        k=args.k, min_train=args.min_train, army_size=args.army,
        fdr_alpha=args.fdr_alpha, bayes_tau=args.bayes_tau,
        bayes_p_edge=args.bayes_p_edge, no_bayes=args.no_bayes,
        ev_window=args.ev_window,
    )
    print(f"===== {args.lottery} 闸门判卷（{len(results)} 条）=====")
    n_pass = 0
    for r in results:
        h = r["hypothesis"]
        mark = "✅ 存活" if r["passed"] else "❌ 驳回"
        n_pass += int(r["passed"])
        print(f"\n[{h['id']}] {h['name']}  →  {mark}  (status={r['status']})")
        print(f"  探测: {h['probe'].get('type', 'credibility_gate')} | 策略: {h['probe'].get('strategy')}")
        if r["metrics"]:
            print("  指标:", "  ".join(f"{k}={v}" for k, v in r["metrics"].items()))
        if r["reasons"]:
            print("  未过闸:")
            for x in r["reasons"]:
                print(f"    · {x}")
        else:
            print("  四道闸全部通过 ✅")
        if r.get("verdict"):
            print(f"  裁判结论: {r['verdict']}")
    print(f"\n存活 {n_pass}/{len(results)}，准入清单已写 training/hypotheses/{args.lottery}_gate_manifest.json")


def _cmd_hypotheses(args):
    """列出假设登记表"""
    from discovery.hypothesis_registry import list_hypotheses

    hyps = list_hypotheses(args.lottery)
    if not hyps:
        print(f"{args.lottery} 假设登记表为空")
        return
    status_mark = {"proposed": "待测", "testing": "测试中", "survived": "✅存活",
                   "rejected": "❌驳回", "dormant": "休眠"}
    print(f"===== {args.lottery} 假设登记表（{len(hyps)} 条）=====")
    for h in hyps:
        last = h["tests"][-1] if h.get("tests") else None
        mark = status_mark.get(h["status"], h["status"])
        print(f"  [{h['id']}] {h['name']}  {mark}")
        print(f"      探测={h['probe'].get('type', 'credibility_gate')} 策略={h['probe'].get('strategy')}  "
              f"promoted={h.get('promoted', False)}")
        if last:
            print(f"      最近检验: {last['tested_at']}  {'通过' if last['passed'] else '驳回'}"
                  f"  样本={last.get('sample_n', '-')}")
            if args.detail:
                for k, v in last.get("metrics", {}).items():
                    print(f"        {k}: {v}")
                for x in last.get("reasons", []):
                    print(f"        · {x}")


def _cmd_rolling(args):
    """滚动预测训练"""
    from train.rolling_trainer import rolling_train

    result = rolling_train(
        args.lottery,
        window_size=args.window,
        eval_periods=args.eval_periods,
    )

    print(f"===== 滚动预测训练完成 =====")
    print(f"彩票: {result['lottery_name']}")
    print(f"窗口: {result['window_size']}, 评估期数: {result['eval_periods']}")
    print(f"总预测次数: {result['total_predictions']}")
    print(f"报告目录: {result['record_dir']}")
    print()
    print("各策略表现:")
    for name, stats in result['strategy_stats'].items():
        print(f"  {name}:")
        print(f"    平均红球命中: {stats['平均红球命中']}")
        print(f"    平均蓝球命中: {stats['平均蓝球命中']}")
        print(f"    中奖率: {stats['中奖率']*100:.2f}%")
        print(f"    中奖等级分布: {stats['中奖等级分布']}")


def _cmd_sales(args):
    """抓取/更新数字型彩种 销售额+各奖级中奖注数（任务1.1）"""
    from data.fetch_sales import update_sales_data, get_sales_summary

    if args.all or args.lottery is None:
        targets = ["排列5", "福彩3D", "排列3", "七星彩"]
    else:
        targets = [args.lottery]

    for name in targets:
        try:
            r = update_sales_data(name)
            print(f"[{name}] {r.get('message', '')}  最新期={r.get('latest_after', '-')}")
        except Exception as e:
            print(f"[{name}] [失败] {type(e).__name__}: {e}")
            continue
        s = get_sales_summary(name)
        if s.get("has_data"):
            print(f"    已入库 {s['records']} 期，最新 {s['latest_issue']} ({s['latest_date']})，"
                  f"奖级列: {', '.join(s['prize_columns']) if s['prize_columns'] else '无'}")


def _cmd_salesmodel(args):
    """P1-2：销量/奖池/一等奖注数预测 → 前瞻 EV 与参与信号（双/大）"""
    import json as _json
    from ev.sales_model import evaluate, forecast_next, signal_consistency

    if args.forecast:
        print(_json.dumps(forecast_next(args.lottery), ensure_ascii=False, indent=1))
        return
    if args.signal:
        print(_json.dumps(signal_consistency(args.lottery, test_frac=args.test_frac),
                          ensure_ascii=False, indent=1))
        return
    print(_json.dumps(evaluate(args.lottery, test_frac=args.test_frac),
                      ensure_ascii=False, indent=1))


def _extreme_demo_nums(model, name):
    """由 crowd 模型找最冷/最热示例号各一个（v1/v2 通用，score_vec 向量化）。
    七星彩第7位 0-14：按位采样而非十进制枚举（避免漏 10-14）。"""
    import numpy as np
    if name in ("排列3", "福彩3D"):
        arr = np.array([[i // 100 % 10, i // 10 % 10, i % 10] for i in range(1000)])
    elif name == "排列5":
        rng = np.random.default_rng(2026)
        arr = rng.integers(0, 10, size=(50000, 5))
    elif name == "七星彩":
        rng = np.random.default_rng(2026)
        arr = np.hstack([rng.integers(0, 10, size=(20000, 6)),
                         rng.integers(0, 15, size=(20000, 1))])
    else:
        return [1, 2, 3], [7, 8, 9]
    sc = model.score_vec(arr)
    return arr[int(np.argmin(sc))].tolist(), arr[int(np.argmax(sc))].tolist()


def _cmd_ev(args):
    """EV 计算：名义EV / 限号EV / 冷门TOP"""
    from ev.engine import net_ev, scan_issues

    def _show(r):
        ev = r["限号EV"]
        if isinstance(ev, dict):
            ev = ev.get("直选")
        print(f"  名义EV={r['名义EV']}  限号EV={ev}  限号暴露={r['限号暴露概率']}")
        print(f"  机制: {r['机制']}")
        if r.get("详情"):
            for k, v in r["详情"].items():
                print(f"    {k}: {v}")
        if r.get("备注"):
            print(f"  备注: {r['备注']}")

    # --cold：冷门号 TOP
    if args.cold:
        r = scan_issues(args.lottery, issue=args.issue, top_n=args.cold)
        if "error" in r:
            print(f"[失败] {r['error']}")
            return
        print(f"=== {r['彩种']} 限号EV TOP{len(r['TOP冷门(限号EV最高)'])}（期号 {r['期号'] or '最新'}）===")
        for i, it in enumerate(r["TOP冷门(限号EV最高)"], 1):
            # dict 的 in 是精确键匹配（不是子串），须按完整键名判断
            if "分薄乘数(≈1=独享)" in it:
                val, lab = it["分薄乘数(≈1=独享)"], "分薄乘数(≈1=独享)"
            else:
                val, lab = it.get("拥挤分位"), "拥挤分位"
            print(f"  {i:>2}. {it['号码']}  限号EV={it['限号EV']}  {lab}={val}")
        return

    # --sample：约束采样出号（任务2.4）
    if args.sample:
        from ev.sampler import sample_tickets
        constraints = {"max_consec": args.max_consec}
        if args.sum_range:
            a, b = args.sum_range.split("-")
            constraints["sum_range"] = (int(a), int(b))
        r = sample_tickets(args.lottery, n=args.sample, constraints=constraints)
        print(f"=== {args.lottery} 约束采样出号 {r['组数']} 组 ===")
        print(f"  约束: {constraints} | 反大众: {r['反大众']}")
        for t in r["号码组"]:
            q = f"拥挤分位={t['拥挤分位']}" if t["拥挤分位"] is not None else "无模型"
            print(f"  {t['号码']}  {q}  和值={t['和值']}  {t['奇偶']}")
        print(f"  说明: {r['说明']}")
        return

    if args.ticket:
        nums = [int(c) for c in args.ticket]
        r = net_ev(args.lottery, nums, issue=args.issue)
        print(f"=== {args.lottery} 号码 {args.ticket}（期号 {args.issue or '最新'}）===")
        _show(r)
        return

    # 无参数：示例（冷门 vs 大众 各一个）——由 crowd 模型驱动（v2 优先），
    # 不再硬编码 0000000（实测 μ=0.37 并非最冷，示例有误导）
    from ev.engine import _get_crowd
    try:
        model = _get_crowd(args.lottery)
    except Exception:
        model = None
    print(f"=== {args.lottery} EV 示例（期号 {args.issue or '最新'}）===")
    if model is not None:
        cold_nums, hot_nums = _extreme_demo_nums(model, args.lottery)
        for label, nums in (("冷门示例", cold_nums), ("热门示例", hot_nums)):
            r = net_ev(args.lottery, nums, issue=args.issue)
            print(f"  --- {label} {' '.join(map(str, nums))} ---")
            _show(r)
    else:
        r = net_ev(args.lottery, [1, 2, 3], issue=args.issue)
        _show(r)
        print(f"  备注: crowd 模型不可用，示例为占位号（用 ev {args.lottery} --cold N 看数据驱动冷门TOP）")


def _cmd_payout(args):
    """NetPayout WP0：实得奖金统一口径（各奖级概率 × 实得单注 + 净EV）"""
    from ev.payout import payout_eff

    if args.lottery in ("双色球", "大乐透"):
        if not (args.red and args.blue):
            print("乐透型请用 --red 1,2,3,4,5,6 --blue 7 指定号码")
            return
        ticket = {"红球": [int(x) for x in args.red.split(",")],
                  "蓝球": [int(x) for x in args.blue.split(",")]}
        shown = f"红球{args.red} 蓝球{args.blue}"
    else:
        if not args.ticket:
            print("数字型请用 --ticket 12345 指定号码")
            return
        ticket = args.ticket
        shown = args.ticket

    r = payout_eff(args.lottery, ticket, issue=args.issue)
    if "error" in r:
        print(f"错误: {r['error']}")
        return

    print(f"=== {r['彩种']} 期{r['期号']} 号码 {shown} ===")
    print(f"{'奖级':<6}{'概率':>14}{'1/概率':>14}{'名义单注':>14}{'实得单注':>14}  来源")
    for row in r["奖级明细"]:
        p = row["概率"]
        inv = f"1/{1/p:,.0f}" if p > 0 else "-"
        nom = "—" if row["名义单注"] is None else f"{row['名义单注']:,.0f}"
        eff = "—" if row["实得单注"] is None else f"{row['实得单注']:,.0f}"
        print(f"{row['奖级']:<6}{p:>14.4e}{inv:>14}{nom:>14}{eff:>14}  {row['来源']}")
    print(f"\n名义期望 {r['名义期望']:.4f} 元/注 ｜ 实得期望 {r['实得期望']:.4f} 元/注 ｜ "
          f"净EV {r['净EV']:.4f} 元/注")
    print(f"分薄乘数 {r['分薄']['乘数']}（{r['分薄']['来源']}）")
    for n in r["备注"]:
        print(f"  备注: {n}")


def _cmd_select(args):
    """NetPayout WP2：按实得奖金选号（候选池 → 净EV 排序 → 低重叠贪心）"""
    from ev.selection import select_tickets

    r = select_tickets(args.lottery, n=args.groups, pool_size=args.pool,
                       seed=args.seed, issue=args.issue)
    if r.get("拒绝"):
        print(f"=== {r['彩种']} 不适用 ===")
        print(f"机制: {r['机制']}")
        print(f"原因: {r['原因']}")
        return
    print(f"=== {r['彩种']} 实得奖金选号（期号 {r['期号'] or '最新'}）===")
    print(f"候选 {r['候选数']:,} → 选中 {r['选中数']} 组（重叠上限 {r['重叠上限']}）")
    print(f"机制: {r['机制']}")
    for i, t in enumerate(r["选中"], 1):
        if "红球" in t["号码"]:
            shown = f"红球 {' '.join(f'{d:02d}' for d in t['号码']['红球'])} + " \
                    f"蓝球 {' '.join(f'{d:02d}' for d in t['号码']['蓝球'])}"
        else:
            shown = " ".join(map(str, t["号码"]["号码"]))
        jz = f"  一等奖实得估计 ¥{t['一等奖实得']:,.0f}" if t.get("一等奖实得") else ""
        nev = t.get("净EV")
        nev_s = f"  净EV {nev:+.4f} 元/注" if isinstance(nev, (int, float)) else "  净EV —（EV 恒定，未打分）"
        print(f"  [{i}] {shown}{nev_s}{jz}")
    print("  ⚠️ 不改变中奖概率，长期期望仍为负；本输出仅为奖金侧优化")


def _cmd_trials(args):
    """NetPayout WP3：前瞻配对对照实验（记录 / 结算 / 报告 / 功效规划）"""
    import json as _json
    from ev import trials as T

    if args.plan:
        print(f"=== {args.lottery} 对照实验功效规划（主终点=中奖率等价性）===")
        pl = T.power_plan(args.lottery, n_per_issue=args.groups)
        print(f"  每注中奖概率 {pl['每注中奖概率']:.4f}　每年 {pl['每年期数']} 期　每组 {pl['每组注数']} 注")
        for row in pl["规划"]:
            print(f"  检出{row['相对效应']}差异：需 {row['需要注数_每组']:,} 注/组 "
                  f"= {row['需要期数']:,} 期 ≈ {row['折合年数']} 年")
        wt = T.wait_time_years(args.lottery, args.groups)
        print("  次终点（实得奖金差异，不可完成）：")
        for g, v in wt["期望等待"].items():
            print(f"    {g}奖 {v['概率']} → 期望等待 {v['期望等待年数']:,.0f} 年")
        print(f"  {wt['结论']}")
        return

    if args.record:
        r = T.record_trial(args.lottery, n=args.groups, seed=args.seed)
        print(f"  记录：{'跳过（本期已有）' if r.get('跳过') else ('成功' if r.get('ok') else '失败')}"
              f"　期号 {r.get('期号', '-')}　{'' if r.get('ok') else r.get('原因', '')}")
        if not r.get("ok"):
            return

    if args.settle:
        s = T.settle_trials(args.lottery)
        if "error" in s:
            print(f"  结算失败：{s['error']}")
            return
        print(f"  结算：新增 {s['新结算期数']} 期（累计已结算 {s['已结算期数']} 期，"
              f"未开奖 {s['未开奖期数']} 期）")

    rep = T.trial_report(args.lottery)
    print(f"=== {args.lottery} A/B 对照实验报告 ===")
    print(f"  记录 {rep['总记录期数']} 期 / 已结算 {rep['已结算期数']} 期 / 未开奖 {rep['未开奖期数']} 期"
          f"（每组 {rep['每组注数']} 注）")
    if rep["已结算期数"] >= 2:
        print("  【主终点｜可完成】中奖率等价性")
        print(f"    A 累计中奖 {rep['A累计中奖注数']} 注 / B {rep['B累计中奖注数']} 注 "
              f"（差 {rep['中奖注数差']:+d}，等价边际 ±{rep['等价边际±10%']:.0f}）")
        print(f"    有差异期数 {rep['有差异期数']}（A 占优 {rep['A占优期数']}）　"
              f"符号检验 p={rep['符号检验p']}　序贯 e-value={rep['序贯e_value']}")
        print("  【次终点｜不可完成】实得奖金")
        print(f"    A ¥{rep['A累计实得']:,.2f} / B ¥{rep['B累计实得']:,.2f}　"
              f"Δ=¥{rep['实得差Δ']:,.2f}　95%CI [{rep['Δ95%CI'][0]:,.0f}, {rep['Δ95%CI'][1]:,.0f}]")
        print(f"    {rep['次终点声明']}")
    print(f"  裁决: {rep['裁决']}")
    if rep.get("建议"):
        print(f"  建议: {rep['建议']}")


def _cmd_drift(args):
    """NetPayout 维护：人群选号行为漂移监控 + 自动重拟合建议"""
    from ev.popularity import drift_report

    r = drift_report(args.lottery)
    print(f"=== {args.lottery} 人群行为漂移监控 ===")
    if not r.get("go"):
        print(f"  {r.get('建议')}")
        return
    print(f"  样本 {r['样本期数']} 期（缓存模型基于 {r['缓存模型期数']} 期，落后 {r['落后期数']} 期）")
    if r["分段提升"]:
        print("  分段冷门提升倍数（时间顺序）：")
        for seg in r["分段提升"]:
            print(f"    第{seg['段']}段（{seg['期数']}期）：{seg['倍数_中位']}×")
    if r["β漂移_p"] is not None:
        print(f"  β 前后半段对比：χ²={r['β漂移卡方']}（df={r['β漂移_df']}）p={r['β漂移_p']}"
              f"　最漂移特征={r['最漂移特征']}（z={r['最漂移特征_z']}）")
    if r.get("近期校准比") is not None:
        print(f"  近期/早期校准比: {r['近期校准比']}（1.0 = 总量未漂移）")
    print(f"  裁决: {r['裁决']}")
    print(f"  建议: {r['建议']}")
    print(f"  性质: {r['性质说明']}")


def _cmd_rollover(args):
    """双/大 Rollover-EV"""
    from ev.rollover import rollover_ev, rollover_history

    if args.history:
        rows = rollover_history(args.lottery, n=args.history)
        print(f"=== {args.lottery} 最近 {len(rows)} 期 Rollover 趋势 ===")
        print(f"  {'期号':<8}{'奖池':>14}{'头奖单注估计':>14}  参与信号")
        for r in rows:
            print(f"  {r['期号']:<8}{r['奖池']:>14,.0f}{r['头奖单注估计']:>14,.0f}  "
                  f"{'★' if r['参与信号'] else '·'}")
        return

    r = rollover_ev(args.lottery, issue=args.issue)
    if "error" in r:
        print(f"[失败] {r['error']}")
        return
    print(f"=== {args.lottery} Rollover-EV（期号 {r['期号']}）===")
    for k, v in r.items():
        if k in ("彩种", "期号"):
            continue
        if isinstance(v, float):
            print(f"  {k}: {v:,.4f}")
        else:
            print(f"  {k}: {v}")
    print(f"  结论: {'★ EV天花板窗口（头奖单注顶格），可小额参与（娱乐级，仍负EV）' if r['参与信号'] else '· 未到EV天花板窗口，不参与'}")


def _cmd_plan(args):
    """Kelly + 资金预算 + 蒙特卡洛"""
    from ev.kelly import kelly_from_ev, monte_carlo_sim
    from ev.bankroll import budget_plan, Budget

    f = kelly_from_ev(args.ev, args.cost, args.prob)
    print(f"=== 资金计划（单注EV={args.ev} 元, p={args.prob:.3e}）===")
    print(f"  Kelly(半Kelly): {f:.6f}  {'(EV≤0 → 0，纪律不下注)' if f <= 0 else '(正EV，按比例参与)'}")
    print(f"  EV≤0 时 Kelly=0 是数学结论：负期望下任何仓位都是负收益")

    bp = budget_plan(Budget(bankroll=args.bankroll, cost_per_ticket=args.cost))
    print(f"  资金预算: 本金{args.bankroll:.0f} 单期预算{bp['单期预算(2%)']:.0f}元 "
          f"({bp['单期最大注数']}注) 止损线{bp['止损线']} 熔断{bp['熔断阈值']}")

    if args.sim and f > 0:
        sim = monte_carlo_sim(args.ev, args.prob, args.cost, args.bankroll,
                              stake_frac=f, n_periods=args.periods)
        print(f"  蒙特卡洛({args.periods}期): P5={sim['终值P5']} P50={sim['终值P50']} "
              f"P95={sim['终值P95']} 最大回撤={sim['平均最大回撤']:.1%} 破产率={sim['破产率(跌破20%本金)']:.1%}")
        print(f"  结论: {sim['结论']}")


if __name__ == "__main__":
    main()
