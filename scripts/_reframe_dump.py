# -*- coding: utf-8 -*-
"""dump docx 中含「证伪/诚实看板/打假/测谎/存活/honest」的 <w:t> 节点，为重写做准备。"""
import re
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
KEYS = ["证伪", "诚实看板", "打假", "测谎", "存活", "honest", "时间序列", "拆穿"]

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)
out = []
for i, s in enumerate(nodes):
    if any(k in s for k in KEYS):
        out.append("[%d] %r" % (i, s))

full = "".join(nodes)
out.append("--- 合并全文出现次数 ---")
for k in KEYS:
    out.append("%s: %d" % (k, full.count(k)))

log = Path(r"E:\707\logs\_reframe_dump.txt")
log.write_text("\n".join(out), encoding="utf-8")
print("written", log, "| nodes:", len(nodes))
