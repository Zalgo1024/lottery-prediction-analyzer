# -*- coding: utf-8 -*-
"""ev/arena/arms.py —— 臂注册表与随机数隔离。

六个臂
------
| 名 | 选择器 | 说明 |
|---|---|---|
| `random` | `ev.selection.control_tickets` | **基线**：同预算、同池、同 cap 的随机票 |
| `tier_coverage` | `ev.tier_coverage.select_tier_coverage` | 现有**通道权重代理**贪心 |
| `union_greedy` | `ev.arena.design.select_union_greedy` | **直接在 P(并集) 上贪心**（本轮新增） |
| `lowcrowd` | `ev.lowcrowd_select.select_lowcrowd` | 低拥挤（需拥挤度代理 GO，否则**降级**） |
| `prod_mix` | `ev.selection.select_tickets` | 生产选号（★ 内部 cap=4，其余臂 cap=3） |
| `hybrid` | `ev.lowcrowd_select.select_joint` | **联合目标**：P(并集) 采样边际 × 冷门同打分加权（λ=0.5 锁定，v1.4 进主检验；拥挤度代理不可用 ⇒ 暂不评估） |

★ 两个必须显式标出的口径污染（方案 §1.2 G3）：
  · `prod_mix` 走 `ev.selection._OVERLAP_CAP["双色球"] = 4`，其余臂 `PROD_OVERLAP_CAP = 3`。
    这会把「重叠上限差异」混进「选号差异」，因此 prod_mix 的结果
    只能作**外部保真度对照**，不进主假设的配对检验。
  · `select_tier_coverage` 在挑不满时会自动把 cap 逐级放宽（最多 +4）且不报错。
    `meta["放宽次数"] > 0` 时结果标记为 `cap污染`。

RNG 隔离
--------
各臂共用了一些依赖 `np.random.default_rng(seed)` 的旧接口，而部分旧接口内部
会读**全局** `np.random` 状态。为保证「同一 seed 复现同一结果」，
所有选号都在 `isolated_global_rng(base)` 上下文里执行。
"""
from __future__ import annotations

import contextlib
import logging
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import numpy as np

from ev.lowcrowd_backtest import PROD_OVERLAP_CAP

logger = logging.getLogger(__name__)

DEFAULT_CAP = int(PROD_OVERLAP_CAP)


# ---------------------------------------------------------------- RNG 隔离
@contextlib.contextmanager
def isolated_global_rng(base: int):
    """进入时固定全局 `np.random` 状态，退出时恢复现场。"""
    st = np.random.get_state()
    np.random.seed(int(base) & 0xFFFFFFFF)
    try:
        yield
    finally:
        np.random.set_state(st)


def derive_seed(base: int, arm: str) -> int:
    """「基准 seed × 臂名」的确定性派生 —— 各臂拿到**不同但可复现**的流。"""
    h = 2166136261
    for ch in f"{int(base)}:{arm}":
        h = ((h ^ ord(ch)) * 16777619) & 0xFFFFFFFF
    return h


# ---------------------------------------------------------------- 各臂实现
def _arm_random(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                target: Any, issue: Any, seed: int, cap: int,
                params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from ev.selection import control_tickets

    tks = control_tickets(lottery, list(pool), K, seed=seed, cap=cap)
    return tks, {"选择器": "control_tickets", "重叠上限": cap, "放宽次数": 0}


def _arm_tier_coverage(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                       target: Any, issue: Any, seed: int, cap: int,
                       params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from ev.tier_coverage import select_tier_coverage

    tks, meta = select_tier_coverage(lottery, pool, K, target, issue,
                                     cap=cap, seed=seed,
                                     trace=bool(params.get("trace", False)))
    out: Dict[str, Any] = {"选择器": "select_tier_coverage",
                           "重叠上限": meta.get("重叠上限"),
                           "初始重叠上限": meta.get("初始重叠上限"),
                           "放宽次数": int(meta.get("放宽次数", 0)),
                           "通道权重和": meta.get("通道权重和")}
    tr = meta.get("贪心轨迹")
    if tr is not None:        # trace=False 时**不出现该键**（哨兵锁的就是这个）
        out["贪心轨迹"] = tr
    return tks, out


def _arm_union_greedy(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                      target: Any, issue: Any, seed: int, cap: int,
                      params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from ev.arena.design import select_union_greedy

    tks, meta = select_union_greedy(
        lottery, pool, K, target, issue, cap=cap, seed=seed,
        m_fit=int(params.get("m_fit", 100_000)),
        m_eval=int(params.get("m_eval", 100_000)),
        eval_seed_offset=int(params.get("eval_seed_offset", 10_000)))
    return tks, {"选择器": "select_union_greedy",
                 "重叠上限": meta.get("重叠上限"),
                 "放宽次数": 0,
                 "拟合覆盖率": meta.get("拟合覆盖率"),
                 "样本外覆盖率": meta.get("样本外覆盖率"),
                 "优化乐观量": meta.get("优化乐观量"),
                 "cap拒绝次数": meta.get("cap拒绝次数")}


def _arm_lowcrowd(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                  target: Any, issue: Any, seed: int, cap: int,
                  params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from ev import lowcrowd_select as ls
    from ev import popularity_proxy as pp
    from ev.arena import crowd as CR

    # ★ 三重探测（支持范围 / Go 门槛 / 训练样本量），任一不过就是「暂不评估」。
    #   以前这里写 `if go is None` —— 而 `go_or_none` 返回 bool，那一支是死代码，
    #   实际靠 `crowd_quantiles` 返回 NaN 兜底，原因文案就说不清卡在哪一项。
    gate = CR.require_usable(lottery, n_periods=params.get("n_periods"))
    u = pp.crowd_quantiles(lottery, list(pool), persist=False)
    if u is None or not np.isfinite(u).any():
        raise NotImplementedError(f"{lottery} 拥挤度分位计算失败，本轮不评估")
    tks, meta = ls.select_lowcrowd(lottery, pool, K, u,
                                   lam=float(params.get("lam", 0.5)),
                                   gate_q=float(params.get("gate_q", 0.3)),
                                   cap=cap,
                                   beta_caliber=params.get("beta_caliber", "冻结β"))
    return tks, {"选择器": "select_lowcrowd", "重叠上限": cap, "放宽次数": 0,
                 "λ": params.get("lam", 0.5), "门控分位": params.get("gate_q", 0.3),
                 "拥挤度闸门": gate["状态"]}


def _arm_prod_mix(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                  target: Any, issue: Any, seed: int, cap: int,
                  params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    from ev.selection import _OVERLAP_CAP, select_tickets

    inner_cap = int(_OVERLAP_CAP.get(lottery, 3))
    res = select_tickets(lottery, n=K, seed=seed, issue=(None if issue is None else str(issue)),
                         candidates=list(pool))
    if res.get("拒绝"):
        raise NotImplementedError(f"{lottery} 生产选号拒绝：{res.get('原因')}")
    rows = res.get("选中") or []
    tks = [r["号码"] for r in rows if isinstance(r, dict) and r.get("号码") is not None]
    return tks, {"选择器": "select_tickets（生产）",
                 "重叠上限": inner_cap,
                 "放宽次数": 0,
                 "★cap污染": (inner_cap != cap),
                 "口径说明": (f"生产选号内部用 cap={inner_cap}，其余臂用 cap={cap} ⇒ "
                             f"{'口径不一致，只可作外部保真度对照' if inner_cap != cap else '一致'}")}


# ---- v1.4 预注册锁定的联合目标参数。★ 不许逐格微调：改它们 = 改实验契约
_HYBRID_LAM = 0.5               # 覆盖↔冷门加权中点（P5 结论 §3 的读数档）
_HYBRID_GATE_Q = 0.30           # 与 lowcrowd 同档，保跨臂可比
_HYBRID_COV_SOURCE = "union"    # 覆盖通道 = 采样结局上的真实 P(并集) 边际


def _arm_hybrid(lottery: str, pool: Sequence[Dict[str, Any]], K: int,
                target: Any, issue: Any, seed: int, cap: int,
                params: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """联合目标臂（P5，v1.4 进主检验）：P(并集) 采样边际 × 冷门，同一打分加权。

    与 `lowcrowd` 共用同一条拥挤度输入与同一道闸门 ⇒ 大乐透同样「暂不评估」。
    ★ 空票集必须**显式降级**（NotImplementedError ⇒ runner 记「暂不评估」），
      不许静默按 0 注结算 —— 0 注回放会得到 0 达标率，看起来像「跑了但全丢」。
    """
    from ev import lowcrowd_select as ls
    from ev import popularity_proxy as pp
    from ev.arena import crowd as CR

    # 三重探测（支持范围 / Go 门槛 / 训练样本量），任一不过即「暂不评估」——
    # 与 _arm_lowcrowd 完全同构，大乐透卡在 Go 门槛（crowd.py 现取实测数字）
    gate = CR.require_usable(lottery, n_periods=params.get("n_periods"))
    u = pp.crowd_quantiles(lottery, list(pool), persist=False)
    if u is None or not np.isfinite(u).any():
        raise NotImplementedError(f"{lottery} 拥挤度分位计算失败（全 NaN）⇒ 暂不评估")

    tks, meta = ls.select_joint(
        lottery, pool, K, u,
        lam=_HYBRID_LAM, gate_q=_HYBRID_GATE_Q, cap=cap,
        cov_source=_HYBRID_COV_SOURCE,
        target=target, issue=issue,
        seed=seed,                      # select_arm 已按臂派生 ⇒ hybrid 有独立 MC 流
        m_fit=int(params.get("m_fit", 100_000)),
        beta_caliber=params.get("beta_caliber", "冻结β"))

    if not tks:
        why = meta.get("降级") or meta.get("不足") or "未给出原因"
        raise NotImplementedError(
            f"{lottery} 联合目标未出票（{why}）⇒ 暂不评估，不按 0 注静默结算")

    out: Dict[str, Any] = {
        "选择器": meta.get("选择器"),
        "覆盖通道": meta.get("覆盖通道"),
        "λ": meta.get("λ"),
        "门控分位": (meta.get("门控") or {}).get("请求分位"),
        "门控": meta.get("门控"),      # 生效分位/放宽次数/门控后容量/全池分位，嵌套原样透传
        "联合位图": meta.get("联合位图"),
        "重叠上限": cap,
        # ★ cap 不放宽：_joint_beam_pick 没有放宽逻辑（过不了重叠约束就跳窗）。
        #   门控放宽记在「门控.放宽次数」，**不算 cap污染** —— 否则 select_arm 的
        #   cap污染 判据（198-200 行）会把「门控多放宽一档」误报成「重叠上限被污染」，
        #   触发停止规则第 3 条、把 hybrid 打成「只能作诊断」。
        "放宽次数": 0,
        "拥挤度闸门": gate["状态"],
        "β口径": meta.get("β口径"),
        "beam窗口": meta.get("beam窗口"),
        "覆盖键数": (meta.get("覆盖") or {}).get("选中覆盖键"),
        "z口径": meta.get("z口径"),
        "循环性声明": meta.get("循环性声明"),
        "分位口径": meta.get("分位口径"),
    }
    if meta.get("联合位图截断") is not None:
        out["联合位图截断"] = meta["联合位图截断"]   # r>4000 时才出现（最冷 4000 建位图）
    if meta.get("不足") is not None:
        out["不足"] = meta["不足"]                   # 阶梯走完仍 <K 时；select_arm 另记「出票不足」
    return tks, out


ARMS: Dict[str, Callable[..., Tuple[List[Dict[str, Any]], Dict[str, Any]]]] = {
    "random": _arm_random,
    "tier_coverage": _arm_tier_coverage,
    "union_greedy": _arm_union_greedy,
    "lowcrowd": _arm_lowcrowd,
    "prod_mix": _arm_prod_mix,
    "hybrid": _arm_hybrid,
}

ARM_NOTES: Dict[str, str] = {
    "random": "同预算同池同 cap 的随机票；所有比较的基线",
    "tier_coverage": "现有通道权重代理贪心（优化的是上界，不是 P(并集)）",
    "union_greedy": "直接在 P(并集) 上 CELF 贪心 + 样本外评估（本轮新增）",
    "lowcrowd": "低拥挤选号；拥挤度代理不可用时降级为「暂不评估」",
    "prod_mix": "生产选号；内部 cap 不同 ⇒ 只作外部保真度对照，不进主检验",
    "hybrid": ("联合目标：P(并集) 采样边际 + 冷门通道同一打分加权"
               "（λ=0.5 锁定，v1.4 进主检验）；拥挤度代理不可用时降级为「暂不评估」"),
}


# ---------------------------------------------------------------- 入口
def select_arm(arm: str, lottery: str, pool: Sequence[Dict[str, Any]], K: int,
               target: Any = None, issue: Any = None, *,
               seed: int = 0, cap: Optional[int] = None,
               params: Optional[Dict[str, Any]] = None) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """执行一个臂的选号。返回 `(票集, meta)`。

    `NotImplementedError` 表示「**暂不评估**」——这是第一等状态，调用方
    必须把它原样带进报告，**不许**当成 0 期或静默跳过。
    """
    if arm not in ARMS:
        raise KeyError(f"未知臂 {arm!r}；可用：{sorted(ARMS)}")
    cap = DEFAULT_CAP if cap is None else int(cap)
    params = dict(params or {})
    sd = derive_seed(seed, arm)
    with isolated_global_rng(sd):
        tks, meta = ARMS[arm](lottery, pool, K, target, issue, sd, cap, params)
    meta.setdefault("臂", arm)
    meta["臂说明"] = ARM_NOTES.get(arm, "")
    meta["seed"] = int(seed)
    meta["派生seed"] = sd
    meta["要求注数"] = int(K)
    meta["实际注数"] = len(tks)
    meta["cap污染"] = bool(meta.get("放宽次数", 0) > 0
                           or meta.get("重叠上限") not in (None, cap)
                           or meta.get("★cap污染", False))
    if len(tks) != K:
        meta["出票不足"] = f"{len(tks)}/{K}"
    return tks, meta
