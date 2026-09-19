# -*- coding: utf-8 -*-
"""结题报告 docx 计数同步·第二轮：处理 run 拆分造成的残留（定向、逐节点白名单）。"""
import re
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")

# 白名单替换：old_node_text -> new_node_text（精确匹配整个文本节点内容）
NODE_MAP = {
    " pytest 491 ": " pytest 500 ",
    "491 ": "500 ",
    "491": "500 ",
    # 大代码行里的 "只跑 tests/ 为 482"（整节点替换风险大，改用子串规则单独处理）
}
SUBSTR = [("为 482", "为 491")]

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

pat = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
hits = {"nodes": 0, "substr": 0}


def patch(m: re.Match) -> str:
    head, body, tail = m.group(1), m.group(2), m.group(3)
    if body in NODE_MAP:
        hits["nodes"] += 1
        return head + NODE_MAP[body] + tail
    for old, new in SUBSTR:
        if old in body and "1.484" not in body:
            body = body.replace(old, new)
            hits["substr"] += 1
    return head + body + tail


new_xml = pat.sub(patch, xml)
tmp = DOCX.with_suffix(".docx.tmp500b")
with zipfile.ZipFile(DOCX) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "word/document.xml":
            data = new_xml.encode("utf-8")
        zout.writestr(item, data)
tmp.replace(DOCX)

# 复检
with zipfile.ZipFile(DOCX) as z:
    check = z.read("word/document.xml").decode("utf-8")
nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", check, re.S)
full = "".join(nodes)
bad = [s for s in nodes if re.search(r"(?<![\d.])(491|482)(?!\d)", s)]
print("节点替换数:", hits["nodes"], "| 子串替换数:", hits["substr"])
print("残留 491/482 节点:", bad or "无 ✅")
print("500 出现次数:", len(re.findall(r"(?<![\d.])500(?!\d)", full)))
print("491 出现次数(tests口径):", len(re.findall(r"(?<![\d.])491(?!\d)", full)))
print("1.484 保护项:", len(re.findall(r"1\.484", full)), "处（应 7）")
