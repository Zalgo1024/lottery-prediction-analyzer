# -*- coding: utf-8 -*-
"""data/prize_versions.py —— 大乐透奖级规则的**版本化单一真源**。

为什么必须建这个文件
--------------------
仓库里曾同时存在**三份**九级表（`data/prize_table.FIXED_PAYOUT`、
`ev/profit/rules._DLT_TABLE`、`ev/coverage_backtest.TIER_PAYOUT`），
而且 `ev/profit/rules.py` 把版本标成 `dlt-2026-02` 却装着 2019 九级的表。
后果是 **2026-02-02 之后每一期的结算与回测都按错误奖级/金额**。

大乐透实际有五段（13 个中奖条件只在 2014 之后恒定；2014 前是 8 级）：

| 版本 | 起(期号/日期) | 级数 | 四/五/六/七/八等固定奖 |
|---|---|---|---|
| dlt-2007-05 | 07001 / 2007-05-30 | 8 | 3000 / 500 / 200 / 10 / 5 |
| dlt-2009-10 | 09121 / 2009-10-17 | 8 | 3000 / 600 / 100 / 10 / 5 |
| dlt-2014-05 | 14052 / 2014-05-07 | 6 | 200 / 10 / 5（三等转为浮动） |
| dlt-2019-02 | 19019 / 2019-02-20 | 9 | 10000 / 3000 / 300 / 200 / 100 / 15 / 5 |
| dlt-2026-02 | 26014 / 2026-02-02 | 7 | 5000 / 300 / 150 / 15 / 5（8 亿上浮 6666/380/200/18/7） |

来源（全部为规则原文或其转载，非二手解读）：
  · 2007 八级：山东体彩网《超级大乐透35选5加12选2游戏规则》第十九条
    `sdticai.com/dlt/200705/t20070517_2521765.html`；江苏体彩网问答
    `js-lottery.com/cms/post-17756.html`（8 个奖级、四~八等固定 3000/500/200/10/5、
    追加只参与浮动奖 60%）；浙江日报 07040/07057 实际开奖表三源一致。
  · 2014 六级：财政部公告（人民网转载）`finance.people.com.cn/big5/n/2014/0422/c1004-24927010.html`
    第二十六条原文；三等为浮动（按注均分 7%）。
  · 2019 九级：6→9，原三/四/五等各拆两级；追加改为仅 1/2 等 80%。
  · 2026 七级：上海体彩网 `shsportslottery.com/qgxw/2385425213.html` 与 `/4395789134.html`、
    羊城晚报 `ep.ycwb.com/epaper/xkb/html/2026-02/04/content_1512_740920.htm`、
    信息时报 `xxsb.gz-cmc.com` —— 四源一致。

★ 金额优先取 `market.db.prize` 的实测值（见 `data/prize_amounts.py`），本文件的常量只是
  「规则应有的值」与「实测缺失时的兜底」。已验证：07001~09120 实测 3000/500/200，
  09121~14051 实测 3000/600/100，14052~19018 实测 200/10/5，与表一致。

★ 数据源缺口：`market.db` 在 2014 前只存了**前 6 级**（七等 10 元 / 八等 5 元 无记录行）。
  但那两级是**固定奖**，结算只需「我自己的票中了哪级 + 该级固定金额」，
  不需要全国注数，所以不影响按票结算；`pending_verification` 里标了这一点。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Dict, Optional, Tuple

from data.issue_key import issue_key

# (前区命中数 h, 后区命中数 j) → 奖级名
Hits = Tuple[int, int]


@dataclass(frozen=True)
class DltVersion:
    version: str
    from_key: Tuple[int, int]              # (年, 序号)
    from_date: str
    grade_table: Dict[Hits, str]
    fixed: Dict[str, float]                # 基本投注固定奖（不含浮动级）
    floating: Tuple[str, ...]              # 浮动奖级
    uplift: Optional[Dict[str, float]] = None   # 奖池≥8亿时的固定奖（仅 2026 版）
    uplift_pool: Optional[float] = None
    first_cap: float = 10_000_000.0        # 一等奖单注封顶（基本投注）
    addon_ratio: float = 0.0               # 追加单注 = 基本 × 该比例
    addon_levels: Tuple[str, ...] = ()     # 追加参与的奖级
    source: str = ""
    pending_verification: str = ""

    @property
    def level_count(self) -> int:
        return len(set(self.grade_table.values()))


def _fixed_amount(self, grade: str, uplift: bool = False) -> Optional[float]:
    if uplift and self.uplift and grade in self.uplift:
        return float(self.uplift[grade])
    v = self.fixed.get(grade)
    return float(v) if v is not None else None


DltVersion.fixed_amount = _fixed_amount      # type: ignore[attr-defined]


_T8_2007: Dict[Hits, str] = {
    (5, 2): "一等", (5, 1): "二等", (5, 0): "三等",
    (4, 2): "四等", (4, 1): "五等",
    (3, 2): "六等", (4, 0): "六等",
    (3, 1): "七等", (2, 2): "七等",
    (3, 0): "八等", (1, 2): "八等", (2, 1): "八等", (0, 2): "八等",
}

_T6_2014: Dict[Hits, str] = {
    (5, 2): "一等", (5, 1): "二等",
    (5, 0): "三等", (4, 2): "三等",
    (4, 1): "四等", (3, 2): "四等",
    (4, 0): "五等", (3, 1): "五等", (2, 2): "五等",
    (3, 0): "六等", (1, 2): "六等", (2, 1): "六等", (0, 2): "六等",
}

_T9_2019: Dict[Hits, str] = {
    (5, 2): "一等", (5, 1): "二等", (5, 0): "三等",
    (4, 2): "四等", (4, 1): "五等", (3, 2): "六等",
    (4, 0): "七等", (3, 1): "八等", (2, 2): "八等",
    (3, 0): "九等", (2, 1): "九等", (1, 2): "九等", (0, 2): "九等",
}

_T7_2026: Dict[Hits, str] = {
    (5, 2): "一等", (5, 1): "二等",
    (5, 0): "三等", (4, 2): "三等",     # ★ 5+0 与 4+2 合并
    (4, 1): "四等",
    (4, 0): "五等", (3, 2): "五等",     # ★ 4+0 与 3+2 合并
    (3, 1): "六等", (2, 2): "六等",
    (3, 0): "七等", (2, 1): "七等", (1, 2): "七等", (0, 2): "七等",
}

_SRC_2007 = ("山东体彩网《超级大乐透35选5加12选2游戏规则》第十九条 "
             "sdticai.com/dlt/200705/t20070517_2521765.html（8 级；四~八等 3000/500/200/10/5；"
             "追加仅参与浮动奖 60%）。浙江日报 07040/07057 开奖表实测一致。")
_SRC_2014 = ("财政部公告《关于变更中国体育彩票超级大乐透游戏规则的通知》（2014-04-21，"
             "14052 期起）第二十六条原文 finance.people.com.cn/big5/n/2014/0422/c1004-24927010.html"
             "（6 级；三等=5+0 或 4+2 且按注均分浮动奖金 7%）。")
_SRC_2019 = ("2019-02-20（19019 期）起 6 级改 9 级：原三/四/五等各拆两级；"
             "追加由「参与 1-5 等 60%」改为「仅 1/2 等 80%」。")
_SRC_2026 = ("上海体彩网 shsportslottery.com/qgxw/2385425213.html 与 /4395789134.html、"
             "羊城晚报 ep.ycwb.com/epaper/xkb/html/2026-02/04/content_1512_740920.htm、"
             "信息时报 xxsb.gz-cmc.com（四源一致）：13 个中奖条件不变；5+0 与 4+2 合并为三等奖，"
             "4+0 与 3+2 合并为五等奖；三~七等 5000/300/150/15/5；奖池≥8 亿上浮 "
             "6666/380/200/18/7；一、二等奖单期奖金总额各封顶 1 亿元。")

DLT_VERSIONS: Tuple[DltVersion, ...] = (
    DltVersion(
        version="dlt-2007-05", from_key=(2007, 1), from_date="2007-05-30",
        grade_table=dict(_T8_2007),
        fixed={"四等": 3000.0, "五等": 500.0, "六等": 200.0, "七等": 10.0, "八等": 5.0},
        floating=("一等", "二等", "三等"),
        first_cap=5_000_000.0,
        addon_ratio=0.60, addon_levels=("一等", "二等", "三等"),
        source=_SRC_2007,
        pending_verification=("数据源 market.db 该段只存前 6 级（无七/八等行）；"
                              "七/八等金额取规则原文 10/5。固定奖按票结算不依赖全国注数，故不影响回测。"),
    ),
    DltVersion(
        version="dlt-2009-10", from_key=(2009, 121), from_date="2009-10-17",
        grade_table=dict(_T8_2007),
        fixed={"四等": 3000.0, "五等": 600.0, "六等": 100.0, "七等": 10.0, "八等": 5.0},
        floating=("一等", "二等", "三等"),
        first_cap=5_000_000.0,
        addon_ratio=0.60, addon_levels=("一等", "二等", "三等"),
        source=_SRC_2007 + " 本段固定奖金额按 market.db 实测调整为 五等 600 / 六等 100。",
        pending_verification=("七/八等金额（10/5）为沿用上一段，数据库无该两级行，**待核实**；"
                              "五等 600 / 六等 100 为 market.db 实测（n=698）。"),
    ),
    DltVersion(
        version="dlt-2014-05", from_key=(2014, 52), from_date="2014-05-07",
        grade_table=dict(_T6_2014),
        fixed={"四等": 200.0, "五等": 10.0, "六等": 5.0},
        floating=("一等", "二等", "三等"),
        first_cap=5_000_000.0,
        addon_ratio=0.60, addon_levels=("一等", "二等", "三等"),
        source=_SRC_2014,
        pending_verification="四/五/六等 200/10/5 与 market.db 实测一致（n=735）。",
    ),
    DltVersion(
        version="dlt-2019-02", from_key=(2019, 19), from_date="2019-02-20",
        grade_table=dict(_T9_2019),
        fixed={"三等": 10000.0, "四等": 3000.0, "五等": 300.0, "六等": 200.0,
               "七等": 100.0, "八等": 15.0, "九等": 5.0},
        floating=("一等", "二等"),
        addon_ratio=0.80, addon_levels=("一等", "二等"),
        source=_SRC_2019,
        pending_verification="三~九等 10000/3000/300/200/100/15/5 与 market.db 实测一致。",
    ),
    DltVersion(
        version="dlt-2026-02", from_key=(2026, 14), from_date="2026-02-02",
        grade_table=dict(_T7_2026),
        fixed={"三等": 5000.0, "四等": 300.0, "五等": 150.0, "六等": 15.0, "七等": 5.0},
        floating=("一等", "二等"),
        uplift={"三等": 6666.0, "四等": 380.0, "五等": 200.0, "六等": 18.0, "七等": 7.0},
        uplift_pool=800_000_000.0,
        addon_ratio=0.80, addon_levels=("一等", "二等"),
        source=_SRC_2026,
        pending_verification=("「二等奖浮动计提比例 22%→40%」仅重庆体彩网单源、与其余来源不互证，"
                              "未进本表任何计算。8 亿上浮判定：实测 89 个非派奖期全命中、0 误判"
                              "（用**上一期** draw.pool ≥8 亿判定）。"),
    ),
)


class RuleVersionError(ValueError):
    """期号早于已知的规则版本。"""


def version_for_key(key: Tuple[int, int]) -> DltVersion:
    """按 (年, 序号) 选版本；早于最早版则抛错（**不静默降级**）。"""
    for v in reversed(DLT_VERSIONS):
        if key >= v.from_key:
            return v
    raise RuleVersionError(
        f"大乐透期号 {key} 早于最早规则版本 {DLT_VERSIONS[0].version}（{DLT_VERSIONS[0].from_date}）")


def version_for_issue(issue) -> DltVersion:
    return version_for_key(issue_key("大乐透", issue))


def version_by_name(name: str) -> DltVersion:
    for v in DLT_VERSIONS:
        if v.version == name:
            return v
    raise KeyError(f"未知的大乐透规则版本: {name}")


def classify_dlt(h: int, j: int, version: Optional[str] = None) -> Optional[str]:
    """(前区命中, 后区命中) → 奖级；未中返回 None。version=None 走现行版本。"""
    v = version_by_name(version) if version else DLT_VERSIONS[-1]
    return v.grade_table.get((int(h), int(j)))


def fixed_amount(grade: str, version: Optional[str] = None,
                 uplift: bool = False) -> Optional[float]:
    v = version_by_name(version) if version else DLT_VERSIONS[-1]
    return v.fixed_amount(grade, uplift)


def uplift_applies(version: Optional[str], pool_before: Optional[float]) -> bool:
    """奖池（**上一期开奖后余额**）是否触发固定奖上浮。仅 2026 版有该机制。"""
    v = version_by_name(version) if version else DLT_VERSIONS[-1]
    if not v.uplift or not v.uplift_pool or pool_before is None:
        return False
    return float(pool_before) >= float(v.uplift_pool)
