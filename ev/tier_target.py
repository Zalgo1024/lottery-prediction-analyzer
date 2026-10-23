# -*- coding: utf-8 -*-
"""ev/tier_target.py —— 奖级达标目标（Target）：反查条件集 + 覆盖通道键。

背景
----
全仓此前只有「按 (h, j) 判到**具体**奖级」，**没有**「奖级 → 命中条件集合」的反查；
`ev/lowcrowd_select.keys_of` 又是为「≥10 元」这一档硬编码的 35 个通道键。
本模块把两者都泛化。`keys_of` 保持原样不动（它有 6 个回归测试锁着）。

三个概念必须分清
----------------
1. **Target**（兑奖线），两种写法：:

       {"kind": "grade",  "grade": "四等"}      # 中到「四等及以上」
       {"kind": "payout", "min_payout": 10.0}   # 单注实得 ≥ 10 元

2. **条件集 conditions**：`[(h, j), ...]`，`(h,j)` 就是 `rules.grade_table` 的格。
   ★ 该集合对同一个 j **在 h 上向上封闭**（h 越大奖级越靠前），构造时会断言。

3. **通道 channel**：一个「子事件」，其全部结局都被同一张票收下（供覆盖贪心用）。

       AGG (h, S)      「票面外层的 h-子集 S 全部出现在开奖里」（内层无所谓）
       SPEC(h, S, c)   同上，且**开奖的内层坐标恰为 c**

   ★ 每个通道都是「≥target」事件的子集；一张票的通道**并集**恰等于它的「≥target」事件
     （已由 tests 用暴力枚举验证）。
   ★ 但通道之间会**重叠**（h 大的通道被 h 小的包住，跨票也会重叠），所以
     「Σ通道权重」是 P(并集) 的**上界**而不是等式（见 `UPPER_BOUND_NOTE`）。
     真实数字一律由实测回测 / `exact_metrics(min_grade=...)` 给出。

前视口径（红线）
----------------
`kind="payout"` 只用**规则表**的固定奖金 / 浮动下界判定，**绝不**读当期实际派奖；
`grade_of_ticket` 只判级、不算钱。否则选号会看到未来。
"""
from __future__ import annotations

import math
from itertools import combinations
from typing import Any, Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

from ev.profit.rules import _GRADE_ORDER, LotteryRules, rules_for

# 双色球/大乐透的票面字段（与 ev/coverage_backtest.grade_ticket 完全一致：
# 大乐透的「后区 2 个」也放在 "蓝球" 里，是个 list）
RZ, BZ = "红球", "蓝球"

DEFAULT_TARGET: Dict[str, Any] = {"kind": "grade", "grade": "四等"}

# 浮动奖（一等/二等）的**单注金额保守下界**：任何彩种、任何期都不会低于此值。
# 用它而不是当期实际值 ⇒ 无前视。
_FLOATING_FLOOR = 1000.0

UPPER_BOUND_NOTE = (
    "通道权重和是 P(至少一注达标) 的**上界**：通道的子事件互相重叠"
    "（同一张票里 h 大的通道被 h 小的包住，跨票也会重叠），"
    "故它按包含-排斥的高阶项系统性高估。判定一律以逐期实测为准。"
)


# ---------------------------------------------------------------- Target 解析
def normalize_target(target: Any = None) -> Dict[str, Any]:
    """把多种写法统一成 `{"kind": ...}` 字典。

    None → 默认（四等）; "四等"/"四等奖" → grade; 10 / "10元" / 10.0 → payout。
    """
    if target is None:
        return dict(DEFAULT_TARGET)
    if isinstance(target, dict):
        t = dict(target)
        t.setdefault("kind", "grade")
        return t
    if isinstance(target, (int, float)) and not isinstance(target, bool):
        return {"kind": "payout", "min_payout": float(target)}
    if isinstance(target, str):
        s = target.strip()
        if s.endswith("元"):
            return {"kind": "payout", "min_payout": float(s[:-1])}
        try:
            return {"kind": "payout", "min_payout": float(s)}
        except ValueError:
            pass
        if s.endswith("奖"):
            s = s[:-1]
        return {"kind": "grade", "grade": s}
    raise ValueError(f"无法识别的 target: {target!r}")


def label_of(target: Any = None) -> str:
    t = normalize_target(target)
    if t["kind"] == "payout":
        v = float(t["min_payout"])
        return f"单注≥{v:g}元"
    return f"≥{t['grade']}"


def grade_payout_floor(lottery: str, grade: str, issue: Any = None) -> float:
    """该奖级的**单注金额保守下界**（仅用于「是否 ≥ 某金额线」的判定）。

    固定奖 → 规则表确定值；浮动奖 → `_FLOATING_FLOOR`（不读当期实际派奖，避免前视）。
    """
    r = rules_for(lottery, issue)
    rule = r.grades.get(grade)
    if rule is None:
        return 0.0
    if rule.kind == "fixed":
        return float(rule.fixed or 0.0)
    return float(_FLOATING_FLOOR)


def _rank_of(lottery: str, grade: str, issue: Any = None) -> Optional[int]:
    """奖级排名（0 = 一等）。数字型的「直选」不在 `_GRADE_ORDER` 里 → None。"""
    if grade not in _GRADE_ORDER:
        return None
    return _GRADE_ORDER.index(grade)


def _achievable_j(rules: LotteryRules) -> Set[int]:
    """内层**可能出现的全部**类（`inner_multiplicity` 的键）。

    ★ 判「能不能用 AGG」必须拿这个全集比，**不能**拿 `grade_table` 里出现过的 j 比：
      双色球 h=3 只有 (3,1) 在表里，但 (3,0) 是**能开出来的未中格**；
      若按「表里出现过的 j」判 AGG，会把 3红+0蓝 的结局错算成达标。
    """
    return set(rules.inner_multiplicity)


def resolve_conditions(lottery: str, target: Any = None,
                       issue: Any = None) -> Dict[str, Any]:
    """Target → 命中条件集 / 达标奖级集。

    返回::

        {"kind", "label", "issue", "rule_version",
         "conditions": [(h, j), ...]        # 升序，供 coverage_keys_of 用
         "condition_set": frozenset(...)    # O(1) 判定
         "grades": frozenset({...}),        # 达标的奖级名（"未中" 永不在内）
         "note": str}
    """
    t = normalize_target(target)
    rules = rules_for(lottery, issue)
    gt = rules.grade_table

    if t["kind"] == "grade":
        g = t["grade"]
        if g not in rules.grades:
            raise ValueError(f"{lottery}（{rules.rule_version}）没有奖级 {g!r}；"
                             f"可选 {sorted(rules.grades, key=lambda x: _GRADE_ORDER.index(x) if x in _GRADE_ORDER else 99)}")
        rk = _rank_of(lottery, g, issue)
        if rk is None:
            if g not in set(gt.values()):
                raise ValueError(
                    f"{lottery} 的奖级 {g!r} 在规则表 (h,j) 格里没有可表达的条件"
                    f"（如数字型的组选玩法），无法做覆盖通道分解")
            grades = {g}
            note = f"{lottery} 单奖级玩法：命中即达标（{g}）"
        else:
            grades = {gg for gg in rules.grades
                      if _rank_of(lottery, gg, issue) is not None
                      and _rank_of(lottery, gg, issue) <= rk}
            note = f"按规则版本 {rules.rule_version} 的奖级序取「{g}及以上」"
    elif t["kind"] == "payout":
        line = float(t["min_payout"])
        # ★ 只保留**在 grade_table 里出现过**的奖级：数字型的「组选3/组选6」也在这张
        #   固定奖金表里，但规则表的 (h,j) 格只建了「直选」⇒ 若不过滤，grades 会包含
        #   条件集表达不出来的奖级，导致 conditions 与 grades 两张口径不一致。
        real = set(gt.values())
        grades = {gg for gg in rules.grades
                  if gg in real and grade_payout_floor(lottery, gg, issue) >= line}
        note = (f"金额线 {line:g} 元按**规则表**金额映射到奖级（浮动奖用保守下界 "
                f"{_FLOATING_FLOOR:g} 元/注）；派奖期的实际金额可能更高，故本映射偏保守")
    else:
        raise ValueError(f"未知 target.kind: {t['kind']!r}")

    conds = sorted((h, j) for (h, j), gg in gt.items() if gg in grades)

    # ★ 向上封闭断言：同一 j 下，h 更大的格若存在就必须同样达标（否则通道分解会漏结局）
    for (h, j) in conds:
        for h2 in range(h + 1, rules.outer_width + 1):
            g2 = gt.get((h2, j))
            if g2 is not None and g2 not in grades:
                raise AssertionError(
                    f"{lottery} 条件集在 h 上不向上封闭：({h},{j})→达标 但 ({h2},{j})→{g2} 不达标")

    return {
        "kind": t["kind"],
        "label": label_of(t),
        "issue": None if issue is None else str(issue),
        "rule_version": rules.rule_version,
        "conditions": conds,
        "condition_set": frozenset(conds),
        "grades": frozenset(grades),
        "note": note,
    }


def grade_conditions_at_or_above(lottery: str, grade: str,
                                 issue: Any = None) -> List[Tuple[int, int]]:
    """所有 `(h,j)` 使奖级排名 ≤ `grade` 的格。"""
    return resolve_conditions(lottery, {"kind": "grade", "grade": grade},
                              issue)["conditions"]


def conditions_for_payout(lottery: str, min_payout: float,
                          issue: Any = None) -> List[Tuple[int, int]]:
    """所有单注金额下界 ≥ `min_payout` 的格。"""
    return resolve_conditions(lottery, {"kind": "payout", "min_payout": min_payout},
                              issue)["conditions"]


# ---------------------------------------------------------------- 判级（不算钱）
def grade_of_ticket(lottery: str, zone_numbers: Dict[str, Any],
                    draw_zones: Dict[str, Any], issue: Any = None) -> str:
    """单注对单期的**奖级**（"未中" 表示不中）。只判级、**不算钱** ⇒ 无前视。

    与 `ev/coverage_backtest.grade_ticket` 的判级部分同源（都用 data/feedback 的
    单一真源）；一致性由 `tests/test_tier_target.py` 逐期对账锁住。
    `zone_numbers` 是票面号码 dict（双色/大乐透用 "红球"/"蓝球"，其他用 "号码"）。
    """
    if lottery in ("双色球", "大乐透"):
        from data.feedback import _classify_dlt_prize, _classify_prize

        rh = len(set(zone_numbers.get(RZ) or []) & set((draw_zones or {}).get(RZ) or []))
        bb = len(set(zone_numbers.get(BZ) or []) & set((draw_zones or {}).get(BZ) or []))
        return (_classify_dlt_prize(rh, bb, issue) if lottery == "大乐透"
                else _classify_prize(rh, bb))

    if lottery == "七星彩":
        from data.feedback import _qxc_prize

        pred = zone_numbers.get("号码") or []
        actual = [x for z in (draw_zones or {}).values() for x in (z or [])]
        zh = {f"第{i + 1}位": (1 if int(pred[i]) == int(actual[i]) else 0)
              for i in range(min(7, len(pred), len(actual)))}
        tier, _fixed = _qxc_prize({"分区命中": zh})
        return "未中" if tier is None else tier.replace("奖", "")

    from data.feedback import _settle_pl

    nums = zone_numbers.get("号码") or []
    fb = {"预测号码": {f"第{i + 1}位": [int(x)] for i, x in enumerate(nums)},
          "实际号码": draw_zones or {}, "期号": issue}
    _pl, _mode, level = _settle_pl(lottery, fb)
    return "未中" if level is None else level


def target_is_hit(lottery: str, zone_numbers: Dict[str, Any], draw_zones: Dict[str, Any],
                  issue: Any = None, target: Any = None) -> bool:
    """该注对该期是否达到 ≥target。

    `target` 也可以直接传 `resolve_conditions(...)` 的结果（回测里逐注复用，省重算）。
    """
    if isinstance(target, dict) and "grades" in target:
        grades = target["grades"]
    else:
        grades = resolve_conditions(lottery, target, issue)["grades"]
    g = grade_of_ticket(lottery, zone_numbers, draw_zones, issue)
    return g != "未中" and g in grades


# ---------------------------------------------------------------- 通道键
class _Space:
    """一个彩种的通道编码空间（每种彩种/几何只建一次）。"""

    __slots__ = ("lottery", "choose", "n", "k", "ncoord", "base", "offsets")

    def __init__(self, lottery: str, rules: LotteryRules):
        self.lottery = lottery
        self.choose = bool(rules.outer_choose)
        self.n = rules.outer_range[1] - rules.outer_range[0] + 1
        self.k = rules.outer_width
        self.ncoord = int(rules.inner_size)
        # 子集编码的基数：choose 型按号码值（0..n），非 choose 型按「位置*10+值」
        self.base = self.n + 1 if self.choose else 10 * self.k + 1
        # 每档 h 的偏移：保证不同 h 的键不撞
        off, self.offsets = 0, {}
        for h in range(0, rules.outer_width + 1):
            self.offsets[h] = off
            off += self.base ** h * (self.ncoord + 1)

    def key(self, h: int, code: int, coord: Optional[int]) -> int:
        """coord=None ⇒ AGG（内层无所谓）；否则 SPEC（内层坐标为 coord）。"""
        return self.offsets[h] + code * (self.ncoord + 1) + (0 if coord is None else int(coord) + 1)


_SPACES: Dict[Tuple[str, str], _Space] = {}


def _space(lottery: str, issue: Any = None) -> _Space:
    rules = rules_for(lottery, issue)
    k = (lottery, rules.rule_version)
    sp = _SPACES.get(k)
    if sp is None:
        sp = _SPACES[k] = _Space(lottery, rules)
    return sp


def _inner_of(ticket: Dict[str, Any], lottery: str) -> Any:
    """票面的内层坐标：双色球 = 蓝球值；大乐透 = 后区 2 元组；七星彩 = 第 7 位；数字型 = 0。"""
    if lottery == "双色球":
        b = ticket.get(BZ)
        b = b[0] if isinstance(b, (list, tuple)) else b
        return int(b or 0)
    if lottery == "大乐透":
        b = ticket.get(BZ) or []
        return tuple(sorted(int(x) for x in b))
    if lottery == "七星彩":
        nums = ticket.get("号码") or []
        return int(nums[6]) if len(nums) > 6 else 0
    return 0


def _draw_coords(sp: _Space, lottery: str, issue: Any = None) -> List[int]:
    """开奖内层坐标的全集（与 `_inner_class` 的入参一致）。"""
    if lottery == "双色球":
        return list(range(1, 17))
    if lottery == "大乐透":
        return [(a, b) for a in range(1, 13) for b in range(a + 1, 13)]
    if lottery == "七星彩":
        return list(range(0, 15))
    return [0]


def _inner_class(lottery: str, ticket_inner: Any, draw_coord: Any) -> int:
    """「内层命中类 j」——与 `data.feedback` 的 (h, j) 语义一致。"""
    if lottery == "双色球":
        return 1 if int(ticket_inner) == int(draw_coord) else 0
    if lottery == "大乐透":
        return len(set(ticket_inner) & set(draw_coord))
    if lottery == "七星彩":
        return 1 if int(ticket_inner) == int(draw_coord) else 0
    return 0


def _subsets_of_ticket(sp: _Space, lottery: str, ticket: Dict[str, Any],
                       h: int) -> List[Tuple[int, float]]:
    """该票在 h 档贡献的 `[(子集编码, 子集概率因子)]`。

    概率因子 = P(该子集被开奖完全命中)：
      · 乐透型（choose）: C(n-h, k-h) / C(n, k)
      · 数字型（逐位）  : (1/10)^h
    """
    if sp.choose:
        vals = sorted(int(x) for x in (ticket.get(RZ) or []))
        if len(vals) != sp.k:
            raise ValueError(f"{lottery} 票面外层应有 {sp.k} 个号，实际 {len(vals)}")
        if h > sp.k:
            return []
        w = math.comb(sp.n - h, sp.k - h) / math.comb(sp.n, sp.k)
        out = []
        for c in combinations(vals, h):
            code = 0
            for i, v in enumerate(sorted(c)):
                code += v * (sp.base ** i)
            out.append((code, w))
        return out

    nums = [int(x) for x in (ticket.get("号码") or [])][:sp.k]
    if h > len(nums):
        return []
    w = (0.1) ** h
    out = []
    for idx in combinations(range(len(nums)), h):
        code = 0
        for i, p in enumerate(idx):
            code += (p * 10 + nums[p]) * (sp.base ** i)
        out.append((code, w))
    return out


def coverage_channels_of(ticket: Dict[str, Any], lottery: str,
                         conditions: Sequence[Tuple[int, int]],
                         issue: Any = None) -> List[Tuple[int, float]]:
    """该票覆盖的全部通道 `[(键, 权重)]`，键已去重。

    ★ 若某 h 档的**全部可能内层类**都在条件集里 ⇒ 用 AGG（内层无所谓，键更少）；
      否则按「开奖内层坐标」展开成 SPEC。
    """
    rules = rules_for(lottery, issue)
    sp = _space(lottery, issue)
    by_h: Dict[int, Set[int]] = {}
    for (h, j) in conditions:
        by_h.setdefault(h, set()).add(int(j))

    ticket_inner = _inner_of(ticket, lottery)
    all_j = _achievable_j(rules)
    coords_cache: Optional[List[int]] = None
    out: Dict[int, float] = {}

    for h, js in by_h.items():
        subs = _subsets_of_ticket(sp, lottery, ticket, h)
        if not subs:
            continue
        if js >= all_j:
            # 该 h 档的所有内层类都达标 ⇒ 内层无所谓，用 AGG
            w = _chan_weight(sp, rules, h, specific=False)
            for code, _sw in subs:
                out[sp.key(h, code, None)] = w
        else:
            w = _chan_weight(sp, rules, h, specific=True)
            if coords_cache is None:
                coords_cache = _draw_coords(sp, lottery, issue)
            sel = [_coord_id(sp, lottery, c) for c in coords_cache
                   if _inner_class(lottery, ticket_inner, c) in js]
            for code, _sw in subs:
                for c in sel:
                    out[sp.key(h, code, c)] = w
    return list(out.items())


def coverage_keys_of(ticket: Dict[str, Any], lottery: str,
                     conditions: Sequence[Tuple[int, int]],
                     issue: Any = None) -> List[int]:
    """该票覆盖的全部通道键（去重、升序）。`coverage_channels_of` 的键部分。"""
    return sorted(k for k, _ in coverage_channels_of(ticket, lottery, conditions, issue))


_DPAIR_ID: Dict[Tuple[int, int], int] = {}


def _coord_id(sp: _Space, lottery: str, coord: Any) -> int:
    """把开奖内层坐标压成 0 起的整数 id（槽位 = id+1 ∈ [1, ncoord]）。"""
    if lottery == "双色球":
        return int(coord) - 1                     # 蓝球 1..16 → 0..15
    if lottery == "大乐透":
        a, b = coord
        if not _DPAIR_ID:
            i = 0
            for x in range(1, 13):
                for y in range(x + 1, 13):
                    _DPAIR_ID[(x, y)] = i
                    i += 1
        return _DPAIR_ID[(a, b)]
    return int(coord)


def _chan_weight(sp: "_Space", rules: LotteryRules, h: int, specific: bool) -> float:
    """单通道概率（不走 rules_for，供循环内复用）。"""
    if h > sp.k:
        return 0.0
    if sp.choose:
        w = math.comb(sp.n - h, sp.k - h) / math.comb(sp.n, sp.k)
    else:
        w = (0.1) ** h
    return w if not specific else w / max(1, int(rules.inner_size))


def channel_weight(lottery: str, h: int, specific: bool = False,
                   issue: Any = None) -> float:
    """单个通道的概率权重。

    specific=False（AGG）→ P(该 h-子集全中)
    specific=True （SPEC）→ 上式 × 1/inner_size（开奖内层坐标恰为该值）
    """
    return _chan_weight(_space(lottery, issue), rules_for(lottery, issue), h, specific)


def single_ticket_probability(lottery: str, conditions: Sequence[Tuple[int, int]],
                              issue: Any = None) -> float:
    """P(单注 ≥target) —— 由条件集直接算，与 rules.grade_table 一致。

    这是**解析值**，用于核对与 `ev/profit.evaluate.exact_metrics` 的口径一致性。
    """
    rules = rules_for(lottery, issue)
    sp = _space(lottery, issue)
    tot = 0.0
    for (h, j) in conditions:
        if sp.choose:
            pw = math.comb(sp.k, h) * math.comb(sp.n - sp.k, sp.k - h) / math.comb(sp.n, sp.k)
        else:
            pw = math.comb(sp.k, h) * (0.1) ** h * (0.9) ** (sp.k - h)
        tot += pw * rules.inner_multiplicity[int(j)] / int(rules.inner_size)
    return tot
