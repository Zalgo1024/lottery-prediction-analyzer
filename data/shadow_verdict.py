# -*- coding: utf-8 -*-
"""影子三臂的**可判性判决**——把「读数」和「结论」分开。

要回答的问题：三臂攒了几期之后，**这点差异够不够下结论**？

为什么必须单独一层
------------------
`data/shadow.summary()` 给的是**读数**（每臂的成本、奖金、净收益率）。读数本身
不会告诉你「差异是不是运气」。8~10 期时三臂的净收益率几乎一定长得不一样，而
人眼会本能地挑最小的那个当"模型变强了"——这正是本项目最想拦下的一类自欺。

所以本模块只做三件事：
1. **配对**：同一期、同注数下三臂的差（Δ/期）。跨期不可比，故必须逐期配对。
2. **可判性**：用配对差的标准差反推 `n*`（80% 功效所需的期数）。`n < n*` 时
   只能报描述，不能报结论。
3. **多条守门**：集中度（Δ 是不是被单独一期撑起）、多重比较（3 对 × 2 口径）、
   样本下限。

★ 三条硬纪律（每条对应一种已知的读错方式）

1. **先验期望差是 0，不是"应该为正"**。固定奖层面 `E[中奖注数]=K·p`，与选哪些票
   恒等无关 ⇒ 三臂的期望回款相同。所以「检出差异」的第一反应必须是**查实现缺陷
   或分摊效应**，不是「策略赚钱了」。本模块把这句话写进每一行判决说明里。
2. **Δ 由单期撑起时不得报可判**。金额分布极端右偏，只要有一期中了个固定奖大头，
   Δ 的均值与 `z` 都会被这一期决定（历史主格里出现过「单期贡献 Δ 的 92%」）。
   ⇒ 剔除最大配对期后若符号翻转（`|单期占Δ比| ≥ 1`），判决直接降级。
3. **3 对 × 2 口径 = 6 个检验，裸 P95 不算判据**。名义 5% 时
   `P(至少亮一个)=1−0.95^6=26.5%` ⇒ 一律过 BH 校正（复用 `ev.arena.stats`）。

★ 两个「≥10 元」不是同一个量：本模块的达标口径是**该期整批回款 ≥10 元**，
  分母是已结算期数，且只在 K 相同的情况下可比。逐票口径
  （`tier_metrics.gate_series`）是另一个量，随 K 机械上升，不能替换。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from data import shadow as SH
from data.issue_key import issue_key

#: 达标金额门槛（元）。与账本的 `中奖注数_ge10` 同门槛，但口径不同（见模块说明）。
GE10 = 10.0

#: 最少期数。少于此值连配对差的标准差都估不出来（ddof=1），判决只能是"不可判"。
MIN_ISSUES = 3

#: 六组配对：三臂两两比。随机臂 vs 固定规则臂要单独留出来 ——
#: 它俩都是无信息臂，唯一差别是「打不打乱池顺序」。
PAIRS: Tuple[Tuple[str, str], ...] = (
    (SH.ARM_MODEL, SH.ARM_RANDOM),
    (SH.ARM_MODEL, SH.ARM_FIXED),
    (SH.ARM_RANDOM, SH.ARM_FIXED),
)

METRICS: Tuple[Tuple[str, str], ...] = (
    ("money", "每期净盈亏（元）"),
    ("binary", "该期整批回款 ≥10 元"),
)

SCOPE_NOTE = (
    "口径：金额侧 = 每期净盈亏（税前奖金 − 成本，元），配对差逐期相减；"
    "达标侧 = 「该期整批回款 ≥10 元」，分母是已结算期数，"
    "且只在三臂注数相同时才可比（它不是逐票「该期至少一注达到 ≥10 元」）。"
)

PRIOR_ZERO_NOTE = (
    "★ 固定奖层面三臂的**期望回款相同**（E[中奖注数]=K·p，与选哪些票恒等无关）。"
    "因此「检出差异」的第一反应是查实现缺陷或浮动奖分摊效应，不是「选号变强了」。"
)

CIRCULAR_NOTE = (
    "★ n* 是用**实测** Δ 与配对差 SD 反推的，故 Δ 被运气抬高时 n* 偏小（偏乐观）。"
    "即便判决为「可判」，结论也只对这批期成立，仍需下一批独立期次复核。"
)


def _p_of_z(z: float) -> float:
    """双侧 p（正态近似）。`z=±inf` ⇒ p=0；`z=nan` ⇒ p=1（不构成任何证据）。"""
    if not math.isfinite(z):
        return 0.0 if math.isinf(z) else 1.0
    return math.erfc(abs(z) / math.sqrt(2.0))


def _sorted_issues(lottery: str, issues) -> List[str]:
    def key(s: str):
        try:
            return (0, issue_key(lottery, s))
        except Exception:                                     # noqa: BLE001
            return (1, (0, 0))
    return sorted(issues, key=key)


def _verdict_of(row: Dict[str, Any], *, n: int) -> Tuple[str, str]:
    """把一行配对统计折成 (判决, 说明)。判决取值：可判 / 边缘 / 不可判 / 未检出差异。

    ★ 调用前已保证 `n >= MIN_ISSUES`（否则连配对差方差都估不出来，见 `compare`）。
    """
    diff = row.get("实测Δ")
    if diff is not None and abs(float(diff)) <= 1e-12:
        # Δ 恰好为 0：n* 是无穷大（检出「零效应」需要无穷期），这里直接报未检出。
        return "未检出差异", f"Δ=0（z={row.get('z_实测'):.2f}），这一批里三臂逐期的钱一样多。"

    share = row.get("单期占Δ比")
    if share is not None and math.isfinite(share) and abs(share) >= 1.0:
        return "不可判", (
            f"Δ 由单独一期撑起（该期贡献 {share:.0%}，剔除后 Δ="
            f"{row.get('Δ_剔除最大配对期'):.2f} 元、z={row.get('z_剔除'):.2f}）"
            "⇒ 换个期次就可能反号，不能当结论。")

    ns = row.get("n*_保守")
    if ns is None or not math.isfinite(ns):
        return "不可判", "n* 无法估计（效应为 0 或方差不可用）。"
    ratio = ns / n if n else float("inf")
    if ratio <= 1.0:
        return "可判", (f"n*≈{ns:.1f} 期 ≤ 现有 {n} 期，功效足够；"
                        f"但见 n* 循环性说明，仍需下一批独立期次复核。")
    if ratio <= 3.0:
        return "边缘", f"需要约 {ns:.0f} 期、现有 {n} 期（{ratio:.1f}×）⇒ 只能报描述。"
    return "不可判", f"需要约 {ns:.0f} 期、现有 {n} 期（{ratio:.1f}×）⇒ 样本远不足。"


def compare(lottery: str, *, base: Optional[Any] = None,
            min_issues: int = MIN_ISSUES) -> Dict[str, Any]:
    """三臂配对对照 + 可判性判决。

    `base`：影子台账根目录（测试用临时目录）；None = 生产路径。
    """
    from ev.arena.stats import Z_ALPHA, adjust_multiplicity, pair

    rows = SH.load(lottery, base=base)
    by_arm: Dict[str, Dict[str, Dict[str, Any]]] = {
        a: {str(r.get("期号")): r for r in rows if r.get("臂") == a} for a in SH.ARMS
    }

    out: Dict[str, Any] = {
        "彩种": lottery,
        "可用": False,
        "原因": None,
        "期数": {"记录": len({str(r.get("期号")) for r in rows})},
        "注数": {a: sorted({int(r.get("注数") or 0) for r in d.values()})
                 for a, d in by_arm.items()},
        "对照": [],
        "结论": [],
        "口径": SCOPE_NOTE,
        "先验": PRIOR_ZERO_NOTE,
        "n*说明": CIRCULAR_NOTE,
        "诚实边界": SH.HONEST_NOTE,
    }

    active = {a: d for a, d in by_arm.items() if d}
    if len(active) < 2:
        out["原因"] = "可用臂不足 2 个（影子台账还没记够）"
        return out

    # ① 窗口一致性：不截断、不取交集 —— 悄悄取交集会让「三臂跑了不同的期」看不出来
    sets = [set(d) for d in active.values()]
    if any(s != sets[0] for s in sets):
        out["原因"] = ("三臂记录的期号集合不一致 ⇒ 差异里混了「跑了不同的期」，"
                       "先让调度器补齐再判")
        out["缺失期"] = {a: sorted(set(d) - set.intersection(*sets)) for a, d in active.items()}
        return out

    # ② 只判已结算期：未结算期的奖金还没产生，此时成本已记 ⇒ 三臂同时显示 −100%
    settled = {a: {k: r for k, r in d.items() if r.get("结算")} for a, d in active.items()}
    # ★ 这里**必须**取三臂都已结算的期：`settle()` 逐行写，某一臂写失败时会出现
    #   「同一条记录，A 臂结算了、B 臂没有」。若直接按全局并集取期号，取数时会
    #   KeyError；若静默用交集而不说，就变成「悄悄换了窗口」。⇒ 取交集但**点名剔除**。
    ssets = [set(d) for d in settled.values()]
    common_settled = set.intersection(*ssets)
    dropped = sorted(set.union(*ssets) - common_settled)
    n_settled = len(common_settled)
    out["期数"].update({"已结算": n_settled,
                        "待结算": len(sets[0]) - n_settled})
    if dropped:
        out["剔除期"] = {
            "数量": len(dropped),
            "期号": _sorted_issues(lottery, dropped)[:20],
            "原因": "该期只有部分臂完成结算（写入失败/留到下一轮），三臂不成对，"
                    "不参与配对——它与『三臂都还没结算』是两件事，不能混成一个 −100%",
        }
    if n_settled == 0:
        out["原因"] = "一期都还没结算：净收益率 −100% 是「奖金尚未产生」，不是读数"
        return out
    if n_settled < min_issues:
        # ★ 不在这里硬算：n<2 时 ddof=1 的样本方差无定义，pair() 会一路 nan，
        #   再被下游读成「有差异但算不显著」——比直接说「样本不足」误导得多。
        out["原因"] = (f"已结算 {n_settled} 期 < {min_issues} 期：配对差的方差无法估计，"
                       "任何净收益率差异都只能当描述，不构成结论")
        return out

    issues = _sorted_issues(lottery, common_settled)

    def _money(arm: str) -> List[float]:
        return [float(settled[arm][i].get("净盈亏") or 0.0) for i in issues]

    def _ge10(arm: str) -> List[float]:
        return [1.0 if float(settled[arm][i].get("税前奖金") or 0.0) >= GE10 else 0.0
                for i in issues]

    raw: List[Dict[str, Any]] = []
    for a, b in PAIRS:
        if a not in settled or b not in settled:
            continue
        for metric, label in METRICS:
            fa, fb = (_money, _money) if metric == "money" else (_ge10, _ge10)
            row = pair(fa(a), fb(b), issues_a=issues, issues_b=issues,
                       name_a=a, name_b=b, metric=metric,
                       effect=None, effect_source="实测（本批期）")
            row["对照"] = f"{a} vs {b}"
            row["口径名"] = label
            row["p_原始"] = _p_of_z(row.get("z_实测"))
            raw.append(row)

    if not raw:
        out["原因"] = "没有可配对的臂组合"
        return out

    # ③ 6 个检验一律过 BH：裸 P95 在 6 个检验下假阳率 26.5%
    adj = adjust_multiplicity([r["p_原始"] for r in raw], method="bh")
    for r, p_adj in zip(raw, adj["校正后p"]):
        r["p_BH"] = p_adj
        r["显著"] = bool(p_adj <= adj["名义α"] and abs(r.get("z_实测") or 0) > Z_ALPHA)

    for r in raw:
        v, why = _verdict_of(r, n=n_settled)
        if v in ("可判", "边缘") and not r["显著"]:
            v, why = ("未检出差异",
                      f"{why} 但 Δ 未过显著性门（z={r.get('z_实测'):.2f}、"
                      f"p_BH={r['p_BH']:.3f}）⇒ 只能说「这一批没检出」。")
        r["判决"] = v
        r["判决说明"] = why
        if v == "可判":
            r["判决说明"] += " " + PRIOR_ZERO_NOTE
        out["对照"].append(r)
        out["结论"].append({"对照": r["对照"], "口径": r["口径名"],
                            "判决": v, "说明": why})

    # ④ 整彩种的可判性：任一对「可判」才算有结论，否则整块挂「不可判」
    verdicts = {r["判决"] for r in out["对照"]}
    out["可用"] = True
    out["总结"] = ("至少一组对照可判" if "可判" in verdicts else
                   ("只有边缘证据，不能下结论" if "边缘" in verdicts else
                    ("各组均未检出差异（不等于三臂相同，只表示这一批看不出）"
                     if "未检出差异" in verdicts else
                     "不可判：样本不足或 Δ 由单期撑起")))
    out["多重比较"] = {"方法": adj["方法"], "检验数": adj["检验数"],
                       "名义α": adj["名义α"], "通过数": adj["通过数"]}
    out["复算"] = f"python scripts/shadow_report.py --lottery {lottery}"
    return out


def compare_all(*, base: Optional[Any] = None) -> Dict[str, Any]:
    """六彩种一轮判决。单彩种失败不影响其余。"""
    from config import LOTTERY_CONFIG

    out: Dict[str, Any] = {}
    for lot in LOTTERY_CONFIG:
        try:
            out[lot] = compare(lot, base=base)
        except Exception as e:                                # noqa: BLE001
            out[lot] = {"彩种": lot, "可用": False, "原因": f"判决计算失败：{e}"}
    return out
