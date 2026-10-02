# -*- coding: utf-8 -*-
"""dump docx 中含 561 / 552 / 543 / 594 / 585 的 <w:t> 节点（为 594 同步做准备）。"""
import re
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)
out = []
for i, s in enumerate(nodes):
    if re.search(r"(?<![\d.])(561|552|543|594|585)(?!\d)", s):
        out.append("[%d] %r" % (i, s))

full = "".join(nodes)


def rx(p):
    return len(re.findall(p, full))


out.append("---")
out.append("561 独立出现: %d" % rx(r"(?<![\d.])561(?!\d)"))
out.append("552 独立出现: %d" % rx(r"(?<![\d.])552(?!\d)"))
out.append("543 独立出现: %d" % rx(r"(?<![\d.])543(?!\d)"))
out.append("1.504 出现: %d" % rx(r"1\.504"))
out.append("1.484 出现: %d" % rx(r"1\.484"))

log = Path(r"E:\707\logs\_inspect_594_out.txt")
log.write_text("\n".join(out), encoding="utf-8")
print("written", log)
