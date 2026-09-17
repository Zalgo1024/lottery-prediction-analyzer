# -*- coding: utf-8 -*-
"""pptx 文本节点内 374→406（求职材料，仅本地不入库）。"""
import re
import zipfile
from pathlib import Path

P = Path(r"E:\707\docs\求职项目简介\求职项目简介.pptx")
OLD, NEW = "374", "406"

with zipfile.ZipFile(P) as z:
    names = z.namelist()
    contents = {n: z.read(n) for n in names}

total = [0]
for n in names:
    if not n.endswith(".xml"):
        continue
    xml = contents[n].decode("utf-8")

    def _sub(m):
        body = m.group(2)
        if OLD in body:
            total[0] += body.count(OLD)
            body = body.replace(OLD, NEW)
        return m.group(1) + body + m.group(3)

    new = re.sub(r"(<a:t(?:\s[^>]*)?>)(.*?)(</a:t>)", _sub, xml, flags=re.S)
    if new != xml:
        contents[n] = new.encode("utf-8")

tmp = P.with_suffix(".pptx.tmp")
with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
    for n in names:
        z.writestr(n, contents[n])
with open(P, "wb") as f:
    f.write(tmp.read_bytes())
tmp.unlink()
print("pptx 替换", total[0], "处")

# 复检
with zipfile.ZipFile(P) as z:
    joined = ""
    for n in z.namelist():
        if n.endswith(".xml"):
            for m in re.finditer(r"<a:t(?:\s[^>]*)?>(.*?)</a:t>", z.read(n).decode("utf-8"), re.S):
                joined += m.group(1)
print("复检 374:", joined.count("374"), "406:", joined.count("406"))
