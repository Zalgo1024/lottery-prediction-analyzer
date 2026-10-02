# -*- coding: utf-8 -*-
"""把 6.3 测试覆盖缩写版补齐本轮新增主题（节点级精确替换，保护 1.504/1.484）。

docx 的 6.3 是缩写版（节点 1262-1264 跨 run）：
  [1262] '封顶）、批量与单注口径对齐、期号口径、写保护回归、'
  [1263] 'NO-GO '
  [1264] '诚实路径等。改代码后经看门狗热重启（...），共享'
在 [1264] 的「诚实路径」后、『等。』前插入新增覆盖主题。
"""
import re
import shutil
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
TMP = DOCX.with_suffix(".docx.tmp63")

OLD = "诚实路径等。改代码后经看门狗热重启"
NEW = ("诚实路径、三口径 EV 对照/冷门口径 rollover/奖池门槛/Kelly 纪律仓位、"
       "策略历史实测命中统计、休市期全链路感知等。改代码后经看门狗热重启")

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

pat = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
hits = {"n": 0}


def patch(m: re.Match) -> str:
    head, body, tail = m.group(1), m.group(2), m.group(3)
    if OLD in body:
        assert "1.504" not in body and "1.484" not in body
        hits["n"] += 1
        return head + body.replace(OLD, NEW) + tail
    return m.group(0)


new_xml = pat.sub(patch, xml)
assert hits["n"] == 1, f"预期命中 1 个节点，实际 {hits['n']}"

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


print("替换节点数:", hits["n"])
print("594 出现:", rx(r"(?<![\d.])594(?!\d)"), "（应 6）")
print("585 出现:", rx(r"(?<![\d.])585(?!\d)"), "（应 1）")
print("新增主题在文:", full.count("休市期全链路感知"), full.count("策略历史实测命中统计"),
      full.count("三口径 EV 对照"))
print("1.504× 保护:", rx(r"1\.504"), "（应 2）")
print("1.484× 保护:", rx(r"1\.484"), "（应 11）")
print("zip 完整性:", "OK" if len(zipfile.ZipFile(DOCX).namelist()) > 0 else "FAIL")
