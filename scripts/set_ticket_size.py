# -*- coding: utf-8 -*-
"""改出号注数 —— 唯一的人工通道，**默认 dry-run**。

为什么需要它，而不是直接手改 JSON
--------------------------------
`config/ticket_size.json` 有三个注数字段（`fixed` / `dynamic` / `current`）和三种
模式，改错一个字段的后果不是「报错」，而是**静默按错的注数出号**：

  · 只改 `current` ⇒ 完全不生效（`effective_count()` 在 dynamic 模式下只读 `dynamic`，
    `current` 只是 `sanitize()` 维护的展示字段）。这正是 2026-10-31 那次误会的来源。
  · `limits` 改小 ⇒ `sanitize()` 会用 `_clamp()` **静默夹取**，你以为改了 500、实际是 200。
  · 模式与字段不匹配（`mode=fixed` 却改 `dynamic`）⇒ 改了等于没改。

所以本脚本一次把三件事做对，并在 dry-run 里把 **before → after → 月度成本** 一次性
打出来让人核对。成本是这次改动唯一真实的后果。

★ 本脚本不启用/不关闭预算闸门 —— 那是 `config/budget_aggregate.json` 的职责，
  两者故意分开：改注数不该顺带改额度上限。
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 默认目标：用户 2026-10-31 拍板的值
TARGET = {"双色球": 500, "大乐透": 400, "排列5": 100,
          "福彩3D": 200, "排列3": 200, "七星彩": 300}


def _month_cost(groups: Dict[str, int]) -> Dict[str, Any]:
    from ev.budget_gate import month_plan
    return month_plan(groups=groups)


def apply_target(target: Dict[str, int], *, apply: bool = False,
                 reason: str = "") -> Dict[str, Any]:
    from data import ticket_size as ts

    cfg = ts.sanitize(ts.load_config())
    before = {k: ts.effective_count(k, cfg) for k in target}

    new = dict(cfg)
    new["mode"] = ts.MODE_DYNAMIC
    new["dynamic"] = {**cfg.get("dynamic", {}), **target}
    # ★ limits 必须 ≥ 目标，否则 sanitize 会静默夹取（改 500 得到 200，且不报错）
    limits = dict(cfg.get("limits") or {})
    limits.update({k: max(int(limits.get(k, 0) or 0), int(v)) for k, v in target.items()})
    new["limits"] = limits
    new["锁定"] = True          # 锁住自动路径，禁止它事后把注数改回去
    new["policy"] = {
        "更新于": time.strftime("%Y-%m-%d %H:%M:%S"),
        "彩种": "全部",
        "理由": reason or "用户拍板恢复目标注数；成本由 budget_gate 封顶",
    }

    after = {k: min(int(ts._as_int(new["dynamic"].get(k), 0)), int(limits.get(k, 0)))
             for k in target}

    out = {
        "before": before,
        "after": after,
        "目标": dict(target),
        "limits": {k: limits.get(k) for k in target},
        "月度成本_before": _month_cost(before).get("月度合计元"),
        "月度成本_after": _month_cost(after).get("月度合计元"),
        "已写入": False,
    }

    if apply:
        ts.save_config(new)
        check = ts.sanitize(ts.load_config())
        real = {k: ts.effective_count(k, check) for k in target}
        out["写入后生效值"] = real
        out["已写入"] = True
        # 写后必须复核：夹取如果发生，差异会在此暴露，而不是等到出号才发现
        out["与目标一致"] = all(real[k] == target[k] for k in target)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="设置出号注数（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真正写入")
    ap.add_argument("--lottery", "-l", action="append", help="只改某个彩种，如 双色球=500")
    ap.add_argument("--reason", default="", help="写进 policy 的理由")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    target = dict(TARGET)
    if args.lottery:
        target = {}
        for item in args.lottery:
            k, _, v = item.partition("=")
            if not v:
                print(f"格式错误：{item}（应为 彩种=注数）")
                return 2
            target[k.strip()] = int(v)

    out = apply_target(target, apply=args.apply, reason=args.reason)

    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    print(f"{'彩种':<8}{'当前':>8}{'目标':>8}{'写入后':>8}{'limits':>9}")
    print("-" * 44)
    real = out.get("写入后生效值") or {}
    for k in target:
        print(f"{k:<8}{out['before'].get(k, 0):>8}{target[k]:>8}"
              f"{real.get(k, '—'):>8}{out['limits'].get(k, 0):>9}")
    print()
    print(f"月度成本：{out['月度成本_before']:,.0f} 元 → {out['月度成本_after']:,.0f} 元")
    print()
    if not args.apply:
        print("（dry-run，未写入。确认后加 --apply）")
        return 0
    if not out.get("与目标一致"):
        print("★ 写入后生效值与目标不一致 ⇒ 检查 limits 是否被夹取、字段是否写对。")
        return 1
    print("已写入，且写入后生效值与目标一致。")
    return 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    raise SystemExit(main())
