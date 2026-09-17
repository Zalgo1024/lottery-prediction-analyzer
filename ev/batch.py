"""
批次（Batch）解析 —— 回答「这条中奖记录是当时出号时的第几批、第几注」

一个「批次」= 一次出号任务产出的那一整组号码。

在数据流里，一次出号会写入**一条** pending 记录，里面 `预测号码` 是一个列表
（乐透型 100 注 / 数字型 5 注，见 config.resolve_groups）。开奖后评估时，
这条 pending 会被拆成 N 条反馈记录。所以：

    一条 pending  ==  一个批次  ==  N 条反馈记录

本模块只做一件事：把 feedback 记录重新归成批次，并算出每条记录的
**期内批号**（同一期第几次出号）与**批内注号**（该批第几注）。

批次号两条来源（响应里用 `批次来源` 标明，不混淆）：

- `explicit`（显式）：出号时打上的 `批次号`。新数据都走这条，精确到每一次点击"预测"。
- `derived`（推算）：2026-09-14 之前的历史记录没有批次号，只能回退按
  （彩种, 期号）+ `评估时间` 的间隔聚簇推断。相邻记录间隔超过 gap_seconds（默认 5 秒）
  就切一刀。这是**近似值**——同一分钟内跑两次会被并成一个批次，跨秒的批次可能被切开。

对外接口：
    group_batches(records, lottery)   -> List[BatchGroup]
    batch_fields(rec, group)          -> dict（写进单条记录的批次字段）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional

#: 推算批次时的切分间隔（秒）。同一次评估是循环写入，间隔在毫秒级；
#: 两次独立出号至少相隔数秒，5 秒足以区分又不会把跨秒的同批切开。
DEFAULT_GAP_SECONDS = 5.0


def _parse_time(value: Any) -> Optional[datetime]:
    """把 ISO 时间字符串解析成 datetime；解析不了返回 None（排序时排最后）。"""
    if not value:
        return None
    s = str(value)
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _time_label(value: Any) -> str:
    """批次显示用时间：'2026-09-13T21:48:58' -> '2026-09-13 21:48'"""
    dt = _parse_time(value)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else (str(value)[:16] if value else "")


@dataclass
class BatchGroup:
    """一个批次（一次出号）及其包含的全部反馈记录。"""

    batch_id: str
    source: str                      # "explicit" | "derived"
    lottery: str
    issue: Any = None
    time_label: str = ""
    records: List[dict] = field(default_factory=list)
    # 同一期号内、按出号时间先后的批次序号（1-based）；由 group_batches 填好
    issue_batch_index: Optional[int] = None
    issue_batch_count: Optional[int] = None

    @property
    def size(self) -> int:
        return len(self.records)

    @property
    def detail(self) -> str:
        return "显式批次号" if self.source == "explicit" else "按评估时间推算"


def group_batches(
    records: Iterable[dict],
    lottery: str,
    gap_seconds: float = DEFAULT_GAP_SECONDS,
) -> List[BatchGroup]:
    """
    把反馈记录切成批次，并给每个批次编上「期号内第几批」。

    归组规则（两条，按优先级）：
      1. 记录带 `批次号`（出号时打的）→ 直接按它归并，最精确；
      2. 没有批次号的旧记录 → 在 (期号) 内按 `评估时间` 排序，
         相邻两条间隔 > gap_seconds 就切一个新批次。

    混合数据（部分有批次号、部分没有）会同时走到两条规则，互不干扰；
    但「期号内第几批」是**跨这两者统一编号**的（derived 的 batch_id 里那个 seq
    只统计 derived 组，不能复用，所以这里重新排一遍）。

    组间返回顺序：时间倒序（最新批次在前），同时间按 batch_id 稳定排序。
    """
    recs = [r for r in (records or []) if isinstance(r, dict)]
    if not recs:
        return []

    explicit: Dict[str, BatchGroup] = {}
    derived_raw: List[dict] = []

    for r in recs:
        bid = r.get("批次号")
        if bid:
            g = explicit.get(str(bid))
            if g is None:
                g = BatchGroup(
                    batch_id=str(bid),
                    source="explicit",
                    lottery=lottery,
                    issue=r.get("期号"),
                    time_label=_time_label(r.get("批次时间") or r.get("评估时间")),
                )
                explicit[str(bid)] = g
            g.records.append(r)
        else:
            derived_raw.append(r)

    derived: List[BatchGroup] = []
    # 推算：先按期号分组，组内按时间排序后按间隔切分
    by_issue: Dict[Any, List[dict]] = {}
    for r in derived_raw:
        by_issue.setdefault(r.get("期号"), []).append(r)

    for issue, items in by_issue.items():
        items.sort(key=lambda x: (_parse_time(x.get("评估时间")) is None,
                                  _parse_time(x.get("评估时间")) or datetime.min))
        cur: Optional[BatchGroup] = None
        prev_t: Optional[datetime] = None
        for r in items:
            t = _parse_time(r.get("评估时间"))
            need_new = cur is None
            if not need_new and t is not None and prev_t is not None:
                if abs((t - prev_t).total_seconds()) > gap_seconds:
                    need_new = True
            if need_new:
                seq = len([g for g in derived if g.issue == issue]) + 1
                cur = BatchGroup(
                    batch_id=f"D:{lottery}:{issue}:{seq}",
                    source="derived",
                    lottery=lottery,
                    issue=issue,
                    time_label=_time_label(r.get("评估时间")),
                )
                derived.append(cur)
            cur.records.append(r)
            if t is not None:
                prev_t = t

    groups = list(explicit.values()) + derived

    # 「期号内第几批」：同一 (彩种, 期号) 内按出号时间先后连续编号 1/2/3…
    # explicit 与 derived 统一编号；只有一批时就是「第 1 批」。
    issue_groups: Dict[Any, List[BatchGroup]] = {}
    for g in groups:
        issue_groups.setdefault((g.lottery, g.issue), []).append(g)
    for gl in issue_groups.values():
        gl.sort(key=lambda g: (g.time_label, g.batch_id))   # 出号时间先后
        for i, g in enumerate(gl, start=1):
            g.issue_batch_index = i
            g.issue_batch_count = len(gl)

    # 时间倒序（最新批次在前）；同时间按批次号稳定排序，保证结果可复现
    groups.sort(key=lambda g: (g.time_label, g.batch_id), reverse=True)
    return groups


#: 组合数折算口径：待开奖弹窗把注按 5 注/组打包展示，/hits 徽章的
#: 「N 注/G 组」用同一口径，两处数字能对上。
GROUP_SIZE_FOR_DISPLAY = 5


def _combo_count(n: Any) -> Optional[int]:
    """注数 → 组合数（5 注/组，向上取整）；输入非法返回 None。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return None
    return (n + GROUP_SIZE_FOR_DISPLAY - 1) // GROUP_SIZE_FOR_DISPLAY if n > 0 else None


def _original_size(rec: dict, fallback: Any) -> Optional[int]:
    """批次原始出号注数：优先压缩前注数（质量压缩批次），否则=登记规模
    （未压缩批次两者本就相等，历史记录无压缩概念也是原值）。"""
    orig = rec.get("压缩前注数")
    try:
        return int(orig) if orig else (int(fallback) if fallback else None)
    except (TypeError, ValueError):
        return None


def batch_fields(rec: dict, group: Optional[BatchGroup] = None) -> Dict[str, Any]:
    """
    单条记录要展示的批次字段（给 /api/hits 的 records 用）。

    `批次内序号`（第几注）优先用出号时写下的原值；2026-09-14 之前的历史记录没有
    这个字段，退化为**组内位置 + 1** —— 一个批次在 JSON 里是连续写入的一段、
    group_batches 又按评估时间升序排过，所以组内顺序 == 当时出号的顺序。
    推算出来的会在 `批次内序号推算` 标 True，前端据此在悬停文案里说明。
    """
    if group is None:
        # 兜底：拿不到归组结果时（例如记录来自另外的入口），至少给出批次规模
        _orig0 = _original_size(rec, rec.get("批次规模"))
        _c0 = _combo_count(_orig0)
        return {
            "批次号": rec.get("批次号") or "",
            "批次来源": "explicit" if rec.get("批次号") else "derived",
            "批次规模": rec.get("批次规模"),
            "原始注数": _orig0,
            "原始组合数": _c0,
            "期号内批号": None,
            "期号内批数": None,
            "批次内序号": rec.get("批次内序号"),
            "批次内序号推算": rec.get("批次内序号") is None,
            "批次时间": _time_label(rec.get("批次时间") or rec.get("评估时间")),
            "批次标签": "",
        }

    within = rec.get("批次内序号")
    estimated = within is None
    if within is None:
        # 用 `is` 而不是 list.index()：后者按值比较，同批若有两条完全相同的记录
        # （号码、策略都一样）会取到同一个下标，导致注号重复。
        pos = next((i for i, x in enumerate(group.records) if x is rec), None)
        within = pos + 1 if pos is not None else None
    if isinstance(within, int) and within <= 0:
        within = None

    bidx = group.issue_batch_index
    bcnt = group.issue_batch_count
    # 原始出号规模：压缩批次取压缩前注数（>登记规模），未压缩/历史批次=批次规模
    orig = _original_size(rec, group.size)
    combos = _combo_count(orig)
    orig_part = f" · 原始 {orig} 注/{combos} 组" if orig else ""
    return {
        "批次号": group.batch_id,
        "批次来源": group.source,
        "批次规模": group.size,                 # 组长度 = 真实批次规模（含未中奖的注）
        "原始注数": orig,
        "原始组合数": combos,
        "期号内批号": bidx,
        "期号内批数": bcnt,
        "批次内序号": within,
        "批次内序号推算": estimated,
        "批次时间": group.time_label,
        "批次标签": (f"第 {bidx} 批 · 第 {within} 注{orig_part}"
                     if bidx and within else ""),
    }


def _prize_name(rec: dict) -> str:
    """中奖等级字段：乐透型用 `中奖等级`，数字型可能只在 `中奖玩法` 里。"""
    return rec.get("中奖等级") or rec.get("中奖玩法") or "未中"


def batch_detail(group: BatchGroup, lottery: str) -> Dict[str, Any]:
    """
    批次详情：这一批的出号时间 + 全部号码（按出号顺序）。

    一个批次虽然只有 N 条 feedback 记录，但那正是当时出的全部注（含未中奖的）——
    所以「这一批都出了什么号码」不需要额外落盘，按组内顺序还原即可。

    出号时间的精度取决于数据来源：
      explicit（2026-09-14 起新数据）→ 出号时写下的 `批次时间`，精确到秒；
      derived（之前的历史）→ 只能退到 `预测日期`（日级），`出号时间精确` 标 False，
        因为当时几点几分出的号没有落盘。
    """
    recs = group.records

    gen_raw = sorted((str(r.get("批次时间")) for r in recs if r.get("批次时间")),
                     key=lambda v: _parse_time(v) or datetime.min)
    pred_dates = sorted({str(r.get("预测日期")) for r in recs if r.get("预测日期")})
    if gen_raw:
        # 显式批次有出号时刻，给到秒级（详情弹窗要求「精确到是什么时间出的号」）
        dt = _parse_time(gen_raw[-1])
        gen_time = dt.strftime("%Y-%m-%d %H:%M:%S") if dt else str(gen_raw[-1])[:19]
        gen_precise = True
    elif pred_dates:
        gen_time, gen_precise = pred_dates[0], False
    else:
        gen_time, gen_precise = "", False

    settle_raw = sorted((str(r.get("评估时间")) for r in recs if r.get("评估时间")),
                        key=lambda v: _parse_time(v) or datetime.min)

    tickets = []
    for i, r in enumerate(recs, start=1):
        if "预测红球" in r or "预测蓝球" in r:
            nums: Dict[str, Any] = {"红球": list(r.get("预测红球") or []),
                                    "蓝球": list(r.get("预测蓝球") or [])}
        else:
            nums = dict(r.get("预测号码") or {})
        tickets.append({
            "序号": i,                       # 出号顺序，1-based（与 批次内序号 同口径）
            "号码": nums,
            "总命中": r.get("总命中", 0),
            "中奖等级": _prize_name(r),
        })

    return {
        "彩种": lottery,
        "期号": group.issue,
        "批次号": group.batch_id,
        "批次来源": group.source,
        "期号内批号": group.issue_batch_index,
        "期号内批数": group.issue_batch_count,
        "注数": group.size,
        # 原始出号规模（压缩批次 > 注数；未压缩/历史批次 == 注数），供详情弹窗对照
        "原始注数": _original_size(recs[0] if recs else {}, group.size),
        "中奖注数": sum(1 for t in tickets if t["中奖等级"] != "未中"),
        "出号时间": gen_time,
        "出号时间精确": gen_precise,
        "结算时间": _time_label(settle_raw[0]) if settle_raw else "",
        "号码": tickets,
    }
