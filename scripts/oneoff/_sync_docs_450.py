# -*- coding: utf-8 -*-
"""结题报告 docx/pptx 同步：6.2.2 节改版（增企业微信群机器人）+ 测试计数 433 -> 450。

只动文本节点（<w:t>/<a:t>），样式与其余段落字节不变；改完重开 zip 复检。
"""
import re, zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
# 本地私有材料，不入库；换机器时按需替换路径
PPTX = Path(r"E:\707\docs\local_only.pptx")

RFONT = ('<w:rFonts w:ascii="微软雅黑" w:hAnsi="微软雅黑" w:eastAsia="微软雅黑" '
         'w:cs="微软雅黑" w:hint="eastAsia"/>')
BODY_TPL = (
    '<w:p><w:pPr/>'
    '<w:r><w:rPr>' + RFONT + '<w:lang w:eastAsia="zh-CN"/></w:rPr>'
    '<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
)

NEW_TITLE = "6.2.2 消息推送：手机随时查看出号、数量与中奖记录（2026-09-16；企业微信 2026-09-17）"

BODY_1_NEW = (
    "自动流水线每跑完一期，把三块内容组装成一条消息推到手机：① 本期出号 —— 号码（前 10 注，"
    "其余看板查）与本次注数/滑轨状态（固定或动态、当前生效 N 注、动态模式附最近一次调整理由）；"
    "② 上期结算 —— 命中注数、中奖等级与明细（前 5 条）；③ 最新开奖号码行。"
)

BODY_2_NEW = (
    "渠道三选一，在看板「微信推送」卡配置：企业微信群机器人（推荐——建一个只有自己的群 → 群设置 → "
    "群机器人 → 添加 → 复制 Webhook 地址；免费且不限条数，Webhook 地址或 key 裸串均可识别）、"
    "PushPlus、或 Server酱（免费 5 条/天）。token 回显一律掩码、掩码回传不覆盖真实值；"
    "每日额度守卫防刷屏（跨天自动清零）；未配置或网络异常一律降级为日志告警，绝不阻塞主流水线。"
    "企业微信群机器人只能单向推送，无法在群里反向下发指令，故调出号数量仍在看板完成 —— "
    "可填「看板地址」（如 Tailscale 内网地址），推送末尾会附一条直达链接。"
)


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def sub_counts(text: str) -> str:
    """把文本节点里的 433 换成 450（1,181,406 这类不含 433，天然安全）。"""
    return text.replace("433", "450")


def rewrite_docx(rep: list):
    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}
    xml = contents["word/document.xml"].decode("utf-8")

    # 1) 标题改版
    old_title = "6.2.2 微信推送：手机随时查看出号与结算（2026-09-16）"
    if old_title in xml:
        xml = xml.replace(old_title, esc(NEW_TITLE))
        rep.append("标题已更新")
    elif "6.2.2 消息推送" in xml:
        rep.append("标题已是新版，跳过")
    else:
        raise AssertionError("未找到 6.2.2 标题锚点，中止（勿强行改）")

    # 2) 正文改版（旧正文整段替换）
    pat = (re.escape("自动流水线每跑完一期（结算 + 出号）") + r".*?"
           + re.escape("绝不阻塞主流水线。"))
    m = re.search(pat, xml, re.S)
    if m:
        xml = xml[:m.start()] + esc(BODY_1_NEW) + xml[m.end():]
        # 在正文段结束后插入渠道说明段
        close = xml.index("</w:p>", m.start())
        at = close + len("</w:p>")
        xml = xml[:at] + BODY_TPL.format(text=esc(BODY_2_NEW)) + xml[at:]
        rep.append("正文已改版 + 新增渠道段")
    elif "企业微信群机器人" in xml:
        rep.append("正文已是新版，跳过")
    else:
        raise AssertionError("未找到 6.2.2 正文锚点，中止")

    # 3) 计数（只在文本节点内替换）
    before = len(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", xml, re.S))
    xml = re.sub(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)",
                 lambda g: g.group(1) + sub_counts(g.group(2)) + g.group(3),
                 xml, flags=re.S)
    rep.append("文本节点 %d 个已处理" % before)
    contents["word/document.xml"] = xml.encode("utf-8")

    with zipfile.ZipFile(DOCX, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, contents[n])


def rewrite_pptx(rep: list):
    with zipfile.ZipFile(PPTX) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}
    changed = 0
    for n in names:
        if not re.match(r"ppt/slides/slide\d+\.xml$", n):
            continue
        xml = contents[n].decode("utf-8")
        if "433" not in xml:
            continue
        new = re.sub(r"(<a:t>)(.*?)(</a:t>)",
                     lambda g: g.group(1) + sub_counts(g.group(2)) + g.group(3),
                     xml, flags=re.S)
        # 只检查「文本节点内」是否还有残留。
        # 注意：pptx 的 EMU 坐标（cx="3543300" 等）天然含 "433" 子串，绝不能动。
        leftovers = [t for t in re.findall(r"<a:t>(.*?)</a:t>", new, re.S) if "433" in t]
        if leftovers:
            rep.append("  [警告] %s 的文本节点内仍有 433（可能被拆成多个 run），未写入" % n)
            continue
        if new != xml:
            contents[n] = new.encode("utf-8")
            changed += 1
    rep.append("pptx 改动幻灯片 %d 张" % changed)
    with zipfile.ZipFile(PPTX, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, contents[n])


def verify(rep: list):
    with zipfile.ZipFile(DOCX) as z:
        chk = z.read("word/document.xml").decode("utf-8")
    texts = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", chk, re.S))
    rep.append("")
    rep.append("[DOCX 复检]")
    rep.append("  含新标题      : %s" % ("6.2.2 消息推送" in texts))
    rep.append("  含企业微信群机器人: %s" % ("企业微信群机器人" in texts))
    rep.append("  433 残留      : %d" % texts.count("433"))
    rep.append("  450 出现      : %d" % texts.count("450"))
    rep.append("  1,181,406 完整: %s" % ("1,181,406" in texts))
    with zipfile.ZipFile(PPTX) as z:
        all_t = ""
        for n in z.namelist():
            if re.match(r"ppt/slides/slide\d+\.xml$", n):
                all_t += "".join(re.findall(r"<a:t>(.*?)</a:t>", z.read(n).decode("utf-8"), re.S))
    rep.append("[PPTX 复检]")
    rep.append("  433 残留      : %d" % all_t.count("433"))
    rep.append("  450 出现      : %d" % all_t.count("450"))


if __name__ == "__main__":
    rep = []
    rewrite_docx(rep)
    rewrite_pptx(rep)
    verify(rep)
    out = "\n".join(rep)
    open(r"E:\707\logs\_sync_docs_450.log", "w", encoding="utf-8").write(out)
    print("done")
