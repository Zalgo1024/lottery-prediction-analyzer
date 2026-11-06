# -*- coding: utf-8 -*-
"""前缀影子验证 · **只读状态快照**落盘（给 Web 静态看板用，不需要重启 Flask）。

用法：
    E:/Python/python.exe scripts/shadow_status.py
    E:/Python/python.exe scripts/shadow_status.py --settle
    E:/Python/python.exe scripts/shadow_status.py --out web/static/data/shadow_status.json

为什么不走新增 Flask 路由
------------------------
新增路由必须重启主服务才生效，而重启会触发启动自愈的 `ensure_pending_groups()`
（当前滑轨已到 10000 注、pending 还停在 1000~2000 注 ⇒ 会真的补出号）。
把状态写成**静态 JSON** 交给 Flask 自带的 `/static/` 路由读取，既能在现有进程里
立刻打开看板，又完全不碰出号链路。新路由 `/api/shadow/status` 同时保留，
下次自然重启后可用。

只读保证
--------
默认只写 `web/static/data/shadow_status.json`（展示产物）与 `logs/` 下的报告，
不写影子台账、不写 pending、不写预算、不改注数。`--settle` 才会追加结算行，
且结算层本身 append-only + 幂等，没开奖的期跳过并写原因（不写成通过）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ev import shadow_bridge as SB                            # noqa: E402

DEFAULT_OUT = ROOT / "web" / "static" / "data" / "shadow_status.json"
DEFAULT_MD = ROOT / "logs" / "prefix_shadow_live.md"


def _atomic_json(path: Path, doc: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    os.replace(tmp, path)


def _markdown(doc: Dict[str, Any]) -> str:
    s = doc.get("影子状态") or {}
    a = doc.get("快照体检") or {}
    f = doc.get("门槛可行性") or {}
    e = doc.get("方案B证据") or {}
    sw = doc.get("开关") or {}
    L = ["# 前缀影子验证 · 实时状态（只读）", "",
         f"> 生成时刻：{doc.get('生成时刻')}；编排版本 {doc.get('编排版本')}。"
         "本报告不产生新读数、不做判决。", "",
         f"**当前状态：{s.get('状态', '—')}**（可开判={s.get('可开判')}）", "",
         "## 台账与证据", "",
         f"- 登记 {s.get('登记期数')} 期 / 结算 {s.get('结算期数')} 期 / "
         f"独立评估批结算 {(s.get('独立评估批') or {}).get('结算期数')} 期。",
         f"- 冻结快照分母 {a.get('文件总数')} 份，具备登记资格 {a.get('合格数')} 份，"
         f"不合格 {a.get('不合格数')} 份（逐份写原因，分母不缩水）。",
         f"- 开关：启用={sw.get('启用')}，模式={sw.get('模式')}，"
         f"外部源={sw.get('外部源')}。",
         f"- 方案 B 证据 {e.get('行数')} 条，坏行 {e.get('坏行数')} 条；"
         f"交叉校验参与 {(e.get('交叉校验') or {}).get('参与校验')} 期、"
         f"矛盾 {(e.get('交叉校验') or {}).get('矛盾')} 期。",
         "", "## 开判门槛与可行性", "",
         f"- 门槛 {f.get('门槛期数')} 期（读数彩种 {f.get('门槛彩种')}），"
         f"还需 {s.get('还需期数')} 期。",
         f"- 按完整年份中位数估算：主口径约 **{f.get('主口径预计年数')} 年**，"
         f"逐彩种最快 {f.get('最快预计年数')} 年 / 最慢 {f.get('最慢预计年数')} 年。",
         "", "**未开判原因**：", ""]
    for i, why in enumerate(s.get("未开判原因") or [], 1):
        L.append(f"{i}. {why}")
    L += ["", "## 纪律", ""]
    for n in doc.get("纪律") or []:
        L.append(f"- {n}")
    for n in f.get("披露") or []:
        L.append(f"- {n}")
    L.append(f"- {e.get('披露')}")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="影子验证只读状态快照（写静态看板 JSON）")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--md", type=Path, default=DEFAULT_MD)
    ap.add_argument("--settle", action="store_true",
                    help="顺带做一次开奖后结算（append-only 幂等；没开奖的期跳过）")
    args = ap.parse_args()

    now = datetime.now().isoformat(timespec="seconds")
    settle = None
    if args.settle:
        settle = SB.settle_after_draw(settle_time=now)
    doc = SB.status_snapshot(as_of=now)
    doc["本次结算"] = settle
    doc["页面"] = "/static/shadow.html"
    doc["接口"] = "/api/shadow/status（需重启后可用；本静态快照无需重启）"

    _atomic_json(Path(args.out), doc)
    md = Path(args.md)
    md.parent.mkdir(parents=True, exist_ok=True)
    md.write_text(_markdown(doc), encoding="utf-8")

    s = doc.get("影子状态") or {}
    f = doc.get("门槛可行性") or {}
    print(f"影子状态：{s.get('状态')}，可开判={s.get('可开判')}，"
          f"登记 {s.get('登记期数')} / 结算 {s.get('结算期数')}")
    print(f"独立评估批结算 {(s.get('独立评估批') or {}).get('结算期数')} 期，"
          f"还需 {s.get('还需期数')} 期")
    print(f"门槛可行性：主口径约 {f.get('主口径预计年数')} 年"
          f"（最快 {f.get('最快预计年数')} / 最慢 {f.get('最慢预计年数')}）")
    if settle is not None:
        print(f"本次结算：已结算 {settle.get('已结算', 0)} / "
              f"跳过 {settle.get('跳过', 0)} / 失败 {settle.get('失败', 0)}")
    print(f"落盘：{args.out} / {md}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
