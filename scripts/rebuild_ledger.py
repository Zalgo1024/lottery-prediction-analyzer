"""重建盈利账本（批处理；Web 页面只读已落盘的账本，不在这里触发）。

用法：
    python scripts/rebuild_ledger.py --all              # 六个彩种
    python scripts/rebuild_ledger.py 双色球 大乐透       # 指定彩种
    python scripts/rebuild_ledger.py --all --periods 200 # 只保留最近 200 期
    python scripts/rebuild_ledger.py --all --dry-run    # 只算不写

★ 为什么要单独一个脚本：
  账本必须包含「已开奖但本期没出号」的期（分母），这要从开奖数据全量比一遍，
  几万条记录的量级。放进 Web 请求里会让页面变慢几百毫秒到几秒，而且 Web 侧
  一律只读、不写盘（`web/routes/ledger.py` 的纪律）。

★ 本脚本幂等：同样的历史记录重复跑，落盘内容一致（`data/ledger.recompute` 幂等，
  成本/奖金全部由记录重算，不累加）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data import ledger as L  # noqa: E402

ALL = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]


def _fmt(s: dict) -> str:
    def money(x):
        return "—" if x is None else f"{x:,.2f}"
    rate = "—" if s.get("净收益率") is None else f"{s['净收益率'] * 100:.1f}%"
    return (f"{s['彩种']}: 开奖 {s['开奖期数']} / 参与 {s['参与期数']} / 未参与 {s['未参与期数']}"
            f" | 成本 {money(s.get('成本'))} 奖金 {money(s.get('税前奖金'))}"
            f" 净 {money(s.get('净盈亏'))} ({rate}) | 回撤 {money(s.get('最大回撤'))}"
            f" | 中奖注 {s.get('中奖注数')} 期 {s.get('中奖期数')}"
            f" | ≥10元期 {s.get('中奖期数_ge10')}"
            f" | 金额未知 {s.get('金额未知注数')} 注")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="重建盈利账本")
    ap.add_argument("lotteries", nargs="*", help="彩种（留空且不给 --all 时等于 --all）")
    ap.add_argument("--all", action="store_true", help="全部六个彩种")
    ap.add_argument("--periods", type=int, default=None, help="只保留最近 N 期")
    ap.add_argument("--dry-run", action="store_true", help="只计算不写盘")
    ap.add_argument("--json", action="store_true", help="输出 JSON 而不是表格行")
    args = ap.parse_args(argv)

    targets = ALL if (args.all or not args.lotteries) else args.lotteries
    out = []
    for lot in targets:
        rows = L.build_rows(lot)
        rows = L.fill_empty(rows, lot)
        if args.periods:
            rows = rows[-int(args.periods):]
        rows = L.recompute(rows)
        if not args.dry_run:
            L._write(lot, rows)
        out.append(L.summary_of(rows, lottery=lot))

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        for s in out:
            print(_fmt(s))
        if args.dry_run:
            print("（dry-run：未写盘）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
