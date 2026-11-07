# -*- coding: utf-8 -*-
"""票面注数与合法性 —— 纯函数，无 IO、不读盘、不写盘。

为什么单独一个模块（2026-11-07）
--------------------------------
本项目一直缺一个「这注票是几注、合不合法」的前置口径：
  · `engine._pad_to_groups` 只保证「出号数量 = 最终组数」，不保证**每组票面合法**；
  · `pending` 里的 `号码组数 / 目标注数 / 实际注数 / len(预测号码)` 是四个各自写入的数字，
    没有任何一处校验它们相等；
  · 复式 / 胆拖目前不产出（生产只出单式），但一旦要接，注数公式必须先定，
    否则会按错误的组合数算成本与额度。

事故形态是同一类：**数字对不上但不报错**。例如「改成 1w 注、重出后变回 1000 注」
（2026-11-07）与「七乐彩特别号 choose=0 被当成 1 ⇒ 八码非法票面」，都是
「某一层的注数/票面数被另一层改写」。本模块把它们收敛成两条可复核的判据：
  ① 注数 = 各分区组合数之积，公式只在这里写一次；
  ② 每组票面必须满足 schema：区名认识、数量等于票面数、号码在区间内、不可重复区无重复。

★ 位置型（排列3/5、福彩3D、七星彩）的注数**不是** `C(池, k)`：
  每位独立取 1 个 ⇒ 注数 = ∏(每位可选项数)。外部参考程序在这里用了 `C(redCount,6)`，
  会把 15 选 10 位的票算成 5005 注而真实是 10^6 量级。
"""
from __future__ import annotations

from math import comb
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from config import ticket_price
from data.schema import get_schema

__all__ = ["MAX_GROUPS", "batch_check", "ticket_count", "ticket_shape",
           "validate_group"]

# 单张票的注数上限（与 data.ticket_size.HARD_CAP 同源：都来自官方「单张最多 1 万注」）
MAX_GROUPS = 10000


def ticket_shape(lottery: str, *, play: Optional[str] = None) -> List[Dict[str, Any]]:
    """该彩种（该玩法）的票面结构：每区应选几个号。

    `draw_only` 区（七乐彩特别号）不出现 —— 它只在开奖里，票面没有这一区。
    """
    schema = get_schema(lottery)
    picks = schema.picks_for(play) if play else schema.picks_for()
    out = []
    for z in schema.zones:
        if z.draw_only:
            continue
        out.append({
            "区": z.name,
            "最小": int(z.min),
            "最大": int(z.max),
            "应选": int(picks.get(z.name, z.ticket_count)),
            "可重复": bool(z.repeatable),
            "有序": bool(z.ordered),
        })
    return out


def _zone_multiplier(size: int, need: int, *, ordered: bool, repeatable: bool) -> int:
    """单个分区的组合数。

    · 位置型（有序）：每位独立取 1 ⇒ `size`
    · 乐透型且不重复：`C(size, need)`
    · 可重复且无序：`size ** need`
    """
    if ordered:
        # 位置型的「池」就是该位可选的数字集合，选 1 个；跨位相乘由调用方做。
        return max(0, int(size))
    if repeatable:
        return max(0, int(size)) ** max(0, int(need))
    return comb(int(size), int(need)) if 0 <= int(need) <= int(size) else 0


def ticket_count(lottery: str, *, pools: Optional[Mapping[str, Sequence[int]]] = None,
                 dan: Optional[Mapping[str, Sequence[int]]] = None,
                 tuo: Optional[Mapping[str, Sequence[int]]] = None,
                 play: Optional[str] = None,
                 cap: int = MAX_GROUPS) -> Dict[str, Any]:
    """算这套选法的注数 / 成本 / 是否超单张上限。

    参数语义（都是「每区」）：
      · `pools=None` 且 `dan/tuo=None` ⇒ **单式**：每区正好 `应选` 个号，1 注
      · `pools` 给了某区 ⇒ **复式**：该区从池里任取 `应选` 个
      · `dan`+`tuo` 给了某区 ⇒ **胆拖**：胆码固定，拖码里补 `应选-胆数` 个

    返回 `{注数, 单价, 成本, 上限, 合法, 原因, 明细}`；不合法时 `注数` 仍给出
    （方便看「超了多少」），但 `合法=False` 且 `原因` 写明。
    """
    shape = ticket_shape(lottery, play=play)
    price = float(ticket_price(lottery))
    total = 1
    detail: List[Dict[str, Any]] = []
    reasons: List[str] = []

    for z in shape:
        name, need = z["区"], int(z["应选"])
        pool = list(pools.get(name) or []) if pools else []
        d = list(dan.get(name) or []) if dan else []
        t = list(tuo.get(name) or []) if tuo else []

        if d or t:
            if need <= 0:
                reasons.append(f"{name}：该区票面不选号，不能设胆拖")
                mult = 0
            elif len(d) >= need:
                reasons.append(f"{name}：胆码 {len(d)} 个 ≥ 应选 {need} 个，拖码无处安放")
                mult = 0
            else:
                mult = _zone_multiplier(len(t), need - len(d), ordered=False, repeatable=False)
                if mult == 0:
                    reasons.append(f"{name}：拖码 {len(t)} 个不足以补齐 {need - len(d)} 个")
            detail.append({"区": name, "形态": "胆拖", "应选": need, "胆": len(d),
                           "拖": len(t), "该区组合数": mult})
        elif pool:
            if need <= 0:
                reasons.append(f"{name}：该区票面不选号，不能设复式")
                mult = 0
            else:
                mult = _zone_multiplier(len(pool), need, ordered=z["有序"],
                                        repeatable=z["可重复"])
                if mult == 0:
                    reasons.append(f"{name}：池 {len(pool)} 个不够取 {need} 个")
            detail.append({"区": name, "形态": "复式", "应选": need, "池": len(pool),
                           "有序": z["有序"], "该区组合数": mult})
        else:
            mult = 1
            detail.append({"区": name, "形态": "单式", "应选": need, "该区组合数": 1})

        total *= int(mult)

    over = total > int(cap)
    if over:
        reasons.append(f"注数 {total} 超过单张上限 {cap}")
    return {
        "彩种": lottery,
        "玩法": play or "",
        "注数": int(total),
        "单价": price,
        "成本": round(total * price, 2),
        "上限": int(cap),
        "合法": (not reasons),
        "原因": "；".join(reasons),
        "明细": detail,
        "公式": "各分区组合数之积（位置型取池大小，乐透型取 C(池,应选)，胆拖取 C(拖,应选-胆)）",
    }


def _validate_with_shape(group: Any, shape: List[Dict[str, Any]]) -> List[str]:
    """`validate_group` 的内核：形状算一次，整批复用（1w 注 × 8 彩种不能每注重算）。"""
    """校验**一组**票面，返回问题列表（空列表 = 合法）。

    `group` 可以是 pending 里的一条（`{"号码": {区: [..]}}`），也可以是裸的 `{区: [..]}`。
    检查项：
      ① 分区名必须是该彩种认识的可选分区（特别号这种 draw_only 不应出现）
      ② 每区号码数 == 票面应选数
      ③ 号码在 [min, max] 内
      ④ 不可重复区不得有重复号
      ⑤ 不得缺区
    """
    if isinstance(group, Mapping) and isinstance(group.get("号码"), Mapping):
        zones: Mapping[str, Any] = group["号码"]
    elif isinstance(group, Mapping):
        zones = group
    else:
        return ["票面不是字典（无法解析分区）"]

    known = {z["区"]: z for z in shape}
    problems: List[str] = []

    # ① 多余分区（典型：把 draw_only 的特别号写进票面）
    for name in zones:
        if name not in known:
            problems.append(f"未知/非票面分区：{name}")

    for name, z in known.items():
        nums = zones.get(name)
        if nums is None:
            problems.append(f"缺分区：{name}")
            continue
        try:
            vals = [int(x) for x in nums]
        except (TypeError, ValueError):
            problems.append(f"{name}：号码不是整数")
            continue
        if len(vals) != int(z["应选"]):
            problems.append(f"{name}：{len(vals)} 个 ≠ 应选 {z['应选']} 个")
        lo, hi = int(z["最小"]), int(z["最大"])
        bad = [v for v in vals if v < lo or v > hi]
        if bad:
            problems.append(f"{name}：越界 {sorted(set(bad))[:5]}（应 {lo}-{hi}）")
        if not z["可重复"] and len(set(vals)) != len(vals):
            problems.append(f"{name}：不可重复分区出现重复号")

    # 红/蓝便捷视图与「号码」不一致（历史记录两种字段并存）
    if isinstance(group, Mapping):
        for alias, key in (("红球", "红球"), ("蓝球", "蓝球")):
            if alias in zones and key in group and isinstance(group.get(key), (list, tuple)):
                if [int(x) for x in zones[alias]] != [int(x) for x in group[key]]:
                    problems.append(f"{alias} 便捷字段与「号码」不一致")
    return problems


def validate_group(lottery: str, group: Any, *, play: Optional[str] = None) -> List[str]:
    """校验**一组**票面，返回问题列表（空列表 = 合法）。

    `group` 可以是 pending 里的一条（`{"号码": {区: [..]}}`），也可以是裸的 `{区: [..]}`。
    """
    return _validate_with_shape(group, ticket_shape(lottery, play=play))


def batch_check(lottery: str, groups: Iterable[Any], *, declared: Optional[int] = None,
                play: Optional[str] = None, max_examples: int = 3) -> Dict[str, Any]:
    """整批校验：注数口径 + 每组票面合法性。

    `declared` 传 pending 里声明的组数（如 `号码组数`）。四个数字必须相等：
    声明组数 / 实际组数 / `len(预测号码)` / 本函数数到的组数。
    """
    lst = list(groups)
    problems: List[str] = []
    if declared is not None and int(declared) != len(lst):
        problems.append(f"声明组数 {int(declared)} ≠ 实际组数 {len(lst)}")

    shape = ticket_shape(lottery, play=play)      # ★ 形状只算一次
    bad: List[str] = []
    n_bad = 0
    for i, g in enumerate(lst):
        p = _validate_with_shape(g, shape)
        if not p:
            continue
        n_bad += 1
        if len(bad) < max_examples:          # 只留示例，计数用 n_bad（不许被截断影响）
            bad.append(f"#{i}: " + "；".join(p))
    if n_bad:
        problems.append(f"票面非法 {n_bad} 组（示例）：" + " | ".join(bad))

    return {
        "彩种": lottery,
        "组数": len(lst),
        "声明组数": None if declared is None else int(declared),
        "非法组数": n_bad,
        "合法": (not problems),
        "问题": problems,
    }
