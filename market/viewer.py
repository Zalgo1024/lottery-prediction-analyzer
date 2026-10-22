# -*- coding: utf-8 -*-
"""market.db 数据浏览器 —— 独立只读 Web 服务。

架构：
    market.db（只读，绝无写入）
        ↓ 合并多源为「每期一行」
    market/cache.db（派生缓存，可随时删除重建）
        ↓ 普通单表查询
    浏览器界面

为什么要缓存：
    多源合并依赖窗口函数（每期取优先级最高的源），直接查要全表重算。
    实测带彩种筛选时 15 秒、双彩种 12 秒 —— 交互不可用。
    物化成单表后所有检索/筛选/排序都是毫秒级，且加了索引。

铁律：
    1. market.db 用 URI mode=ro 打开，协议层就不可能写入。
    2. 缓存过期自动重建（按 draw 行数 + 最新 fetched_at 判定），不会读到旧数据。
    3. 不接入现有 Web（5000），不 import 任何业务模块，不碰 lottery_data/。

启动：
    python -m market.viewer              # http://127.0.0.1:5001/
    python -m market.viewer --open       # 顺带打开浏览器
    python -m market.viewer --rebuild    # 强制重建缓存
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sqlite3
import threading
import time
import webbrowser
from datetime import datetime
from urllib.parse import quote

from flask import Flask, Response, jsonify, render_template, request

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(HERE, "market.db")
CACHE_PATH = os.path.join(HERE, "cache.db")
TEMPLATE_DIR = os.path.join(HERE, "templates")
CACHE_VER = "2"

app = Flask(__name__, template_folder=TEMPLATE_DIR)

SRC_CN = {"cwl": "福彩官方", "sporttery": "体彩官方",
          "zhcw": "中彩网", "csv500": "现有CSV", "mof": "财政部"}

SRC_PRIO_SQL = ("CASE source WHEN 'cwl' THEN 0 WHEN 'sporttery' THEN 0 "
                "WHEN 'zhcw' THEN 1 ELSE 2 END")

SORT_COLS = {
    "draw_date": ["t.draw_date", "t.issue_seq"],
    "issue": ["t.issue_year", "t.issue_seq"],
    "lottery": ["t.lottery", "t.issue_year", "t.issue_seq"],
    "sales": ["t.sales", "t.issue_seq"],
    "pool": ["t.pool", "t.issue_seq"],
    "first_winners": ["t.fw", "t.issue_seq"],
}


# ---------------------------------------------------------------- 连接
def connect() -> sqlite3.Connection:
    """market.db 只读连接。"""
    con = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"),
                          uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    return con


def open_cache() -> sqlite3.Connection:
    con = sqlite3.connect("file:%s?mode=ro" % CACHE_PATH.replace("\\", "/"),
                          uri=True, timeout=20)
    con.row_factory = sqlite3.Row
    return con


# ---------------------------------------------------------------- 缓存
MERGED_SELECT = """
SELECT m.lottery, m.issue, m.issue_year, m.issue_seq, m.draw_date,
       m.front, m.back,
       -- 出球顺序同样只有部分源有（福彩官方没有，中彩网/体彩有）⇒ 跨源补
       COALESCE(NULLIF(m.seq_front, ''), a.m_seq)  AS seq,
       COALESCE(NULLIF(m.seq_back, ''),  a.m_seqb) AS seq_back,
       COALESCE(m.sales, a.m_sales)          AS sales,
       COALESCE(m.pool,  a.m_pool)           AS pool,
       COALESCE(m.first_winners, a.m_fw)     AS fw,
       a.m_region                            AS region_raw,
       a.n_src, a.srcs,
       COALESCE(g.c, 0)                      AS region_n
FROM r m
LEFT JOIN agg a
  ON a.lottery = m.lottery AND a.issue_year = m.issue_year
 AND a.issue_seq = m.issue_seq
LEFT JOIN (SELECT lottery, issue, COUNT(*) AS c
             FROM prize_region GROUP BY lottery, issue) g
  ON g.lottery = m.lottery AND g.issue = m.issue
WHERE m.rn = 1
"""

MERGED_CTE = """
WITH r AS (
  SELECT d.*,
         ROW_NUMBER() OVER (
           PARTITION BY lottery, issue_year, issue_seq
           ORDER BY __PRIO__, LENGTH(issue) DESC
         ) AS rn
  FROM draw d
  WHERE issue_year IS NOT NULL
),
agg AS (
  SELECT lottery, issue_year, issue_seq,
         MAX(sales) AS m_sales, MAX(pool) AS m_pool,
         MAX(first_winners) AS m_fw,
         MAX(NULLIF(first_region_raw, '')) AS m_region,
         MAX(NULLIF(seq_front, '')) AS m_seq,
         MAX(NULLIF(seq_back, ''))  AS m_seqb,
         COUNT(*) AS n_src,
         GROUP_CONCAT(source) AS srcs
  FROM draw
  WHERE issue_year IS NOT NULL
  GROUP BY lottery, issue_year, issue_seq
)
""".replace("__PRIO__", SRC_PRIO_SQL)


def _fingerprint() -> str:
    """用行数 + 最新拉取时间判定缓存是否过期。"""
    con = connect()
    try:
        n, mx = con.execute(
            "SELECT COUNT(*), COALESCE(MAX(fetched_at), '') FROM draw").fetchone()
        regions = con.execute("SELECT COUNT(*) FROM prize_region").fetchone()[0]
        return "%s|%s|%s" % (n, mx, regions)
    finally:
        con.close()


def ensure_cache(force: bool = False) -> bool:
    """确保缓存存在且与源库同步。返回是否重建了。"""
    fp = _fingerprint()

    if not force and os.path.exists(CACHE_PATH):
        try:
            c = sqlite3.connect(CACHE_PATH, timeout=10)
            try:
                got = dict(c.execute("SELECT key, value FROM meta").fetchall())
            finally:
                c.close()
            if got.get("ver") == CACHE_VER and got.get("fp") == fp:
                return False
        except sqlite3.Error:
            pass

    con = connect()
    try:
        con.execute("ATTACH DATABASE ? AS cache", (CACHE_PATH,))
        con.execute("BEGIN")
        con.execute("DROP TABLE IF EXISTS cache.draws")
        con.execute("CREATE TABLE cache.draws AS " + MERGED_CTE + MERGED_SELECT)
        con.execute("CREATE INDEX cache.idx_d1 ON draws(lottery, issue_seq)")
        con.execute("CREATE INDEX cache.idx_d2 ON draws(draw_date)")
        con.execute("CREATE INDEX cache.idx_d3 ON draws(lottery, draw_date)")
        con.execute("CREATE INDEX cache.idx_d4 ON draws(sales)")
        con.execute("CREATE TABLE IF NOT EXISTS cache.meta "
                    "(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("DELETE FROM cache.meta")
        con.executemany("INSERT INTO cache.meta(key, value) VALUES(?,?)", [
            ("ver", CACHE_VER), ("fp", fp),
            ("built_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ])
        con.execute("COMMIT")
    except Exception:
        try:
            con.execute("ROLLBACK")
        except sqlite3.Error:
            pass
        raise
    finally:
        con.close()
    return True


# ---------------------------------------------------------------- 缓存自动跟随
_CACHE_WATCH_INTERVAL = 60


def _cache_watcher(interval: float = _CACHE_WATCH_INTERVAL, stop: "threading.Event" = None):
    """后台守护线程：数据变了就重建 cache.db。

    ★ 为什么需要它：ensure_cache() 原本只在「进程启动」和「/api/rebuild」两处被调用，
      没有 before_request。于是库被更新（每日 23:10 补库、手动更新、或直接改库）之后，
      只要数据浏览器不重启，界面就一直显示旧缓存 —— 用户看到的是"更新完了但页面没变"。
      这里每 60 秒比对一次指纹（三个 COUNT/MAX 查询，实机 <10ms），变了就重建，
      不阻塞任何请求。
    """
    stop = stop or threading.Event()
    while not stop.is_set():
        stop.wait(interval)
        if stop.is_set():
            break
        try:
            ensure_cache()
        except Exception:
            # 重建失败不能让守护线程死掉（下次还会再试），也不能影响 Web 服务
            pass


def cache_info() -> dict:
    try:
        c = sqlite3.connect(CACHE_PATH, timeout=10)
        try:
            got = dict(c.execute("SELECT key, value FROM meta").fetchall())
            n = c.execute("SELECT COUNT(*) FROM draws").fetchone()[0]
        finally:
            c.close()
        return dict(rows=n, built_at=got.get("built_at"), ver=got.get("ver"))
    except sqlite3.Error:
        return dict(rows=0, built_at=None, ver=None)


# ---------------------------------------------------------------- 查询构造
def _build_filters(args) -> tuple[list, list]:
    """查询参数 → WHERE 条件（作用在缓存表的合并值上）。"""
    where, params = [], []

    lots = [x.strip() for x in (args.get("lottery") or "").split(",") if x.strip()]
    if lots:
        where.append("t.lottery IN (%s)" % ",".join("?" * len(lots)))
        params.extend(lots)

    src = (args.get("source") or "").strip()
    if src and src != "all":
        where.append("instr(',' || t.srcs || ',', ',' || ? || ',') > 0")
        params.append(src)

    d0 = (args.get("date_from") or "").strip()
    if d0:
        where.append("t.draw_date >= ?")
        params.append(d0)
    d1 = (args.get("date_to") or "").strip()
    if d1:
        where.append("t.draw_date <= ?")
        params.append(d1)

    if args.get("has_region") in ("1", "true"):
        where.append("t.region_raw IS NOT NULL")
    if args.get("has_seq") in ("1", "true"):
        where.append("t.seq IS NOT NULL")

    def num(key, expr, op, scale=1):
        v = (args.get(key) or "").strip()
        if not v:
            return
        try:
            val = float(v)
        except ValueError:
            return
        params.append(int(val * scale))
        where.append("%s %s ?" % (expr, op))

    # 销售额输入按「万元」，库内是「元」
    num("sales_min", "t.sales", ">=", 10000)
    num("sales_max", "t.sales", "<=", 10000)
    num("fw_min", "t.fw", ">=")
    num("fw_max", "t.fw", "<=")

    q = (args.get("q") or "").strip()
    if q:
        tokens = [x for x in q.replace("，", ",").replace(" ", ",").split(",") if x]
        balls = [x for x in tokens if x.isdigit() and 1 <= len(x) <= 2]
        rest = [x for x in tokens if x not in balls]
        # 短数字 ⇒ 号码精确匹配（json_each，避免 13 命中 31 / 130）
        for b in balls:
            n = int(b)
            where.append(
                "(EXISTS (SELECT 1 FROM json_each(t.front) WHERE value=?)"
                " OR EXISTS (SELECT 1 FROM json_each(t.back) WHERE value=?)"
                " OR EXISTS (SELECT 1 FROM json_each(t.seq) WHERE value=?))")
            params.extend([n, n, n])
        for x in rest:
            where.append("(t.issue LIKE ? OR t.draw_date LIKE ?)")
            params.extend(["%" + x + "%", "%" + x + "%"])

    return where, params


def _query(args) -> tuple[str, str, list]:
    where, params = _build_filters(args)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    return ("SELECT * FROM draws t" + clause,
            "SELECT COUNT(*) FROM draws t" + clause,
            params)


def _order_by(args) -> str:
    cols = SORT_COLS.get(args.get("sort", "draw_date"), SORT_COLS["draw_date"])
    order = "ASC" if args.get("order", "desc").lower() == "asc" else "DESC"
    parts = []
    for c in cols:
        parts.append("%s IS NULL" % c)   # 空值恒排最后，不随升/降序翻转
        parts.append("%s %s" % (c, order))
    return ", ".join(parts)


# ---------------------------------------------------------------- 页面
@app.get("/")
def index():
    # ?embed=1：被主看板（5000 的 /market）用 iframe 内嵌时，隐藏本页自带的标题
    # （h1 + market.db 徽标），避免与主看板顶栏/侧栏形成「双层标题」。
    embed = request.args.get("embed") in ("1", "true", "yes")
    return render_template("explore.html", embed=embed)


@app.get("/api/meta")
def api_meta():
    con = connect()
    try:
        lots = [dict(r) for r in con.execute(
            "SELECT code, name, family, note FROM lottery ORDER BY family, code")]
        stats = {r["lottery"]: dict(n=r["n"], d0=r["d0"], d1=r["d1"])
                 for r in con.execute("""
                    SELECT lottery, COUNT(*) n, MIN(d) d0, MAX(d) d1 FROM (
                      SELECT lottery, issue_year, issue_seq, MIN(draw_date) d
                      FROM draw WHERE issue_year IS NOT NULL
                      GROUP BY lottery, issue_year, issue_seq
                    ) GROUP BY lottery""")}
        srcs = [dict(source=r["source"], n=r["n"]) for r in con.execute(
            "SELECT source, COUNT(*) n FROM draw GROUP BY source ORDER BY n DESC")]
    finally:
        con.close()

    total = 0
    for it in lots:
        s = stats.get(it["code"], {})
        it.update(n=s.get("n", 0), d0=s.get("d0"), d1=s.get("d1"))
        total += it["n"]
    for s in srcs:
        s["name"] = SRC_CN.get(s["source"], s["source"])

    return jsonify(lotteries=lots, total=total, sources=srcs,
                   size_mb=round(os.path.getsize(DB_PATH) / 1048576, 1),
                   cache=cache_info())


@app.get("/api/draws")
def api_draws():
    try:
        page = max(1, int(request.args.get("page", 1)))
        size = min(500, max(10, int(request.args.get("size", 50))))
    except ValueError:
        page, size = 1, 50

    sql, cnt_sql, params = _query(request.args)
    sql += "\nORDER BY %s LIMIT ? OFFSET ?" % _order_by(request.args)

    con = open_cache()
    try:
        total = con.execute(cnt_sql, params).fetchone()[0]
        rows = [dict(r) for r in con.execute(sql, params + [size, (page - 1) * size])]
    finally:
        con.close()

    for r in rows:
        for k in ("front", "back", "seq", "seq_back"):
            v = r.get(k)
            r[k] = json.loads(v) if v else None
        r["region_raw"] = (r.get("region_raw") or "").strip() or None

    day_counts = _day_counts(request.args, rows)

    return jsonify(total=total, page=page, size=size, rows=rows,
                   pages=(total + size - 1) // size,
                   day_counts=day_counts)


def _day_counts(args, rows) -> dict:
    """当前页涉及的开奖日，在**整个筛选结果**里各有多少期。

    ★ 为什么必须问服务端而不是前端自己数：界面按天分组，一天 6~8 期、每页 50 期
      ⇒ 一页约 7 天，**首尾那天必然被页边界切开**。前端只有本页的行，数出来的
      「当天 N 期」在跨页那天就是错的（少算），显示会比实际少几期。
      这里带着同一套筛选条件重跑一次 GROUP BY，拿全量口径。
    """
    dates = sorted({r["draw_date"] for r in rows if r.get("draw_date")})
    if not dates:
        return {}
    where, params = _build_filters(args)
    where.append("t.draw_date IN (%s)" % ",".join("?" * len(dates)))
    con = open_cache()
    try:
        return {d: n for d, n in con.execute(
            "SELECT t.draw_date, COUNT(*) FROM draws t WHERE %s GROUP BY t.draw_date"
            % " AND ".join(where), params + dates)}
    finally:
        con.close()


@app.get("/api/detail")
def api_detail():
    lot = request.args.get("lottery", "")
    issue = request.args.get("issue", "")
    if not lot or not issue:
        return jsonify(error="缺少 lottery / issue"), 400

    con = connect()
    try:
        key = con.execute(
            "SELECT issue_year, issue_seq FROM draw WHERE lottery=? AND issue=? LIMIT 1",
            (lot, issue)).fetchone()
        if not key:
            return jsonify(error="未找到该期"), 404
        y, s = key["issue_year"], key["issue_seq"]

        srcs = [dict(r) for r in con.execute("""
            SELECT source, issue, draw_date, front, back, seq_front, seq_back,
                   sales, pool, first_winners, first_region_raw, fetched_at
            FROM draw WHERE lottery=? AND issue_year=? AND issue_seq=?
            ORDER BY %s""" % SRC_PRIO_SQL, (lot, y, s))]

        for it in srcs:
            it["source_cn"] = SRC_CN.get(it["source"], it["source"])
            for k in ("front", "back", "seq_front", "seq_back"):
                v = it.get(k)
                it[k] = json.loads(v) if v else None

        prizes = [dict(r) for r in con.execute("""
            SELECT source, level, level_name, winners, amount, amount_kind
            FROM prize WHERE lottery=? AND issue IN
              (SELECT issue FROM draw WHERE lottery=? AND issue_year=? AND issue_seq=?)
            ORDER BY level, source""", (lot, lot, y, s))]

        regions = [dict(r) for r in con.execute("""
            SELECT source, region, winners FROM prize_region
            WHERE lottery=? AND issue IN
              (SELECT issue FROM draw WHERE lottery=? AND issue_year=? AND issue_seq=?)
            ORDER BY winners DESC""", (lot, lot, y, s))]

        # 跨源合并视图：号码/出球序/金额可能分散在不同源里（福彩官方没有出球序，
        # 中彩网有），详情页主区必须显示「合并后」的值，否则会出现有数据却不显示。
        merged = {}
        for it in srcs:
            for k in ("front", "back", "seq_front", "seq_back", "draw_date",
                      "sales", "pool", "first_winners", "first_region_raw"):
                if merged.get(k) in (None, "", []):
                    merged[k] = it.get(k)

        return jsonify(lottery=lot, issue=issue, issue_year=y, issue_seq=s,
                       sources=srcs, merged=merged,
                       prizes=prizes, regions=regions)
    finally:
        con.close()


@app.get("/api/export")
def api_export():
    sql, _cnt, params = _query(request.args)
    sql += "\nORDER BY %s" % _order_by(request.args)

    buf = io.StringIO()
    buf.write("\ufeff")  # BOM：Excel 打开中文不乱码
    w = csv.writer(buf)
    w.writerow(["彩种", "期号", "开奖日", "前区/号码", "后区", "销售额(元)",
                "奖池(元)", "一等奖注数", "出球顺序", "分省头奖", "源"])

    con = open_cache()
    try:
        n = 0
        for r in con.execute(sql, params):
            front = json.loads(r["front"]) if r["front"] else []
            back = json.loads(r["back"]) if r["back"] else []
            seq = json.loads(r["seq"]) if r["seq"] else []
            w.writerow([r["lottery"], r["issue"], r["draw_date"],
                        " ".join("%02d" % x for x in front),
                        " ".join("%02d" % x for x in back),
                        r["sales"], r["pool"], r["fw"],
                        " ".join("%02d" % x for x in seq),
                        r["region_raw"] or "", r["srcs"] or ""])
            n += 1
            if n >= 200000:
                break
    finally:
        con.close()

    data = buf.getvalue().encode("utf-8")
    return Response(data, mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition":
                             "attachment; filename*=UTF-8''%s"
                             % quote("draws_%d.csv" % n)})


@app.get("/api/region_board")
def api_region_board():
    lot = request.args.get("lottery", "ssq")
    con = connect()
    try:
        rows = [dict(r) for r in con.execute("""
            SELECT region, SUM(winners) w, COUNT(DISTINCT issue) n
            FROM prize_region WHERE lottery=? AND winners>0
            GROUP BY region ORDER BY w DESC""", (lot,))]
        return jsonify(lottery=lot, rows=rows)
    finally:
        con.close()


@app.get("/healthz")
def healthz():
    return jsonify(ok=os.path.exists(DB_PATH), db=os.path.basename(DB_PATH),
                   cache=cache_info())


@app.get("/api/rebuild")
def api_rebuild():
    """重建缓存（数据更新后手动触发，也可重启服务自动判定）。"""
    rebuilt = ensure_cache(force=True)
    return jsonify(rebuilt=rebuilt, cache=cache_info())


def main() -> None:
    ap = argparse.ArgumentParser(description="market.db 数据浏览器")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5001)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    ap.add_argument("--rebuild", action="store_true", help="强制重建查询缓存")
    a = ap.parse_args()

    if not os.path.exists(DB_PATH):
        raise SystemExit("找不到 market.db：%s\n先运行 python -m market.build" % DB_PATH)

    rebuilt = ensure_cache(force=a.rebuild)
    info = cache_info()
    print("查询缓存: %s（%s 期，构建于 %s）"
          % ("已重建" if rebuilt else "命中", info["rows"], info["built_at"]))

    url = "http://%s:%d/" % (a.host, a.port)
    print("数据浏览器: %s" % url)

    # 缓存自动跟随：数据更新后 1 分钟内界面自动反映，无需重启进程
    threading.Thread(target=_cache_watcher, name="cache-watcher", daemon=True).start()

    if a.open:
        webbrowser.open(url)
    app.run(host=a.host, port=a.port, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
