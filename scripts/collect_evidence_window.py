# -*- coding: utf-8 -*-
"""方案 B 证据采集的窗口运行器（服务未启动时也能按频率采）。

    python scripts/collect_evidence_window.py --window   # 21:05–21:45 每 2 分钟
    python scripts/collect_evidence_window.py --once     # 立刻采一次（忽略窗口）
    python scripts/collect_evidence_window.py --status   # 只看状态，不采集

频率 / 窗口 / 单实例锁 / 日志的真源是 `web/evidence_schedule.py`，本文件只做
参数解析与输出。**与 Flask 内置调度器共用同一把锁和同一份状态**，所以两条路径
同时存在也不会重复采集。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web import evidence_schedule as ES  # noqa: E402
from datetime import datetime  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="开奖证据采集窗口运行器")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--window", action="store_true", help="按窗口循环采集（默认）")
    g.add_argument("--once", action="store_true", help="立刻采一次，忽略窗口")
    g.add_argument("--status", action="store_true", help="只打印状态，不采集")
    args = ap.parse_args(argv)

    now = datetime.now()
    if args.status:
        print(json.dumps(ES.status(now=now), ensure_ascii=False, indent=1))
        return 0

    if args.once:
        r = ES.run_once(now=now)
        print(json.dumps(r, ensure_ascii=False, indent=1))
        return 0 if r.get("状态") == "成功" else 1

    if not ES.in_window(now):
        print(json.dumps({
            "状态": "窗口外", "当前": now.isoformat(timespec="seconds"),
            "窗口": "21:05-21:45", "节拍秒": ES.INTERVAL_SECONDS,
            "说明": "本运行器只在窗口内循环；要在别的时间试跑请用 --once。",
        }, ensure_ascii=False, indent=1))
        return 0

    r = ES.run_window()
    print(json.dumps({
        "状态": "窗口结束", "轮数": r["轮数"], "退出原因": r["退出原因"],
        "逐轮": [{"状态": x.get("状态"), "返回码": x.get("返回码"),
                  "耗时": x.get("耗时"), "汇总": x.get("汇总")}
                 for x in r["结果"]],
    }, ensure_ascii=False, indent=1))
    return 0 if all(x.get("状态") == "成功" for x in r["结果"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
