"""
回填 `valid_prediction`：把「按现行判定应为真预测、但落盘仍是 False」的历史记录修正过来。

背景
----
`data/feedback.py::_is_valid_prediction` 在 2026-09-21 从「只看派生字段 预测日期」
改成「优先看事实字段 生成时间」。判定逻辑改了，**但当时已经落盘的历史记录没有回填**，
于是有一批记录至今仍带着旧口径的 False 标签。

典型受害样本：2026-09-20 是国庆调休上班日，被开奖日历误判为休市 ⇒ `预测日期`
被推到 09-22 ⇒ 旧规则（预测日期 > 开奖日期 ⇒ 马后炮）判成「训练/回测」。
而这批票实际生成于 09-17 ~ 09-20 凌晨，全部早于当晚开奖。

本脚本只做一件事：**用与结算侧逐字相同的逻辑重算 `valid_prediction`，只把
False 修正为 True 的那部分写回**。任何 True→False 都视为异常并中止（说明判定
逻辑相对落盘数据发生了方向性变化，必须人工确认，不能自动改）。

用法
----
    python scripts/backfill_valid_prediction.py            # 默认 dry-run，只打印
    python scripts/backfill_valid_prediction.py --apply    # 实际写盘（自动备份）

纪律
----
- 只改 `valid_prediction` 一个键，其余字段一字不动、顺序不变。
- 写盘走 `data.feedback._safe_write_json`（原子写 + 损坏隔离）。
- 只处理 `来源` 不是「训练/train」的记录（训练回测样本一律保持 False）。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from data.feedback import (            # noqa: E402
    FEEDBACK_DIR,
    _is_valid_prediction,
    _safe_write_json,
)

LOTTERIES = ["双色球", "大乐透", "排列3", "排列5", "福彩3D", "七星彩"]
TRAIN_SOURCES = ("train", "训练")


def _parse_date(v):
    """把 `开奖日期` 转成 date；解析失败返回 None（此时不改动该条）。"""
    if not v:
        return None
    try:
        return date.fromisoformat(str(v)[:10])
    except (ValueError, TypeError):
        return None


def _gen_time(rec: dict) -> str:
    """该记录能拿到的「生成时间」原文（空串 = 没有时间戳）。"""
    return str(rec.get("批次时间") or rec.get("生成时间") or rec.get("记录时间") or "")


def recompute(rec: dict):
    """
    按 `data/feedback.py` 结算侧（第 1232-1238 行）的**同一套**逻辑重算。

    返回 True / False / None（None = 关键字段缺失，无法判定，保持原值）。
    """
    actual = _parse_date(rec.get("开奖日期"))
    if actual is None:
        return None
    gen = _gen_time(rec)
    valid = _is_valid_prediction(str(rec.get("预测日期") or ""), actual, gen or None)
    if str(rec.get("来源") or "") in TRAIN_SOURCES:
        valid = False
    return bool(valid)


def eligible(rec: dict):
    """
    是否有资格回填。**除了「重算结果变了」，还要求有事实依据**：

    1. 必须有生成时间戳 —— 没有时间戳的记录只能靠派生字段 `预测日期` 推定，
       而推定不足以推翻一条已经落盘的 False。实证：`双色球 26090` 无时间戳、
       日期也自洽，但它是被点名的**污染记录**（预测蓝球 [1,2] 违反 blue_count=1，
       见 data/feedback.py `_hit_detail` 附近注释），标 False 是故意为之。
    2. 来源不能是「训练/train」—— 训练回测样本一律不参与真预测口径。

    返回 (是否可回填, 原因)。
    """
    if str(rec.get("来源") or "") in TRAIN_SOURCES:
        return False, "训练来源"
    if not _gen_time(rec):
        return False, "无生成时间戳（仅日期推定，不足以推翻已落盘结论）"
    if recompute(rec) is not True:
        return False, "重算仍非真预测"
    return True, "有生成时间戳且早于开奖"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="实际写盘（默认 dry-run）")
    ap.add_argument("--stamp", default=datetime.now().strftime("%Y%m%d-%H%M%S"),
                    help="备份后缀（默认当前时间）")
    args = ap.parse_args()

    grand = Counter()
    plan: list[tuple[Path, list, list]] = []       # (路径, 新数据, 变更明细)
    to_false: list[tuple] = []
    skipped: list[tuple] = []                      # 应改但证据不足 → 保持原值

    for name in LOTTERIES:
        path = FEEDBACK_DIR / f"{name}_feedback_history.json"
        if not path.exists():
            print(f"[跳过] 无文件：{path.name}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            print(f"[跳过] 结构非 list：{path.name}")
            continue

        changes = []
        for i, rec in enumerate(data):
            if not isinstance(rec, dict):
                continue
            old = rec.get("valid_prediction")
            new = recompute(rec)
            if new is None:
                grand["无法判定·保持原值"] += 1
                continue
            # 落盘 False、重算非 False → 候选。但还要过「证据门禁」才允许回填
            if old is False and new is True:
                ok, why = eligible(rec)
                if ok:
                    changes.append((i, rec, False, True))
                else:
                    skipped.append((name, rec.get("期号"), _gen_time(rec)[:19] or "—",
                                    str(rec.get("开奖日期"))[:10], why))
                    grand["候选但证据不足·保持原值"] += 1
            elif old is True and new is False:
                to_false.append((name, rec.get("期号"), rec.get("来源"),
                                 _gen_time(rec)[:19], rec.get("开奖日期")))
            else:
                grand["不变"] += 1

        if changes:
            for i, rec, _o, _n in changes:
                data[i]["valid_prediction"] = True
            plan.append((path, data, changes))
            grand["False→True"] += len(changes)
            src = Counter(str(r.get("来源")) for _i, r, _o, _n in changes)
            per = Counter((r.get("期号"), str(r.get("开奖日期"))[:10]) for _i, r, _o, _n in changes)
            print(f"\n=== {name}：待修正 {len(changes)} 条 ===")
            print(f"  来源分布：{dict(src)}")
            for (issue, draw), n in per.most_common():
                print(f"  期号 {issue}  开奖日 {draw}  ← {n} 条")
            # 每条都给出「生成时间 vs 开奖日」的最小证据
            gens = sorted(str(r.get("批次时间") or "")[:19] for _i, r, _o, _n in changes)
            print(f"  生成时间范围：{gens[0]} ~ {gens[-1]}")
            bad = [r for _i, r, _o, _n in changes
                   if not (r.get("批次时间") or r.get("生成时间") or r.get("记录时间"))]
            if bad:
                print(f"  [!] {len(bad)} 条无生成时间（不该出现在待修正集里，请检查）")
        else:
            print(f"\n=== {name}：无需修正 ===")

    print("\n" + "=" * 64)
    print("汇总：", dict(grand))

    if skipped:
        print(f"\n--- 重算后看似该改、但证据不足故保持原值：{len(skipped)} 条 ---")
        seen = set()
        for row in skipped:
            if row in seen:
                continue
            seen.add(row)
            print(f"  {row[0]} 期号 {row[1]}  生成时间 {row[2]}  开奖日 {row[3]}  ← {row[4]}")

    if to_false:
        print(f"\n[中止] 发现 {len(to_false)} 条 True→False 漂移，判定逻辑相对落盘数据发生"
              f"方向性变化，不能自动改。样本：")
        for x in to_false[:5]:
            print("   ", x)
        return 2

    if not plan:
        print("\n没有需要回填的记录。")
        return 0

    if not args.apply:
        print("\n[dry-run] 未写盘。确认无误后加 --apply 执行。")
        print("提示：回填会把这些记录计入「真预测」统计，"
              "累计命中率/策略表现的分母会随之变化。")
        return 0

    for path, data, changes in plan:
        backup = path.with_suffix(path.suffix + f".bak-{args.stamp}")
        shutil.copy2(path, backup)
        _safe_write_json(path, data)
        print(f"\n[已写] {path.name}  修正 {len(changes)} 条  → 备份 {backup.name}")

    # 写后自检：重新读回，确认**可回填集**已经清零（用与执行时同一个 eligible 口径）
    print("\n=== 写后自检（口径 = eligible，与执行一致）===")
    for path, _data, _ch in plan:
        after = json.loads(path.read_text(encoding="utf-8"))
        still = sum(1 for rec in after
                    if isinstance(rec, dict)
                    and rec.get("valid_prediction") is False
                    and eligible(rec)[0])
        print(f"  {path.name}: 仍可回填 {still} 条"
              + ("  [OK]" if still == 0 else "  [失败]"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
