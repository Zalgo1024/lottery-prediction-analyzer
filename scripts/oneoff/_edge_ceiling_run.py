# -*- coding: utf-8 -*-
"""同期跨位依赖 + 可获利偏差检出力：一次跑完，输出 markdown 与 JSON。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, r"E:\707")

from credibility import cross_position as CP                      # noqa: E402
from ev import edge_ceiling as EC                                 # noqa: E402

OUT_DIR = Path(r"E:\707\output")


def main() -> None:
    print("=" * 118)
    print("一、可获利偏差：需求侧 × 检出力")
    print("=" * 118)
    rows = EC.run_all(alpha=0.05, power=0.8)
    md1 = EC.render_markdown(rows, alpha=0.05, power=0.8)
    print(md1)
    for r in rows:
        if r.get("状态") != "ok":
            continue
        w = r["最坏分摊"]
        top = sorted(w.items(), key=lambda kv: -kv[1])[:2]
        print(f"  {r['彩种']:<6} [{r['口径']}] 最坏分摊检出力={r['最坏分摊检出力']:.3f}"
              f"（等分 {r['联合检出力']:.3f}）⇒ {r['判决']}")
        print(f"         最难检测的分摊："
              + "、".join(f"{k} {v:.0%}" for k, v in top)
              + f"；缺口 {r['期数缺口']:.1f}×")

    print()
    print("=" * 118)
    print("二、同期跨位依赖（此前从未检验的备择）")
    print("=" * 118)
    cp = CP.run_all(alpha=0.05)
    md2 = CP.render_markdown(cp, alpha=0.05)
    print(md2)
    for r in cp:
        if r.get("状态") != "ok":
            continue
        top = max(r["明细"], key=lambda d: abs(d["z"]) if d.get("z") == d.get("z") else -1)
        print(f"  {r['彩种']:<6} 期数{r['期数']:>5} 位对{r['位对']:>2} "
              f"最大|z|={r['最大|z|']:.2f}（{top['位对']}） "
              f"同号率 {top['同号率']:.4f} vs 零假设 {top['零假设同号率']:.4f}")
        print(f"         对角最大增益 实测 {top['对角最大增益']:.3f}"
              f" / 上界 {top['对角增益上界']:.3f}  ⇒ {r['判决']}")

    # 依赖通道能否单独把 EV 顶正
    print()
    print("=" * 118)
    print("三、依赖通道能否单独把 EV 顶正（对角增益上界 vs G_req）")
    print("=" * 118)
    print(f"  {'彩种':<8} {'G_req':>7} {'依赖通道上界':>12} {'余量':>7}  结论")
    for c in cp:
        if c.get("状态") != "ok":
            continue
        r = next((x for x in rows if x["彩种"] == c["彩种"]), None)
        if r is None:
            continue
        ub = c["最大对角增益上界"]
        if ub != ub:
            continue
        gap = r["G_req"] / ub
        note = ("依赖通道单独不足以翻正 ⇒ 该通道可排除" if ub < r["G_req"]
                else "依赖通道单独未被排除（样本量不足，与主表一致）")
        print(f"  {c['彩种']:<8} {r['G_req']:>7.3f} {ub:>12.3f} {gap:>6.2f}×  {note}")
    print()
    print("  ★ 两条通道的上界**不可相乘**：各自都是「同时把所有取值/所有位对顶到"
          "上界」的极宽松情形，乘起来只是把松弛叠两次，没有统计含义。")
    print("     判决一律来自第一节的检出力口径，本节只回答「依赖通道单独够不够」。")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "_edge_ceiling.md").write_text(md1, encoding="utf-8")
    (OUT_DIR / "_cross_position.md").write_text(md2, encoding="utf-8")
    payload = {"模块": ["ev/edge_ceiling.py", "credibility/cross_position.py"],
               "alpha": 0.05, "power": 0.8,
               "返奖率口径": EC.RETURN_RATE_SOURCE,
               "可获利偏差": rows, "同期跨位依赖": cp}
    (OUT_DIR / "_edge_ceiling.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=1, default=float),
        encoding="utf-8")
    print("\n已写 output/_edge_ceiling.md / _cross_position.md / _edge_ceiling.json")


if __name__ == "__main__":
    main()
