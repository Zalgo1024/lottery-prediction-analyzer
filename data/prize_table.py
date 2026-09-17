"""
data/prize_table.py —— 奖级 → 单注奖金 的权威映射（唯一来源）

用途：把 feedback / 命中记录里的「中奖等级」翻译成「这一注能中多少钱」。
规则依据（官方，2026-09 核对；机制说明见 ev/rulebook.py 头部）：

  双色球        三等 3000 / 四等 200 / 五等 10 / 六等 5        （一、二等浮动）
  大乐透        三等 10000 / 四等 3000 / 五等 300 / 六等 200 /
                七等 100 / 八等 15 / 九等 5                    （一、二等浮动）
  福彩3D/排列3  直选 1040 / 组选3 346 / 组选6 173
  排列5         直选 100000（受《风险控制办法》约束，井喷时按实际单注奖金下调）
  七星彩        三等 3000 / 四等 500 / 五等 30 / 六等 5        （一、二等浮动）

每注 2 元。浮动奖级（双/大/七星彩的一、二等）优先读**当期实际单注奖金**
（已落库在 lottery_data / 销售数据 CSV），读不到则回退名义参考值并标注「浮动」。

⚠️ 命名坑：feedback 里奖级有两套写法 —— 乐透型 "一等"/"五等"（_classify_prize）
   与七星彩 "一等奖"/"五等奖"（_qxc_prize）。本模块统一归一化为 "N等" 键，
   两套写法都能查。
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# 固定奖级：彩种 → {奖级: 单注金额}
FIXED_PAYOUT: Dict[str, Dict[str, float]] = {
    "双色球": {"三等": 3000, "四等": 200, "五等": 10, "六等": 5},
    "大乐透": {"三等": 10000, "四等": 3000, "五等": 300, "六等": 200,
               "七等": 100, "八等": 15, "九等": 5},
    "福彩3D": {"直选": 1040, "组选3": 346, "组选6": 173},
    "排列3": {"直选": 1040, "组选3": 346, "组选6": 173},
    "排列5": {"直选": 100000},
    "七星彩": {"三等": 3000, "四等": 500, "五等": 30, "六等": 5},
}

# 浮动奖级的名义参考值（仅当读不到当期实际单注奖金时用于展示「约」）
FLOATING_REF: Dict[str, Dict[str, Optional[float]]] = {
    "双色球": {"一等": 10000000, "二等": None},   # 头奖封顶 1000 万
    "大乐透": {"一等": 10000000, "二等": None},
    "七星彩": {"一等": 5000000, "二等": None},    # 封顶 500 万
}

COST = 2.0  # 单注成本（元）

# 拼音数字 → 阿拉伯（用于归一化 "一等奖" → "一等"）
_CN = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_NUM2CN = {v: k for k, v in _CN.items()}

# 内部缓存：彩种 → (加载时间戳, {期号(int): {"一等": 金额, "二等": 金额}})
# 带 TTL：Flask worker 长期驻留，新一期开出并回填奖金后需能感知（避免永久陈旧）。
_ACTUAL_CACHE: Dict[str, tuple] = {}
_CACHE_TTL = 120.0  # 秒


def _norm_grade(grade: str) -> str:
    """归一化奖级名：'一等奖'/'一等'/'一等奖奖' → '一等'；玩法名原样返回。"""
    g = str(grade or "").strip()
    g = g.replace("奖", "")  # "一等奖" → "一等"；"直选"不受影响
    return g


def clear_cache():
    """清空当期实际奖金缓存（测试/数据刷新后调用）。"""
    _ACTUAL_CACHE.clear()


def _load_actual_payouts(lottery: str) -> Dict[int, Dict[str, float]]:
    """加载该彩种「期号 → {奖级: 当期实际单注奖金}」；失败返回空表（TTL 缓存）。"""
    hit = _ACTUAL_CACHE.get(lottery)
    if hit and (time.time() - hit[0]) < _CACHE_TTL:
        return hit[1]

    out: Dict[int, Dict[str, float]] = {}
    try:
        if lottery in ("双色球", "大乐透"):
            from data.loader import load_lottery
            for r in load_lottery(lottery).records:
                issue = getattr(r, "期号", None)
                if not issue:
                    continue
                vals = {}
                for lv, attr in (("一等", "一等奖奖金"), ("二等", "二等奖奖金")):
                    v = getattr(r, attr, None)
                    try:
                        v = float(v) if v is not None else 0.0
                    except (TypeError, ValueError):
                        v = 0.0
                    if v > 0:
                        vals[lv] = v
                if vals:
                    out[int(issue)] = vals
        else:
            # 数字型浮动奖级（七星彩一/二等；排列5 直选受风险控制）
            from data.fetch_sales import load_sales
            df = load_sales(lottery)
            if df is not None and not df.empty:
                cand = {"排列5": {"直选": "一等奖单注奖金"},
                        "七星彩": {"一等": "一等奖单注奖金", "二等": "二等奖单注奖金"}}
                for lv, col in cand.get(lottery, {}).items():
                    if col not in df.columns:
                        continue
                    for _, row in df.iterrows():
                        try:
                            issue = int(row["期号"])
                        except (TypeError, ValueError, KeyError):
                            continue
                        v = row[col]
                        try:
                            v = float(v)
                        except (TypeError, ValueError):
                            continue
                        if v == v and v > 0:  # 排除 NaN
                            out.setdefault(issue, {})[lv] = v
    except Exception as e:  # 任何数据源问题都降级为「用名义值」
        logger.debug(f"读取 {lottery} 当期实际单注奖金失败，将使用名义值: {e}")

    _ACTUAL_CACHE[lottery] = (time.time(), out)
    return out


def _fmt_amount(v: float) -> str:
    """金额格式化：整数不带小数，带千分位。"""
    if v is None:
        return "—"
    if abs(v - round(v)) < 1e-6:
        return f"¥{int(round(v)):,}"
    return f"¥{v:,.2f}"


def prize_payout(lottery: str, grade: str, issue: Any = None) -> Dict[str, Any]:
    """
    查询「该彩种 + 该奖级（+ 该期号）」的单注奖金。

    返回：
      {
        "金额": float | None,       # 单注金额（元）；浮动且无实际值时 None
        "浮动": bool,               # 是否浮动奖级
        "来源": str,                # 固定奖金 / 当期实际单注奖金 / 名义参考 / 未知奖级
        "文本": str,                # 直接可展示的文案
        "备注": str,                # 悬浮提示用
      }
    """
    g = _norm_grade(grade)
    fixed = FIXED_PAYOUT.get(lottery, {})

    # 1) 固定奖级
    if g in fixed:
        amt = float(fixed[g])
        return {"金额": amt, "浮动": False, "来源": "固定奖金",
                "文本": _fmt_amount(amt), "备注": f"{lottery} {grade} 固定奖金 {_fmt_amount(amt)}/注"}

    # 2) 浮动奖级
    float_ref = FLOATING_REF.get(lottery, {})
    if g in float_ref:
        actual = None
        if issue is not None:
            try:
                actual = _load_actual_payouts(lottery).get(int(issue), {}).get(g)
            except (TypeError, ValueError):
                actual = None
        if actual:
            return {"金额": float(actual), "浮动": True, "来源": "当期实际单注奖金",
                    "文本": _fmt_amount(actual),
                    "备注": f"{lottery} 第{issue}期 {grade} 当期实际单注奖金（已含奖池与撞号分摊）"}
        ref = float_ref.get(g)
        if ref:
            return {"金额": None, "浮动": True, "来源": "名义参考",
                    "文本": f"浮动（上限 {_fmt_amount(ref)}）",
                    "备注": f"{lottery} {grade} 为浮动奖级，金额随奖池与中奖注数变化"
                            f"（无当期数据，仅显示名义上限）"}
        return {"金额": None, "浮动": True, "来源": "名义参考",
                "文本": "浮动（视奖池）",
                "备注": f"{lottery} {grade} 为浮动奖级，本地无当期单注奖金数据"}

    # 3) 未中 / 未知
    return {"金额": 0.0, "浮动": False, "来源": "未中",
            "文本": "—", "备注": f"{lottery} {grade}：无奖金（未中奖或奖级未知）"}


def gross_and_net(lottery: str, grade: str, issue: Any = None) -> Dict[str, Any]:
    """在 prize_payout 基础上追加单注净盈亏（扣 2 元成本）。

    语义：
      - 中奖（金额已知）：net = payout - cost
      - 未中：net = 0（命中历史页只回看"如果买了能赚多少"，未中不计入成本）
      - 浮动且无当期实际值：net = None（金额未知，无法计算）
    """
    info = prize_payout(lottery, grade, issue)
    amt = info.get("金额")
    if info["来源"] == "未中":
        info["净盈亏"] = 0.0
    elif amt:
        info["净盈亏"] = round(float(amt) - COST, 2)
    else:
        info["净盈亏"] = None
    return info


if __name__ == "__main__":
    import os
    import sys
    # 直接以脚本运行（python data/prize_table.py）时 sys.path[0] 是 data/，
    # `import data.loader` 会失败并被静默降级为名义值 → 先把项目根加入 path。
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    demo = [("双色球", "五等", 26105), ("双色球", "一等", 26105),
            ("大乐透", "四等", 26103), ("大乐透", "一等", 26103),
            ("福彩3D", "直选", 2026243), ("福彩3D", "组选6", 2026243),
            ("排列5", "直选", 26243), ("七星彩", "六等", 26104), ("双色球", "未中", 26105)]
    for lot, grade, issue in demo:
        p = prize_payout(lot, grade, issue)
        print(f"{lot:5s} {grade:4s} 期{issue} → {p['文本']:22s} [{p['来源']}]")
