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

from config import ticket_price

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
    # 七乐彩四~七等：实测 3005 期各自只有 1 种金额 ⇒ 真固定奖
    "七乐彩": {"四等": 200, "五等": 50, "六等": 10, "七等": 5},
}

# 浮动奖级的名义参考值（仅当读不到当期实际单注奖金时用于展示「约」）
FLOATING_REF: Dict[str, Dict[str, Optional[float]]] = {
    "双色球": {"一等": 10000000, "二等": None},   # 头奖封顶 1000 万
    "大乐透": {"一等": 10000000, "二等": None},
    "七星彩": {"一等": 5000000, "二等": None},    # 封顶 500 万
}

COST = float(ticket_price("双色球"))  # 单注成本（元）★ 真源 config.ticket_price

# 浮动奖单注奖金的**历史均值估算**：仅用于「缺当期实际数据」时的兜底
# （静态 EV / 方差 / 凯利 这类不需要当期开奖的粗算）。
# ★ 键一律用**规则层级名**（「一等」），查找走 `estimate_floating_prize` 做归一化。
FLOATING_ESTIMATE: Dict[str, Dict[str, float]] = {
    "双色球": {"一等": 5_000_000, "二等": 200_000},
    "大乐透": {"一等": 5_000_000, "二等": 150_000},
}


# ============================================================
# 「名义受规则约束」档（regulated）—— 七乐彩 / 快乐8
# ============================================================
# ★ 为什么需要第三类，而不是塞进 fixed 或 floating（2026-11-01 实测 market.db 得出）
#
#   快乐8 的 39 档**没有一档是真固定**：
#     x10z7 = 80/80 恒值，但 x7z7 在 2026 年出现 8500（名义 1 万）；
#     x9z9 名义 30 万，2026 年有 220 期是 25 万；x10z10 有 173 期 0 元、1101 期无人中出、
#     其余期满额 500 万或按 418~498 万分摊。
#   这些既不是"固定奖金"（会随限赔/派奖变动），也不是 ssq/dlt 那种
#   "奖池按注均分"的浮动奖（`floating` 需要 float_pool 与 μ 建模，硬套会给 0 元）。
#   它们是**名义值由规则规定、单注实际金额按期由官方公布**，已含分摊/封顶/限赔/派奖。
#
#   ⇒ kind="regulated"，取值链唯一：当期实测（market.db 的 prize.amount）→ 名义值（标注）。
#     不参与 ev/profit/payouts.py 的浮动池建模：官方已经算好了，再套分薄模型只是引入误差。
#
#   七乐彩四~七等 200/50/10/5 实测 3005 期各自只有 1 种金额 ⇒ 仍是真 fixed；
#   一/二/三等分别有 2197/2905/1816 种金额 ⇒ regulated。
#   一等的 nominal 取全期正金额均值（官方不公布名义值，一等奖本就是奖池分成）。

REGULATED_NOMINAL: Dict[str, Dict[str, float]] = {
    "七乐彩": {"一等": 1_270_224, "二等": 27_414, "三等": 1_930},
    "快乐8": {
        # 选十
        "选十中十": 5_000_000, "选十中九": 8_000, "选十中八": 800,
        "选十中七": 80, "选十中六": 5, "选十中五": 3, "选十中零": 2,
        # 选九
        "选九中九": 300_000, "选九中八": 2_000, "选九中七": 200,
        "选九中六": 20, "选九中五": 5, "选九中四": 3, "选九中零": 2,
        # 选八
        "选八中八": 50_000, "选八中七": 800, "选八中六": 88,
        "选八中五": 10, "选八中四": 3, "选八中零": 2,
        # 选七
        "选七中七": 10_000, "选七中六": 288, "选七中五": 28,
        "选七中四": 4, "选七中零": 2,
        # 选六
        "选六中六": 3_000, "选六中五": 30, "选六中四": 10, "选六中三": 3,
        # 选五
        "选五中五": 1_000, "选五中四": 21, "选五中三": 3,
        # 选四
        "选四中四": 100, "选四中三": 5, "选四中二": 3,
        # 选三
        "选三中三": 53, "选三中二": 3,
        # 选二 / 选一
        "选二中二": 19, "选一中一": 4.6,
    },
}

# 单注封顶（仅 regulated 有；七乐彩一等奖无 500 万封顶——实测最大 847 万）
REGULATED_CAP: Dict[str, Dict[str, float]] = {
    "快乐8": {"选十中十": 5_000_000},
}

# 单注保底（限赔下限；七乐彩三等实测最小 400）
REGULATED_FLOOR: Dict[str, Dict[str, float]] = {
    "七乐彩": {"三等": 400},
}


def regulated_nominal(lottery: str, grade: str) -> Optional[float]:
    """regulated 档的名义单注金额；非 regulated 档返回 None。"""
    tbl = {_norm_grade(k): v for k, v in REGULATED_NOMINAL.get(lottery, {}).items()}
    return tbl.get(_norm_grade(grade))


def regulated_cap(lottery: str, grade: str) -> Optional[float]:
    tbl = {_norm_grade(k): v for k, v in REGULATED_CAP.get(lottery, {}).items()}
    return tbl.get(_norm_grade(grade))


def regulated_floor(lottery: str, grade: str) -> Optional[float]:
    tbl = {_norm_grade(k): v for k, v in REGULATED_FLOOR.get(lottery, {}).items()}
    return tbl.get(_norm_grade(grade))


def estimate_floating_prize(lottery: str, grade: str) -> float:
    """浮动奖单注金额的历史估算（按奖级名归一化查找）。

    ★ 为什么单独立成一个函数：这张表历史上在 `step2_expected` / `step4_risk` /
      `step5_betting` 里各抄了一份，且键写的是「一等奖」；而规则层（`rules_for`）
      与 `prize_probabilities_*` 用的是「一等」⇒ `.get("一等", 0)` 恒返回 **0**，
      浮动奖在静态 EV/方差/凯利里**静默消失**。三份副本就是它长期没被发现的原因。
    """
    table = {_norm_grade(k): v for k, v in FLOATING_ESTIMATE.get(lottery, {}).items()}
    return float(table.get(_norm_grade(grade), 0.0))

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


# 公开别名：判级层返回「六等奖」、规则表键是「六等」，两套写法必须走同一个归一。
# 外部模块请用这个，不要 import 下划线名（本文件历史上已有 3 份各自为政的副本）。
norm_grade = _norm_grade


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

    # ★ 大乐透有五个规则版本（8/8/6/9/7 级），固定奖金额**按期选版本**。
    #   曾因这里只读静态九级表、而概率已按 2026 七级算，导致「七等」按 100 元
    #   × 0.06 的命中率算出 +6 元/注的假正 EV（2026 的七等实际是 5 元）。
    if lottery == "大乐透":
        from data.prize_versions import DLT_VERSIONS, RuleVersionError, version_for_issue

        try:
            v = version_for_issue(issue) if issue is not None else DLT_VERSIONS[-1]
        except (RuleVersionError, ValueError):
            # 期号越界/脏数据：展示函数不抛错，回退现行版本并在备注里标注
            v = DLT_VERSIONS[-1]
        amt_v = v.fixed_amount(g)
        if amt_v is not None:
            return {"金额": float(amt_v), "浮动": False, "来源": "固定奖金",
                    "文本": _fmt_amount(amt_v),
                    "备注": f"{lottery} {grade} 固定奖金 {_fmt_amount(amt_v)}/注"
                            f"（规则版本 {v.version}）"}
        if g in v.floating:
            # 浮动奖级：优先当期实际单注奖金
            actual = None
            if issue is not None:
                try:
                    actual = _load_actual_payouts(lottery).get(int(issue), {}).get(g)
                except (TypeError, ValueError):
                    actual = None
            if actual:
                return {"金额": float(actual), "浮动": True, "来源": "当期实际单注奖金",
                        "文本": _fmt_amount(actual),
                        "备注": f"{lottery} 第{issue}期 {grade} 当期实际单注奖金（规则版本 {v.version}）"}
            ref = FLOATING_REF.get(lottery, {}).get(g)
            return {"金额": None, "浮动": True, "来源": "名义参考",
                    "文本": (f"浮动（上限 {_fmt_amount(ref)}）" if ref else "浮动（视奖池）"),
                    "备注": f"{lottery} {grade} 为浮动奖级，金额随奖池与中奖注数变化"
                            f"（规则版本 {v.version}）"}
        return {"金额": 0.0, "浮动": False, "来源": "未中",
                "文本": "—", "备注": f"{lottery} {grade}：该规则版本下不存在此奖级"}

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
