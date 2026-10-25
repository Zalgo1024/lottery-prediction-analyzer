# -*- coding: utf-8 -*-
"""ev/arena/periods.py —— 期窗的唯一真源。

为什么单独一个模块
------------------
方案 §1.2 G1：`ev/tier_backtest` 与 `ev/lowcrowd_backtest` 的期窗不同
（双色球 500 期：tier 取到 26121、lowcrowd 对齐后到 26120），尾部差 12 期。
把结果并列比较时，这 12 期会混进「策略差异」里。

本模块把期窗变成**可断言的对象**：
  · `assert_same_periods(a, b)` 要求**逐位相等**；
    不允许求交集、不允许按多数取、不允许截断到较短的那个 ——
    这三种做法都会**静默**制造出「看起来对齐了」的假象。

★ 期号一律按**数值**比较（双色球 2003~2009 是一位年 3001…，大乐透 7001…，
  跨年重编号 `26281 → 27001`）。字符串比较会错序。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

from ev.tier_backtest import load_test_recs


def build_periods(lottery: str, periods: Optional[int] = None, *,
                  exclude_rule_versions: Optional[Iterable[str]] = None,
                  rule_version_of: Optional[Any] = None) -> List[Any]:
    """取统一期窗（**升序**）。

    periods=None           全量
    periods=N              最近 N 期
    exclude_rule_versions  剔除指定规则版本覆盖的期（例：大乐透早期两段
                           `dlt-2007-05` / `dlt-2009-10`）。★ 必须在预注册里
                           事先固化，不允许事后挑。
    """
    recs = load_test_recs(lottery)
    out: List[Any] = []
    drop = set(exclude_rule_versions or ())
    for r in recs:
        iss = getattr(r, "期号", None)
        if iss is None:
            continue
        if drop:
            from ev.profit.rules import rules_for
            if rules_for(lottery, iss).rule_version in drop:
                continue
        out.append(iss)
    out.sort(key=_num)
    if periods:
        out = out[-int(periods):]
    return out


def _num(x: Any) -> float:
    try:
        return float(int(x))
    except (TypeError, ValueError):
        return float("inf")


def assert_same_periods(a: Sequence[Any], b: Sequence[Any], *,
                        what: str = "臂") -> None:
    """两臂的期窗必须**逐位相等**，否则抛异常。

    ★ 这里刻意做得「不宽容」：宁可让实验跑不起来，也不要产出一份
      「期窗其实没对齐但没人发现」的对照表。
    """
    la, lb = list(a), list(b)
    if la == lb:
        return
    n = min(len(la), len(lb))
    first = next((i for i in range(n) if la[i] != lb[i]), n)
    raise ValueError(
        f"{what} 的期窗不一致：长度 {len(la)} vs {len(lb)}，"
        f"首个不同位置 {first}：{la[first] if first < len(la) else '-'} vs "
        f"{lb[first] if first < len(lb) else '-'}。"
        f"★ 不允许求交/取众数/截断 —— 期窗是硬不变量，请回到 build_periods 统一生成。")


def profile(lottery: str, issues: Sequence[Any]) -> Dict[str, Any]:
    """期窗画像：规则版本分布、期号范围、跨年重编号次数。"""
    from ev.profit.rules import rules_for

    if not issues:
        return {"彩种": lottery, "期数": 0}
    vers: Dict[str, int] = {}
    for i in issues:
        v = rules_for(lottery, i).rule_version
        vers[v] = vers.get(v, 0) + 1
    nums = [_num(i) for i in issues]
    renumber = sum(1 for p in range(1, len(nums)) if nums[p] < nums[p - 1])
    return {
        "彩种": lottery,
        "期数": len(issues),
        "期号起": issues[0],
        "期号止": issues[-1],
        "规则版本分布": dict(sorted(vers.items())),
        "规则版本数": len(vers),
        "跨年重编号次数": renumber,
    }
