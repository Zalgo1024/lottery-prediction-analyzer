"""号码数据完整性扫描（只读，不改任何数据）

检查项：
  A. 期号连续性（按年分组，年内序号跳过 = 真缺期，需补抓）
  B. 开奖日期：可解析性 / 与期号年份一致 / 时序单调
  C. 开奖日历：日期所属星期是否符合该彩种 draw_days（历史调休/挪期会提示）
  D. 重复期号
  E. 号码合法性：数量、范围、乐透红球区内不重复、空值
  F. 销售奖级数据与历史数据的期号对齐（缺/多）
  G. 数据滞后：最新一期 vs 最近应开奖日

输出：控制台摘要 + logs/数据体检_<日期>.md + .json
用法：E:/Python/python.exe scripts/scan_data_integrity.py
"""

import csv
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from config import LOTTERY_CONFIG, LOTTERY_DATA_DIR  # noqa: E402

try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

# 优先用 cleaned（UTF-8）；原始文件（GBK）仅在 cleaned 不存在时回退
HIST_FILES = {
    "双色球": ["双色球历史数据_cleaned.csv", "双色球历史数据.csv"],
    "大乐透": ["大乐透历史数据_cleaned.csv", "大乐透历史数据.csv"],
    "排列5": ["排列5历史数据.csv"],
    "福彩3D": ["福彩3D历史数据.csv"],
    "排列3": ["排列3历史数据.csv"],
    "七星彩": ["七星彩历史数据.csv"],
}

SALES_FILES = {
    "双色球": None,  # 双色球销量在历史文件里（总投注额列）
    "大乐透": None,
    "排列5": "排列5销售奖级数据.csv",
    "福彩3D": "福彩3D销售奖级数据.csv",
    "排列3": "排列3销售奖级数据.csv",
    "七星彩": "七星彩销售奖级数据.csv",
}

# 七星彩第 7 位规则变更：2020-10-11 起 0-14，此前 0-9
QXC_ZONE7_CHANGE = date(2020, 10, 11)

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def read_csv_any(path: Path):
    """按编码回退读取，返回 (rows, encoding)"""
    for enc in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            with open(path, encoding=enc, errors="strict") as f:
                rows = list(csv.DictReader(f))
            if rows and rows[0]:
                return rows, enc
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f)), "replace"


def pick_hist_file(name: str):
    for fn in HIST_FILES[name]:
        p = LOTTERY_DATA_DIR / fn
        if p.exists():
            return p
    return None


def parse_issue(issue: str, name: str):
    """解析期号 -> (year, seq)。福彩3D 为 7 位 YYYYxxx，其余 5 位 YYxxx。"""
    s = str(issue).strip()
    if not s.isdigit():
        return None, None
    if len(s) == 7:
        return int(s[:4]), int(s[4:])
    if len(s) == 5:
        yy = int(s[:2])
        return 2000 + yy, int(s[2:])
    if len(s) == 4:
        # 前导零丢失：03001 → "3001"、09154 → "9154"（CSV 被当数字写丢首位 0）。
        # 首位即年份个位（双色球 2003 起、大乐透 2007 起），据此还原。
        return 2000 + int(s[0]), int(s[1:])
    return None, None


def parse_date(v: str):
    v = (v or "").strip()
    # 注意：乐透型 CSV 用 DD/MM/YYYY（如 06/09/2026 = 2026-09-06），
    # 数字型用 YYYY-MM-DD。两种都要认，否则整列被判为"无法解析"。
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%Y年%m月%d日"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def num_cols(name: str, row: dict):
    """取出该行的号码列值（原始字符串列表），按分区组织"""
    cfg = LOTTERY_CONFIG[name]
    keys = list(row.keys())
    out = []
    if "红球1" in keys:  # 乐透型
        z = 0
        while f"红球{z+1}" in keys:
            out.append((f"红球{z+1}", row.get(f"红球{z+1}")))
            z += 1
        z = 0
        while f"蓝球{z+1}" in keys:
            out.append((f"蓝球{z+1}", row.get(f"蓝球{z+1}")))
            z += 1
    else:  # 数字型
        z = 0
        while f"号码{z+1}" in keys:
            out.append((f"号码{z+1}", row.get(f"号码{z+1}")))
            z += 1
    return out


def scan_lottery(name: str, today: date):
    rep = {"彩种": name, "问题": []}
    path = pick_hist_file(name)
    if path is None:
        rep["问题"].append({"级别": "严重", "项": "文件缺失", "说明": "找不到历史数据文件"})
        return rep

    rows, enc = read_csv_any(path)
    rep["文件"] = path.name
    rep["编码"] = enc
    rep["行数"] = len(rows)
    if not rows:
        rep["问题"].append({"级别": "严重", "项": "空文件", "说明": str(path)})
        return rep

    cfg = LOTTERY_CONFIG[name]
    draw_days = cfg.get("draw_days", [])

    # ---- 期号 / 日期 / 号码解析 ----
    recs = []
    bad_issue, bad_date = [], []
    for r in rows:
        issue = (r.get("期号") or "").strip()
        y, seq = parse_issue(issue, name)
        if y is None:
            bad_issue.append(issue)
            continue
        d = parse_date(r.get("开奖日期"))
        if d is None:
            bad_date.append({"期号": issue, "开奖日期": r.get("开奖日期")})
        recs.append({"期号": issue, "year": y, "seq": seq, "date": d, "row": r})

    if bad_issue:
        rep["问题"].append({
            "级别": "严重", "项": "期号无法解析", "数量": len(bad_issue),
            "样例": bad_issue[:10],
        })
    if bad_date:
        rep["问题"].append({
            "级别": "严重", "项": "开奖日期无法解析", "数量": len(bad_date),
            "样例": bad_date[:10],
        })

    # ---- A. 期号连续性（年内序号）----
    by_year = defaultdict(list)
    for r in recs:
        by_year[r["year"]].append(r["seq"])
    missing_issue = []
    for y in sorted(by_year):
        seqs = sorted(set(by_year[y]))
        if not seqs:
            continue
        # 起点用该年实际最小序号：七星彩 2004 年从 101 期起编（04101），
        # 若按 1 起算会凭空造出 100 个"缺期"假警报。
        full = set(range(seqs[0], seqs[-1] + 1))
        gaps = sorted(full - set(seqs))
        if gaps:
            prefix = "20" if len(str(y)) == 4 else ""
            missing_issue.append({
                "年": y, "应有": seqs[-1], "实有": len(seqs),
                "缺号数": len(gaps),
                "缺号": [f"{y}{s:03d}" if y < 2010 or len(str(y)) == 4 else f"{str(y)[2:]}{s:03d}" for s in gaps[:40]],
            })
    rep["缺期总数"] = sum(m["缺号数"] for m in missing_issue)
    rep["缺期明细"] = missing_issue
    if missing_issue:
        rep["问题"].append({
            "级别": "严重", "项": "期号不连续（真缺期）",
            "数量": rep["缺期总数"],
            "说明": "官方期号休市不跳号，缺号=本地数据缺失，需补抓",
        })

    # ---- D. 重复期号 ----
    cnt = Counter(r["期号"] for r in recs)
    dups = {k: v for k, v in cnt.items() if v > 1}
    rep["重复期号"] = dups
    if dups:
        rep["问题"].append({
            "级别": "严重", "项": "重复期号", "数量": len(dups),
            "样例": list(dups.items())[:10],
        })

    # ---- B/C. 日期 ----
    dated = [r for r in recs if r["date"]]
    year_mismatch = [{"期号": r["期号"], "开奖日期": str(r["date"])}
                     for r in dated if r["date"].year != r["year"]]
    rep["期号年份与日期不符"] = year_mismatch[:20]
    if year_mismatch:
        rep["问题"].append({
            "级别": "警告", "项": "期号年份与开奖日期年份不一致",
            "数量": len(year_mismatch), "样例": year_mismatch[:5],
        })

    # 时序方向
    seq_dates = [r["date"] for r in recs if r["date"]]
    if seq_dates:
        rep["方向"] = "新→旧" if seq_dates[0] > seq_dates[-1] else "旧→新"
        asc = sorted(seq_dates)
        rep["日期范围"] = [str(asc[0]), str(asc[-1])]
        # 重复日期
        dcnt = Counter(seq_dates)
        dup_dates = {str(k): v for k, v in dcnt.items() if v > 1}
        rep["重复开奖日"] = dup_dates
        if dup_dates:
            # 期号连续的同日多期 = 历史补开奖（正常），期号相同才是真重复
            same_issue_dup = any(
                len({r["期号"] for r in recs if r["date"] == d}) < n
                for d, n in dcnt.items() if n > 1
            )
            rep["问题"].append({
                "级别": "严重" if same_issue_dup else "提示",
                "项": "同一期号重复" if same_issue_dup else "同日多期（补开奖，非重复）",
                "数量": len(dup_dates),
                "样例": {str(k): sorted(r["期号"] for r in recs if r["date"] == k)
                         for k, v in list(dup_dates.items())[:5]},
            })

        # 星期是否符合 draw_days（仅提示级）
        if draw_days and len(draw_days) < 7:
            off = []
            for r in dated:
                if r["date"].weekday() not in draw_days:
                    off.append({"期号": r["期号"], "日期": str(r["date"]),
                                "星期": WEEKDAY_CN[r["date"].weekday()]})
            rep["非开奖日记录"] = len(off)
            rep["非开奖日样例"] = off[:10]
            if off:
                rep["问题"].append({
                    "级别": "提示", "项": "开奖日期不在该彩种常规开奖日",
                    "数量": len(off),
                    "说明": "多为春节/国庆调休挪期或历史规则调整，需人工确认",
                })

        # 相邻间隔异常（> 2 倍常规间隔），用于发现长假之外的空档
        gaps = []
        for a, b in zip(asc, asc[1:]):
            delta = (b - a).days
            if delta > 10:
                gaps.append({"起": str(a), "止": str(b), "间隔天": delta})
        rep["超长空档(>10天)"] = gaps
        if gaps:
            rep["问题"].append({
                "级别": "提示", "项": "相邻开奖间隔超过 10 天",
                "数量": len(gaps), "样例": gaps[:5],
                "说明": "春节休市属正常；若期号同时不连续则为真缺期",
            })

    # ---- E. 号码合法性 ----
    zones = cfg.get("zones", [])
    bad_num = []
    empty_num = []
    dup_in_zone = []
    zone7_rule = []
    n_checked = 0
    for r in recs:
        cols = num_cols(name, r["row"])
        if not cols:
            continue
        n_checked += 1
        # 期望列数 = 各分区 choose 之和（双色球 6+1=7、大乐透 5+2=7、七星彩 7）
        expect = sum(z["choose"] for z in zones)
        if len(cols) != expect:
            bad_num.append({"期号": r["期号"],
                            "说明": f"号码列数 {len(cols)} != 应有 {expect}"})
            continue
        # 列→分区映射：按每个分区的 choose 数顺序切分
        # （红球 6 列全归红球区、蓝球 1 列归蓝球区；数字型每位一个区）
        col_zone = []
        idx = 0
        for zone in zones:
            for _ in range(zone["choose"]):
                if idx < len(cols):
                    col_zone.append((cols[idx][0], cols[idx][1], zone))
                idx += 1
        for col, val, zone in col_zone:
            v = (val or "").strip()
            if v == "":
                empty_num.append({"期号": r["期号"], "列": col})
                continue
            try:
                n = int(v)
            except ValueError:
                bad_num.append({"期号": r["期号"], "列": col, "值": v, "说明": "非数字"})
                continue
            lo, hi = zone["min"], zone["max"]
            # 七星彩第 7 位历史段 0-9
            if name == "七星彩" and col == "号码7" and r["date"] and r["date"] < QXC_ZONE7_CHANGE:
                hi = 9
            if not (lo <= n <= hi):
                bad_num.append({"期号": r["期号"], "列": col, "值": n,
                                "说明": f"超出范围 {lo}-{hi}"})
                if name == "七星彩" and col == "号码7":
                    zone7_rule.append({"期号": r["期号"], "值": n, "日期": str(r["date"])})
        # 乐透型：红球区内不重复
        if "红球1" in r["row"]:
            for tag in ("红球", "蓝球"):
                vals = [r["row"].get(f"{tag}{i+1}") for i in range(10)]
                vals = [v for v in vals if v not in (None, "")]
                if len(vals) != len(set(vals)):
                    dup_in_zone.append({"期号": r["期号"], "区": tag,
                                        "值": vals})

    rep["号码检查行数"] = n_checked
    rep["号码越界/非法"] = len(bad_num)
    rep["号码非法样例"] = bad_num[:10]
    if bad_num:
        rep["问题"].append({"级别": "严重", "项": "号码越界或非数字",
                            "数量": len(bad_num), "样例": bad_num[:5]})
    rep["号码空值"] = len(empty_num)
    if empty_num:
        rep["问题"].append({"级别": "严重", "项": "号码列为空",
                            "数量": len(empty_num), "样例": empty_num[:5]})
    if dup_in_zone:
        rep["问题"].append({"级别": "严重", "项": "同区号码重复",
                            "数量": len(dup_in_zone), "样例": dup_in_zone[:5]})
    if zone7_rule:
        rep["问题"].append({
            "级别": "提示", "项": "七星彩第7位越界（疑似规则段混用）",
            "数量": len(zone7_rule), "样例": zone7_rule[:5],
        })

    # ---- F. 销售奖级数据对齐 ----
    sf = SALES_FILES.get(name)
    if sf:
        p = LOTTERY_DATA_DIR / sf
        if p.exists():
            srows, _ = read_csv_any(p)
            sissues = {(r.get("期号") or "").strip() for r in srows}
            hissues = {r["期号"] for r in recs}
            only_h = sorted(hissues - sissues)
            only_s = sorted(sissues - hissues)
            rep["销售数据"] = {"文件": sf, "行数": len(srows),
                               "历史有销售无": len(only_h), "销售有历史无": len(only_s),
                               "样例_历史有销售无": only_h[:10]}
            if only_h or only_s:
                rep["问题"].append({
                    "级别": "警告", "项": "销售奖级数据与历史数据期号不对齐",
                    "历史有销售无": len(only_h), "销售有历史无": len(only_s),
                })
        else:
            rep["销售数据"] = {"文件": sf, "状态": "缺失"}

    # ---- G. 滞后 ----
    if dated:
        latest = max(r["date"] for r in dated)
        rep["最新一期"] = {"日期": str(latest),
                           "期号": max(recs, key=lambda x: (x["date"] or date.min))["期号"]}
        # 最近应开奖日（不含今天之后）
        d = today
        while d >= latest:
            if not draw_days or d.weekday() in draw_days:
                if d <= today:
                    break
            d -= timedelta(days=1)
        expected = d
        lag_days = (today - latest).days
        rep["滞后天数"] = lag_days
        miss = []
        if lag_days >= 1:
            cur = latest + timedelta(days=1)
            # 只统计"昨天及以前"：今天若还没到开奖时刻不算缺失
            while cur < today:
                if (not draw_days) or cur.weekday() in draw_days:
                    miss.append(str(cur))
                cur += timedelta(days=1)
        rep["疑似未抓取的开奖日"] = miss
        if miss:
            rep["问题"].append({
                "级别": "警告", "项": "按开奖日历应有但本地缺失的日期",
                "数量": len(miss), "样例": miss[:10],
                "说明": "含法定节假日休市，需与期号缺号对照判断",
            })
    return rep


def main():
    today = date.today()
    reports = [scan_lottery(n, today) for n in LOTTERY_CONFIG]

    print("=" * 78)
    print(f"号码数据完整性扫描   {today}")
    print("=" * 78)
    for r in reports:
        name = r["彩种"]
        print(f"\n【{name}】{r.get('文件', '-')}  行数={r.get('行数', 0)}  "
              f"编码={r.get('编码', '-')}  顺序={r.get('方向', '-')}")
        if "日期范围" in r:
            print(f"  日期范围: {r['日期范围'][0]} ~ {r['日期范围'][1]}")
        if "最新一期" in r:
            print(f"  最新: {r['最新一期']['期号']} @ {r['最新一期']['日期']}  滞后 {r['滞后天数']} 天")
        print(f"  缺期(期号不连续): {r.get('缺期总数', 0)}")
        print(f"  号码非法/越界: {r.get('号码越界/非法', 0)}   空值: {r.get('号码空值', 0)}   "
              f"重复期号: {len(r.get('重复期号', {}))}")
        if r.get("非开奖日记录"):
            print(f"  非常规开奖日记录: {r['非开奖日记录']}")
        if r.get("销售数据"):
            sd = r["销售数据"]
            if "行数" in sd:
                print(f"  销售数据对齐: 历史有销售无={sd['历史有销售无']} 销售有历史无={sd['销售有历史无']}")
        probs = r.get("问题", [])
        if probs:
            print("  问题:")
            for p in probs:
                print(f"    [{p['级别']}] {p['项']}" + (f" × {p['数量']}" if "数量" in p else ""))
        else:
            print("  问题: 无")

    out_dir = BASE / "logs"
    out_dir.mkdir(exist_ok=True)
    stamp = today.strftime("%Y%m%d")
    md = out_dir / f"数据体检_{stamp}.md"
    js = out_dir / f"数据体检_{stamp}.json"
    with open(js, "w", encoding="utf-8") as f:
        json.dump(reports, f, ensure_ascii=False, indent=2, default=str)

    lines = [f"# 号码数据体检报告 {today}", "",
             "| 彩种 | 行数 | 日期范围 | 最新期号 | 滞后 | 缺期 | 号码非法 | 空值 | 重复期号 |",
             "|---|---|---|---|---|---|---|---|---|"]
    for r in reports:
        rng = "~".join(r["日期范围"]) if "日期范围" in r else "-"
        lines.append(
            f"| {r['彩种']} | {r.get('行数', 0)} | {rng} | "
            f"{r.get('最新一期', {}).get('期号', '-')} | {r.get('滞后天数', '-')} 天 | "
            f"{r.get('缺期总数', 0)} | {r.get('号码越界/非法', 0)} | {r.get('号码空值', 0)} | "
            f"{len(r.get('重复期号', {}))} |"
        )
    for r in reports:
        lines.extend(["", f"## {r['彩种']}", ""])
        if r.get("问题"):
            for p in r["问题"]:
                lines.append(f"- [{p['级别']}] {p['项']}"
                             + (f"（{p['数量']} 处）" if "数量" in p else ""))
                if p.get("说明"):
                    lines.append(f"  - {p['说明']}")
                if p.get("样例"):
                    lines.append(f"  - 样例: `{json.dumps(p['样例'], ensure_ascii=False)[:400]}`")
        else:
            lines.append("- 无问题")
        for m in r.get("缺期明细", []):
            lines.append(f"  - {m['年']} 年：应有 {m['应有']} 期，实有 {m['实有']}，缺 {m['缺号数']} 期")
            lines.append(f"    - 缺号: {', '.join(m['缺号'][:40])}")
    with open(md, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"\n报告已写出: {md}")
    print(f"JSON: {js}")


if __name__ == "__main__":
    main()
