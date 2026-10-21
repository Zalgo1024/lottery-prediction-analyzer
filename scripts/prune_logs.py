# -*- coding: utf-8 -*-
"""按日期清理旧日志（只认严格的日期文件名，绝不通配删除）。

背景（2026-10-21 体检）：`logs/` 已累积 220 个 `.log` + 129 个 `.txt`，
日志按天新建（`logs/YYYYMMDD.log`、`logs/automation/auto_YYYYMMDD.log`），
但**没有任何保留策略** —— 只涨不清。单看 16MB 不算大事，问题是它让
"翻上周到底发生了什么"变成在一堆同类文件里翻，而且会一直涨下去。

安全边界（这是删文件的脚本，宁可少删不可多删）：
  · 只匹配两种严格命名：`logs/YYYYMMDD.log`、`logs/automation/<前缀>_YYYYMMDD.log`；
  · 日期取自**文件名**，不看 mtime（mtime 会被后续追加改写）；
  · 只处理 `logs/` 直接子级和 `logs/automation/` 直接子级，不递归；
  · 默认 dry-run，必须显式 `--apply` 才真删；
  · 永不删除 `logs/logger.py`（它是运行时源码，不是日志产物）。

用法：
    E:/Python/python.exe scripts/prune_logs.py               # 只看会删什么
    E:/Python/python.exe scripts/prune_logs.py --apply        # 真删（默认保留 90 天）
    E:/Python/python.exe scripts/prune_logs.py --apply --days 180
"""

from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
LOGS = BASE / "logs"

# logs/20261021.log
_DAILY = re.compile(r"^(\d{8})\.log$")
# logs/automation/auto_20261021.log（前缀可有可无，但必须以 _YYYYMMDD.log 结尾）
_ROTATED = re.compile(r"^[A-Za-z0-9_\-]*_(\d{8})\.log$")


def _targets(days: int) -> list[Path]:
    cutoff = date.today().toordinal() - days
    out: list[Path] = []

    def _scan(d: Path, rx: re.Pattern) -> None:
        if not d.is_dir():
            return
        for p in d.iterdir():
            if not p.is_file():
                continue
            m = rx.match(p.name)
            if not m:
                continue
            try:
                stamp = datetime.strptime(m.group(1), "%Y%m%d").date()
            except ValueError:
                continue
            if stamp.toordinal() < cutoff:
                out.append(p)

    _scan(LOGS, _DAILY)
    _scan(LOGS / "automation", _ROTATED)
    return sorted(out)


def apply(days: int = 90) -> dict:
    """供调度器调用：直接真删。返回 {"删除": n, "释放KB": x, "保留天数": days}。"""
    files = _targets(days)
    freed = 0
    deleted = 0
    for p in files:
        try:
            freed += p.stat().st_size
            p.unlink()
            deleted += 1
        except Exception:
            pass
    return {"删除": deleted, "释放KB": round(freed / 1024), "保留天数": days}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=90, help="保留最近多少天（默认 90）")
    ap.add_argument("--apply", action="store_true", help="真正删除（默认只预览）")
    args = ap.parse_args()

    if args.days < 7:
        print("❌ 保留天数不得小于 7（避免误操作把本周日志删光）")
        return 2

    files = _targets(args.days)
    total = sum(p.stat().st_size for p in files if p.exists())
    print(f"保留最近 {args.days} 天 · 待清理 {len(files)} 个文件 · "
          f"约 {total / 1024:.0f} KB")

    if not files:
        print("✅ 没有需要清理的日志")
        return 0

    for p in files[:10]:
        print(f"  {p.relative_to(BASE)}")
    if len(files) > 10:
        print(f"  …另有 {len(files) - 10} 个")

    if not args.apply:
        print("\n（预览模式，未删除。加 --apply 执行）")
        return 0

    res = apply(args.days)
    print(f"\n✅ 已删除 {res['删除']} 个，释放 {res['释放KB']} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
