# -*- coding: utf-8 -*-
"""ev/arena/crowd.py —— 拥挤度闸门：把「暂不评估」变成一等的**状态**，而不是 0 期。

为什么要有这个模块
------------------
`ev/popularity_proxy.SUPPORTED = ("双色球", "大乐透")`，大乐透**建过模**但
`go_or_none = False`（2,932 期拟合，LRT 显著而时序 5 折 CV 仅 2/5 折优于零模型；
NB2 复核后仍 2/5）。此时 `lowcrowd` 臂**没有可用的拥挤度输入**。

朴素做法有两种，都不许：
  · 把该臂记成 0 期 / 0 达标率 —— 报表上看起来像「跑了但很惨」；
  · 为了凑齐六臂，把 Go 门槛降低 —— 用更弱的模型硬跑，得到一个没法解释的数。

本模块给出第三种：`crowd_gate()` 返回结构化的**三重探测**结果，
`可用=False` 时调用方必须把它写成「暂不评估」并附原因，且**不得计入多重比较**。

三重探测（顺序即依赖）
----------------------
1. **支持范围** `SUPPORTED`：有没有为这个彩种**建过**拥挤度代理（从未建模 = 无证据可查）；
2. **Go 门槛** `go_or_none`：建过，但模型是否达到可用门槛（No-Go = 有证据但没过）；
3. **训练样本量** `MIN_TRAIN`：可用，但历史期数够不够 walk-forward 拟合。

★ 第 3 项是「能不能拟合」，不是「值不值得跑」。三者任一不过都返回 `可用=False`，
  但 `原因` 必须说清是哪一项 —— 否则「暂不评估」会变成一个黑箱借口。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from ev import popularity_proxy as PP

# 状态取值（对外文案的单一真源）★ 过 tests/test_narrative_unbinding.py 扫描
STATE_OK = "可用"
STATE_SKIP = "暂不评估"

SKIP_NOTE = (
    "「暂不评估」= 该臂在本彩种上**没有可用的输入**，因此没有数、也不计入多重比较；"
    "它不是 0 期，不是失败，更不能被读成「该策略在此彩种上无效」。"
)


def crowd_gate(lottery: str, *, n_periods: Optional[int] = None) -> Dict[str, Any]:
    """三重探测拥挤度代理是否可用于 `lottery`。"""
    probe: Dict[str, Any] = {}

    probe["支持范围"] = {"通过": bool(PP.is_supported(lottery)),
                        "SUPPORTED": list(PP.SUPPORTED)}
    if not probe["支持范围"]["通过"]:
        return {"可用": False, "状态": STATE_SKIP, "彩种": lottery,
                "卡在": "支持范围",
                "原因": (f"{lottery} 不在拥挤度代理的支持范围 {list(PP.SUPPORTED)} 内"
                         f"（从未建模）⇒ 无输入可用。"),
                "探测": probe, "说明": SKIP_NOTE}

    try:
        go = bool(PP.go_or_none(lottery))
        go_err = None
    except Exception as e:                                        # noqa: BLE001
        go, go_err = False, f"{type(e).__name__}: {e}"
    probe["Go门槛"] = {"通过": go, "错误": go_err}
    if not go:
        # ★ 数字一律现取，不许硬编码 —— 数据每期都在长，写死的「2/5」会静默过期。
        ev = PP.go_evidence(lottery)
        probe["Go门槛"]["实测"] = ev
        n_per = ev.get("n")
        cvb = ev.get("cv_better_folds")
        cvs = ev.get("cv_sig_folds")
        lrtp = ev.get("lrt_p")
        bits = []
        if n_per is not None:
            bits.append(f"样本 {int(n_per)} 期")
        if lrtp is not None:
            bits.append(f"LRT p={float(lrtp):.3g}（显著）")
        # 主口径用显著性折数（cv_sig_folds）；旧缓存缺该字段时退回纯符号计数（cv_better_folds）
        if cvs is not None:
            bits.append(f"时序 5 折 CV 仅 {int(cvs)}/5 折逐期偏差显著低于零模型"
                        f"（Wilcoxon 单侧 p<0.05；门槛 ≥3）")
        elif cvb is not None:
            bits.append(f"时序 5 折 CV 仅 {int(cvb)}/5 折优于零模型（门槛 ≥3）")
        detail = "、".join(bits) if bits else str(ev.get("verdict", "无实测数字"))
        return {"可用": False, "状态": STATE_SKIP, "彩种": lottery,
                "卡在": "Go门槛",
                "原因": (f"{lottery} 的 popularity 模型**建过模但未达 Go 门槛**：{detail}"
                         f" ⇒ 无数据驱动的拥挤度。这不是「没做过」，是「做了没通过」。"
                         + (f" 读取异常：{go_err}" if go_err else "")),
                "探测": probe, "说明": SKIP_NOTE}

    need = int(PP.MIN_TRAIN)
    probe["训练样本量"] = {"需要": need, "实际": n_periods, "步长": int(PP.STEP)}
    if n_periods is not None and int(n_periods) < need:
        return {"可用": False, "状态": STATE_SKIP, "彩种": lottery,
                "卡在": "训练样本量",
                "原因": (f"可用期数 {int(n_periods)} < walk-forward 最小训练期数 {need}"
                         f" ⇒ 无法拟合 β。"),
                "探测": probe, "说明": SKIP_NOTE}

    return {"可用": True, "状态": STATE_OK, "彩种": lottery, "卡在": None,
            "原因": "", "探测": probe, "说明": SKIP_NOTE}


def require_usable(lottery: str, *, n_periods: Optional[int] = None) -> Dict[str, Any]:
    """可用则返回探测结果，不可用则抛 `NotImplementedError`（带原因）。"""
    g = crowd_gate(lottery, n_periods=n_periods)
    if not g["可用"]:
        raise NotImplementedError(f"{g['状态']} —— {g['原因']}")
    return g
