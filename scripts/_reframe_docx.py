# -*- coding: utf-8 -*-
"""结题报告 docx「去证伪腔调」重写：节点精确替换 + 子串替换，保护 1.504/1.484。

规则：只改 <w:t> 文本节点；节点含 GUARD 数字时整节点跳过；改完重开 zip 复检。
"""
import re
import shutil
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
TMP = DOCX.with_suffix(".docx.tmpreframe")

GUARD = ("1.504", "1.484")

NODE_EXACT = {
    "时间序列证伪分析框架": "时间序列分析框架",
    "带严格证伪机制的时间序列分析框架（彩票作为示例场景）":
        "带严格统计检验的时间序列分析框架（彩票作为示例场景）",
    "4. 证伪流水线（技术核心 ① ）": "4. 假设检验流水线（技术核心 ① ）",
    "4. 证伪流水线（技术核心 ①）": "4. 假设检验流水线（技术核心 ①）",
    "造一台测谎仪": "建立一套客观的检验流程",
    "把项目想成一台测谎仪：要证明它好使，最好的办法不是去测一个诚实的人，而是去测一个":
        "把项目看成一套检验流程：要证明它好使，最好的办法是拿一个",
    "必定在说谎": "不含真实信号",
    "的对象。彩票就是那个对象": "的对象来试。彩票就是这样的对象",
    "规则完全公开、结果纯随机、没有任何内部信息可作弊。如果一台检测工具能在这种场景里干脆地判出":
        "规则完全公开、结果纯随机、没有任何内部信息可利用。如果一套检验流程能在这种场景里干脆地判出",
    "打假": "假设检验",
    "以证伪为核心目标": "以严格统计检验为核心",
    "① 证伪结论：": "① 检验结论：",
    "、存活": "，结果",
    " 0——": "全部留存、可复现。",
    "彩票开奖不可预测，且本框架能严格证明这一点。": "",
    "2.3 设计哲学：先证伪，再建设": "2.3 设计哲学：先检验，再建设",
    "2. 可证伪优先。": "2. 可检验优先。",
    "证伪裁判层": "检验裁判层",
    "诚实看板；": "数据体检看板；",
    "4.2 实战结果：25 条假设，存活 0": "4.2 实战结果：25 条假设的检验",
    "25/25 rejected，存活 0": "25/25 rejected",
    "③ 诚实边界：": "③ 边界声明：",
    "一个诚实的结果": "一份检验记录",
    "证伪闸门总览": "可信度层总览",
}

SUBSTR = [
    ("的想法全部被证伪；顺带证明", "的方法逐一检验；同时验证"),
    ("章给出证伪流水线的设计与", "章给出假设检验流水线的设计与"),
    ("项目自身证伪：", "项目自身检验："),
    ("证伪流水线五道工序", "假设检验流水线五道工序"),
    ("概率侧被证伪后", "概率侧无提升空间后"),
    ("（看板 honest 页依赖）", "（看板 audit 页依赖）"),
    ("证伪流水线抽象为", "检验流水线抽象为"),
]

with zipfile.ZipFile(DOCX) as z:
    xml = z.read("word/document.xml").decode("utf-8")

pat = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
hits = {"node": 0, "substr": 0, "guard": 0}
unmatched = []


def patch(m: re.Match) -> str:
    head, body, tail = m.group(1), m.group(2), m.group(3)
    if any(g in body for g in GUARD):
        hits["guard"] += 1
        return m.group(0)
    if body in NODE_EXACT:
        hits["node"] += 1
        return head + NODE_EXACT[body] + tail
    orig = body
    for old, new in SUBSTR:
        if old in body:
            body = body.replace(old, new)
    if body != orig:
        hits["substr"] += 1
    elif "证伪" in orig or "honest" in orig:
        unmatched.append(orig[:80])
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


print("整节点替换:", hits["node"], "| 子串替换:", hits["substr"], "| GUARD 跳过:", hits["guard"])
print("残留「证伪」:", full.count("证伪"), "（应 0）")
print("残留 诚实看板:", full.count("诚实看板"), "（应 0）| 残留 honest:", full.count("honest"), "（应 0）")
print("残留 打假/测谎:", full.count("打假"), full.count("测谎"), "（应 0 0）")
print("594 出现:", rx(r"(?<![\d.])594(?!\d)"), "（应 6）| 585:", rx(r"(?<![\d.])585(?!\d)"), "（应 1）")
print("1.504× 保护:", rx(r"1\.504"), "（应 2）| 1.484× 保护:", rx(r"1\.484"), "（应 11）")
print("数据体检看板:", full.count("数据体检看板"), "| 假设检验流水线:", full.count("假设检验流水线"))
if unmatched:
    print("!! 未匹配的残留节点:", unmatched)
print("zip 完整性:", "OK" if len(zipfile.ZipFile(DOCX).namelist()) > 0 else "FAIL")
