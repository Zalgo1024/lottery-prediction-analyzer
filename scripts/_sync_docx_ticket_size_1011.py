# -*- coding: utf-8 -*-
"""结题报告.docx 同步（2026-10-11）。

两件事：
① 更新 6.2.1「出号数量」为新口径（出号数量 = 最终注数；动态只剩命中率安全阀）；
② 修正历史脚本留下的**章节错位**——6.2.1/6.2.2 曾被插到 §2 开头处，根因是
   旧脚本锚点正则 `<w:p>…Heading3…</w:pPr>.*?6.3 测试与运维.*?</w:p>` 里的
   `.*?` 会跨段匹配，命中全文**第一个** Heading3（= 2.1 需求原点）而非 6.3。
   本次把这两节整体移到「6.2 写生产三道闸」之后、「6.3 测试与运维」之前，
   并按 md 顺序排成 6.2.1 → 6.2.2。

只搬移段落 + 替换文本节点，样式沿用 6.2.1/6.2.2 既有模板；改完重开 zip 复检。
"""
import re
import shutil
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")

RFONT = ('<w:rFonts w:ascii="微软雅黑" w:hAnsi="微软雅黑" w:eastAsia="微软雅黑" '
         'w:cs="微软雅黑" w:hint="eastAsia"/>')
HEAD3_TPL = ('<w:p><w:pPr><w:pStyle w:val="Heading3"/></w:pPr><w:r><w:rPr>' + RFONT +
             '<w:b/><w:color w:val="1565C0"/><w:sz w:val="20"/>'
             '<w:lang w:eastAsia="zh-CN"/></w:rPr><w:t>{t}</w:t></w:r></w:p>')
BODY_TPL = ('<w:p><w:pPr/><w:r><w:rPr>' + RFONT + '<w:lang w:eastAsia="zh-CN"/></w:rPr>'
            '<w:t xml:space="preserve">{t}</w:t></w:r></w:p>')

NEW_HEAD = "6.2.1 出号数量：固定 / 动态（2026-09-14；2026-10-11 统一为「出号数量 = 最终注数」）"
NEW_BODY = [
    "在看板「自动化运行状态」卡下方提供出号数量控件，用户可用滑轨直接设定每个彩种每期出几注，"
    "分两种模式：固定数量——每次出号恒等于设定值（数字型上限 50、乐透型上限 200，下限 1 注），"
    "且该值就是当期最终入账注数；动态数量——系统随每期评估自适应，当前实际策略为命中率安全阀："
    "命中率显著偏离理论值（|z| > 2 个标准误）时回补 +2 注，其余情形维持当前注数（硬下限 1 注）。",

    "2026-10-11 口径修正：此前生产入账前还会按 TICKET_COMPRESS_KEEP_RATIO=0.4 再压缩一刀，"
    "使滑轨设 176 实际只出 64 注，与本卡文案「每次出号几注，由你决定」矛盾。现已在生产路径"
    "改为以出号数量为准（compress_tickets(..., target_count=目标注数)，目标 ≥ 候选时不削减）"
    "——出号数量即最终注数。动态模式原来的下调信号是「上一批重叠压缩的冗余淘汰占比」；不再产生"
    "压缩冗余后该信号随之失效，故动态模式目前不再自动下调，只保留命中率安全阀。若要恢复向下"
    "调节，需引入新的冗余判据。",

    "配置持久化于 config/ticket_size.json（原子写），经 config.resolve_groups 统一下发，自动流水线、"
    "内置调度器、worker、启动恢复、CLI 与 Windows 计划任务六条路径全部生效；cli.py predict "
    "--groups N 的显式参数仍可临时绕过。下一期若已有 pending 预测，其组数与当前出号数量一致才复用，"
    "不一致则按新数量重新生成。动态调整的每次理由写入 policy 字段，看板与 "
    "GET /api/settings/ticket-size 可查。",

    "诚实边界：改注数只改变下注规模与成本，单注中奖概率恒定（期望线性性），整体 EV 仍为负；"
    "动态下调（若启用）不是预测能力提升，只是省钱与冗余控制。",
]


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def paras_of(xml: str):
    out = []
    for m in re.finditer(r"<w:p\b.*?</w:p>", xml, re.S):
        out.append((m.start(), m.end(), re.sub(r"<[^>]+>", "", m.group(0))))
    return out


def para_span(xml: str, prefix: str):
    for s, e, t in paras_of(xml):
        if t.strip().startswith(prefix):
            return s, e
    raise AssertionError(f"未找到段落：{prefix!r}")


def main():
    shutil.copy2(DOCX, DOCX.with_suffix(".docx.bak-20261011"))
    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}
    xml = contents["word/document.xml"].decode("utf-8")

    # ---- ① 定位错位块：从 6.2.2 标题段起，到 6.2.1 正文段止 ----
    s622, _ = para_span(xml, "6.2.2 消息推送")
    s621, e621 = para_span(xml, "6.2.1 出号数量")
    assert s622 < s621, "预期错位块里 6.2.2 在 6.2.1 之前"
    # 6.2.1 正文段 = 紧随 6.2.1 标题段的那一段
    after = [p for p in paras_of(xml) if p[0] >= e621]
    assert after, "6.2.1 后应有正文段"
    body621_s, body621_e, body621_t = after[0]
    assert body621_t.startswith("在看板"), f"6.2.1 正文段异常：{body621_t[:30]!r}"

    part622 = xml[s622:s621]                 # 6.2.2 标题 + 4 段正文（原样保留）
    block = xml[s622:body621_e]
    print(f"错位块 offsets {s622}-{body621_e}（{body621_e - s622} 字节）")
    xml_wo = xml[:s622] + xml[body621_e:]    # 先摘除

    # ---- ② 组装新块：新 6.2.1 + 原 6.2.2 ----
    new621 = HEAD3_TPL.format(t=esc(NEW_HEAD)) + \
        "".join(BODY_TPL.format(t=esc(p)) for p in NEW_BODY)
    new_block = new621 + part622

    # ---- ③ 插到「6.3 测试与运维」之前 ----
    s63, _ = para_span(xml_wo, "6.3 测试与运维")
    xml2 = xml_wo[:s63] + new_block + xml_wo[s63:]
    contents["word/document.xml"] = xml2.encode("utf-8")

    tmp = DOCX.with_suffix(".docx.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, contents[n])
    tmp.replace(DOCX)

    # ---- ④ 复检 ----
    chk = zipfile.ZipFile(DOCX).read("word/document.xml").decode("utf-8")
    texts = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", chk, re.S))
    print("含新标题:", NEW_HEAD in texts)
    print("含旧措辞('系统随每期评估自适应：以上一批'):",
          "系统随每期评估自适应：以上一批" in texts, "（应为 False）")
    print("含'2026-10-11 口径修正':", "2026-10-11 口径修正" in texts)
    for k in ("6.1 数据质量", "6.2 写生产三道闸", "6.2.1 出号数量",
              "6.2.2 消息推送", "6.3 测试与运维", "2.1 需求原点"):
        print(f"  offset[{k}] = {chk.find(k)}")
    import docx as _docx
    d = _docx.Document(DOCX)
    seq = [p.text.strip()[:26] for p in d.paragraphs
           if p.text.strip().startswith(("2.1 需求原点", "6.0", "6.1 ", "6.2 ", "6.2.1", "6.2.2", "6.3 ", "6.4 "))]
    print("章节顺序:", seq)


if __name__ == "__main__":
    main()
