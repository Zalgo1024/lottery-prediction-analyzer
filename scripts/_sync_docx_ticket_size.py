# -*- coding: utf-8 -*-
"""结题报告.docx 同步：插入 6.2.1（出号数量固定/动态）+ 测试计数 374→406。

只在 <w:t> 文本节点内替换计数；新段落用与 6.2 完全一致的 Heading3 样式复刻标题，
正文段落复用 6.1/6.3 正文段（<w:p><w:pPr/> + 微软雅黑 run）的样式。
改完重开 zip 复检。
"""
import re
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")

RFONT = ('<w:rFonts w:ascii="微软雅黑" w:hAnsi="微软雅黑" w:eastAsia="微软雅黑" '
         'w:cs="微软雅黑" w:hint="eastAsia"/>')

HEAD3_TPL = (
    '<w:p><w:pPr><w:pStyle w:val="Heading3"/></w:pPr>'
    '<w:r><w:rPr>' + RFONT + '<w:b/><w:color w:val="1565C0"/><w:sz w:val="20"/>'
    '<w:lang w:eastAsia="zh-CN"/></w:rPr><w:t>{title}</w:t></w:r></w:p>'
)

BODY_TPL = (
    '<w:p><w:pPr/>'
    '<w:r><w:rPr>' + RFONT + '<w:lang w:eastAsia="zh-CN"/></w:rPr>'
    '<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
)

NEW_HEAD = "6.2.1 出号数量：固定 / 动态"
NEW_BODY_RAW = (
    "在看板「自动化运行状态」卡下方提供出号数量控件，用户可用滑轨直接设定每个彩种每期出几注，"
    "分两种模式：固定数量——每次出号恒等于设定值（数字型上限 50、乐透型上限 200，下限 1 注）；"
    "动态数量——系统随每期评估自适应：以上一批「质量 / 重叠压缩的冗余淘汰占比」为主信号，"
    "冗余高（≥50%）且命中率贴近理论期望（edge 稳定）时逐步走低，命中率显著偏离理论值"
    "（|z| > 2 个标准误）时回补，避免把注数压得过低而失去统计功效，硬下限 1 注。"
    "配置持久化于 config/ticket_size.json（原子写），经 config.resolve_groups 统一下发，"
    "自动流水线、内置调度器、worker、启动恢复、CLI 与 Windows 计划任务六条路径全部生效；"
    "cli.py predict --groups N 的显式参数仍可临时绕过。动态调整的每次理由写入 policy 字段，"
    "看板与 GET /api/settings/ticket-size 可查。诚实边界：改注数只改变下注规模与成本，"
    "单注中奖概率恒定（期望线性性），整体 EV 仍为负；动态下调不是预测能力提升，只是省钱与冗余控制。"
)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def patch_counts(xml: str) -> tuple[str, int]:
    """文本节点内 374→406。"""
    pat = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)
    n = 0

    def _sub(m):
        nonlocal n
        body = m.group(2)
        if "374" in body:
            n += body.count("374")
            body = body.replace("374", "406")
        return m.group(1) + body + m.group(3)

    return pat.sub(_sub, xml), n


def main():
    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}

    xml = contents["word/document.xml"].decode("utf-8")

    # 1) 文本节点计数替换
    xml, cnt = patch_counts(xml)
    print("计数替换:", cnt, "处")

    # 2) 在 "6.3 测试与运维" 标题段之前插入新节
    anchor = re.search(
        r'<w:p><w:pPr><w:pStyle w:val="Heading3"/></w:pPr>.*?6\.3 测试与运维.*?</w:p>',
        xml, re.S)
    assert anchor, "未找到 6.3 标题段（锚点失效，勿强行插入）"

    insert = (HEAD3_TPL.format(title=esc(NEW_HEAD))
              + BODY_TPL.format(text=esc(NEW_BODY_RAW)))
    xml = xml[:anchor.start()] + insert + xml[anchor.start():]
    print("已插入 6.2.1 节")

    contents["word/document.xml"] = xml.encode("utf-8")

    tmp = DOCX.with_suffix(".docx.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, contents[n])
    with open(DOCX, "wb") as f:
        f.write(tmp.read_bytes())
    tmp.unlink()

    # ---- 复检：重开 zip 数文本节点 ----
    with zipfile.ZipFile(DOCX) as z:
        chk = z.read("word/document.xml").decode("utf-8")
    texts = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", chk, re.S))
    print("复检 374:", texts.count("374"), " 406:", texts.count("406"),
          " 含标题:", NEW_HEAD in texts, " 含正文:", "出号数量控件" in texts)


if __name__ == "__main__":
    main()
