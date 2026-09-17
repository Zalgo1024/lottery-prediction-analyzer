# -*- coding: utf-8 -*-
"""红蓝拆分子模型训练入口（双色球/大乐透）。

红球=逐号二分类，蓝球=多分类子模型（双 16 类 / 大 12 类双头选 2），
走前验证分开评估，最后组合成 redblue_split_v1 结构化模型。
训练后 `_get_trained_params` 会优先加载它 → predict/trained 模式自动组合使用。

用法：
    python scripts/blue_split_train.py                        # 双色球 + 大乐透，lightgbm
    python scripts/blue_split_train.py --lottery 双色球        # 单彩种
    python scripts/blue_split_train.py --model logistic --features rich
"""
import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LOTTERY_CONFIG

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def main() -> int:
    ap = argparse.ArgumentParser(description="红蓝拆分子模型训练（双色球/大乐透）")
    ap.add_argument("--lottery", default="全部",
                    help="双色球 | 大乐透 | 全部（默认全部）")
    ap.add_argument("--model", default="lightgbm",
                    choices=("logistic", "random_forest", "lightgbm"))
    ap.add_argument("--window", type=int, default=None, help="滚动窗口（默认取 TRAIN_DEFAULTS）")
    ap.add_argument("--features", default="standard", choices=("standard", "rich"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()

    if args.lottery == "全部":
        scope = ["双色球", "大乐透"]
    elif args.lottery in LOTTERY_CONFIG:
        scope = [args.lottery]
    else:
        print(f"未知彩种：{args.lottery}")
        return 1

    from train.blue_model import train_blue_split

    exit_code = 0
    for name in scope:
        print(f"\n===== {name} 红蓝拆分子模型（{args.model}） =====")
        try:
            res = train_blue_split(
                name, model_type=args.model, window=args.window,
                features=args.features, seed=args.seed,
            )
        except Exception as e:
            print(f"[失败] {name}: {e}")
            exit_code = 1
            continue
        print(f"红球平均命中  : {res['red_mean_hits']:.3f}（随机基线 {res['red_expected_hits']:.3f}，"
              f"Lift {res['red_mean_hits'] / res['red_expected_hits']:.2f}）")
        print(f"蓝球平均命中  : {res['blue_mean_hits']:.3f}（随机基线 {res['blue_expected_hits']:.3f}，"
              f"Lift {res['blue_lift']:.2f}）")
        print(f"蓝球频率基线  : {res['blue_freq_mean_hits']:.3f}（Lift {res['blue_freq_lift']:.2f}）")
        c = res["combined"]
        print(f"组合整票命中  : {c['observed_total_hits']:.3f} vs 随机 {c['expected_total_hits']:.3f}"
              f"（Δ{c['total_advantage']:+.3f}，n={c['n_periods']}）")
        print(f"训练记录目录  : {res['record_dir']}")

    # 顺便把汇总写一份到 output/ 方便翻阅
    print("\n完成。报告在各训练记录目录 report.md。")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
