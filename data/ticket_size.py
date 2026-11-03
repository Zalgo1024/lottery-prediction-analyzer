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

# ------------------------------------------------------------
# 上限（可按彩种配置覆盖）
# ------------------------------------------------------------
# ★★ 2026-10-24 事故：原实现把上限硬编码成「数字型 50 / 乐透型 200」，
#   而生产配置恰好是 双色球 fixed=200、排列3 fixed=50 —— **两者都正好等于上限**。
#   于是用户把滑轨往右拖，前端 `max` 就到头了，表现为「注数被固定在最大值，
#   怎么调都没用」。上限本身也不该是全局一刀切：
#     · 双色球 cap=3 + MAX_POOL=8000 ⇒ 选号可行上限约 649 注（Stage 0 实测）
#     · 大乐透同结构更宽（约 3057 注）
#     · 数字型排列3/3D 直选空间 1000、排列5 空间 100000
#   故改为「按彩种给默认值 + 允许 config/ticket_size.json 的 "limits" 覆盖」，
#   并保留 HARD_CAP 防误设。
MIN_TICKETS = 1
HARD_CAP = 1000                       # 绝对上限（任何配置都不得超过）

# ★★ 2026-11-02 用户要求：**上限统一为 500**，不再按彩种给 400/300/200/100。
#   原实现是逐彩种给值（各有空间/耗时/预算理由，见下），但滑轨上出现
#   「上限 500 / 400 / 300 / 200 / 100」五种刻度，用户要逐个记哪个能拖到哪 ——
#   这是**操作成本**，不是安全收益：真正的安全约束在预算闸门（`ev/budget_gate`），
#   不在这个数上。故统一到 500。
#
#   各彩种 500 注的可行性（不是拍脑袋，是空间/覆盖的客观上界）：
#     · 双色球 cap=3 + MAX_POOL=8000 ⇒ 选号可行约 649 注（Stage 0 实测）> 500 ✔
#     · 大乐透同结构更宽（约 3057 注）✔
#     · 七星彩 位置型 10^7 组合 ✔
#     · 排列3 / 福彩3D 直选空间 1000 ⇒ 500 注 = 50% 覆盖 ✔（未触 `_ensure_zone_coverage` 缺口）
#     · 排列5 空间 100000 ⇒ 500 注 = 0.5% ✔
#     · 七乐彩 C(30,7)≈203 万 ✔
#     · 快乐8 选十 C(80,10)≈1.6e12 ✔
#
#   ★★ 但「上限 500」≠「可以随便买 500」：真正的硬约束是
#      `config/budget_aggregate.json` 的 `月度总额` 与 `单期上限`（闸门启用时）。
#      全部 8 彩种都按 500 出号 = 172,000 元/月（4 个 13 期彩种 52,000 + 4 个日开彩种 120,000），
#      是当前月度总额 61,330 元的约 2.8 倍 ⇒ 月中就会停发。
#      改上限只放开**可拖拽范围**，不改实际出号注数；实际注数由滑轨/动态值决定。
DEFAULT_LIMITS = {
    "双色球": 500,
    "大乐透": 500,
    "七星彩": 500,
    "排列3": 500,
    "福彩3D": 500,
    "排列5": 500,
    # ★ 2026-11-01/02 接入的两个新彩种必须在此显式给值，
    #   否则会落到 `_FALLBACK_LIMIT_*` 兜底（历史上因此出现过「按 13 期/月想当然」的错配）。
    "七乐彩": 500,
    "快乐8": 500,
}
_FALLBACK_LIMIT_DIGITAL = 500
_FALLBACK_LIMIT_LOTTO = 500

# 动态策略参数
DYNAMIC_STEP_DOWN = 2         # 冗余高且 edge 稳定时，每期下调注数
DYNAMIC_MAX_PER_ADJUST = 8    # 单次调整幅度上限
DYNAMIC_FLOOR = 1             # 动态模式硬下限（1 注）
DYNAMIC_REDUNDANCY_HIGH = 0.5  # 压缩淘汰占比 ≥ 此值视为"冗余高"
DYNAMIC_REDUNDANCY_LOW = 0.2   # 淘汰占比 ≤ 此值视为"冗余低"
DYNAMIC_DEVIATION_Z = 2.0      # 命中率偏离理论值超过 2 个标准误 → **仅诊断**
# 命中率偏离只作诊断所需的最小独立观测数（= 批次数，不是注数！）
MIN_BATCHES_FOR_DEVIATION = 20

_DIGITAL = {"福彩3D", "排列3", "排列5"}


def is_digital(lottery: str) -> bool:
    return lottery in _DIGITAL


def _resolve_limits(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, int]:
    """各彩种上限：`DEFAULT_LIMITS` ←← `cfg["limits"]` 覆盖，并夹在 [1, HARD_CAP]。"""
    from config import LOTTERY_CONFIG

    src = {}
    if isinstance(cfg, dict):
        raw = cfg.get("limits")
        if isinstance(raw, dict):
            src = raw
    out: Dict[str, int] = {}
    for lot in LOTTERY_CONFIG:
        d = DEFAULT_LIMITS.get(
            lot, _FALLBACK_LIMIT_DIGITAL if is_digital(lot) else _FALLBACK_LIMIT_LOTTO)
        try:
            v = int(src.get(lot, d))
        except (TypeError, ValueError):
            v = d
        out[lot] = max(MIN_TICKETS, min(HARD_CAP, v))
    return out


def max_tickets(lottery: str, cfg: Optional[Dict[str, Any]] = None) -> int:
    lim = _resolve_limits(cfg if cfg is not None else load_config()).get(lottery)
    if lim is None:
        return _FALLBACK_LIMIT_DIGITAL if is_digital(lottery) else _FALLBACK_LIMIT_LOTTO
    return int(lim)


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


def _clamp(v: int, lottery: str, limits: Optional[Dict[str, int]] = None) -> int:
    lo = MIN_TICKETS
    if limits is None:
        hi = max_tickets(lottery)
    else:
        hi = int(limits.get(lottery) or max_tickets(lottery))
    return max(lo, min(hi, int(v)))


def sanitize(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """归一化任意输入为规范结构。

    结构：
      {
        "mode": "fixed" | "dynamic",
        "fixed":  {"双色球": 40, ...},
        "dynamic": {"双色球": 40, ...},   # 动态模式的当前值（每期由策略更新）
        "current": {"双色球": 40, ...},   # 最近一次实际生效值（只读展示）
        "policy": {"更新于": "...", "理由": "..."},
        "limits": {"双色球": 500, ...},   # 各彩种上限（可覆盖 DEFAULT_LIMITS）
        "锁定": false                     # true ⇒ 动态策略不再自动改动注数
      }
    """
    payload = payload or {}
    mode = str(payload.get("mode") or MODE_FIXED).lower()
    if mode not in VALID_MODES:
        mode = MODE_FIXED

    from config import LOTTERY_CONFIG

    limits = _resolve_limits(payload)

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
        fixed[lot] = _clamp(d, lot, limits)

        dv = _as_int(dyn_in.get(lot), fixed[lot])
        dynamic[lot] = _clamp(dv, lot, limits)

        cv = _as_int(cur_in.get(lot), fixed[lot] if mode == MODE_FIXED else dynamic[lot])
        current[lot] = _clamp(cv, lot, limits)

    return {
        "mode": mode,
        "fixed": fixed,
        "dynamic": dynamic,
        "current": current,
        "policy": payload.get("policy") or {},
        "limits": limits,
        "锁定": bool(payload.get("锁定")),
    }


# ------------------------------------------------------------
# 生效值
# ------------------------------------------------------------
def effective_count(lottery: str, cfg: Optional[Dict[str, Any]] = None) -> int:
    """取某彩种当前**生效**的注数（固定模式=fixed，动态模式=dynamic 当前值）。"""
    cfg = cfg if cfg is not None else sanitize(load_config())
    if not isinstance(cfg, dict) or "fixed" not in cfg:
        cfg = sanitize(cfg)
    limits = cfg.get("limits") if isinstance(cfg.get("limits"), dict) else None
    if cfg.get("mode") == MODE_DYNAMIC:
        return _clamp(_as_int(cfg["dynamic"].get(lottery), default_count(lottery)),
                      lottery, limits)
    return _clamp(_as_int(cfg["fixed"].get(lottery), default_count(lottery)),
                  lottery, limits)


# ------------------------------------------------------------
# 配置健康度哨兵
# ------------------------------------------------------------
def health(*, path: Optional[Path] = None) -> Dict[str, Any]:
    """配置健康度哨兵 —— 判「这份配置还能不能用来出号」。

    ★ 为什么必须有它（2026-10-31 事故）
    ----------------------------------
    `effective_count()` 在配置文件缺失时会**静默回落** `default_count()`
    （数字型 5 / 乐透型 100），而 `sanitize()` 还会把 `mode` 兜底成 `fixed`、
    `锁定` 兜底成 `False`。于是「配置文件被删」这个事实，在系统里表现为：
    生效注数从 50 悄悄变成 100/100/5/5/5/100，**而且「自动路径不加注」的锁随之失效**
    （`锁定=False` ⇒ `apply_dynamic_result` 又能改注数）。机器照跑、日志全绿、
    看板照长，没有任何一处提示 —— 与「一切正常」在外部完全无法区分。

    所以判据不能是「能不能读到配置」，必须是「读到的是不是我们认可的那一份」：
      `安全` = 文件存在 ∧ mode=dynamic ∧ 锁定=true ∧ limits 覆盖全部彩种。

    ★ 不安全时**停发**，不是回落默认值继续出号。停发是可观测的事件（写台账 +
      日志 + 看板），回落是静默的，后者正是本函数要消灭的那类故障模式。
    """
    p = Path(path) if path is not None else STORE_PATH
    reasons: List[str] = []

    exists = p.exists()
    raw: Dict[str, Any] = {}
    if not exists:
        reasons.append(f"配置文件缺失：{p}")
    else:
        try:
            loaded = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
            else:
                reasons.append("配置根节点不是对象")
        except Exception as e:                                # noqa: BLE001
            reasons.append(f"配置解析失败：{type(e).__name__}: {e}")

    cfg = sanitize(raw) if raw else None
    if cfg is None:
        # 完全拿不到配置 ⇒ 上层会走 default_count，模式/锁定都是兜底值
        mode, locked = MODE_FIXED, False
        if not reasons:
            reasons.append("配置为空 ⇒ 生效值与锁定态均为兜底默认")
    else:
        mode = cfg.get("mode")
        locked = bool(cfg.get("锁定"))
        if mode != MODE_DYNAMIC:
            reasons.append(f"模式为 {mode}（非 {MODE_DYNAMIC}）")
        if not locked:
            reasons.append("锁定=false ⇒ 自动路径可改注数")
        try:
            from config import LOTTERY_CONFIG
            # ★ 必须看**文件里写的** limits，不能看 `sanitize()` 之后的：
            #   `_resolve_limits()` 会用 DEFAULT_LIMITS 兜底补全每个彩种，于是
            #   sanitize 后的 limits 永远「覆盖全部」—— 那样这个检查就是死代码。
            #   而本次事故恰恰是「兜底值看起来像配置值」（DEFAULT_LIMITS 正好等于
            #   用户以为生效的 500/400/100/200/200/300），所以判据只能是「文件里写了」。
            raw_limits = raw.get("limits") if isinstance(raw.get("limits"), dict) else {}
            missing = [lot for lot in LOTTERY_CONFIG if lot not in raw_limits]
            if missing:
                reasons.append(f"limits 未覆盖彩种：{'/'.join(missing)}")
        except Exception:                                     # noqa: BLE001
            pass

    return {
        "存在": exists,
        "模式": mode,
        "锁定": locked,
        "安全": not reasons,
        "原因": reasons,
        "路径": str(p),
    }


def stop_reason(hc: Optional[Dict[str, Any]] = None) -> str:
    """把 `health()` 的结论压成一句可写进停发台账的原因。"""
    hc = hc if hc is not None else health()
    return "出号配置不安全：" + ("；".join(hc.get("原因") or []) or "原因未知")


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


def _assert_no_step_up(cur: int, nxt: int, reason: str) -> int:
    """静态锁：动态策略**只能减或维持**，任何上调都是 bug。

    ★ 为什么写成运行时断言而不是「靠 review 保证」：2026-10-24 之前的那条
      「命中率偏离 → 回补 +2 注」让双色球单向爬到上限，用户手动调小后下一期
      又被推回，表现为「怎么调都固定在最大值」，而日志一路正常。
      这类「规则本身错」的 bug 不会抛异常，只会让一个数字静默漂移。
      断言放在返回前，任何未来改动只要引入上调路径就会立刻炸。
    """
    if int(nxt) > int(cur):
        raise AssertionError(
            f"动态注数策略出现上调：{cur} → {nxt}（{reason}）。"
            f"★ 自动路径永不加注：E[中奖注数]=K·p，加注不改变单注概率，只放大方差。")
    return int(nxt)


def decide_next_count(
    lottery: str,
    *,
    current: Optional[int] = None,
    last_kpi: Optional[Dict[str, Any]] = None,
    hit_rate: Optional[float] = None,
    expected_rate: Optional[float] = None,
    hit_n: Optional[int] = None,
    n_batches: Optional[int] = None,
    anchor: Optional[int] = None,
) -> Dict[str, Any]:
    """动态模式的下一期建议注数（纯函数，便于测试）。

    ★★ 2026-10-24 语义修正（这是「注数永远卡在最大值」的根因）：
    原规则第 2 条是「命中率 |z|>2 ⇒ 强制回补 +2 注」。它有**两个**致命问题：

    ① **方向上不成立**：`E[中奖注数] = K·p`（期望线性性），加注只把注数按比例
       放大，**单注中奖概率 p 恒定** ⇒ 命中率不会因为加注而回升。
       所以「命中率低 → 加注」这条路径在数学上永远追不上，只会单向爬到上限。
    ② **样本量上不成立**：z 原用「注数」当 n，但同一批次内的票共享蓝球
       （实测一批 ~88 注只覆盖 12/16 个蓝球），中奖事件强相关
       ⇒ 有效独立观测 ≈ **批次数**。实查 `lookback=500` 只覆盖 4 批，
       批均命中率从 0.0% 到 4.50% 剧烈跳动 ⇒ 该 z 是噪声，不是偏离。
    后果（实证）：双色球被一路推到 200 注上限（26120、26121 均 = 200），
    用户手动调小后下一期又被 +2 推回，表现为「**怎么调都固定在最大值**」。

    现规则（只有一条会动注数）：
      1) **冗余度高**（上一批压缩淘汰占比 ≥ 50%）→ 下调，硬下限 `DYNAMIC_FLOOR`。
      2) 其余一律**维持**。
    命中率偏离（|z| > 2）**只写入诊断字段**，不改注数；且要求
    `n_batches >= MIN_BATCHES_FOR_DEVIATION` 才算数，否则直接标注「样本不足」。

    参数 `anchor`（固定模式的用户设定值）**已不参与地板计算**：压缩已改为
    「出号数量 = 最终注数」（`TICKET_COMPRESS_KEEP_RATIO=1.0`），冗余下调实际
    几乎不触发，再加一层锚点地板只会让「用户设大值」后动态永远降不下来。
    保留形参是为了兼容既有调用方。

    返回 {"next", "reason", "冗余度", "z", "命中率", "理论率", "诊断", "样本够"}
    """
    cur = _as_int(current, None) if current is not None else None
    if not cur or cur <= 0:
        cur = default_count(lottery)
    cur = _clamp(cur, lottery)
    hi = max_tickets(lottery)
    floor = DYNAMIC_FLOOR

    # --- 命中率偏离 z 值（仅诊断） ---
    z: Optional[float] = None
    if hit_rate is not None and expected_rate is not None and (hit_n or 0) > 0:
        p = float(expected_rate)
        n = int(hit_n)
        if 0.0 < p < 1.0:
            se = math.sqrt(p * (1.0 - p) / n)
            if se > 0:
                z = (float(hit_rate) - p) / se

    samples_ok = (n_batches is None) or (int(n_batches) >= MIN_BATCHES_FOR_DEVIATION)
    diag = ""
    if z is not None and abs(z) > DYNAMIC_DEVIATION_Z:
        if not samples_ok:
            diag = (f"命中率 {float(hit_rate):.2%} vs 理论 {float(expected_rate):.2%}"
                    f"（名义 z={z:+.1f}，但只有 {n_batches} 批独立样本 < "
                    f"{MIN_BATCHES_FOR_DEVIATION}）⇒ 判为噪声，不改注数")
        else:
            diag = (f"命中率 {float(hit_rate):.2%} vs 理论 {float(expected_rate):.2%}"
                    f"（z={z:+.1f}）。★ 加注不改变单注中奖概率（E[注数]=K·p）"
                    f"⇒ 不据此调整注数，仅记录")

    r = _redundancy_ratio(last_kpi)

    if r is None:
        reason = "无上一批压缩数据，维持当前注数"
        return {"next": _assert_no_step_up(cur, cur, reason), "reason": reason,
                "冗余度": None, "z": z,
                "命中率": hit_rate, "理论率": expected_rate,
                "诊断": diag, "样本够": samples_ok}

    if r >= DYNAMIC_REDUNDANCY_HIGH and cur > floor:
        step = min(DYNAMIC_STEP_DOWN, DYNAMIC_MAX_PER_ADJUST)
        nxt = max(floor, cur - step)
        reason = (f"上一批冗余淘汰占比 {r:.0%} ≥ {DYNAMIC_REDUNDANCY_HIGH:.0%}"
                  f"（出号重叠度高）→ 下调 -{cur - nxt} 注")
    elif cur >= hi:
        nxt = hi
        reason = f"已达彩种上限 {hi} 注，保持不变"
    else:
        nxt = cur
        if r <= DYNAMIC_REDUNDANCY_LOW:
            reason = (f"上一批冗余淘汰占比 {r:.0%} ≤ {DYNAMIC_REDUNDANCY_LOW:.0%}"
                      f"（出号已精简，无可压缩空间）→ 维持 {cur} 注")
        else:
            reason = f"上一批冗余淘汰占比 {r:.0%} 处于合理区间 → 维持 {cur} 注"

    return {"next": _assert_no_step_up(cur, nxt, reason), "reason": reason,
            "冗余度": r, "z": z, "命中率": hit_rate, "理论率": expected_rate,
            "诊断": diag, "样本够": samples_ok}


def apply_dynamic_result(lottery: str, next_count: int, reason: str,
                         *, force: bool = False) -> Dict[str, Any]:
    """把动态策略的结果写回配置（更新 dynamic + current）。

    `锁定` 为 true 时**自动路径不得改动注数**（`force=True` 供用户手点
    「按策略调整」使用 —— 那是用户主动行为，应当生效）。
    这是「用户调好的注数不要被后台偷偷改回去」的硬开关。
    """
    cfg = sanitize(load_config())
    if cfg.get("锁定") and not force:
        return cfg
    limits = cfg.get("limits")
    cfg["dynamic"][lottery] = _clamp(int(next_count), lottery, limits)
    cfg["current"][lottery] = _clamp(int(next_count), lottery, limits)
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
    from config import LOTTERY_CONFIG

    cfg = sanitize(load_config())
    limits = cfg.get("limits") or _resolve_limits(cfg)
    lots: List[Dict[str, Any]] = []
    for lot in LOTTERY_CONFIG:
        item = {
            "lottery": lot,
            "数字型": is_digital(lot),
            "上限": limits.get(lot, max_tickets(lot, cfg)),
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
                n_batches=st.get("n_batches"),
                anchor=cfg["fixed"].get(lot),
            )
        lots.append(item)
    return {"mode": cfg.get("mode"), "lotteries": lots,
            "policy": cfg.get("policy") or {}, "store": str(STORE_PATH),
            "锁定": bool(cfg.get("锁定")),
            "上限说明": "各彩种上限可在 config/ticket_size.json 的 limits 里改（≤%d）"
                        % HARD_CAP}
