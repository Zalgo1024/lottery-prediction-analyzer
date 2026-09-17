"""
CSV 数据加载器
负责读取原始 CSV，统一列名映射，返回结构化 LotteryData
"""

import logging
import os
from typing import Optional

import pandas as pd

from config import SSQ_CSV, DLT_CSV, LOTTERY_CONFIG, CLEANED_FILE_PATHS, BASE_DIR, LOTTERY_DATA_DIR
from data.schema import DrawRecord, LotteryData, parse_draw_date

logger = logging.getLogger(__name__)

# CSV 编码探测与回退
CSV_ENCODINGS = ["utf-8-sig", "gb2312", "gbk", "gb18030", "latin-1"]

# ===== 列名映射 =====
# 原始 CSV 列名 → 统一内部列名
SSQ_COLUMN_MAP = {
    "期号": "期号",
    "红球1": "红球1",
    "红球2": "红球2",
    "红球3": "红球3",
    "红球4": "红球4",
    "红球5": "红球5",
    "红球6": "红球6",
    "蓝球1": "蓝球1",
    "奖池奖金(元)": "奖池奖金",
    "一等奖注数": "一等奖注数",
    "一等奖奖金（元）": "一等奖奖金",
    "二等奖注数": "二等奖注数",
    "二等奖奖金（元）": "二等奖奖金",
    "总投注额(元)": "总投注额",
    "开奖日期": "开奖日期",
}

DLT_COLUMN_MAP = {
    "期号": "期号",
    "红球1": "红球1",
    "红球2": "红球2",
    "红球3": "红球3",
    "红球4": "红球4",
    "红球5": "红球5",
    "蓝球1": "蓝球1",
    "蓝球2": "蓝球2",
    "奖池奖金(元)": "奖池奖金",
    "一等奖注数": "一等奖注数",
    "一等奖奖金（元）": "一等奖奖金",
    "二等奖注数": "二等奖注数",
    "二等奖奖金（元）": "二等奖奖金",
    "总投注额(元)": "总投注额",
    "开奖日期": "开奖日期",
}

COLUMN_MAPS = {
    "双色球": SSQ_COLUMN_MAP,
    "大乐透": DLT_COLUMN_MAP,
}

FILE_PATHS = {
    "双色球": SSQ_CSV,
    "大乐透": DLT_CSV,
}

# 新彩种（阶段2）的默认历史 CSV 路径：<名称>历史数据.csv（位于 LOTTERY_DATA_DIR）
def _file_path(lottery_name: str) -> "Path":
    """返回某彩种原始 CSV 路径；新彩种按约定文件名推导"""
    if lottery_name in FILE_PATHS:
        return FILE_PATHS[lottery_name]
    return LOTTERY_DATA_DIR / f"{lottery_name}历史数据.csv"


def _resolve_csv_path(lottery_name: str, use_cleaned: bool = True) -> "Path":
    """返回实际要读取的 CSV 路径（优先 cleaned，回退原始）。"""
    if use_cleaned:
        path = CLEANED_FILE_PATHS.get(lottery_name)
        if path and path.exists():
            return path
    return _file_path(lottery_name)


# ===== 内存缓存：避免每次请求都重读 CSV + 逐行解析 =====
# key = 彩种名, value = (文件mtime, LotteryData)。数据文件未修改时直接复用。
_lottery_cache: dict = {}


def invalidate_lottery_cache(lottery_name: str = None):
    """清除内存缓存。数据更新/刷新后调用，确保下次 load_lottery 重新读取。"""
    if lottery_name:
        _lottery_cache.pop(lottery_name, None)
    else:
        _lottery_cache.clear()
    logger.info(f"内存缓存已清除: {lottery_name or '全部'}")


def _generic_col_map(df) -> dict:
    """为新彩种构造列映射：保留 期号/开奖日期 与所有 号码* 列"""
    keep = {"期号", "开奖日期"}
    col_map = {}
    for c in df.columns:
        if c in keep or str(c).startswith("号码"):
            col_map[c] = c
    return col_map


def _parse_int(value) -> Optional[int]:
    """安全转换为 int，'#' 或空返回 None"""
    if value is None:
        return None
    s = str(value).strip().replace(",", "").replace(" ", "")
    if not s or s.startswith("#"):
        return None
    try:
        return int(s)
    except ValueError:
        return None


def load_raw_df(lottery_name: str, use_cleaned: bool = True) -> pd.DataFrame:
    """
    读取 CSV 返回 pandas DataFrame（优先使用清洗后数据）
    列名已映射为统一内部名

    use_cleaned=False 时强制读取原始文件（如数据清洗场景）
    """
    path = _resolve_csv_path(lottery_name, use_cleaned)
    cleaned = CLEANED_FILE_PATHS.get(lottery_name)
    if use_cleaned and path == cleaned:
        logger.info(f"使用清洗后数据: {path}")
    elif use_cleaned:
        logger.info(f"清洗文件不存在，使用原始数据: {path}")
    else:
        logger.info(f"强制使用原始数据（清洗模式）: {path}")

    # 自动尝试编码
    df = None
    errors = []
    for enc in CSV_ENCODINGS:
        try:
            df = pd.read_csv(path, encoding=enc, dtype=str)
            logger.info(f"编码 {enc} 读取成功")
            break
        except (UnicodeDecodeError, UnicodeError) as e:
            errors.append(f"{enc}: {e}")
            continue
    if df is None:
        raise RuntimeError(f"无法解码 CSV 文件 {path}，尝试了编码：{', '.join(CSV_ENCODINGS)}")
    # 统一列名
    col_map = COLUMN_MAPS.get(lottery_name) or _generic_col_map(df)
    df.rename(columns=col_map, inplace=True)
    # 只保留映射过的列
    expected = list(col_map.values())
    df = df[[c for c in expected if c in df.columns]]
    logger.info(f"加载完成：{len(df)} 行")
    return df


def load_lottery(lottery_name: str) -> LotteryData:
    """
    加载指定彩票的所有历史记录，返回 LotteryData。

    双色球/大乐透走原有红/蓝写死逻辑（零回归）；
    阶段2 新彩种（数字型/乐透型）走泛型分区逻辑：按 Schema.zones
    从 CSV 的 号码1..N 列按序映射到各分区。
    """
    # 内存缓存：数据文件未修改时直接复用，避免重复读 CSV + 逐行解析
    csv_path = _resolve_csv_path(lottery_name)
    try:
        _mtime = os.path.getmtime(csv_path)
    except OSError:
        _mtime = 0
    cached = _lottery_cache.get(lottery_name)
    if cached and cached[0] == _mtime:
        logger.debug(f"内存缓存命中: {lottery_name}")
        return cached[1]

    df = load_raw_df(lottery_name)
    cfg = LOTTERY_CONFIG[lottery_name]
    red_count = cfg.get("red_count")
    blue_count = cfg.get("blue_count")

    # 旧彩种：保持原有红/蓝解析逻辑不变
    if red_count is not None and blue_count is not None:
        records = []
        for _, row in df.iterrows():
            期号 = _parse_int(row.get("期号"))
            if 期号 is None:
                continue
            reds = []
            for i in range(1, red_count + 1):
                v = _parse_int(row.get(f"红球{i}"))
                if v is not None:
                    reds.append(v)
            blues = []
            for i in range(1, blue_count + 1):
                v = _parse_int(row.get(f"蓝球{i}"))
                if v is not None:
                    blues.append(v)
            record = DrawRecord(
                期号=期号,
                开奖日期=parse_draw_date(row.get("开奖日期", "")),
                奖池奖金=_parse_int(row.get("奖池奖金")),
                一等奖注数=_parse_int(row.get("一等奖注数")),
                一等奖奖金=_parse_int(row.get("一等奖奖金")),
                二等奖注数=_parse_int(row.get("二等奖注数")),
                二等奖奖金=_parse_int(row.get("二等奖奖金")),
                总投注额=_parse_int(row.get("总投注额")),
            )
            record.红球 = reds
            record.蓝球 = blues
            records.append(record)
        # 统一按开奖期号降序排列，保证 records[0] 永远是最新一期
        records.sort(key=lambda r: r.期号 or 0, reverse=True)
        result = LotteryData(lottery_name=lottery_name, records=records)
        _lottery_cache[lottery_name] = (_mtime, result)
        return result

    # 新彩种：泛型分区驱动（号码1..N 按序映射到各 Zone）
    from data.schema import get_schema
    schema = get_schema(lottery_name)
    records = []
    for _, row in df.iterrows():
        期号 = _parse_int(row.get("期号"))
        if 期号 is None:
            continue
        zone_numbers = {}
        for k, zone in enumerate(schema.zones):
            v = _parse_int(row.get(f"号码{k + 1}"))
            if v is None:
                v = _parse_int(row.get(zone.name))  # 兼容按区名直接成列
            if v is not None:
                zone_numbers[zone.name] = [v]
        record = DrawRecord(
            期号=期号,
            开奖日期=parse_draw_date(row.get("开奖日期", "")),
            zone_numbers=zone_numbers,
        )
        records.append(record)
    # 数字型 CSV 可能是升序，统一按开奖期号降序排列，保证 records[0] 为最新一期
    records.sort(key=lambda r: r.期号 or 0, reverse=True)
    logger.info(f"泛型加载完成：{lottery_name} {len(records)} 期")
    result = LotteryData(lottery_name=lottery_name, records=records)
    _lottery_cache[lottery_name] = (_mtime, result)
    return result
