# -*- coding: utf-8 -*-
"""ev/arena/report.py —— 把对照结果渲染成报告。

两条硬要求
----------
1. **首部回显预注册**：版本 / 锁定日 / 哈希 / 常量 / 主格。读者必须能一眼看到
   「这份数是按哪份契约跑出来的」，否则无法判断有没有事后挑。
2. **不可判 ≠ 无效**：任何判为「不可判」的格，表格上方必须出现这句话。
   这是 Stage 0 就定下的措辞红线。

★ 净收益侧（2026-10-24 起有数）：**期望差恒为 0**，不是「测不出」而是数学习恒等 ——
  金额网格按 (h, j) 查，而 (h, j) 的边际分布与票集无关。所以净收益表只作
  ①恒等式一致性检验 ②分布形状描述（回撤 / 集中度 / 每 100 元），
  不得读成「更赚钱」。单次大奖占比 > 0.3 时强制降级为「不可判」。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Sequence

UNDECIDABLE_NOTE = (
    "「不可判」= **功效不足以识别差异**，不等于「策略无效」；"
    "「可判」也不等于「策略有效」，只说明该格上差异大到能被这套样本识别。"
)


def header(doc: Dict[str, Any], arms: Sequence[str]) -> List[str]:
    L: List[str] = []
    L.append("## 预注册回显")
    L.append("")
    L.append("| 项 | 值 |")
    L.append("|---|---|")
    L.append(f"| 版本 | {doc['版本']} |")
    L.append(f"| 锁定日 | {doc['锁定日']} |")
    L.append(f"| 哈希 | `{doc['哈希'][:16]}…` |")
    L.append(f"| 基线臂 | {doc['基线臂']} |")
    L.append(f"| 臂 | {', '.join(arms)} |")
    c = doc["常量"]
    L.append(f"| 候选池 / 重叠上限 | {c['候选池规模']} / {c['重叠上限']} |")
    L.append(f"| MC 拟合 / 评估采样 | {c['MC拟合采样数']} / {c['MC评估采样数']}"
             f"（评估 seed 偏移 {c['评估seed偏移']}） |")
    L.append("")
    L.append(f"> {UNDECIDABLE_NOTE}")
    L.append("")
    return L


def cell_block(run: Dict[str, Any]) -> List[str]:
    L: List[str] = []
    tag = "**主假设**" if run.get("主假设") else "描述性"
    L.append(f"### {run['格']}　{tag}")
    L.append("")
    L.append(f"- 彩种 {run['彩种']}，K={run['K']}，预算 {run['预算元每期']} 元/期，"
             f"目标 {run['目标']}")
    L.append(f"- 期窗 {run['期号起']} ~ {run['期号止']}，共 {run['期数']} 期，"
             f"规则版本 `{run['规则版本']}`，判级口径 **{run['规则口径']}**")
    L.append(f"- 单注达标概率 p={run['单注达标概率']:.6e}，"
             f"K·p={run['理论上限Kp']:.4f}，随机基线 1−(1−p)^K={run['随机基线']:.4f}")
    L.append("")
    L.append("| 臂 | 出票 | 达标率 | 哨兵（单期达标注数） | 精确P | 实测Δ | SE | z | p_BH | n*保守/可用 | 判决 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for name, row in run["臂"].items():
        if row.get("状态") != "完成":
            L.append(f"| {name} | — | {row.get('状态')} | — | — | — | — | — | — | — | "
                     f"{str(row.get('原因'))[:48]} |")
            continue
        pr = row.get("配对") or {}

        def g(k, fmt="{:.4f}"):
            v = pr.get(k)
            return fmt.format(v) if isinstance(v, (int, float)) else "—"

        ns = pr.get("n*_保守")
        nsr = (f"{ns:.0f}/{run['期数']}" if isinstance(ns, (int, float)) and ns != float("inf")
               else "∞")
        ex = row.get("精确P")
        L.append(f"| {name} | {row['实际注数']}/{run['K']} | {row['达标率']:.4f} | "
                 f"{row['哨兵_单期达标注数']:.3f} | "
                 f"{(f'{ex:.4f}' if isinstance(ex, (int, float)) else '—')} | "
                 f"{g('实测Δ')} | {g('配对SE')} | {g('z_实测', '{:.2f}')} | "
                 f"{g('p_BH')} | {nsr} | {pr.get('判决', '（基线）')} |")
    L.append("")
    if run.get("多重比较"):
        mc = run["多重比较"]
        L.append(f"- 多重比较：{mc['检验数']} 个检验，BH 校正后通过 {mc['通过数']} 个 "
                 f"（名义 α={mc['名义α']}）")
    bad = [n for n, r in run["臂"].items()
           if (r.get("配对") or {}).get("★哨兵判据") == "实现缺陷嫌疑"]
    if bad:
        L.append(f"- ⚠ 哨兵判据触发（{', '.join(bad)}）："
                 f"E[达标注数]=K·p 与选哪些票恒等无关 ⇒ 两臂差应在抽样误差内；"
                 f"|z|>4 ⇒ 先查实现，不报结论")
    L.append("")
    L.extend(money_block(run))
    return L


def money_block(run: Dict[str, Any]) -> List[str]:
    """净收益侧表格（描述性 + 恒等式一致性）。

    ★ 判读顺序：先看右栏判决，再看金额。金额差**不是效应量** ——
      E[单期实得] 与选哪些票恒等相同，这里的差只可能是抽样波动或实现缺陷。
    """
    mp = run.get("净收益配对") or []
    if not mp:
        return []
    L: List[str] = []
    L.append("#### 净收益侧（税前，每注 2 元）")
    L.append("")
    L.append(f"> ★ **E[单期实得] 与选哪些票恒等无关**（金额按 (h,j) 查表，而 (h,j) 的边际"
             f"分布由组合结构决定）⇒ 本表**期望差恒为 0**。右栏看恒等式判据，"
             f"金额列只作分布形状描述，不得读成「更赚钱」。")
    L.append("")
    L.append(f"> ★ 实现返奖率 ≠ 理论返奖率：金额侧被少数浮动奖支配，样本里中不中"
             f"一/二等会让「元/100元」在 −42% 与 −76% 之间跳。**判断结算是否正确不看"
             f"这一列**，看 ① `scripts/oneoff/_arena_money_check.py` 的逐期逐分对账；"
             f"② `scripts/oneoff/_payout_analytic_probe.py` 的解析期望（应为"
             f"官方返奖率量级）。")
    L.append("")
    L.append("| 臂 | 净收益(元) | 元/100元 | 剔除最大一期 | 大奖占比 | 最大回撤(元/期数) "
             "| 中奖注数 | 中奖期数 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in mp:
        def g(k, fmt="{:.2f}"):
            v = r.get(k)
            return fmt.format(v) if isinstance(v, (int, float)) else "—"
        L.append(f"| {r['臂A']} | {g('金额_净收益', '{:,.0f}')} | "
                 f"{g('金额_净收益每100元')} | {g('金额_净收益每100元_剔除最大一期')} | "
                 f"{g('金额_单次大奖占总实得比', '{:.1%}')} | "
                 f"{g('金额_最大回撤', '{:,.0f}')} / {r.get('金额_最大回撤持续期数', '—')} | "
                 f"{r.get('金额_中奖注数', '—')} | {r.get('金额_中奖期数', '—')} |")
    L.append("")
    L.append("**配对判决**（★ 这一张才是判据；上一张只是分布形状）")
    L.append("")
    L.append("| 臂 vs 基线 | 净额差(元/期) | 配对SE | z | 最大配对期 | 剔除该期后 Δ | z_剔除 "
             "| 单期占Δ比 | 判决 | 集中度 / 恒等式 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|")
    for r in mp:
        def h(k, fmt="{:.2f}"):
            v = r.get(k)
            return fmt.format(v) if isinstance(v, (int, float)) else "—"
        share = r.get("单期占Δ比")
        share_s = f"{share:.0%}" if isinstance(share, (int, float)) else "—"
        bright = "**" if (isinstance(share, (int, float)) and abs(share) > 1.0) else ""
        L.append(f"| {r['臂A']} vs {r.get('臂B', '—')} | {h('实测Δ')} | {h('配对SE')} | "
                 f"{h('z_实测')} | {r.get('最大配对期', '—')} | "
                 f"{bright}{h('Δ_剔除最大配对期')}{bright} | {h('z_剔除')} | "
                 f"{bright}{share_s}{bright} | {r.get('判决', '—')} | "
                 f"{r.get('集中度守门', '—')}；{r.get('★恒等式判据', '—')} |")
    L.append("")
    L.append("> **单期占Δ比 > 100%** 表示：删掉「最大配对期」那一期，Δ 会**变号** ⇒ 该差由单期决定，不可解释为策略差异。")
    L.append("")
    return L


def to_markdown(runs: Sequence[Dict[str, Any]], doc: Dict[str, Any],
                arms: Sequence[str], *, extra: Optional[Sequence[str]] = None) -> str:
    out: List[str] = list(header(doc, arms))
    out.append("## 逐格结果")
    out.append("")
    for r in runs:
        out.extend(cell_block(r))
    if extra:
        out.extend(list(extra))
    return "\n".join(out)


def verdict_cell(run: Dict[str, Any], cell: Optional[Dict[str, Any]] = None) -> str:
    """主格的判决。

    ★ 必须按**预注册指名的主假设臂**取，不能取「所有臂里最保守的那个」：
      后者等于让最差的臂决定主格，而且事后换一个臂就能换一个结论 ——
      这正是预注册要防的事。只有在没指定主假设臂时才退回保守取值。
    """
    if not run.get("主假设"):
        return "（描述性格，不出判决）"
    named = (cell or {}).get("主假设臂")
    if named:
        row = (run.get("臂") or {}).get(named) or {}
        v = (row.get("配对") or {}).get("判决")
        if v in ("可判", "边缘", "不可判"):
            return v
        return f"无可用配对（主假设臂 {named}: {row.get('状态') or '缺失'}）"
    vals = []
    for name, row in run["臂"].items():
        pr = row.get("配对") or {}
        v = pr.get("判决")
        if v in ("可判", "边缘", "不可判"):
            vals.append(v)
    if not vals:
        return "无可用配对"
    for v in ("不可判", "边缘", "可判"):
        if v in vals:
            return v
    return "未知"


# ---------------------------------------------------------------- 判决矩阵措辞
# 命中侧 × 净收益侧 的 3×3 判决矩阵。措辞**必须**满足三条红线：
#   · 绝不能写肯定式的本体论断言（例：不能说开奖结果是不可能预测的）；
#   · 「无法识别差异」不得被读成「策略无效」；
#   · 净收益侧的输出在单次大奖占比过高时**强制**降级为「不可判」。
AO_VERDICT_TEXT: Dict[Tuple[str, str], str] = {
    ("改善", "改善"): (
        "两侧同向改善。允许进入影子跑，但必须先过三道闸："
        "① 200 期**未曾参与任何调参**的未来期；② 最大回撤 ≤ 1.2×（净收益）/ 1.5×（命中）；"
        "③ 四路口径核对（实测/精确/解析上界/哨兵）逐项一致。"
        "★ 净收益侧的改善若来自单次大奖（最大单期实得占比 > 0.3），本条降级为「不可判」。"
    ),
    ("改善", "不可判"): (
        "命中侧可识别、净收益侧不可识别。这是**最常见也最容易被误读**的格子："
        "只能说「该设计让达标注数分布更靠上」，不能说「更赚钱」。"
        "对外表述必须带限定：「更容易中到某个奖级」，不得出现「提高收益」。"
    ),
    ("改善", "恶化"): (
        "命中侧改善、净收益侧恶化。机制上可解释：提高达标频率往往靠压低单注奖金档位，"
        "而返奖率由规则固定 ⇒ 频率↑ 不等于金额↑。**不得**据此推荐任何一侧。"
    ),
    ("不可判", "改善"): (
        "净收益侧可识别而命中侧不可判。净收益高度受少数大奖支配，"
        "该格必须先把「单次大奖占总实得比」报出来；占比 > 0.3 ⇒ 强制降级为两侧都不可判。"
    ),
    ("不可判", "不可判"): (
        "两侧都不可判。**这是本项目多数格的常态**，含义是「这套样本量下这套终点识别不出差异」，"
        "不是「策略无效」。报告首行必须写这句话，且不得用「未检出」以外的措辞。"
    ),
    ("不可判", "恶化"): "净收益侧恶化且命中侧识别不出。仅作描述性记录，不构成任何推荐。",
    ("恶化", "改善"): "命中侧恶化、净收益侧改善。与「改善/恶化」同源，不得据此推荐任何一侧。",
    ("恶化", "不可判"): "命中侧恶化（可识别）、净收益侧不可识别。可报告「命中侧变差」，不得推断收益。",
    ("恶化", "恶化"): "两侧同向恶化。这是唯一可以给出**否定性推荐**的格子（不建议采用该设计）。",
}


def verdict_matrix(hit_verdict: str, net_verdict: str,
                   *, max_single_share: Optional[float] = None) -> Dict[str, Any]:
    """查 3×3 判决矩阵。`max_single_share` > 0.3 时**强制**把净收益侧降级。"""
    forced = False
    if max_single_share is not None and max_single_share > 0.3 and net_verdict == "改善":
        net_verdict, forced = "不可判", True
    key = (hit_verdict, net_verdict)
    if key not in AO_VERDICT_TEXT:
        raise ValueError(f"未知判决组合 {key}（两侧只能是 改善/不可判/恶化）")
    return {"命中侧": hit_verdict, "净收益侧": net_verdict,
            "强制降级": forced, "措辞": AO_VERDICT_TEXT[key]}
