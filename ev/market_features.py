# -*- coding: utf-8 -*-
"""ev/market_features.py —— Phase 2 市场假设研究的「只读数据层」

只从 market/market.db 读，绝不写。它把市场维度的新数据（出球顺序 / 头奖分省 /
分省月销量）抽成干净的 Python 结构，供 ev/market_backtest.py 做信号检验。

为什么单独成层：
  - market.db 是旁路补偿库（删 .db 即回滚），这里用 mode=ro 强制只读；
  - 真源字段名（seq_front / first_region_raw / region_sales.ym）与上层研究代码解耦；
  - 彩种在库里用**代码**（ssq/dlt/pl3/pl5/qxc/sd/qlc/kl8），这里统一出入口。

死数据红线（项目级，写在此处集中提醒）：
  - 库里**没有**全国每注购买量 ⇒ 「最少购买组合」不可直接求得；
  - 库里**没有**每彩种每期分地区下注量 ⇒ 无法做「某期某省被安排中奖」的细检验；
  - region_sales 只有「福彩/体彩」两 agency，无彩种维度、无中奖地区。
这些边界是 Phase 2 结论的硬约束，任何「可预测」结论都不得越过。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------- 常量

# ★ 2026-10-26 修复：原来是硬编码绝对路径 "E:/707/market/market.db"——换一台
# 机器（或任一不是 E: 盘的环境）就静默指向不存在的文件，报错信息还挂着本机路径。
# 与 data/prize_amounts._db_path / market/build.py 一致改成 repo 相对。
DB_PATH = str(Path(__file__).resolve().parents[1] / "market" / "market.db")

CODE_TO_NAME = {
    "ssq": "双色球", "dlt": "大乐透", "pl3": "排列3", "pl5": "排列5",
    "qxc": "七星彩", "sd": "福彩3D", "qlc": "七乐彩", "kl8": "快乐8",
}
NAME_TO_CODE = {v: k for k, v in CODE_TO_NAME.items()}

# 每个出球顺序可用彩种的「位置规格」。
#   positions : 出球顺序长度（=前区/数字位数；后区不计入 seq_front）
#   lo/hi     : 各位置统一取值范围（乐透型无放回，但每个位置的边缘分布仍均匀）
#   per_pos   : 逐位置上限（仅 qxc 第7位 0..14 与其余 0..9 不同）
#   replace   : 数字型逐位独立=True；乐透型无放回=False（边缘仍均匀，期望同 1/(hi-lo+1)）
LOT_SPEC: Dict[str, Dict[str, object]] = {
    "ssq": {"positions": 6, "lo": 1, "hi": 33, "replace": False},
    "dlt": {"positions": 5, "lo": 1, "hi": 35, "replace": False},
    "qlc": {"positions": 7, "lo": 1, "hi": 30, "replace": False},
    "pl5": {"positions": 5, "lo": 0, "hi": 9, "replace": True},
    "sd":  {"positions": 3, "lo": 0, "hi": 9, "replace": True},
    "qxc": {"positions": 7, "lo": 0, "hi": 9,
            "per_pos_hi": [9, 9, 9, 9, 9, 9, 14], "replace": True},
}


# ---------------------------------------------------------------- 连接

def connect_ro() -> sqlite3.Connection:
    """只读连接（uri mode=ro），失败时给出清晰报错。"""
    try:
        # 反斜杠归一化：与 market/update.py 同款。Windows 上本机 SQLite
        # 能直读反斜杠 URI，但按 URI 规范它并非转义符，跨版本/跨平台不稳。
        return sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"),
                               uri=True)
    except sqlite3.Error as e:  # pragma: no cover - 环境错误
        raise RuntimeError(f"无法以只读方式打开 market.db: {DB_PATH} ({e})")


# ---------------------------------------------------------------- 出球顺序

def load_seq_front(code: str, source: Optional[str] = None,
                   limit: Optional[int] = None) -> List[Tuple[str, str, List[int]]]:
    """出球顺序原始序列。返回 [(期号, 开奖日, [按顺序的号码...]), ...]（按时间升序）。

    只取 seq_front 非空且能解析的期；source 可限定数据来源（如 'zhcw'/'sporttery'）。
    limit 用于测试/抽样，取最近 limit 期。
    """
    if code not in LOT_SPEC:
        return []
    sql = ("SELECT issue, draw_date, seq_front FROM draw "
           "WHERE lottery=? AND seq_front IS NOT NULL AND seq_front <> ''")
    args: list = [code]
    if source:
        sql += " AND source=?"
        args.append(source)
    sql += " ORDER BY issue_year, issue_seq"
    if limit:
        sql += f" LIMIT {int(limit)}"
    out: List[Tuple[str, str, List[int]]] = []
    con = connect_ro()
    try:
        for issue, date, raw in con.execute(sql, args):
            try:
                seq = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if not seq:
                continue
            try:
                out.append((issue, date, [int(x) for x in seq]))
            except (TypeError, ValueError):
                continue
    finally:
        con.close()
    return out


# ---------------------------------------------------------------- 头奖分省

def load_prize_region(code: str) -> List[Tuple[str, str, int]]:
    """头奖分省注数。返回 [(期号, 省份, 注数), ...]。

    仅 ssq/qlc/kl8 有数据（福彩官方 content 字段）。3D/体彩全无。
    """
    if code not in ("ssq", "qlc", "kl8"):
        return []
    con = connect_ro()
    try:
        rows = con.execute(
            "SELECT issue, region, winners FROM prize_region WHERE lottery=?",
            (code,)).fetchall()
    finally:
        con.close()
    return [(r[0], r[1], int(r[2])) for r in rows]


def province_jackpot_totals(code: str) -> Dict[str, int]:
    """每个省份的头奖注数合计。"""
    totals: Dict[str, int] = {}
    for _issue, region, w in load_prize_region(code):
        totals[region] = totals.get(region, 0) + w
    return totals


# ---------------------------------------------------------------- 分省月销量

def load_region_sales(agency: str = "福彩",
                      limit: Optional[int] = None) -> List[Tuple[str, str, float, Optional[float], Optional[float]]]:
    """分省月销量。返回 [(ym, 省份, sales_wan, yoy, cumulative_wan), ...]（按 ym, 省份）。

    agency ∈ {'福彩','体彩'}（库里无彩种维度）。
    ym 统一规范为 'YYYYMM'（库内原始可能是 'YYYY-MM'）。
    """
    sql = ("SELECT ym, region, sales_wan, yoy, cumulative_wan FROM region_sales "
           "WHERE agency=? ORDER BY ym, region")
    if limit:
        sql += f" LIMIT {int(limit)}"
    con = connect_ro()
    try:
        rows = con.execute(sql, (agency,)).fetchall()
    finally:
        con.close()
    out: List[Tuple[str, str, float, Optional[float], Optional[float]]] = []
    for ym, region, sales, yoy, cum in rows:
        ymn = str(ym).replace("-", "")
        out.append((ymn, region, float(sales),
                    (float(yoy) if yoy is not None else None),
                    (float(cum) if cum is not None else None)))
    return out


def province_sales_totals(agency: str = "福彩") -> Dict[str, float]:
    """每个省份的累计销量合计（所有月份 sales_wan 求和）。"""
    totals: Dict[str, float] = {}
    for _ym, region, sales, _yoy, _cum in load_region_sales(agency):
        totals[region] = totals.get(region, 0.0) + sales
    return totals


def region_sales_missing_months(agency: str = "福彩") -> List[str]:
    """销量表缺失的月份（与 2008-01→最新 的连续月份比对）。"""
    rows = load_region_sales(agency)
    if not rows:
        return []
    yms = sorted({r[0] for r in rows})
    # 以首末月份展开连续月份
    y0, m0 = int(yms[0][:4]), int(yms[0][4:6])
    y1, m1 = int(yms[-1][:4]), int(yms[-1][4:6])
    full = []
    y, m = y0, m0
    while (y, m) <= (y1, m1):
        full.append(f"{y:04d}{m:02d}")
        m += 1
        if m > 12:
            y += 1
            m = 1
    return [ym for ym in full if ym not in set(yms)]
