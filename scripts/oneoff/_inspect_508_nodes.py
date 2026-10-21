# -*- coding: utf-8 -*-
"""检查结题报告.docx 中所有含 508 的 <w:t> 文本节点（改计数前必做，防 run 拆分误伤 1.504×/1.484×）。"""
import re
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S)
print("文本节点总数:", len(nodes))
print()
print("=== 含 508 的节点 ===")
for i, s in enumerate(nodes):
    if "508" in s or "50 8" in s or "5 0 8" in s:
        print(f"[{i}] {s!r}")
print()
print("=== 含 499 的节点 ===")
for i, s in enumerate(nodes):
    if "499" in s or "49 9" in s:
        print(f"[{i}] {s!r}")
print()
full = "".join(nodes)
print("拼接后 508 计数（边界）:", len(re.findall(r"(?<![\d.])508(?!\d)", full)))
print("拼接后 499 计数（边界）:", len(re.findall(r"(?<![\d.])499(?!\d)", full)))
print("拼接后 1.504 出现:", len(re.findall(r"1\.504", full)))
print("拼接后 1.484 出现:", len(re.findall(r"1\.484", full)))
print()
for m in re.finditer(r"(?<![\d.])(?:508|499)(?!\d)", full):
    a, b = max(0, m.start() - 45), min(len(full), m.end() + 45)
    print("...", full[a:b].replace("\n", " "), "...")
