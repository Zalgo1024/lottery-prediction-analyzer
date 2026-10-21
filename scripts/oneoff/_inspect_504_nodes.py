# -*- coding: utf-8 -*-
"""检查结题报告.docx 中所有含 504 的 <w:t> 文本节点（改计数前必做，防 run 拆分误伤 1.504×）。"""
import re
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)
print("文本节点总数:", len(nodes))
print()
for i, s in enumerate(nodes):
    if "504" in s or "50 4" in s or "5 0 4" in s:
        print(f"[{i}] {s!r}")
print()
# 全文拼接口径（跨 run 也能看出）
full = "".join(nodes)
print("拼接后 504 计数（边界）:", len(re.findall(r"(?<![\d.])504(?!\d)", full)))
print("拼接后 1.504 出现:", len(re.findall(r"1\.504", full)))
print("拼接后 1.484 出现:", len(re.findall(r"1\.484", full)))
print()
# 附近上下文
for m in re.finditer(r"504", full):
    a, b = max(0, m.start() - 40), min(len(full), m.end() + 40)
    print("...", full[a:b].replace("\n", " "), "...")
