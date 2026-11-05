# -*- coding: utf-8 -*-
"""K 网格：精确贪心 / 代理贪心 / 真随机 的**精确并集概率**对比。

用法：
    E:/Python/python.exe scripts/tier_k_grid.py                    # 双色球，默认档
    E:/Python/python.exe scripts/tier_k_grid.py --lottery 大乐透 --seeds 5

产出：控制台 markdown 表 + `logs/tier_k_grid_<彩种>.json`（供决策规则消费）。

口径纪律：
· 概率一律用 `exact_union_probability`（外层×内层全枚举）复算，**不信任何臂自报的数**；
  模块自报值与复核值并列打印，差值不为 0 就直接报错。
· Δ 与 z 都是相对**真随机（多种子）**，不是相对 iid 模型 —— iid 只是参照线。
· 每行都带 K 与 K·p：报覆盖收益不报 K 是无意义的（可争取空间随 K 变大再变小）。
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import numpy as np                      # noqa: E402

import data.feedback as fb              # noqa: E402
from ev import tier_exact_decision as DEC   # noqa: E402
from ev import tier_exact_greedy as G       # noqa: E402
from ev.tier_coverage import (              # noqa: E402
    _eq_matrix, exact_union_probability, select_tier_coverage,
)

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "logs")


def _pool(lottery: str, mode: str = "pending", size: int = 2000):
    """候选池。

    `pending`：当期 pending 的生产批（500/400 注）—— **这是输出批，不是候选池**。
                K 接近池规模时「选号」退化成「全选」，优化器没有自由度。
    `gen`    ：与生产同源的候选池（`ev.coverage_backtest.build_pool`，预注册写的是 8000）。
                这才是选号算法真正面对的池。
    """
    if mode == "gen":
        from ev.coverage_backtest import build_pool
        from prediction.coverage_production import _stable_seed
        prod = fb.latest_production_pending(fb.load_pending(lottery))
        issue = (prod or {}).get("目标期号")
        seed = _stable_seed(lottery, issue)
        return list(build_pool(lottery, int(size), seed=seed)), issue
    prod = fb.latest_production_pending(fb.load_pending(lottery))
    tks = (prod or {}).get("预测号码") or []
    return list(tks), (prod or {}).get("目标期号")


def _random_arm(pool, K, cap, seed, relax=False):
    """同池同 cap 的真随机（按种子打乱后顺序收下满足 cap 的票）。

    `relax=True` 时与精确臂用同一套阶梯放宽策略 —— 否则 K 大时一方少选、
    另一方选满，比出来的差值毫无意义（实测：431 注 vs 500 注 会被读成"算法更差"）。
    """
    from ev.tier_coverage import RELAX_MAX
    V = _eq_matrix(pool, lottery_global)
    rng = np.random.default_rng(int(seed))
    idx, mx = [], np.zeros(len(pool), dtype=np.int16)
    cur, relax_n = int(cap), 0
    for i in rng.permutation(len(pool)):
        if mx[i] <= cur:
            idx.append(int(i))
            mx = np.maximum(mx, V @ V[i])
            if len(idx) == K:
                break
        elif relax and cur - cap < RELAX_MAX:
            cur += 1
            relax_n += 1
    return [pool[i] for i in idx], relax_n


def main() -> int:
    global lottery_global
    ap = argparse.ArgumentParser()
    ap.add_argument("--lottery", default="双色球")
    ap.add_argument("--target-grade", default="四等")
    ap.add_argument("--seeds", type=int, default=5)
    ap.add_argument("--cap", type=int, default=3)
    ap.add_argument("--ks", default="50,122,200,354,500")
    ap.add_argument("--pool-mode", choices=("pending", "gen"), default="pending",
                    help="pending=当期输出批（会退化）；gen=生产同源候选池")
    ap.add_argument("--pool-size", type=int, default=2000)
    ap.add_argument("--relax", action="store_true",
                    help="cap 阶梯放宽（两条臂同一策略）；会引入 cap 污染，单列标注")
    args = ap.parse_args()

    lottery_global = args.lottery
    pool, issue = _pool(lottery_global, args.pool_mode, args.pool_size)
    if not pool:
        print(f"{lottery_global}: 没有生产 pending 批，无法测")
        return 1
    tgt = {"kind": "grade", "grade": args.target_grade}
    ks = [int(x) for x in args.ks.split(",") if x.strip()]
    ks = [k for k in ks if k <= len(pool)]

    print(f"=== {lottery_global} · 目标 ≥{args.target_grade} · 候选池 {len(pool)} 注"
          f"（{args.pool_mode}，期号 {issue}）· cap={args.cap} · 随机 {args.seeds} seed ===")
    p1 = G.exact_union_from_ctx(G._Ctx(lottery_global, tgt, None), [pool[0]])
    print(f"单注达标概率 p = {p1:.6e}\n")

    rows = []
    for K in ks:
        note = DEC.describe_pool_ratio(len(pool), K)
        p_exact, m = G.select_exact_tier(lottery_global, pool, K, tgt, cap=args.cap,
                                         relax=args.relax)
        p_ex = m["精确达标签概率"]
        ref_ex = exact_union_probability(lottery_global, p_exact, target=tgt)
        if abs(ref_ex - p_ex) > 1e-15:
            raise SystemExit(f"★ K={K} 精确贪心自报 {p_ex!r} 与复核 {ref_ex!r} 不一致")
        t_proxy = time.time()
        p_proxy_tks, _ = select_tier_coverage(lottery_global, pool, K, tgt,
                                              cap=args.cap, seed=0)
        p_proxy = exact_union_probability(lottery_global, p_proxy_tks, target=tgt)
        t_proxy = time.time() - t_proxy
        rand_pairs = [_random_arm(pool, K, args.cap, s, relax=args.relax)
                      for s in range(args.seeds)]
        rnd = [exact_union_probability(lottery_global, tks_s, target=tgt)
               for tks_s, _ in rand_pairs]
        rnd_m, rnd_sd = float(np.mean(rnd)), float(np.std(rnd, ddof=1)) if len(rnd) > 1 else 0.0
        kp = p1 * K
        iid = 1.0 - (1.0 - p1) ** K
        head = min(1.0, kp) - iid
        short = m.get("容量不足")
        d_pp = (p_ex - rnd_m) * 100.0
        rows.append({
            "K": K, "K/池": round(K / len(pool), 3), "K·p": round(kp, 4),
            "iid": iid, "随机均值": rnd_m, "随机sd": rnd_sd,
            "代理": p_proxy, "精确": p_ex,
            "Δ_pp": None if short else round(d_pp, 4),
            "不可比": short,
            "Δ_vs代理_pp": round((p_ex - p_proxy) * 100.0, 4),
            "z": (round(d_pp / (rnd_sd * 100.0), 2)
                  if (rnd_sd > 0 and not short) else None),
            "可争取空间_pp": round(head * 100.0, 4),
            "抢占空间%": round((p_ex - iid) / head * 100.0, 1) if head > 1e-15 else None,
            "耗时s": m["耗时秒"], "代理复核耗时s": round(t_proxy, 2),
            "说明": note,
        })
        print(f"  K={K:>3}  K·p={kp:.4f}  精确={p_ex:.6f}  代理={p_proxy:.6f}  "
              f"随机={rnd_m:.6f}±{rnd_sd:.1e}  Δ={d_pp:+.3f}pp  "
              f"抢占空间={rows[-1]['抢占空间%']}%  {m['耗时秒']}s   [{note}]")

    print("\n| K | K/池 | K·p | iid | 真随机(均±sd) | 代理贪心 | 精确贪心 | Δ vs 随机 | z | 可争取空间 | 抢占% |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for r in rows:
        print(f"| {r['K']} | {r['K/池']:.0%} | {r['K·p']:.4f} | {r['iid']:.6f} | "
              f"{r['随机均值']:.6f}±{r['随机sd']:.1e} | {r['代理']:.6f} | "
              f"{r['精确']:.6f} | {r['Δ_pp']:+.3f}pp | {r['z']} | "
              f"{r['可争取空间_pp']:.3f}pp | {r['抢占空间%']}% |")

    v = DEC.verdict(rows)
    print(f"\n=== 判决（判据版本 {v['判据版本']}）===")
    print(f"  {v['判决']}")
    print(f"  原因：{v['原因']}")

    os.makedirs(OUT_DIR, exist_ok=True)
    # ★ 文件名必须带池口径：上一版只按彩种命名，2000 池那次的证据被 8000 池直接覆盖。
    # ★ 2026-11-05 追加目标口径：前缀研究要同时看 ≥四等 / ≥六等，两个目标的
    #   `可争取空间` 差几个数量级；不带目标就会让后跑的把前跑的证据覆盖掉。
    tag = (f"{args.pool_mode}{len(pool)}" + ("_relax" if args.relax else "")
           + f"_cap{args.cap}" + f"_t{args.target_grade}")
    path = os.path.join(OUT_DIR, f"tier_k_grid_{lottery_global}_{tag}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"彩种": lottery_global, "期号": issue, "目标": tgt,
                   "候选池": len(pool), "cap": args.cap, "seeds": args.seeds,
                   "单注概率": p1, "行": rows, "判决": v}, f,
                  ensure_ascii=False, indent=2)
    print(f"\n已落盘：{os.path.abspath(path)}")
    return 0


lottery_global = "双色球"

if __name__ == "__main__":
    raise SystemExit(main())
