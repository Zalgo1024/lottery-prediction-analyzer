# -*- coding: utf-8 -*-
"""影子三臂的观察点报告（只读）。

用途：攒够若干期后，用同一套口径把「读数」和「可判性判决」一次打出来，
避免临时手算净收益率、再凭肉眼挑一个当结论。

```bash
python scripts/shadow_report.py                  # 六彩种
python scripts/shadow_report.py --lottery 双色球
python scripts/shadow_report.py --json out.json  # 落 JSON 便于再加工
```

★ 本脚本**不写任何文件、不改台账**（`--json` 是唯一落盘点，且由调用方指定路径）。
★ 输出里的每一行判决都带「为什么不能下结论」，原因见 `data/shadow_verdict.py`。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _fmt(v, nd=2):
    if v is None:
        return "—"
    if isinstance(v, float):
        if v != v:                                  # nan
            return "—"
        return f"{v:,.{nd}f}"
    return str(v)


def _pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def _arm_table(res) -> list:
    lines = ["| 臂 | 期数 | 已结算 | 待结算 | 注数合计 | 成本 | 税前奖金 | 净盈亏 | 净收益率 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for arm, d in (res.get("臂") or {}).items():
        if d.get("缺"):
            lines.append(f"| {arm} | 0 | — | — | — | — | — | — | — |")
            continue
        lines.append("| {a} | {n} | {s} | {p} | {k} | {c} | {g} | {net} | {r} |".format(
            a=arm, n=d.get("期数"), s=d.get("已结算期数"), p=d.get("待结算期数"),
            k=d.get("注数合计"), c=_fmt(d.get("成本")), g=_fmt(d.get("税前奖金")),
            net=_fmt(d.get("净盈亏")), r=_pct(d.get("净收益率"))))
    return lines


def _verdict_table(res) -> list:
    rows = res.get("对照") or []
    if not rows:
        return []
    lines = ["| 对照 | 口径 | Δ | 配对差SD | z | p_BH | 判决 | 依据 |",
             "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        lines.append("| {c} | {m} | {d} | {sd} | {z} | {p} | **{v}** | {w} |".format(
            c=r.get("对照"), m=r.get("口径名"),
            d=_fmt(r.get("实测Δ"), 2), sd=_fmt(r.get("配对差SD"), 2),
            z=_fmt(r.get("z_实测"), 2), p=_fmt(r.get("p_BH"), 3),
            v=r.get("判决"), w=(r.get("判决说明") or "").replace("|", "/")))
    return lines


def _one(lottery: str, res) -> list:
    out = [f"### {lottery}", ""]
    per = res.get("期数") or {}
    out.append(f"- 记录期数 {per.get('记录', '—')}｜已结算 {per.get('已结算', '—')}｜"
               f"待结算 {per.get('待结算', '—')}｜注数 {res.get('注数')}")
    if not res.get("可用"):
        out.append(f"- **无判决**：{res.get('原因')}")
        out.append("")
        return out
    out.append(f"- 同预算：{(res.get('同预算') or {}).get('说明')}")
    out.append(f"- **总结**：{res.get('总结')}")
    out.append("")
    out.extend(_arm_table(res))
    out.append("")
    vt = _verdict_table(res)
    if vt:
        out.extend(vt)
        out.append("")
        mc = res.get("多重比较") or {}
        out.append(f"- 多重比较：{mc.get('方法')}｜检验数 {mc.get('检验数')}｜"
                   f"通过 {mc.get('通过数')}｜名义α {mc.get('名义α')}")
    out.append("")
    return out


def build_report(lotteries=None) -> str:
    from data import shadow as SH
    from data import shadow_verdict as V
    from config import LOTTERY_CONFIG

    lott = list(lotteries or LOTTERY_CONFIG)
    lines = ["# 影子三臂观察点报告", ""]
    lines.append("同一笔钱、同注数的三臂对照：当前模型 / 等注数随机 / 固定规则。")
    lines.append("两个无信息臂唯一差别是「打不打乱候选池顺序」——把「打败随机」与"
                 "「打败顺序」分开。")
    lines.append("")
    for lot in lott:
        try:
            res = SH.summary(lot)                     # 读数（每臂）
            verdict = V.compare(lot)                  # 判决（配对 + 可判性）
            res.update({k: verdict.get(k) for k in
                        ("可用", "原因", "期数", "对照", "总结", "多重比较", "注数")})
        except Exception as e:                        # noqa: BLE001
            lines.append(f"### {lot}\n\n- **计算失败**：{e}\n")
            continue
        lines.extend(_one(lot, res))

    lines.append("---")
    lines.append("")
    lines.append("## 读法（每次都要一起报）")
    lines.append("")
    lines.append("1. **先看「判决」再看数字**：`不可判` 的 Δ 只是描述，不是结论；"
                 "`可判` 也只用这一批期成立（n* 用实测 Δ 反推，偏乐观）。")
    lines.append("2. **Δ 由单期撑起时不看 Δ**：金额极右偏，一期固定奖大头就能让符号翻转。")
    lines.append("3. **优先比「当前模型 vs 等注数随机」**：它与固定规则臂的差只反映"
                 "随机化本身，不反映选号质量。")
    lines.append("4. **固定奖层面三臂的期望回款相同**（E[中奖注数]=K·p）⇒ "
                 "检出差异先查实现缺陷或浮动奖分摊效应。")
    lines.append("5. **样本不足不加注**：影子模式不占预算，只积累证据。")
    lines.append("")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="影子三臂观察点报告（只读）")
    ap.add_argument("--lottery", action="append", help="只跑指定彩种（可重复）")
    ap.add_argument("--json", metavar="PATH", help="把判决数据结构另存为 JSON")
    ap.add_argument("--out", metavar="PATH", help="把 Markdown 报告另存为文件")
    args = ap.parse_args(argv)

    md = build_report(args.lottery)

    if args.json:
        from data import shadow_verdict as V
        payload = V.compare_all()
        if args.lottery:
            payload = {k: v for k, v in payload.items() if k in set(args.lottery)}
        os.makedirs(os.path.dirname(os.path.abspath(args.json)) or ".", exist_ok=True)
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        print(f"[已写] {args.json}")

    if args.out:
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(md)
        print(f"[已写] {args.out}")
    else:
        print(md)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
