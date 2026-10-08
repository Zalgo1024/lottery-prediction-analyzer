"""
阶段0 回归基线采集脚本（在重构前运行，保存参考输出）。
运行：python scripts/regression_ref.py
注意：仅用于开发期回归校验，不参与生产逻辑。

★数据窗口钉死（2026-10-08）
采集时把「当时的最新期号」写进 baseline `_meta.pinned_issue`，
校验端（regression_check.py）据此把输入截断到同一窗口。
否则 CSV 每追加一期，行数与预测输出就变一次，回归校验必然误报。
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

import os
import sys

# 项目根 / scripts 目录按脚本位置推导，不再写死绝对路径（换目录/换机器直接可用）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, SCRIPTS_DIR)

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
import regression_pin as rp

LOTS = ["双色球", "大乐透"]
out = {}
pins = {}

# ---------- 第一遍：确定 pinned 期号 + 算特征（此时还不触发 predict） ----------
for lot in LOTS:
    cfg = config.LOTTERY_CONFIG[lot]
    full = load_lottery(lot)
    pin = max((getattr(r, "期号", None) for r in full.records),
              key=rp.issue_num, default=None)
    pins[lot] = pin
    data = rp.truncate_data(full, pin)

    win = data.records[:50]
    v1 = build_feature_vector_v1(win, cfg)
    v2 = build_feature_vector_v2(win, cfg)
    bf_X, bf_y = te._build_features(data, 50)
    fd1 = feature_dim(cfg, 1)
    fd2 = feature_dim(cfg, 2)
    Xs, ys = build_features(data.records, 50, lot, 1)
    Xr, yr = build_features(data.records, 50, lot, 2)

    # v1 与 train._build_features 首样本应精确相等（逻辑等价校验）
    # ⚠️ 窗口口径：build_features 样本 0 的窗口是 records[1:1+ws]（2026-08-31 时间方向修正后），
    #    不是 records[:ws]。必须用同一个窗口构建 v1 才能比（否则恒 False）。
    ws = 50
    v1_on_bf_window = build_feature_vector_v1(data.records[1:1 + ws], cfg)
    bf_head = bf_X[0].tolist() if bf_X.size else []

    # 标签是「每个号码一个二分类」，列数 = 号码总数(红+蓝)，
    # 与特征维数 fd1 无关（2026-10-08 修：原先拿 bf_y.cols 比 fd1，恒 False）
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]
    n_numbers = (r_max - r_min + 1) + (b_max - b_min + 1)

    lot_out = {
        "n_numbers": n_numbers,
        "v1_len": len(v1),
        "v2_len": len(v2),
        "v1_head": v1[:6],
        "v2_head": v2[:6],
        "bf_X_shape": list(bf_X.shape),
        "bf_y_shape": list(bf_y.shape),
        "bf_X_head": bf_head[:6],
        "v1_eq_bfX0": v1_on_bf_window == bf_head,
        "fd1": fd1,
        "fd2": fd2,
        "Xs_shape": list(Xs.shape),
        "Xr_shape": list(Xr.shape),
        "fd1_eq_Xs_cols": fd1 == (Xs.shape[1] if Xs.size else -1),
        "fd2_eq_Xr_cols": fd2 == (Xr.shape[1] if Xr.size else -1),
    }

    out[lot] = lot_out

# ---------- 第二遍：钉死 predict 的输入窗口后再预测 ----------
# prediction/engine.py 顶层是 `from data.loader import load_lottery`，
# 必须 patch 它自己的属性（patch data.loader 对已导入的名字无效）
rp.install_pinned_loader(pe, pins)

for lot in LOTS:
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
    out[lot]["preds"] = preds

out[rp.META_KEY] = {
    "pinned_issue": pins,
    "generated_at": datetime.datetime.now().isoformat(timespec="seconds"),
    "note": (
        "preds 与 Xs_shape 均在上述 pinned 期号（各彩种当时最新期号）下采集；"
        "regression_check.py 会把输入截断到同一窗口后再比对，"
        "故 CSV 后续追加期次不会误报回归。"
    ),
}

with open(os.path.join(SCRIPTS_DIR, "_reg_ref.json"), "w", encoding="utf-8") as f:
    json.dump(out, f, ensure_ascii=False, indent=2)

# 校验并汇报
ok = True
for lot in LOTS:
    o = out[lot]
    checks = {
        "v1==train._build_features[0]": o["v1_eq_bfX0"],
        "fd1==Xs.cols": o["fd1_eq_Xs_cols"],
        "fd2==Xr.cols": o["fd2_eq_Xr_cols"],
        "bf_y.cols==号码总数(红+蓝)": o["bf_y_shape"][1] == o["n_numbers"],
    }
    print(f"== {lot} ==")
    for k, v in checks.items():
        print(f"  {k}: {v}")
        ok = ok and v
    print(f"  v1_len={o['v1_len']} v2_len={o['v2_len']} fd1={o['fd1']} fd2={o['fd2']}")
    print(f"  preds: high_freq={len(o['preds']['high_freq'])} missing={len(o['preds']['missing'])} balanced={len(o['preds']['balanced'])}")

print(f"\n  数据窗口 pinned_issue = {pins}（已写入 baseline _meta，校验端按此截断）")
print("基线采集完成，全部核心等价校验:", ok)
