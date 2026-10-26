# -*- coding: utf-8 -*-
"""探针 2：把候选池按 K 放大后，覆盖效率还退化吗？

探针 1 的池固定 8000：K=2251 时要从 8000 张里挑 2251 张（28%），
贪心几乎没有选择余地 ⇒ 测到的「效率退化」可能只是**池耗竭**，
不是覆盖极限。本探针把池按 40×K 放大，把两个解释分开。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from ev.arena import arms as A                                    # noqa: E402
from ev.coverage_backtest import build_pool                       # noqa: E402
from ev.tier_coverage import exact_union_probability              # noqa: E402
from ev import tier_target as TT                                  # noqa: E402

TARGET = {"kind": "grade", "grade": "四等"}
OUT = Path("output") / "_probe_large_k2.json"
M = 20000


def main() -> int:
    rows = []
    res = TT.resolve_conditions("双色球", TARGET, None)
    p1 = TT.single_ticket_probability("双色球", res["conditions"], None)
    print(f"单注 p={p1:.6e}  MC={M}", flush=True)
    for K in (500, 2251):
        pool_n = max(8000, 40 * K)
        t0 = time.time()
        pool = build_pool("双色球", pool_n, seed=20261024)
        t_pool = time.time() - t0
        t1 = time.time()
        try:
            tks, meta = A.select_arm("union_greedy", "双色球", pool, K, TARGET, None,
                                     seed=0, cap=3,
                                     params={"m_eval": M, "m_fit": M})
        except Exception as e:                                # noqa: BLE001
            rows.append({"K": K, "池": pool_n, "状态": f"选号失败 {e}"})
            print(f"K={K} 池={pool_n} 选号失败 {e}", flush=True)
            continue
        t_sel = time.time() - t1
        t2 = time.time()
        P = exact_union_probability("双色球", tks, target=TARGET)
        t_enum = time.time() - t2
        kp = min(1.0, K * p1)
        row = {"K": K, "池": pool_n, "池建秒": round(t_pool, 1), "注数": len(tks),
               "选号秒": round(t_sel, 1), "枚举秒": round(t_enum, 1), "精确P": P,
               "Kp": kp, "效率": P / kp if kp else None,
               "随机基线": 1.0 - (1.0 - p1) ** K}
        rows.append(row)
        print(f"K={K} 池={pool_n} 注数={len(tks)} 选号 {t_sel:.1f}s 枚举 {t_enum:.1f}s "
              f"P={P:.6f} eff={row['效率']:.4f} 随机={row['随机基线']:.6f}", flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
