# -*- coding: utf-8 -*-
"""修复 2026-09-14.md 里被 shell 反引号吞掉的两处文件名。"""
import io

P = r"E:\707\.workbuddy\memory\2026-09-14.md"
s = io.open(P, encoding="utf-8").read()
s = s.replace(
    "-  合并行新增 `彩种注数`（行内各彩种注数构成，按注数降序）。",
    "- ev/attribution.py::attribution_global 合并行新增 `彩种注数`（行内各彩种注数构成，按注数降序）。",
)
s = s.replace(
    "- ：汇总区拆两段——先渲染「各彩种」总计表",
    "- web/templates/honest.html：汇总区拆两段——先渲染「各彩种」总计表",
)
io.open(P, "w", encoding="utf-8", newline="").write(s)
print("fixed")
