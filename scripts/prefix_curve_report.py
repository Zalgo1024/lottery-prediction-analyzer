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


def render(result: dict, verdict: dict) -> str:
    pre = result["预注册"]
    L: list = []
    L.append(f"# 前缀 K 曲线（离线研究 · {result['彩种']}）")
    L.append("")
    L.append("## 预注册（跑数之前固定）")
    for k in ("版本", "锁定日", "性质", "口径", "主口径n", "前缀档", "门槛",
              "基线臂", "族规模", "名义α", "探索窗比例", "判据", "停止规则", "确认力"):
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
        L.append("")
        L.append("| n | 门槛 | 臂 | 实测Δ | z | p | p_BH | 可判性 | 单期占Δ比 | 判决 |")
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        for c in verdict["格"]:
            row = c["配对"]
            L.append(f"| {c['n']} | {c['门槛']} | {c['臂']} | "
                     f"{_fmt(row.get('实测Δ'))} | {_fmt(row.get('z_实测'))} | "
                     f"{_fmt(c['p'])} | {_fmt(c['p_BH'])} | "
                     f"{row.get('判决', '—')} | "
                     f"{_fmt(c['集中度']['单期占Δ比'])} | {c['判决']} |")
        L.append("")
        L.append(f"**结论**：{verdict['结论']}")
        L.append("")
        L.append(f"> 功效限制：评估窗仅 {verdict['评估窗期数']} 期，绝大多数格的 "
                 "`n*_保守` 远大于可用期数 ⇒ 「未检出」包含「功效不足以识别」，"
                 "不等于「已测出无差异」。窄门槛在短窗里零命中是预期，不是异常。")
    L.append("")
    L.append("## 稳定性（跨 seed 全距，描述性、不进 BH）")
    L.append("")
    L.append("| 臂/n | 状态 | ≥5元期率全距 | ≥四等期率全距 | 说明 |")
    L.append("|---|---|---|---|---|")
    for k, v in result["稳定性"].items():
        if v.get("状态") != "已测":
            L.append(f"| {k} | {v.get('状态', '—')} | — | — | {v.get('原因', '')} |")
        else:
            L.append(f"| {k} | 已测 | {v['≥5元期率_跨seed全距']:.4f} | "
                     f"{v['≥四等期率_跨seed全距']:.4f} | seeds={v['seeds']} |")
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
