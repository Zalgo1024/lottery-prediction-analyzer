"""market.db 建库与导入。

用法：
    python -m market.build --init                 # 只建空库
    python -m market.build --csv                  # 现有 6 个 CSV 只读导入（source=csv500）
    python -m market.build --cwl                  # 福彩官方 4 彩种（含分省中奖）
    python -m market.build --sporttery             # 体彩官方 4 彩种（含出球顺序）
    python -m market.build --zhcw                 # 中彩网 双色球/3D/七乐彩（全历史出球顺序）
    python -m market.build --all                   # 全部
    python -m market.build --status                # 看库里有什么

铁律：
  - 只写 market/market.db，不碰 lottery_data/*.csv，不碰任何现有模块
  - 全程 INSERT OR REPLACE，可重复跑、可断点续跑
  - 删掉 market.db 即完全回滚
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(HERE, "market.db")
SCHEMA = os.path.join(HERE, "schema.sql")
LOTTERY_DIR = os.path.join(ROOT, "lottery_data")

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from market import sources  # noqa: E402


# ------------------------------------------------------------------ 建库

def connect(path: str = DB_PATH) -> sqlite3.Connection:
    con = sqlite3.connect(path, timeout=30)
    con.execute("PRAGMA journal_mode = WAL")
    con.execute("PRAGMA synchronous = NORMAL")
    return con


def init_db(path: str = DB_PATH) -> sqlite3.Connection:
    con = connect(path)
    with open(SCHEMA, "r", encoding="utf-8") as f:
        con.executescript(f.read())
    con.commit()
    _seed_lotteries(con)
    return con


def _seed_lotteries(con: sqlite3.Connection) -> None:
    for code, meta in sources.LOTTERIES.items():
        con.execute(
            "INSERT OR REPLACE INTO lottery(code,name,family,issue_width,zones,note)"
            " VALUES(?,?,?,?,?,?)",
            (code, meta["name"], meta["family"], meta["width"],
             json.dumps(meta["zones"], ensure_ascii=False), None))
    con.commit()


# ------------------------------------------------------------------ 写行

def write_rows(con: sqlite3.Connection, code: str, source: str, rows,
               progress_every: int = 500, verbose: bool = True) -> dict:
    """消费 (draw_row, prize_rows, region_rows) 三元组。返回统计。

    verbose=False 供定时更新用：后台线程不该把上千行进度刷进 flask_stdout.log。
    """
    n_draw = n_prize = n_region = 0
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for draw, prizes, regions in rows:
        draw = dict(draw, fetched_at=ts)
        con.execute(
            "INSERT OR REPLACE INTO draw(lottery,issue,source,issue_year,issue_seq,"
            "draw_date,front,back,seq_front,seq_back,sales,pool,first_winners,"
            "first_region_raw,fetched_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (draw["lottery"], draw["issue"], draw["source"], draw["issue_year"],
             draw["issue_seq"], draw["draw_date"], draw["front"], draw["back"],
             draw["seq_front"], draw["seq_back"], draw["sales"], draw["pool"],
             draw["first_winners"], draw["first_region_raw"], draw["fetched_at"]))
        n_draw += 1
        for p in prizes:
            con.execute(
                "INSERT OR REPLACE INTO prize(lottery,issue,source,level,level_name,"
                "winners,amount,amount_kind) VALUES(?,?,?,?,?,?,?,?)",
                (draw["lottery"], draw["issue"], draw["source"], p["level"],
                 p["level_name"], p["winners"], p["amount"],
                 p.get("amount_kind")))
            n_prize += 1
        for r in regions:
            con.execute(
                "INSERT OR REPLACE INTO prize_region(lottery,issue,source,region,winners)"
                " VALUES(?,?,?,?,?)",
                (draw["lottery"], draw["issue"], draw["source"], r["region"],
                 r["winners"]))
            n_region += 1
        if verbose and n_draw % progress_every == 0:
            con.commit()
            print("    ... %s/%s 已写入 %d 期" % (code, source, n_draw), flush=True)
    con.commit()
    return dict(draw=n_draw, prize=n_prize, region=n_region)


def log_sync(con, task: str, started: str, rows: int, status: str, note: str = ""):
    con.execute(
        "INSERT INTO sync_log(task,started_at,finished_at,rows,status,note)"
        " VALUES(?,?,?,?,?,?)",
        (task, started, datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
         rows, status, note))
    con.commit()


# ------------------------------------------------------------------ 各源导入

def build_csv(con, codes=None, verbose=True) -> dict:
    codes = codes or list(sources.CSV_MAP)
    out = {}
    for code in codes:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            st = write_rows(con, code, "csv500",
                            sources.iter_csv(LOTTERY_DIR, code), verbose=verbose)
        except Exception as e:  # 单彩种失败不影响其它
            log_sync(con, "csv:%s" % code, started, 0, "fail", str(e)[:300])
            print("  [FAIL] CSV %s: %s" % (code, e), flush=True)
            continue
        log_sync(con, "csv:%s" % code, started, st["draw"], "ok")
        out[code] = st
        if verbose:
            print("  [csv] %-4s 期=%-6d 奖级=%-6d" % (code, st["draw"], st["prize"]),
                  flush=True)
    return out


def build_cwl(con, codes=None, verbose=True, max_pages: int = 40) -> dict:
    """福彩官方。max_pages 控制翻页深度：官方第 1 页就返 500 期（最新在前），
    日常增量只需 1 页，全量才需要翻到 40 页。"""
    codes = codes or [c for c, m in sources.LOTTERIES.items() if m.get("cwl")]
    out = {}
    for code in codes:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            st = write_rows(con, code, "cwl", sources.iter_cwl(code, max_pages=max_pages), verbose=verbose)
        except Exception as e:
            log_sync(con, "cwl:%s" % code, started, 0, "fail", str(e)[:300])
            print("  [FAIL] cwl %s: %s" % (code, e), flush=True)
            continue
        log_sync(con, "cwl:%s" % code, started, st["draw"], "ok")
        out[code] = st
        if verbose:
            print("  [cwl] %-4s 期=%-6d 奖级=%-6d ★地区=%d"
                  % (code, st["draw"], st["prize"], st["region"]), flush=True)
    return out


def build_sporttery(con, codes=None, verbose=True, max_pages: int = 120) -> dict:
    """体彩官方。pageSize 上限 100（实测传 500 也只返 100），第 1 页即最新 100 期。"""
    codes = codes or [c for c, m in sources.LOTTERIES.items()
                      if m.get("family") == "sporttery"]
    out = {}
    for code in codes:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            st = write_rows(con, code, "sporttery", sources.iter_sporttery(code, max_pages=max_pages), verbose=verbose)
        except Exception as e:
            log_sync(con, "sporttery:%s" % code, started, 0, "fail", str(e)[:300])
            print("  [FAIL] sporttery %s: %s" % (code, e), flush=True)
            continue
        log_sync(con, "sporttery:%s" % code, started, st["draw"], "ok")
        out[code] = st
        if verbose:
            print("  [sporttery] %-4s 期=%-6d 奖级=%-6d"
                  % (code, st["draw"], st["prize"]), flush=True)
    return out


def build_zhcw(con, codes=None, verbose=True, count: int = 4000) -> dict:
    """中彩网。一次取 count 条（pageNum 参数无效，只能靠 issueCount 拉）。"""
    codes = codes or [c for c, m in sources.LOTTERIES.items() if m.get("zhcw")]
    out = {}
    for code in codes:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        try:
            st = write_rows(con, code, "zhcw", sources.fetch_zhcw(code, count=count), verbose=verbose)
        except Exception as e:
            log_sync(con, "zhcw:%s" % code, started, 0, "fail", str(e)[:300])
            print("  [FAIL] zhcw %s: %s" % (code, e), flush=True)
            continue
        log_sync(con, "zhcw:%s" % code, started, st["draw"], "ok")
        out[code] = st
        if verbose:
            print("  [zhcw] %-4s 期=%-6d" % (code, st["draw"]), flush=True)
    return out


def import_mof(con, xlsx_path: str, ym: str, verbose=True) -> int:
    rows = sources.parse_mof_xlsx(xlsx_path, ym)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    for r in rows:
        con.execute(
            "INSERT OR REPLACE INTO region_sales(ym,region,agency,sales_wan,yoy,"
            "cumulative_wan,source,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
            (r["ym"], r["region"], r["agency"], r["sales_wan"], r["yoy"],
             r["cumulative_wan"], "mof", ts))
    con.commit()
    if verbose:
        print("  [mof] %s 写入 %d 行（%d 省 × 2 机构）"
              % (ym, len(rows), len(rows) // 2), flush=True)
    return len(rows)


MOF_CACHE = os.path.join(HERE, ".mof_cache")


def build_mof(con, months=None, max_pages=None, verbose=True, cache_dir=None,
              force=False) -> dict:
    """财政部「各地区彩票销售情况」→ region_sales。

    months=None     扫描全部分页（首次回填用，实测 217 个月 / 2008-08 起）
    months=[...]    只处理这些月份（日常增量）
    force=False     库里该月已有数据就跳过，不重复下载附件

    ★为什么不复用 import_mof：那个只要本地 xlsx，且不记录失败月份；
      这里要「逐月走网络 + 失败不影响其它月 + 断点续跑」，必须自己控流程。
    ★全部 insert 走 INSERT OR REPLACE ⇒ 重跑安全；单月失败只记进 failed，不中断。
    """
    cache_dir = cache_dir or MOF_CACHE
    log = (lambda s: print(s, flush=True)) if verbose else None
    max_pages = max_pages if max_pages is not None else sources.MOF_MAX_PAGES

    have = set()
    if not force:
        have = {r[0] for r in con.execute(
            "SELECT DISTINCT ym FROM region_sales WHERE source='mof'")}

    if months is None:
        log and log("  [mof] 扫描公告清单（最多 %d 页）…" % max_pages)
        posts = sources.iter_mof_posts(max_pages=max_pages, log=log)
    else:
        # 只给了月份 ⇒ 只需要最近几页就能覆盖，扫全量太浪费
        posts = sources.iter_mof_posts(max_pages=max_pages, want=months)
        posts = {ym: posts[ym] for ym in months if ym in posts}

    todo = [ym for ym in sorted(posts) if force or ym not in have]
    log and log("  [mof] 公告 %d 个月，已有 %d 个月，待补 %d 个月"
                % (len(posts), len(posts) - len(todo), len(todo)))

    st = {"months": 0, "rows": 0, "skipped": sorted(have & set(posts)),
          "failed": {}, "incomplete": {}, "no_data": [], "cache": cache_dir}
    t_start = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    for i, ym in enumerate(todo, 1):
        res = sources.fetch_mof_month(ym, posts[ym]["url"], cache_dir, log=None)
        rows = res.get("rows") or []
        if not rows:
            if res.get("no_region") and not res.get("error"):
                # 源头当月就没发分省表（如 2020-02 疫情停售只发了全国汇总）
                st["no_data"].append(ym)
                log and log("  [mof] %-7s － 源头未提供分省表，跳过" % ym)
            else:
                st["failed"][ym] = res.get("error") or "解析为空（附件 %d 个）" % res.get("files", 0)
                log and log("  [mof] %-7s ✗ %s" % (ym, st["failed"][ym]))
            continue
        for r in rows:
            con.execute(
                "INSERT OR REPLACE INTO region_sales(ym,region,agency,sales_wan,yoy,"
                "cumulative_wan,source,fetched_at) VALUES(?,?,?,?,?,?,?,?)",
                (r["ym"], r["region"], r["agency"], r["sales_wan"], r["yoy"],
                 r["cumulative_wan"], "mof", ts))
        con.commit()
        provs = len({r["region"] for r in rows})
        st["months"] += 1
        st["rows"] += len(rows)
        if provs < 31:
            st["incomplete"][ym] = provs
            log and log("  [mof] %-7s 写入 %d 行（⚠ 仅 %d 省）" % (ym, len(rows), provs))
        else:
            log and log("  [mof] %-7s 写入 %d 行（%d 省）" % (ym, len(rows), provs))

    log_sync(con, "mof", t_start, st["months"],
             "ok" if not st["failed"] else "partial",
             note="; ".join(sorted(st["failed"])[:8]))
    log and log("  [mof] 完成：%d 个月 / %d 行；失败 %d 个月；源头无分省表 %d 个月"
                % (st["months"], st["rows"], len(st["failed"]), len(st["no_data"])))
    return st


# ------------------------------------------------------------------ 状态

def status(con) -> None:
    print("库文件: %s (%.1f MB)" % (DB_PATH, os.path.getsize(DB_PATH) / 1048576.0))
    print()
    print("%-4s %-8s %-10s %8s %8s %8s %8s" %
          ("彩种", "名称", "源", "期数", "奖级", "地区行", "有出球序"))
    q = """
    SELECT d.lottery, l.name, d.source, COUNT(*) 期数,
           (SELECT COUNT(*) FROM prize p WHERE p.lottery=d.lottery AND p.source=d.source),
           (SELECT COUNT(*) FROM prize_region r WHERE r.lottery=d.lottery AND r.source=d.source),
           SUM(CASE WHEN d.seq_front IS NOT NULL THEN 1 ELSE 0 END)
    FROM draw d JOIN lottery l ON l.code=d.lottery
    GROUP BY d.lottery, d.source ORDER BY d.lottery, d.source
    """
    for r in con.execute(q):
        print("%-4s %-8s %-10s %8d %8d %8d %8d" % r)

    print()
    n = con.execute("SELECT COUNT(*) FROM region_sales").fetchone()[0]
    print("分省销量行数: %d" % n)

    print()
    print("最新一期:")
    for r in con.execute(
            "SELECT lottery, issue, draw_date, front, back FROM v_draw "
            "ORDER BY draw_date DESC LIMIT 8"):
        print("  %-4s %-8s %s  %s + %s" % r)


# ------------------------------------------------------------------ CLI

def main(argv=None):
    ap = argparse.ArgumentParser(prog="market.build", description="market.db 建库/导入")
    ap.add_argument("--init", action="store_true", help="只建空库")
    ap.add_argument("--csv", action="store_true", help="导入现有 6 个 CSV（只读）")
    ap.add_argument("--cwl", action="store_true", help="福彩官方（含分省中奖）")
    ap.add_argument("--sporttery", action="store_true", help="体彩官方（含出球顺序）")
    ap.add_argument("--zhcw", action="store_true", help="中彩网（全历史出球顺序）")
    ap.add_argument("--mof", metavar="XLSX", help="财政部销量附件 .xlsx（本地文件）")
    ap.add_argument("--ym", help="--mof 对应的月份，如 2026-06")
    ap.add_argument("--mof-all", action="store_true",
                    help="财政部：回填全部历史月份（2008-08 起，约 217 个月）")
    ap.add_argument("--mof-months", metavar="YMS",
                    help="财政部：只补这些月份，逗号分隔，如 2026-08,2026-09")
    ap.add_argument("--mof-pages", type=int, default=None,
                    help="财政部：扫描公告页数（默认 40，实测 33 页到底）")
    ap.add_argument("--mof-force", action="store_true",
                    help="财政部：已有数据的月份也重新下载")
    ap.add_argument("--all", action="store_true", help="全部（含 CSV）")
    ap.add_argument("--status", action="store_true", help="查看库内容")
    ap.add_argument("--db", default=DB_PATH, help="库路径（默认 market/market.db）")
    args = ap.parse_args(argv)

    if args.status:
        con = connect(args.db)
        status(con)
        return 0

    con = init_db(args.db)
    t0 = time.time()

    if args.all or args.csv:
        print("[1/4] 现有 CSV 只读导入")
        build_csv(con)
    if args.all or args.cwl:
        print("[2/4] 福彩官方")
        build_cwl(con)
    if args.all or args.zhcw:
        print("[3/4] 中彩网")
        build_zhcw(con)
    if args.all or args.sporttery:
        print("[4/4] 体彩官方")
        build_sporttery(con)

    if args.mof:
        if not args.ym:
            print("--mof 需要同时给 --ym（如 2026-06）")
            return 2
        print("[mof] 财政部分省销量")
        import_mof(con, args.mof, args.ym)

    if args.mof_all or args.mof_months:
        print("[mof] 财政部分省销量（网络抓取）")
        months = ([m.strip() for m in args.mof_months.split(",") if m.strip()]
                  if args.mof_months else None)
        st = build_mof(con, months=months, max_pages=args.mof_pages,
                       force=args.mof_force)
        if st["failed"]:
            print("  失败月份 %d：%s" % (len(st["failed"]),
                  ", ".join(sorted(st["failed"])[:20])))
        if st["incomplete"]:
            print("  省份不全：%s" % st["incomplete"])
        if st["no_data"]:
            print("  源头未提供分省表（非失败）：%s" % ", ".join(st["no_data"]))

    if not any([args.all, args.csv, args.cwl, args.sporttery, args.zhcw,
                args.mof, args.mof_all, args.mof_months, args.init]):
        ap.print_help()
        return 0

    print("\n完成，用时 %.1fs" % (time.time() - t0))
    status(con)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
