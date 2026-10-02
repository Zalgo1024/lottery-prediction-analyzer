# -*- coding: utf-8 -*-
"""dump docx 指定节点区段（含上下文），用于精确重写。"""
import re
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
RANGES = [(10, 17), (44, 48), (83, 92), (111, 116), (262, 285),
          (548, 562), (600, 606), (698, 705), (719, 735), (757, 762),
          (883, 888), (934, 938), (1183, 1187), (1317, 1321),
          (1343, 1347), (1393, 1397)]

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)
out = []
for a, b in RANGES:
    out.append("=== nodes[%d:%d] ===" % (a, b))
    for i in range(a, min(b, len(nodes))):
        out.append("[%d] %r" % (i, nodes[i]))

Path(r"E:\707\logs\_reframe_ctx.txt").write_text("\n".join(out), encoding="utf-8")
print("ok")
