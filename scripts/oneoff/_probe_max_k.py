# -*- coding: utf-8 -*-
"""探针 3：生产的重叠上限下，双色球 ≥四等 最多能选出多少注？

探针 2 在 K=2251 时只出了 938 注（要求 2251）。这说明卡住的不是概率，
而是 `重叠上限=3`（POProd 口径）把可选的票族卡死了。本探针把 K 网格往上推，
找出「要多少注才开始出票不足」，并记录 meta 里的放宽/污染标记。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ev.arena import arms as A                                    # noqa: E402
from ev.coverage_backtest import build_pool                       # noqa: E402
from ev import tier_target as TT                                  # noqa: E402

TARGET = {"kind": "grade", "grade": "四等"}
OUT = Path("output") / "_probe_max_k.json"
M = 2000


def main() -> int:
    rows = []
    res = TT.resolve_conditions("双色球", TARGET, None)
    p1 = TT.single_ticket_probability("双色球", res["conditions"], None)
    need = int(-(-1 // p1)) if p1 > 0 else 0
    print(f"单注 p={p1:.6e}  K·p=1 需要 K≈{need} 注", flush=True)
    for K in (1000, 1500, 2000, 2251, 2700):
        pool_n = max(8000, 40 * K)
        pool = build_pool("双色球", pool_n, seed=20261024)
        t0 = time.time()
        try:
            tks, meta = A.select_arm("union_greedy", "双色球", pool, K, TARGET, None,
                                     seed=0, cap=3, params={"m_eval": M, "m_fit": M})
        except Exception as e:                                # noqa: BLE001
            rows.append({"K": K, "池": pool_n, "状态": f"失败 {type(e).__name__}: {e}"})
            print(f"K={K} 失败 {e}", flush=True)
            continue
        dt = time.time() - t0
        rec = {"K": K, "池": pool_n, "要求注数": K, "实际注数": len(tks),
               "出票率": round(len(tks) / K, 4), "耗时秒": round(dt, 1),
               "放宽次数": meta.get("放宽次数"), "重叠上限": meta.get("重叠上限"),
               "cap污染": meta.get("cap污染"), "出票不足": meta.get("出票不足")}
        rows.append(rec)
        print(f"K={K} 池={pool_n} 实际 {len(tks)}/{K} "
              f"放宽={meta.get('放宽次数')} cap污染={meta.get('cap污染')} {dt:.0f}s",
              flush=True)
    # 池耗竭 vs 重叠上限：把 cap 放松到 4 再看 K=2251
    pool = build_pool("双色球", 90000, seed=20261024)
    t0 = time.time()
    try:
        tks, meta = A.select_arm("union_greedy", "双色球", pool, 2251, TARGET, None,
                                 seed=0, cap=4, params={"m_eval": M, "m_fit": M})
        rec = {"K": 2251, "池": 90000, "cap": 4, "实际注数": len(tks),
               "放宽次数": meta.get("放宽次数"), "cap污染": meta.get("cap污染"),
               "耗时秒": round(time.time() - t0, 1)}
        print(f"cap=4 K=2251 实际 {len(tks)}/2251 放宽={meta.get('放宽次数')}", flush=True)
    except Exception as e:                                    # noqa: BLE001
        rec = {"K": 2251, "cap": 4, "状态": f"失败 {e}"}
        print(f"cap=4 K=2251 失败 {e}", flush=True)
    rows.append(rec)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
