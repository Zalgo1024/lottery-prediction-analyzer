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
- 净EV 模式：**默认结论是「无可证明优势」（不参与）**，并把这条结论排在返回结构的
  首行（`结论首行`）。依据是两件事，缺一不可：① 最保守情景净 EV ≤ 0；② 一等奖封顶的
  结构性上限（`P(一等)×封顶税后`）仍补不上转正缺口。②**与 δ / 奖池假设无关**，
  所以结论不依赖情景网格挑出来的单点。
- **概率口径**：`delta_scenario.grade_probabilities` 按 `outer_choose` 分流 ——
  乐透型（双色球/大乐透/七乐彩/快乐8）用无放回组合 `C(n,k)`；数字型（排列3/福彩3D/
  排列5/七星彩）用可重复数字 `10^k`。数字型因此与乐透型走同一条净EV 通路
  （排列3 直选 1/1000、七星彩一等 1/1500万，均已实测对齐；修前分别错成 1/84、1/1260）。
  纯固定奖彩种的「结构性上限」由 `structural_bound` 显式标「不适用」；
  七乐彩/快乐8 若选择器表示不了（`基本号` 票面结构 / hypergeom 不可枚举）
  判「不适用」并带上真实原因。缺席 ≠ 通过。
- **倾斜来源分级**（`_tilt_reliability`）：`measured_GO` / `walk_forward` 过 Go 门槛与
  CV ⇒ 可用于排序；`crowd_pi_v2` 是**未检验代理**（无 Go 门槛）、以及空来源 ⇒
  不可用 ⇒ 在选号前就把 κ 强制为 0（退化中性基线），热门度只保留「估分奖人数」这一
  合法用途，不许拿不稳的分数主导选票。
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


def _conclusion_line(v: Dict[str, Any]) -> str:
    """把 `conservative_ev.verdict()` 压成**一行**默认结论。

    ★ 默认结论就是「**无可证明优势**」—— 不是「暂无」/「待观察」/「轻微为正」。
      依据有两条，缺一不可：
        ① 最保守情景（δ=0、奖池 −30%、税后）的净 EV ≤ 0；
        ② 一等奖封顶的结构性上限（`P(一等) × 封顶税后`）仍补不上「转正所需缺口」。
      ②**与 δ / 奖池假设无关** ⇒ 这条判决不依赖情景网格挑出来的单点。
      反过来，只有两条都通过（保守情景 > 0 且上限能补缺口）才升级为「可进一步评估倍数分配」，
      且升级也只是"允许另开预注册评估"，不是"建议加倍"。
    """
    con_ev = float(v.get("保守净EV_元每注") or 0.0)
    txt = str(v.get("判决") or "")
    if txt == "不参与":
        return (f"无可证明优势（默认结论 · 不参与）：保守情景净 EV = {con_ev:+.4f} 元/注 ≤ 0，"
                f"且一等奖封顶的结构性上限仍补不上转正缺口 ⇒ 在所登记的情景网格内找不到"
                f"正期望的证据（不代表永远不可能）。")
    if txt == "仅情景内正期望":
        return (f"仅情景内正期望：保守情景净 EV = {con_ev:+.4f} 元/注 > 0，"
                f"但结构性上限仍补不上缺口 ⇒ 不得据情景单点放大投入。")
    if txt == "可进一步评估倍数分配":
        return (f"可进一步评估倍数分配：保守情景净 EV = {con_ev:+.4f} 元/注 > 0，"
                f"且结构性上限显示缺口可被补上（须先登记新预注册块再动手）。")
    return f"判决={txt or '未知'}：保守情景净 EV = {con_ev:+.4f} 元/注。"


# 倾斜来源的可靠性分级（★ 判"有没有被检验过"，不是判"有没有分数"）
_TILT_VALIDATED = ("measured_GO", "walk_forward")
_TILT_PROXY = ("crowd_pi_v2",)


def _tilt_reliability(src: str, lottery: str) -> tuple:
    """倾斜来源是否可用于「买冷门更容易开出」的排序依据。返回 (reliable, tier, why)。

    ★ 依据是这个分数**有没有被检验过**，不是它"看起来有没有值"：
      · `measured_GO` / `walk_forward` —— 乐透型 μ 模型，过 Go 门槛 + CV 检验 ⇒ 可用于排序；
      · `crowd_pi_v2` —— 数字型/七星彩的**未检验代理**（位置独立是结构假设，代码明写
        "没有 Go 门槛"）⇒ 只能当"分奖人数"的粗估，**不能**当"买冷门更容易开出"的依据；
      · 空 / `无（模型未Go/不支持）` —— 没有来源 ⇒ 不可用。

    不可用时的正确处置是「退化中性基线」：把 κ 强制为 0（移除"开出概率随冷门变"的假设），
    只保留热门度在**分奖估计**里的合法作用（买冷门 ⇒ 少人分 ⇒ 实得高）。
    """
    s = str(src or "")
    if any(s.startswith(k) for k in _TILT_VALIDATED):
        return True, "validated", "来源经过 Go 门槛/CV 检验"
    if any(s.startswith(k) for k in _TILT_PROXY):
        return False, "proxy_unvalidated", (
            f"{lottery} 的倾斜来源是 crowd_pi_v2：未检验代理（位置独立是结构假设、"
            "无 Go 门槛）⇒ 不能当作'买冷门更容易开出'的依据")
    return False, "unavailable", "无可用倾斜来源（模型未 Go / 不支持 / 空）"


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
    from ev.profit import tilt as T

    try:
        c = ctx if ctx is not None else P.build_ctx(lottery, issue,
                                                    sales_override=sales_override)
    except Exception as e:                                   # noqa: BLE001
        # 彩种超出净EV 模型的表示能力（例如快乐8 的开奖摇出数 ≠ 票面数、不可枚举）
        # ⇒ 显式判「不适用」，**不要**把异常抛给调用方（那会把"不支持"伪装成"故障"）。
        reason = f"无法构造当期上下文：{type(e).__name__}: {e}"
        return {
            "结论首行": f"不适用：{lottery} 的净EV 模式给不出可信判决 —— {reason}",
            "无可证明优势": True,
            "模式": mode,
            "选择器": "select_profit_tickets",
            "票": [],
            "注数": 0,
            "明细": {"模型适用": False, "不适用原因": reason},
            "判决": "不适用",
            "判决理由": reason,
            "结构性上限能否补缺口": None,
            "退化": True,
            "说明": [f"净EV 模型不适用于 {lottery}：{reason}",
                     "本模式的判决不作数；该彩种需另走与其结局空间匹配的概率口径"],
            "诚实边界": _净EV边界,
        }

    # ★ 数字型（排列3/福彩3D/排列5/七星彩）曾在此被判「不适用」——根因不是"数字型不能算"，
    #   而是 `delta_scenario.grade_probabilities` 把**可重复数字**按无放回 `C(n,k)` 算
    #   （排列3 直选 1/84 而非 1/1000）。2026-11-03 已按 `outer_choose` 分流修正
    #   （乐透型超几何 / 数字型二项），故本闸撤除：数字型与乐透型走同一条净EV 通路。
    #   纯固定奖彩种的「结构性上限」由 `structural_bound` 显式标「不适用」；
    #   七乐彩/快乐8 若选择器表示不了，由下方 try/except 兜住并判「不适用」。

    # ---- 选号之前先判倾斜来源可靠性（决定 κ 是否被强制降为 0）----
    #   ★ 必须**先**判：κ 是选号的输入，不可靠时要在调用前就降到 0，
    #     否则未检验的热门度分数会直接主导排序（先选完再喊"退化"已经晚了）。
    pre = T.outer_tilt(lottery)
    src_pre = ""
    if pre is not None:
        src_pre = (f"{pre.source}（β as-of {pre.beta_asof}）"
                   if getattr(pre, "beta_asof", None) else pre.source)
    reliable, tier, why = _tilt_reliability(src_pre, lottery)

    kappa_req = float(kappa or 0.0)
    forced_neutral = (not reliable) and abs(kappa_req) > 1e-12
    kappa_eff = 0.0 if (not reliable) else kappa_req

    try:
        res = PS.select_profit_tickets(lottery, int(K), c, kappa=kappa_eff,
                                       seed=int(seed))
        meta = dict(res.get("元数据") or {})
        v = CE.verdict(lottery, issue, sales_override=sales_override)
    except Exception as e:                                   # noqa: BLE001
        # 选择器/判决无法表示该彩种（七乐彩 '基本号' 票面结构、快乐8 hypergeom 不可枚举）
        # ⇒ 显式判「不适用」并带上真实原因，**不要**当成故障抛出去。
        reason = f"净EV 选择器/判决无法处理该彩种：{type(e).__name__}: {e}"
        return {
            "结论首行": f"不适用：{lottery} 的净EV 模式给不出可信判决 —— {reason}",
            "无可证明优势": True,
            "模式": mode,
            "选择器": "select_profit_tickets",
            "票": [],
            "注数": 0,
            "明细": {"模型适用": False, "不适用原因": reason},
            "判决": "不适用",
            "判决理由": reason,
            "结构性上限能否补缺口": None,
            "退化": True,
            "说明": [f"净EV 模型不适用于 {lottery}：{reason}",
                     "本模式的判决不作数；该彩种需先补齐其票面结构 / 结局空间的支持再启用"],
            "诚实边界": _净EV边界,
        }

    # ★ 退化判定：来源不可靠（未检验代理 / 无来源）⇒ 退化中性基线。
    #   必须说出来，否则"排了序"会被当成"选得更准"。
    degraded = not reliable
    notes: List[str] = []
    if degraded:
        notes.append(f"倾斜来源={src_pre or '无'}（{tier}）⇒ {why}；"
                     "排序退化中性基线（本模式在此状态下不给任何选号优势）")
    if forced_neutral:
        notes.append(f"请求 κ={kappa_req:g} 已强制降为 0：来源不可靠时不允许"
                     "让未检验的热门度分数主导选票（热门度只保留「估分奖人数」这一合法用途）")
    if abs(kappa_eff) < 1e-12:
        notes.append("κ=0 ⇒ 机制倾斜未启用，差异只可能来自分奖（少人买 ⇒ 分奖少）")
    if v.get("判决") == "不参与":
        notes.append("保守情景净 EV ≤ 0 且结构性上限补不上缺口 ⇒ 默认不参与；"
                     "本模式选出的票**没有**可证明的优势")

    return {
        "结论首行": _conclusion_line(v),
        "无可证明优势": (v.get("判决") == "不参与"),
        "模式": mode,
        "选择器": "select_profit_tickets",
        "票": res.get("票") or [],
        "注数": len(res.get("票") or []),
        "明细": meta,
        "判决": v.get("判决"),
        "判决理由": v.get("理由") or "",
        "结构性上限能否补缺口": bool(v.get("结构性上限能否补缺口")),
        "退化": degraded,
        "说明": notes,
        "诚实边界": _净EV边界,
    }
