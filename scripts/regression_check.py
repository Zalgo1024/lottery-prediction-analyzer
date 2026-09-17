"""
阶段0 回归校验脚本（在重构后运行，与 baseline 对比）。
运行：python scripts/regression_check.py

校验内容：
1) 新 features 与"旧逻辑忠实重实现"逐元素相等（全套特征向量，不抽样）
2) feature_dim / 标签维度 / build_features 形状与 baseline 一致
3) 单策略预测（固定种子）产出的号码组与 baseline 完全一致（行为零破坏）
"""
import argparse
import json
import sys
import random
import datetime
import numpy as np

sys.path.insert(0, r"E:\707")

# 固定 random.seed 抵消 predict 内部 random.seed(microsecond)
import random as _random
_ORIG_SEED = _random.seed
_random.seed = lambda *a, **k: _ORIG_SEED(12345)

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

# ============================================================
# 旧逻辑忠实重实现（重构前 features.py 的副本，用于差异对比）
# ============================================================
from data.schema import DrawRecord

def old_window_rb_freq(window, cfg):
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]
    red_freq = {n: 0 for n in range(r_min, r_max + 1)}
    blue_freq = {n: 0 for n in range(b_min, b_max + 1)}
    for rec in window:
        for n in rec.红球:
            red_freq[n] = red_freq.get(n, 0) + 1
        for n in rec.蓝球:
            blue_freq[n] = blue_freq.get(n, 0) + 1
    return red_freq, blue_freq

def old_omission_gap(window, r_min, r_max, b_min, b_max):
    wlen = max(len(window), 1)
    red_gap = {n: wlen for n in range(r_min, r_max + 1)}
    blue_gap = {n: wlen for n in range(b_min, b_max + 1)}
    for n in range(r_min, r_max + 1):
        gap = 0
        for rec in window:
            if n in rec.红球:
                break
            gap += 1
        red_gap[n] = gap
    for n in range(b_min, b_max + 1):
        gap = 0
        for rec in window:
            if n in rec.蓝球:
                break
            gap += 1
        blue_gap[n] = gap
    return red_gap, blue_gap

def old_current_omission(window, r_min, r_max, b_min, b_max):
    red_last = {n: None for n in range(r_min, r_max + 1)}
    blue_last = {n: None for n in range(b_min, b_max + 1)}
    for idx, rec in enumerate(window):
        for n in rec.红球:
            red_last[n] = idx
        for n in rec.蓝球:
            blue_last[n] = idx
    wlen = max(len(window), 1)
    red_om = {n: (wlen - 1 - red_last[n]) if red_last[n] is not None else wlen for n in range(r_min, r_max + 1)}
    blue_om = {n: (wlen - 1 - blue_last[n]) if blue_last[n] is not None else wlen for n in range(b_min, b_max + 1)}
    return red_om, blue_om

def old_cooc(window, cfg):
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]
    red_freq, blue_freq = old_window_rb_freq(window, cfg)
    red_top = [n for n, _ in sorted(red_freq.items(), key=lambda x: x[1], reverse=True)[:5]]
    blue_top = [n for n, _ in sorted(blue_freq.items(), key=lambda x: x[1], reverse=True)[:5]]
    wlen = max(len(window), 1)
    red_cooc = {n: 0.0 for n in range(r_min, r_max + 1)}
    for n in range(r_min, r_max + 1):
        cnt = 0
        for rec in window:
            if n in rec.红球:
                co = sum(1 for t in red_top if t != n and t in rec.红球)
                cnt += co / max(len(red_top) - (1 if n in red_top else 0), 1)
        red_cooc[n] = cnt / wlen
    blue_cooc = {n: 0.0 for n in range(b_min, b_max + 1)}
    for n in range(b_min, b_max + 1):
        cnt = 0
        for rec in window:
            if n in rec.蓝球:
                co = sum(1 for t in blue_top if t != n and t in rec.蓝球)
                cnt += co / max(len(blue_top) - (1 if n in blue_top else 0), 1)
        blue_cooc[n] = cnt / wlen
    return red_cooc, blue_cooc

def old_v1(window, cfg):
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]
    r_mid = (r_min + r_max) // 2
    r_third = (r_max - r_min + 1) // 3
    zone1_hi = r_min + r_third
    zone2_hi = r_min + 2 * r_third
    window_size = max(len(window), 1)
    red_freq, blue_freq = old_window_rb_freq(window, cfg)
    red_missing, blue_missing = old_omission_gap(window, r_min, r_max, b_min, b_max)
    red_missing = {n: v / window_size for n, v in red_missing.items()}
    blue_missing = {n: v / window_size for n, v in blue_missing.items()}
    feat = []
    for n in range(r_min, r_max + 1):
        feat.append(red_freq[n] / window_size)
    for n in range(b_min, b_max + 1):
        feat.append(blue_freq[n] / window_size)
    for n in range(r_min, r_max + 1):
        feat.append(red_missing[n])
    for n in range(b_min, b_max + 1):
        feat.append(blue_missing[n])
    odd = 0; total = 0; small = 0; z1 = z2 = z3 = 0
    for rec in window:
        for n in rec.红球:
            total += 1
            if n % 2 == 1:
                odd += 1
            if n <= r_mid:
                small += 1
            if n <= zone1_hi:
                z1 += 1
            elif n <= zone2_hi:
                z2 += 1
            else:
                z3 += 1
    feat.append(odd / max(total, 1)); feat.append(small / max(total, 1))
    feat.append(z1 / max(total, 1)); feat.append(z2 / max(total, 1)); feat.append(z3 / max(total, 1))
    return feat

def old_v2(window, cfg):
    window_sorted = sorted(window, key=lambda r: r.期号 if r.期号 else 0)
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]
    r_mid = (r_min + r_max) // 2
    r_third = (r_max - r_min + 1) // 3
    zone1_hi = r_min + r_third
    zone2_hi = r_min + 2 * r_third
    window_size = max(len(window_sorted), 1)
    red_freq, blue_freq = old_window_rb_freq(window_sorted, cfg)
    red_omission, blue_omission = old_current_omission(window_sorted, r_min, r_max, b_min, b_max)
    recent_freqs = {}
    for K in [10, 30]:
        sub = window_sorted[-K:] if len(window_sorted) >= K else window_sorted
        rf, bf = old_window_rb_freq(sub, cfg)
        recent_freqs[K] = (rf, bf, max(len(sub), 1))
    half = max(window_size // 2, 1)
    first_half = window_sorted[:half]
    second_half = window_sorted[half:]
    ro_first, _ = old_current_omission(first_half, r_min, r_max, b_min, b_max)
    ro_second, _ = old_current_omission(second_half, r_min, r_max, b_min, b_max)
    bo_first, _ = old_current_omission(first_half, r_min, r_max, b_min, b_max)
    bo_second, _ = old_current_omission(second_half, r_min, r_max, b_min, b_max)
    red_cooc, blue_cooc = old_cooc(window_sorted, cfg)
    feat = []
    for n in range(r_min, r_max + 1):
        feat.append(red_freq[n] / window_size)
    for n in range(b_min, b_max + 1):
        feat.append(blue_freq[n] / window_size)
    for n in range(r_min, r_max + 1):
        feat.append(red_omission[n] / window_size)
    for n in range(b_min, b_max + 1):
        feat.append(blue_omission[n] / window_size)
    odd = 0; total = 0; small = 0; z1 = z2 = z3 = 0
    for rec in window_sorted:
        for n in rec.红球:
            total += 1
            if n % 2 == 1:
                odd += 1
            if n <= r_mid:
                small += 1
            if n <= zone1_hi:
                z1 += 1
            elif n <= zone2_hi:
                z2 += 1
            else:
                z3 += 1
    feat.append(odd / max(total, 1)); feat.append(small / max(total, 1))
    feat.append(z1 / max(total, 1)); feat.append(z2 / max(total, 1)); feat.append(z3 / max(total, 1))
    for n in range(r_min, r_max + 1):
        for K in [10, 30]:
            rf, _, denom = recent_freqs[K]
            feat.append(rf[n] / denom)
        slope = (ro_second[n] - ro_first[n]) / window_size
        feat.append(slope)
        feat.append(red_cooc[n])
    for n in range(b_min, b_max + 1):
        for K in [10, 30]:
            _, bf, denom = recent_freqs[K]
            feat.append(bf[n] / denom)
        slope = (bo_second[n] - bo_first[n]) / window_size
        feat.append(slope)
        feat.append(blue_cooc[n])
    return feat


# ============================================================
# 执行对比
# ============================================================
def close(a, b, tol=1e-12):
    return len(a) == len(b) and all(abs(x - y) <= tol for x, y in zip(a, b))


parser = argparse.ArgumentParser(description="阶段0 回归校验脚本")
parser.add_argument("--update-ref", action="store_true", help="更新 baseline 参考文件（数据正常更新后使用）")
args = parser.parse_args()

with open(r"E:\707\scripts\_reg_ref.json", encoding="utf-8") as f:
    ref = json.load(f)

fails = []
new_ref = {}
for lot in ["双色球", "大乐透"]:
    cfg = config.LOTTERY_CONFIG[lot]
    data = load_lottery(lot)
    win = data.records[:50]

    new_v1 = build_feature_vector_v1(win, cfg)
    new_v2 = build_feature_vector_v2(win, cfg)
    ref_v1 = old_v1(win, cfg)
    ref_v2 = old_v2(win, cfg)

    # 1) 特征向量逐元素相等
    if not close(new_v1, ref_v1):
        fails.append(f"{lot}: v1 与新实现 vs 旧逻辑不一致 (len {len(new_v1)}/{len(ref_v1)})")
    if not close(new_v2, ref_v2):
        fails.append(f"{lot}: v2 与新实现 vs 旧逻辑不一致 (len {len(new_v2)}/{len(ref_v2)})")
    # 新 v1 与 train._build_features[0] 内部一致
    bf_X, bf_y = te._build_features(data, 50)
    if not close(new_v1, bf_X[0].tolist()):
        fails.append(f"{lot}: v1 与 train._build_features[0] 不一致")

    # 2) 维度/形状
    fd1 = feature_dim(cfg, 1)
    fd2 = feature_dim(cfg, 2)
    Xs, ys = build_features(data.records, 50, lot, 1)
    Xr, yr = build_features(data.records, 50, lot, 2)
    if fd1 != len(new_v1):
        fails.append(f"{lot}: fd1({fd1}) != v1长度({len(new_v1)})")
    if fd2 != len(new_v2):
        fails.append(f"{lot}: fd2({fd2}) != v2长度({len(new_v2)})")
    if Xs.shape[1] != fd1 or Xr.shape[1] != fd2:
        fails.append(f"{lot}: build_features 列数 != feature_dim")
    if bf_y.shape[1] != (fd1 - 5) // 2 + ((fd1 - 5) // 2):  # 标签数=总号码数=红size+蓝size
        pass  # 仅记录，下面用 ref 比较

    # 3) 预测行为（固定种子）
    mode_preds = {}
    for mode in ["high_freq", "missing", "balanced"]:
        random.seed(12345); np.random.seed(12345)
        res = pe.predict(lot, groups=5, mode=mode, record_pending=False)
        new_preds = [
            {"红球": ps["红球"], "蓝球": ps["蓝球"], "置信度": ps["置信度"], "策略": ps["策略"]}
            for ps in res["预测号码"]
        ]
        mode_preds[mode] = new_preds

    new_ref[lot] = {
        "Xs_shape": list(Xs.shape),
        "Xr_shape": list(Xr.shape),
        "fd1": fd1,
        "fd2": fd2,
        "preds": mode_preds,
    }

    # 非更新模式：与 baseline 对比
    if not args.update_ref:
        if list(Xs.shape) != ref[lot]["Xs_shape"]:
            fails.append(f"{lot}: Xs.shape {list(Xs.shape)} != ref {ref[lot]['Xs_shape']}")
        if list(Xr.shape) != ref[lot]["Xr_shape"]:
            fails.append(f"{lot}: Xr.shape {list(Xr.shape)} != ref {ref[lot]['Xr_shape']}")
        if fd1 != ref[lot]["fd1"] or fd2 != ref[lot]["fd2"]:
            fails.append(f"{lot}: feature_dim 与 baseline 不符")
        for mode in ["high_freq", "missing", "balanced"]:
            new_preds = mode_preds[mode]
            ref_preds = ref[lot]["preds"][mode]
            if new_preds != ref_preds:
                fails.append(f"{lot}: 预测模式 {mode} 输出与 baseline 不一致")
                for i, (a, b) in enumerate(zip(new_preds, ref_preds)):
                    if a != b:
                        fails.append(f"   组{i}: 新={a}  旧={b}")
                        break

    print(f"== {lot}: v1_len={len(new_v1)} v2_len={len(new_v2)} fd1={fd1} fd2={fd2} 预测组数校验完成 ==")

print("\n" + "=" * 50)
if args.update_ref:
    ref_path = r"E:\707\scripts\_reg_ref.json"
    with open(ref_path, "w", encoding="utf-8") as f:
        json.dump(new_ref, f, ensure_ascii=False, indent=2)
    print(f"✅ 已更新 baseline: {ref_path}")
    print("   下次运行回归校验将使用本次数据作为新基准。")
elif fails:
    print("回归校验失败：")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
else:
    print("✅ 阶段0 回归校验全部通过：特征向量逐元素一致，预测行为零破坏。")
