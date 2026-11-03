# -*- coding: utf-8 -*-
"""ev/profit/rules.py —— 彩种奖金规则表与「命中格 → 奖级」映射（**版本化**）。

设计要点
--------
1. **统一结构**：六个彩种都能写成同一个二元格 `(外层命中数 h, 内层命中类 j)`：
       · 双色球   外层=红球(33选6)，内层=蓝球(16)
       · 大乐透   外层=前区(35选5)，内层=后区命中数 j∈{0,1,2}
       · 七星彩   外层=前 6 位，    内层=第 7 位是否命中（第 7 位 0-14，共 15 值）
       · 排列5/3/D 外层=全部位数，  内层=空（单一奖级）
   于是「枚举 + 命中直方图」的算法对六彩种**同一套代码**，只是格表不同。

2. **大乐透有五个规则版本**（见 `data/prize_versions.py`），`rules_for(lottery, issue)`
   按期号选版本；`issue=None` 一律返回**现行版本**，因此现有「只传彩种」的调用点零改动。

3. `grade_probabilities` 等下游必须用 `ctx.rules.rule_version` 取对应版本，
   否则 2026 段会拿九级概率算七级奖（EV 偏约 0.1 元/注）。
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

from config import ticket_price
from data.prize_versions import (
    DLT_VERSIONS,
    DltVersion,
    RuleVersionError,
    version_for_issue,
)

# 单注成本（元）。大乐透若买追加投注则为 3 元，本模型只做基本投注。
# ★ 真源 config.ticket_price；此前的裸 2.0 是一份独立拷贝（2026-10-30 收口）。
COST = float(ticket_price("双色球"))

# 大乐透追加投注反解占比 q（由票量校准因子 1.1324 反推：均价 = 2×1.1324 = 2.2648，
# 而均价 = 2×(1−q) + 3×q ⇒ q = 0.2648）。实际票量 N = 销售额 / 均价。
DLT_ADDON_SHARE = 0.2648
DLT_AVG_PRICE = 2.0 * (1 - DLT_ADDON_SHARE) + 3.0 * DLT_ADDON_SHARE

# 双色球票量校准因子实测 1.0041（CI 含 1）⇒ N = 销售额 / 2 成立
SSQ_TICKET_FACTOR = 1.0041

CAP_DEFAULT = 10_000_000.0


@dataclass(frozen=True)
class GradeRule:
    """单个奖级的规则。

    `kind` 三值（2026-11-01 因快乐8 接入而扩展）：
      - "fixed"     ：金额恒定（双/大/七星彩/3D/排列3/排列5 的固定档，七乐彩四~七等）
      - "floating"  ：奖池按注均分（双/大/七星彩的一二等），需 float_pool 建模
      - "regulated" ：**名义值由规则规定、单注实际金额按期由官方公布**，
                      已含分摊/封顶/限赔/派奖（七乐彩一~三等、快乐8 全 39 档）。
                      不参与浮动池建模——官方已经算好，再套分薄模型只是引入误差。
    """
    name: str
    kind: str                     # "fixed" | "floating" | "regulated"
    fixed: Optional[float] = None  # fixed 用
    cap: Optional[float] = None    # 单注封顶（floating / regulated 才有）
    prize_level: Optional[int] = None
    risk_control: bool = False
    nominal: Optional[float] = None  # regulated 用的名义值（读不到当期实测时回退）
    floor: Optional[float] = None    # regulated 的限赔保底


@dataclass(frozen=True)
class LotteryRules:
    name: str
    cost: float
    outer_label: str
    outer_width: int
    outer_range: Tuple[int, int]
    outer_choose: bool
    inner_size: int
    inner_multiplicity: Dict[int, int]
    grade_table: Dict[Tuple[int, int], str]
    grades: Dict[str, GradeRule]
    rule_version: str
    effective_from: str
    source: str
    avg_ticket_price: float = COST
    until_issue: Optional[str] = None      # ★ 带默认值 ⇒ 不破坏现有构造
    # ---- 2026-11-01 快乐8 接入新增（全部带默认值，既有 6 彩种零改动）----
    model: str = "binary_cell"             # "binary_cell"（二元命中格）| "hypergeom"（一维超几何）
    draw_size: Optional[int] = None        # 开奖摇出数；None ⇒ 等于 outer_width
    play: str = ""                         # 玩法名（快乐8 选一~选十）；非玩法彩种为 ""

    @property
    def draw_count(self) -> int:
        return int(self.outer_width) if self.draw_size is None else int(self.draw_size)

    @property
    def outer_size(self) -> int:
        lo, hi = self.outer_range
        n = hi - lo + 1
        if self.outer_choose:
            return math.comb(n, self.outer_width)
        return n ** self.outer_width

    @property
    def outcome_size(self) -> int:
        # ★ hypergeom 不许给"结局数"：快乐8 选十的 C(80,10)=1.65e12，
        #   任何下游枚举/建表都会 OOM 或挂死。宁可显式失败，也不要一个看着合法的巨数。
        if self.model != "binary_cell":
            raise NotImplementedError(
                f"{self.name}{('/' + self.play) if self.play else ''} 是 {self.model} 模型，"
                "没有可枚举的结局数（请用概率函数，不要枚举）")
        return self.outer_size * self.inner_size

    def grade_of(self, h: int, j: int) -> Optional[str]:
        return self.grade_table.get((h, j))

    def floating(self) -> Tuple[str, ...]:
        return tuple(g for g, r in self.grades.items() if r.kind == "floating")

    def regulated(self) -> Tuple[str, ...]:
        return tuple(g for g, r in self.grades.items() if r.kind == "regulated")


# ---------------------------------------------------------------- 非大乐透的格表

_SSQ_TABLE = {
    (6, 1): "一等", (6, 0): "二等",
    (5, 1): "三等", (5, 0): "四等", (4, 1): "四等",
    (4, 0): "五等", (3, 1): "五等",
    (2, 1): "六等", (1, 1): "六等", (0, 1): "六等",
}


def _qxc_table() -> Dict[Tuple[int, int], str]:
    """七星彩：X=前6位命中数、Y=第7位是否命中（照 data.feedback._qxc_prize）。"""
    t: Dict[Tuple[int, int], str] = {}
    for x in range(0, 7):
        for y in (0, 1):
            if x + y == 7:
                g = "一等"
            elif x == 6 and y == 0:
                g = "二等"
            elif x == 5 and y == 1:
                g = "三等"
            elif x + y == 5:
                g = "四等"
            elif x + y == 4:
                g = "五等"
            elif x + y >= 3 or y == 1:
                g = "六等"
            else:
                continue
            t[(x, y)] = g
    return t


def _build_grades(name: str) -> Dict[str, GradeRule]:
    """非大乐透彩种：由 prize_table 的固定/浮动/受约束奖表构造（**惰性导入**避免循环依赖）。"""
    from data.prize_table import (
        FIXED_PAYOUT,
        FLOATING_REF,
        REGULATED_CAP,
        REGULATED_FLOOR,
        REGULATED_NOMINAL,
    )

    out: Dict[str, GradeRule] = {}
    level_map = {
        "双色球": {"一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6},
        "七星彩": {"一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6},
        "七乐彩": {"一等": 1, "二等": 2, "三等": 3, "四等": 4, "五等": 5, "六等": 6, "七等": 7},
        "排列3": {"直选": 1}, "福彩3D": {"直选": 1}, "排列5": {"直选": 1},
    }.get(name, {})
    for g, amt in FIXED_PAYOUT.get(name, {}).items():
        out[g] = GradeRule(name=g, kind="fixed", fixed=float(amt),
                           prize_level=level_map.get(g))
    # regulated：名义值 + 可选封顶/保底（快乐8 无「第N等」，prize_level 为 None）
    caps = REGULATED_CAP.get(name, {})
    floors = REGULATED_FLOOR.get(name, {})
    for g, nom in REGULATED_NOMINAL.get(name, {}).items():
        out[g] = GradeRule(name=g, kind="regulated", nominal=float(nom),
                           cap=float(caps[g]) if g in caps else None,
                           floor=float(floors[g]) if g in floors else None,
                           prize_level=level_map.get(g))
    for g, cap in FLOATING_REF.get(name, {}).items():
        out[g] = GradeRule(name=g, kind="floating",
                           cap=float(cap) if cap else None,
                           prize_level=level_map.get(g))
    if name == "排列5" and "直选" in out:
        r = out["直选"]
        out["直选"] = GradeRule(name="直选", kind="fixed", fixed=r.fixed,
                                prize_level=1, risk_control=True)
    return out


def _grades_dlt(v: DltVersion) -> Dict[str, GradeRule]:
    """大乐透：**完全由版本表生成**（不再手写九级，也不再读 prize_table）。"""
    order = ["一等", "二等", "三等", "四等", "五等", "六等", "七等", "八等", "九等"]
    out: Dict[str, GradeRule] = {}
    for g in sorted(set(v.grade_table.values()), key=order.index):
        if g in v.floating:
            cap = v.first_cap if g == "一等" else None
            out[g] = GradeRule(name=g, kind="floating", cap=cap, prize_level=order.index(g) + 1)
        else:
            out[g] = GradeRule(name=g, kind="fixed", fixed=float(v.fixed[g]),
                               prize_level=order.index(g) + 1)
    return out


def _rules_ssq() -> LotteryRules:
    return LotteryRules(
        name="双色球", cost=COST, outer_label="红球", outer_width=6,
        outer_range=(1, 33), outer_choose=True,
        inner_size=16, inner_multiplicity={1: 1, 0: 15},
        grade_table=dict(_SSQ_TABLE), grades=_build_grades("双色球"),
        rule_version="ssq-2026-02", effective_from="2026-02-01",
        source="《中国福利彩票双色球游戏规则》（本模型只建六大奖级）",
        avg_ticket_price=2.0,
    )


def _rules_dlt(v: DltVersion) -> LotteryRules:
    return LotteryRules(
        name="大乐透", cost=COST, outer_label="前区", outer_width=5,
        outer_range=(1, 35), outer_choose=True,
        inner_size=66, inner_multiplicity={2: 1, 1: 20, 0: 45},
        grade_table=dict(v.grade_table), grades=_grades_dlt(v),
        rule_version=v.version, effective_from=v.from_date,
        source=v.source, avg_ticket_price=DLT_AVG_PRICE,
    )


def _rules_qxc() -> LotteryRules:
    return LotteryRules(
        name="七星彩", cost=COST, outer_label="前6位", outer_width=6,
        outer_range=(0, 9), outer_choose=False,
        inner_size=15, inner_multiplicity={1: 1, 0: 14},
        grade_table=_qxc_table(), grades=_build_grades("七星彩"),
        rule_version="qxc-2020", effective_from="2020-10-11",
        source="《七星彩游戏规则》2020 升级版（任意位置匹配位数，不要求连续）",
        avg_ticket_price=2.0,
    )


def _rules_digit(name: str, width: int) -> LotteryRules:
    return LotteryRules(
        name=name, cost=COST, outer_label="全部位数", outer_width=width,
        outer_range=(0, 9), outer_choose=False,
        inner_size=1, inner_multiplicity={0: 1},
        grade_table={(width, 0): "直选"}, grades=_build_grades(name),
        rule_version=f"{name}-fixed", effective_from="2004-01-01",
        source="固定赔率玩法（直选），不涉及奖池与分摊",
        avg_ticket_price=2.0,
    )


# ---------------------------------------------------------------- 七乐彩
# 30 选 7 基本号 + 从**剩余 23 个**里摇 1 个特别号。七档全是 (h, j) 二元格：
#   h = 票面 7 个号里命中基本号的个数，j = 票面是否含特别号。
#   注意 (7,1) 不可能发生（7 个号全是基本号时票里没有"剩余号"），故不入表。
_QLC_TABLE: Dict[Tuple[int, int], str] = {
    (7, 0): "一等", (6, 1): "二等", (6, 0): "三等",
    (5, 1): "四等", (5, 0): "五等",
    (4, 1): "六等", (4, 0): "七等",
}


def _rules_qlc() -> LotteryRules:
    return LotteryRules(
        name="七乐彩", cost=COST, outer_label="基本号", outer_width=7,
        outer_range=(1, 30), outer_choose=True,
        inner_size=23, inner_multiplicity={1: 1, 0: 22},
        grade_table=dict(_QLC_TABLE), grades=_build_grades("七乐彩"),
        rule_version="qlc-2004", effective_from="2004-02-15",
        source="《七乐彩游戏规则》（30 选 7 基本号 + 从剩余 23 个摇 1 个特别号）",
        avg_ticket_price=2.0,
    )


# ---------------------------------------------------------------- 快乐8
# 80 选 20 开出；投注侧「选一」~「选十」共 10 种玩法，各自一张奖表（共 39 档）。
# ★ 模型不是二元命中格，而是**一维超几何**：命中数 k 直接决定奖级。
#   用 LotteryRules 承载只是为了复用 `grades`/`rule_version`；概率必须走
#   ev/payout._probs_kl8 的超几何式，绝不能走 _probs_from_grade_table
#   （那个通用式的分母是 C(n,w)，而快乐8 的分母是 C(80,票面数)，且开奖摇 20 个）。
KL8_PLAYS: Dict[str, int] = {
    "选一": 1, "选二": 2, "选三": 3, "选四": 4, "选五": 5,
    "选六": 6, "选七": 7, "选八": 8, "选九": 9, "选十": 10,
}

# 玩法 → 有奖的命中数（降序）。合计 39 档，与 market.db 的 level_name 集合一一对应。
_KL8_WIN: Dict[str, Tuple[int, ...]] = {
    "选十": (10, 9, 8, 7, 6, 5, 0),
    "选九": (9, 8, 7, 6, 5, 4, 0),
    "选八": (8, 7, 6, 5, 4, 0),
    "选七": (7, 6, 5, 4, 0),
    "选六": (6, 5, 4, 3),
    "选五": (5, 4, 3),
    "选四": (4, 3, 2),
    "选三": (3, 2),
    "选二": (2,),
    "选一": (1,),
}

_CN_NUM = {0: "零", 1: "一", 2: "二", 3: "三", 4: "四",
           5: "五", 6: "六", 7: "七", 8: "八", 9: "九", 10: "十"}


def kl8_grade_name(play: str, k: int) -> str:
    """「选十中十」式奖级名。单一生成点，别处不许手拼。"""
    return f"{play}中{_CN_NUM[int(k)]}"


def kl8_play_of_width(width: int) -> str:
    """票面选号数 → 玩法名（快乐8 的 10 个玩法与票面数 1:1，故可反解）。

    ★ 为什么需要它：pending/feedback 里只存了号码，没存玩法。结算时必须把
      「这注是选几」反推出来，否则 10 注的票会被当成「选十」以外的玩法，
      或者干脆按「命中 6 个算几等奖」去猜 —— 猜错会静默判错奖级。
      1:1 是规则本身的性质（选一选 1 个、选十选 10 个），不是我们的约定，
      所以这里可以安全反解；一旦哪天出现两个玩法同宽度，这个函数会直接报错。
    """
    w = int(width)
    for p, n in KL8_PLAYS.items():
        if int(n) == w:
            return p
    raise KeyError(f"快乐8 无票面宽度 {w} 的玩法（可选：{sorted(KL8_PLAYS.values())}）")


def _rules_kl8(play: str) -> LotteryRules:
    n = KL8_PLAYS[play]
    table = {(k, 0): kl8_grade_name(play, k) for k in _KL8_WIN[play]}
    all_grades = _build_grades("快乐8")           # 39 档的名义值/封顶全在这里
    mine = {g: r for g, r in all_grades.items() if g.startswith(f"{play}中")}
    missing = set(table.values()) - set(mine)
    assert not missing, f"快乐8 {play} 缺名义金额：{missing}"
    return LotteryRules(
        name="快乐8", cost=COST, outer_label="基本号", outer_width=n,
        outer_range=(1, 80), outer_choose=True,
        inner_size=1, inner_multiplicity={0: 1},
        grade_table=table, grades=mine,
        rule_version=f"kl8-x{n}-2020", effective_from="2020-10-28",
        source="《快乐8游戏规则》2020 版（80 选 20 开出，投注侧选一~选十）",
        avg_ticket_price=2.0,
        model="hypergeom", draw_size=20, play=play,
    )


# 彩种 → 玩法元组。无玩法概念的彩种为 ("",) —— 用空串而不是 None 作键，
# 是因为 dict 键要可哈希且稳定，且 logs/展示里 "" 明确表示"该彩种没有玩法维度"。
PLAYS: Dict[str, Tuple[str, ...]] = {
    "双色球": ("",), "大乐透": ("",), "七星彩": ("",),
    "排列3": ("",), "福彩3D": ("",), "排列5": ("",),
    "七乐彩": ("",),
    "快乐8": tuple(KL8_PLAYS),
}

# ★ 2026-11-01：键从「彩种」改为「(彩种, 玩法)」。对外签名只做**关键字增参**，
#   位置参数语义不变 ⇒ 现有 6 个彩种的调用点零改动。
_RULES: Dict[Tuple[str, str], Tuple[LotteryRules, ...]] = {
    ("双色球", ""): (_rules_ssq(),),
    ("大乐透", ""): tuple(_rules_dlt(v) for v in DLT_VERSIONS),
    ("七星彩", ""): (_rules_qxc(),),
    ("排列3", ""): (_rules_digit("排列3", 3),),
    ("福彩3D", ""): (_rules_digit("福彩3D", 3),),
    ("排列5", ""): (_rules_digit("排列5", 5),),
    ("七乐彩", ""): (_rules_qlc(),),
    **{("快乐8", p): (_rules_kl8(p),) for p in KL8_PLAYS},
}

# 大乐透版本 → 起始 (年, 序号)
_DLT_SERIES: Tuple[Tuple[Tuple[int, int], LotteryRules], ...] = tuple(
    (v.from_key, r) for v, r in zip(DLT_VERSIONS, _RULES[("大乐透", "")])
)

# ★ 版本只改奖级，不改几何：枚举缓存（outcomes.outer_universe 的 lru_cache(key=lottery)）因此安全
assert len({r.outer_size for r in _RULES[("大乐透", "")]}) == 1

# 彩种级集合（保持既有顺序与内容；快乐8 的 10 个玩法不算 10 个彩种）
SUPPORTED = tuple(dict.fromkeys(lot for lot, _ in _RULES))


def _play_of(lottery: str, play: Any = None) -> str:
    """解析玩法：None ⇒ 该彩种的默认（快乐8=选十，其余=""）。"""
    plays = PLAYS.get(lottery)
    if plays is None:
        raise KeyError(f"未知彩种：{lottery}")
    if play is None or play == "":
        return plays[-1] if len(plays) > 1 else plays[0]
    if play not in plays:
        raise KeyError(f"{lottery} 无此玩法：{play}（可选：{'/'.join(p for p in plays if p)}）")
    return play


def rule_versions(lottery: str, *, play: Any = None) -> Tuple[str, ...]:
    return tuple(r.rule_version for r in _RULES[(lottery, _play_of(lottery, play))])


# 「X等」奖级的级数序（七星彩用「X等奖」另一套命名，不在此表）
_GRADE_ORDER = ("一等", "二等", "三等", "四等", "五等", "六等", "七等", "八等", "九等")


def grade_rank_names() -> Tuple[str, ...]:
    """「X等」奖级名全集（按级数升序）—— 供展示层排序/筛选权重使用。

    大乐透历史上有 6/7/8/9 级四种级数（双色球/七星彩 6 级，七乐彩 7 级），展示层不再手写
    「一~九等」副本：从本表取，缺级天然不存在（`.get(x, 99)` 语义不变）。
    数字型的「直选/组选3/组选6」与快乐8 的「选十中十」不在此表（不以「等」结尾）。
    """
    names = {g for vs in _RULES.values() for r in vs for g in r.grades
             if g.endswith("等")}
    assert names <= set(_GRADE_ORDER), f"未知奖级名: {names - set(_GRADE_ORDER)}"
    return tuple(sorted(names, key=_GRADE_ORDER.index))


def rules_for(lottery: str, issue: Any = None, *, play: Any = None) -> LotteryRules:
    """取彩种规则。

    `issue=None` ⇒ **现行版本**（决策路径，现有调用点行为不变）。
    给定 `issue` ⇒ 按 (年, 序号) 从新到旧取第一个 `key >= from_key`；
    早于最早版本抛 `RuleVersionError`，**不静默降级**。

    `play`：快乐8 的玩法（选一~选十），None ⇒ 默认选十。其余彩种忽略。
    ★ issue 的版本循环**只在单一玩法内**进行：若跨玩法取值，会拿「选十」的规则
      去结算「选五」的票 —— 两者奖级表完全不同，而且结果不会报错。
    """
    pid = _play_of(lottery, play)
    vs = _RULES[(lottery, pid)]
    if issue is None:
        return vs[-1]
    from data.issue_key import issue_key

    key = issue_key(lottery, issue)
    for r in reversed(vs):
        if key >= _series_key(lottery, r.rule_version):
            return r
    raise RuleVersionError(
        f"{lottery} 期号 {issue} 早于最早规则版本 {rule_versions(lottery, play=pid)[0]}")


def rules_by_version(lottery: str, version: str, *, play: Any = None) -> LotteryRules:
    """按版本名取规则（供 payout.grade_probabilities 用）。未知版本抛 KeyError。

    版本名本身带玩法命名空间（kl8-x10-2020），所以先在各玩法里找同名版本。
    """
    vs = _RULES[(lottery, _play_of(lottery, play))]
    for r in vs:
        if r.rule_version == version:
            return r
    # 版本名跨玩法（如仅给 "kl8-x10-2020" 而 lottery=快乐8）时，全局兜一层
    for (lot, _p), vs2 in _RULES.items():
        if lot != lottery:
            continue
        for r in vs2:
            if r.rule_version == version:
                return r
    raise KeyError(f"{lottery} 没有规则版本 {version}（有 {rule_versions(lottery, play=play)}）")


def _series_key(lottery: str, version: str) -> Tuple[int, int]:
    if lottery == "大乐透":
        for k, r in _DLT_SERIES:
            if r.rule_version == version:
                return k
    return (0, 0)


def grade_of_hits(lottery: str, hits: int, special: int = 0, *,
                  play: Any = None, issue: Any = None) -> Optional[str]:
    """按命中数判奖级的**单一真源**。

    只接管二元格之外的两种新形态，既有 6 彩种一律 raise —— 它们的判级散落在
    data/feedback._classify_prize / _qxc_prize 等处，本次不动，避免回归。

    - 七乐彩：hits = 命中基本号个数，special = 票面是否含特别号（0/1）
    - 快乐8：hits = 命中开奖号个数（0~票面数），special 忽略
    """
    if lottery == "七乐彩":
        return rules_for(lottery, issue).grade_table.get((int(hits), int(special)))
    if lottery == "快乐8":
        return rules_for(lottery, issue, play=play).grade_table.get((int(hits), 0))
    raise KeyError(f"grade_of_hits 不接管 {lottery}（其判级在 data/feedback 的专用函数里）")


def outcome_size(lottery: str, *, play: Any = None) -> int:
    return rules_for(lottery, play=play).outcome_size


def avg_ticket_price(lottery: str, *, play: Any = None) -> float:
    return rules_for(lottery, play=play).avg_ticket_price


__all__ = [
    "CAP_DEFAULT", "COST", "DLT_ADDON_SHARE", "DLT_AVG_PRICE", "GradeRule",
    "KL8_PLAYS", "LotteryRules", "PLAYS", "RuleVersionError", "SUPPORTED",
    "avg_ticket_price", "grade_of_hits", "grade_rank_names", "kl8_grade_name",
    "kl8_play_of_width", "outcome_size", "rule_versions", "rules_by_version", "rules_for",
]
