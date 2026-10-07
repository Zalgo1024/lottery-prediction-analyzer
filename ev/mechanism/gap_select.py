"""ev/mechanism/gap_select.py —— 缝隙内选号（P4）。

——
★ 硬红线：γ=0 时必须**逐位**退化到今日行为
——
`mechanism_gamma` 是红线的唯一实现点（`ev/gap.py`）：模型未 Go / γ 不可折算 /
γ 的 CI 含 0 ⇒ 一律返回 0.0。本模块在 γ=0 时：

  · `mechanism_multiplier` 返回 **None**，调用方**跳过乘法本身**（不是乘 1.0）；
  · `select_in_gap` 直接**返回传入的 base 数组对象本身**（`key is base` 可证）。

为什么连「乘 1.0」都要省：
  1. 对 -inf/inf/nan，`1.0 * x` 与 `x` 不完全等价；
  2. `np.argsort` 在**完全并列**时依赖稳定性，乘与不乘哪怕只差 1 ULP，
     并列组的相对顺序就可能变 —— 而并列在本项目里极多（全组合 110 万个
     只有 21 万个不同分数，最大并列组 149 个，见 `ev/popularity._score_std`）。
  跳过 = 零风险；乘 1.0 = 把红线押在 BLAS 的舍入方向上。

——
倾斜方向（别搞反）
——
γ 的约定见 `gap_b_drawn_quantile`：`g(c) ∝ exp(-γ·z(c))`，γ>0 = **开奖偏向冷门**。
z(c) = 全组合标准化线性打分（`popularity._score_std`，z 大 = 热门）。

我们想选「更容易被开出」的组合 ⇒ 乘数必须与 g(c) **同向**：

    mult(c) ∝ exp(-γ · ss(c))

γ>0 时 ss 大（热门）的乘数小 ⇒ 选冷门 ✓。
γ<0（若将来测出开奖偏向热门）时自动反向，无需改代码。
乘数整体归一到**均值 1**，避免污染「一等奖实得」等量纲相关的展示字段。

⚠️ 这个乘数改的是**中奖概率的权重**，不是奖金。它乘在「一等奖实得」上得到的是
「机制倾斜后的期望收益」——**仍然不是**「更可能中奖」的宣传口径：γ 不显著时
本模块根本不激活。

——
文案黑名单（γ=0 / 不可用时）
——
不得出现「更可能 / 更准 / 提高命中 / 预测号码 / 优势」——见
`tests/test_gap_select.py::test_zero_gamma_copy_is_worded_honestly`。
"""
from __future__ import annotations

import math
from typing import Any, Dict, Optional, Sequence

import numpy as np

__all__ = ["mechanism_multiplier", "select_in_gap", "tilt_state",
           "crowd_gate", "resolve_gamma", "CROWD_GATE_DEFAULT"]


def resolve_gamma(lottery: str) -> float:
    """公开入口：取当前 γ（走红线，取不到/不显著 → 0.0）。"""
    return _resolve_gamma(lottery)

CROWD_GATE_DEFAULT = 0.5          # 数字型 sampler 的反大众分位阈值（γ=0 时保持此值）
_GATE_SHIFT_PER_GAMMA = 0.10      # γ 每 +1，阈值下移多少（γ>0 = 更冷）
_GATE_MAX_SHIFT = 0.40            # 阈值下移上限（不许把阈值压到 ≤0.1 以下，否则近乎全拒）


def _resolve_gamma(lottery: str) -> float:
    """
    取 γ。**lazy import**：`ev.gap` 会 import 本包，模块级 import 会成环。

    任何异常 → 0.0（红线：取不到就不倾斜）。
    """
    try:
        from ev.gap import mechanism_gamma      # noqa: PLC0415
        return float(mechanism_gamma(lottery))
    except Exception:                            # noqa: BLE001
        return 0.0


def mechanism_multiplier(lottery: str, R: Any, *,
                         gamma: Optional[float] = None,
                         params: Optional[Dict[str, Any]] = None,
                         normalize: bool = True) -> Optional[np.ndarray]:
    """
    每个候选的**机制倾斜乘数**。

    返回 **None** = 不倾斜（调用方必须跳过乘法，不是乘 1.0）。
    返回 (m,) 数组 = 乘数（均值归一到 1）。

    R: (m, k) 红球矩阵（乐透型）。数字型走 `crowd_gate`，不走这里。
    """
    if gamma is None:
        gamma = _resolve_gamma(lottery)
    if not gamma or not np.isfinite(gamma):
        return None

    try:
        from ev import popularity as P          # noqa: PLC0415
    except Exception:                            # noqa: BLE001
        return None

    params = params if params is not None else P.get_popularity_model(lottery)
    if not params.get("go"):
        return None                              # 模型未 Go → 无 z 标尺 → 不倾斜

    try:
        beta = np.asarray(params["beta"], dtype=np.float64)
        m_ = np.asarray(params["m_"], dtype=np.float64)
        s_ = np.asarray(params["s_"], dtype=np.float64)
        F = P.features_matrix(np.asarray(R, dtype=np.int16))
        # ⚠️ 必须用 `_score_std`（逐特征累加），不能用 `Fz @ beta`：
        #    BLAS gemv 在不同行数下累加顺序不同 → 差 1 ULP → 并列组边界跳变。
        ss = P._score_std(F, m_, s_, beta)
        w = np.exp(-float(gamma) * ss)
        if not np.all(np.isfinite(w)):
            return None
        if normalize:
            mu = float(w.mean())
            if not (mu > 0) or not math.isfinite(mu):
                return None
            w = w / mu
        return w
    except Exception:                            # noqa: BLE001
        return None


def select_in_gap(lottery: str, R: Any, base: Any, *,
                  gamma: Optional[float] = None,
                  params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    把「基础分」（一等奖实得 / 限号EV / 任何越大越好的分数）乘以机制乘数。

    返回 `{"key", "倾斜", "gamma", "口径", "说明"}`。
    ★ γ=0 时 `key` **就是传入的 base 对象本身**（`key is base`），逐位一致可证。

    ⚠️ 只做乘法，**不做选择**——贪心/低重叠仍由 `ev.selection._greedy_pick` 负责，
    这样倾斜层与选择层解耦，也保证 γ=0 时连 argsort 的输入都逐位相同。

    ⚠️ `key` 是**每个候选一个 float** 的大数组（候选池可能上万）。
    要写进 pending 快照 / 看板 / JSON 时请用 **`状态`** 子字典（纯标量，可序列化），
    不要把整个返回值塞进去（P5 实测：直接塞会写进 3000 个 float）。
    """
    base_arr = np.asarray(base, dtype=np.float64)
    g = _resolve_gamma(lottery) if gamma is None else float(gamma)

    def _pack(key, active, gamma_v, 口径, 说明):
        # 顶层保留 倾斜/gamma/口径/说明 便于调用方直接读；
        # `状态` 是**不含 key** 的可序列化副本，供 pending 快照 / 看板 / JSON 用。
        return {
            "key": key,
            "倾斜": active,
            "gamma": float(gamma_v),
            "口径": 口径,
            "说明": 说明,
            "状态": {"倾斜": active, "gamma": float(gamma_v),
                     "口径": 口径, "说明": 说明},
        }

    if not g or not np.isfinite(g):
        return _pack(
            base_arr if isinstance(base, np.ndarray) else base, False, 0.0,
            "γ=0（不可折算或未过显著门）→ 不做任何倾斜",
            "机制倾斜未激活：γ=0，本结果与不做倾斜时**逐位相同**。"
            "这不表示开奖已证明公平，只表示当前测量未支持非零倾斜。")

    w = mechanism_multiplier(lottery, R, gamma=g, params=params)
    if w is None or len(w) != len(base_arr):
        return _pack(
            base_arr if isinstance(base, np.ndarray) else base, False, g,
            "γ≠0 但乘数不可用（模型未 Go / 打分失败）→ 显式降级为不倾斜",
            "机制倾斜未激活：γ 非零但拿不到流行度打分（模型未 Go），"
            "按「不许静默降级」纪律显式回退，不拿别的口径顶替。")
    return _pack(
        base_arr * w, True, g,
        "乘数 = exp(-γ·z(组合))，z=全组合标准化线性打分（z 大=热门），归一到均值 1",
        f"机制倾斜已激活：γ={g:.4f}（{'偏向冷门' if g > 0 else '偏向热门'}）。"
        "乘数与开奖倾向同向，改的是**中奖概率权重**；"
        "γ 的显著性由四轨测量单独给出，不代表预测号码。")


def tilt_state(lottery: str, *, gamma: Optional[float] = None,
               reason: Optional[str] = None,
               significant: Optional[bool] = None) -> Dict[str, Any]:
    """
    当前彩种的倾斜状态（给 pending 快照 / 看板 / 推送 footer 用）。

    γ=0 → `倾斜=False`，且 `说明` 走诚实文案（不含「更可能/更准/提高命中」）。

    gamma: 显式给（P5 从**测量快照**取，避免为存个字段就触发一次完整测量）。
    reason: 覆盖「为什么没激活」的原因文案（如「无测量缓存」），默认走红线文案。

    ★ significant: **这个 γ 有没有过显著门**。给了 False ⇒ 强制按 γ=0 处理。
      ⚠️ 必填场景：从**快照**取 γ 时必须一并给出——快照里的 γ 是**原始测量值**，
      它可能非零但 CI 含 0（如双色球 γ=-0.0185 且显著=False）。只看 γ≠0 就判激活
      会**直接绕过 γ=0 红线**：出号权重被动了，而测量其实并不支持。
      （2026-10-04 端到端实测撞到：快照写 显著=False，状态却报 倾斜=True。）
      不给（None）= 调用方保证 γ 已过红线（如直接取自 `mechanism_gamma`）。
    """
    raw = _resolve_gamma(lottery) if gamma is None else (
        0.0 if gamma is None else float(gamma))
    g = 0.0 if significant is False else raw     # ★ 未过显著门 ⇒ 与 γ=0 同处理
    active = bool(g) and np.isfinite(g) and g != 0.0
    if significant is False and not reason:
        # 措辞要分家：「不可折算」与「不显著」是两种不同的降级，别混着说
        reason = (("γ 未过显著门（CI 含 0）→ 按 γ=0 处理，不做倾斜"
                   if raw else "γ 不可折算（模型未 Go / 无 z 标尺）→ 按 γ=0 处理")
                  + "（红线实现点 ev.gap.mechanism_gamma）")
    if not active:
        return {
            "彩种": lottery, "γ": 0.0, "倾斜": False, "可用": False,
            "原因": reason or ("γ=0：模型未 Go / γ 不可折算 / γ 的 CI 含 0 "
                               "（红线实现点 ev.gap.mechanism_gamma）"),
            "说明": ("机制倾斜未激活，出号与不倾斜时逐位一致。"
                     "这不是「开奖公平」的证明，只是「当前测量未支持非零倾斜」。"),
        }
    return {
        "彩种": lottery, "γ": round(float(g), 6), "倾斜": True, "可用": True,
        "方向": "偏向冷门" if g > 0 else "偏向热门",
        "原因": f"γ={g:.4f} 且 CI 不含 0（四轨测量判定显著）",
        "说明": (f"机制倾斜已激活：乘数 exp(-γ·z) 与开奖倾向同向（{'偏冷' if g>0 else '偏热'}）。"
                 "它改变的是中奖概率的权重，不是奖金口径。"),
    }


def crowd_gate(gamma: Optional[float]) -> float:
    """
    数字型（七星彩）sampler 的反大众分位阈值。

    γ=0 → 返回 `CROWD_GATE_DEFAULT`（0.5）⇒ sampler 行为**逐位不变**。
    γ>0（开奖偏冷门）→ 阈值下移（更严格，只留更冷的组合）。
    γ<0 → 不许上移（阈值 >0.5 意味着"偏向热门"，那与「选冷门降分薄」的
    既有设计相反，且没有任何测量支持 ⇒ 保守保持默认）。
    """
    if not gamma or not np.isfinite(gamma) or gamma <= 0:
        return CROWD_GATE_DEFAULT
    shift = min(_GATE_MAX_SHIFT, float(gamma) * _GATE_SHIFT_PER_GAMMA)
    return max(0.10, CROWD_GATE_DEFAULT - shift)
