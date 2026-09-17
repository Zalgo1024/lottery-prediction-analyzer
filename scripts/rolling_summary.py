"""滚动回测结果汇总 + 多重比较校正（只读 training/ 下的 *_rolling 报告）

对每个 (彩种 × 策略) 的逐期命中，做「观测 vs 随机基线」的 z 检验，
再对全部检验做 BH-FDR 校正 —— 防止 24 组里冒出一个 p<0.05 就当成"有效策略"。

用法：E:/Python/python.exe scripts/rolling_summary.py [--periods N]（N 省略则取各彩种最新报告）
输出：控制台 + logs/滚动回测汇总_<日期>.md
"""

import glob
import json
import math
import sys
from datetime import date
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from config import LOTTERY_CONFIG  # noqa: E402
from train.metrics import (  # noqa: E402
    _random_selection_variance,
    compute_random_baseline,
)

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass


def bh_fdr(p_values):
    """Benjamini-Hochberg 校正，返回与输入同序的 q 值"""
    m = len(p_values)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: p_values[i])
    qs = [0.0] * m
    prev = 1.0
    for rank, idx in enumerate(reversed(order), start=1):
        k = m - rank + 1
        prev = min(prev, p_values[idx] * m / k)
        qs[idx] = prev
    return [min(q, 1.0) for q in qs]


def main():
    latest = {}
    for f in glob.glob(str(BASE / "training" / "*_rolling" / "rolling_report.json")):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except Exception:
            continue
        n = d.get("lottery_name")
        if n and (n not in latest or f > latest[n][0]):
            latest[n] = (f, d)

    rows = []
    for name in LOTTERY_CONFIG:
        if name not in latest:
            continue
        f, d = latest[name]
        base = compute_random_baseline(name)["expected_total_hits"]
        var = _random_selection_variance(name)
        det = d.get("predictions_detail", [])
        for s in sorted({p["策略"] for p in det}):
            hits = [p["总命中"] for p in det if p["策略"] == s]
            if not hits:
                continue
            n = len(hits)
            mean = sum(hits) / n
            se = math.sqrt(var / n) if var > 0 else 0.0
            z = (mean - base) / se if se > 0 else 0.0
            p = 0.5 * (1 + math.erf(-z / math.sqrt(2)))  # 单尾（优势方向）
            rows.append({"彩种": name, "策略": s, "期数": n, "观测": mean,
                         "基线": base, "优势": mean - base, "z": z, "p": p,
                         "报告": Path(f).parent.name})

    qs = bh_fdr([r["p"] for r in rows])
    for r, q in zip(rows, qs):
        r["q"] = q
        r["结论"] = ("显著优于随机" if q < 0.05 and r["优势"] > 0
                     else "显著差于随机" if q < 0.05 and r["优势"] < 0
                     else "与随机无显著差异")

    print("=" * 96)
    print(f"滚动回测汇总（BH-FDR 校正）   {date.today()}   检验组数 {len(rows)}")
    print("=" * 96)
    print(f"{'彩种':<7}{'策略':<12}{'期数':>6}{'观测':>8}{'基线':>8}{'优势':>8}{'z':>7}{'p':>9}{'q':>9}  结论")
    print("-" * 96)
    for r in sorted(rows, key=lambda x: (x["彩种"], x["p"])):
        print(f"{r['彩种']:<7}{r['策略']:<12}{r['期数']:>6}{r['观测']:>8.3f}{r['基线']:>8.3f}"
              f"{r['优势']:>+8.3f}{r['z']:>7.2f}{r['p']:>9.4f}{r['q']:>9.4f}  {r['结论']}")

    sig = [r for r in rows if r["q"] < 0.05]
    raw = [r for r in rows if r["p"] < 0.05]
    print()
    print(f"未校正 p<0.05 的组数: {len(raw)}（{len(rows)} 组里出现 {len(raw)} 个属多重比较的必然期望）")
    print(f"BH 校正后 q<0.05 的组数: {len(sig)}")
    if not sig:
        print("→ 结论：没有任何策略在统计上优于闭眼随机，与诚实层 25 条假设全 rejected 一致。")

    lines = [
        f"# 滚动回测汇总（{date.today()}）",
        "",
        f"- 检验组数：{len(rows)}（6 彩种 × 4 策略）",
        f"- 未校正 p<0.05：{len(raw)} 组；**BH-FDR 校正后 q<0.05：{len(sig)} 组**",
        "- 检验口径：单期命中数 vs 随机基线（超几何闭式解），单尾 z 检验",
        "",
        "| 彩种 | 策略 | 期数 | 观测命中 | 随机基线 | 优势 | z | p | q(BH) | 结论 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda x: (x["彩种"], x["p"])):
        lines.append(
            f"| {r['彩种']} | {r['策略']} | {r['期数']} | {r['观测']:.3f} | {r['基线']:.3f} | "
            f"{r['优势']:+.3f} | {r['z']:.2f} | {r['p']:.4f} | {r['q']:.4f} | {r['结论']} |"
        )
    lines.extend([
        "",
        "## 怎么读",
        "",
        "24 组检验里冒出 1 个 p<0.05 完全正常（期望假阳性 ≈ 24×0.05 ≈ 1.2 个）。",
        "BH 校正把最小的那个 p 抬到 q≈0.5 量级，即「在 24 次比较的背景下它毫无说服力」。",
        "这正是项目坚持多重比较校正的原因：不校正的话，任何人都能靠多试几个策略「试出」一个显著结果。",
    ])
    out = BASE / "logs" / f"滚动回测汇总_{date.today().strftime('%Y%m%d')}.md"
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n报告: {out}")


if __name__ == "__main__":
    main()
