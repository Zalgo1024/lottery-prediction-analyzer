# -*- coding: utf-8 -*-
"""生产口径实测：按当前生效注数跑一次完整出号，量覆盖选择的真实成本与收益。

★ `record_pending=False` —— 绝不写生产 pending（生产 pending 只由调度器写）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from config import resolve_groups                              # noqa: E402
from prediction import engine                                  # noqa: E402

OUT = Path("output") / "_coverage_prod_measured.json"


def main() -> int:
    rows = []
    for lot in ("双色球",):
        K = int(resolve_groups(lot))
        t0 = time.time()
        r = engine.predict(lot, groups=K, record_pending=False)
        dt = time.time() - t0
        cov = r.get("覆盖优化") or {}
        rec = {"彩种": lot, "生效注数": K, "号码组数": r["号码组数"],
               "目标注数": r.get("目标注数"), "总耗时秒": round(dt, 1),
               "覆盖": {k: cov.get(k) for k in
                        ("是否替换", "选中臂", "选中精确P", "随机独立基线",
                         "理论上限Kp", "覆盖率效率", "对比随机基线", "耗时秒",
                         "候选池", "降级原因")},
               "候选臂": {k: {kk: v.get(kk) for kk in ("精确P", "状态", "原因")}
                        for k, v in (cov.get("候选臂") or {}).items()}}
        rows.append(rec)
        print(json.dumps(rec, ensure_ascii=False, indent=1), flush=True)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    print("done", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
