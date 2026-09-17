"""
数据清洗模块
负责清洗 CSV 中的 # 占位值、修复类型、校验合法性、生成清洗报告
"""

import logging
from typing import Dict, List, Tuple

from config import LOTTERY_CONFIG, SSQ_CSV, DLT_CSV, SSQ_CLEANED, DLT_CLEANED
from data.loader import load_raw_df, load_lottery, _parse_int, FILE_PATHS
from data.schema import DrawRecord, parse_draw_date

logger = logging.getLogger(__name__)

# 清洗输出路径
CLEANED_PATHS = {
    "双色球": SSQ_CLEANED,
    "大乐透": DLT_CLEANED,
}


class CleanReport:
    """清洗报告"""

    def __init__(self, lottery_name: str):
        self.lottery_name = lottery_name
        self.total_rows = 0
        self.cleaned_rows = 0
        self.issues: List[str] = []

    def add_issue(self, msg: str):
        self.issues.append(msg)

    def summary(self) -> str:
        lines = [
            f"===== {self.lottery_name} 清洗报告 =====",
            f"原始行数: {self.total_rows}",
            f"清洗后行数: {self.cleaned_rows}",
            f"丢弃行数: {self.total_rows - self.cleaned_rows}",
        ]
        if self.issues:
            lines.append(f"问题记录 ({len(self.issues)} 条):")
            for i, issue in enumerate(self.issues[:20], 1):
                lines.append(f"  {i}. {issue}")
            if len(self.issues) > 20:
                lines.append(f"  ... 还有 {len(self.issues) - 20} 条")
        return "\n".join(lines)


def run_clean(lottery_name: str) -> CleanReport:
    """
    清洗指定彩票的 CSV 文件，输出清洗后文件
    返回清洗报告
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    red_count = cfg["red_count"]
    blue_count = cfg["blue_count"]
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]

    report = CleanReport(lottery_name)
    df = load_raw_df(lottery_name, use_cleaned=False)
    report.total_rows = len(df)

    cleaned_rows = []
    for idx, row in df.iterrows():
        row_issues = []

        期号 = _parse_int(row.get("期号"))
        if 期号 is None:
            continue

        # 解析红球
        reds = []
        for i in range(1, red_count + 1):
            v = _parse_int(row.get(f"红球{i}"))
            if v is None:
                row_issues.append(f"红球{i} 无效值: {row.get(f'红球{i}')}")
                break
            if not (r_min <= v <= r_max):
                row_issues.append(f"红球{i}={v} 超出范围 [{r_min},{r_max}]")
                break
            reds.append(v)
        else:
            # 红球去重检查
            if len(set(reds)) != red_count:
                row_issues.append(f"红球存在重复: {reds}")
                continue

        if row_issues:
            for issue in row_issues:
                report.add_issue(f"期号{期号}: {issue}")
            continue

        # 解析蓝球
        blues = []
        for i in range(1, blue_count + 1):
            v = _parse_int(row.get(f"蓝球{i}"))
            if v is None:
                report.add_issue(f"期号{期号}: 蓝球{i} 无效值: {row.get(f'蓝球{i}')}")
                break
            if not (b_min <= v <= b_max):
                report.add_issue(f"期号{期号}: 蓝球{i}={v} 超出范围 [{b_min},{b_max}]")
                break
            blues.append(v)
        else:
            if len(set(blues)) != blue_count:
                report.add_issue(f"期号{期号}: 蓝球存在重复: {blues}")
                continue

        # 解析奖金字段（替换 # 为 0）
        奖池 = _clean_bonus(row.get("奖池奖金"))
        一注 = _clean_bonus(row.get("一等奖注数"))
        一金 = _clean_bonus(row.get("一等奖奖金"))
        二注 = _clean_bonus(row.get("二等奖注数"))
        二金 = _clean_bonus(row.get("二等奖奖金"))
        投注 = _clean_bonus(row.get("总投注额"))

        # 解析日期
        日期 = parse_draw_date(row.get("开奖日期", ""))

        cleaned_rows.append({
            "期号": 期号,
            **{f"红球{i}": reds[i - 1] for i in range(1, red_count + 1)},
            **{f"蓝球{i}": blues[i - 1] for i in range(1, blue_count + 1)},
            "奖池奖金": 奖池,
            "一等奖注数": 一注,
            "一等奖奖金": 一金,
            "二等奖注数": 二注,
            "二等奖奖金": 二金,
            "总投注额": 投注,
            "开奖日期": 日期.strftime("%d/%m/%Y") if 日期 else "",
        })

    report.cleaned_rows = len(cleaned_rows)

    if cleaned_rows:
        import pandas as pd
        out_df = pd.DataFrame(cleaned_rows)
        out_path = CLEANED_PATHS[lottery_name]
        out_df.to_csv(out_path, index=False, encoding="utf-8-sig")
        logger.info(f"清洗完成，输出到 {out_path}，共 {len(cleaned_rows)} 行")
    else:
        logger.warning(f"{lottery_name} 清洗后无有效数据")

    print(report.summary())
    return report


def _clean_bonus(value) -> int:
    """
    清理奖金/金额字段
    # 或空 → 0，数字字符串 → int，去除逗号
    """
    if value is None:
        return 0
    s = str(value).strip().replace(",", "").replace(" ", "")
    if not s or s.startswith("#"):
        return 0
    try:
        return int(s)
    except ValueError:
        return 0


def clean_all() -> Dict[str, CleanReport]:
    """清洗全部彩票数据"""
    reports = {}
    for name in LOTTERY_CONFIG:
        reports[name] = run_clean(name)
    return reports
