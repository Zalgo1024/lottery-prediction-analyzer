"""
数字型彩种：销售额 + 各奖级中奖注数 抓取（整合方案任务 1.1）

数据源（2026-08-25 实测确认）：
- 排列3 / 排列5 / 七星彩：体彩官方接口 webapi.sporttery.cn（权威，字段完整）
    gameNo：排列3=35、排列5=350133、七星彩=04
    URL: https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry
         ?gameNo={gno}&provinceId=0&pageSize=100&isVerify=1&pageNo={page}
    字段：lotteryDrawNum(期号) / lotteryDrawTime(开奖日期) / totalSaleAmount(销售额,千分位)
         / poolBalance(奖池) / prizeLevelList[{prizeLevel, stakeCount(注数), stakeAmount(单注奖金)}]
- 福彩3D：500.com datachart（销售额 + 直选注数 + 组选3/组选6）
    URL: https://datachart.500.com/sd/history/inc/history.php?limit=10000
    列: 期号,中奖号码,总和,总销售额(元),直选注数,直选奖金(元),
        组选3注数,组选3奖金(元),组选6注数,组选6奖金(元),开奖日期

  ⚠️ 2026-08-30 数据质量复核（勿再被旧注释误导）：
  - 组选3/组选6 **注数**自 2022 年起全为 0/空（2021 年正常：组选3 24.5%、组选6 70.1%），
    源数据本身缺失，非解析问题。已核 cwl.gov.cn 官方接口 findDrawNotice 与详情页
    （prizegrades 全空、详情页无奖级表），**无可用替代源**。
  - 但**全项目无任何代码消费"组选3注数/组选6注数/组选3单注奖金/组选6单注奖金"四列**
    （crowd 模型 _signal_col 对 3D 类取的是"直选注数"，填充率 78.6% 健康；
     EV 走 rulebook 名义赔率）。且闸门已确认"固定赔率 EV 恒定"，
    故**该列缺失对 EV/结论零影响，不投入补全**。
  - 判据（用于识别"真缺失"vs"真实无人中奖"）：3D 类三位数有重号概率 = 1-720/1000 = 28%
    （此时才可能中组选3），全异 = 72%（此时才可能中组选6）。排列3 实测 27.8%/71.1%
    与该理论值吻合 → 排列3 数据健康；福彩3D 2022 起 0%/0% → 确为缺失。

输出：lottery_data/{彩种}销售奖级数据.csv（utf-8-sig，动态奖级列）
  通用列: 期号, 开奖日期, 销售额, 奖池
  奖级列（动态，按源数据出现顺序）: {奖级名}注数, {奖级名}单注奖金, ...

原则：接口字段缺失/变更 → 明确报错或列留空，绝不硬编、不写脏数据。
"""
import csv
import json
import logging
import re
import ssl
import time
import urllib.request
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_DIR = BASE_DIR / "lottery_data"  # E:\707\lottery_data

_HEADERS_TIYU = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Referer": "https://static.sporttery.cn/",
}
_HEADERS_500 = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120 Safari/537.36"),
    "Referer": "https://datachart.500.com/",
}
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE

# 体彩官方接口 gameNo 表
TIYU_SOURCES = {
    "排列3": {"game_no": "35"},
    "排列5": {"game_no": "350133"},
    "七星彩": {"game_no": "04"},
}

# 福彩3D：500.com
SD_SOURCES = {"福彩3D": {"code": "sd"}}

_ALL_NAMES = ["排列5", "福彩3D", "排列3", "七星彩"]  # 与 config.LOTTERY_CONFIG 键一致


# ============================================================
# 基础工具
# ============================================================
def _fetch(url: str, headers: dict, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout, context=_CTX) as resp:
        return resp.read()


def _decode(raw: bytes) -> str:
    for enc in ("utf-8", "gbk", "gb2312", "gb18030"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return raw.decode("latin-1", errors="replace")


def _num(s):
    """千分位/空格/特殊占位 → float 或 None（缺数据）"""
    if s is None:
        return None
    s = str(s).strip()
    if s in ("", "-", "---", "—", "&nbsp;", "&nbsp"):
        return None
    s = s.replace(",", "").replace(" ", "").replace("&nbsp;", "").replace("&nbsp", "")
    if not s or not re.fullmatch(r"-?\d+(\.\d+)?", s):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _sales_path(name: str) -> Path:
    return OUT_DIR / f"{name}销售奖级数据.csv"


# ============================================================
# 体彩官方接口（排列3/排列5/七星彩）
# ============================================================
def fetch_tiyu_page(game_no: str, page_no: int, page_size: int = 100) -> dict:
    url = ("https://webapi.sporttery.cn/gateway/lottery/getHistoryPageListV1.qry"
           f"?gameNo={game_no}&provinceId=0&pageSize={page_size}&isVerify=1&pageNo={page_no}")
    raw = _fetch(url, _HEADERS_TIYU)
    j = json.loads(_decode(raw))
    if not j.get("success"):
        raise RuntimeError(f"体彩接口返回失败: {j.get('errorMessage') or j.get('errorMsg') or j}")
    return j


def parse_tiyu_page(j: dict) -> list:
    """解析一页体彩 JSON → [{期号, 开奖日期, 销售额, 奖池, {奖级名: (注数, 单注奖金)}}]"""
    records = []
    for r in j["value"].get("list", []):
        issue = str(r.get("lotteryDrawNum", "")).strip()
        if not issue:
            continue
        prizes = {}
        for p in r.get("prizeLevelList") or []:
            name = str(p.get("prizeLevel", "")).strip()
            if not name:
                continue
            prizes[name] = (_num(p.get("stakeCount")), _num(p.get("stakeAmount")))
        records.append({
            "期号": issue,
            "开奖日期": str(r.get("lotteryDrawTime", "")).strip()[:10],
            "销售额": _num(r.get("totalSaleAmount")),
            "奖池": _num(r.get("poolBalance")),
            "奖级": prizes,
        })
    return records


# ============================================================
# 500.com（福彩3D）
# ============================================================
def fetch_sd_records(limit: int = 10000) -> list:
    """抓 500.com 福彩3D 历史 → [{期号, 开奖日期, 销售额, 奖池(None), 奖级}]"""
    url = f"https://datachart.500.com/sd/history/inc/history.php?limit={limit}"
    html = _decode(_fetch(url, _HEADERS_500))
    records = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S):
        tr = re.sub(r"<!--.*?-->", "", tr)  # 去掉注释里的假 <td>
        tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        cells = [re.sub(r"<[^>]+>", "", c).strip() for c in tds]
        cells = [c for c in cells if c != ""]
        if len(cells) < 9:
            continue
        if not re.fullmatch(r"\d{4,7}", cells[0] or ""):
            continue
        # 列: 期号,中奖号码,总和,总销售额,直选注数,直选奖金,组选3注数,组选3奖金,组选6注数,组选6奖金,开奖日期
        def _cell(i):
            v = cells[i] if i < len(cells) else ""
            return None if v in ("", "&nbsp;", "&nbsp") else _num(v)
        prizes = {
            "直选": (_cell(4), _cell(5)),
            "组选3": (_cell(6), _cell(7)),
            "组选6": (_cell(8), _cell(9)),
        }
        records.append({
            "期号": cells[0],
            "开奖日期": cells[-1] if len(cells) >= 11 else "",
            "销售额": _cell(3),
            "奖池": None,  # 500.com 3D 页不含奖池
            "奖级": prizes,
        })
    if not records:
        raise RuntimeError("500.com 福彩3D 未解析到任何数据（接口可能变更）")
    return records


# ============================================================
# 核心：增量更新
# ============================================================
def _load_local(path: Path) -> list:
    """读本地 CSV → [{列名: 值}]（值全部保持字符串/None）"""
    if not path.exists():
        return []
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        r = csv.reader(f)
        header = next(r, None)
        if not header:
            return []
        for line in r:
            if not line:
                continue
            row = {}
            for i, h in enumerate(header):
                v = line[i] if i < len(line) else ""
                row[h] = None if v in ("", "&nbsp;", "&nbsp; ") else v
            rows.append(row)
    return rows


def _to_flat_rows(records: list) -> list:
    """[{期号,开奖日期,销售额,奖池,奖级:{奖级名:(注数,奖金)}}] → [{列: 值}]"""
    rows = []
    for rec in records:
        row = {
            "期号": rec["期号"],
            "开奖日期": rec["开奖日期"],
            "销售额": "" if rec["销售额"] is None else str(int(rec["销售额"]) if float(rec["销售额"]).is_integer() else rec["销售额"]),
            "奖池": "" if rec["奖池"] is None else str(rec["奖池"]),
        }
        for name, (cnt, amt) in rec["奖级"].items():
            row[f"{name}注数"] = "" if cnt is None else str(int(cnt))
            row[f"{name}单注奖金"] = "" if amt is None else str(int(amt))
        rows.append(row)
    return rows


def _write_csv(path: Path, rows: list):
    """rows: [{列: 值}]，列集合 = 按 通用列 + 奖级列出现顺序"""
    if not rows:
        raise RuntimeError("无可写数据")
    # 列顺序：通用列固定，奖级列按首次出现顺序
    fixed = ["期号", "开奖日期", "销售额", "奖池"]
    prize_cols = []
    for row in rows:
        for k in row.keys():
            if k not in fixed and k not in prize_cols:
                prize_cols.append(k)
    header = fixed + prize_cols
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow([row.get(h, "") for h in header])


def _merge_by_issue(rows: list) -> list:
    """按期号去重合并（保留后出现者），按期号升序"""
    by_issue = {}
    for row in rows:
        by_issue[row["期号"]] = row
    return [by_issue[k] for k in sorted(by_issue.keys(), key=lambda s: (len(s), s))]


def update_sales_data(name: str) -> dict:
    """
    增量更新 {彩种} 销售奖级数据。
    返回: {"lottery", "latest_before", "latest_after", "fetched", "new_records", "message"}
    """
    if name in TIYU_SOURCES:
        return _update_tiyu(name)
    if name in SD_SOURCES:
        return _update_sd(name)
    return {"error": f"不支持的彩种: {name}（仅支持 {_ALL_NAMES}）"}


def _update_tiyu(name: str) -> dict:
    gno = TIYU_SOURCES[name]["game_no"]
    path = _sales_path(name)
    local = _load_local(path)
    local_issues = {r["期号"] for r in local}
    latest_before = max(local_issues, key=lambda s: (len(s), s)) if local_issues else ""

    # 逐页拉取，遇到"整页都已在本地"即停（增量）
    new_records = []
    page = 1
    while True:
        j = fetch_tiyu_page(gno, page)
        records = parse_tiyu_page(j)
        if not records:
            break
        unknown = [r for r in records if r["期号"] not in local_issues]
        if not unknown:
            break  # 本页全部已知 → 已到本地最新处
        new_records.extend(unknown)
        local_issues.update(r["期号"] for r in unknown)
        total = j["value"].get("total", 0)
        if page * 100 >= total or len(records) < 100:
            break
        page += 1
        time.sleep(0.3)

    if not new_records:
        return {
            "lottery": name, "latest_before": latest_before, "latest_after": latest_before,
            "fetched": 0, "new_records": [], "message": "已是最新，无需更新",
        }

    new_rows = _to_flat_rows(new_records)
    merged = _merge_by_issue(local + new_rows)
    _write_csv(path, merged)
    latest_after = merged[-1]["期号"] if merged else latest_before
    logger.info(f"销售数据更新: {name} 新增 {len(new_rows)} 期，最新 {latest_after}")
    return {
        "lottery": name, "latest_before": latest_before, "latest_after": latest_after,
        "fetched": len(new_rows), "new_records": new_rows[:3],
        "message": f"新增 {len(new_rows)} 期",
    }


def _update_sd(name: str) -> dict:
    path = _sales_path(name)
    local = _load_local(path)
    local_issues = {r["期号"] for r in local}
    latest_before = max(local_issues, key=lambda s: (len(s), s)) if local_issues else ""

    records = fetch_sd_records()
    new_records = [r for r in records if r["期号"] not in local_issues]
    if not new_records:
        return {
            "lottery": name, "latest_before": latest_before, "latest_after": latest_before,
            "fetched": 0, "new_records": [], "message": "已是最新，无需更新",
        }
    new_rows = _to_flat_rows(new_records)
    merged = _merge_by_issue(local + new_rows)
    _write_csv(path, merged)
    latest_after = merged[-1]["期号"] if merged else latest_before
    logger.info(f"销售数据更新: {name} 新增 {len(new_rows)} 期，最新 {latest_after}")
    return {
        "lottery": name, "latest_before": latest_before, "latest_after": latest_after,
        "fetched": len(new_rows), "new_records": new_rows[:3],
        "message": f"新增 {len(new_rows)} 期",
    }


# ============================================================
# 读取
# ============================================================
def load_sales(name: str):
    """
    读取 {彩种} 销售奖级数据 → DataFrame（期号 str，数值列 float，缺失为 NaN）。
    无数据返回 None。
    """
    path = _sales_path(name)
    if not path.exists():
        return None
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"期号": str})
    for col in df.columns:
        if col not in ("期号", "开奖日期"):
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_sales_summary(name: str) -> dict:
    """简要统计（供 CLI / Web）"""
    df = load_sales(name)
    if df is None or df.empty:
        return {"name": name, "has_data": False, "message": "无销售数据文件，请先运行 python cli.py sales"}
    prize_cols = [c for c in df.columns if c.endswith("注数") or c.endswith("单注奖金")]
    return {
        "name": name,
        "has_data": True,
        "records": len(df),
        "latest_issue": str(df["期号"].iloc[-1]),
        "latest_date": str(df["开奖日期"].iloc[-1]),
        "sales_latest": None if pd.isna(df["销售额"].iloc[-1]) else float(df["销售额"].iloc[-1]),
        "prize_columns": prize_cols,
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    targets = sys.argv[1:] or _ALL_NAMES
    for t in targets:
        try:
            r = update_sales_data(t)
            print(f"[{r.get('lottery', t)}] {r.get('message')} 最新={r.get('latest_after')}")
        except Exception as e:
            print(f"[{t}] [失败] {e}")
