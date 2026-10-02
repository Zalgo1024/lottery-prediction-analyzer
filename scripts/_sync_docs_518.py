# -*- coding: utf-8 -*-
"""结题报告 docx 计数同步：508 → 518（节点白名单 + 1.504×/1.484× 保护）。

规则：
1) 只改 <w:t> 文本节点，绝不全文替换（EMU 坐标含数字子串）。
2) 节点文本整体等于某已知计数节点时，整节点替换（NODE_EXACT）。
3) 其余走受限子串规则（SUBSTR），且节点含 1.504 / 1.484 时直接跳过。
4) 改完重开 zip 复检：残留 508 应为 0，518 应 6 处，1.504× 应仍为 2 处，1.484× 应仍为 7 处。
"""
import re
import shutil
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
TMP = DOCX.with_suffix(".docx.tmp518")

# --- 整节点替换（inspection 脚本确认过的 6 个计数节点）---------------------
NODE_EXACT = {
    " pytest 508 ": " pytest 518 ",
    "508 ": "518 ",
    "pytest 508 例": "pytest 518 例",
    "pytest -q   # 508 passed（全量；只跑 tests/ 为 499，另有 9 例在 scripts/ 下）":
        "pytest -q   # 518 passed（全量；只跑 tests/ 为 509，另有 9 例在 scripts/ 下）",
}
# --- 受限子串替换 ---------------------------------------------------------
SUBSTR = [
    ("508 passed", "518 passed"),
    ("为 499", "为 509"),
]
GUARD = ("1.504", "1.484")  # 命中即跳过该节点

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
except OSError as e:  # 目标被预览句柄占用时回退
    print("replace 失败（", e, "）→ 回退 copyfile")
    shutil.copyfile(TMP, DOCX)
    try:
        TMP.unlink()
    except OSError:
        pass

# --- 复检 -----------------------------------------------------------------
with zipfile.ZipFile(DOCX) as z:
    check = z.read("word/document.xml").decode("utf-8")
nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", check, re.S)
full = "".join(nodes)

print("整节点替换:", hits["node"], "| 子串替换:", hits["substr"])
bad = [s for s in nodes if re.search(r"(?<![\d.])508(?!\d)", s)]
print("残留 508 节点:", bad or "无 ✅")
print("518 出现次数:", len(re.findall(r"(?<![\d.])518(?!\d)", full)), "（应 6）")
print("1.504× 保护:", len(re.findall(r"1\.504", full)), "处（应 2）")
print("1.484× 保护:", len(re.findall(r"1\.484", full)), "处（应 7）")
print("残留 499:", len(re.findall(r"(?<![\d.])499(?!\d)", full)), "（应 0）")
print("509 出现次数:", len(re.findall(r"(?<![\d.])509(?!\d)", full)), "（应 1）")
print("zip 完整性:", "OK" if len(zipfile.ZipFile(DOCX).namelist()) > 0 else "FAIL")
