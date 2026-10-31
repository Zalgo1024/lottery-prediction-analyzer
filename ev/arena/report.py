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
from typing import Any, Dict, List, Optional, Sequence, Tuple

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


# ------------------------------------------------------------------ K 网格
def kgrid_block(cells: Sequence[Dict[str, Any]], *,
                label: str = "≥10元",
                bh: Optional[Dict[str, Any]] = None) -> List[str]:
    """五档 K 网格的 markdown 表（任务 1「单位预算验证」）。

    三张表刻意分开，因为它们的**性质不同**，合在一起必然被读歪：
      表 1 规模效应 —— 随机臂 vs 理论 `1−(1−p)^K`，这是**口径体检**，不是结论；
      表 2 覆盖效应 —— 同 K 下臂 − 基线，这才是「覆盖有没有贡献」；
      表 3 附加门 —— 同一副票集在别的门上的表现，**描述性**，不进 BH。

    ★ 任何一格 `Δ` 都必须与 `K` 同栏出现：`可争取空间` 非单调（峰在 `K·p≈1`），
      只看 Δ 会得出「K 越大越值」的错误结论。
    """
    L: List[str] = ["## K 网格：单期达标率（%s）" % label, ""]
    L.append(f"> 评估窗 = 后 {int((1 - 0.6) * 100)}%（唯一判决窗口）；"
             "Δ = 臂 − `random` R 副本逐期均值；空间 = `min(1,K·p) − [1−(1−p)^K]`；"
             "η = Δ ÷ 空间。**空间非单调，Δ 必须与 K 同读。**")
    L.append("")

    # ---- 表 1：规模效应（口径体检）
    L.append("### 表 1 · 规模效应（随机臂 vs 理论）")
    L.append("")
    L.append("| 彩种 | K | 预算元/期 | 随机达标率 | 理论 `1−(1−p)^K` | 偏离 / SE | 判 |")
    L.append("|---|---|---|---|---|---|---|")
    for c in cells:
        if "降级" in c:
            continue
        a = ((c.get("臂") or {}).get("random") or {}).get(label) or {}
        row = a.get("全窗") or {}
        if not row:
            continue
        rate = row.get("基线达标率")
        p = c.get("单注达标概率") or 0.0
        theory = 1.0 - (1.0 - p) ** int(c["K"])
        dev = (rate - theory) if isinstance(rate, float) else float("nan")
        se = row.get("抽样SE") or float("nan")
        zs = dev / se if se and se > 0 else float("nan")
        if not isinstance(zs, float) or zs != zs:
            flag = "—"
        elif abs(zs) <= 2:
            flag = "相容"
        elif zs > 0:
            # ★ 正偏离是**预期**的：cap 强制同批票低重叠 ⇒ 命中事件负相关 ⇒
            #   并集概率高于 iid。K 越大越明显，这不是缺陷。
            flag = "随机优于 iid（cap 负相关，预期）"
        else:
            flag = "**随机差于 iid：查池/cap**"
        L.append(f"| {c['彩种']} | {c['K']} | {c.get('预算元每期', '—')} | "
                 f"{rate:.4f} | {theory:.4f} | {zs:+.2f} | {flag} |")
    L.append("")
    L.append("> ★ **正偏离不是缺陷，负偏离才是**。随机臂抽出的 K 注同样受 `重叠上限`"
             "约束（预注册 = 3 个号），这迫使票间低重叠 ⇒ 命中事件**负相关** ⇒ "
             "并集概率系统性高于独立同分布的 `1−(1−p)^K`，且 K 越大差得越多"
             "（双色球 K=500 实测 +0.83pp）。反过来说：如果实测**低于**理论，"
             "那才说明池或 cap 口径错了。")
    L.append("> ★★ 由此一条推论必须一起看：`可争取空间 = min(1,K·p) − [1−(1−p)^K]` "
             "用的是 **iid 基线**，而实测随机臂已经高过它 ⇒ **高 K 档的空间被高估**"
             "（双色球 K=400：iid 空间 3.71pp，相对实测基线只剩 2.19pp）。"
             "所以表 2 同时给 `η`（对 iid 空间）与 `η′`（对实测剩余空间 `1−基线`）："
             "**η 是乐观口径，η′ 才是「还能从随机手里抢多少」**。")
    L.append("")

    # ---- 表 2：覆盖效应
    L.append(f"### 表 2 · 覆盖效应 Δ（同 K 比臂，评估窗）")
    L.append("")
    L.append("| 彩种 | K | 臂 | 达标率 | 基线 | Δ | 相对提升 | 空间(iid) | η | "
             "剩余空间(实测) | η′ | n* / 可用期 | 判决 | cap | BH |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    bh_map = {}
    if bh:
        for r in bh.get("明细") or []:
            bh_map[(r["格"], r["臂"])] = r
    for c in cells:
        if "降级" in c:
            continue
        for name, a in (c.get("臂") or {}).items():
            g = (a.get(label) or {})
            row = g.get("评估窗") or {}
            pr = g.get("配对") or {}
            if not row:
                continue
            d = row.get("覆盖效应Δ")
            rel = row.get("相对提升")
            eta = row.get("归一化η")
            base = row.get("基线达标率")
            # η′：对**实测**剩余空间归一。iid 空间在高 K 档已被 cap 负相关吃掉一部分，
            # 只有 1−基线 才是「真的还能抢到的」。
            rest = (1.0 - float(base)) if isinstance(base, (int, float)) else None
            eta2 = (float(d) / rest) if (isinstance(d, float)
                                         and isinstance(rest, float) and rest > 1e-12
                                         ) else None
            ns = pr.get("n*_可用期数比")
            b = bh_map.get((c.get("格"), name)) or {}
            star = "★" if a.get("★cap污染") else ""
            L.append(
                f"| {c['彩种']} | {c['K']} | {name}{star} | "
                f"{row['达标率']:.4f} | {row['基线达标率']:.4f} | {d:+.4f} | "
                f"{(f'{rel:+.1%}' if isinstance(rel, float) else '—')} | "
                f"{row['可争取空间']:.4f} | "
                f"{(f'{eta:+.2f}' if isinstance(eta, float) else '—')} | "
                f"{(f'{rest:.4f}' if isinstance(rest, float) else '—')} | "
                f"{(f'{eta2:+.2f}' if isinstance(eta2, float) else '—')} | "
                f"{(f'{ns:.2f}' if isinstance(ns, float) else '—')} | "
                f"{pr.get('判决', '—')} | "
                f"{'污染' if a.get('★cap污染') else '—'} | "
                f"{('是' if b.get('BH通过') else '否') if b else '（不进）'} |")
    L.append("")
    L.append("> `★` = 该臂 cap 口径与实验臂不一致（生产选号的内部重叠上限不同）⇒ "
             "只作外部保真度对照，**不进任何配对检验**。")
    L.append("")

    # ---- 表 3：附加门（描述性）
    others = [lb for lb in ("≥四等", "≥三等")
              if any(lb in ((a or {})) for c in cells if "降级" not in c
                     for a in (c.get("臂") or {}).values())]
    if others:
        L.append("### 表 3 · 附加门（描述性，不进 BH）")
        L.append("")
        L.append("| 彩种 | K | 臂 | 门 | 达标率 | Δ | 空间 | η |")
        L.append("|---|---|---|---|---|---|---|---|")
        for c in cells:
            if "降级" in c:
                continue
            for name, a in (c.get("臂") or {}).items():
                for lb in others:
                    row = ((a.get(lb) or {}).get("评估窗")) or {}
                    if not row:
                        continue
                    eta = row.get("归一化η")
                    L.append(
                        f"| {c['彩种']} | {c['K']} | {name} | {lb} | "
                        f"{row['达标率']:.4f} | {row['覆盖效应Δ']:+.4f} | "
                        f"{row['可争取空间']:.4f} | "
                        f"{(f'{eta:+.2f}' if isinstance(eta, float) else '—')} |")
        L.append("")
        L.append("> 票集是按主终点选出的 ⇒ 附加门的 Δ 是「**同一副票集**在别的门上的表现」，"
                 "不是为那门优化的结果。为某门单独优化必须另跑一格（任务 3）。")
        L.append("")

    L.append("> 读法：Δ≈0 而达标率随 K 上升 ⇒ **纯规模效应**；Δ>0 但绝对值 <0.5pp ⇒ "
             "覆盖有效但低于实益门槛 ⇒ 保留为研究结论，不接生产选号。")
    L.append("")

    # ---- 表 4：钱（成本 / 实得 / 净收益 / 回撤）
    L.append("### 表 4 · 成本与实得（全窗，税前单注奖金之和）")
    L.append("")
    L.append("| 彩种 | K | 臂 | 成本元 | 总实得元 | 净收益元 | 净收益/100元 | "
             "中奖期数 | 每100元命中期数 | 每100元奖金元 | 最大回撤元 | 大奖占总实得 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|---|")
    for c in cells:
        if "降级" in c:
            continue
        for name, a in (c.get("臂") or {}).items():
            m = a.get("meta") or {}
            if "净收益" not in m:
                continue
            far = m.get("单次大奖占总实得比")
            per100 = m.get("净收益每100元")
            per100_s = f"{per100:+.2f}" if isinstance(per100, (int, float)) else "—"
            far_s = f"{far:.1%}" if isinstance(far, (int, float)) else "—"
            cost = m.get("成本") or 0.0
            gross = m.get("总实得") or 0.0
            wins = m.get("中奖期数")
            # 每 100 元投入的换算：命中「期」数 = 中奖期数 × 100/成本；
            # 奖金 = 总实得 × 100/成本。**两者都是单位投入的收益率，跨 K 可比。**
            hp100 = (float(wins) * 100.0 / cost) if (wins is not None and cost) else None
            gp100 = (float(gross) * 100.0 / cost) if cost else None
            L.append(
                f"| {c['彩种']} | {c['K']} | {name} | {cost:,.0f} | "
                f"{gross:,.0f} | {(m.get('净收益') or 0):,.0f} | {per100_s} | "
                f"{(f'{int(wins):,}' if isinstance(wins, (int, float)) else '—')} | "
                f"{(f'{hp100:.2f}' if isinstance(hp100, float) else '—')} | "
                f"{(f'{gp100:.2f}' if isinstance(gp100, float) else '—')} | "
                f"{(m.get('最大回撤') or 0):,.0f} | {far_s} |")
    L.append("")
    L.append("> ★ **这一张表读不出覆盖效应**：`E[单期实得]` 与选哪些票恒等无关"
             "（期望线性性），所以同 K 各臂的期望净收益本来就该相同，"
             "表里的差异全部是**分布形状与抽样噪声**。能读出覆盖效应的是表 2 的期次占比。")
    L.append("> ★ 金额是**税前单注奖金之和**，不是实得、更不是净收益口径的投资回报；"
             "缺当期派奖数据的期不参与求和（见 `结算口径明细`）。")
    L.append("> ★★ **两个「≥10元」不是同一个量，禁止互相替代**：表 2 的 `达标率` 是"
             "「该期**至少一注**实得 ≥10 元」的期占比（逐票判定，`tier_metrics`）；"
             "而 `money_series` 里的 `≥10元期占比` 分母是**该期全部票实得之和**，"
             "会随 K **机械上升**（双色球 K=50 实测就已 =1.0，同一批票的逐票口径只有 0.38）。"
             "跨 K 比较、或回答「票多了还是覆盖好了」，**只能用表 2**。")
    L.append("> ★ 「每100元命中期数 / 每100元奖金」= 中奖期数（总实得）× 100 ÷ 成本，"
             "是单位投入的收益率，**跨 K 可比**；它们仍然读不出覆盖效应，"
             "但能直接回答「这笔预算换到几次命中、多少钱」。")
    L.append("")
    return L


# ------------------------------------------------- 任务 3：为某门单独优化（矩阵）
# 「实益门槛」：Δ 低于这个数就只当研究结论，不接生产选号（与 kgrid_block 的读法一致）
TIER_OPT_MIN_GAIN = 0.005


def tier_opt_matrix_block(cells: Sequence[Dict[str, Any]], *,
                          labels: Sequence[str] = ("≥10元", "≥四等", "≥三等"),
                          lotteries: Sequence[str] = ("双色球", "大乐透"),
                          arms: Sequence[str] = ("tier_packing", "union_greedy")
                          ) -> List[str]:
    """「为某门单独优化 → 在同一副票集上看所有门」的矩阵表（任务 3）。

    为什么必须是矩阵而不是一列数
    ----------------------------
    同一副票集在三个门上的表现**不是**同一个排序。为 `≥10元` 优化的票集可能在
    `≥四等` 上毫无变化（四等的条件集更窄、且大乐透跨版本还变过）。只报
    「主终点 Δ」会把「误配的代价」整个藏起来，所以：

        行 = 优化目标（× 臂）    列 = 被测量的门

    对角线 = 「为该门优化的效果」；非对角线 = 「为别的门优化，在这门上顺带/受损」。

    ★ 判决只看**对角线**，且门槛是 `TIER_OPT_MIN_GAIN`；低于门槛一律写明
      「保留为研究结论，不接生产选号」，不给「趋势向好」这类模糊话。
    ★ 大乐透 `≥四等` 的条件集随规则版本变 ⇒ 额外给一张**分版本**表，
      合并平均是错的口径。
    """
    L: List[str] = ["## 任务 3 · 「四等奖起步」单独优化（矩阵）", ""]
    L.append(f"> 行 = 该格的**优化目标**，列 = **被测量**的门；`Δ` = 臂 − 同 K 的 "
             f"`random` 副本均值（评估窗）。**只读对角线**。实益门槛 Δ ≥ "
             f"{TIER_OPT_MIN_GAIN * 100:.1f}pp，低于门槛一律不接生产选号。")
    L.append("")

    for lot in lotteries:
        sub = [c for c in cells if c.get("彩种") == lot and "降级" not in c]
        if not sub:
            continue
        Ks = sorted({int(c["K"]) for c in sub})
        L.append(f"### {lot}（K = {'/'.join(str(k) for k in Ks)}）")
        L.append("")

        # ---- 表 A：达标率矩阵（行 = 目标 × 臂）
        L.append("| 优化目标 | 臂 | " + " | ".join(f"{lb} 达标率" for lb in labels)
                 + " | 主终点 Δ | 判决 |")
        L.append("|---|---|" + "---|" * len(labels) + "---|---|")
        # 对角线：门 → {臂: (Δ, 空间)}，供下面**逐门**判（不是逐彩种判）
        diag: Dict[str, Dict[str, Any]] = {lb: {} for lb in labels}
        for c in sub:
            tgt_label = str(c.get("优化目标门") or "")
            for name in arms:
                a = (c.get("臂") or {}).get(name) or {}
                picks = []
                for lb in labels:
                    row = ((a.get(lb) or {}).get("评估窗")) or {}
                    picks.append(f"{row['达标率']:.4f}" if row else "—")
                prim = ((a.get(tgt_label) or {}).get("评估窗")) or {}
                pr = ((a.get(tgt_label) or {}).get("配对")) or {}
                d = prim.get("覆盖效应Δ")
                if tgt_label in diag:
                    diag[tgt_label][name] = {
                        "Δ": d, "空间": prim.get("可争取空间"),
                        "η": prim.get("归一化η"), "判决": pr.get("判决"),
                        "达标率": prim.get("达标率"),
                        "基线": prim.get("基线达标率")}
                L.append(
                    f"| {tgt_label} | {name} | " + " | ".join(picks) +
                    f" | {(f'{d:+.4f}' if isinstance(d, float) else '—')} | "
                    f"{pr.get('判决', '—')} |")
        L.append("")

        # ---- 表 B：Δ 矩阵（对角线才是该门的真实收益）
        L.append("| 优化目标 | 臂 | " + " | ".join(f"Δ {lb}" for lb in labels)
                 + " | 空间 | η |")
        L.append("|---|---|" + "---|" * len(labels) + "---|---|")
        for c in sub:
            tgt_label = str(c.get("优化目标门") or "")
            for name in arms:
                a = (c.get("臂") or {}).get(name) or {}
                ds, space, eta = [], None, None
                for lb in labels:
                    row = ((a.get(lb) or {}).get("评估窗")) or {}
                    if row:
                        ds.append(f"{row['覆盖效应Δ']:+.4f}")
                        if lb == tgt_label:
                            space, eta = row.get("可争取空间"), row.get("归一化η")
                    else:
                        ds.append("—")
                L.append(
                    f"| {tgt_label} | {name} | " + " | ".join(ds) +
                    f" | {(f'{space:.4f}' if isinstance(space, float) else '—')} | "
                    f"{(f'{eta:+.2f}' if isinstance(eta, float) else '—')} |")
        L.append("")

        # ---- 表 C：分版本（只对确实跨版本的门有意义）
        # ★ 臂别只取 arms[0]：某臂在该门上一注都不中时，整张分版本表会全是 0，
        #   看着像「数据缺失」，其实只是那个臂没中。取**达标期数最多**的臂，
        #   并在表里显式写出用的是哪个臂。
        ver_rows: List[str] = []
        for c in sub:
            tgt_label = str(c.get("优化目标门") or "")
            best_name, best_info, best_hits = None, None, -1
            for name in arms:
                a = (c.get("臂") or {}).get(name) or {}
                pv = (a.get(tgt_label) or {}).get("分版本") or {}
                if len(pv) < 2:
                    continue
                hits = sum(int(v.get("达标期数") or 0) for v in pv.values())
                if hits > best_hits:
                    best_name, best_info, best_hits = name, pv, hits
            if best_info is None:
                continue
            for ver, info in best_info.items():
                if "期占比" not in info:
                    ver_rows.append(
                        f"| {tgt_label} | {best_name} | {ver} | "
                        f"{info.get('期数', '—')} | 不支持 | — | — | — |")
                    continue
                ver_rows.append(
                    f"| {tgt_label} | {best_name} | {ver} | {info['期数']} | "
                    f"{info.get('达标期数', '—')} | {info['期占比']:.4f} | "
                    f"{info['p']:.6g} | {info['K·p']:.4f} |")
        if ver_rows:
            L.append("**分版本（该门的条件集随规则版本变化，禁止合并成一个平均）**")
            L.append("")
            L.append("| 优化目标 | 臂 | 规则版本 | 期数 | 达标期数 | 期占比 | p | K·p |")
            L.append("|---|---|---|---|---|---|---|---|")
            L.extend(ver_rows)
            L.append("")
            # 同一门在不同版本下 p 差得离谱时，必须点出来 —— 那不是「同一件事的
            # 不同测量」，而是「规则改了，这个奖级的门槛变了」。
            ratios: List[Tuple[str, float, float]] = []
            for lb in labels:
                ps = [float(r.split("|")[7]) for r in ver_rows
                      if r.startswith(f"| {lb} |") and "不支持" not in r]
                if len(ps) >= 2 and min(ps) > 0:
                    ratios.append((lb, max(ps), min(ps)))
            for lb, hi, lo in ratios:
                if hi / lo >= 2.0:
                    L.append(f"> ⚠️ **`{lb}` 跨版本不是同一件事**：同一句「{lb}」的"
                             f"单注概率在各规则版本间相差 **{hi / lo:.1f} 倍**"
                             f"（最高 {hi:.3g} / 最低 {lo:.3g}）。合并成一个平均数是"
                             "错的口径，只能分版本读、分版本下结论。")
            L.append("")

        # ---- 结论：**逐门**判（不是逐彩种）——用户问的是特定的门，
        #      某个门在别的门上过了门槛，不能替它作证。
        L.append("| 门 | 对角线上最大 Δ | 空间 | η | 达实益门槛 |")
        L.append("|---|---|---|---|---|")
        passed: List[str] = []
        for lb in labels:
            cand = [(v["Δ"], n, v) for n, v in (diag.get(lb) or {}).items()
                    if isinstance(v.get("Δ"), float)]
            if not cand:
                L.append(f"| {lb} | — | — | — | 无数据 |")
                continue
            d, _n, v = max(cand, key=lambda t: t[0])
            ok = d >= TIER_OPT_MIN_GAIN
            if ok:
                passed.append(lb)
            sp = v.get("空间")
            et = v.get("η")
            L.append(f"| {lb} | {d:+.4f} | "
                     f"{(f'{sp:.4f}' if isinstance(sp, float) else '—')} | "
                     f"{(f'{et:+.2f}' if isinstance(et, float) else '—')} | "
                     f"{'是' if ok else '否'} |")
        L.append("")
        L.append("> ★ `η` 在 `空间` 趋零时**失去意义**（分母趋零 ⇒ 会把噪声放大成 +14.78 / "
                 "+2297 这种数）。`空间 < 0.001`（0.1pp）的格子只看 `Δ` 的绝对值，"
                 "**不许**用 `η` 论证「效率高」。")
        L.append("")
        if passed:
            L.append(f"> **{lot}**：对角线达实益门槛的门 = {'、'.join(passed)}。"
                     "这**不等于**可以接生产：① 只看对角线，非对角线不作证；"
                     "② 达门槛只说明效应量够大，统计可判性看各门自己的配对判决；"
                     "③ 接生产前仍须过 BH 与成本核算。")
        else:
            L.append(f"> **{lot}**：三个门的对角线 Δ 全部 < "
                     f"{TIER_OPT_MIN_GAIN * 100:.1f}pp ⇒ 按事先声明，"
                     "**保留为研究结论，不接生产选号**。"
                     "这不是「优化器不够好」，而是该预算下这些门的可争取空间本身"
                     "就接近于零（见 `空间` 列）——`E[达标注数]=K·p` 与选哪些票无关。")
        L.append("")

    L.append("> ★ 本表不改变任何一注的中奖概率。矩阵里的非对角线格是「为别的门优化，"
             "在这门上顺带发生的事」，不能拿来充当「该门有收益」的证据。")
    L.append("")
    return L
