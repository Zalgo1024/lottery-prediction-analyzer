# -*- coding: utf-8 -*-
"""盈利/覆盖**两个模式显式二选一**的入口 —— 结构上禁止把它们合成一个总分。

## 为什么要有这个模块

两条线优化的是**不同的量**：

| 模式 | 目标 | 用到的信息 | 与号码的关系 |
| --- | --- | --- | --- |
| 四等奖覆盖 | `P(至少一注 ≥ 四等)` 的**并集概率** | 只用票面「哪些 (h,j) 能达标」 | 与开奖概率无关，纯组合覆盖 |
| 净EV | 保守情景下的**预计净回款** | 奖池 / 销量 / 分奖人数（热门度） | 通过「少人买 ⇒ 分奖少」影响实得 |

把它们加权成一个标量分数有三个后果，且都不是理论洁癖：
1. **权重无法事先约定**：λ 只能靠看结果调，那就是事后挑结论；
2. **必然出现交换**：为了让几个 5 元小奖多中一次，牺牲四等奖覆盖 —— 而这两个"一次"的
   价值差别由 λ 决定，λ 又说不清；
3. **热门度会被误用**：热门度只能估算「中奖后有几个人分」，**不能**提高号码被开出的概率。
   一旦混进覆盖目标，就会滑向"买冷门号更容易中"这种错话。

所以本模块**不提供** `weights=` 参数（不是默认禁用，是没有这个参数），
并强制 `mode` 只能是下面二者之一。

## 两种模式各自的诚实边界

- 覆盖模式：不改变任何一注自己的中奖概率；`E[达标注数] = K·p` 恒成立；
  可争取空间 `O((K·p)²)` 量级，报收益必须同时报 K 与 K·p。
- 净EV 模式：默认判决是「不参与」。热门度模型 Go/No-Go 未通过、或倾斜来源为空时，
  所有候选同分 ⇒ **排序没有信息**，此时必须显式退化为中性基线并说明，
  不许拿一个不稳定的分数去主导选票。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

模式_覆盖 = "四等奖覆盖"
模式_净EV = "净EV"
VALID_MODES = (模式_覆盖, 模式_净EV)

_覆盖边界 = ("不改变任何一注自己的中奖概率（E[达标注数] = K·p）；"
             "只改变 K 注之间「达标结局」的重叠结构；"
             "可争取空间受 P(并集) ≤ min(1, K·p) 约束、量级为 O((K·p)²)，"
             "报收益必须同时报 K 与 K·p；不预测开奖号码、不保证盈利。")

_净EV边界 = ("只优化「保守情景下的预计净回款」，不改变任何号码被开出的概率；"
             "热门度模型只能估算中奖后的分奖人数，不能提高中奖概率；"
             "模型不可用或无差异时排序无信息，必须退化中性基线并如实标注。")


def _reject(msg: str) -> None:
    raise ValueError(msg + "；两个目标的权重无法事先约定，见模块 docstring")


def select(
    mode: str,
    *,
    lottery: str,
    K: int,
    issue: Any = None,
    # ---- 覆盖模式专用 ----
    pool: Optional[Sequence[Dict[str, Any]]] = None,
    target: Any = None,
    cap: Optional[int] = None,
    selector: str = "exact",
    relax: bool = False,
    # ---- 净EV 模式专用 ----
    ctx: Any = None,
    kappa: float = 0.0,
    seed: int = 0,
    sales_override: Optional[float] = None,
) -> Dict[str, Any]:
    """按 `mode` 走其中**一条**线，返回统一结构的结果。混用参数直接报错。

    `selector`（仅覆盖模式）：`exact` = 精确边际增益贪心（`ev.tier_exact_greedy`）；
    `proxy` = 旧的通道权重代理（`ev.tier_coverage`，保留作对照）。
    """
    if mode not in VALID_MODES:
        raise ValueError(
            f"未知模式 {mode!r}：只允许 {VALID_MODES}。"
            "★ 不提供「综合分/混合模式」——两个目标的权重无法事先约定，"
            "合成一个标量必然出现「为堆小奖牺牲高奖」的隐含交换。")

    if mode == 模式_覆盖:
        # ★ 结构性禁止：覆盖模式不得读拥挤度/热门度（κ 与倾斜都属于净EV 侧）
        if kappa not in (0, 0.0, None):
            _reject("覆盖模式不接受 kappa（机制倾斜/拥挤度属于净EV 侧的口径）")
        if ctx is not None:
            _reject("覆盖模式不接受 ctx（奖池/销量/分奖上下文与覆盖无关）")
        if not pool:
            raise ValueError("覆盖模式必须给候选池 `pool`（不许现场造票）")

        if selector == "exact":
            from ev.tier_exact_greedy import select_exact_tier
            tks, meta = select_exact_tier(lottery, pool, K, target, issue,
                                          cap=cap, relax=relax)
            detail = meta
        elif selector == "proxy":
            from ev.tier_coverage import select_tier_coverage
            tks, meta = select_tier_coverage(lottery, pool, K, target, issue, cap=cap)
            detail = meta
        else:
            raise ValueError(f"未知选择器 {selector!r}：只允许 exact / proxy")

        return {
            "模式": mode,
            "选择器": selector,
            "票": tks,
            "注数": len(tks),
            "明细": detail,
            "判决": None,                 # 覆盖模式不给"要不要参与"的判决，那是预算闸门的事
            "诚实边界": _覆盖边界,
        }

    # ---- 净EV 模式 ----
    if target is not None:
        _reject("净EV 模式不接受覆盖目标 `target`（会用覆盖目标去挑净EV票）")
    if pool is not None:
        _reject("净EV 模式不接受外部候选池 `pool`（候选来自外层全枚举的抽样，不是票面清单）")
    if issue is None:
        # ★ 不给 issue 就等于允许用未来数据：奖池/销量/分奖人数全是按期取的，
        #   `payouts.build_ctx` 内部走 `pit.as_of(lottery, issue)`，没有期号根本构造不出来。
        raise ValueError(
            "净EV 模式必须显式给 `issue`：这条线的奖池/销量/分奖人数都是按期取数的，"
            "不给期号无法保证「只用当期之前的信息」（前视禁令）")

    from ev.profit import conservative_ev as CE
    from ev.profit import payouts as P
    from ev.profit import select as PS

    c = ctx if ctx is not None else P.build_ctx(lottery, issue,
                                                sales_override=sales_override)
    res = PS.select_profit_tickets(lottery, int(K), c, kappa=float(kappa or 0.0),
                                   seed=int(seed))
    meta = dict(res.get("元数据") or {})
    v = CE.verdict(lottery, issue, sales_override=sales_override)

    # ★ 退化判定：倾斜来源为空/模型未 Go ⇒ 全部候选同分 ⇒ 排序无信息。
    #   此时必须说出来，否则"排了序"会被当成"选得更准"。
    src = str(meta.get("倾斜来源") or "")
    degraded = (not src) or src.startswith("无") or ("未Go" in src) or ("未 Go" in src)
    notes: List[str] = []
    if degraded:
        notes.append(f"倾斜来源={src or '无'} ⇒ 全部候选同分，排序退化中性基线"
                     "（本模式在此状态下不给任何选号优势）")
    if abs(float(kappa or 0.0)) < 1e-12:
        notes.append("κ=0 ⇒ 机制倾斜未启用，差异只可能来自分奖（少人买 ⇒ 分奖少）")
    if v.get("判决") == "不参与":
        notes.append("保守情景净 EV ≤ 0 且结构性上限补不上缺口 ⇒ 默认不参与；"
                     "本模式选出的票**没有**可证明的优势")

    return {
        "模式": mode,
        "选择器": "select_profit_tickets",
        "票": res.get("票") or [],
        "注数": len(res.get("票") or []),
        "明细": meta,
        "判决": v.get("判决"),
        "判决理由": v.get("理由") or "",
        "退化": degraded,
        "说明": notes,
        "诚实边界": _净EV边界,
    }
