# -*- coding: utf-8 -*-
"""从 `output/_arena_run.json` 重新渲染 Markdown 报告（不重跑实验）。

为什么需要它：`_arena_run.py` 一次要 15 分钟（双色球 prod_mix 单臂 200 s），
而报告排版（表格列、措辞、注脚）会反复改。把「跑」与「渲染」拆开，
改文案就不必重跑 8 个格 × 5 个臂。

用法：
    PYTHONUTF8=1 E:/Python/python.exe -u scripts/oneoff/_arena_report_from_json.py
读：output/_arena_run.json　写：output/_arena_report.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main() -> int:
    from ev.arena import report as RPT

    src = ROOT / "output" / "_arena_run.json"
    if not src.exists():
        print(f"找不到 {src}，先跑 _arena_run.py")
        return 1
    with open(src, encoding="utf-8") as f:
        d = json.load(f)

    pr = d.get("预注册") or {}
    doc = {"版本": pr.get("版本"), "锁定日": pr.get("锁定日"), "哈希": pr.get("哈希")}
    # report.header 需要完整预注册文档（常量、格、主假设臂）；从权威文件取
    from ev import preregistration as PR
    full = PR.load()
    gen_ver = doc.get("版本")
    gen_hash = doc.get("哈希") or ""
    # ★ 用**当前**预注册文件回显版本/哈希，但显式标注数据生成时的版本差 ——
    #   事后加强守门（只降级不升级）会让文件版本高于数据版本，
    #   静默对齐会让读者以为这份数据是在新规则下跑出来的。
    drift = (gen_ver and gen_ver != full.get("版本")) or (
        gen_hash[:12] and gen_hash[:12] != str(full.get("哈希"))[:12])

    cells = d.get("格") or []
    arms = d.get("臂") or []
    main_run = next((c for c in cells if c.get("主假设")), {})
    # ★ 预注册里的「主假设臂/对比臂」不在运行结果里，必须从权威文件取，
    #   否则 verdict_cell 会退回「取最保守」⇒ 主格被误报成不可判。
    pr_cell = (full.get("格") or [{}])[0]
    main_cell = {"格": main_run.get("格"),
                 "名": main_run.get("格"),
                 "主假设臂": pr_cell.get("主假设臂"),
                 "对比臂": pr_cell.get("对比臂")}

    vd = RPT.verdict_cell(main_run, main_cell)
    extra = ["## 主格判决", ""]
    if drift:
        extra += [f"> ⚠ 数据生成于预注册 **v{gen_ver}**（哈希 `{gen_hash[:12]}…`），"
                  f"本报告按当前 **v{full.get('版本')}**（哈希 "
                  f"`{str(full.get('哈希'))[:12]}…`）回显。两版之间只做了"
                  f"**朝保守方向**的守门加强，故本报告不会比数据生成时更乐观。",
                  ""]
    extra += [f"- 格：`{main_cell.get('格')}`　主假设臂 "
              f"`{main_cell.get('主假设臂')}` vs 对比臂 `{main_cell.get('对比臂')}`",
              f"- 判决：**{vd}**"]
    if vd == "可判":
        arm_name = main_cell.get("主假设臂")
        pairs = (main_run.get("臂") or {}).get(arm_name, {}).get("配对") or {}
        hit_v = "改善" if (pairs.get("实测Δ") or 0) > 0 else "恶化"
        net_v, share, ndelta = "不可判", None, None
        for mp in (main_run.get("净收益配对") or []):
            if mp.get("臂A") == arm_name:
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

    md = RPT.to_markdown(cells, full, arms, extra=extra)
    out = ROOT / "output" / "_arena_report.md"
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"已写 {out}（{len(md)} 字）　主格判决：{vd}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
