# -*- coding: utf-8 -*-
"""只读探查：docx/pptx 文本节点中 406 的上下文，决定替换策略。"""
import re
import zipfile
from pathlib import Path

# 第二项为本地私有材料，不入库；换机器时按需替换路径
for p, tag in [(Path(r"E:\707\docs\结题报告.docx"), "w:t"),
               (Path(r"E:\707\docs\local_only.pptx"), "a:t")]:
    if not p.exists():
        print(p, "不存在，跳过")
        continue
    print("=" * 20, p.name)
    hits = []
    with zipfile.ZipFile(p) as z:
        for n in z.namelist():
            if not n.endswith(".xml"):
                continue
            xml = z.read(n).decode("utf-8")
            pat = "w:t" if tag == "w:t" else "a:t"
            for m in re.finditer(r"<%s(?:\s[^>]*)?>(.*?)</%s>" % (pat, pat), xml, re.S):
                body = m.group(1)
                for mm in re.finditer(r"406", body):
                    s = max(0, mm.start() - 30)
                    e = min(len(body), mm.end() + 30)
                    hits.append((n, body[s:e]))
    print("共", len(hits), "处 406：")
    for n, ctx in hits:
        print("  [%s] ...%s..." % (n, ctx.replace("\n", " ")))
