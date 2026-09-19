# -*- coding: utf-8 -*-
"""结题报告 docx 计数同步（491/482 → 500/491）+ 漂移句修正。

铁律（同 _sync_docs_491.py）：
- 只改 word/document.xml 的 <w:t> 文本节点；styles 色值/坐标绝不触碰；
- 含 "1.484" 的节点默认跳过（业务指标 1.484× 绝不能被计数替换波及），
  唯一例外是漂移句的定向替换（旧文本不含 1.484，节点里其他位置有也要改）；
- 改完重开 zip 复检残留与保护项，再由 _export_pdf.py 重导 PDF。
"""
import re
import shutil
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
BAK = DOCX.with_suffix(".docx.bak_500")

# (old, new, 备注)——漂移句放最前（定向，允许所在节点含 1.484）
SPECIAL = [
    ("漂移监控按季度重拟合（流水线自动）",
     "漂移监控为手动命令（cli.py drift），自动重拟合尚未接入；"
     "已上线的自动训练为持续锚点走前验证 + 夜间 lightgbm 重训"),
]
GUARDED = [
    ("491 例", "500 例"),
    ("491 passed", "500 passed"),
    ("482 passed", "491 passed"),
]

xml = None
with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

pat = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
counts = {"special": 0}
guarded_counts = {old: 0 for old, _ in GUARDED}


def patch(m: re.Match) -> str:
    head, body, tail = m.group(1), m.group(2), m.group(3)
    changed = False
    for old, new in SPECIAL:
        if old in body:
            body = body.replace(old, new)
            counts["special"] += 1
            changed = True
    if "1.484" in body and not changed:
        return m.group(0)          # 保护：1.484 指标节点不动
    for old, new in GUARDED:
        if old in body:
            body = body.replace(old, new)
            guarded_counts[old] += 1
    return head + body + tail


new_xml = pat.sub(patch, xml)

# 回写（先备份）
shutil.copy2(DOCX, BAK)
tmp = DOCX.with_suffix(".docx.tmp500")
with zipfile.ZipFile(DOCX) as zin, zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
    for item in zin.infolist():
        data = zin.read(item.filename)
        if item.filename == "word/document.xml":
            data = new_xml.encode("utf-8")
        zout.writestr(item, data)
tmp.replace(DOCX)

# ---- 复检：重开 zip ----
with zipfile.ZipFile(DOCX) as z:
    check = z.read("word/document.xml").decode("utf-8")
nodes = re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", check, re.S)
full = "".join(nodes)

print("特殊替换（漂移句）节点数:", counts["special"])
for old, n in guarded_counts.items():
    print(f"  {old!r} -> 替换 {n} 个节点")
res_491 = [s for s in nodes if re.search(r"(?<![\d.])491(?!\d)", s) and "491 passed" not in s and "491 例" not in s]
res_482 = [s for s in nodes if re.search(r"(?<![\d.])482(?!\d)", s)]
print("残留 491 节点:", res_491 or "无")
print("残留 482 节点:", res_482 or "无")
print("1.484 保护项仍为:", len(re.findall(r"1\.484", full)), "处（应 7）")
print("新漂移句在 docx:", "自动重拟合尚未接入" in full)
print("备份:", BAK.name)
