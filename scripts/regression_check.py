"""
阶段0 回归校验脚本（在重构后运行，与 baseline 对比）。
运行：python scripts/regression_check.py

校验内容：
1) 新 features 与"旧逻辑忠实重实现"逐元素相等（全套特征向量，不抽样）
2) feature_dim / 标签维度 / build_features 形状与 baseline 一致
3) 单策略预测（固定种子）产出的号码组与 baseline 完全一致（行为零破坏）

★数据窗口钉死（2026-10-08）
baseline 的 Xs_shape 行数与 preds 都依赖采集当天的 CSV 快照，而 CSV 每期追加。
不钉死的话，数据一前进就必然报"回归失败"——那是数据在动，不是代码坏了；
而每次失败都去 --update-ref 刷新基准，会把当前行为固化成新基准，掩盖真回归。
现按 baseline `_meta.pinned_issue` 把输入截断到当时的最新期号，使校验可复现。
"""
import argparse
import json
import os
import sys
import random
import datetime
import numpy as np

# 项目根 / scripts 目录按脚本位置推导，不再写死绝对路径（换目录/换机器直接可用）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)
sys.path.insert(0, SCRIPTS_DIR)

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
import regression_pin as rp

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

REF_PATH = os.path.join(SCRIPTS_DIR, "_reg_ref.json")
with open(REF_PATH, encoding="utf-8") as f:
    ref = json.load(f)

pins = rp.read_pins(ref)
if pins is None and not args.update_ref:
    print("❌ baseline 为旧格式（缺 _meta.pinned_issue），无法确定它对应的数据窗口，")
    print("   直接按当前全量数据比对必然因「数据前进」而误报回归。")
    print("   请执行一次基线采集写入 pin：  python scripts/regression_ref.py")
    print("   或显式重设基准：              python scripts/regression_check.py --update-ref")
    raise SystemExit(2)

# 钉死 predict 的输入窗口（prediction/engine.py 顶层 `from data.loader import load_lottery`，
# 必须 patch 它自己的属性，patch data.loader 对已导入的名字无效）
rp.install_pinned_loader(pe, pins)

fails = []
new_ref = {}
update_pins = {}
for lot in ["双色球", "大乐透"]:
    cfg = config.LOTTERY_CONFIG[lot]
    full = load_lottery(lot)
    pin = (pins or {}).get(lot)
    # 更新基准时 pin 取当前最新期号；校验时沿用 baseline 记录的期号
    update_pins[lot] = pin if pin is not None else (full.records[0].期号 if full.records else None)
    data = rp.truncate_data(full, pin)
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
    # ⚠️ 窗口口径（2026-10-08 修）：build_features 第 i 个样本取
    #    window = records[i+1 : i+1+ws]、target = records[i]
    #    —— 这是 2026-08-31「时间方向」修正后的语义（用过去 ws 期预测下一期）。
    #    所以样本 0 的窗口是 records[1:1+ws]，**不是** records[:ws]。
    #    拿 records[:50] 去比 bf_X[0] 属于窗口错位，会恒定失败（实测 max|diff|=1.0）。
    WS = 50
    bf_X, bf_y = te._build_features(data, WS)
    v1_on_bf_window = build_feature_vector_v1(data.records[1:1 + WS], cfg)
    if not close(v1_on_bf_window, bf_X[0].tolist()):
        fails.append(f"{lot}: v1 与 train._build_features[0] 不一致（窗口 records[1:1+{WS}]）")

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
    # 标签是「每个号码一个二分类」，列数应等于号码总数(红+蓝)，与特征维数 fd1 无关。
    # （2026-10-08：原写法是 `if ...: pass` 的死代码且公式错，等于没校验，现改为真断言）
    r_min, r_max = cfg["red_range"]
    b_min, b_max = cfg["blue_range"]
    n_numbers = (r_max - r_min + 1) + (b_max - b_min + 1)
    if bf_y.shape[1] != n_numbers:
        fails.append(f"{lot}: bf_y 列数 {bf_y.shape[1]} != 号码总数 {n_numbers}")

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

    pin_note = f" 数据窗口 pinned≤{pin}" if pin is not None else " 数据窗口=当前全量"
    print(f"== {lot}: v1_len={len(new_v1)} v2_len={len(new_v2)} fd1={fd1} fd2={fd2}"
          f" Xs{list(Xs.shape)} |{pin_note} ==")

print("\n" + "=" * 50)
if args.update_ref:
    new_ref[rp.META_KEY] = {
        "pinned_issue": update_pins,
        "note": "preds/Xs_shape 均在此 pinned 期号下采集；校验时输入被截断到同一窗口，故数据追加不会误报回归。",
    }
    with open(REF_PATH, "w", encoding="utf-8") as f:
        json.dump(new_ref, f, ensure_ascii=False, indent=2)
    print(f"✅ 已更新 baseline: {REF_PATH}")
    print(f"   数据窗口 pinned_issue = {update_pins}（校验时按此截断，数据追加不再误报）")
elif fails:
    print("回归校验失败：")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
else:
    print("✅ 阶段0 回归校验全部通过：特征向量逐元素一致，预测行为零破坏。")
