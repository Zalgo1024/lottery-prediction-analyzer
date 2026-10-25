# -*- coding: utf-8 -*-
"""哨兵恒等式的结构性核对：两臂的 E[单期达标注数] 在**模型分布**下是否不同。

背景：主终点回放里出现 `prod_mix` 的哨兵（单期达标注数均值）比 random 高
0.122 注（z=+2.96，大乐透 K=400）。理论上 `E[nhit] = Σ_i p_i`，而两臂的票
都是同质单注 ⇒ 期望应当**逐位相同**。所以要么是抽样噪声，要么是某处 p 不同。

这里用均匀 MC（30 万期）算两臂在模型分布下的 E[nhit]，与 1e-3 量级的分辨率
对照：若两者在 MC 误差内一致 ⇒ 实测差是抽样噪声；若系统性不同 ⇒ 有结构原因。
"""
from __future__ import annotations

import math
import sys

import numpy as np

sys.path.insert(0, "E:/707")

from ev.arena import arms as A                      # noqa: E402
from ev.arena.design import outcome_gate, _onehot    # noqa: E402
from ev.coverage_backtest import build_pool          # noqa: E402
from ev.profit.rules import rules_for                # noqa: E402
from ev.tier_backtest import load_test_recs          # noqa: E402
from ev.tier_coverage import _inner_pool_size        # noqa: E402

M = 300_000
LOT, K = "大乐透", 400
TARGET = {"kind": "payout", "min_payout": 10.0}


def nhit_mean(tks, R, B, gate):
    n_out, n_in = R.shape[1], B.shape[1]
    A_ = _onehot(tks, "红球", n_out)
    Bm = _onehot(tks, "蓝球", n_in)
    h = (A_.astype(np.int16) @ R.T.astype(np.int16))
    j = (Bm.astype(np.int16) @ B.T.astype(np.int16))
    ok = gate[h, j]                                   # (K, M)
    return ok.sum(axis=0).astype(np.float64)


def main() -> None:
    recs = load_test_recs(LOT)
    issue = recs[-1].期号
    r = rules_for(LOT, issue)
    n_out, n_in = int(r.outer_range[1]), _inner_pool_size(
        r, max(jj for (_h, jj) in r.grade_table))
    gate, _w, _m, _n = outcome_gate(LOT, TARGET, issue)
    print(f"{LOT} issue={issue} 规则 {r.rule_version} n_out={n_out} n_in={n_in}")

    pool = build_pool(LOT, 8000, seed=20261024)
    tks_r, _ = A.select_arm("random", LOT, pool, K, TARGET, issue, seed=0, cap=3)
    tks_p, _ = A.select_arm("prod_mix", LOT, pool, K, TARGET, issue, seed=0, cap=3)
    print(f"random {len(tks_r)} 注 | prod_mix {len(tks_p)} 注")

    rng = np.random.default_rng(20261024)
    # 外层：每期抽 w 个；内层：每期抽 m 个
    w = int(r.outer_width)
    m_in = max(jj for (_h, jj) in r.grade_table)
    oi = np.argsort(rng.random((M, n_out)), axis=1)[:, :w]
    ii = np.argsort(rng.random((M, n_in)), axis=1)[:, :m_in]
    R = np.zeros((M, n_out), dtype=np.int8)
    B = np.zeros((M, n_in), dtype=np.int8)
    np.put_along_axis(R, oi, 1, axis=1)
    np.put_along_axis(B, ii, 1, axis=1)

    nr = nhit_mean(tks_r, R, B, gate)
    np_ = nhit_mean(tks_p, R, B, gate)
    d = np_ - nr
    se_r = nr.std(ddof=1) / math.sqrt(M)
    se_d = d.std(ddof=1) / math.sqrt(M)
    print()
    print(f"模型分布下 E[nhit]：random {nr.mean():.6f} ± {se_r:.6f}"
          f"（解析 K·p = {K}×{ (nr.mean()/K):.8f}）")
    print(f"                  prod_mix {np_.mean():.6f} ± {se_r:.6f}")
    print(f"差 {d.mean():+.6f} ± {se_d:.6f}  ⇒ z = {d.mean()/se_d:+.2f}")
    print()
    print("判读：|z|<4 ⇒ 两臂期望相同，实测里那 0.122 注是抽样噪声；")
    print("      |z|>4 ⇒ 存在结构性差异，须查明哪一处 p 不同。")


if __name__ == "__main__":
    main()
