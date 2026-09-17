# -*- coding: utf-8 -*-
"""
开奖号码外部核对（只读，不写任何数据文件）。

从 500.com 重新抓取最近 N 期开奖，逐期与本地 CSV 的开奖号码比对。
用于发现「开奖号码录入错误 / 漏抓 / 错位」这类最严重的号码问题。

用法: E:/Python/python.exe scripts/verify_draws_vs_source.py [--recent 60]
产物: logs/开奖核对_<日期>.md + .json
"""
import argparse
import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE_DIR = Path(__file__).resolve().parent.parent
DATA = BASE_DIR / "lottery_data"

LOCAL = {
    "双色球": ("双色球历史数据_cleaned.csv", "DD/MM/YYYY"),
    "大乐透": ("大乐透历史数据_cleaned.csv", "DD/MM/YYYY"),
    "排列5": ("排列5历史数据.csv", "ISO"),
    "福彩3D": ("福彩3D历史数据.csv", "ISO"),
    "排列3": ("排列3历史数据.csv", "ISO"),
    "七星彩": ("七星彩历史数据.csv", "ISO"),
}


def local_numbers(name, recent):
    """本地 CSV 最近 recent 期 → {期号(int): [号码...]}（按开奖号码列顺序）"""
    fn, _ = LOCAL[name]
    rows = list(csv.DictReader(open(DATA / fn, encoding="utf-8-sig")))
    out = {}
    for r in rows[:recent]:
        iss = str(r["期号"]).strip()
        try:
            i = int(iss)
        except ValueError:
            continue
        if name in ("双色球", "大乐透"):
            nums = []
            k = 1
            while f"红球{k}" in r:
                nums.append(int(r[f"红球{k}"]))
                k += 1
            k = 1
            while f"蓝球{k}" in r:
                nums.append(int(r[f"蓝球{k}"]))
                k += 1
        else:
            nums = [int(r[f"号码{k}"]) for k in range(1, 8) if f"号码{k}" in r]
        out[i] = nums
    return out


def source_numbers(name, recent, timeout=30):
    """从 500.com 抓取 → {期号(int): [号码...]}"""
    out = {}
    if name in ("双色球", "大乐透"):
        from data.fetcher import _fetch_endpoints, parse_html_rows, row_to_csv_record, LOTTERY_CONFIG
        # 最近期号估算
        loc = local_numbers(name, 1)
        end = max(loc)
        start = end - recent
        html = None
        last_err = None
        for url in _fetch_endpoints(name, start, end):
            try:
                from data.fetcher import _fetch_page
                html = _fetch_page(url)
                if html and "期号" in html:
                    break
            except Exception as e:
                last_err = e
                html = None
        if not html:
            raise RuntimeError(f"抓取失败: {last_err}")
        cfg = LOTTERY_CONFIG[name]
        rc, bc = cfg["red_count"], cfg["blue_count"]
        for row in parse_html_rows(html):
            rec = row_to_csv_record(row, name)
            if not rec:
                continue
            try:
                out[int(rec[0])] = [int(x) for x in rec[1:1 + rc + bc]]
            except ValueError:
                continue
    else:
        from data.fetch_500 import SOURCES, fetch, decode, parse_rows
        code, url, choose, _note = SOURCES[name]
        raw = fetch(url, timeout=timeout, ref=f"https://datachart.500.com/{code}/history/history.shtml")
        for iss, _d, nums in parse_rows(decode(raw)):
            nums = [int(x) for x in nums]
            out[int(str(iss).lstrip("0") or "0")] = nums
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=60)
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()

    results = {}
    total_bad = 0
    for name in LOCAL:
        try:
            loc = local_numbers(name, args.recent)
            src = source_numbers(name, args.recent, args.timeout)
        except Exception as e:
            print(f"[{name}] 抓取/解析失败: {type(e).__name__}: {e}")
            results[name] = {"状态": f"抓取失败: {e}"}
            continue
        # 只比双方都有的期号
        common = sorted(set(loc) & set(src), reverse=True)
        bad = []
        only_local = sorted(set(loc) - set(src), reverse=True)[:10]
        for i in common:
            if loc[i] != src[i]:
                bad.append({"期号": i, "本地": loc[i], "数据源": src[i]})
        total_bad += len(bad)
        results[name] = {
            "本地期数": len(loc), "数据源期数": len(src), "共同期数": len(common),
            "号码不一致": bad, "仅本地有(数据源缺)": only_local,
        }
        flag = "✅" if not bad else "❌"
        print(f"[{name}] 共同 {len(common)} 期，号码不一致 {len(bad)} 期 {flag}")
        for b in bad[:8]:
            print(f"    期{b['期号']} 本地={b['本地']} 源={b['数据源']}")

    out_md = BASE_DIR / "logs" / f"开奖核对_{date.today():%Y%m%d}.md"
    out_json = BASE_DIR / "logs" / f"开奖核对_{date.today():%Y%m%d}.json"
    out_json.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [f"# 开奖号码核对（vs 500.com）{date.today()}", "", f"号码不一致合计：**{total_bad}** 期", ""]
    for name, r in results.items():
        lines.append(f"## {name}")
        if "状态" in r:
            lines.append(f"- ⚠️ {r['状态']}")
        else:
            lines.append(f"- 本地 {r['本地期数']} / 源 {r['数据源期数']} / 共同 {r['共同期数']}；不一致 {len(r['号码不一致'])}")
            for b in r["号码不一致"][:30]:
                lines.append(f"  - 期{b['期号']} 本地={b['本地']} 源={b['数据源']}")
        lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n不一致合计 {total_bad} 期 → {out_md.name}")


if __name__ == "__main__":
    main()
