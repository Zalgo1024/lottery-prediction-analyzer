"""样本量 / 检出能力估算（只读）

回答两个问题：
  1. 想检出「选号比随机多中 δ 个号」的优势，按当前每期 K 注，需要多少期、多少年？
  2. 以现有实时战绩样本，当前能检出的最小优势（MDE）是多少？

口径：
  - 单注单期命中数的方差用 train.metrics._random_selection_variance（超几何闭式解）。
  - 每期 K 注取平均 → 标准误 = sqrt(var / (n_periods × K))。
  - 双侧 α=0.05、power=0.8 → 系数 (z_{0.975} + z_{0.8})² ≈ 7.849。

用法：E:/Python/python.exe scripts/sample_size_estimate.py
输出：控制台 + logs/样本量估算_<日期>.md
"""

import json
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from config import LOTTERY_CONFIG, resolve_groups  # noqa: E402
from train.metrics import (  # noqa: E402
    _random_selection_variance,
    compute_random_baseline,
)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

Z_ALPHA = 1.959964  # 双侧 0.05
Z_BETA = 0.841621  # power 0.8
COEF = (Z_ALPHA + Z_BETA) ** 2

# 每彩种每年开奖期数（由本地数据实测换算，见 scripts/scan_data_integrity.py）
PER_YEAR = {
    "双色球": 148.7,
    "大乐透": 151.5,
    "排列5": 353.6,
    "福彩3D": 354.0,
    "排列3": 353.7,
    "七星彩": 151.9,
}

DELTAS = [0.02, 0.05, 0.10, 0.20]


def feedback_valid_count(lottery: str) -> int:
    p = BASE / "training" / "feedback" / f"{lottery}_feedback_history.json"
    if not p.exists():
        return 0
    try:
        recs = json.load(open(p, encoding="utf-8"))
        recs = recs if isinstance(recs, list) else recs.get("records", [])
        return sum(1 for r in recs if r.get("valid_prediction", True))
    except Exception:
        return 0


def main():
    today = date.today()
    rows = []
    print("=" * 92)
    print(f"样本量 / 检出能力估算   每期注数按彩种（数字型 {resolve_groups('福彩3D')} / 乐透型 {resolve_groups('双色球')}）"
          f"   双侧 α=0.05, power=0.8   {today}")
    print("=" * 92)
    print(f"{'彩种':<7}{'基线命中':>9}{'单注方差':>9}{'现有样本':>9}{'当前MDE':>9}   需要期数(δ=0.02/0.05/0.10/0.20)")
    print("-" * 92)

    for name in LOTTERY_CONFIG:
        var = _random_selection_variance(name)
        K = resolve_groups(name)  # 每期注数按彩种（数字型 5 / 乐透型 100）
        base = compute_random_baseline(name)["expected_total_hits"]
        n_now = feedback_valid_count(name)
        # 现有样本能检出的最小优势（把 n_now 条记录视作 n_now 注，而非期×注）
        mde = (Z_ALPHA + Z_BETA) * (var / max(n_now, 1)) ** 0.5 if n_now else float("nan")
        need = {}
        for d in DELTAS:
            n_periods = COEF * var / (K * d * d)
            need[d] = n_periods
        years = {d: need[d] / PER_YEAR.get(name, 150) for d in DELTAS}
        rows.append({"彩种": name, "基线命中": base, "单注方差": var,
                     "现有有效样本": n_now, "当前MDE": mde,
                     "需要期数": need, "折合年数": years})
        print(f"{name:<7}{base:>9.3f}{var:>9.3f}{n_now:>9d}{mde:>9.3f}   "
              + " / ".join(f"{need[d]:,.0f}" for d in DELTAS))

    print()
    print("折合年数（按各彩种每期注数、每年开奖期数实测）")
    print(f"{'彩种':<7}" + "".join(f"{'δ='+str(d):>14}" for d in DELTAS))
    for r in rows:
        print(f"{r['彩种']:<7}" + "".join(f"{r['折合年数'][d]:>13.1f}y" for d in DELTAS))

    # Markdown 报告
    lines = [
        f"# 样本量与检出能力估算（{today}）",
        "",
        f"- 每期出号注数按彩种（`config.resolve_groups`）：数字型 {resolve_groups('福彩3D')} 注 / 乐透型 {resolve_groups('双色球')} 注",
        "- 双侧 α=0.05、检验效能 0.8；单注命中数方差用超几何闭式解（`train/metrics`）",
        "- 「需要期数」= 想让「选号比随机多中 δ 个号」被判显著所需的开奖期数",
        "",
        "| 彩种 | 随机基线命中 | 单注方差 | 现有有效样本 | 当前可检出最小优势(MDE) |"
        + "".join(f" 需期数δ={d} " for d in DELTAS) + " |",
        "|---|---|---|---|---" + "|" * len(DELTAS) + "|",
    ]
    for r in rows:
        lines.append(
            f"| {r['彩种']} | {r['基线命中']:.3f} | {r['单注方差']:.3f} | {r['现有有效样本']} | "
            f"{r['当前MDE']:.3f} |"
            + "".join(f" {r['需要期数'][d]:,.0f} |" for d in DELTAS)
        )
    lines.extend([
        "",
        "## 折合年数",
        "",
        "| 彩种 |" + "".join(f" δ={d} |" for d in DELTAS) + "",
        "|---|" + "---|" * len(DELTAS),
    ])
    for r in rows:
        lines.append(f"| {r['彩种']} |" + "".join(f" {r['折合年数'][d]:.1f} 年 |" for d in DELTAS))
    lines.extend([
        "",
        "## 怎么读这张表",
        "",
        "- **δ=0.05** 表示「平均每期比闭眼随机多中 0.05 个号码」，对双色球约等于命中率提升 4.3%。",
        "- 数字型（排列5/3D/排列3）每天开奖，年期数约 354，攒样本最快；乐透型每周 3 次，慢 2.4 倍。",
        "- ⚠️ 样本变多**不会改变负 EV**：它只会把「与随机无显著差异」这个结论的置信区间收窄。",
        "  诚实层已用数千期历史验证 MI≈0、25 条 edge 假设全 rejected，扩大样本量是让结论更确定，",
        "  而不是为了「等出一个 edge」。",
    ])
    out = BASE / "logs" / f"样本量估算_{today.strftime('%Y%m%d')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告: {out}")


if __name__ == "__main__":
    main()
