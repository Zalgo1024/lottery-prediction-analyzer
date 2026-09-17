"""出号数量配置（固定 / 动态）—— 用户可在看板自由调整「每次出号几注」。

设计（2026-09-14 用户需求）：
  - **固定数量（fixed）**：每次出号恒等于设定值。
  - **动态数量（dynamic）**：系统按情况自适应——可能变多、可能变少，
    当"冗余度高 / edge 稳定"时逐步走低，命中率显著偏离理论期望时回补。

诚实边界（必读）：
  改变出号注数只改变**下注规模与成本**，单注中奖概率恒定（期望线性性），
  整体 EV 仍为负。动态模式下调注数**不是**"预测更准"，只是省钱 + 控制冗余。
  命中率高/低在小样本下多由噪声决定，故动态策略对"偏离"只做**温和回补**，
  且设硬下限，避免被噪声带走。

持久化：`config/ticket_size.json`（原子写：tmp + fsync + os.replace）。
未配置时回落到 config.py 的彩种默认（数字型 5 / 乐透型 100）。
"""

from __future__ import annotations

import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

# ------------------------------------------------------------
# 路径与常量
# ------------------------------------------------------------
_BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = _BASE_DIR / "config"
STORE_PATH = CONFIG_DIR / "ticket_size.json"

MODE_FIXED = "fixed"
MODE_DYNAMIC = "dynamic"
VALID_MODES = (MODE_FIXED, MODE_DYNAMIC)

# 滑轨硬边界（按彩种分别设置，下限一律 1 注）
MIN_TICKETS = 1
MAX_TICKETS_DIGITAL = 50      # 数字型：空间小、每日开奖，上限压低
MAX_TICKETS_LOTTO = 200       # 乐透型：上限 200（低于全包危险线）

# 动态策略参数
DYNAMIC_STEP_DOWN = 2         # 冗余高且 edge 稳定时，每期下调注数
DYNAMIC_STEP_UP = 2           # 命中率异常偏离时，每期上调注数
DYNAMIC_MAX_PER_ADJUST = 8    # 单次调整幅度上限
DYNAMIC_FLOOR = 1             # 动态模式硬下限（1 注）
DYNAMIC_REDUNDANCY_HIGH = 0.5  # 压缩淘汰占比 ≥ 此值视为"冗余高"
DYNAMIC_REDUNDANCY_LOW = 0.2   # 淘汰占比 ≤ 此值视为"冗余低"
DYNAMIC_DEVIATION_Z = 2.0      # 命中率偏离理论值超过 2 个标准误 → 回补

_DIGITAL = {"福彩3D", "排列3", "排列5"}


def is_digital(lottery: str) -> bool:
    return lottery in _DIGITAL


def max_tickets(lottery: str) -> int:
    return MAX_TICKETS_DIGITAL if is_digital(lottery) else MAX_TICKETS_LOTTO


def default_count(lottery: str, fallback: int = 100) -> int:
    """未显式配置时的默认注数（与 config.py 的历史口径一致）。"""
    return 5 if is_digital(lottery) else fallback


# ------------------------------------------------------------
# 原子读写
# ------------------------------------------------------------
def _atomic_write_json(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_config() -> Dict[str, Any]:
    """读取配置；文件缺失/损坏时返回空 dict（由上层回落默认）。"""
    try:
        raw = STORE_PATH.read_text(encoding="utf-8")
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except (OSError, ValueError):
        pass
    return {}


def save_config(payload: Dict[str, Any]) -> Dict[str, Any]:
    """写入配置（原子）。返回清洗后的配置。"""
    clean = sanitize(payload)
    _atomic_write_json(STORE_PATH, clean)
    return clean


# ------------------------------------------------------------
# 清洗 / 归一
# ------------------------------------------------------------
def _as_int(v: Any, fallback: int) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return fallback


def _clamp(v: int, lottery: str) -> int:
    lo = MIN_TICKETS
    hi = max_tickets(lottery)
    return max(lo, min(hi, int(v)))


def sanitize(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """归一化任意输入为规范结构。

    结构：
      {
        "mode": "fixed" | "dynamic",
        "fixed":  {"双色球": 40, ...},
        "dynamic": {"双色球": 40, ...},   # 动态模式的当前值（每期由策略更新）
        "current": {"双色球": 40, ...},   # 最近一次实际生效值（只读展示）
        "policy": {"更新于": "...", "理由": "..."}
      }
    """
    payload = payload or {}
    mode = str(payload.get("mode") or MODE_FIXED).lower()
    if mode not in VALID_MODES:
        mode = MODE_FIXED

    from config import LOTTERY_CONFIG

    fixed_in = payload.get("fixed") or {}
    dyn_in = payload.get("dynamic") or {}
    cur_in = payload.get("current") or {}

    fixed: Dict[str, int] = {}
    dynamic: Dict[str, int] = {}
    current: Dict[str, int] = {}
    for lot in LOTTERY_CONFIG:
        d = default_count(lot)
        if not is_digital(lot) and lot in fixed_in:
            d = _as_int(fixed_in.get(lot), d)
        elif is_digital(lot) and lot in fixed_in:
            d = _as_int(fixed_in.get(lot), d)
        fixed[lot] = _clamp(d, lot)

        dv = _as_int(dyn_in.get(lot), fixed[lot])
        dynamic[lot] = _clamp(dv, lot)

        cv = _as_int(cur_in.get(lot), fixed[lot] if mode == MODE_FIXED else dynamic[lot])
        current[lot] = _clamp(cv, lot)

    return {
        "mode": mode,
        "fixed": fixed,
        "dynamic": dynamic,
        "current": current,
        "policy": payload.get("policy") or {},
    }


# ------------------------------------------------------------
# 生效值
# ------------------------------------------------------------
def effective_count(lottery: str, cfg: Optional[Dict[str, Any]] = None) -> int:
    """取某彩种当前**生效**的注数（固定模式=fixed，动态模式=dynamic 当前值）。"""
    cfg = cfg if cfg is not None else sanitize(load_config())
    if not isinstance(cfg, dict) or "fixed" not in cfg:
        cfg = sanitize(cfg)
    if cfg.get("mode") == MODE_DYNAMIC:
        return _clamp(_as_int(cfg["dynamic"].get(lottery), default_count(lottery)), lottery)
    return _clamp(_as_int(cfg["fixed"].get(lottery), default_count(lottery)), lottery)


# ------------------------------------------------------------
# 动态策略
# ------------------------------------------------------------
def _redundancy_ratio(kpi: Optional[Dict[str, Any]]) -> Optional[float]:
    """从 compress_tickets 的 KPI 估算冗余淘汰占比（0~1）。"""
    if not isinstance(kpi, dict) or not kpi.get("启用"):
        return None
    n = _as_int(kpi.get("压缩前注数"), 0)
    if n <= 0:
        return None
    dropped = (_as_int(kpi.get("质量淘汰"), 0)
               + _as_int(kpi.get("重复淘汰"), 0)
               + _as_int(kpi.get("重叠淘汰"), 0))
    return max(0.0, min(1.0, dropped / float(n)))


def decide_next_count(
    lottery: str,
    *,
    current: Optional[int] = None,
    last_kpi: Optional[Dict[str, Any]] = None,
    hit_rate: Optional[float] = None,
    expected_rate: Optional[float] = None,
    hit_n: Optional[int] = None,
) -> Dict[str, Any]:
    """动态模式的下一期建议注数（纯函数，便于测试）。

    规则（用户选择「两者结合」）：
      1) **冗余度为主**：上一批压缩淘汰占比高（冗余大）→ 下调；
         冗余低（出号已经精简）→ 保持/轻微下调；已在下限则不动。
      2) **命中率安全阀**：累计命中率显著偏离理论期望（|z|>2）→ 强制回补到
         不低于上一次的注数（避免把注数压得过低后失去统计功效）。
      3) 硬下限 DYNAMIC_FLOOR，上限按彩种类型。

    返回 {"next": int, "reason": str, "冗余度": float|None, "z": float|None,
          "命中率": float|None, "理论率": float|None}
    """
    from config import resolve_groups as _rg  # 仅用于取默认基线

    cur = _as_int(current, None) if current is not None else None
    if not cur or cur <= 0:
        cur = default_count(lottery)
    cur = _clamp(cur, lottery)
    hi = max_tickets(lottery)

    # --- 命中率偏离 z 值 ---
    z: Optional[float] = None
    if hit_rate is not None and expected_rate is not None and (hit_n or 0) > 0:
        p = float(expected_rate)
        n = int(hit_n)
        if 0.0 < p < 1.0:
            se = math.sqrt(p * (1.0 - p) / n)
            if se > 0:
                z = (float(hit_rate) - p) / se

    r = _redundancy_ratio(last_kpi)

    # --- 安全阀优先：显著偏离（无论正负）都回补 ---
    if z is not None and abs(z) > DYNAMIC_DEVIATION_Z:
        step = min(DYNAMIC_STEP_UP, DYNAMIC_MAX_PER_ADJUST)
        nxt = min(hi, cur + step)
        reason = (f"命中率 {hit_rate:.2%} 偏离理论 {expected_rate:.2%} "
                  f"(z={z:.1f} > {DYNAMIC_DEVIATION_Z}) → 回补 +{nxt - cur} 注以维持统计功效")
        return {"next": nxt, "reason": reason, "冗余度": r, "z": z,
                "命中率": hit_rate, "理论率": expected_rate}

    if r is None:
        reason = "无上一批压缩数据，维持当前注数"
        return {"next": cur, "reason": reason, "冗余度": None, "z": z,
                "命中率": hit_rate, "理论率": expected_rate}

    if r >= DYNAMIC_REDUNDANCY_HIGH and cur > DYNAMIC_FLOOR:
        step = min(DYNAMIC_STEP_DOWN, DYNAMIC_MAX_PER_ADJUST)
        nxt = max(DYNAMIC_FLOOR, cur - step)
        reason = (f"上一批冗余淘汰占比 {r:.0%} ≥ {DYNAMIC_REDUNDANCY_HIGH:.0%}"
                  f"（edge 稳定、出号重叠度高）→ 下调 -{cur - nxt} 注")
    elif r <= DYNAMIC_REDUNDANCY_LOW and cur > DYNAMIC_FLOOR:
        nxt = max(DYNAMIC_FLOOR, cur - 1)
        reason = (f"上一批冗余淘汰占比 {r:.0%} ≤ {DYNAMIC_REDUNDANCY_LOW:.0%}"
                  f"（出号已精简）→ 维持并轻微下调 -{cur - nxt} 注")
    elif cur >= hi:
        nxt = hi
        reason = f"已达彩种上限 {hi} 注，保持不变"
    else:
        nxt = cur
        reason = f"上一批冗余淘汰占比 {r:.0%} 处于合理区间 → 维持 {cur} 注"

    return {"next": nxt, "reason": reason, "冗余度": r, "z": z,
            "命中率": hit_rate, "理论率": expected_rate}


def apply_dynamic_result(lottery: str, next_count: int, reason: str) -> Dict[str, Any]:
    """把动态策略的结果写回配置（更新 dynamic + current）。"""
    cfg = sanitize(load_config())
    cfg["dynamic"][lottery] = _clamp(int(next_count), lottery)
    cfg["current"][lottery] = _clamp(int(next_count), lottery)
    from datetime import datetime
    cfg["policy"] = {
        "更新于": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "彩种": lottery,
        "理由": reason,
    }
    return save_config(cfg)


def snapshot(lottery: Optional[str] = None, *, expected_rates: Optional[Dict[str, float]] = None,
             hit_stats: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """给前端用的完整状态：各彩种当前值、上限、下次建议（动态模式）。"""
    from config import LOTTERY_CONFIG, resolve_groups as _rg

    cfg = sanitize(load_config())
    lots: List[Dict[str, Any]] = []
    for lot in LOTTERY_CONFIG:
        item = {
            "lottery": lot,
            "数字型": is_digital(lot),
            "上限": max_tickets(lot),
            "下限": MIN_TICKETS,
            "默认": default_count(lot),
            "配置值": cfg["fixed"].get(lot),
            "动态值": cfg["dynamic"].get(lot),
            "当前生效": effective_count(lot, cfg),
        }
        if cfg.get("mode") == MODE_DYNAMIC:
            exp = (expected_rates or {}).get(lot)
            st = (hit_stats or {}).get(lot) or {}
            item["建议"] = decide_next_count(
                lot,
                current=cfg["dynamic"].get(lot),
                last_kpi=st.get("last_kpi"),
                hit_rate=st.get("hit_rate"),
                expected_rate=exp,
                hit_n=st.get("hit_n"),
            )
        lots.append(item)
    return {"mode": cfg.get("mode"), "lotteries": lots,
            "policy": cfg.get("policy") or {}, "store": str(STORE_PATH)}
