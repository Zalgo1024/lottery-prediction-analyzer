# -*- coding: utf-8 -*-
"""scripts/collect_draw_evidence.py —— 方案 B 的开奖证据采集（独立、只读抓取）。

它做什么
--------
从与生产同一套抓取端点读「最新一期开奖信息」，把
**结果首次可被观测到的时刻** + 开奖日期 + 原始开奖行 追加到
`logs/draw_evidence.jsonl`。**不写 CSV、不写 pending、不动台账、不碰预算。**

为什么需要「首次可观测时刻」
--------------------------
官方渠道不公布秒级摇奖时刻。但「我第一次看到这期结果是什么时候」是可测的，
且它给出开奖事件的**上界**（实际摇奖 ≤ 首次可观测）。配合方案 A 的声明时刻，
`min(声明值, 实测)` 就是更保守的上界（见 `ev/draw_evidence.py`）。

纪律
----
  · 只追加 + 幂等键 (版本, 彩种, 期号, 来源)：同一期同一源重复跑不会写第二行；
  · 抓不到就**不写记录**（不编造 `开奖号码`，也不拿抓取失败当「已开奖」）；
  · 退出码：全部成功=0，有彩种失败=1（便于计划任务告警），但失败原因逐条打印；
  · 本脚本不参与出号，不进生产链路。

用法
----
    python scripts/collect_draw_evidence.py --all
    python scripts/collect_draw_evidence.py --lots 双色球,大乐透
    python scripts/collect_draw_evidence.py --all --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ev import draw_evidence as DE          # noqa: E402


def _fetch_digital(lottery: str) -> dict:
    """数字型彩种（七星彩 / 排列5 / 福彩3D / 排列3）的**只读**抓取。

    ★ 不能复用 `data.fetch_500.fetch_one()`：它会**覆盖** `{彩种}历史数据.csv`。
      这里只借它的 `fetch` / `decode` / `parse_rows` 三个纯读函数。
    """
    from data import fetch_500 as F5

    if lottery not in F5.SOURCES:
        raise RuntimeError(f"{lottery} 不在 fetch_500.SOURCES 内，无法只读采集")
    code, url, choose, note = F5.SOURCES[lottery]
    raw = F5.fetch(url, ref=f"{F5.BASE}/{code}/history/history.shtml")
    rows = F5.parse_rows(F5.decode(raw))
    keep = [(str(i), str(d or ""), [int(x) for x in nums])
            for i, d, nums in rows if str(i).isdigit() and len(nums) == choose]
    if not keep:
        raise RuntimeError(f"{lottery} 未解析到任何合法数据行（接口可能变更）")
    issue, day, nums = max(keep, key=lambda r: int(r[0]))
    return {"期号": issue, "开奖日期": day,
            "原始行": [issue, day] + [str(x) for x in nums],
            "来源": url}


def _fetch_latest(lottery: str) -> dict:
    """抓最新一期开奖行；返回 {期号, 开奖日期, 原始行, 来源} 或抛异常。

    ★ `_fetch_endpoints(lottery, start, end)` 的参数是**期号区间**，不是页码：
      早期写成 `(1, 1)` 会去要「第 1 期」，解析出 `期号=1` 这种垃圾。
      这里按生产口径用「本地最新期号」作起点、上界放开，再取期号最大的那一行。
    ★ 七乐彩 / 快乐8 走 market 层（`MARKET_SOURCE`），其更新路径会写 `market.db`
      —— 那是生产副作用，只读采集**不做**，显式失败并写原因（缺席≠通过）。
    """
    from data import fetcher as F

    if lottery in F.MARKET_SOURCE:
        raise RuntimeError(
            f"{lottery} 的数据来自 market 层（{F.MARKET_SOURCE[lottery]}），"
            "其更新路径会写 market.db（生产副作用）⇒ 只读采集不实现，"
            "本轮不产生该彩种证据")
    if lottery not in F.CSV_FILES:
        raise RuntimeError(f"{lottery} 不在 500.com 彩种表内，无法只读采集")
    if lottery in getattr(F, "DIGITAL_LOTTERIES", ()):
        return _fetch_digital(lottery)

    csv_path = F.CSV_FILES[lottery]
    if not csv_path.exists():
        raise RuntimeError(f"本地 CSV 不存在：{csv_path.name}")
    latest = F._latest_draw_number(csv_path)
    try:
        start = max(1, int(latest or 1))
    except (TypeError, ValueError):
        start = 1

    urls = F._fetch_endpoints(lottery, start, 99999)
    if not urls:
        raise RuntimeError("没有可用的抓取端点")

    last_err = None
    best = None
    for url in urls:
        try:
            rows = F.parse_html_rows(F._fetch_page(url))
        except Exception as exc:                      # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            continue
        for row in rows:
            rec = F.row_to_csv_record(row, lottery)
            if not rec:
                continue
            iss = str(rec[0]).strip()
            # ★ 期号合理性：必须是 >=4 位的纯数字（挡掉表头 / 走势图行的 "1"）
            if not iss.isdigit() or len(iss) < 4:
                continue
            if best is None or int(iss) > int(best["期号"]):
                best = {"期号": iss,
                        "开奖日期": str(rec[-1]).strip(),
                        "原始行": [str(x) for x in rec],
                        "来源": str(url)}
        if best is not None:
            break
    if best is None:
        raise RuntimeError(f"全部端点都未解析出有效开奖行（最后错误：{last_err}）")
    if latest and latest.isdigit() and int(best["期号"]) < int(latest):
        raise RuntimeError(
            f"抓到的最新期号 {best['期号']} 早于本地最新 {latest} ⇒ 疑似解析错，拒写证据")
    return best


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="采集开奖证据（只读抓取，只写证据文件）")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--all", action="store_true", help="全部 8 彩种")
    g.add_argument("--lots", help="逗号分隔的彩种名，如 双色球,大乐透")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写证据文件")
    ap.add_argument("--path", default=None, help="证据文件路径（默认 logs/draw_evidence.jsonl）")
    args = ap.parse_args(argv)

    from config import LOTTERY_CONFIG
    lots = list(LOTTERY_CONFIG) if args.all else [x.strip() for x in (args.lots or "").split(",") if x.strip()]
    unknown = [x for x in lots if x not in LOTTERY_CONFIG]
    if unknown:
        print(f"❌ 未知彩种：{unknown}（可用：{list(LOTTERY_CONFIG)}）")
        return 2

    path = Path(args.path) if args.path else DE.EVIDENCE_PATH
    now = datetime.now().isoformat(timespec="seconds")
    print(f"采集时间 {now}  目标 {len(lots)} 彩种  证据文件 {path}"
          f"{'（dry-run，不写）' if args.dry_run else ''}")

    ok, fail, skip = [], [], []
    for lot in lots:
        try:
            got = _fetch_latest(lot)
        except Exception as exc:                      # noqa: BLE001
            fail.append((lot, f"{type(exc).__name__}: {exc}"))
            print(f"  ✗ {lot}：抓取失败，未写证据（{type(exc).__name__}: {exc}）")
            continue
        rec = {
            "版本": DE.EVIDENCE_VERSION,
            "彩种": lot,
            "期号": got["期号"],
            "开奖日期": got["开奖日期"],
            "来源": got["来源"],
            "抓取时间": now,
            # ★ 抓到的这一刻就是「结果首次可被观测」的时刻（本次运行首次看到该期）
            "首次可观测时刻": now,
            "开奖号码": {"原始CSV行": got["原始行"]},
            "备注": "开奖号码原样保存抓取行，未做字段解释；字段含义按当期 CSV 表头解读。"
                    "首次可观测时刻 ≠ 摇奖时刻，只是开奖事件的上界。",
        }
        try:
            DE.validate_record(rec)
        except DE.DrawEvidenceError as exc:
            fail.append((lot, f"记录不合格：{exc}"))
            print(f"  ✗ {lot}：记录不合格，未写（{exc}）")
            continue
        if args.dry_run:
            print(f"  · {lot} 期 {rec['期号']} 日期 {rec['开奖日期']}（dry-run）")
            skip.append(lot)
            continue
        written = DE.append_evidence(rec, path=path)
        if written:
            ok.append(lot)
            print(f"  ✓ {lot} 期 {rec['期号']} 已写证据")
        else:
            skip.append(lot)
            print(f"  · {lot} 期 {rec['期号']} 同键已存在，幂等跳过")

    print(f"\n汇总：写入 {len(ok)} / 幂等跳过 {len(skip)} / 失败 {len(fail)}"
          f"（总数 {len(lots)}）")
    if fail:
        print("失败明细：")
        for lot, why in fail:
            print(f"  - {lot}：{why}")
        print("★ 失败不等于「未开奖」：只表示本次没取到证据，不影响出号。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
