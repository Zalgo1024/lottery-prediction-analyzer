# -*- coding: utf-8 -*-
"""预算闸门 · 只读调额决策表（离线脚本，不改配置、不写台账、不发推送）。

用法：
    E:/Python/python.exe scripts/budget_monitor.py
    E:/Python/python.exe scripts/budget_monitor.py --rounds 8 --multiples 1,1.5,2,3
    E:/Python/python.exe scripts/budget_monitor.py --days-in-month 30

★ 定位：看板 `web/routes/settings.py::_budget_snapshot()` 已经给出
  「月度总额 / 单期上限 / 已投 / 剩余 / 已用尽 / 月份 / 锁定」，本脚本**不重复**它。
  本脚本只补三样看板没有的东西：
    ① 逐轮夹注 / 停发推演（按当前生效注数一轮一轮往下走）；
    ② 调额决策表（候选档 → 可出注数 / 可支撑轮数 / 真钱敞口 / 单期上限是否构成约束）；
    ③ 口径声明（月度累计、无生效期号、反事实、只读）。

★ 只读纪律：本模块不写任何文件，不调用 record_spend / save_config / mark_stopped，
  也不调用 clamp()（它会读盘且带重出批回补）。推演走纯函数 clamp_projection()，
  公式与 ev.budget_gate.clamp() 的额度段一致，并由 tests/test_budget_monitor.py
  与真实 clamp() 对账钉住。产物只走 stdout；需要留档请用 shell 重定向。

★ 候选档用「现额度的倍数」表达，**不预设目标值**；数值由使用者拍板。
★ 满月估算有两个口径，都显式标注为估算、非实测：
  · 按开奖日历：逐彩种 `draw_days` 每周天数 × --days-in-month / 7 四舍五入取月期数
    （与 config/budget_aggregate.json 口径注释的「每日彩约 30 期、隔日彩约 13 期」同源）；
  · 每日开上界：假设每彩种天天开，作为最坏情况对照。
  两者都**不套用节假日顺延**（日历真源在 data/holiday.py，本脚本不引入该复杂度）。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from config import LOTTERY_CONFIG, ticket_price       # noqa: E402
from data import ticket_size as TS                     # noqa: E402
from ev import budget_gate as BG                      # noqa: E402

COUNTERFACTUAL_NOTE = (
    "整批 K 注口径不代表真实投入：实际通常只买前几注、且并非每期都买。"
    "本表所有金额按「系统出号整批全买」计，属反事实示意，不进任何闸门计算。"
)
READONLY_NOTE = (
    "未写 config、未写台账、未发推送、未改注数。"
    "推演中的「累计已投」是模拟值，不是真实台账。"
)
MONTHLY_NOTE = (
    "额度是**月度累计口径**：load_limit() 无「生效期号」字段，改 config 后作用于"
    "本月剩余全部期，无法只让单期生效；跨月由 load_spend() 归零。"
)
GATE_NOTE = (
    "闸门只封顶投入，不改变任何一注的中奖概率：E[中奖注数]=K·p，"
    "加注不改变单注期望，只放大方差与回撤。「未触发闸门」不代表「该投入划算」。"
)


# ------------------------------------------------------------------ 纯函数
def clamp_projection(want: int, *, remaining: float, price: float,
                     cap: float) -> int:
    """与 `ev.budget_gate.clamp()` 额度段同式的只读推演（不读盘、不写盘）。

    remaining 由调用方按「月度总额 − 模拟累计已投」逐轮推进；不含重出批回补
    （推演假设每轮都是首出批）。cap 已按 enabled_limit() 取 min(单期上限, 月度总额)。
    """
    left = max(0.0, float(remaining))
    n_month = int(left // price) if price > 0 else 0
    n_cap = int(float(cap) // price) if price > 0 else 0
    return max(0, min(int(want), n_month, n_cap))


def snapshot(*, limit_path=None, spend_path=None,
             ticket_cfg: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """汇总当前状态（只读）。测试通过 monkeypatch 模块常量注入临时文件。"""
    lim = BG.enabled_limit(path=limit_path)
    if lim is None:
        return {"可用": False, "原因": "预算闸门未启用（启用=false 或额度文件缺失）"}
    doc = BG.load_spend(path=spend_path)
    cfg = ticket_cfg if ticket_cfg is not None else TS.sanitize(TS.load_config())
    lots = list(LOTTERY_CONFIG)
    wants = {lot: int(TS.effective_count(lot, cfg)) for lot in lots}
    prices = {lot: float(ticket_price(lot)) for lot in lots}
    monthly = float(lim["月度总额"])
    cap = float(lim["单期上限"])
    spent = float(doc.get("已投") or 0.0)
    return {"可用": True, "月份": doc.get("月份"), "彩种": lots,
            "生效注数": wants, "单价": prices,
            "月度总额": monthly, "单期上限": cap, "已投": spent,
            "剩余": max(0.0, monthly - spent),
            "锁定": bool(lim.get("锁定", True)),
            "停发记录": list(doc.get("停发") or []),
            "台账损坏": doc.get("★台账损坏")}


def project_rounds(snap: Dict[str, Any], *, rounds: int) -> List[Dict[str, Any]]:
    """逐轮推演：每轮把 8 彩种各出一轮，按 clamp 公式夹注并累计模拟已投。"""
    monthly = float(snap["月度总额"])
    cap = float(snap["单期上限"])
    cur = float(snap["已投"])
    rows: List[Dict[str, Any]] = []
    for r in range(1, int(rounds) + 1):
        per: Dict[str, int] = {}
        for lot in snap["彩种"]:
            left = max(0.0, monthly - cur)
            n = clamp_projection(snap["生效注数"][lot], remaining=left,
                                 price=snap["单价"][lot], cap=cap)
            per[lot] = n
            cur += n * snap["单价"][lot]
        money = sum(per[lot] * snap["单价"][lot] for lot in snap["彩种"])
        rows.append({"轮次": r, "逐彩种注数": per, "本轮注数": sum(per.values()),
                     "本轮金额": round(money, 2), "模拟累计已投": round(cur, 2),
                     "本轮全停发": all(n == 0 for n in per.values())})
    return rows


def decision_table(snap: Dict[str, Any], *, multiples: Sequence[float],
                   days_in_month: int) -> Dict[str, Any]:
    """候选档决策表。档位用「现额度倍数」表达，不预设目标值。"""
    monthly = float(snap["月度总额"])
    cap = float(snap["单期上限"])
    spent = float(snap["已投"])
    days = int(days_in_month)
    round_need = sum(snap["生效注数"][lot] * snap["单价"][lot]
                     for lot in snap["彩种"])
    # 按开奖日历的月期数：draw_days 是每周开奖的星期几列表（config.py 单一真源）。
    month_periods = {
        lot: round(len((LOTTERY_CONFIG.get(lot) or {}).get("draw_days") or [])
                   * days / 7.0)
        for lot in snap["彩种"]}
    calendar_est = sum(snap["生效注数"][lot] * snap["单价"][lot]
                       * month_periods[lot] for lot in snap["彩种"])
    upper_est = round_need * days           # 每彩种天天开的最坏情况上界
    n_caps = {lot: int(cap // snap["单价"][lot]) if snap["单价"][lot] > 0 else 0
              for lot in snap["彩种"]}
    cap_binds = [lot for lot in snap["彩种"]
                 if snap["生效注数"][lot] > n_caps[lot]]

    def _row(m: float) -> Dict[str, Any]:
        candidate = monthly * float(m)
        left = max(0.0, candidate - spent)
        # 单轮出号：逐彩种共用剩余额度，按顺序消耗
        cur = spent
        n_total = 0
        for lot in snap["彩种"]:
            n = clamp_projection(snap["生效注数"][lot],
                                 remaining=max(0.0, candidate - cur),
                                 price=snap["单价"][lot], cap=cap)
            n_total += n
            cur += n * snap["单价"][lot]
        return {
            "倍数": float(m), "候选额度": round(candidate, 2),
            "本月剩余可投": round(left, 2),
            "单轮可出注数": n_total,
            "可支撑完整轮数": int(left // round_need) if round_need > 0 else 0,
            "真钱敞口": round(left, 2),
            "与满月按日历估算差额": round(calendar_est - candidate, 2),
        }

    rows: List[Dict[str, Any]] = [_row(m) for m in multiples]
    # 满月口径档：动态算出「按开奖日历全覆盖」对应现额度的多少倍。
    # 只作口径对照，不是推荐值；与用户传入档位重复时跳过。
    if monthly > 0:
        full_m = calendar_est / monthly
        if not any(abs(r["倍数"] - full_m) < 1e-6 for r in rows):
            rows.append(_row(full_m))
            rows[-1]["口径档"] = True
    return {"每轮需求": round(round_need, 2),
            "满月按开奖日历估算": round(calendar_est, 2),
            "满月每日开上界": round(upper_est, 2),
            "逐彩种月期数": month_periods,
            "天数假设": days,
            "单期上限每彩种可出注数": n_caps,
            "单期上限构成约束的彩种": cap_binds,
            "行": rows}


# ------------------------------------------------------------------ 渲染
def _fmt(x: Any) -> str:
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "是" if x else "否"
    if isinstance(x, float):
        return f"{x:,.2f}"
    if isinstance(x, int):
        return f"{x:,}"
    return str(x)


def _cell(x: Any) -> str:
    """单元格禁写裸 `|`（会撑破 markdown 表格）。"""
    return _fmt(x).replace("|", "／").replace("\n", " ")


def _sep(ncols: int) -> str:
    """分隔行按列数生成，不手数 `---` 段数。"""
    return "|" + "|".join(["---"] * ncols) + "|"


def _table(header: Sequence[str], rows: Sequence[Sequence[Any]]) -> List[str]:
    out = ["| " + " | ".join(header) + " |", _sep(len(header))]
    for row in rows:
        assert len(row) == len(header), f"表格列数不一致：{row}"
        out.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return out


def build_report(snap: Dict[str, Any], rounds_rows: List[Dict[str, Any]],
                 table: Dict[str, Any]) -> str:
    L: List[str] = ["# 预算闸门 · 只读调额决策表", ""]
    L += ["## 1. 口径与边界", "",
          f"- 只读：{READONLY_NOTE}",
          f"- 月度：{MONTHLY_NOTE}",
          f"- 反事实：{COUNTERFACTUAL_NOTE}",
          f"- 机制：{GATE_NOTE}",
          "- 候选档以「现额度倍数」表达，**不含推荐值**；目标值由使用者拍板。",
          f"- 满月估算按 `--days-in-month={table['天数假设']}` 与逐彩种 `draw_days`"
          " 推月期数；不套用节假日顺延，是**估算**不是实测。"
          f"按开奖日历 {table['满月按开奖日历估算']:,.2f} 元，"
          f"每日开上界 {table['满月每日开上界']:,.2f} 元（最坏情况对照）。", ""]

    if not snap["可用"]:
        L += ["## 2. 现状", "", f"不适用：{snap['原因']}。", "",
              "未参与：额度文件缺失或闸门未启用 ⇒ 逐轮推演与决策表均不计算（缺席≠通过）。", ""]
        return "\n".join(L) + "\n"

    L += ["## 2. 现状（看板已有字段，此处只作推演起点）", ""]
    L += _table(["字段", "值"], [
        ["月份", snap["月份"]], ["月度总额（元）", snap["月度总额"]],
        ["单期上限（元）", snap["单期上限"]], ["已投（元）", snap["已投"]],
        ["剩余（元）", snap["剩余"]], ["锁定", snap["锁定"]],
        ["台账中停发记录数", len(snap["停发记录"])],
        ["台账本次读取失败", snap["台账损坏"] or "否"],
        ["每轮需求（元，按生效注数）", table["每轮需求"]],
        ["剩余可支撑完整轮数",
         int(snap["剩余"] // table["每轮需求"]) if table["每轮需求"] > 0 else 0],
        ["距一整轮还差（元）",
         round(max(0.0, table["每轮需求"] - snap["剩余"]), 2)],
        ["满月按开奖日历估算（元）", table["满月按开奖日历估算"]],
        ["满月每日开上界（元）", table["满月每日开上界"]],
    ])
    L.append("")
    L.append(f"生效注数逐彩种：" + "、".join(
        f"{lot} {snap['生效注数'][lot]}" for lot in snap["彩种"]) + "。")
    L.append("")
    L.append("逐彩种月期数（按 `draw_days` 估算，不套节假日）：" + "、".join(
        f"{lot} {table['逐彩种月期数'][lot]}" for lot in snap["彩种"]) + "。")
    L.append("")
    if table["每轮需求"] > snap["剩余"] > 0:
        L.append(f"剩余 {snap['剩余']:,.2f} 元**不够一整轮**"
                 f"（每轮需 {table['每轮需求']:,.2f} 元）⇒ 下一轮会逐彩种夹注，"
                 "排在后面的彩种可能直接停发；逐轮明细见第 3 节。")
        L.append("")

    L += ["## 3. 逐轮夹注 / 停发推演（模拟，不写台账）", ""]
    L += _table(["轮次", "本轮注数", "本轮金额（元）", "模拟累计已投（元）", "本轮全停发"],
                [[r["轮次"], r["本轮注数"], r["本轮金额"],
                  r["模拟累计已投"], r["本轮全停发"]] for r in rounds_rows])
    L += ["", "逐彩种注数（每轮）：", ""]
    L += _table(["轮次"] + list(snap["彩种"]),
                [[r["轮次"]] + [r["逐彩种注数"][lot] for lot in snap["彩种"]]
                 for r in rounds_rows])
    L += ["", "说明：推演假设每轮均为首出批（不含重出批回补），"
          "且按 `auto` 之外的口径不额外夹到基线；真实出号还受 `auto` 永不加注约束，"
          "故真实注数不会高于本表。", ""]

    L += ["## 4. 调额决策表（数值待拍板）", ""]
    L += _table(["现额度倍数", "候选额度（元）", "本月剩余可投（元）",
                 "下一轮可出注数", "可支撑完整轮数", "真钱敞口（元）",
                 "与满月按日历估算差额（元）"],
                [[r["倍数"], r["候选额度"], r["本月剩余可投"],
                  r["单轮可出注数"], r["可支撑完整轮数"], r["真钱敞口"],
                  r["与满月按日历估算差额"]] for r in table["行"]])
    L += ["", "最后一行是「满月按开奖日历估算」对应的额度档（倍数由 `draw_days` 动态算出），"
          "只作口径对照，**不是推荐值**；目标值由使用者拍板。"]
    L.append("")
    binds = table["单期上限构成约束的彩种"]
    if binds:
        L.append(f"单期上限**构成约束**：" + "、".join(binds)
                 + " 的生效注数高于 `单期上限 ÷ 单价`；"
                 "只调月度总额不会解除该约束，须另行拍板是否同步调整单期上限。")
    else:
        L.append("单期上限**当前不构成约束**：各彩种生效注数均不高于"
                 " `单期上限 ÷ 单价`；调月度总额即生效，无需同步调整单期上限。"
                 "（单期上限是每期每彩种口径，与月度总额独立。）")
    L.append("")
    L.append("「真钱敞口」= 候选额度 − 本月已投，属整批全买的反事实上界，"
             "不等于实际购买金额。")
    L.append("")

    L += ["## 5. 待拍板 / 未参与项", "",
          "1. 调额目标值：本表不含推荐值，未拍板前 `config/budget_aggregate.json` 不写。",
          "2. 若确需「只让下一期生效」：当前配置无「生效期号」字段，"
          "需改生产闸门口径，属另行授权范围（本轮未做）。",
          "3. 满月期数口径：按 `draw_days` 与天数参数估算，未套用节假日顺延，"
          "故两列均标「估算」，不写成实测；每日开上界仅作最坏情况对照。",
          "4. 「按实际只买前 n 注」口径：缺 n 真实值与买期规则，"
          "本轮未参与、不进闸门（缺席≠通过）。", ""]
    return "\n".join(L) + "\n"


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="预算闸门只读调额决策表（不写盘）")
    ap.add_argument("--rounds", type=int, default=8, help="逐轮推演轮数（默认 8）")
    ap.add_argument("--multiples", default="1,1.5,2,3",
                    help="候选档：现额度的倍数，逗号分隔（默认 1,1.5,2,3）")
    ap.add_argument("--days-in-month", type=int, default=30,
                    help="满月估算的天数假设（默认 30；配合 draw_days 推月期数）")
    args = ap.parse_args(argv)
    try:
        multiples = tuple(float(x.strip()) for x in args.multiples.split(",") if x.strip())
    except ValueError:
        ap.error("--multiples 必须是逗号分隔的数字")
    if not multiples or any(m <= 0 for m in multiples):
        ap.error("--multiples 必须全为正数")
    if args.rounds < 1 or args.days_in_month < 1:
        ap.error("--rounds / --days-in-month 必须 ≥ 1")

    snap = snapshot()
    if not snap["可用"]:
        # 缺席≠通过：闸门不可用时推演与决策表都标「不适用/未参与」，不伪造 0 值结论。
        print(build_report(snap, [], {
            "每轮需求": 0, "满月按开奖日历估算": 0, "满月每日开上界": 0,
            "逐彩种月期数": {}, "天数假设": args.days_in_month,
            "单期上限每彩种可出注数": {}, "单期上限构成约束的彩种": [], "行": []}))
        return 0
    rows = project_rounds(snap, rounds=args.rounds)
    table = decision_table(snap, multiples=multiples,
                           days_in_month=args.days_in_month)
    print(build_report(snap, rows, table))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
