# -*- coding: utf-8 -*-
"""核验 PDF 内文本口径：应出现 518，不出现独立 508/499，且保留 1.504× / 1.484×。"""
import re

PDF = r"E:\707\docs\结题报告.pdf"

try:
    from pypdf import PdfReader
except ImportError:
    from PyPDF2 import PdfReader

r = PdfReader(PDF)
txt = "\n".join((p.extract_text() or "") for p in r.pages)
flat = txt.replace("\u3000", " ")

print("页数:", len(r.pages))
print("518 出现:", len(re.findall(r"(?<![\d.])518(?!\d)", flat)))
print("509 出现:", len(re.findall(r"(?<![\d.])509(?!\d)", flat)))
print("独立 508 出现:", len(re.findall(r"(?<![\d.])508(?!\d)", flat)), "（应 0）")
print("独立 499 出现:", len(re.findall(r"(?<![\d.])499(?!\d)", flat)), "（应 0）")
print("独立 504 出现:", len(re.findall(r"(?<![\d.])504\s*(?:例|passed|pytest)", flat)), "（应 0）")
print("1.504 出现:", len(re.findall(r"1\.504", flat)), "（应 2）")
print("1.484 出现:", len(re.findall(r"1\.484", flat)), "（应 7）")
print()
for m in re.finditer(r"(?<![\d.])(?:518|509)(?!\d)", flat):
    a, b = max(0, m.start() - 50), min(len(flat), m.end() + 50)
    print("  ...", flat[a:b].replace("\n", " "), "...")
