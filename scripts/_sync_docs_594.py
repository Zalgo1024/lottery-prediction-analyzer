# -*- coding: utf-8 -*-
"""结题报告 docx 计数同步：561 → 594（节点白名单 + 1.504×/1.484× 保护）。

沿用 561 同步的节点面（6 个计数节点，其中 1 个复合节点内含 tests/ 口径）。
规则：只改 <w:t> 文本节点；节点含 1.504 / 1.484 时跳过；改完重开 zip 复检。
"""
import re
import shutil
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
TMP = DOCX.with_suffix(".docx.tmp594")

NODE_EXACT = {
    " pytest 561 ": " pytest 594 ",
    "561 ": "594 ",
    "pytest 561 例": "pytest 594 例",
    "pytest -q   # 561 passed（全量；只跑 tests/ 为 552，另有 9 例在 scripts/ 下）":
        "pytest -q   # 594 passed（全量；只跑 tests/ 为 585，另有 9 例在 scripts/ 下）",
}
SUBSTR = [
    ("561 passed", "594 passed"),
    ("为 552", "为 585"),
]
GUARD = ("1.504", "1.484")

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

pat = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
hits = {"node": 0, "substr": 0}


def patch(m: re.Match) -> str:
    head, body, tail = m.group(1), m.group(2), m.group(3)
    if body in NODE_EXACT:
        hits["node"] += 1
        return head + NODE_EXACT[body] + tail
    if any(g in body for g in GUARD):
        return m.group(0)
    orig = body
    for old, new in SUBSTR:
        if old in body:
            body = body.replace(old, new)
    if body != orig:
        hits["substr"] += 1
    return head + body + tail


new_xml = pat.sub(patch, xml)

with zipfile.ZipFile(DOCX) as zin, zipfile.ZipFile(TMP, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "word/document.xml":
            data = new_xml.encode("utf-8")
        zout.writestr(item, data)

try:
    TMP.replace(DOCX)
except OSError as e:
    print("replace 失败（", e, "）→ 回退 copyfile")
    shutil.copyfile(TMP, DOCX)
    try:
        TMP.unlink()
    except OSError:
        pass

# --- 复检 ---
with zipfile.ZipFile(DOCX) as z:
    check = z.read("word/document.xml").decode("utf-8")
nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", check, re.S)
full = "".join(nodes)


def rx(p):
    return len(re.findall(p, full))


print("整节点替换:", hits["node"], "| 子串替换:", hits["substr"])
print("残留 561:", rx(r"(?<![\d.])561(?!\d)"), "（应 0）")
print("残留 552:", rx(r"(?<![\d.])552(?!\d)"), "（应 0）")
print("594 出现次数:", rx(r"(?<![\d.])594(?!\d)"), "（应 6）")
print("585 出现次数:", rx(r"(?<![\d.])585(?!\d)"), "（应 1）")
print("1.504× 保护:", rx(r"1\.504"), "（应 2）")
print("1.484× 保护:", rx(r"1\.484"), "（应 11，含 5.5 节 4 处）")
print("zip 完整性:", "OK" if len(zipfile.ZipFile(DOCX).namelist()) > 0 else "FAIL")
