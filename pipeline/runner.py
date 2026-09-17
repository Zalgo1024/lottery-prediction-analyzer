"""
Pipeline 编排器
按序执行 5 个计算步骤，支持全量运行和单步/范围执行
"""

import logging
from typing import Dict, List, Optional

from config import LOTTERY_CONFIG
from data.schema import LotteryData
from data.loader import load_lottery

logger = logging.getLogger(__name__)

# 步骤映射
STEP_MODULES = {
    1: "pipeline.step1_probability",
    2: "pipeline.step2_expected",
    3: "pipeline.step3_distribution",
    4: "pipeline.step4_risk",
    5: "pipeline.step5_betting",
}

STEP_NAMES = {
    1: "基础概率",
    2: "收益与期望",
    3: "统计分布与多次试验",
    4: "风险度量与统计波动",
    5: "风控与最优投注",
}


def _import_step(step_num: int):
    """动态导入步骤模块"""
    import importlib
    mod_path = STEP_MODULES[step_num]
    return importlib.import_module(mod_path)


def _parse_step_range(step_spec: str) -> List[int]:
    """解析步骤范围，如 '1-2' → [1, 2], '3-5' → [3, 4, 5]"""
    if "-" in step_spec:
        parts = step_spec.split("-")
        start, end = int(parts[0]), int(parts[1])
        return list(range(start, end + 1))
    return [int(step_spec)]


def run_step(
    step_num: int,
    data: LotteryData,
    previous_results: Optional[Dict[int, dict]] = None,
) -> dict:
    """
    执行单个步骤
    """
    mod = _import_step(step_num)
    logger.info(f"执行 Step {step_num}: {STEP_NAMES[step_num]}")

    result = mod.run(data)

    # 传入之前步骤的结果（供后续步骤使用）
    if previous_results:
        result["_previous"] = {str(k): v for k, v in previous_results.items()}

    return result


def run_pipeline(
    lottery_name: str,
    steps: Optional[List[int]] = None,
) -> Dict[int, dict]:
    """
    执行 pipeline

    参数：
    - lottery_name: 彩票类型
    - steps: 要执行的步骤列表，None 表示全部 1-5

    返回步骤编号 → 结果的字典
    """
    if steps is None:
        steps = list(range(1, 6))

    logger.info(f"开始 Pipeline: {lottery_name} → 步骤 {steps}")

    # 加载数据
    data = load_lottery(lottery_name)
    logger.info(f"数据加载完成: {len(data.records)} 条记录")

    results = {}
    for step_num in steps:
        if step_num not in STEP_MODULES:
            logger.warning(f"跳过未知步骤: {step_num}")
            continue

        # 检查模块是否存在
        try:
            mod = _import_step(step_num)
        except (ImportError, ModuleNotFoundError) as e:
            logger.warning(f"步骤 {step_num} 模块未找到: {e}")
            continue

        result = mod.run(data)
        results[step_num] = result
        logger.info(f"Step {step_num} 完成")

    return results


def run_pipeline_cli(args) -> None:
    """CLI 入口"""
    lottery_name = args.lottery
    if args.all:
        steps = list(range(1, 6))
    elif args.steps:
        steps = _parse_step_range(args.steps)
    else:
        steps = list(range(1, 6))

    results = run_pipeline(lottery_name, steps)

    # 打印摘要
    for step_num in sorted(results.keys()):
        r = results[step_num]
        print(f"\n{'='*50}")
        print(f"Step {step_num}: {STEP_NAMES[step_num]}")
        print(f"{'='*50}")

        if step_num == 1:
            _print_step1(r)
        elif step_num == 2:
            _print_step2(r)
        elif step_num == 3:
            _print_step3(r)
        elif step_num == 4:
            _print_step4(r)
        elif step_num == 5:
            _print_step5(r)
        else:
            # 其他步骤打印摘要
            for k, v in r.items():
                if k != "step" and k != "lottery_name" and not k.startswith("_"):
                    if isinstance(v, dict):
                        print(f"  {k}: {len(v)} 项")
                    elif isinstance(v, list):
                        print(f"  {k}: {len(v)} 项")
                    else:
                        print(f"  {k}: {v}")


def _print_step1(r: dict):
    print(f"  总组合数: {r['total_combinations']:,}")
    print(f"  头奖概率: 1/{1/r['head_prize_probability']:,.0f}")
    print(f"  总中奖概率: {r['total_win_probability']*100:.4f}%")
    print(f"  各奖级:")
    for level in r["prize_levels"]:
        pct = level["probability"] * 100
        print(f"    {level['prize_name']:8s}  {level['condition']:20s}  概率 {pct:.6f}%  {level.get('概率倒数','')}")


def _print_step2(r: dict):
    ev = r["expected_value"]
    print(f"  单注期望收益: {ev['单注期望收益']} 元")
    print(f"  {ev['结论']}")
    print(f"  理论返奖率: {r['official_return_rate']['理论返奖率']:.2f}%")


def _print_step3(r: dict):
    b = r["二项分布(100期)"]
    print(f"  100期至少中一次: {b['至少中一次概率']*100:.2f}%")
    g = r["几何分布(首次中奖)"]
    print(f"  首次中奖期望期数: {g['期望首次中奖期数']}")
    print(f"  50期内累积中奖概率: {g['50期内累积中奖概率']*100:.2f}%")
    s = r["蒙特卡洛模拟"]
    print(f"  模拟100期: 平均中奖{s['中奖次数统计']['均值']}次, 盈利比例{s['简化盈亏统计']['盈利比例']}%")


def _print_step4(r: dict):
    v = r["方差与标准差"]
    print(f"  标准差: {v['标准差']} (期望{v['期望收益']})")
    print(f"  信息熵: {r['信息熵']['总信息熵']}")
    ci = r["置信区间"]
    print(f"  1000期95%置信区间: {ci['95%置信区间']}")
    hv = r["历史波动分析"]
    if "红球" in hv:
        print(f"  红球偏离度: {hv['红球']['偏离度']}")
        print(f"  蓝球偏离度: {hv['蓝球']['偏离度']}")
    else:
        for zname, zv in hv.get("各分区", {}).items():
            print(f"  {zname}偏离度: {zv['偏离度']}")


def _print_step5(r: dict):
    k = r["凯利准则"]
    print(f"  凯利比例: {k['综合凯利比例']} ({k['结论']})")
    print(f"  夏普比率: {r['夏普比率']['夏普比率']} ({r['夏普比率']['评价']})")
    f = r["假设检验(公平性)"]
    if "红球检验" in f:
        print(f"  红球公平性: {f['红球检验']['结论']} (p={f['红球检验']['p值']})")
        print(f"  蓝球公平性: {f['蓝球检验']['结论']} (p={f['蓝球检验']['p值']})")
    else:
        for zname, zt in f.get("各分区检验", {}).items():
            print(f"  {zname}公平性: {zt['结论']} (p={zt['p值']})")
