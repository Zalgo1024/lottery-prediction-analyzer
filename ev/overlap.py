# -*- coding: utf-8 -*-
"""臂间选号重合 + 零假设（只读诊断，不参与任何判决）。

为什么要算这个（2026-11-07）
---------------------------
本项目有两条选号臂（`existing_coverage` / `tier_coverage`），却从没有过
「它们是不是在挑同一批票」的读数。外部参考程序有 —— 但它只报重合率，
**没有零假设**，于是「重合率下降」被当成「策略已解耦」的证据。

重合率本身没有意义：从 N 张池子里各挑 k 张，即使双方都均匀随机，
也会自然重合 k²/N 张。所以本模块把同一组数放进零假设里：
  · E[重合] = k₁k₂/N（两臂独立均匀抽样）
  · Var = k₁k₂(N-k₁)(N-k₂) / (N²(N-1))（多元超几何方差）
  · z = (实测 − E) / √Var

★ 判读纪律：
  - `|z| < 2` ⇒ 与独立抽取无差别，**不要**把差异读成「解耦成功」；
  - `z ≤ -2` ⇒ 显著低于独立抽取，只说明两臂**选法不同**，不说明谁更准；
  - `z ≥ 2` ⇒ 显著高于独立抽取，此时两臂的差异会被大量共有票稀释。
  - 一句话：**重合 ≠ 独立 ≠ 有信息**。低重合也可能是抖动（探索项）造成的。
"""
from __future__ import annotations

from math import sqrt
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

__all__ = ["NOTE", "arm_overlap", "overlap_stats", "ticket_key"]

NOTE = ("重合 ≠ 独立 ≠ 有信息：两臂挑同一批票不代表它们等价，"
        "挑不同票也不代表谁更准；低重合可能只是抖动（探索项）。"
        "本读数只用于发现「两臂其实在重复劳动」，不作为任何判决依据。")


def ticket_key(ticket: Dict[str, Any]) -> Tuple:
    """一张票的规范化键（分区 + 排序后的号码），与号码书写顺序无关。"""
    zones = ticket.get("号码")
    if not isinstance(zones, dict):
        zones = {}
        for k in ("红球", "蓝球"):
            if ticket.get(k):
                zones[k] = ticket[k]
    return tuple(sorted(
        (str(k), tuple(sorted(int(x) for x in v))) for k, v in zones.items() if v
    ))


def _distinct(seq: Iterable[Dict[str, Any]]) -> List[Tuple]:
    out, seen = [], set()
    for t in seq:
        k = ticket_key(t)
        if k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def overlap_stats(a: Sequence[Dict[str, Any]], b: Sequence[Dict[str, Any]],
                  universe: int) -> Dict[str, Any]:
    """两臂的重合数与零假设下的偏离。

    `universe` = 候选池张数（两臂都从这个池子里挑）。用**去重后**的票数当 k，
    因为同一臂里出现重复票时，它对该臂的有效容量贡献是 0。
    """
    A, B = _distinct(a), _distinct(b)
    k1, k2 = len(A), len(B)
    N = max(int(universe), 1)
    obs = len(set(A) & set(B))

    exp = (k1 * k2) / N
    if N > 1 and k1 and k2:
        var = (k1 * k2 * (N - k1) * (N - k2)) / (N * N * (N - 1))
    else:
        var = 0.0
    z = (obs - exp) / sqrt(var) if var > 0 else None

    if z is None:
        verdict = "样本不足（池或某一臂为空），无法与零假设比较"
    elif abs(z) < 2:
        verdict = "与独立抽取无差别：重合数落在噪声范围内"
    elif z <= -2:
        verdict = "显著低于独立抽取：两臂在挑不同票 —— 只说明选法不同，不说明谁更准"
    else:
        verdict = "显著高于独立抽取：两臂在挑同一批票，差异会被共有票稀释"

    return {
        "臂A去重票数": k1,
        "臂B去重票数": k2,
        "候选池": N,
        "重合票数": obs,
        "重合率_相对较大臂": round(obs / max(k1, k2), 4) if max(k1, k2) else None,
        "零假设期望重合": round(exp, 2),
        "零假设标准差": round(sqrt(var), 3) if var > 0 else None,
        "z": round(z, 2) if z is not None else None,
        "判读": verdict,
        "说明": NOTE,
    }


def arm_overlap(lottery: str, *, k: int = 200, pool_size: int = 2000, seed: int = 0,
                arms: Tuple[str, str] = ("existing_coverage", "tier_coverage")) -> Dict[str, Any]:
    """在同一池、同注数、同种子下跑两条臂，比较它们的选号重合。

    ★ 注数用 `k`（默认 200）而**不是生产注数**：`tier_coverage` 的选择是贪心，
      1w 注在页面上等不起。读数里写明 k，避免被读成「生产批次的重合」。
    ★ 达标基准取「本地最近一期开奖」（`target`），所以这是**当下的**对照读数，
      不是跨期回测；跨期重合率需要逐期跑，成本高得多，未做（见「未做」）。
    """
    from ev.coverage_backtest import build_pool
    from ev.tier_backtest import _select, load_test_recs

    recs = load_test_recs(lottery)
    if not recs:
        return {"适用": False, "原因": f"{lottery} 无本地开奖数据（不记为通过）"}
    rec = recs[-1]
    issue = getattr(rec, "期号", None)
    # ★ target 是「达标口径」而不是开奖记录：`_select` 会把它交给
    #   `tier_target.resolve_conditions`。传 None = 用默认档（与 arena 一致）；
    #   传 DrawRecord 会在归一化时直接报「无法识别的 target」。
    # 达标口径用「≥5 元」而不是默认的「四等」：后者是乐透型奖级名，
    # 快乐8/数字型没有这一档，`resolve_conditions` 会直接报错。≥5 元对所有彩种成立，
    # 也与 `existing_coverage` 的目标一致 ⇒ 两臂比的是同一件事。
    tier_target = {"kind": "payout", "min_payout": 5.0}

    pool = build_pool(lottery, m=int(pool_size), seed=int(seed))
    try:
        ta, meta_a = _select(arms[0], lottery, pool, int(k), tier_target, issue,
                             seed=int(seed), cap=int(k))
        tb, meta_b = _select(arms[1], lottery, pool, int(k), tier_target, issue,
                             seed=int(seed), cap=int(k))
    except Exception as e:                                    # noqa: BLE001
        return {"适用": False, "原因": f"{type(e).__name__}: {e}（不记为通过）"}

    stats = overlap_stats(ta, tb, len(pool))
    # ★ 两臂任一方产不出足量**不重复**票时，重合率没有意义（1 张票重复 200 次
    #   会算出 z≈45 这种荒谬读数）。此时标不适用并写清是哪一臂、几张。
    k_eff = min(stats["臂A去重票数"], stats["臂B去重票数"])
    if k_eff < max(10, int(k) // 2):
        return {"适用": False,
                "原因": (f"两臂在该彩种上未产出足量不重复票（A {stats['臂A去重票数']} 张 / "
                         f"B {stats['臂B去重票数']} 张，要求 ≥{max(10, int(k) // 2)}）"
                         " ⇒ 重合率无意义，不记为通过")}
    return {
        "适用": True,
        "彩种": lottery,
        "基准期号": issue,
        "达标口径": "≥5 元（对所有彩种成立，与 existing_coverage 目标一致）",
        "注数K": int(k),
        "候选池": int(pool_size),
        "种子": int(seed),
        "臂": [{"名称": arms[0], "策略": (meta_a or {}).get("策略")},
               {"名称": arms[1], "策略": (meta_b or {}).get("策略")}],
        **stats,
        "口径": (f"同池 {pool_size} 张、各挑 {k} 注、同种子 {seed}；"
                 "达标基准 = 本地最近一期开奖（当期对照，不是跨期回测）"),
    }
