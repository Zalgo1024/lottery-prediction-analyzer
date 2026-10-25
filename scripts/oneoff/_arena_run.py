# -*- coding: utf-8 -*-
"""按 `config/preregistration.json` 跑完整对照实验（Stage 1）。

用法：PYTHONUTF8=1 E:/Python/python.exe -u scripts/oneoff/_arena_run.py
输出：output/arena_run_<ts>.json + 控制台摘要
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, r"E:\707")

from ev import preregistration as PR          # noqa: E402
from ev.arena import runner as RUN            # noqa: E402

ARMS = ["random", "tier_coverage", "union_greedy", "lowcrowd", "prod_mix", "hybrid"]


def expand(cells):
    out = []
    for c in cells:
        K = c["K"]
        if isinstance(K, list):
            for k in K:
                d = dict(c)
                d["K"] = int(k)
                d["名"] = f"{c['名']}-K{k}"
                d["主假设"] = False
                out.append(d)
        else:
            out.append(c)
    return out


def main() -> None:
    doc = PR.load()
    const = PR.constants(doc)
    cells = expand(PR.cells(doc))
    print("=" * 120)
    print(f"预注册 版本 {doc['版本']} 锁定日 {doc['锁定日']} 哈希 {doc['哈希'][:12]}…")
    print(f"常量：{json.dumps(const, ensure_ascii=False)}")
    print(f"臂：{ARMS}")
    print(f"格：{[c['名'] for c in cells]}")
    print("=" * 120, flush=True)

    t0 = time.time()
    outs = []
    for c in cells:
        lot = c["彩种"]
        # 大乐透的 lowcrowd 与 hybrid 必然降级（同一道拥挤度闸门，hybrid 的 u 输入
        # 与 lowcrowd 同源），仍然跑，把「暂不评估」记下来 —— 这是第一等状态，
        # 不是 0 期、不是失败（v1.4 起 hybrid 进主检验，BH 的 m 3→4）
        r = RUN.run_cell(c, const=const, arms=ARMS, verbose=True)
        outs.append(r)
        print()
        with open(r"E:\707\output\_arena_run_partial.json", "w", encoding="utf-8") as f:
            json.dump({"预注册": {"版本": doc["版本"], "锁定日": doc["锁定日"],
                                  "哈希": doc["哈希"]},
                       "常量": const, "格": outs}, f, ensure_ascii=False, indent=1,
                      default=str)
        print(f"   （中间结果已写，累计 {time.time()-t0:.0f}s）", flush=True)
        print()

    # ---------------- 汇总表
    print("=" * 150)
    print(f"{'格':<26}{'臂':<15}{'注':>5}{'达标率':>9}{'哨兵':>8}{'精确P':>9}"
          f"{'实测Δ':>10}{'SE':>8}{'z':>7}{'精确Δ':>10}{'p_BH':>9}{'n*保守':>10}{'期数':>7}  {'判决'}")
    print("-" * 150)
    for r in outs:
        for name, row in r["臂"].items():
            if row.get("状态") != "完成":
                print(f"{r['格']:<26}{name:<16}{'—':>4}  {row.get('状态')}：{str(row.get('原因'))[:60]}")
                continue
            pr = row.get("配对") or {}
            def f(k, d=4):
                v = pr.get(k)
                return f"{v:.{d}f}" if isinstance(v, (int, float)) else "—"
            dex = pr.get("外部效应量")
            ex = row.get("精确P")
            exs = f"{ex:.4f}" if isinstance(ex, (int, float)) else "—"
            dex_s = f"{dex:.4f}" if isinstance(dex, (int, float)) else "—"
            ns = pr.get("n*_保守")
            ns_s = f"{ns:.0f}" if isinstance(ns, (int, float)) and ns != float("inf") else "—"
            print(f"{r['格']:<26}{name:<16}{row['实际注数']:>4}"
                  f"{row['达标率']:>9.4f}{row['哨兵_单期达标注数']:>8.3f}"
                  f"{exs:>9}{f('实测Δ',4):>10}{f('配对SE',4):>8}{f('z_实测',2):>7}"
                  f"{dex_s:>10}{f('p_BH',4):>9}{ns_s:>10}"
                  f"{r['期数']:>7}  {pr.get('判决','（基线）')}")
        print("-" * 150)

    out = r"E:\707\output\_arena_run.json"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump({"预注册": {"版本": doc["版本"], "锁定日": doc["锁定日"],
                              "哈希": doc["哈希"], "基线臂": doc["基线臂"]},
                   "常量": const, "臂": ARMS, "格": outs},
                  f, ensure_ascii=False, indent=1, default=str)
    print(f"已写 {out}；总耗时 {time.time()-t0:.0f}s")

    # ---------------- Markdown 报告（复用 ev.arena.report，让「预注册回显」这条路被真正走过）
    from ev.arena import report as RPT
    main_cell = next((c for c in cells if c.get("主假设")), {})
    main_run = next((r for r in outs if r.get("主假设")), {})
    vd = RPT.verdict_cell(main_run, main_cell)
    extra = [f"## 主格判决\n",
             f"- 格：`{main_cell.get('名')}`　主假设臂 `{main_cell.get('主假设臂')}`"
             f" vs 对比臂 `{main_cell.get('对比臂')}`",
             f"- 判决：**{vd}**"]
    if vd == "可判":
        hit_v = ("改善" if (main_run.get("臂", {}).get(main_cell.get("主假设臂"), {})
                            .get("配对", {}).get("实测Δ") or 0) > 0 else "恶化")
        # ★ 净收益侧（2026-10-24 起有数）：从主假设臂的**净收益配对**取判决，
        #   不再硬写「未测」。可判 + Δ>0 → 改善；其余（含集中度守门降级）→ 不可判。
        net_v, share, ndelta = "不可判", None, None
        for mp in (main_run.get("净收益配对") or []):
            if mp.get("臂A") == main_cell.get("主假设臂"):
                share = mp.get("金额_单次大奖占总实得比")
                ndelta = mp.get("实测Δ")
                if mp.get("判决") == "可判":
                    net_v = "改善" if (ndelta or 0) > 0 else "恶化"
                break
        v = RPT.verdict_matrix(hit_v, net_v, max_single_share=share)
        extra += ["", f"> 命中侧 **{v['命中侧']}** × 净收益侧 **{v['净收益侧']}**"
                  + ("（大奖占比>30% 强制降级）" if v["强制降级"] else ""),
                  "",
                  (f"> 净收益差 {ndelta:+.2f} 元/期（**期望差恒为 0**，"
                   f"这只是抽样波动或实现缺陷，不是效应）"
                   if isinstance(ndelta, (int, float)) else ""),
                  "", f"> {v['措辞']}"]
    md = RPT.to_markdown(outs, doc, ARMS, extra=extra)
    md_path = r"E:\707\output\_arena_report.md"
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写 {md_path}（{len(md)} 字）")
    print(f"主格判决：{vd}")


if __name__ == "__main__":
    main()
