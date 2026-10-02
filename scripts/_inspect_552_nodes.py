# -*- coding: utf-8 -*-
"""dump docx 中含 518 / 509 / 552 / 543 的 <w:t> 节点（为 552 同步做准备）。"""
import re
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)
out = []
for i, s in enumerate(nodes):
    if re.search(r"(?<![\d.])(518|509|552|543)(?!\d)", s):
        out.append(f"[{i}] {s!r}")

full = "".join(nodes)
rx518 = re.compile(r"(?<![\d.])518(?!\d)")
rx509 = re.compile(r"(?<![\d.])509(?!\d)")
rx1504 = re.compile(r"1\.504")
rx1484 = re.compile(r"1\.484")
out.append("---")
out.append("518 独立出现: %d" % len(rx518.findall(full)))
out.append("509 独立出现: %d" % len(rx509.findall(full)))
out.append("1.504 出现: %d" % len(rx1504.findall(full)))
out.append("1.484 出现: %d" % len(rx1484.findall(full)))
out.append(f"含 '## 6' 或 '工程与可靠性' 的节点: "
           f"{[s for s in nodes if '工程与可靠性' in s]!r}")

log = Path(r"E:\707\logs\_inspect_552_out.txt")
log.write_text("\n".join(out), encoding="utf-8")
print("written", log)
