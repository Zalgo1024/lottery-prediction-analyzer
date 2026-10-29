# -*- coding: utf-8 -*-
"""同步「测试用例数」到所有需要手工维护的地方 —— 一条命令替代手工逐个改。

为什么值得单独写（2026-10-21）：

加完测试后，这个数字散落在 README 的徽章 / 命令注释 / 说明段等多处文本里。
每加一批测试都要手工数一遍再逐个改，改漏一处就变成"文档在撒谎"。
而这个数字恰恰是外人判断项目质量的第一个信号 —— 它错了比没有更糟。

2026-10-29：`docs/结题报告.*` 已整体移除，本脚本随之只保留 README 一个载体。
docx 的跨节点替换逻辑（Word 拆 run 导致正则落空）一并删除 —— 没有载体就不该
留着会腐烂的代码。需要时从 git 历史取回。

做法：
  1. 真源 = `pytest --collect-only -q`（两次：全量 / tests/ 子集），不靠人记；
  2. 对 md 做**带上下文的正则替换**（不做裸数字全局替换，
     否则正文里的"915"以外的数字会被误伤）；
  3. 每条规则都要求至少命中一次，某条零命中 = 文档表述变了或脚本失效，
     直接报错退出（防止"跑成功但什么都没改"的静默失真）。

★ 新增数字时必须套进 RULES 里的现有句式，否则那个数字会**永久脱管**：
  `--check` 只会说「全部一致」，而它根本不在任何规则的视野里。

用法：
    E:/Python/python.exe scripts/sync_counts.py --check    # 只检查，不一致返回 1
    E:/Python/python.exe scripts/sync_counts.py            # 改 README
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
README = BASE / "README.md"

# (正则, 归类, 适用载体集合) —— 正则需要恰好三个组：前缀 / 数字 / 后缀
RULES: list[tuple[str, str, tuple[str, ...]]] = [
    (r"(pytest-)(\d+)(%20passed)", "total", ("readme",)),
    (r"(全量 )(\d+)( 例)", "total", ("readme",)),
    (r"(# )(\d+)( passed（全量)", "total", ("readme",)),
    (r"(pytest tests/\s+# )(\d+)( passed)", "tests_only", ("readme",)),
    (r"(另有 )(\d+)( 例在 scripts/ 下)", "scripts_only", ("readme",)),
]

_FILE_KIND = {"README.md": "readme"}


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


# ------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只检查不修改")
    args = ap.parse_args()

    print("· 收集用例数（全量）...")
    total = _collect([])
    print("· 收集用例数（tests/）...")
    tests_only = _collect(["tests/"])
    vals = _values(total, tests_only)
    print(f"  全量 {vals['total']} 例 ｜ tests/ {vals['tests_only']} 例 ｜ "
          f"scripts/ {vals['scripts_only']} 例")

    report: list = []
    ok = True
    if not README.exists():
        raise SystemExit(f"❌ README 不存在：{README}")
    same = _sync_md(README, vals, check=args.check, report=report)
    print(f"  {'✅' if same else '❌'} {README.name}")
    ok &= same

    # 规则零命中 = 文档措辞变了 / 脚本已失效，必须报出来
    zeros = _zero_hit_rules(README.read_text(encoding="utf-8"), README.name)
    for z in zeros:
        print(f"  ⚠️ {README.name} 里规则零命中（可能措辞已变）：{z}")
        ok = False

    if report:
        print("\n差异明细：" + ("（check 模式）" if args.check else ""))
        for pat, got, want in report[:30]:
            print(f"  {got} → {want}   [{pat[:40]}]")

    if args.check:
        print("\n✅ 全部一致" if ok else "\n❌ 存在不一致")
        return 0 if ok else 1

    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
