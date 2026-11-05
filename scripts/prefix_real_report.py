# -*- coding: utf-8 -*-
"""真实生产票序前缀读数 —— 离线报告（只读、不接生产、不改下注量）。

用法：
    E:/Python/python.exe scripts/prefix_real_report.py --seeds 0,1,2,3,4
    E:/Python/python.exe scripts/prefix_real_report.py --lottery 双色球 \
        --out logs/prefix_real_ssq.json

产物：`--out` 指定的 JSON（含全部逐期序列）+ 同名 `_report.md`。
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data.feedback import FEEDBACK_DIR                     # noqa: E402
from ev import prefix_real as PR                           # noqa: E402


def available_lotteries() -> List[str]:
    """★ 彩种清单从反馈文件反推，不手写具名清单（防「半份清单」静默漏卡片）。"""
    return sorted(p.name[: -len("_feedback_history.json")]
                  for p in Path(FEEDBACK_DIR).glob("*_feedback_history.json"))


def _fmt(x: Any) -> str:
    if x is None:
        return "—"
    if isinstance(x, bool):
        return "是" if x else "否"
    if isinstance(x, float):
        return "∞" if not math.isfinite(x) else f"{x:,.4g}"
    return str(x)


def _grade_cell(c: Dict[str, Any]) -> str:
    """「≥四等」不适用必须写人话，不能被「—」读成缺失或 0。"""
    v = c["≥四等期率"]
    return _fmt(v) if v is not None else "不适用"


def _sep(ncols: int) -> str:
    """分隔行按列数生成：手写 `---` 段数极易少一段，撑破 markdown 表格。"""
    return "|" + "|".join(["---"] * ncols) + "|"


def render(results: List[Dict[str, Any]], *, main_lottery: str) -> str:
    head = results[0]
    L: List[str] = []
    L.append("# 真实生产票序 · 前缀 K 曲线（离线研究，不接生产）")
    L.append("")
    L.append("> 与上一轮 `prefix_curve` 的区别：那一轮用的是**离线三臂回放生成的票序**"
             "（K=100、反事实假设）；本报告的票序来自 `training/feedback/"
             "*_feedback_history.json` 里**当时真实出号**的反馈，"
             "按 `批次内序号` 还原顺序。两者票序 / K / 期数都不同，**不可直接比较读数**。")
    L.append("")
    L.append("## 口径声明（结果之前固定）")
    L.append("")
    for k, v in head["口径说明"].items():
        L.append(f"- **{k}**：{v}")
    L.append("")
    L.append(f"- **主口径 n = {head['主口径n']}**（沿用前缀研究的授权假设值，非实测）；"
             f"档表 {head['前缀档']}；seeds={head['seeds']}。")
    L.append("- **接线状态**：历史反馈回放；**未接未来开奖**。影子验证仍是缺席项，"
             "不把「未纳入」读成「已通过」。")
    L.append("")
    L.append("## 数据体检（每一项排除都写原因，不静默 filter）")
    L.append("")
    L.append("| 彩种 | 开奖期(窗口) | 参与 | 未参与 | 显式批 | 主口径批 | 序号推算批 | "
             "重出批 | 前视否 | 序号不全 | 注数范围 | 「≥四等」 |")
    L.append(_sep(12))
    for r in results:
        d = r["分母"]
        e = r["数据体检"]["排除计数"]
        rng = r["数据体检"]["注数范围"]
        rng_s = f"{rng[0]}~{rng[1]}" if len(rng) == 2 else "—（无可用批次）"
        g = r["数据体检"]["等级门槛"]
        L.append(f"| {r['彩种']} | {d['开奖期数']} | {d['参与期数']} | "
                 f"{d['未参与期数']} | {e['explicit批数']} | {e['通过批数']} | "
                 f"{e['derived批数']} | {e['重出批数']} | "
                 f"{e['前视不通过批数']} | {e['序号不完整批数']} | {rng_s} | "
                 f"{'适用' if g['适用'] else '不适用'} |")
    L.append("")
    L.append("- **序号推算批**＝无显式 `批次号` 的旧记录，`批次内序号` 只能按组内位置推算"
             " ⇒ 不满足「票序还原」前提，整批排除。")
    L.append("- **重出批**＝同一期有多个批次；主口径只取**出号时间最早**的第 1 批，"
             "其余批不进主判据，避免事后挑表现最好的批。")
    L.append("- **前视否**＝批次时间缺失或晚于开奖日期（日级精度）。全部通过只说明"
             "「日级旁证成立」，**不能自证排名在开奖前冻结**。")
    L.append("- 「≥四等」对数字型 / 快乐8 **不适用**：规则表里的奖级名是玩法名"
             "（直选 / 组选 / 选十中零），不是等级序号；写作「不适用 + 原因」，不写 0。")
    L.append("")
    L.append("## 分母（全期时间轴建行，三数并列）")
    L.append("")
    L.append("| 彩种 | 开奖期数 | 参与期数 | 未参与期数 | 开奖轴可用 | 未参与期号 |")
    L.append(_sep(6))
    for r in results:
        d = r["分母"]
        ids = d["未参与期号"]
        shown = ", ".join(ids[:6]) + ("…" if len(ids) > 6 else "") if ids else "—"
        L.append(f"| {r['彩种']} | {d['开奖期数']} | {d['参与期数']} | "
                 f"{d['未参与期数']} | {'是' if d['开奖轴可用'] else '否'} | {shown} |")
    L.append("")
    L.append(f"> {head['口径说明']['分母']}")
    L.append("")
    L.append(f"## 主口径 n = {head['主口径n']}：四判据（逐彩种）")
    L.append("")
    L.append("| 彩种 | ≥5元期率 | ≥5元(参与期分母) | ≥5元(全期轴分母) | ≥四等期率 | "
             "可用/参与 | 成本(元) | 净收益(元) | 单期最大实得 | 剔除后净收益 |")
    L.append(_sep(10))
    for r in results:
        c = r["曲线"][str(head["主口径n"])]
        L.append(f"| {r['彩种']} | {_fmt(c['≥5元期率'])} | "
                 f"{_fmt(c['≥5元期率_参与期分母'])} | "
                 f"{_fmt(c['≥5元期率_全期轴分母'])} | "
                 f"{_grade_cell(c)} | "
                 f"{c['可用期数']}/{c['参与期数']} | {c['成本合计']:,.2f} | "
                 f"{c['净收益']:,.2f} | {c['单期最大实得']:,.2f} | "
                 f"{c['净收益_剔除单期最大实得']:,.2f} |")
    L.append("")
    for r in results:
        c = r["曲线"][str(head["主口径n"])]
        if c["不适用原因"]:
            L.append(f"- {r['彩种']} 不适用原因：{'；'.join(c['不适用原因'])}")
        if not r["数据体检"]["等级门槛"]["适用"]:
            L.append(f"- {r['彩种']} 「≥四等」：{c['≥四等状态']}")
        share = (c["单期最大实得"] / c["实得合计"]
                 if c["实得合计"] > 0 else None)
        if share is not None and (share >= 0.5 or c["被单期实得翻转"]):
            L.append(f"- ⚠ {r['彩种']} 单期最大实得占该档总实得 {share:.1%}"
                     + ("，且**翻转净收益符号**" if c["被单期实得翻转"] else "")
                     + " ⇒ 净收益对单期大奖敏感，不可当作稳定读数。")
    L.append("")
    main = next((r for r in results if r["彩种"] == main_lottery), head)
    L.append(f"## 前缀曲线：{main['彩种']}（真实票序）")
    L.append("")
    L.append("| 档 | 可用期数 | 不适用期数 | ≥5元期率 | ≥5元(参与期分母) | ≥5元(全期轴分母) | "
             "≥四等期率 | 成本(元) | 实得(元) | 净收益(元) | 剔除最大单期实得后 | "
             "金额未知注 | 单价缺失注 | 非实际购买 |")
    L.append(_sep(14))
    def curve_row(key: str, c: Dict[str, Any]) -> str:
        return (f"| {key} | {c['可用期数']} | {c['不适用期数']} | "
                f"{_fmt(c['≥5元期率'])} | {_fmt(c['≥5元期率_参与期分母'])} | "
                f"{_fmt(c['≥5元期率_全期轴分母'])} | {_grade_cell(c)} | "
                f"{c['成本合计']:,.2f} | {c['实得合计']:,.2f} | "
                f"{c['净收益']:,.2f} | "
                f"{c['净收益_剔除单期最大实得']:,.2f} | "
                f"{c['金额未知注数']} | {c['单价缺失注数']} | "
                f"{'★ 是' if c['非实际购买'] else '否'} |")
    for n in main["前缀档"]:
        L.append(curve_row(str(n), main["曲线"][str(n)]))
    L.append(curve_row(PR.WHOLE_BATCH_KEY, main["曲线"][PR.WHOLE_BATCH_KEY]))
    L.append("")
    L.append("> `≥5元期率` 随 n 上升是 `1−(1−p)^n` 的必然结果，**不是策略效果**；"
             "三个分母并列就是为了不让 K<n 的期被静默剔除后抬高档位读数。"
             "成本按 2 元/注计；`单价缺失注` 按 2 元补并单独计数。")
    L.append(f"> {main['口径说明']['整批对照']}")
    L.append("")
    L.append(f"## 顺序对照：{main['彩种']}（描述性，不进显著检验）")
    L.append("")
    L.append("| n | 对照臂 | 状态 | 配对期数 | 实测Δ(跨seed均值) | Δ跨seed全距 | "
             "噪声判读 | 单期占Δ比(跨seed最大) | 未配对期数 |")
    L.append(_sep(9))
    for n in main["前缀档"]:
        for label, arm in main["顺序对照"][str(n)].items():
            per = arm["逐seed"]
            L.append(f"| {n} | {label} | {arm['状态']} | "
                     f"{next((s['配对期数'] for s in per if s.get('配对期数')), '—')} | "
                     f"{_fmt(arm['实测Δ_跨seed均值'])} | "
                     f"{_fmt(arm['实测Δ_跨seed全距'])} | "
                     f"{arm['噪声判读']} | "
                     f"{_fmt(arm['单期占Δ比_跨seed最大'])} | "
                     f"{arm['未配对期数']} |")
    L.append("")
    L.append(f"> {main['口径说明']['顺序对照']}")
    L.append(f"> {main['口径说明']['先验零']}")
    L.append("")
    L.append(f"## 稳定性：{main['彩种']}（描述性，不进 BH）")
    L.append("")
    L.append("| n | 状态 | 可用期数 | 评估窗前半 ≥5元期率 | 后半 | 前后半差 | 说明 |")
    L.append(_sep(7))
    for n in main["前缀档"]:
        s = main["稳定性"][str(n)]
        if s["状态"].startswith("未测"):
            L.append(f"| {n} | {s['状态']} | {s['可用期数']} | — | — | — | {s['原因']} |")
        else:
            note = ("评估窗前后半均零命中 ⇒ 无信息，不是稳定；"
                    if s["评估窗无信息"] else "")
            L.append(f"| {n} | 已测（描述性） | {s['可用期数']} | "
                     f"{s['≥5元期率_评估窗前半']:.4f} | "
                     f"{s['≥5元期率_评估窗后半']:.4f} | {s['前后半差']:.4f} | "
                     f"{note}切分沿用 `window_split` 单一真源（探索窗 "
                     f"{s['切分']['探索窗期数']} 期 / 评估窗 "
                     f"{s['切分']['评估窗期数']} 期） |")
    L.append("")
    L.append("> 时间窗切分与跨 seed 全距都只是**描述性**稳定性读数：本轮期数不足以"
             "支撑任何显著性结论；「未测」显式写原因，前后半同为 0 标「无信息」，"
             "都不许用 0 冒充稳定。窄门槛在短窗里零命中是预期，不是异常。")
    L.append("> ★ 评估窗用的是可用期自身的 `window_split` 切分，与判决层同源；"
             "本模块不做判决，切分只用于描述性读数。")
    L.append("")
    L.append("## 判决状态与功效限制")
    L.append("")
    L.append(f"- **{main['判决']['状态']}**：{main['判决']['原因']}")
    L.append("- 上一轮正式读数（`logs/prefix_curve_formal_ssq_K100_p360_s5.json` 的"
             " `n*_保守` 列）在主口径 ≥5元 上是上万期量级；本轮真实票序只有 19~39 期，"
             "相差三个数量级 ⇒ 不做 BH、不做配对检验、不写「检出 / 未检出」结论。"
             "这里刻意不把那个数复制成常量，避免形成第二真源。")
    L.append(f"- {main['口径说明']['术语']}")
    L.append(f"- {main['口径说明']['判决限制']}")
    L.append("")
    L.append(f"## 描述性结论：{main['彩种']}（不是统计判决）")
    L.append("")
    key = str(main["主口径n"])
    c = main["曲线"][key]
    wb = main["曲线"][PR.WHOLE_BATCH_KEY]
    L.append(f"- 按真实票序**只买前 {key} 注**：{c['可用期数']} 期成本 "
             f"{c['成本合计']:,.0f} 元、实得 {c['实得合计']:,.0f} 元、"
             f"净收益 {c['净收益']:,.0f} 元；`≥5元期率` {_fmt(c['≥5元期率'])}，"
             f"`≥四等期率` {_grade_cell(c)}。")
    L.append(f"- 同一批票**整批全买**（非实际购买）：成本 "
             f"{wb['成本合计']:,.0f} 元、`≥5元期率` {_fmt(wb['≥5元期率'])}，"
             f"是前 {key} 注成本的 "
             f"{(wb['成本合计'] / c['成本合计'] if c['成本合计'] else float('nan')):,.1f} 倍。"
             "整批档期率更高是 `1−(1−p)^K` 把 K 堆上去的必然结果，"
             "不代表选号更好，也不等于真实投入。")
    noisy = [f"n={n}/{label}" for n in main["前缀档"]
             for label, arm in main["顺序对照"][str(n)].items()
             if "不可分辨" in arm["噪声判读"]]
    L.append(f"- 顺序对照：{len(noisy)} 个「档×对照臂」组合的跨 seed 全距不小于 "
             "Δ 绝对值 ⇒ 抽样噪声与信号同量级，本样本无法分辨前 n 注是否比同批"
             "随机 n 注承载更多顺序信息；这**不是**统计判决，也不构成证明。")
    L.append("- 结论口径：在可得期数内，前 n 注的读数由成本与 `n·p` 主导；"
             "把整批口径的高达标率当成「我买前几注」的战绩，是口径错配。"
             "窄门槛在短窗里零命中是预期，不是异常。"
             f"{main['口径说明']['术语']}")
    if wb["单价缺失注数"]:
        L.append(f"- 整批档有 {wb['单价缺失注数']} 注**未记录单价**，按 2 元/注补并单独"
                 "计数（老批次没写 `单价` 字段）；成本合计含这部分推定值，"
                 "已在表中并列披露。")
    L.append("")
    L.append("## 边界")
    L.append("")
    L.append("全程离线只读：`config/ticket_size.json` / CSV / 预算闸门 / 看板结算 / "
             "推送 / 下注量零改动，生产路径不 import 本模块。整批 K 注口径原样保留，"
             "本报告只回答「按真实出号顺序买前 n 注」的描述性读数。")
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="真实生产票序前缀读数（离线）")
    ap.add_argument("--lottery", default="", help="只跑一个彩种；缺省跑全部反馈文件")
    ap.add_argument("--seeds", default="0,1,2,3,4")
    ap.add_argument("--ks", default="", help="覆盖前缀档，如 1,3,5,10")
    ap.add_argument("--out", default="logs/prefix_real_all.json")
    args = ap.parse_args()

    lots = [args.lottery] if args.lottery else available_lotteries()
    seeds = tuple(int(s) for s in args.seeds.split(",") if s.strip())
    ks = (tuple(int(k) for k in args.ks.split(",") if k.strip())
          if args.ks else None)
    if not lots:
        print("没有找到任何反馈文件", file=sys.stderr)
        return 2

    results = []
    for lot in lots:
        r = PR.run_prefix_real(lot, seeds=seeds, prefix_ks=ks)
        results.append(r)
        print(f"[prefix-real] {lot}: 开奖 {r['分母']['开奖期数']} / "
              f"参与 {r['分母']['参与期数']} / 未参与 {r['分母']['未参与期数']} 期；"
              f"n={r['主口径n']} ≥5元期率={r['曲线'][str(r['主口径n'])]['≥5元期率']}")

    main_lottery = args.lottery or ("双色球" if "双色球" in lots else lots[0])
    text = render(results, main_lottery=main_lottery)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"主彩种": main_lottery, "彩种": lots,
                               "seeds": list(seeds), "结果": results,
                               "报告": text},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    md = out.with_name(out.stem + "_report.md")
    md.write_text(text + "\n", encoding="utf-8")
    print(f"[prefix-real] 落盘 {out} / {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
