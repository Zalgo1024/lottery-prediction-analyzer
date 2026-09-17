"""
ev/selection.py —— 按「实得奖金」选号（NetPayout WP2）

目标函数（方案文档 §5-WP2）：
    score(t) = Σ_g P_理论(g|t) × payout_eff(g, t, issue) − cost
    因 P_理论 与号码无关，score 的差异 **100% 来自奖金侧**（分薄/奖池），
    模型在数学上不可能"作弊"声称更准。

适用性（诚实边界）：
    双色球          ✅ 流行度模型 GO（WP1）→ 号码相关 EV（向量化批量打分）
    七星彩          ✅ crowd v2 撞号分薄 → 号码相关 EV
    大乐透          ⚠️ WP1 判 No-Go（OOS 不稳健）→ **跳过打分**直接随机出号
                      （EV 恒定时排序是伪信息，做排序反而暗示虚假优选）
    排列3/3D/排列5  ❌ 固定赔率撞号不分薄 → 选号不改变期望，明确拒绝
                      （如需反大众形态采样请用 `cli.py ev <lottery> --sample N`）

性能设计：
    双色球候选打分走 ev.popularity.batch_mu + ev.payout.pool_share_expected_batch
    的向量化路径（2 万候选 < 1 秒），只有最终选中的 n 注才逐注调 payout_eff
    取完整奖级明细——逐注口径与批量口径有单测对齐（tests/test_payout_eff.py）。
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

import numpy as np

logger = logging.getLogger(__name__)

_OVERLAP_CAP = {"双色球": 4, "大乐透": 3, "七星彩": 3}
_FIXED_ODDS = ("排列3", "福彩3D", "排列5")


def _rand_tickets(lottery: str, m: int, rng: np.random.Generator) -> List[Dict[str, Any]]:
    """随机合法候选号码。"""
    out = []
    if lottery == "双色球":
        for _ in range(m):
            reds = sorted(rng.choice(np.arange(1, 34), size=6, replace=False).tolist())
            out.append({"红球": reds, "蓝球": [int(rng.integers(1, 17))]})
    elif lottery == "大乐透":
        for _ in range(m):
            front = sorted(rng.choice(np.arange(1, 36), size=5, replace=False).tolist())
            out.append({"红球": front, "蓝球": sorted(rng.choice(np.arange(1, 13), size=2, replace=False).tolist())})
    elif lottery == "七星彩":
        for _ in range(m):
            out.append({"第1位..第7位": None,
                        "号码": rng.integers(0, 10, size=6).tolist() + [int(rng.integers(0, 15))]})
    return out


def _overlap(a, b, lottery: str) -> int:
    if lottery in ("双色球", "大乐透"):
        return len(set(a["红球"]) & set(b["红球"]))
    return sum(1 for x, y in zip(a["号码"], b["号码"]) if x == y)


def _greedy_pick(scored: List, n: int, cap: int, lottery: str) -> List[Dict[str, Any]]:
    """scored: [(key, t), ...] 已按 key 降序 → 贪心 + 低重叠约束。"""
    picked, used = [], []
    for key, t in scored:
        if len(picked) >= n:
            break
        if all(_overlap(t, u, lottery) <= cap for u in used):
            picked.append({"号码": t, "排序键": float(key)})
            used.append(t)
    return picked


def _select_redblue_vectorized(lottery: str, cands: List[Dict[str, Any]],
                               issue: Optional[str], n: int, cap: int,
                               rng: np.random.Generator) -> Optional[Dict[str, Any]]:
    """
    双色球 GO 路径：向量化批量打分（μ → E[min(cap,池/(1+X))]）→ 贪心低重叠。
    返回 {"机制","选中":[...]}（选中含 payout_eff 完整明细）；不可用返回 None（调用方回退）。
    """
    try:
        from ev.payout import (_grade_pool, _resolve_issue, grade_probabilities,
                               pool_share_expected_batch, payout_eff)
        from ev.popularity import batch_mu, get_popularity_model

        params = get_popularity_model(lottery)
        if not params.get("go"):
            return None
        issue = _resolve_issue(lottery, issue)
        gp = _grade_pool(lottery, issue, "一等")
        if not gp:
            return None
        pool, N = gp
        p1 = grade_probabilities(lottery)["一等"]

        R = np.array([t["红球"] for t in cands], dtype=np.int16)
        mus = batch_mu(lottery, R, N, p1, params)
        eff1 = pool_share_expected_batch(pool, mus)      # 一等奖实得（号码相关）
        ok = np.isfinite(eff1)
        if ok.sum() < n:
            return None

        order = np.argsort(-np.where(ok, eff1, -np.inf))
        scored = [(float(eff1[i]), cands[i]) for i in order[: min(len(order), 10 * n + 200)] if ok[i]]
        picked = _greedy_pick(scored, n, cap, lottery)

        out = []
        for p in picked:
            t = p["号码"]
            r = payout_eff(lottery, t, issue=issue)
            jz = next((x for x in r.get("奖级明细", []) if x["奖级"] == "一等"), None)
            out.append({
                "号码": t,
                "净EV": float(r.get("净EV", float("nan"))),
                "一等奖实得": (float(jz["实得单注"]) if jz and jz.get("实得单注") else round(p["排序键"], 2)),
                "分薄乘数": r.get("分薄", {}).get("乘数"),
            })
        mechanism = (f"{lottery}：一等奖实得=E[min(1000万封顶, 池/(1+X))]，X~Poisson(μ(组合))，"
                     "μ 由 WP1 流行度模型给出 → 冷门组合 EV 高于热门（不改变中奖概率）；"
                     "批量向量化打分，与逐注 payout_eff 口径对齐")
        return {"机制": mechanism, "选中": out, "期号": issue}
    except Exception as e:
        logger.warning(f"[selection] 向量化路径失败 {lottery}: {e}")
        return None


def control_tickets(lottery: str, cands: List[Dict[str, Any]], n: int,
                    seed: Optional[int] = None,
                    cap: Optional[int] = None) -> List[Dict[str, Any]]:
    """
    阴性对照组：从**同一候选池**随机取 n 注，施加同样的低重叠约束，不做任何打分排序。

    ⚠️ 对照的严格性全靠这一点：A/B 两组来自同一个候选池、同样的重叠约束，
    **唯一差异就是"有没有按一等奖实得排序"**。若换成另生成一批随机号，
    则比较的是"两批随机样本"，而非"排序这件事有没有用"。
    """
    rng = np.random.default_rng(seed)
    cap = cap if cap is not None else _OVERLAP_CAP.get(lottery, 3)
    picked: List[Dict[str, Any]] = []
    for i in rng.permutation(len(cands)):
        if len(picked) >= n:
            break
        t = cands[i]
        if all(_overlap(t, u, lottery) <= cap for u in picked):
            picked.append(t)
    return picked


def select_tickets(lottery: str, n: int = 5, pool_size: int = 20000,
                   seed: Optional[int] = None, issue: str = None,
                   candidates: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """
    返回
      {
        "彩种", "期号", "候选数", "选中数",
        "机制": str,           # 本彩种 EV 与号码的关系（诚实标注）
        "选中": [{"号码", "净EV", "一等奖实得", "分薄乘数"}, ...]   # 按实得降序
      }
    固定赔率彩种返回 {"彩种", "机制", "拒绝": True, "原因": ...}

    candidates: 外部传入的候选池（A/B 对照实验时用同一池，保证对照严格）。
    """
    if lottery in _FIXED_ODDS:
        return {
            "彩种": lottery,
            "机制": "固定赔率：撞号不分薄每注赔付，单注 EV 恒定",
            "拒绝": True,
            "原因": ("选号不改变期望——任何'高奖金号码'的说法对固定赔率彩种都不成立。"
                     "如需反大众形态采样（纯形态偏好）请用 `cli.py ev " + lottery + " --sample N`"),
        }

    rng = np.random.default_rng(seed)
    cands = candidates if candidates is not None else _rand_tickets(lottery, pool_size, rng)
    cap = _OVERLAP_CAP.get(lottery, 3)

    # ---- 双色球：优先走向量化批量打分（GO 时） ----
    if lottery == "双色球":
        fast = _select_redblue_vectorized(lottery, cands, issue, n, cap, rng)
        if fast is not None:
            return {
                "彩种": lottery,
                "期号": fast["期号"],
                "候选数": len(cands),
                "选中数": len(fast["选中"]),
                "重叠上限": cap,
                "机制": fast["机制"],
                "选中": fast["选中"],
            }
        # 模型不可用/数据缺失 → 回退逐注 payout_eff（EV 仍号码相关或恒定）

    # ---- 大乐透 NO-GO：跳过打分（EV 恒定 → 排序是伪信息），诚实随机出号 ----
    if lottery == "大乐透":
        from ev.payout import _resolve_issue
        from ev.popularity import get_popularity_model
        go = bool(get_popularity_model(lottery).get("go"))
        if not go:
            picked = []
            for t in cands[:n]:
                picked.append({
                    "号码": t,
                    "净EV": None,
                    "一等奖实得": None,
                    "分薄乘数": None,
                })
            return {
                "彩种": lottery,
                "期号": _resolve_issue(lottery, issue),
                "候选数": len(cands),
                "选中数": len(picked),
                "重叠上限": cap,
                "机制": ("大乐透：WP1 流行度模型 OOS 未达门槛（诚实否决）→ EV 恒定，"
                         "排序无信息量，故不做打分、直接随机出号（无号码相关收益）"),
                "选中": picked,
            }
        # 万一未来模型转 GO，走与双色球相同的向量化路径
        fast = _select_redblue_vectorized(lottery, cands, issue, n, cap, rng)
        if fast is not None:
            return {
                "彩种": lottery, "期号": fast["期号"], "候选数": len(cands),
                "选中数": len(fast["选中"]), "重叠上限": cap,
                "机制": fast["机制"], "选中": fast["选中"],
            }

    # ---- 逐注打分路径（七星彩 crowd v2；双/大回退） ----
    scored = []
    detail: Dict[int, Dict[str, Any]] = {}   # id(票) → payout_eff 明细（仅双/大逐注路径）
    if lottery in ("双色球", "大乐透"):
        from ev.payout import payout_eff
        for t in cands:
            r = payout_eff(lottery, t, issue=issue)
            if "error" in r:
                continue
            # 主排序键 = 一等奖实得（号码相关项）；净EV 作展示
            jz_row = next((x for x in r["奖级明细"] if x["奖级"] == "一等"), None)
            key = float(jz_row["实得单注"]) if (jz_row and jz_row["实得单注"]) else 0.0
            scored.append((key, t))
            detail[id(t)] = r
        mechanism = ("双色球：一等奖实得=E[min(1000万封顶, 池/(1+X))]，X~Poisson(μ(组合))，"
                     "μ 由 WP1 流行度模型给出 → 冷门组合 EV 高于热门（不改变中奖概率）"
                     if lottery == "双色球" else
                     "大乐透：EV 恒定，本结果仅为随机形态采样，无号码相关收益")
    else:  # 七星彩
        from ev.engine import net_ev
        for t in cands:
            nums = t["号码"]
            r = net_ev(lottery, nums, issue=issue)
            ev = r.get("限号EV")
            if ev is None:
                continue
            scored.append((float(ev) + 2.0, t))  # key=头奖贡献+固定部分
        mechanism = ("七星彩：浮动头奖撞号分薄（crowd v2），冷门组合 EV 高于热门（不改变中奖概率）")

    scored.sort(key=lambda x: x[0], reverse=True)
    picked_raw = _greedy_pick(scored, n, cap, lottery)

    # 回填展示字段（逐注路径只在最终选中时保留明细）
    # ⚠️ _greedy_pick 返回的是 {"号码","排序键"} 字典列表，不能按 (key, t) 解包
    #    （历史 bug：解包拿到的是 dict 的键名 → round("号码", 2) TypeError）。
    picked = []
    for item in picked_raw:
        t, key = item["号码"], float(item["排序键"])
        row = {"号码": t, "净EV": None, "一等奖实得": None, "分薄乘数": None}
        d = detail.get(id(t))
        if d is not None:                       # 双/大：一次算出，直接回填真实明细
            row["净EV"] = d.get("净EV")
            row["一等奖实得"] = round(key, 2)
            row["分薄乘数"] = (d.get("分薄") or {}).get("乘数")
        else:                                   # 七星彩：key = 头奖 EV 贡献 + 2
            row["净EV"] = round(key - 2.0, 4)
        picked.append(row)

    from ev.payout import _resolve_issue
    return {
        "彩种": lottery,
        "期号": _resolve_issue(lottery, issue),
        "候选数": len(scored),
        "选中数": len(picked),
        "重叠上限": cap,
        "机制": mechanism,
        "选中": picked,
    }


if __name__ == "__main__":
    import json
    import sys

    sys.stdout.reconfigure(errors="replace")
    lot = sys.argv[1] if len(sys.argv) > 1 else "双色球"
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    print(json.dumps(select_tickets(lot, n=n), ensure_ascii=False, indent=1))
