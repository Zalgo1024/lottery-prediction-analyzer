"""
从 500 彩票网拉取新彩种全量历史，整理为项目统一 CSV。

已验证可用（2026-08-10 探测）：
- 七星彩 qxc : datachart inc/history.php?start=04001&end=99999 -> 全量
  （end 必须足够大；25199 会在 2025 年底被截断，导致最新数据缺失）
- 排列5  plw : datachart inc/history.php?limit=10000          -> 全量(~7688)
- 福彩3D sd  : datachart inc/history.php?limit=10000          -> 全量(~7723)
- 排列3  pls : datachart inc/history.php?limit=10000          -> 全量(赠品)

说明：快乐8(kl8) 在 500 仅有走势图、无历史接口；排列7(pl7) 500 全站无。
这两个本脚本不处理，需另找数据源。

输出 CSV 列：期号, 开奖日期, 号码1..号码N  （单区、有序，适配通用 zones）

增量更新：
    update_digital_lottery_data(name) 会拉取全量并合并本地 CSV，只追加本地缺失的新期号。
"""
import urllib.request
import urllib.error
import re
import csv
import sys
import time
from pathlib import Path

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120 Safari/537.36",
}

BASE = "https://datachart.500.com"

# name -> (code, url_template, choose, 备注)
SOURCES = {
    "七星彩": ("qxc", f"{BASE}/qxc/history/inc/history.php?start=04001&end=99999", 7, "体彩7星彩"),
    "排列5": ("plw", f"{BASE}/plw/history/inc/history.php?limit=10000", 5, "体彩排列5"),
    "福彩3D": ("sd", f"{BASE}/sd/history/inc/history.php?limit=10000", 3, "福彩3D"),
    "排列3": ("pls", f"{BASE}/pls/history/inc/history.php?limit=10000", 3, "体彩排列3(赠品)"),
}

OUT_DIR = Path(__file__).resolve().parent.parent / "lottery_data"  # E:\707\lottery_data

def fetch(url, timeout=30, ref=None):
    import ssl
    h = dict(HEADERS)
    if ref:
        h["Referer"] = ref
    req = urllib.request.Request(url, headers=h)
    # 绕过 SSL 证书验证：500.com 证书链有时不完整（与 data.fetcher._CTX 一致）
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
        return resp.read()

def decode(raw):
    for enc in ("gbk", "gb2312", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("latin-1", errors="replace")

def parse_rows(html):
    """抽取所有数据行：返回 [(期号, 开奖日期, [号码...]), ...]"""
    out = []
    # 找到含 期号 表头的表格，取其后所有 <tr>
    for table in re.findall(r"<table[^>]*>(.*?)</table>", html, re.S):
        if "期号" not in table:
            continue
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) < 2:
                continue
            cells = [re.sub(r"<[^>]+>", "", c).strip() for c in tds]
            cells = [c for c in cells if c != ""]
            if not cells:
                continue
            # 期号：首个纯数字单元格
            期号 = None
            for c in cells:
                if re.fullmatch(r"\d{4,7}", c):
                    期号 = c
                    break
            if 期号 is None:
                continue
            # 开奖日期：首个 YYYY-MM-DD
            日期 = None
            for c in cells:
                if re.fullmatch(r"\d{4}-\d{2}-\d{2}", c):
                    日期 = c
                    break
            # 中奖号码：含空格分隔数字的单元格
            号码 = None
            for c in cells:
                m = re.fullmatch(r"(?:\d{1,2}\s+)+\d{1,2}", c)
                if m:
                    号码 = [int(x) for x in c.split()]
                    break
            if 号码 is None:
                continue
            out.append((期号, 日期 or "", 号码))
    return out

def fetch_one(name):
    code, url, choose, note = SOURCES[name]
    print(f"[拉取] {name} ({note}) code={code}")
    raw = fetch(url, ref=f"{BASE}/{code}/history/history.shtml")
    html = decode(raw)
    rows = parse_rows(html)
    # 去重（按 期号），按 期号 升序
    seen = {}
    for 期号, 日期, 号码 in rows:
        if len(号码) != choose:
            continue
        seen[期号] = (期号, 日期, 号码)
    rows = sorted(seen.values(), key=lambda r: r[0])
    if not rows:
        raise RuntimeError(f"{name} 未解析到任何数据行（接口可能变更）")
    # 写 CSV
    out_path = OUT_DIR / f"{name}历史数据.csv"
    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["期号", "开奖日期"] + [f"号码{i+1}" for i in range(choose)])
        for 期号, 日期, 号码 in rows:
            w.writerow([期号, 日期] + 号码)
    print(f"    成功：{len(rows)} 期，保存 -> {out_path.name}")
    print(f"    样例(最早): {rows[0]}")
    print(f"    样例(最新): {rows[-1]}")
    return len(rows)

def update_digital_lottery_data(name):
    """
    数字型彩种增量更新：拉取远端全量，与本地 CSV 合并，只追加新期号。
    返回与 data.fetcher.update_lottery_data 兼容的字典。
    """
    if name not in SOURCES:
        return {"error": f"不支持的数字型彩种: {name}"}

    code, url, choose, note = SOURCES[name]
    out_path = OUT_DIR / f"{name}历史数据.csv"

    # 读取本地最新期号
    latest_before = ""
    local_rows = []
    if out_path.exists():
        with open(out_path, encoding="utf-8-sig", newline="") as f:
            r = csv.reader(f)
            header = next(r, None)
            for row in r:
                if len(row) >= 2 + choose:
                    local_rows.append(row)
        if local_rows:
            latest_before = max(row[0] for row in local_rows)

    # 拉取远端
    raw = fetch(url, ref=f"{BASE}/{code}/history/history.shtml")
    html = decode(raw)
    rows = parse_rows(html)

    seen = {}
    for 期号, 日期, 号码 in rows:
        if len(号码) != choose:
            continue
        seen[期号] = (期号, 日期, 号码)
    remote_rows = sorted(seen.values(), key=lambda r: r[0])
    if not remote_rows:
        return {"error": f"{name} 未解析到任何数据行（接口可能变更）"}

    # 只保留本地没有的新期号
    local_issues = {row[0] for row in local_rows}
    new_rows = [(期号, 日期, 号码) for 期号, 日期, 号码 in remote_rows if 期号 not in local_issues]

    if not new_rows:
        return {
            "lottery": name,
            "latest_before": latest_before,
            "latest_after": latest_before,
            "fetched": 0,
            "new_records": [],
            "message": "已是最新，无需更新",
            "cleaned": False,
        }

    # 合并并按期号升序保存（与 fetch_one 保持一致，load_lottery 会再统一降序）
    merged = local_rows + [[期号, 日期] + 号码 for 期号, 日期, 号码 in new_rows]
    # 去重并排序
    merged_by_issue = {}
    for row in merged:
        if row[0] not in merged_by_issue:
            merged_by_issue[row[0]] = row
    merged = sorted(merged_by_issue.values(), key=lambda r: r[0])

    with open(out_path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["期号", "开奖日期"] + [f"号码{i+1}" for i in range(choose)])
        for row in merged:
            w.writerow(row)

    latest_after = merged[-1][0] if merged else latest_before
    return {
        "lottery": name,
        "latest_before": latest_before,
        "latest_after": latest_after,
        "fetched": len(new_rows),
        "new_records": [[期号, 日期] + 号码 for 期号, 日期, 号码 in new_rows],
        "message": f"新增 {len(new_rows)} 期",
        "cleaned": False,
    }


def main():
    targets = sys.argv[1:] or list(SOURCES.keys())
    for name in targets:
        if name not in SOURCES:
            print(f"  跳过未知彩种: {name}")
            continue
        try:
            fetch_one(name)
        except Exception as e:
            print(f"    [失败] {name}: {e}")
        time.sleep(0.6)


if __name__ == "__main__":
    main()
