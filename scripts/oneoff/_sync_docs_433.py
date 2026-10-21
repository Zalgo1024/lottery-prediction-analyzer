# -*- coding: utf-8 -*-
"""docx/pptx 文本节点内测试计数 406→433（微信推送 +27 例）。

保护规则：负向断言 (?<![0-9,])406(?![0-9])，中奖概率 1/1,181,406 中的 406
（前缀为逗号）不会被替换。只在 <w:t>/<a:t> 文本节点内操作，改完重开 zip 复检。
"""
import re
import zipfile
from pathlib import Path

PAT = re.compile(r"(?<![0-9,])406(?![0-9])")

JOBS = [
    (Path(r"E:\707\docs\结题报告.docx"), r"<w:t(?:\s[^>]*)?>", r"</w:t>"),
    # 本地私有材料，不入库；换机器时按需替换路径
    (Path(r"E:\707\docs\local_only.pptx"), r"<a:t(?:\s[^>]*)?>", r"</a:t>"),
]

for path, open_tag, close_tag in JOBS:
    if not path.exists():
        print(path.name, "不存在，跳过")
        continue
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}

    full = re.compile(r"(%s)(.*?)(%s)" % (open_tag, close_tag), re.S)
    total = [0]

    for n in names:
        if not n.endswith(".xml"):
            continue
        xml = contents[n].decode("utf-8")

        def _sub(m):
            body = m.group(2)
            new, k = PAT.subn("433", body)
            if k:
                total[0] += k
                return m.group(1) + new + m.group(3)
            return m.group(0)

        new_xml = full.sub(_sub, xml)
        if new_xml != xml:
            contents[n] = new_xml.encode("utf-8")

    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, contents[n])
    with open(path, "wb") as f:
        f.write(tmp.read_bytes())
    tmp.unlink()
    print(path.name, "替换", total[0], "处")

    # 复检：重开 zip 统计文本节点
    tag = "w:t" if "w:t" in open_tag else "a:t"
    with zipfile.ZipFile(path) as z:
        joined = ""
        for n in z.namelist():
            if n.endswith(".xml"):
                for m in re.finditer(r"<%s(?:\s[^>]*)?>(.*?)</%s>" % (tag, tag),
                                     z.read(n).decode("utf-8"), re.S):
                    joined += m.group(1)
    print("  复检 406:", joined.count("406"), " 433:", joined.count("433"),
          " 概率保护 1,181,406:", "1,181,406" in joined)
