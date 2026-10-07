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

__all__ = ["mechanism_snapshot", "mechanism_snapshot_all", "LOTTERIES_FOR_SNAPSHOT"]

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
