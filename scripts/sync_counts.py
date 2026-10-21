# -*- coding: utf-8 -*-
"""同步「测试用例数」到所有需要手工维护的地方 —— 一条命令替代五处手改。

为什么值得单独写（2026-10-21）：

加完测试后，这个数字散落在 4 个载体、至少 8 处文本里：
  README 徽章 / README 命令注释 / README 说明段 / 结题报告 md（5 处）/ 结题报告 docx / PDF。
每加一批测试都要手工数一遍再逐个改，改漏一处就变成"文档在撒谎"。
而这个数字恰恰是外人判断项目质量的第一个信号 —— 它错了比没有更糟。

做法：
  1. 真源 = `pytest --collect-only -q`（两次：全量 / tests/ 子集），不靠人记；
  2. 对 md / docx 的文本节点做**带上下文的正则替换**（不做裸数字全局替换，
     否则正文里的"915"以外的数字会被误伤）；
  3. 每条规则都要求至少命中一次，某条零命中 = 文档表述变了或脚本失效，
     直接报错退出（防止"跑成功但什么都没改"的静默失真）。

用法：
    E:/Python/python.exe scripts/sync_counts.py --check    # 只检查，不一致返回 1
    E:/Python/python.exe scripts/sync_counts.py            # 改 md + docx
    E:/Python/python.exe scripts/sync_counts.py --pdf      # 再重导 PDF（较慢）
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
README = BASE / "README.md"
REPORT_MD = BASE / "docs" / "结题报告.md"
REPORT_DOCX = BASE / "docs" / "结题报告.docx"
EXPORT_PDF = BASE / "scripts" / "export_pdf.py"

# (正则, 归类, 适用载体集合) —— 正则需要恰好三个组：前缀 / 数字 / 后缀
# 载体必须分开标注：README 有徽章 URL、md 报告有"测试：pytest N 例通过"、
# docx 因为 Word 拆 run 会出现"pytest N 例全量通过"这类 md 里没有的句式。
# 混在一起的后果是"零命中告警"变成常态，而常态告警等于没有告警。
_ALL = ("readme", "report", "docx")
RULES: list[tuple[str, str, tuple[str, ...]]] = [
    (r"(pytest-)(\d+)(%20passed)", "total", ("readme",)),
    (r"(全量 )(\d+)( 例)", "total", ("readme",)),
    (r"(# )(\d+)( passed（全量)", "total", _ALL),
    (r"(测试：pytest )(\d+)( 例通过)", "total", ("report",)),
    (r"(pytest )(\d+)( 例全量通过)", "total", ("docx",)),
    (r"(pytest )(\d+)( 例通过)", "total", ("docx",)),
    (r"()(\d+)(\s\spytest\s测试全量通过)", "total", ("docx",)),
    (r"(、)(\d+)( 例单元测试)", "total", ("report", "docx")),
    (r"(\*\*pytest )(\d+)( 例\*\*)", "total", ("report",)),
    (r"(pytest（)(\d+)( 例）)", "total", ("report", "docx")),
    (r"(全量运行：)(\d+)( 例）)", "total", ("report",)),
    (r"(pytest tests/\s+# )(\d+)( passed)", "tests_only", ("readme", "report")),
    (r"(只跑 tests/ 为 )(\d+)()", "tests_only", ("docx",)),
    (r"(另有 )(\d+)( 例在 scripts/ 下)", "scripts_only", _ALL),
]

_FILE_KIND = {"README.md": "readme", "结题报告.md": "report", "结题报告.docx": "docx"}


def _rules_for(filename: str):
    kind = _FILE_KIND.get(filename, "")
    return [(p, k) for p, k, where in RULES if kind in where]


def _collect(args: list[str]) -> int:
    """跑 pytest --collect-only 取用例总数。"""
    cmd = [sys.executable, "-m", "pytest", "--collect-only", "-q",
           "-p", "no:cacheprovider", *args]
    env = {**__import__("os").environ, "PYTHONUTF8": "1"}
    try:
        p = subprocess.run(cmd, cwd=str(BASE), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=600, env=env)
    except Exception as e:
        raise SystemExit(f"❌ 收集用例失败: {e}")
    m = re.search(r"(\d+)\s+tests?\s+collected", p.stdout or "")
    if not m:
        tail = (p.stdout or "")[-500:] + (p.stderr or "")[-500:]
        raise SystemExit(f"❌ 无法解析用例数，pytest 输出尾部:\n{tail}")
    return int(m.group(1))


def _values(total: int, tests_only: int) -> dict[str, int]:
    return {"total": total, "tests_only": tests_only,
            "scripts_only": max(total - tests_only, 0)}


def _apply(text: str, vals: dict[str, int], *, filename: str = "",
           report: list | None = None) -> str:
    for pat, kind in _rules_for(filename):
        rx = re.compile(pat)
        val = str(vals[kind])

        def _sub(m, _val=val, _kind=kind, _pat=pat):
            if report is not None and m.group(2) != _val:
                report.append((_pat, m.group(2), _val))
            return m.group(1) + _val + m.group(3)

        text = rx.sub(_sub, text)
    return text


def _zero_hit_rules(text: str, filename: str) -> list[str]:
    return [pat for pat, _ in _rules_for(filename) if not re.search(pat, text)]


# ------------------------------------------------------------ md
def _sync_md(path: Path, vals, *, check: bool, report: list) -> bool:
    old = path.read_text(encoding="utf-8")
    new = _apply(old, vals, filename=path.name, report=report)
    if check:
        return old == new
    if old != new:
        path.write_text(new, encoding="utf-8")
    return True


# ------------------------------------------------------------ docx
_T = re.compile(r"(<w:t(?:\s[^>]*)?>)(.*?)(</w:t>)", re.S)


def _apply_cross(nodes: list[str], vals: dict[str, int], filename: str,
                 report: list | None = None) -> tuple[list[str], int]:
    """在「节点拼接后的全文」上做替换，再把结果映射回各个 `<w:t>` 节点。

    为什么不能逐节点替换（2026-10-21 实踩）：
      Word 会把一句话拆成多个 run —— `pytest 915 例通过` 在 XML 里可能是
      `pytest 915 ` / `例通过` 两个节点，甚至 `）` / `915 ` / ` ` / `pytest ` /
      `测试全量通过` 五个。逐节点匹配时**后缀根本不在同一节点内**，正则全部落空，
      脚本会显示"改成功"而实际一个字没改 —— 正是它在注释里声称要防的那种静默失真。
      第一版就是这样漏掉了 docx 里 5 处 915 中的 4 处。

    返回 (新节点列表, 变动节点数)。跨节点的替换文本落在起点所在节点，
    其余节点删掉被覆盖的字符（都是纯数字替换，不涉及样式重建）。
    """
    joined = "".join(nodes)
    raw: list[tuple[int, int, str, str, str]] = []
    for pat, kind in _rules_for(filename):
        val = str(vals[kind])
        for m in re.finditer(pat, joined):
            raw.append((m.start(), m.end(), m.group(1) + val + m.group(3),
                        m.group(2), val))
    raw.sort()
    spans: list[tuple[int, int, str, str, str]] = []
    for item in raw:
        if spans and item[0] < spans[-1][1]:      # 重叠：先到先得
            continue
        spans.append(item)
    if not spans:
        return nodes, 0

    if report is not None:
        for s, _e, _rep, got, want in spans:
            if got != want:
                report.append((f"{filename}@{s}", got, want))

    starts, off = [], 0
    for t in nodes:
        starts.append(off)
        off += len(t)

    new_nodes, changed = [], 0
    for i, t in enumerate(nodes):
        s0, e0 = starts[i], starts[i] + len(t)
        cur, parts = 0, []
        for s, e, rep, _g, _w in spans:
            if e <= s0 or s >= e0:
                continue
            ls = max(s, s0) - s0
            le = min(e, e0) - s0
            if s >= s0:          # 替换起点在本节点 → 整段替换文本落在本节点
                parts.append(t[cur:ls])
                parts.append(rep)
                cur = le
            else:                # 替换从更早节点开始 → 本节点只删被覆盖部分
                parts.append(t[cur:max(cur, ls)])
                cur = max(cur, le)
        parts.append(t[cur:])
        nt = "".join(parts)
        # 只在新文本确实不同时计数：否则"值已经是目标值"也会算成变更，
        # --check 永远不可能通过（幂等性 bug，2026-10-21 被单测抓出）。
        if nt != t:
            changed += 1
        new_nodes.append(nt)

    # 幂等短路：全文内容没变就必须原样返回。
    # 否则"值已经是目标值"时，替换文本会被塞进第一个相交节点、
    # 其余节点被清空 —— 内容一样但节点结构被重排，且 changed>0 ⇒
    # `--check` 永远不可能通过（2026-10-21 被单测抓出）。
    if "".join(new_nodes) == joined:
        return nodes, 0
    return new_nodes, changed


def _sync_docx(path: Path, vals, *, check: bool, report: list) -> bool:
    if not path.exists():
        report.append(("docx", "缺失", str(path)))
        return False
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        blobs = {n: z.read(n) for n in names}
    xml = blobs["word/document.xml"].decode("utf-8")

    ms = list(_T.finditer(xml))
    n_before = len(ms)
    olds = [m.group(2) for m in ms]
    news, changed = _apply_cross(olds, vals, path.name, report)

    out, last = [], 0
    for m, nt in zip(ms, news):
        out.append(xml[last:m.start()])
        out.append(m.group(1) + nt + m.group(3))
        last = m.end()
    out.append(xml[last:])
    new_xml = "".join(out)

    # 纪律：节点数必须不变（只改文本内部，不碰标签/样式）
    if len(_T.findall(new_xml)) != n_before:
        report.append(("docx", "节点数变化，已终止", f"{n_before}"))
        return False
    if check:
        return changed == 0

    if changed:
        bak = path.with_suffix(".docx.bak")
        shutil.copy2(path, bak)
        blobs["word/document.xml"] = new_xml.encode("utf-8")
        tmp = path.with_suffix(".docx.tmp")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for n in names:
                z.writestr(n, blobs[n])
        tmp.replace(path)
        print(f"  docx：{changed} 个文本节点已更新（备份 {bak.name}）")
    return True


# ------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不修改")
    ap.add_argument("--pdf", action="store_true", help="docx 改完顺便重导 PDF")
    ap.add_argument("--skip-collect", action="store_true",
                    help="跳过 pytest 收集（复用 lock 场景下手工传数）")
    args = ap.parse_args()

    if args.skip_collect:
        raise SystemExit("❌ --skip-collect 需要显式用例数，本脚本不提供该捷径（避免手抄出错）")

    print("· 收集用例数（全量）...")
    total = _collect([])
    print("· 收集用例数（tests/）...")
    tests_only = _collect(["tests/"])
    vals = _values(total, tests_only)
    print(f"  全量 {vals['total']} 例 ｜ tests/ {vals['tests_only']} 例 ｜ "
          f"scripts/ {vals['scripts_only']} 例")

    report: list = []
    ok = True
    for p in (README, REPORT_MD):
        if not p.exists():
            print(f"  ⚠️ 跳过（不存在）：{p.name}")
            continue
        same = _sync_md(p, vals, check=args.check, report=report)
        print(f"  {'✅' if same else '❌'} {p.name}")
        ok &= same

    same = _sync_docx(REPORT_DOCX, vals, check=args.check, report=report)
    print(f"  {'✅' if same else '❌'} {REPORT_DOCX.name}")
    ok &= same

    # 规则零命中 = 文档措辞变了 / 脚本已失效，必须报出来
    for p in (README, REPORT_MD):
        if p.exists():
            zeros = _zero_hit_rules(p.read_text(encoding="utf-8"), p.name)
            for z in zeros:
                print(f"  ⚠️ {p.name} 里规则零命中（可能措辞已变）：{z}")
                ok = False

    if report:
        print("\n差异明细：" + ("（check 模式）" if args.check else ""))
        for pat, got, want in report[:30]:
            print(f"  {got} → {want}   [{pat[:40]}]")

    if args.check:
        print("\n✅ 全部一致" if ok else "\n❌ 存在不一致")
        return 0 if ok else 1

    if args.pdf and EXPORT_PDF.exists():
        print("\n· 重导 PDF ...")
        r = subprocess.run([sys.executable, str(EXPORT_PDF)], cwd=str(BASE))
        if r.returncode != 0:
            print("❌ PDF 导出失败")
            return 1
        print("✅ PDF 已重导")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
