# -*- coding: utf-8 -*-
"""ev/mechanism/common.py —— 装载、票量校准、block bootstrap、结论工具。

只放被 A/B（及后续 C/D）共用的东西；不放任何轨道专属逻辑。

⚠️ 本模块的所有统计量都刻意**避免依赖流行度模型**——模型只在校准时出场，
   且降级必须显式。理由：主判据一旦依赖模型，"测不出缝隙"就可能是模型的锅，
   而不是真没有缝隙。

关于「为什么不用模型隐含方差当标准误」：早期有一个 `ratio_se()`（解析加权均值
se），已删除——A 轨实际走块 bootstrap，比它粗但不会假窄。结论保留在
`gap_a_ticket_ratio` 的模块 docstring、`ev/gap.py` 的报告口径段与项目记忆里。
实测依据：双色球的经验 se 0.0203 是模型隐含 se 0.0030 的 **6.7 倍**（模型只解释
得了形态特征那部分异质，实际票量分布还有极重长尾）。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

import numpy as np

# 有「一等奖注数 / 二等奖注数 / 总投注额」三列的乐透型彩种。
# 数字型（排列3/5、福彩3D）与七星彩走 A′/C/D 轨，不走 A/B。
LOTTO_LOTTERIES: Tuple[str, ...] = ("双色球", "大乐透")

# 有「销售奖级数据.csv」的数字型 + 七星彩（走 A′/C/D 轨）
DIGITAL_LOTTERIES: Tuple[str, ...] = ("排列5", "七星彩", "排列3", "福彩3D")

# 报告默认覆盖的彩种（按「判定力」从强到弱排）
ALL_LOTTERIES: Tuple[str, ...] = LOTTO_LOTTERIES + DIGITAL_LOTTERIES

# 已接入生产、但**本模块不适用**的彩种 → 原因（显式拒绝，不是「查无此彩种」）。
# ★ 为什么必须把原因写出来：七乐彩/快乐8 在 config、pending、账本、推送、看板里
#   都是一等公民，只有 A/B 轨不适用。若只报「不在适用清单内」，读的人会误以为
#   是名字打错或漏接，于是反复来问、甚至去改装载代码硬塞进来。
#   写清原因才能把「不适用」与「还没接」分开 —— 前者不该被"修"，后者才该。
NOT_APPLICABLE: Dict[str, str] = {
    "七乐彩": ("历史 CSV 只有 7 个基本号 + 1 个特别号，没有「一等奖注数 / 二等奖注数 / "
               "总投注额」三列；A/B 轨靠这三列按票量比估缝隙，缺列就无法装载。"
               "要接入必须先补齐这三列数据源，而不是放宽装载条件。"),
    "快乐8": ("开奖摇 20 个、票面按玩法选 1~10 个（10 玩法 / 39 档奖级），"
              "A/B 轨的「二元命中格 + 一等奖注数 + 总投注额」口径整体不成立；"
              "它的一等奖只在「选十中十」存在，且大部分期无人中出（分母为 0）。"),
}

DEFAULT_BOOT_SEED = 20261003
DEFAULT_BLOCK = 20
DEFAULT_B = 4000

# 标准正态 97.5 分位。**单一真源** —— 以前 `gap_a_ticket_ratio._Z95` 与
# `common.block_bootstrap_ci` 各写一份字面量，其中前者还是死代码。
Z95 = 1.959963984540054


# ------------------------------------------------------------------ 装载
@dataclass(frozen=True)
class DrawFrame:
    """按**期号升序**对齐的一批开奖记录（只保留三列齐全的期）。"""

    lottery: str
    issues: List[str]
    dates: List[date]
    reds: np.ndarray          # (n, k) int16，前区/红球
    prev: np.ndarray          # (n, k) int16，上一期红球（"上期重合数"特征用）
    y1: np.ndarray            # (n,) 一等奖注数
    y2: np.ndarray            # (n,) 二等奖注数
    sales: np.ndarray         # (n,) 总投注额（元）
    dropped: int              # 因缺列/缺销售额被跳过的期数

    @property
    def n(self) -> int:
        return len(self.y1)


def load_draw_frame(lottery: str, min_n: int = 200) -> Optional[DrawFrame]:
    """
    装载 (一期一行) 的开奖框架。样本不足返回 None（调用方显式降级）。

    跳过规则（与 ev/popularity._load_draw_rows 同源，保证口径一致）：
      · 红球位数不符合 schema → 跳过，但**仍推进 prev**（"上一期"是真实上一期，
        不是"上一个可用行"）
      · 一等奖注数 / 二等奖注数 / 总投注额 任一缺失或销售额 <= 0 → 跳过
    """
    try:
        from data.loader import load_lottery
        from data.schema import get_schema
        data = load_lottery(lottery)
    except Exception:                                    # noqa: BLE001
        return None

    red_zone = get_schema(lottery).red_zone
    k = red_zone.choose if red_zone else None
    if not k:
        return None

    recs = sorted(data.records, key=lambda r: r.期号)
    issues: List[str] = []
    dates: List[date] = []
    reds: List[List[int]] = []
    prevs: List[List[int]] = []
    y1: List[float] = []
    y2: List[float] = []
    sales: List[float] = []
    last_red: Optional[List[int]] = None
    dropped = 0

    for r in recs:
        red = list(getattr(r, "红球", []) or [])
        if len(red) != k:
            dropped += 1
            continue
        c1 = getattr(r, "一等奖注数", None)
        c2 = getattr(r, "二等奖注数", None)
        s = getattr(r, "总投注额", None)
        if c1 is None or c2 is None or s is None or not s or float(s) <= 0:
            dropped += 1
            last_red = red
            continue
        issues.append(str(r.期号))
        d = getattr(r, "开奖日期", None)
        dates.append(d if isinstance(d, date) else date(1970, 1, 1))
        reds.append(red)
        prevs.append(last_red if last_red is not None else red)
        y1.append(float(c1))
        y2.append(float(c2))
        sales.append(float(s))
        last_red = red

    if len(reds) < min_n:
        return None
    return DrawFrame(
        lottery=lottery, issues=issues, dates=dates,
        reds=np.asarray(reds, dtype=np.int16),
        prev=np.asarray(prevs, dtype=np.int16),
        y1=np.asarray(y1, dtype=np.float64),
        y2=np.asarray(y2, dtype=np.float64),
        sales=np.asarray(sales, dtype=np.float64),
        dropped=dropped,
    )


# ------------------------------------------------------------------ 奖级概率
def prize_probs(lottery: str) -> Dict[str, float]:
    """{'一等': p1, '二等': p2}——组合数学精确值，与号码无关（ev/payout.py 为唯一权威）。"""
    from ev.payout import grade_probabilities
    g = grade_probabilities(lottery)
    if "一等" not in g or "二等" not in g:
        raise ValueError(f"{lottery} 无 一等/二等 概率（非乐透型）")
    return {"一等": float(g["一等"]), "二等": float(g["二等"])}


# ------------------------------------------------------------------ 统计工具
def block_bootstrap_ci(stat: Callable[[np.ndarray], float], n: int,
                       block: int = DEFAULT_BLOCK, B: int = DEFAULT_B,
                       seed: int = DEFAULT_BOOT_SEED,
                       alpha: float = 0.05) -> Dict[str, float]:
    """
    对「按索引取子样本再算统计量」的函数做**移动块 bootstrap**。

    为什么不是 iid bootstrap：开奖号本身独立，但销售额/奖池滚存有强自相关，
    且 r_t 的尾部很重；块 bootstrap 对二者都稳健（实测双色球 se 从 0.0203 降到
    0.0183，方向合理且不会假窄）。

    返回 {'ci_lo','ci_hi','se','B'}；se 由 (hi-lo)/z_{1-a/2} 反推。
    """
    if n <= 0:
        return {"ci_lo": float("nan"), "ci_hi": float("nan"),
                "se": float("nan"), "B": 0}
    rng = np.random.default_rng(seed)
    block = max(1, min(block, n))
    n_blk = int(math.ceil(n / block))
    offs = np.arange(block)
    vals = np.empty(B, dtype=np.float64)
    for b in range(B):
        st = rng.integers(0, n, size=n_blk)
        idx = (st[:, None] + offs[None, :]).ravel()[:n] % n
        vals[b] = stat(idx)
    lo, hi = (float(v) for v in np.percentile(vals, [100 * alpha / 2,
                                                    100 * (1 - alpha / 2)]))
    zz = Z95
    return {"ci_lo": lo, "ci_hi": hi, "se": (hi - lo) / (2 * zz), "B": B,
            "block": block}


def autocorr(x: np.ndarray, lags: Tuple[int, ...] = (1, 2, 5, 10)) -> Dict[str, float]:
    """r_t 的样本自相关——用来判断块 bootstrap 是否必要（也用来发现异常结构）。"""
    x = np.asarray(x, dtype=np.float64)
    out: Dict[str, float] = {}
    for k in lags:
        if len(x) <= k + 2:
            out[f"lag{k}"] = float("nan")
            continue
        a, b = x[:-k], x[k:]
        sa, sb = a.std(), b.std()
        out[f"lag{k}"] = float(np.corrcoef(a, b)[0, 1]) if sa > 0 and sb > 0 else float("nan")
    return out


# ------------------------------------------------------------------ 票量校准
def ticket_calibration(fr: DrawFrame, *, block: int = DEFAULT_BLOCK,
                       B: int = DEFAULT_B, seed: int = DEFAULT_BOOT_SEED,
                       tol_sigma: float = 4.0) -> Dict[str, Any]:
    """
    检验「票量 = 总投注额 ÷ 2」这个**被全项目默认使用的前提**。

    做法：公平开奖下 E[二等奖注数] = 票量 · p2（p2 是组合常数），
          于是票量可由二等奖注数**独立反推**，与销售额口径互校。

    实测（2026-10-03）：
      双色球 factor = 1.0041 → 前提成立（无追加，2 元一注）
      大乐透 factor = 1.1324 → **前提不成立**，销售额/2 高估票量 13.24%，
                                差额来自追加投注（2 元基本 + 1 元追加）：
                                1 + f/2 = 1.1324 ⇒ 追加率 f ≈ 26.5%

    后果：任何用 sales/2 当票量的大乐透推断都要打这个折。
          **A 轨因此改用免票量的 一等/二等 比值口径**（见 gap_a_ticket_ratio）。
    """
    p = prize_probs(fr.lottery)
    n = fr.n
    n_from_2 = float(fr.y2.sum()) / (n * p["二等"])
    n_from_sales = float((fr.sales / 2.0).mean())
    factor = n_from_sales / n_from_2 if n_from_2 > 0 else float("nan")

    def _stat(idx: np.ndarray) -> float:
        s = idx.sum()
        a = float(fr.y2[idx].sum()) / (len(idx) * p["二等"])
        b = float((fr.sales[idx] / 2.0).mean())
        return b / a if a > 0 else float("nan")

    boot = block_bootstrap_ci(_stat, n, block=block, B=B, seed=seed)
    ok = bool(abs(factor - 1.0) <= tol_sigma * boot["se"]) if boot["se"] == boot["se"] else False

    # 追加率反解：factor = 1 + f/2 ⇒ f = 2(factor-1)。
    # ⚠️ 只在偏差**显著**时报数：否则双色球的 0.5% 抽样噪声会被读成
    #    「追加率 1.3%」这种不存在的结论（实测踩到）。
    addon = max(0.0, 2.0 * (factor - 1.0)) if not ok else 0.0
    if ok:
        note = "票量前提成立：总投注额/2 可作为票量"
    else:
        note = (f"票量前提**不成立**：销售额/2 相对二等反推高估 "
                f"{100*(factor-1):.2f}%（推定追加率≈{100*addon:.1f}%）。"
                f"用销售额当票量的推断须按此折减；"
                f"A 轨主口径改用免票量的 一等/二等 比值。")
    return {
        "N_from_二等": n_from_2,
        "N_from_销售额": n_from_sales,
        "factor": factor,
        "factor_ci": [boot["ci_lo"], boot["ci_hi"]],
        "ok": ok,
        "推定追加率": addon,
        "note": note,
    }


# ------------------------------------------------------------------ JSON 原子写
def atomic_write_json(path: str, obj: Any) -> None:
    """先写临时文件再 os.replace（共享 JSON 的既有约定）。"""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(obj, fh, ensure_ascii=False, indent=1, default=str)
        os.replace(tmp, path)
    except BaseException:                                # noqa: BLE001
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
