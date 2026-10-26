# -*- coding: utf-8 -*-
"""scripts/budget_check.py —— 娱乐预算包络的**可执行**检查入口（v1.8 用户指令一）。

为什么需要它
------------
`ev/budget_envelope.py` 提供纪律，但纪律要被真正执行，必须有一个能跑、能看、
能把「本月已投」记住的入口。否则「我会控制预算」只是句话。

用法
----
  # 1) 只看包络与额度（快，不读行情、不跑判定树）
  E:/Python/python.exe scripts/budget_check.py

  # 2) 记一笔本期投入（写 config/budget_ledger.json，原子写）
  E:/Python/python.exe scripts/budget_check.py --record 400 --lottery 双色球

  # 3) 清零本月账本
  E:/Python/python.exe scripts/budget_check.py --reset

  # 4) 真跑判定树并过闸门（慢：需要 market.db 与足够历史）
  E:/Python/python.exe scripts/budget_check.py --decide --lottery 双色球 --issue 26122

★ 退出码：0 = 有可执行额度；2 = 全部彩种额度已耗尽（供 cron/告警使用）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))

from ev import budget_envelope as BE                        # noqa: E402

DEFAULT_LOTTERIES = ("双色球", "大乐透")


def _print_envelope(lot: str, led: BE.Ledger) -> dict:
    env = BE.default_envelope(lot)
    spent = led.spent()
    alloc = BE.allocate(env, spent)
    dec = BE.decide(env, spent, None)          # 净 EV 未给 ⇒ 默认不参与
    rep = BE.monthly_report(env, return_rate=0.5245)
    row = {"彩种": lot, "包络": {
        "月度定额元": env.monthly_budget, "单期上限元": env.per_period_cap,
        "基线注数": env.baseline_tickets, "标签": env.标签,
        "本月已投元": spent, "剩余额度元": alloc["剩余额度"],
        "本期可执行注数": alloc["注数"], "月度期望净亏元": rep["期望净亏元"]},
        "默认判决": dec["判决"], "原因": dec["原因"]}
    print(f"[{lot}] 定额 {env.monthly_budget:.0f} 元/月 | 单期上限 "
          f"{env.per_period_cap:.0f} 元（{env.cap_tickets} 注）| 基线 "
          f"{env.baseline_tickets} 注 | 本月已投 {spent:.0f} 元 | 剩余 "
          f"{alloc['剩余额度']:.0f} 元 ⇒ 本期可执行 {alloc['注数']} 注")
    print(f"    默认判决：{dec['判决']} —— {dec['原因']}")
    if rep["期望净亏元"] < 0:
        print(f"    按税后返奖率 {rep['返奖率口径']:.4f} 折算：月计划投入 "
              f"{rep['月计划投入元']:.0f} 元 ⇒ 期望净亏 {rep['期望净亏元']:+.0f} 元")
    return row


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="娱乐预算包络检查（只封顶投入，不改概率）")
    ap.add_argument("--lottery", default=None, help="彩种（默认 双色球/大乐透）")
    ap.add_argument("--record", type=float, default=None, metavar="元",
                    help="记一笔本期投入")
    ap.add_argument("--reset", action="store_true", help="清零本月账本")
    ap.add_argument("--decide", action="store_true",
                    help="真跑判定树并过闸门（慢，需要 market.db）")
    ap.add_argument("--issue", default=None, help="配合 --decide 指定期号")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args(argv)

    lots = (args.lottery,) if args.lottery else DEFAULT_LOTTERIES
    led = BE.Ledger()

    if args.reset:
        led.reset()
        print(f"已清零本月账本：{led.path}")
        return 0
    if args.record is not None:
        doc = led.add(args.record, note=args.lottery or "手工记账")
        print(f"已记账 {args.record:.2f} 元 ⇒ 本月累计 {doc['已投']:.2f} 元（{led.path.name}）")

    rows = []
    for lot in lots:
        rows.append(_print_envelope(lot, led))

    if args.decide:
        from ev.profit import solvers as SL
        for lot in lots:
            env = BE.default_envelope(lot)
            issue = args.issue
            if issue is None:
                from ev.tier_backtest import load_test_recs
                issue = load_test_recs(lot)[-1].期号
            print(f"\n[{lot} {issue}] 跑判定树 + 包络闸门 ...")
            out = SL.decide(lot, issue, envelope=env, spent_this_month=led.spent())
            g = out.get("包络") or {}
            print(f"    上游判决：{out.get('上游判决', out.get('判决'))} | "
                  f"闸门后：{out.get('判决')} | 注数 {out.get('注数')}")
            if g.get("是否降级"):
                print(f"    降级原因：{g.get('原因')}")

    print("\n" + BE.HONEST_NOTE)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=1))

    any_left = any(r["包络"]["本期可执行注数"] > 0 for r in rows)
    return 0 if any_left else 2


if __name__ == "__main__":
    raise SystemExit(main())
