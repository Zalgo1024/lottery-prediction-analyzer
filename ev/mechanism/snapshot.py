"""ev/mechanism/snapshot.py —— 机制缝隙**快照**（只读，供 pending / 看板 / 推送共用）。

——
为什么单独一个模块
——
出号时要在 pending 上记一笔「当时的机制状态」，看板要显示，推送要带 footer。
三处都要同一份**轻量**数据，但**都不能**顺手跑一次完整测量（双色球约 17s）：
出号路径不该有"顺便跑个统计"的副作用，看板每次刷新更不该。

所以这里只做一件事：**从缓存里读出最近一次测量结果，裁成标量**。
缓存缺失 ⇒ 返回 None，由调用方显式标注「未测量」（不许拿 0 / 空 dict 顶替）。

——
新鲜度由调用方负责声明
——
返回里带 `测量时间` 与 `指纹`，读者自行判断是否过期。
要最新数据请跑 `python cli.py gap`（会写缓存，本模块下次就能读到）。
"""
from __future__ import annotations

from typing import Any, Dict, Optional

__all__ = ["mechanism_snapshot", "mechanism_snapshot_all",
           "mechanism_status_card", "LOTTERIES_FOR_SNAPSHOT"]

from ev.mechanism.common import ALL_LOTTERIES          # noqa: E402

LOTTERIES_FOR_SNAPSHOT = ALL_LOTTERIES


def mechanism_snapshot(lottery: str) -> Optional[Dict[str, Any]]:
    """
    取**最近一次**四轨测量结果，裁成纯标量快照。**只读缓存，绝不触发新测量**。

    返回 None = 缓存缺失 / 缓存不可读 / 该彩种不适用（不是"测量结果为空"）。
    """
    try:
        from ev.mechanism import gap_cache             # noqa: PLC0415
    except Exception:                                   # noqa: BLE001
        return None
    try:
        o = gap_cache.load(lottery)                     # 不带指纹 = 直接读，不比对
    except Exception:                                   # noqa: BLE001
        return None
    if not isinstance(o, dict) or not o.get("applicable"):
        return None

    vd = o.get("裁决") or {}
    gm = (o.get("gamma") or {}).get("gamma_合并") or {}
    tr = o.get("track结果") or {}

    R = None
    for k in ("A", "A′"):                               # 乐透型走 A，数字型走 A′
        r_ = ((tr.get(k) or {}).get("主口径") or {}).get("R")
        if r_ is not None:
            R = r_
            break
    g = gm.get("gamma")
    sig = bool(gm.get("显著"))
    return {
        "彩种": lottery,
        "期数": o.get("n"),
        "裁决": vd.get("status"),
        "判定轨": list(vd.get("判定轨") or []),
        "应有判定轨": list(vd.get("应有判定轨") or []),
        "R": R,
        "ū": (tr.get("B") or {}).get("mean_u"),
        "γ": g,
        "γ显著": sig,
        # ★ 过红线后的 γ：**不显著 ⇒ 0.0**。消费方一律用这个字段决定倾不倾斜。
        #   只判 `γ != 0` 会绕过红线（实测：双色球 γ=-0.0185 但显著=False）。
        "γ_可倾斜": (float(g) if (sig and g) else 0.0),
        "模型Go": (o.get("B过程") or {}).get("模型Go"),
        "测量时间": o.get("_cached_at"),
        "指纹": o.get("sig"),
        "来源": "只读缓存（不触发新测量；要最新请跑 python cli.py gap）",
    }


def mechanism_snapshot_all(lotteries=LOTTERIES_FOR_SNAPSHOT) -> Dict[str, Any]:
    """六彩种快照汇总（看板 / 推送 footer 用）。未测量的彩种单独列出，不混进已测量。"""
    out: Dict[str, Any] = {}
    missing: list = []
    for lot in lotteries:
        s = mechanism_snapshot(lot)
        if s is None:
            missing.append(lot)
        else:
            out[lot] = s
    return {
        "已测量": out,
        "未测量彩种": missing,
        "已测量数": len(out),
        "来源": "只读缓存（不触发新测量）",
    }


def mechanism_status_card(lottery: str) -> Dict[str, Any]:
    """
    战绩榜 / 命中历史共用的「机制缝隙」**状态卡**数据（只读缓存，绝不触发新测量）。

    ⚠️ 它是**状态卡，不是战绩行**：γ=0（不可折算或 CI 含 0）时出号与基础策略
    逐位相同，没有独立战绩样本，所以调用方不得把它塞进 strategies 聚合。

    三种状态（`状态` 字段）：
      - `未测量`：无缓存 ⇒ 明写「尚未测量」，**不**用 0 / 空表粉饰成「无缝隙」；
      - `未激活`：γ=0（不可折算或 CI 含 0）⇒ 与基础策略逐位相同，无独立样本；
      - `已激活`：γ≠0 且过显著门 ⇒ 乘数 exp(-γ·z) 生效。

    ★ 从快照取 γ 时必须一并传 `显著` 位：快照里的 γ 是**原始测量值**，
      可能非零但 CI 含 0（双色球 γ=-0.0185 且显著=False）。只判 γ≠0 会绕过红线。
    """
    from ev.mechanism.gap_select import tilt_state        # noqa: PLC0415
    from ev.mechanism.narrative import NO_SIGNAL_CAVEAT   # noqa: PLC0415

    # ★ 「不适用」必须先于「未测量」判断（2026-11-02）：
    #   七乐彩/快乐8 不在 A/B 轨的适用范围内（原因见 common.NOT_APPLICABLE），
    #   `cli.py gap` 会直接拒绝 —— 此时再提示"运行 cli.py gap 后本卡片即有数据"
    #   是**空头承诺**，用户跑完还是这张卡。
    try:
        from ev.mechanism.common import NOT_APPLICABLE   # noqa: PLC0415
        _na_reason = NOT_APPLICABLE.get(lottery)
    except Exception:                                     # noqa: BLE001
        _na_reason = None
    if _na_reason:
        return {
            "彩种": lottery,
            "未测量": True,
            "不适用": True,
            "状态": "不适用",
            "提示": "本彩种不在 A/B 轨的适用范围内，`cli.py gap` 会拒绝执行，跑了也不会有数据。",
            "原因": _na_reason,
            "诚实说明": ("「不适用」≠「无缝隙」也≠「已证明公平」——是这套测量框架的"
                         "前提（一等奖注数/二等奖注数/总投注额三列）在本彩种上不成立。"),
        }

    snap = mechanism_snapshot(lottery)
    if snap is None:
        return {
            "彩种": lottery,
            "未测量": True,
            "状态": "未测量",
            "提示": ("尚无测量缓存。运行 `python cli.py gap` 后本卡片即有数据"
                     "（约十几秒，会写缓存）。"),
            "诚实说明": ("「未测量」≠「无缝隙」也≠「已证明公平」——只是还没测。"
                         "本卡片只读缓存、不触发测量，也不进战绩统计。"),
        }

    # ★ 原始 γ + 显著位交 tilt_state 过红线（不显著 ⇒ 与 γ=0 同处理）
    tilt = tilt_state(lottery, gamma=snap.get("γ"),
                      significant=snap.get("γ显著"))
    card: Dict[str, Any] = {
        "彩种": lottery,
        "未测量": False,
        "状态": "已激活" if tilt.get("倾斜") else "未激活",
        "快照": snap,
        "机制倾斜": tilt,
        "来源": snap.get("来源"),
    }
    if tilt.get("倾斜"):
        card["说明"] = tilt.get("说明")
    else:
        # 「未检出 / 不显著」类断言必须带功效从句（单一真源，勿手抄）
        card["说明"] = (tilt.get("说明") or "") + NO_SIGNAL_CAVEAT
    return card
