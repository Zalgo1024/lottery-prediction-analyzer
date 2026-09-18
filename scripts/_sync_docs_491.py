"""把结题报告 .docx 里的测试计数 484 → 491、475 → 482（只改 <w:t> 文本节点）。

安全规则（本仓库 OOXML 铁律）：
  1. **只改 <w:t> 文本节点**，绝不碰属性/坐标（EMU 里天然含 "484" 之类的数字子串）。
  2. 节点文本含 "1.484"（实得提升倍数这个业务指标）时**整节点跳过**——
     否则 1.484× 会被改成 1.491×，那是把结论改坏了。
  3. 改完**重新打开 zip 复检**，确认旧值归零、1.484 一处未动。

用法：PYTHONUTF8=1 python scripts/_sync_docs_491.py
"""
from __future__ import annotations

import re
import shutil
import zipfile
from pathlib import Path

ROOT = Path(r"E:\707")
DOCX = ROOT / "docs" / "结题报告.docx"
BAK = ROOT / ".pytest_tmp" / "结题报告_before_491.docx"

OLD_NEW = (("484", "491"), ("475", "482"))
PROTECT = "1.484"          # 业务指标，绝不能被动
T_RE = re.compile(r"<w:t(?:\s[^>]*)?>(.*?)</w:t>", re.S)
NUM_RE = re.compile(r"(?<![\d.])(484|475)(?!\d)")


def _counts(text: str) -> dict:
    return {old: len(re.findall(r"(?<![\d.])" + old + r"(?!\d)", text)) for old, _ in OLD_NEW}


def main() -> int:
    if not DOCX.exists():
        print("找不到", DOCX)
        return 1

    BAK.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(DOCX, BAK)
    print(f"已备份 -> {BAK}  ({BAK.stat().st_size:,} 字节)")

    with zipfile.ZipFile(DOCX) as z:
        names = z.namelist()
        originals = {n: z.read(n) for n in names}

    xml = originals["word/document.xml"].decode("utf-8")
    before = _counts(xml)
    metric_before = xml.count(PROTECT)
    print(f"改前：484×{before['484']}  475×{before['475']}  |  1.484×{metric_before}（应保持）")

    changed = {"nodes": 0, "skipped_protected": 0}
    details = []

    def repl(m: re.Match) -> str:
        inner = m.group(1)
        if PROTECT in inner:                      # 业务指标节点：整体跳过
            changed["skipped_protected"] += 1
            return m.group(0)
        if not NUM_RE.search(inner):
            return m.group(0)
        new = inner
        for old, nv in OLD_NEW:
            new = re.sub(r"(?<![\d.])" + old + r"(?!\d)", nv, new)
        changed["nodes"] += 1
        details.append((inner[:70], new[:70]))
        return m.group(0).replace(inner, new, 1)

    new_xml = T_RE.sub(repl, xml)

    if changed["nodes"] == 0:
        print("⚠️ 没有可改的文本节点，未写回。")
        return 1

    print(f"\n改动 {changed['nodes']} 个文本节点（跳过受保护节点 {changed['skipped_protected']} 个）：")
    for a, b in details:
        print(f"  - {a!r}\n    -> {b!r}")

    with zipfile.ZipFile(DOCX, "w", zipfile.ZIP_DEFLATED) as z:
        for n in names:
            z.writestr(n, new_xml.encode("utf-8") if n == "word/document.xml" else originals[n])
    print(f"\n已写回：{DOCX}  ({DOCX.stat().st_size:,} 字节)")

    # ---- 复检：重新打开 zip 看文本节点 ----
    with zipfile.ZipFile(DOCX) as z:
        bad = z.testzip()
        x2 = z.read("word/document.xml").decode("utf-8")
    after = _counts(x2)
    metric_after = x2.count(PROTECT)
    print("\n===== 复检（重新打开 zip）=====")
    print("  zip 完整性:", bad or "OK")
    print(f"  484 残留 {after['484']}（应 0） | 475 残留 {after['475']}（应 0）")
    print(f"  1.484 指标 {metric_after} 处（应仍为 {metric_before}）")
    ok = after["484"] == 0 and after["475"] == 0 and metric_after == metric_before
    print("  结论:", "✅ 通过" if ok else "❌ 未通过")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
