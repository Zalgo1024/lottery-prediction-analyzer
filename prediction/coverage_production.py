# -*- coding: utf-8 -*-
"""prediction/coverage_production.py —— 把覆盖选择接进出号路径（双色球 ≥10元）。

为什么接这个
------------
v1.8 双轨在「双色球 K=200 / 至少中一次 ≥10 元」上得到本项目第一次**同时**
通过 BH 校正与可判性双门的结果：

    随机独立基线   83.17%（实测 1171/1408）
    union_greedy   88.21%（实测 1242/1408，Δ=+5.04pp，p_BH=0.0024）
    tier_packing   87.00%（实测 1225/1408，Δ=+3.84pp，p_BH=0.0266）

反面同样重要：**生产在用的策略混合 `prod_mix` 的覆盖率效率只有 0.8074，
比随机池的 0.8276 还低。** 也就是说现路径在这个门上不是「没优化」，是「反向」。

★★ 必须同时记住的另一半（写进 KPI，不许只报上涨的那半）
    同一批票的评估窗实际回款从 −50.05 元/百元 变成 −74.80 / −75.39 元/百元。
    这不是 bug：`E[中奖注数] = K·p` 与选哪些票无关，覆盖设计重排的是**分布**
    （把「少数几期中很多注」换成「更多期各中一注」），不是总量。
    ⇒ 本模块改善的是「更少空手期」，**不改善期望回款、不提高任何一注的中奖概率**。

设计约束（一条都不许破）
------------------------
1. **注数守恒**：选出多少注就是多少注，绝不增注（加注只放大同一个负期望）。
2. **无前视**：候选池是均匀随机 RNG（seed 由 `(彩种, 期号)` 派生），
   选号判据是精确概率（与开奖结果无关）。整条链不读任何历史开奖号码。
3. **失败即降级**：任何异常都原样返回上游票集，只在 KPI 里记原因。
   出号路径宁可用旧票，也不能因为优化失败而不出号。
4. **可复现**：同一 `(彩种, 期号)` 在不同进程里必须得到同一套号（crc32，不用 `hash()`）。
5. **不改 pending 语义**：本模块只返回号码，不写盘、不记台账。
"""
from __future__ import annotations

import json
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_BASE_DIR = Path(__file__).resolve().parent.parent

# 可选覆盖配置。`config/*.json` 在 .gitignore 里 ⇒ 这是**本机调参**，
# 默认值写在代码里，仓库里不需要这个文件也能跑。
CONFIG_PATH = _BASE_DIR / "config" / "coverage_production.json"

DEFAULT_CONFIG: Dict[str, Any] = {
    "启用彩种": ["双色球"],
    "目标": {"kind": "payout", "min_payout": 10.0},
    "候选臂": ["union_greedy", "tier_packing"],
    "候选池下限": 8000,
    "候选池倍数": 40,          # 池 = max(下限, 倍数 × K)：池不放大就会测到池耗竭
    "MC评估采样数": 30000,
    "MC拟合采样数": 30000,
    "_MC说明": (
        "预注册主跑用 100000（那是**测量**，必须不失真）；生产是**出号**，"
        "每晚多跑 2 分钟换不到可分辨的差异。实测 m=400 时效率 0.9606、"
        "m=100000 时 0.9772（K=200 ≥四等），30000 落在两端之间，代价可接受。"
        "要复现登记数字请显式传 100000。"),
    "重叠上限": 3,
    "超时秒": 600,             # 超时就用当前最好的那个臂，不无限等
    # ★ 空间闸：可争取空间 = min(1,K·p) − 随机基线。太小就不值得花算力。
    #   实测：K=200 时 17.2pp（值得做）、K=500 时只有 1.6pp（做 4 分钟换 1 个点，不值）。
    #   这不是「优化无效」的借口，是把算力花在有空间的地方；空间够大时照样会跑。
    "最小可争取空间": 0.02,
}

HONEST_NOTE = (
    "覆盖选择只重排「至少中一次达标」的**分布**：把「少数几期中很多注」换成"
    "「更多期各中一注」。按恒等式 E[中奖注数] = K·p，期望中奖注数与选哪些票无关，"
    "所以**期望回款不变**、单注中奖概率不变，也不预测开奖号码。"
    "它的用途是减少空手期；要少亏只能靠减少投入。"
)


def _load_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if Path(CONFIG_PATH).exists():
            doc = json.loads(Path(CONFIG_PATH).read_text(encoding="utf-8"))
            if isinstance(doc, dict):
                cfg.update(doc)
    except Exception:                                        # noqa: BLE001
        # 配置坏了不能拖垮出号：直接用默认值（双色球照样启用）
        pass
    return cfg


def is_enabled(lottery: str, cfg: Optional[Dict[str, Any]] = None) -> bool:
    c = cfg or _load_config()
    return lottery in (c.get("启用彩种") or [])


def _stable_seed(lottery: str, issue: Any) -> int:
    """跨进程稳定的 seed（`hash()` 会被 PYTHONHASHSEED 加盐，不能用）。"""
    return zlib.crc32(f"coverage|{lottery}|{issue}".encode("utf-8")) & 0xFFFFFFFF


def _zone_defs(lottery: str) -> Dict[str, Dict[str, Any]]:
    """`{分区名: {min,max,choose,...}}`。`LOTTERY_CONFIG['zones']` 是**列表**不是字典。"""
    from config import LOTTERY_CONFIG
    cfg = LOTTERY_CONFIG.get(lottery) or {}
    out: Dict[str, Dict[str, Any]] = {}
    for z in (cfg.get("zones") or []):
        if isinstance(z, dict) and z.get("name"):
            out[str(z["name"])] = z
    return out


def _zone_key(lottery: str) -> Tuple[str, str]:
    """红/蓝分区的名字（按彩种配置取，不在这里抄字面量）。"""
    names = list(_zone_defs(lottery).keys())
    if len(names) >= 2:
        return names[0], names[1]
    return "红球", "蓝球"


def _normalize(lottery: str, tickets: Sequence[Dict[str, Any]]) -> List[Dict[str, List[int]]]:
    """把臂返回的票面规整成 `{分区: [号码,...]}`，并做硬校验。"""
    z1, z2 = _zone_key(lottery)
    out: List[Dict[str, List[int]]] = []
    for t in tickets:
        row: Dict[str, List[int]] = {}
        for z in (z1, z2):
            v = t.get(z)
            if v is None:
                raise ValueError(f"票面缺分区 {z!r}：{list(t)}")
            seq = list(v)
            row[z] = sorted(int(x) for x in seq)
        out.append(row)
    return out


def _validate(lottery: str, rows: Sequence[Dict[str, List[int]]], K: int) -> None:
    """注数守恒 / 无重复 / 号码在范围内 —— 任一条不满足就抛错让调用方降级。"""
    if len(rows) != K:
        raise ValueError(f"注数不符：{len(rows)} != {K}（覆盖选择不许增删注数）")
    seen = set()
    for r in rows:
        key = tuple((k, tuple(v)) for k, v in sorted(r.items()))
        if key in seen:
            raise ValueError(f"出现重复注：{key}")
        seen.add(key)
    defs = _zone_defs(lottery)
    for r in rows:
        for zname, nums in r.items():
            z = defs.get(zname) or {}
            lo, hi = z.get("min"), z.get("max")
            choose = z.get("choose")
            if lo is not None and hi is not None:
                if any(n < int(lo) or n > int(hi) for n in nums):
                    raise ValueError(f"{zname} 号码越界：{nums} 不在 [{lo},{hi}]")
            if choose is not None and len(nums) != int(choose):
                raise ValueError(f"{zname} 号码个数 {len(nums)} != {choose}")
            if len(set(nums)) != len(nums):
                raise ValueError(f"{zname} 号码重复：{nums}")


def select(lottery: str, K: int, *, issue: Any = None,
           cfg: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, List[int]]], Dict[str, Any]]:
    """按目标选出 K 注（无前视、可复现）。返回 `(票面, KPI)`。

    多个候选臂时**按精确概率取高者**：精确枚举的分辨力比实测频率高约 9 个数量级，
    所以这是本项目里唯一可以拿来做选择判据的量（实测差值属于噪声，不用于选型）。
    """
    c = cfg or _load_config()
    if K <= 0:
        raise ValueError(f"注数必须为正：{K}")
    t0 = time.time()
    kpi: Dict[str, Any] = {
        "启用": True, "是否替换": False, "注数": int(K),
        "候选臂": {}, "诚实声明": HONEST_NOTE,
    }

    from ev.arena import arms as ARMS
    from ev.coverage_backtest import build_pool
    from ev.tier_coverage import exact_union_probability
    from ev import tier_target as TT

    target = c.get("目标") or DEFAULT_CONFIG["目标"]
    pool_n = max(int(c.get("候选池下限", 8000)),
                 int(c.get("候选池倍数", 40)) * int(K))
    pool_seed = _stable_seed(lottery, issue)
    pool = build_pool(lottery, pool_n, seed=pool_seed)
    kpi["候选池"] = {"规模": int(len(pool)), "seed": int(pool_seed)}

    res = TT.resolve_conditions(lottery, target, issue)
    p1 = TT.single_ticket_probability(lottery, res["conditions"], issue)
    cap = min(1.0, float(K) * float(p1))
    base = 1.0 - (1.0 - p1) ** int(K)
    kpi.update({"目标": res["label"], "单注达标概率": p1,
                "理论上限Kp": cap, "随机独立基线": base,
                "可争取空间": cap - base})

    # ★ 空间闸：K·p 已经远大于 1 时，「至少中一次」本来就接近必然，
    #   覆盖优化只剩几个点的空间，跑一次要几分钟 ⇒ 直接跳过并如实登记。
    floor = float(c.get("最小可争取空间", 0.02))
    if (cap - base) < floor:
        kpi.update({
            "是否替换": False, "跳过": True,
            "跳过原因": (f"可争取空间 {cap - base:.4f} < 门槛 {floor:.4f}"
                     f"（K·p={float(K) * float(p1):.3f}，随机基线已 {base:.4f}）"
                     f" ⇒ 不做覆盖选择，沿用原票"),
        })
        return [], kpi

    params = {"m_eval": int(c.get("MC评估采样数", DEFAULT_CONFIG["MC评估采样数"])),
              "m_fit": int(c.get("MC拟合采样数", DEFAULT_CONFIG["MC拟合采样数"]))}
    arm_seed = _stable_seed(lottery, issue) ^ 0x5F5F
    best: Optional[Tuple[float, str, List[Dict[str, Any]]]] = None

    for arm in (c.get("候选臂") or []):
        if time.time() - t0 > float(c.get("超时秒", 600)):
            kpi["超时"] = f"已用 {time.time() - t0:.0f}s，跳过剩余候选臂"
            break
        rec: Dict[str, Any] = {}
        try:
            tks, meta = ARMS.select_arm(arm, lottery, pool, int(K), target, issue,
                                        seed=int(arm_seed),
                                        cap=int(c.get("重叠上限", 3)),
                                        params=dict(params))
            rows = _normalize(lottery, tks)
            _validate(lottery, rows, int(K))
            P = exact_union_probability(lottery, rows, target=target, issue=issue)
            rec.update({"状态": "完成", "精确P": P,
                        "覆盖率效率": (P / cap) if cap > 0 else None,
                        "实际注数": len(rows), "cap污染": bool(meta.get("cap污染"))})
            if best is None or P > best[0] + 1e-15:
                best = (P, arm, tks)
        except Exception as e:                               # noqa: BLE001
            rec.update({"状态": "失败", "原因": f"{type(e).__name__}: {e}"})
        kpi["候选臂"][arm] = rec

    if best is None:
        raise RuntimeError("所有候选臂都未产出合格票集：" +
                           "；".join(f"{k}:{v.get('原因', v.get('状态'))}"
                                    for k, v in kpi["候选臂"].items()))
    rows = _normalize(lottery, best[2])
    _validate(lottery, rows, int(K))
    kpi.update({
        "是否替换": True, "选中臂": best[1], "选中精确P": best[0],
        "覆盖率效率": (best[0] / cap) if cap > 0 else None,
        "对比随机基线": best[0] - base,
        "耗时秒": round(time.time() - t0, 1),
    })
    return rows, kpi


__all__ = ["select", "is_enabled", "DEFAULT_CONFIG", "CONFIG_PATH", "HONEST_NOTE"]
