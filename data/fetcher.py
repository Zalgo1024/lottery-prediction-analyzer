"""
数据抓取模块：从 500.com 自动抓取最新开奖数据

用法：
    from data.fetcher import update_lottery_data
    result = update_lottery_data("双色球")

接口：
    https://datachart.500.com/ssq/history/newinc/history.php?start=XXXXX&end=XXXXX
    https://datachart.500.com/dlt/history/newinc/history.php?start=XXXXX&end=XXXXX

返回 HTML 表格，每行 17 列（双色球）/ 16 列（大乐透）：
    行号, 期号, 红球..., 蓝球..., [快乐星期天], 奖池奖金, 一等奖注数, 一等奖奖金, 二等奖注数, 二等奖奖金, 总投注额, 开奖日期

网页日期格式：2026-07-02（ISO），本地 CSV 格式：2/7/2026（d/m/yyyy）
"""

import csv
import logging
import re
import ssl
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config import BASE_DIR, LOTTERY_CONFIG, LOTTERY_DATA_DIR

logger = logging.getLogger(__name__)

# 500.com 数据接口（多端点回退，防止单个端点被拦截/改版导致整条抓取挂掉）
# 顺序：newinc（当前主力）→ inc（老接口）→ 用户给的 ?expect=all&from=&to= 历史页形式
_CODES = {"双色球": "ssq", "大乐透": "dlt"}
def _fetch_endpoints(lottery_name: str, start: int, end: int) -> List[str]:
    code = _CODES[lottery_name]
    return [
        f"https://datachart.500.com/{code}/history/newinc/history.php?start={start}&end={end}",
        f"https://datachart.500.com/{code}/history/inc/history.php?start={start}&end={end}",
        f"https://datachart.500.com/{code}/?expect=all&from={start:05d}&to={end:05d}&jumpsrc=https://datachart.500.com/{code}/",
    ]

# 本地 CSV 文件（保持与 config.py 一致，位于 LOTTERY_DATA_DIR）
CSV_FILES = {
    "双色球": LOTTERY_DATA_DIR / "双色球历史数据.csv",
    "大乐透": LOTTERY_DATA_DIR / "大乐透历史数据.csv",
    "排列5": LOTTERY_DATA_DIR / "排列5历史数据.csv",
    "福彩3D": LOTTERY_DATA_DIR / "福彩3D历史数据.csv",
    "排列3": LOTTERY_DATA_DIR / "排列3历史数据.csv",
    "七星彩": LOTTERY_DATA_DIR / "七星彩历史数据.csv",
}

# 数字型彩种走 500.com 通用接口
DIGITAL_LOTTERIES = {"排列5", "福彩3D", "排列3", "七星彩"}

# 原始 CSV 编码（GBK/GB18030）
CSV_ENCODING = "gb18030"

# 网页请求头
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Referer": "https://datachart.500.com/",
}

# 忽略 SSL 证书验证（500.com 证书链有时不完整）
_CTX = ssl.create_default_context()
_CTX.check_hostname = False
_CTX.verify_mode = ssl.CERT_NONE


# ============================================================
# 网络抓取
# ============================================================

def _fetch_page(url: str, referer: Optional[str] = None) -> str:
    """抓取网页内容，自动解码。UA/Referer 失败自动换一组重试，增强抗拦截能力。"""
    ua_list = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:121.0) Gecko/20100101 Firefox/121.0",
    ]
    ref_list = [referer, "https://datachart.500.com/", "https://www.500.com/"]
    last_err = None
    for ua in ua_list:
        for ref in ref_list:
            h = dict(_HEADERS)
            h["User-Agent"] = ua
            if ref:
                h["Referer"] = ref
            try:
                req = urllib.request.Request(url, headers=h)
                with urllib.request.urlopen(req, timeout=20, context=_CTX) as resp:
                    raw = resp.read()
                for enc in ("utf-8", "gbk", "gb18030"):
                    try:
                        return raw.decode(enc)
                    except UnicodeDecodeError:
                        continue
                return raw.decode("utf-8", errors="replace")
            except Exception as e:
                last_err = e
                continue
    raise last_err or RuntimeError("抓取失败（未知原因）")


def parse_html_rows(html: str) -> List[List[str]]:
    """
    解析 500.com 历史数据表格，返回数据行列表（每行是干净的字符串列表）
    表头行会被跳过（通过期号数字判断）
    """
    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html, re.S)
    result = []
    for row in rows:
        tds = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        clean = [re.sub(r"<[^>]+>", "", t).replace("&nbsp;", "").strip() for t in tds]
        # 数据行特征：第 2 列（索引1）是数字期号
        if len(clean) >= 15 and clean[1].isdigit():
            result.append(clean)
    return result


# ============================================================
# 格式转换
# ============================================================

def _convert_date_iso_to_csv(date_str: str) -> str:
    """网页日期 2026-07-02 → CSV 日期 2/7/2026"""
    try:
        dt = datetime.strptime(date_str.strip(), "%Y-%m-%d")
        return f"{dt.day}/{dt.month}/{dt.year}"
    except ValueError:
        return date_str.strip()


def _clean_int(value: str) -> str:
    """去掉千分位逗号，保留纯数字（或 0）"""
    v = value.replace(",", "").replace("&nbsp;", "").strip()
    if v.isdigit():
        return v
    return "0"


def row_to_csv_record(row: List[str], lottery_name: str) -> Optional[List[str]]:
    """
    网页行 → CSV 记录（与本地 CSV 列顺序完全一致）
    双色球网页列：行号,期号,6红,蓝球,快乐星期天,奖池,一等注数,一等奖金,二等注数,二等奖金,总投注额,日期
    大乐透网页列：行号,期号,5红,2蓝,奖池,一等注数,一等奖金,二等注数,二等奖金,总投注额,日期
    """
    cfg = LOTTERY_CONFIG[lottery_name]
    red_count = cfg["red_count"]
    blue_count = cfg["blue_count"]

    # 网页结构：索引0=行号, 1=期号, 2..2+red_count-1=红球, 然后蓝球
    # 双色球额外有"快乐星期天"列（索引 2+red_count+blue_count）
    red_start = 2
    red_end = red_start + red_count
    blue_start = red_end
    blue_end = blue_start + blue_count

    期号 = row[1].strip()
    reds = [n.zfill(2) for n in row[red_start:red_end]]
    blues = [n.zfill(2) for n in row[blue_start:blue_end]]
    if len(reds) != red_count or len(blues) != blue_count:
        logger.warning(f"期号 {期号} 列数不符: 红{len(reds)} 蓝{len(blues)}")
        return None

    # 奖金区起始（跳过红球+蓝球，以及双色球多出的快乐星期天列）
    bonus_start = blue_end
    if lottery_name == "双色球":
        bonus_start += 1  # 跳过快乐星期天
    try:
        奖池 = _clean_int(row[bonus_start])
        一等注数 = _clean_int(row[bonus_start + 1])
        一等奖金 = _clean_int(row[bonus_start + 2])
        二等注数 = _clean_int(row[bonus_start + 3])
        二等奖金 = _clean_int(row[bonus_start + 4])
        总投注额 = _clean_int(row[bonus_start + 5])
        日期 = _convert_date_iso_to_csv(row[bonus_start + 6])
    except IndexError:
        logger.warning(f"期号 {期号} 奖金列缺失")
        return None

    return [期号, *reds, *blues, 奖池, 一等注数, 一等奖金, 二等注数, 二等奖金, 总投注额, 日期]


# ============================================================
# 本地 CSV 读写
# ============================================================

def _read_csv_records(csv_path: Path) -> Tuple[List[str], List[List[str]]]:
    """读取本地 CSV（自动探测编码），返回表头和记录"""
    with open(csv_path, "rb") as f:
        raw = f.read(200)
    enc = "utf-8-sig"
    for e in ("utf-8-sig", "gbk", "gb18030"):
        try:
            raw.decode(e)
            enc = e
            break
        except UnicodeDecodeError:
            continue

    with open(csv_path, encoding=enc) as f:
        reader = csv.reader(f)
        header = next(reader)
        records = list(reader)
    return header, records


def _latest_draw_number(csv_path: Path) -> Optional[str]:
    """获取本地 CSV 最新期号（第一行数据）"""
    header, records = _read_csv_records(csv_path)
    if records:
        return records[0][0].strip()
    return None


def _write_csv(csv_path: Path, header: List[str], records: List[List[str]]):
    """写回 CSV（GBK 编码，保持原有格式）"""
    with open(csv_path, "w", encoding=CSV_ENCODING, newline="") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(records)
    logger.info(f"已写入 {csv_path.name}: {len(records)} 条记录")


# ============================================================
# 主入口：更新指定彩票数据
# ============================================================

def update_lottery_data(
    lottery_name: str,
    end_draw: Optional[str] = None,
    start_draw: Optional[str] = None,
    clean_after: bool = True,
) -> Dict:
    """
    从 500.com 抓取最新开奖数据，合并到本地 CSV

    参数：
    - lottery_name: 双色球 / 大乐透
    - end_draw: 抓取到哪一期（默认自动探测，抓最新）
    - start_draw: 从哪一期开始抓（默认本地最新+1）。指定后可做历史区间回填，
                  形如用户给的 URL：from=24001&to=25013
    - clean_after: 抓取后是否自动重新清洗（默认 True）

    返回：
    {
        "lottery": 彩票名,
        "latest_before": 抓取前最新期号,
        "latest_after": 抓取后最新期号,
        "fetched": 抓取到的新期数,
        "new_records": [新增记录...],
        "cleaned": 是否已重新清洗,
    }
    """
    if lottery_name not in CSV_FILES:
        return {"error": f"不支持的彩票类型: {lottery_name}"}

    # 数字型彩种委托给 fetch_500 做增量合并
    if lottery_name in DIGITAL_LOTTERIES:
        from data.fetch_500 import update_digital_lottery_data
        return update_digital_lottery_data(lottery_name)

    csv_path = CSV_FILES[lottery_name]
    if not csv_path.exists():
        return {"error": f"CSV 文件不存在: {csv_path.name}"}

    header, records = _read_csv_records(csv_path)
    latest_before = _latest_draw_number(csv_path)

    # 抓取范围：默认从本地最新期号+1 开始；指定 start_draw 时做区间回填
    if start_draw:
        try:
            start_int = int(start_draw)
        except ValueError:
            start_int = 1
    elif latest_before:
        try:
            start_int = int(latest_before) + 1
        except ValueError:
            start_int = 1
    else:
        start_int = 1

    end_int = int(end_draw) if end_draw else 99999

    # 多端点回退：newinc（主）→ inc → 用户给的 ?expect=all 历史页形式
    # 关键判定：
    #  - 主端点成功抓取但返回 0 行 → 确实没有新开奖 → 真正"已是最新"
    #  - 主端点抛异常（网络/拦截）→ 才回退到下一端点
    #  - 任何端点返回的行若无法转成合法 CSV 记录（如走势图页）→ 视为无效，继续回退
    #  - 全部端点失败 → 明确报错，不再伪造"已是最新"
    endpoints = _fetch_endpoints(lottery_name, start_int, end_int)
    html = None
    used_url = None
    errors = []
    for idx, url in enumerate(endpoints):
        try:
            logger.info(f"抓取: {lottery_name} {url}")
            raw_html = _fetch_page(url)
        except Exception as e:
            errors.append(f"{url} → 抓取失败 {type(e).__name__}: {e}")
            continue  # 抛异常才回退到下一端点
        rows = parse_html_rows(raw_html)
        if not rows:
            if idx == 0:
                # 主端点成功但无数据行：确实没有新开奖 → 真正已最新
                return {
                    "lottery": lottery_name,
                    "latest_before": latest_before,
                    "latest_after": latest_before,
                    "fetched": 0,
                    "new_records": [],
                    "message": "已是最新，无需更新",
                    "cleaned": False,
                }
            errors.append(f"{url} → 返回 0 行数据（可能被拦截/改版）")
            continue
        # 校验：至少一条记录能成功转换为合法 CSV（过滤走势图等无效结构）
        sample = next((row_to_csv_record(r, lottery_name) for r in rows), None)
        if sample is None:
            errors.append(f"{url} → 解析到 {len(rows)} 行但均为无效结构（疑似走势图页而非数据页）")
            continue
        html = raw_html
        used_url = url
        break

    if html is None:
        # 关键修复：端点失效时明确报错，不再伪造"已是最新"
        return {
            "error": "全部抓取端点均失败，无法获取最新开奖数据",
            "details": errors,
            "tip": "500.com 接口可能变更/拦截，请检查网络或更换 Referer/UA，或手动更新 CSV。",
        }

    web_rows = parse_html_rows(html)
    logger.info(f"网页返回 {len(web_rows)} 行数据（来源: {used_url}）")

    # 转换为 CSV 记录
    new_records = []
    for row in web_rows:
        rec = row_to_csv_record(row, lottery_name)
        if rec is None:
            continue
        # 跳过已存在的期号（防重复）
        if any(r and r[0] == rec[0] for r in records):
            continue
        # 跳过比本地最新期号旧的（防御；除非是显式区间回填）
        if not start_draw and latest_before and rec[0].isdigit() and latest_before.isdigit():
            if int(rec[0]) <= int(latest_before):
                continue
        new_records.append(rec)

    if not new_records:
        # 仅当期望有新数据却拿到 0 行时，提示端点可能异常（而非谎称已最新）
        expected_new = end_int >= (int(latest_before) + 1) if (latest_before and latest_before.isdigit()) else True
        if expected_new and start_int <= end_int and not start_draw:
            logger.warning("期望有新期号但解析为 0 行，端点可能已改版")
        return {
            "lottery": lottery_name,
            "latest_before": latest_before,
            "latest_after": latest_before,
            "fetched": 0,
            "new_records": [],
            "message": "已是最新，无需更新",
            "cleaned": False,
        }

    # 加文件锁：防止 Web 按钮和定时任务同时写 CSV（读→合并→写 原子化）
    from data.file_lock import file_lock
    with file_lock(csv_path):
        # 锁内重新读取（避免锁外读到的旧数据被并发写覆盖）
        _, records_latest = _read_csv_records(csv_path)
        merged = new_records + records_latest
        # 按期号降序排序去重
        merged = _dedup_sorted(merged)
        _write_csv(csv_path, header, merged)

    # 重新清洗
    cleaned = False
    if clean_after:
        try:
            from data.cleaner import run_clean
            run_clean(lottery_name)
            cleaned = True
        except Exception as e:
            logger.warning(f"清洗失败: {e}")

    return {
        "lottery": lottery_name,
        "latest_before": latest_before,
        "latest_after": _latest_draw_number(csv_path),
        "fetched": len(new_records),
        "new_records": new_records,
        "cleaned": cleaned,
    }


def _dedup_sorted(records: List[List[str]]) -> List[List[str]]:
    """按期号去重并降序排序"""
    seen = set()
    out = []
    for rec in sorted(records, key=lambda r: int(r[0]) if r and r[0].isdigit() else 0, reverse=True):
        key = rec[0] if rec else ""
        if key and key not in seen:
            seen.add(key)
            out.append(rec)
    return out


# ============================================================
# 历史列回填（500.com 全史抓取，用于补缺失的 总投注额 等列）
# ============================================================

def _norm_issue(q: str) -> Optional[int]:
    """期号规范化：'03020'/'3020'/'26102' → int（前导 0 一律去掉）"""
    q = str(q).strip()
    if q.isdigit():
        return int(q)
    return None


def _num_key(nums: List[str]) -> str:
    """号码列规范化键（'3' 与 '03' 视为同一）"""
    return ",".join(n.zfill(2) for n in nums)


def _sales_column_index(header: List[str]) -> int:
    """定位 总投注额 列；找不到返回 -1"""
    for i, h in enumerate(header):
        if "总投注额" in h or h.strip() == "投注总额":
            return i
    return -1


def backfill_columns(lottery_name: str = "双色球",
                     columns: Optional[Tuple[str, ...]] = None,
                     segments: Optional[List[Tuple[int, int]]] = None,
                     clean_after: bool = True) -> Dict:
    """500.com 全史回填缺失列（默认：奖池/注数/奖金/总投注额 全部可回填列）。

    背景（2026-09-04 探查）：
    - 双色球 总投注额 原 96% 缺失（仅 2003 + 2026-05 起有值）→ 全史回填后 100%。
    - 双色球 尾部/个别期 奖池·一等奖注数·奖金 存在"占位 0"（网页当时未公布即抓取，
      update_lottery_data 只加新期不修旧期）→ 同样用本函数修复。

    布局前提：row_to_csv_record 与本地 CSV 同构（期号,红…,蓝…,奖池,一等注数,
    一等奖金,二等注数,二等奖金,总投注额,日期）。列序不符时该列自动跳过并警告。

    参数 columns：本地表头列名子串；缺省取全部语义列。仅覆盖「本地=0/空 且 网页>0」格子。
    安全：
    - 覆盖前校验 红球/蓝球 与网页完全一致（防网页列错位污染号码）。
    - 已有非 0 值不动；网页=0 不动（一等奖注数 0 是真实值，保留）。
    - 写回 GBK CSV + 可选重清洗（与 update_lottery_data 同路径）。

    返回 {各列回填数, 覆盖行数, 跳过(号码不符), 未找到, 各段行数, 各列完整性}
    """
    if lottery_name not in CSV_FILES or lottery_name in DIGITAL_LOTTERIES:
        return {"error": f"backfill_columns 仅支持 双色球/大乐透（当前: {lottery_name}）"}

    csv_path = CSV_FILES[lottery_name]
    header, records = _read_csv_records(csv_path)
    cfg = LOTTERY_CONFIG[lottery_name]
    n_red, n_blue = cfg["red_count"], cfg["blue_count"]
    red_idx = list(range(1, 1 + n_red))
    blue_idx = list(range(1 + n_red, 1 + n_red + n_blue))

    # 语义列 → 网页 rec 位置（号码区后：奖池,一等注数,一等奖金,二等注数,二等奖金,总投注额）
    n_head = 1 + n_red + n_blue
    _SEMANTIC = {"奖池": n_head, "一等奖注数": n_head + 1, "一等奖奖金": n_head + 2,
                 "二等奖注数": n_head + 3, "二等奖奖金": n_head + 4, "总投注额": n_head + 5}
    col_of = {}
    for sem in _SEMANTIC:
        for i, h in enumerate(header):
            if sem in h:
                col_of[sem] = i
                break
    if columns is None:
        columns = tuple(col_of)
    cols = [c for c in columns if c in col_of]
    # 列序一致性护栏：本地列位置必须与网页布局一致，否则写错列（防表头错位污染）
    cols = [c for c in cols
            if col_of[c] == _SEMANTIC[c] or _warn_col_mismatch(c, col_of[c], _SEMANTIC[c])]
    if not cols:
        return {"error": f"CSV 无可回填列（现有: {header}）"}

    # 本地索引：期号 → 各目标列 raw 值
    local_idx = {}
    for i, rec in enumerate(records):
        q = _norm_issue(rec[0]) if rec else None
        if q is None:
            continue
        maxc = max(col_of[c] for c in cols)
        if len(rec) <= maxc:
            continue
        nums = [rec[j] for j in red_idx + blue_idx if j < len(rec)]
        local_idx.setdefault(q, []).append({
            "row": i, "num_key": _num_key(nums),
            "raw": {c: rec[col_of[c]] for c in cols},
        })
    if not local_idx:
        return {"error": "本地 CSV 无有效记录"}

    # 分段：缺省按 int 期号均分（每段 ≤ ~1100 期，接口实测千行内稳定）
    if segments is None:
        keys = sorted(local_idx)
        lo, hi = keys[0], keys[-1]
        span = max(1, (hi - lo + 1) // 4)
        segments = [(lo + k * span, lo + (k + 1) * span - 1 if k < 3 else hi)
                    for k in range(4)]

    # 网页拉取 → {int期号: (号码键, {sem: 网页值})}
    web_map = {}
    seg_rows = {}
    for s, e in segments:
        html = None
        for url in _fetch_endpoints(lottery_name, s, e):
            try:
                html = _fetch_page(url)
                break
            except Exception as ex:
                logger.warning(f"[backfill] 段 {s}-{e} 端点失败: {type(ex).__name__} {ex}")
        if html is None:
            seg_rows[f"{s}-{e}"] = "FAIL"
            continue
        rows = parse_html_rows(html)
        n_ok = 0
        for r in rows:
            rec = row_to_csv_record(r, lottery_name)
            if rec is None or len(rec) <= n_head + 5:
                continue
            q = _norm_issue(rec[0])
            if q is None:
                continue
            nums = [rec[j] for j in red_idx + blue_idx if j < len(rec)]
            web_map[q] = (_num_key(nums),
                          {sem: _clean_int(rec[_SEMANTIC[sem]]) for sem in _SEMANTIC})
            n_ok += 1
        seg_rows[f"{s}-{e}"] = n_ok
        logger.info(f"[backfill] 段 {s}-{e}: 网页 {n_ok} 期")

    # 回填：仅覆盖「本地=0/空 且 网页>0」的格子；号码校验通过才写
    filled_by_col = {c: 0 for c in cols}
    skipped = 0
    not_found = []
    for q, entries in local_idx.items():
        hit = web_map.get(q)
        if hit is None:
            not_found.append(q)
            continue
        web_key, web_vals = hit
        for en in entries:
            if en["num_key"] != web_key:
                skipped += 1
                continue
            for c in cols:
                # ⚠️ 必须先剥离千分位逗号：本地 CSV 可能存 "798,037,522"，
                # 直接 float() 会 ValueError → 被当成 0 → 绕过「已有值不动」把好值也重写。
                raw_cur = str(en["raw"].get(c) or "").replace(",", "").replace("，", "").strip()
                try:
                    cur = int(float(raw_cur or 0))
                except ValueError:
                    cur = 0
                if cur > 0:
                    continue  # 已有值不动
                wv = int(web_vals[c])
                if wv <= 0:
                    continue  # 网页=0（如真实未中出）→ 保留本地
                records[en["row"]][col_of[c]] = str(wv)
                filled_by_col[c] += 1

    total_filled = sum(filled_by_col.values())
    if total_filled == 0:
        return {"彩种": lottery_name, "segments": seg_rows,
                "说明": "无可回填（目标列可能已全有值）", "filled_by_col": filled_by_col,
                "skipped_号码不符": skipped, "not_found": len(not_found)}

    # 写回 + 清洗
    from data.file_lock import file_lock
    with file_lock(csv_path):
        _write_csv(csv_path, header, records)
    cleaned = False
    if clean_after:
        try:
            from data.cleaner import run_clean
            run_clean(lottery_name)
            cleaned = True
        except Exception as e:
            logger.warning(f"清洗失败: {e}")

    # 完整性统计（写回后重读）
    header2, recs2 = _read_csv_records(csv_path)
    integ = {}
    for c in cols:
        for i, h in enumerate(header2):
            if c in h and len(recs2) and len(recs2[0]) > i:
                vals = [r[i] for r in recs2 if len(r) > i]
                nz = sum(1 for v in vals if str(v).strip() not in ("", "0"))
                integ[c] = f"{nz}/{len(recs2)} ({nz / max(len(recs2), 1) * 100:.1f}%)"
                break

    return {
        "彩种": lottery_name,
        "segments": seg_rows,
        "回填合计": total_filled,
        "filled_by_col": filled_by_col,
        "skipped_号码不符": skipped, "not_found_期数": len(not_found),
        "not_found_样例": [str(q) for q in not_found[:10]],
        "完整性": integ,
        "cleaned": cleaned,
    }


def _warn_col_mismatch(sem: str, local_pos: int, web_pos: int) -> bool:
    """列序不一致护栏：返回 False 让该列被跳过，并记警告"""
    logger.warning(f"[backfill] 列 {sem} 本地位置 {local_pos} ≠ 网页布局 {web_pos}，跳过该列")
    return False


def backfill_sales_column(lottery_name: str = "双色球",
                          segments: Optional[List[Tuple[int, int]]] = None,
                          clean_after: bool = True) -> Dict:
    """兼容 wrapper：只回填 总投注额 列（等价 backfill_columns(columns=("总投注额",))）"""
    return backfill_columns(lottery_name, columns=("总投注额",),
                            segments=segments, clean_after=clean_after)
# ============================================================
# CLI 入口
# ============================================================

if __name__ == "__main__":
    import sys
    from logs.logger import setup_logger
    setup_logger("fetcher")

    targets = sys.argv[1:] if len(sys.argv) > 1 else ["双色球", "大乐透"]
    for t in targets:
        r = update_lottery_data(t)
        print(f"\n===== {r.get('lottery', t)} =====")
        if "error" in r:
            print(f"❌ {r['error']}")
            continue
        print(f"抓取前最新: {r['latest_before']} | 抓取后最新: {r['latest_after']} | 新增: {r['fetched']} 期")
        if r.get("new_records"):
            for rec in r["new_records"]:
                if t in DIGITAL_LOTTERIES:
                    print(f"  + {rec[0]}: {' '.join(rec[2:])} ({rec[1]})")
                else:
                    print(f"  + {rec[0]}: {' '.join(rec[1:1+6])} 蓝 {' '.join(rec[1+6:1+6+1])} ({rec[-1]})")
        if r.get("cleaned"):
            print("✅ 已重新清洗")
