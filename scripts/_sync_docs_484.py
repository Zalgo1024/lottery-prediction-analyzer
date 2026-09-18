# -*- coding: utf-8 -*-
"""结题报告.docx 同步（2026-09-18）：6.2.2 节改版为「图片推送 + 全入口覆盖」+ 测试计数 450 -> 484。

规矩（沿用本项目踩坑结论）：
  - 只改 <w:t> 文本节点，绝不碰样式/坐标，其余段落字节不变；
  - 每处目标段落先断言「恰好 1 个 <w:t>」，不满足就中止（勿强行改）；
  - 落位前备份，写完重新打开 zip 复检文本节点；
  - 计数「是否仍残留」的判定也只看文本节点。
"""
import re
import shutil
import zipfile
from pathlib import Path

DOCX = Path(r"E:\707\docs\结题报告.docx")
BAK = Path(r"E:\707\.pytest_tmp\结题报告_before_484.docx")
LOG = Path(r"E:\707\logs\_sync_docs_484.log")

RFONT = ('<w:rFonts w:ascii="微软雅黑" w:hAnsi="微软雅黑" w:eastAsia="微软雅黑" '
         'w:cs="微软雅黑" w:hint="eastAsia"/>')
NEW_PARA_TPL = (
    '<w:p><w:pPr/>'
    '<w:r><w:rPr>' + RFONT + '<w:lang w:eastAsia="zh-CN"/></w:rPr>'
    '<w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
)

TITLE_NEW = "6.2.2 消息推送：手机随时查看出号与中奖记录（2026-09-16；企业微信 09-17；图片推送 09-18）"

BODY_A = (
    "每个自动化 / 自检入口跑完都会推一次，内容以图片为主：① 每个彩种一张「号码图」—— 本期出号"
    "全部注数（超 40 注自动分列，不截断）与目标期号/滑轨状态；② 每个彩种一张「中奖图」—— 表格形式"
    "列出命中（期号｜号码｜命中球数｜奖级）；③ 每张图前各附一行短文字（彩种 · 期号 · 注数），并带上"
    "最新开奖号码行；④ 渲染或发送失败时自动降级为文字消息（受 4096 字节限制，最多展示 60 注）。"
    "用图片的原因：企业微信群机器人 markdown 单条上限 4096 字节（中文 3 字节/字），双色球/大乐透一次"
    "上百注必然被截断、末尾的诚实声明也会被砍掉；图片没有这个限制。"
)

BODY_B = (
    "触发时机：六条并行入口全部覆盖 —— 内置调度器（每晚 22:05，按开奖日触发 + 21:00–23:00 滞后补跑）、"
    "worker 事件驱动、启动恢复、Web 手动/批量、Windows 计划任务（cli.py auto 路径）；另加两处自检推送"
    "（scripts/health_check.py 项目体检、web/startup_recovery.py 启动自检）。结算段落只在当期确有新命中"
    "时才出现，而新命中只在开奖后才有 —— 夜间推送自然带中奖图、白天只推号码图。"
)

BODY_C = (
    "渠道三选一，在看板「微信推送」卡配置：企业微信群机器人（推荐 —— 建一个只有自己的群 → 群设置 → "
    "群机器人 → 添加 → 复制 Webhook 地址；免费且不限条数，且只有它支持图片消息，Webhook 地址或 key 裸串"
    "均可识别；群机器人无独立标题字段、markdown 不渲染列表，图片消息也没有 caption，故彩种名、期号、"
    "注数与诚实声明都画进图里）、PushPlus、或 Server酱（免费 5 条/天，不支持图片，自动降级为文字）。"
    "token 回显一律掩码、掩码回传不覆盖真实值；发送节流（企微每个机器人 20 条/分钟，默认相邻间隔 3.2 秒）"
    "避免一个开奖夜十余条消息被限速丢弃；每日上限默认 0 = 不限制（可设为大于 0 重新启用保护）；"
    "conftest.py 夹具把配置存储重定向到临时目录，保证测试永不写坏真身配置；未配置或网络异常一律降级为"
    "日志告警，绝不阻塞主流水线。企业微信群机器人只能单向推送，无法在群里反向下发指令，故调出号数量仍在"
    "看板完成 —— 可填「看板地址」（如 Tailscale 内网地址），推送末尾会附一条直达链接。"
)

CODE_NEW = "pytest -q   # 484 passed（全量；只跑 tests/ 为 475，另有 9 例在 scripts/ 下）"


def esc(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def para_text(p: str) -> str:
    return "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", p, re.S))


def set_para_text(p: str, new: str, rep: list, tag: str) -> str:
    """只替换段落里唯一的 <w:t> 内容；节点数不为 1 直接中止。"""
    n = len(re.findall(r"<w:t(?:\s[^>]*)?>", p))
    assert n == 1, f"{tag}: 该段落有 {n} 个 <w:t>，中止（勿强行改）"
    rep.append(f"  {tag}: 替换 1 个文本节点")
    return re.sub(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)",
                  lambda g: g.group(1) + esc(new) + g.group(3), p, count=1, flags=re.S)


def main():
    rep = []
    BAK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DOCX, BAK)
    rep.append(f"已备份 -> {BAK}")

    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        contents = {n: z.read(n) for n in names}
    xml = contents["word/document.xml"].decode("utf-8")

    spans = [(m.start(), m.end()) for m in re.finditer(r"<w:p\b.*?</w:p>", xml, re.S)]
    paras = [xml[a:b] for a, b in spans]
    i_title = next(i for i, p in enumerate(paras) if para_text(p).startswith("6.2.2 消息推送"))
    i_a = next(i for i, p in enumerate(paras) if para_text(p).startswith("自动流水线每跑完一期"))
    i_c = next(i for i, p in enumerate(paras) if para_text(p).startswith("渠道三选一"))
    rep.append(f"锚点段落: 标题#{i_title} 正文#{i_a} 渠道#{i_c}（共 {len(paras)} 段）")

    new_title = set_para_text(paras[i_title], TITLE_NEW, rep, "标题")
    new_a = set_para_text(paras[i_a], BODY_A, rep, "正文①图片/降级")
    new_b = set_para_text(paras[i_c], BODY_B, rep, "正文②触发时机")
    new_c = set_para_text(paras[i_c], BODY_C, rep, "正文③渠道/可靠性")

    # 从后往前替换，保证偏移有效；并在「触发时机」段后插入「渠道/可靠性」段
    out = xml[:spans[i_c][0]] + new_b + NEW_PARA_TPL.format(text=esc(BODY_C)) \
        + xml[spans[i_c][1]:]
    out = out[:spans[i_a][0]] + new_a + out[spans[i_a][1]:]
    out = out[:spans[i_title][0]] + new_title + out[spans[i_title][1]:]

    # 8.2 关键命令里的老命令行（用 NBSP 对齐）
    def _fix_cmd(g):
        rep.append("  8.2 命令行: 已改写为 pytest -q（484）")
        return g.group(1) + esc(CODE_NEW) + g.group(2)
    out, n_cmd = re.subn(r"(<w:t(?:\s[^>]*)?>)pytest tests/[\s\u00a0]*# 450 passed(</w:t>)",
                         _fix_cmd, out)
    if n_cmd == 0:
        rep.append("  [警告] 未找到 8.2 的 pytest 命令行节点")

    # 计数 450 -> 484（只在文本节点内）
    def _cnt(g):
        return g.group(1) + g.group(2).replace("450", "484") + g.group(3)
    out = re.sub(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", _cnt, out, flags=re.S)

    contents["word/document.xml"] = out.encode("utf-8")
    with zipfile.ZipFile(DOCX, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, contents[n])
    rep.append("已写回 docx")

    # —— 复检（重新打开 zip，只看文本节点）——
    with zipfile.ZipFile(DOCX) as z:
        chk = z.read("word/document.xml").decode("utf-8")
    texts = "".join(re.findall(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", chk, re.S))
    rep.append("")
    rep.append("[复检]")
    rep.append("  含新标题(图片推送) : %s" % ("图片推送 09-18" in texts))
    rep.append("  含『号码图』       : %s" % ("号码图" in texts))
    rep.append("  含『六条并行入口』 : %s" % ("六条并行入口" in texts))
    rep.append("  含『发送节流』     : %s" % ("发送节流" in texts))
    rep.append("  含『每日上限默认 0』: %s" % ("每日上限默认 0" in texts))
    rep.append("  旧文案残留(前10注) : %s" % ("前 10 注" in texts))
    rep.append("  旧文案残留(前5条)  : %s" % ("前 5 条" in texts))
    rep.append("  450 残留          : %d" % texts.count("450"))
    rep.append("  484 出现          : %d" % texts.count("484"))
    rep.append("  8.2 命令行        : %s" % ("pytest -q" in texts))

    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text("\n".join(rep), encoding="utf-8")
    print("\n".join(rep))


if __name__ == "__main__":
    main()
