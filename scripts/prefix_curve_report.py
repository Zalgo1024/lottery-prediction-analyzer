# -*- coding: utf-8 -*-
"""前缀 K 曲线离线报告（研究通道，**不接生产、不改下注量**）。

用法：
    E:/Python/python.exe scripts/prefix_curve_report.py --periods 120
    E:/Python/python.exe scripts/prefix_curve_report.py --K 50 --seeds 2 --out logs/x.json

★ 默认参数是**算力受限的离线取值**（K=50 / 120 期 / 2 seed），不是生产 K。
  生产 K 由 `config/ticket_size.json` 现读且会漂移；本回放的整批对照列只代表
  这里传入的 K。前缀 n≤10 的读数依赖「按该 K 生成的票序」，换 K 要重跑。
  报告首部会原样回显预注册与反事实声明，不许只贴一张好看的表。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
# 不替换 sys.stdout：pytest / 采集器给的是 StringIO，TextIOWrapper 会直接抛。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from ev import prefix_curve as PC          # noqa: E402

OUT_DIR = Path(__file__).resolve().parent.parent / "logs"


def _fmt(x) -> str:
    if x is None:
        return "—"
    if isinstance(x, float):
        return f"{x:.6g}"
    return str(x)


def _fmt_ns(x, reason: str = "不可达") -> str:
    """n* 是期数：∞ 必须写成人话，且写明是「空间为 0」还是「无信息」。"""
    if x is None:
        return "—（未锚定）"
    if isinstance(x, float) and not math.isfinite(x):
        return f"∞（{reason}）"
    if isinstance(x, float):
        return f"{x:,.0f}"
    return str(x)


def _ns_reason(cell: dict) -> str:
    if cell.get("无信息"):
        return "评估窗无信息"
    if cell.get("空间上界") == 0:
        return "理论空间为 0"
    return "不可达"


def render(result: dict, verdict: dict) -> str:
    pre = result["预注册"]
    L: list = []
    L.append(f"# 前缀 K 曲线（离线研究 · {result['彩种']}）")
    L.append("")
    L.append("## 预注册（跑数之前固定）")
    for k in ("版本", "锁定日", "性质", "口径", "主口径n", "前缀档", "门槛",
              "基线臂", "族规模", "名义α", "探索窗比例", "判据", "效应量锚定",
              "停止规则", "确认力"):
        L.append(f"- **{k}**：{json.dumps(pre[k], ensure_ascii=False)}")
    L.append("")
    L.append("## 口径声明")
    for k, v in result["口径说明"].items():
        L.append(f"- **{k}**：{v}")
    if not result.get("降级"):
        ps = result["单注p"]
        L.append(f"- **单注 p（分门槛）**：≥5元 {ps['≥5元']:.6e} / "
                 f"≥四等 {ps['≥四等']:.6e}；两者差几个数量级，上界必须分开算")
    d = result["分母"]
    L.append(f"- **分母**：开奖期数 {d['开奖期数']} / 参与期数 {d['参与期数']} / "
             f"未参与期数 {d['未参与期数']}；{d['未参与说明']}")
    L.append(f"- **参数**：K={result['K']}（离线取值，非生产 K）、"
             f"目标 {result['目标']}（单注 p={result['单注达标概率']:.6e}）、"
             f"seeds={result['seeds']}、臂={result['臂']}")
    L.append("")
    if result.get("降级"):
        L.append(f"降级：{result['降级']}")
        return "\n".join(L)

    L.append("## 前缀曲线（主口径按 n 结算，整批只作对照）")
    L.append("")
    L.append("| n | 臂 | ≥5元期率 | ≥四等期率 | 成本(元) | 净收益(元) | ≥5元:n·p | ≥5元:空间上界 | ≥四等:空间上界 | 饱和 | 非实际购买 |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    sk = str(result["seeds"][0])
    for n in result["全档"]:
        for arm in result["臂"]:
            r = result["曲线"][sk][arm][str(n)]
            b5, bt = r["上界"]["≥5元"], r["上界"]["≥四等"]
            flag = "★ 整批对照" if r["非实际购买"] else ""
            L.append(f"| {n} | {arm} {flag} | {r['≥5元期率']:.4f} | "
                     f"{r['≥四等期率']:.4f} | {r['成本']:.2f} | {r['净收益']:.2f} | "
                     f"{b5['n·p']:.4g} | {b5['可争取空间上界']:.4g} | "
                     f"{bt['可争取空间上界']:.4g} | "
                     f"{'是' if r['饱和'] else '否'} | "
                     f"{'是' if r['非实际购买'] else '否'} |")
    L.append("")
    L.append(f"> {result['口径说明']['整批对照']}")
    L.append(f"> {result['口径说明']['机械上升提示']}")
    # ★ 整批列的大额净收益常被单期浮动奖撑起；不披露就会被读成「策略赚钱」。
    whole = [(arm, result["曲线"][sk][arm][str(result["K"])])
             for arm in result["臂"]]
    flips = [(arm, r) for arm, r in whole
             if r.get("期数") and r["净收益"] * r["剔除最大一期后净收益"] < 0]
    if flips:
        L.append("")
        L.append("> ⚠️ **整批对照列的净收益被单期最大实得翻转**："
                 + "；".join(
                     f"{arm} {r['净收益']:,.0f} 元 → 剔除最大一期后 "
                     f"{r['剔除最大一期后净收益']:,.0f} 元"
                     for arm, r in flips)
                 + "。该列是「非实际购买」对照，浮动奖金额缺失另计，"
                   "不得把单期大奖读成策略收益。")
    else:
        L.append("")
        L.append("> 整批对照列未检出「单期最大实得翻转净收益符号」的情形"
                 "（仅表示本次样本未见，不代表不存在）。")
    L.append("")
    L.append("## 理论上界（按门槛分列，解析值不具确认力）")
    L.append("")
    L.append("| n | 门槛 | 单注 p | n·p | iid 随机基线 | 理论上限 | 可争取空间上界 |")
    L.append("|---|---|---|---|---|---|---|")
    first = result["曲线"][sk][result["臂"][0]]
    for n in result["全档"]:
        for label in ("≥5元", "≥四等"):
            b = first[str(n)]["上界"][label]
            L.append(f"| {n} | {label} | {b['单注p']:.6e} | {b['n·p']:.6g} | "
                     f"{b['iid随机基线']:.6g} | {b['理论上限']:.6g} | "
                     f"{b['可争取空间上界']:.6g} |")
    L.append("")
    L.append("## 判决（评估窗唯一判决）")
    if verdict.get("降级"):
        L.append(f"- 降级：{verdict['降级']}")
    else:
        L.append(f"- 探索窗 {verdict['探索窗期数']} 期 / 评估窗 {verdict['评估窗期数']} 期；"
                 f"BH 族规模 {verdict['族规模']}，α={verdict['名义α']}，"
                 f"检出 {verdict['检出数']} 格")
        others = len(result["seeds"]) - 1
        tail = (f"其余 {others} 个 seed 只用于稳定性描述，"
                if others else "本批仅 1 个 seed，跨 seed 稳定性显式标「未测」，")
        L.append(f"- **判决 seed = {result['seeds'][0]}**（预注册指定）；{tail}"
                 "不进 BH、不另作一次判决——不许把「未纳入检验」读成「未检出即通过」。")
        L.append("")
        L.append("| n | 门槛 | 臂 | 实测Δ | z | p | p_BH | 空间上界 | n*实测 | n*上界锚定 | n*保守 | 可判性 | 单期占Δ比 | 判决 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for c in verdict["格"]:
            row = c["配对"]
            why = _ns_reason(c)
            L.append(f"| {c['n']} | {c['门槛']} | {c['臂']} | "
                     f"{_fmt(row.get('实测Δ'))} | {_fmt(row.get('z_实测'))} | "
                     f"{_fmt(c['p'])} | {_fmt(c['p_BH'])} | "
                     f"{_fmt(c['空间上界'])} | {_fmt_ns(c['n*_实测效应'])} | "
                     f"{_fmt_ns(c['n*_上界锚定'], why)} | {_fmt_ns(c['n*_保守'], why)} | "
                     f"{row.get('判决', '—')} | "
                     f"{_fmt(c['集中度']['单期占Δ比'])} | {c['判决']} |")
        L.append("")
        L.append(f"**结论**：{verdict['结论']}")
        L.append("")
        L.append(f"> 功效限制：评估窗仅 {verdict['评估窗期数']} 期，绝大多数格的 "
                 "`n*_保守` 远大于可用期数 ⇒ 「未检出」包含「功效不足以识别」，"
                 "不等于「已测出无差异」。窄门槛在短窗里零命中是预期，不是异常。")
        L.append(f"> 理论不可判 {verdict['理论不可判档数']} 格"
                 f"（空间上界为 0 ⇒ n*=∞）；未锚定 {verdict['未锚定档数']} 格。")
        L.append("> `∞（评估窗无信息）` = 评估窗两臂逐期全同（通常是窄门槛零命中）；"
                 "`pair()` 的零方差分支原本会给 `n*=1`，本层已归一为 ∞ —— "
                 "不许把「没测出差异」读成「一期就够」。")
    L.append("")
    L.append("## 影子验证所需期数（按解析上界锚定）")
    L.append("")
    if verdict.get("降级"):
        L.append("- 降级，未计算；不许把「没算」写成「不需要」。")
    else:
        main_n = result["预注册"]["主口径n"]
        avail = verdict["评估窗期数"]
        rows = [c for c in verdict["格"] if c["n"] == main_n]
        L.append(f"- 主口径 n={main_n}，评估窗可用 {avail} 期；`n*_保守` = "
                 "max(实测 Δ 反推, 解析上界锚定)，取大者为准。")
        L.append("")
        L.append("| 门槛 | 臂 | 空间上界 | n*（实测） | n*（上界锚定） | n*保守 | 相对可用期数 |")
        L.append("|---|---|---|---|---|---|---|")
        for c in rows:
            ns = c["n*_保守"]
            why = _ns_reason(c)
            ratio = ("∞" if not isinstance(ns, float) or not math.isfinite(ns)
                     else f"{ns / avail:,.1f}×")
            L.append(f"| {c['门槛']} | {c['臂']} | {_fmt(c['空间上界'])} | "
                     f"{_fmt_ns(c['n*_实测效应'])} | {_fmt_ns(c['n*_上界锚定'], why)} | "
                     f"{_fmt_ns(ns, why)} | {ratio} |")
        L.append("")
        L.append("- n=1 的可争取空间恒为 0 ⇒ 这一档无论攒多少期都不能判；"
                 "窄门槛（≥四等）的 n* 通常远超可用期数。")
        L.append("- 影子验证应先攒够 `n*_保守` 期再开判；只用实测 Δ 反推会偏乐观，"
                 "搜索批读数还会被抬高，故须另取独立评估批、只追加不回改历史。")
        L.append("- 接线状态：本轮为**历史回放**，未接生产出号冻结、未接未来开奖追加；"
                 "影子台账无真实未来期记录（缺席≠通过，不许拿历史窗冒充确认力）。")
    L.append("")
    L.append("## 稳定性（跨 seed + 时间窗，均为描述性、不进 BH）")
    L.append("")
    L.append("| 臂/n | 跨seed状态 | ≥5元全距 | ≥四等全距 | 窗口状态 | ≥5元前后半差 | ≥四等前后半差 | 说明 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for k, v in result["稳定性"].items():
        measured = v.get("状态") == "已测"
        w = v.get("窗口状态", "—")
        d5 = v.get("≥5元期率_评估窗前后半差_跨seed最大")
        dt = v.get("≥四等期率_评估窗前后半差_跨seed最大")
        note = f"seeds={v['seeds']}" if measured else v.get("原因", "")
        if not str(w).startswith("已测"):
            note = "；".join(x for x in (note, v.get("窗口原因", "")) if x)
        L.append(f"| {k} | {v.get('状态', '—')} | "
                 f"{_fmt(v.get('≥5元期率_跨seed全距')) if measured else '—'} | "
                 f"{_fmt(v.get('≥四等期率_跨seed全距')) if measured else '—'} | "
                 f"{w} | {_fmt(d5) if d5 is not None else '—'} | "
                 f"{_fmt(dt) if dt is not None else '—'} | {note} |")
    L.append("")
    L.append("> 前后半差 = 评估窗对半后两段的期率差，逐 seed 取最大绝对值；"
             "这是**口径稳定性诊断**，不是显著性检验，也不构成「策略稳定」的结论。")
    L.append("")
    L.append(f"冻结快照 {len(result['冻结快照'])} 份（版本 {result['预注册']['版本']}）。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lottery", default="双色球")
    ap.add_argument("--K", type=int, default=50,
                    help="整批注数（离线取值；生产 K 由 ticket_size.json 现读）")
    ap.add_argument("--periods", type=int, default=120)
    ap.add_argument("--seeds", default="0,1")
    ap.add_argument("--target-grade", default="四等")
    ap.add_argument("--prefix-ks", default="1,3,5,10")
    ap.add_argument("--pool-size", type=int, default=500)
    ap.add_argument("--reselect-every", type=int, default=30)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    seeds = [int(x) for x in args.seeds.split(",") if x.strip()]
    ks = [int(x) for x in args.prefix_ks.split(",") if x.strip()]
    target = {"kind": "grade", "grade": args.target_grade}
    result = PC.run_prefix_curve(
        args.lottery, K=args.K, target=target, prefix_ks=ks,
        periods=args.periods, seeds=seeds, pool_size=args.pool_size,
        reselect_every=args.reselect_every)
    verdict = PC.verdict_curve(result)
    text = render(result, verdict)
    print(text)

    if args.out:
        out = Path(args.out)
    else:
        tag = f"{args.lottery}_K{args.K}_p{args.periods}_s{len(seeds)}_{args.target_grade}"
        out = OUT_DIR / f"prefix_curve_{tag}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"结果": result, "判决": verdict, "报告": text},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已落盘：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
