"""
阶段0 回归基线采集脚本（在重构前运行，保存参考输出）。
运行：E:\Python\python.exe scripts/regression_ref.py
注意：仅用于开发期回归校验，不参与生产逻辑。
"""
import json
import random
import datetime
import numpy as np

# 让 random.seed 始终落到固定种子，抵消 predict 内部 random.seed(microsecond) 的随机化，
# 使单策略预测可复现（仅测试用）。
import random as _random
_ORIG_SEED = _random.seed
_random.seed = lambda *a, **k: _ORIG_SEED(12345)

import sys
sys.path.insert(0, r"E:\707")

import config
from data.loader import load_lottery
from data.features import (
    build_feature_vector_v1,
    build_feature_vector_v2,
    build_features,
    feature_dim,
)
import train.engine as te
import prediction.engine as pe

out = {}

for lot in ["双色球", "大乐透"]:
    cfg = config.LOTTERY_CONFIG[lot]
    data = load_lottery(lot)

    win = data.records[:50]
    v1 = build_feature_vector_v1(win, cfg)
    v2 = build_feature_vector_v2(win, cfg)
    bf_X, bf_y = te._build_features(data, 50)
    fd1 = feature_dim(cfg, 1)
    fd2 = feature_dim(cfg, 2)
    Xs, ys = build_features(data.records, 50, lot, 1)
    Xr, yr = build_features(data.records, 50, lot, 2)

    # v1 与 train._build_features 首样本应精确相等（逻辑等价校验）
    bf_head = bf_X[0].tolist() if bf_X.size else []

    lot_out = {
        "v1_len": len(v1),
        "v2_len": len(v2),
        "v1_head": v1[:6],
        "v2_head": v2[:6],
        "bf_X_shape": list(bf_X.shape),
        "bf_y_shape": list(bf_y.shape),
        "bf_X_head": bf_head[:6],
        "v1_eq_bfX0": v1 == bf_head,
        "fd1": fd1,
        "fd2": fd2,
        "Xs_shape": list(Xs.shape),
        "Xr_shape": list(Xr.shape),
        "fd1_eq_Xs_cols": fd1 == (Xs.shape[1] if Xs.size else -1),
        "fd2_eq_Xr_cols": fd2 == (Xr.shape[1] if Xr.size else -1),
    }

    # 单策略预测（固定种子，可复现）
    preds = {}
    for mode in ["high_freq", "missing", "balanced"]:
        random.seed(12345)
        np.random.seed(12345)
        res = pe.predict(lot, groups=5, mode=mode, record_pending=False)
        preds[mode] = [
            {
                "红球": ps["红球"],
                "蓝球": ps["蓝球"],
                "置信度": ps["置信度"],
                "策略": ps["策略"],
            }
            for ps in res["预测号码"]
        ]
    lot_out["preds"] = preds
    out[lot] = lot_out

with open(r"E:\707\scripts\_reg_ref.json", "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

# 校验并汇报
ok = True
for lot in out:
    o = out[lot]
    checks = {
        "v1==train._build_features[0]": o["v1_eq_bfX0"],
        "fd1==Xs.cols": o["fd1_eq_Xs_cols"],
        "fd2==Xr.cols": o["fd2_eq_Xr_cols"],
        "bf_y.cols==fd1": o["bf_y_shape"][1] == o["fd1"],
    }
    print(f"== {lot} ==")
    for k, v in checks.items():
        print(f"  {k}: {v}")
        ok = ok and v
    print(f"  v1_len={o['v1_len']} v2_len={o['v2_len']} fd1={o['fd1']} fd2={o['fd2']}")
    print(f"  preds: high_freq={len(o['preds']['high_freq'])} missing={len(o['preds']['missing'])} balanced={len(o['preds']['balanced'])}")

print("\n基线采集完成，全部核心等价校验:", ok)
