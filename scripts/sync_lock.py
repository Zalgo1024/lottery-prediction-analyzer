# -*- coding: utf-8 -*-
"""生成 `requirements.lock.txt`：把「能跑的环境」钉死成可复现的版本集合。

为什么需要（2026-10-21 体检结论）：

`requirements.txt` 九行全是 `>=`，等于没有任何约束 —— 今天能跑不代表半年后
重装能跑。这个项目最贵的东西不是代码，是 `training/` 里那批训练产物和
`lottery_data/` 二十年历史，一次 numpy/lightgbm 的大版本跳跃可能让它们全部失效，
而错误往往表现为"结果看起来正常但数字变了"，比直接报错难发现得多。

做法：从 `requirements.txt` 的**顶层包**出发，按 `Requires-Dist` 递归求闭包，
只锁项目真正用到的这部分（不把本机无关的 300 多个包写进来）。

用法：
    E:/Python/python.exe scripts/sync_lock.py            # 写 requirements.lock.txt
    E:/Python/python.exe scripts/sync_lock.py --check    # 只比对，不一致返回 1

更新依赖后（改 requirements.txt / 升级包）请重跑一次并一起提交。
"""

from __future__ import annotations

import argparse
import re
import sys
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
REQ = BASE / "requirements.txt"
LOCK = BASE / "requirements.lock.txt"

_HEADER = """\
# requirements.lock.txt —— 可复现环境（由 scripts/sync_lock.py 生成，勿手改）
#
# Python {py} ｜ 顶层来源：requirements.txt（{n} 个直接依赖）
# 内容：顶层包 + 其传递依赖的精确版本（共 {total} 个）
#
# 复现：pip install -r requirements.lock.txt
# 升级：改 requirements.txt → 重跑 scripts/sync_lock.py → 跑全量测试 → 一起提交
#
# 刻意不记主机名/生成时间：否则换机或在 CI 里重新生成时头部天天变，
# 每次 diff 都有一堆无意义噪声，真正的版本变化反而被淹掉。
"""


def _canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _top_level() -> list[str]:
    out = []
    for line in REQ.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        s = re.split(r"[<>=!~\[;]", s, maxsplit=1)[0].strip()
        if s:
            out.append(s)
    return out


def _closure(tops: list[str]) -> dict[str, str]:
    """按 Requires-Dist 递归求闭包，返回 {规范名: 版本}。"""
    from packaging.markers import default_environment
    from packaging.requirements import Requirement

    env = default_environment()
    got: dict[str, str] = {}
    missing: list[str] = []
    stack = list(tops)
    while stack:
        name = stack.pop()
        key = _canon(name)
        if key in got:
            continue
        try:
            d = distribution(name)
        except PackageNotFoundError:
            missing.append(name)
            continue
        got[key] = d.version
        for raw in d.requires or []:
            try:
                req = Requirement(raw)
            except Exception:
                continue
            # extras 带来的依赖默认不算（本项目都没用 extras）
            try:
                if req.marker is not None and not req.marker.evaluate(env):
                    continue
            except Exception:
                continue
            stack.append(req.name)
    if missing:
        print("⚠️ 已声明但未安装（未能入锁）：" + ", ".join(missing), file=sys.stderr)
    return got, sorted(set(missing))


def _render(pkgs: dict[str, str], missing: list[str] | None = None) -> str:
    import platform

    tops = _top_level()
    head = _HEADER.format(py=platform.python_version(), n=len(tops), total=len(pkgs))
    if missing:
        # 如实记录：这些包被上游声明为依赖，但本机没装。
        # 不补装、不假装完整 —— 装上可能改变现有数值，属于需要单独决策的变更。
        head += ("#\n# ⚠️ 上游声明但本机未安装（未入锁，相关可选功能不可用）："
                 + ", ".join(missing) + "\n")
    body = "\n".join(f"{k}=={v}" for k, v in sorted(pkgs.items()))
    return head + "\n" + body + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="只比对，不写文件")
    args = ap.parse_args()

    pkgs, missing = _closure(_top_level())
    text = _render(pkgs, missing)
    if args.check:
        if not LOCK.exists():
            print("❌ requirements.lock.txt 不存在，请先运行 scripts/sync_lock.py")
            return 1
        old = LOCK.read_text(encoding="utf-8")
        # 只比包行，比头部的机器名/路径毫无意义
        def _pkg_lines(t: str) -> list[str]:
            return [x for x in t.splitlines() if x and not x.startswith("#")]
        a, b = _pkg_lines(old), _pkg_lines(text)
        if a == b:
            print(f"✅ lock 与当前环境一致（{len(b)} 个包）")
            return 0
        diff = set(a) ^ set(b)
        print(f"❌ lock 与当前环境不一致（差异 {len(diff)} 行）")
        for x in sorted(diff)[:20]:
            print("   ", x)
        return 1

    LOCK.write_text(text, encoding="utf-8")
    n = len([x for x in text.splitlines() if x and not x.startswith("#")])
    print(f"✅ 已写 {LOCK}（{n} 个包）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
