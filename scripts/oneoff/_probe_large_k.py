# -*- coding: utf-8 -*-
"""探针：大 K 下选号与精确枚举的墙钟成本。

只回答一个问题：K 从 200 拉到 2251 时，`select_arm` 与
`exact_union_probability` 还跑得动吗？跑不动就先砍网格，别等主跑崩。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ev.arena import arms as A                                    # noqa: E402
from ev.coverage_backtest import build_pool                       # noqa: E402
from ev.tier_coverage import exact_union_probability              # noqa: E402
from ev import tier_target as TT                                  # noqa: E402

TARGET = {"kind": "grade", "grade": "四等"}
OUT = Path("output") / "_probe_large_k.json"


def main() -> int:
    rows = []
    pool_big = build_pool("双色球", 8000, seed=20261026)
    res = TT.resolve_conditions("双色球", TARGET, None)
    p1 = TT.single_ticket_probability("双色球", res["conditions"], None)
    print(f"单注 p={p1:.6e}", flush=True)
    for K in (200, 500, 1000, 2251):
        for arm in ("union_greedy",):
            t0 = time.time()
            try:
                tks, meta = A.select_arm(arm, "双色球", pool_big, K, TARGET, None,
                                         seed=20261026, cap=5,
                                         params={"m_eval": 400, "m_fit": 400})
            except Exception as e:                                # noqa: BLE001
                rows.append({"K": K, "臂": arm, "状态": f"选号失败 {type(e).__name__}: {e}"})
                print(f"K={K} {arm} 选号失败 {e}", flush=True)
                continue
            t_sel = time.time() - t0
            t1 = time.time()
            try:
                P = exact_union_probability("双色球", tks, target=TARGET)
            except Exception as e:                                # noqa: BLE001
                P = None
                print(f"K={K} 枚举失败 {e}", flush=True)
            t_enum = time.time() - t1
            kp = min(1.0, K * p1)
            row = {"K": K, "臂": arm, "注数": len(tks), "选号秒": round(t_sel, 1),
                   "枚举秒": round(t_enum, 1), "精确P": P,
                   "Kp": kp, "效率": (P / kp) if (P and kp > 0) else None,
                   "随机基线": 1.0 - (1.0 - p1) ** K}
            rows.append(row)
            print(f"K={K} {arm} 注数={len(tks)} 选号 {t_sel:.1f}s 枚举 {t_enum:.1f}s "
                  f"P={P} eff={row['效率']}", flush=True)
            if t_sel + t_enum > 900:
                print("★ 单格超过 900s，后续网格需砍", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
