# -*- coding: utf-8 -*-
"""每注中奖率核查：理论单注任意奖中奖率 vs 实际反馈，含按期数/注数分组

回答「为什么感觉中奖率低」：
- 若按【每注】看，数字型单注任意奖率本就极低（3D/排列3 ≈1%、排列5 =0.001%），
  七星彩/双色球/大乐透也只有 ~6~8%（靠小奖蓝球/后区/第7位凑）。
- 只有按【每期 × 投入注数】看才直观：满 100 注时，双/大/七星彩期望中 6~8 注。
- 早期反馈多为手动 --groups 5 的测试注（每期 5 注），几乎必不中，会放大"偏低"错觉。

用法：E:/Python/python.exe scripts/winrate_check.py
"""

import json
import math
import sys
from collections import Counter
from math import comb
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

try:
    from scipy.stats import binom
except Exception:
    binom = None


def C(n, k):
    return comb(n, k)


def p_ssq():
    p_blue = 1 / 16
    red4 = C(6, 4) * C(27, 2) / C(33, 6)
    red5 = C(6, 5) * C(27, 1) / C(33, 6)
    red6 = C(6, 6) * C(27, 0) / C(33, 6)
    return p_blue + (red4 + red5 + red6) * (15 / 16)


def p_dlt():
    total = C(35, 5) * C(12, 2)
    win = 0
    for i in range(6):
        for j in range(3):
            def prize():
                if (i == 5 and j == 2) or (i == 5 and j == 1) or (i == 5 and j == 0):
                    return True
                if (i == 4 and j == 2) or (i == 4 and j == 1) or (i == 3 and j == 2):
                    return True
                if (i == 4 and j == 0) or (i == 3 and j == 1) or (i == 2 and j == 2):
                    return True
                if (i == 3 and j == 0) or (i == 2 and j == 1) or (i == 1 and j == 2) or (i == 0 and j == 2):
                    return True
                return False
            if prize():
                win += C(5, i) * C(30, 5 - i) * C(2, j) * C(10, 2 - j)
    return win / total


def p_qxc():
    return 1188270 / 15000000  # 六等注数 / 总组合


def p_fc3d():
    return (1 + 3 + 6) / 1000  # 直选+组选3+组选6


def p_pl5():
    return 1 / 100000


THEORY = {
    "双色球": p_ssq(),
    "大乐透": p_dlt(),
    "排列5": p_pl5(),
    "福彩3D": p_fc3d(),
    "排列3": p_fc3d(),
    "七星彩": p_qxc(),
}


def load_valid(lottery):
    p = BASE / "training" / "feedback" / f"{lottery}_feedback_history.json"
    if not p.exists():
        return []
    recs = json.load(open(p, encoding="utf-8"))
    recs = recs if isinstance(recs, list) else recs.get("records", [])
    return [r for r in recs if r.get("valid_prediction", True)]


def main():
    print("=" * 92)
    print("各彩种「每注」任意奖中奖率：理论 vs 实际")
    print("=" * 92)
    print(f"{'彩种':<7}{'有效注数':>8}{'实际中奖':>9}{'实际率':>9}{'理论单注率':>11}"
          f"{'期望中奖':>8}  判断")
    print("-" * 92)
    for name in ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]:
        v = load_valid(name)
        n = len(v)
        won = sum(1 for r in v if r.get("中奖等级") not in ("未中", "", None))
        p0 = THEORY[name]
        exp = n * p0
        tag = "-"
        if n > 20 and binom is not None:
            pl = binom.cdf(won, n, p0)
            ph = 1 - binom.cdf(won - 1, n, p0)
            if min(pl, ph) < 0.01:
                tag = "显著偏离(待查)"
            elif min(pl, ph) < 0.05:
                tag = "偏离(波动内)"
            else:
                tag = "与理论吻合"
        print(f"{name:<7}{n:>8}{won:>9}{won / n * 100 if n else 0:>8.2f}%"
              f"{p0 * 100:>10.3f}%{exp:>8.1f}  {tag}")

    print()
    print("提示：反馈记录为【每注一条】。单注口径下数字型几乎必不中（排列5=0.001%、3D≈1%）；")
    print("要感受真实中奖率请按【每期×注数】看：满 100 注时，双/大/七星彩期望中 ~7 注。")
    print("早期手动 --groups 5 的测试注占多数，会显著放大'几乎不中'的观感。")


if __name__ == "__main__":
    main()
