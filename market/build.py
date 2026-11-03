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


# ------------------------------------------------------------------ 中彩网详情：头奖分省回填

# 中彩网「详情接口」（transactionType=10001002）的 firstPrizeAddress 字段能给出头奖分省。
# 只有乐透型有这份数据；数字型（3D/排3/排5/七星彩）接口返回空/None。
ZHCW_REGION_CODES = ("dlt", "ssq", "qlc", "kl8")

# 每个彩种用哪个源的期号当「候选期号」。★必须用接口认得的那种位数：
#   大乐透=sporttery(5 位 26124)  ／ 双色球/七乐彩=zhcw(7 位 2026124，覆盖全史)
#   快乐8=cwl(7 位 2026290)；kl8 无 zhcw 列表。
#   若用 csv500 的 4/5 位旧写法去问接口，一律返回 None ⇒ 白跑且丢期。
_CANDIDATE_SOURCE = {
    "dlt": "sporttery",
    "ssq": "zhcw",
    "qlc": "zhcw",
    "kl8": "cwl",
}


def _candidate_issues(con, code: str) -> list[tuple[str, int, int]]:
    """返回 [(issue_str, issue_year, issue_seq)]，最近在前。

    issue_str 取「接口认得的那个源」的写法；year/seq 用于把同一期的所有源行
    （csv500 旧写法、sporttery、cwl…）一起更新 first_region_raw。
    """
    src = _CANDIDATE_SOURCE.get(code)
    sql = ("SELECT issue, issue_year, issue_seq FROM draw "
           "WHERE lottery=? AND issue_year IS NOT NULL AND issue_seq IS NOT NULL")
    args: list = [code]
    if src:
        sql += " AND source=?"
        args.append(src)
    sql += " GROUP BY issue_year, issue_seq ORDER BY issue_year DESC, issue_seq DESC"
    try:
        rows = con.execute(sql, tuple(args)).fetchall()
    except sqlite3.Error:
        return []
    if not rows and src:
        # 该源还没有行（例如 zhcw 尚未建）→ 退回任意源，至少别漏掉整段历史
        rows = con.execute(
            "SELECT issue, issue_year, issue_seq FROM draw "
            "WHERE lottery=? AND issue_year IS NOT NULL AND issue_seq IS NOT NULL "
            "GROUP BY issue_year, issue_seq "
            "ORDER BY issue_year DESC, issue_seq DESC", (code,)).fetchall()
    return [(r[0], r[1], r[2]) for r in rows]


def _reference_first_winners(con, code: str) -> dict:
    """每期「参考一等奖注数」= 各源 level=1 注数的**最大值**。返回 {issue: n}。

    为什么取 max：
      ★大乐透派奖期 sporttery 的 level=1 行叫「一等奖基本派奖」，winners 是**子集**
        （21039 该值=4、实际一等奖=7），单源取值会把 7 读成 4；取 max 不会低估。
      ★csv500 的 first_winners 有占位 0（26122/26120 报 0，实际 7/3），但它在 draw 表
        不在 prize 表，不参与本函数 ⇒ 不受污染。
    用途：判「该期有没有人中过一等奖」。参考值=0 的期不可能有分省数据，不必请求。
    ★实证（2026-11-01，5346 期已填充数据）：参考>0 的占 100%、参考=0 的 0 期
      ⇒ 判据没有把一个真有分省的期判成 0。
    """
    out: dict = {}
    try:
        rows = con.execute(
            "SELECT issue, winners FROM prize WHERE lottery=? AND level=1",
            (code,)).fetchall()
    except sqlite3.Error:
        return out
    for iss, w in rows:
        if w is None:
            continue
        try:
            w = int(w)
        except (TypeError, ValueError):
            continue
        if w > out.get(iss, -1):
            out[iss] = w
    return out


def _ensure_region_probe(con) -> bool:
    """确保 `region_probe` 存在（老库没有这张表）。返回是否可用。"""
    try:
        con.execute(
            "CREATE TABLE IF NOT EXISTS region_probe ("
            " lottery TEXT NOT NULL, issue TEXT NOT NULL, outcome TEXT NOT NULL,"
            " checked_at TEXT, PRIMARY KEY (lottery, issue))")
        return True
    except sqlite3.Error:
        return False


def _probed_set(con, code: str) -> set:
    try:
        return {r[0] for r in con.execute(
            "SELECT issue FROM region_probe WHERE lottery=?", (code,))}
    except sqlite3.Error:
        return set()


def pending_region_issues(con, code: str, only_missing=True, skip_no_winner=True,
                         skip_checked=True):
    """仍需抓取的期：返回 ([(issue, year, seq)], skipped_no_winner, skipped_checked)。

    ★所有过滤判据集中在这一个函数里，**别在别处再写一份**（判据漂移是这类批处理
      最常见的坑：两处判据不一致就会出现「说没缺口了但还在抓」或反之）。
    四道过滤：
      1. 已有分省行（only_missing）——防跨源双计
      2. 已探过（region_probe）——「查过、源头没有」不是缺口，不重抓
      3. 参考一等奖注数=0——本来无人中，不可能有分省
      4. （期号写法）由 `_candidate_issues` 保证用接口认得的那个源
    """
    cand = _candidate_issues(con, code)
    if only_missing:
        try:
            have = {r[0] for r in con.execute(
                "SELECT DISTINCT issue FROM prize_region WHERE lottery=?", (code,))}
        except sqlite3.Error:
            have = set()
        cand = [c for c in cand if c[0] not in have]
    n_checked = 0
    if skip_checked:
        probed = _probed_set(con, code)
        if probed:
            kept = [c for c in cand if c[0] not in probed]
            n_checked = len(cand) - len(kept)
            cand = kept
    n_nohit = 0
    if skip_no_winner:
        ref = _reference_first_winners(con, code)
        if ref:
            kept = []
            for c in cand:
                if ref.get(c[0], None) == 0:
                    n_nohit += 1
                else:
                    kept.append(c)      # 值未知(None) 也保留：不拿缺数据当「无人中」
            cand = kept
    return cand, n_nohit, n_checked


def build_zhcw_region(con, codes=None, limit_per_code=None, only_missing=True,
                      verbose=True, sleep=0.2, skip_no_winner=True,
                      max_consecutive_failures=20, skip_checked=True) -> dict:
    """用中彩网「详情接口」补头奖分省：draw.first_region_raw + prize_region(source='zhcw')。

    背景：体彩官方（sporttery）不提供分省中奖分布，旧代码直接写 first_region_raw=None，
    导致大乐透 2942 期分省一直是 0 行。中彩网详情页（avalon 模板 {{j1zjqk}}）背后的
    接口返回 firstPrizeAddress，是这项数据唯一的结构化来源。

    ★只补「该期一条分省行都没有」的期（only_missing=True）：
      v_region_prize / region_prize_board 都是**跨源求和、不按 source 过滤**，
      同一期若同时存在 cwl 与 zhcw 两套地区行，注数会被算两遍。只在缺口期写入，
      天然避免重复计数。
    ★只填 first_region_raw 的空值（不覆盖已有原文）：cwl 的福彩原文优先级更高。
    ★数字型不写：接口返回空，不是「抓失败」，不伪造行。
    ★「共0注。」这种「查了但无人中」的期也会写进 first_region_raw（不写 prize_region），
      以便与「没查过」区分。
    ★skip_no_winner=True：参考一等奖注数（`_reference_first_winners`）=0 的期直接跳过，
      不发请求。这类期本就没有分省数据（kl8 缺口 1285 期里有 1274 期是「选十中十无人中」），
      抓了也是空 ⇒ 白耗配额；而且它们永远填不进 prize_region，`only_missing` 每轮都会
      再抓一遍，形成永久重抓。force（skip_no_winner=False）时不跳，用于怀疑参考值失真时复核。
    ★★连续失败 `max_consecutive_failures` 次就**中止**（并跳过后续彩种）：
      中彩网在约 1600 次请求后会 `RemoteDisconnected`（2026-11-01 实测）。
      「抓不到」绝不能记成「无人中」，所以 `sources.fetch_zhcw_region` 失败时**抛异常**，
      这里计 `failed`；连续失败说明是限流而非个别期缺失 ⇒ 停手，已写入的都是真数据，
      冷却后用同样的命令续跑（`only_missing` 幂等）。
    ★★skip_checked=True：已探过的期不再抓（`region_probe` 表）。中彩网对**部分期**
      返回空 firstPrizeAddress（大乐透 14048~14055 全空，而 14056 有「上海（基本1 注）」），
      这种「查过、源头没有」写不出行，等于永远留在缺口里 ⇒ 不记下来就会永久重抓。

    返回 {彩种: {checked, filled_issues, region_rows, draw_updated, empty, failed,
                  skipped_no_winner, skipped_checked, aborted, abort_reason}}
    """
    codes = list(codes or ZHCW_REGION_CODES)
    out = {}
    for code in codes:
        started = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        st = dict(checked=0, filled_issues=0, region_rows=0, draw_updated=0,
                  empty=0, failed=0, skipped_no_winner=0, skipped_checked=0,
                  aborted=False, abort_reason="")
        probe_ok = _ensure_region_probe(con)
        cand, n_nohit, n_checked = pending_region_issues(
            con, code, only_missing=only_missing, skip_no_winner=skip_no_winner,
            skip_checked=skip_checked and probe_ok)
        st["skipped_no_winner"] = n_nohit
        st["skipped_checked"] = n_checked
        if limit_per_code:
            cand = cand[:int(limit_per_code)]
        if not cand:
            out[code] = st
            if verbose:
                print("  [zhcw-region] %-4s 无需回填（无人中跳过 %d，已探过跳过 %d）"
                      % (code, st["skipped_no_winner"], st["skipped_checked"]), flush=True)
            continue

        consec = 0
        aborted = False
        for i, (issue, year, seq) in enumerate(cand, 1):
            st["checked"] += 1
            try:
                raw, regions = sources.fetch_zhcw_region(code, issue)
                consec = 0
            except Exception as e:
                st["failed"] += 1
                consec += 1
                # ★连续失败=被限流/断连，不是「这批期没数据」。继续跑只会：
                #   ① 白等 ② 把封禁拖长。直接停，已写入的都是真实数据，可随时续跑。
                if consec >= max_consecutive_failures:
                    aborted = True
                    st["aborted"] = True
                    st["abort_reason"] = "连续 %d 次请求失败（疑似限流）：%s" % (
                        consec, str(e)[:90])
                    break
                time.sleep(sleep)
                continue
            if raw:
                # 按 (year, seq) 更新 → 同期的 csv500 旧写法 / sporttery / cwl 行一起补，
                # 否则 v_draw 按优先级选中的那行可能仍为空。
                cur = con.execute(
                    "UPDATE draw SET first_region_raw=? WHERE lottery=? AND issue_year=? "
                    "AND issue_seq=? AND (first_region_raw IS NULL OR first_region_raw='')",
                    (raw, code, year, seq))
                st["draw_updated"] += cur.rowcount
            if not regions:
                st["empty"] += 1          # 无人中头奖 / 源头该期无分省（如返回「无」）
            else:
                for region, n in regions:
                    con.execute(
                        "INSERT OR REPLACE INTO prize_region"
                        "(lottery,issue,source,region,winners) VALUES(?,?,?,?,?)",
                        (code, issue, "zhcw", region, n))
                    st["region_rows"] += 1
                st["filled_issues"] += 1
            # ★记「探过了」，无论有没有抓到分省 —— 否则源头没有的期会永久重抓
            if probe_ok:
                con.execute(
                    "INSERT OR REPLACE INTO region_probe(lottery,issue,outcome,checked_at)"
                    " VALUES(?,?,?,?)",
                    (code, issue, "filled" if regions else "empty",
                     datetime.now().strftime("%Y-%m-%d %H:%M:%S")))
            if verbose and i % 200 == 0:
                con.commit()
                print("    ... %s 已检查 %d/%d，补 %d 期"
                      % (code, i, len(cand), st["filled_issues"]), flush=True)
            time.sleep(sleep)
        con.commit()
        if st["checked"]:
            log_sync(con, "zhcw_region:%s" % code, started, st["filled_issues"],
                     "aborted" if aborted else
                     ("ok" if not st["failed"] else "partial"),
                     note="checked=%d rows=%d empty=%d skipped_nohit=%d"
                          " skipped_checked=%d failed=%d%s"
                          % (st["checked"], st["region_rows"], st["empty"],
                             st["skipped_no_winner"], st["skipped_checked"],
                             st["failed"],
                             "" if not aborted
                             else " aborted=" + st.get("abort_reason", "")[:80]))
        out[code] = st
        if verbose:
            if aborted:
                print("  [zhcw-region] %-4s ★中止：%s"
                      % (code, st.get("abort_reason", "")), flush=True)
            print("  [zhcw-region] %-4s 检查 %-5d → 补 %-5d 期 / %-6d 行"
                  "（更新 draw %d，源头无分省 %d，失败 %d，无人中跳过 %d，已探过跳过 %d）"
                  % (code, st["checked"], st["filled_issues"], st["region_rows"],
                     st["draw_updated"], st["empty"], st["failed"],
                     st["skipped_no_winner"], st["skipped_checked"]), flush=True)
        if aborted:
            break               # 已被限流，后面的彩种别试了（续跑时再补）
    return out


def reparse_zhcw_region(con, codes=None, verbose=True) -> dict:
    """解析器改进后，用**已存的 `first_region_raw` 原文离线重建** `prize_region(source='zhcw')`。

    不发任何网络请求；只动 source='zhcw' 的行（不碰 cwl 的福彩行）。
    ★为什么需要：2026-11-01 首次回填后发现有 132 期分省合计比官方一等奖少 1~2 注，
      根因是老期原文里的排版空格（「甘 肃」「浙 江」「追加 1注」）与「内蒙」缩写。
      解析器修好后，原文已经完整存在库里，直接重解析即可修正，不必再抓一遍。
    ★只处理「当前已有 zhcw 分省行」的期：避免把 cwl 原文也重新解析成 zhcw 行而双计。
    """
    codes = list(codes or ZHCW_REGION_CODES)
    out = {}
    for code in codes:
        try:
            issues = [r[0] for r in con.execute(
                "SELECT DISTINCT issue FROM prize_region WHERE lottery=? AND source='zhcw'",
                (code,))]
        except sqlite3.Error:
            issues = []
        raws = {}
        for issue in issues:
            row = con.execute(
                "SELECT first_region_raw FROM draw WHERE lottery=? AND issue=? "
                "AND first_region_raw IS NOT NULL AND first_region_raw<>'' LIMIT 1",
                (code, issue)).fetchone()
            if row:
                raws[issue] = row[0]
        changed = 0
        for issue, raw in raws.items():
            new = sources.parse_region_text(raw)
            old = con.execute(
                "SELECT region, winners FROM prize_region"
                " WHERE lottery=? AND issue=? AND source='zhcw'", (code, issue)).fetchall()
            if sorted(new) == sorted(old):
                continue
            con.execute("DELETE FROM prize_region WHERE lottery=? AND issue=? AND source='zhcw'",
                        (code, issue))
            for region, n in new:
                con.execute(
                    "INSERT OR REPLACE INTO prize_region(lottery,issue,source,region,winners)"
                    " VALUES(?,?,?,?,?)", (code, issue, "zhcw", region, n))
            changed += 1
        con.commit()
        out[code] = {"issues": len(raws), "changed": changed}
        if verbose:
            print("  [zhcw-reparse] %-4s 依据 %d 期原文重建，修正 %d 期"
                  % (code, len(raws), changed), flush=True)
    return out


def check_zhcw_region(con, codes=None, verbose=True, sample=8) -> dict:
    """分省数据自检：**分省合计 vs 参考一等奖注数**，逐期比对，给四类判决。

    判决（每期一个）：
      ok            分省合计 == 参考值
      ok_explained  分省合计 + 非地域口径（积分换彩）== 参考值
      src_conflict  分省合计 == **原文自述的「共N注」**，但与奖级表不一致
                    ⇒ 解析是忠实的，矛盾在源头自己（实测 ssq 2008065：
                      原文「北京1注,…共11注」合计 11，而奖级表一等奖只有 4）。
                      这类不能算「偏多」——把它当解析错去改，只会越改越假。
      low           分省合计 < 参考值（漏省 / 解析丢注）
      high          分省合计 > 参考值且原文也没这么说（重复计数 / 追加当额外注）
      noref         没有参考值（奖项行缺失），不判

    ★参考值取**各源 level=1 的最大值**（`_reference_first_winners`）：
      大乐透派奖期 sporttery 的 level=1 行是「一等奖基本派奖」子集，
      单看一个源会把 7 读成 4 ⇒ 假报「偏多」。
    ★同时检查跨源双计：同一期若同时有 cwl 与 zhcw 两套地区行，
      `v_region_prize` 会把注数加两遍 —— 这里直接点名，不靠人眼看汇总。
    """
    codes = list(codes or ZHCW_REGION_CODES)
    out = {}
    for code in codes:
        ref = _reference_first_winners(con, code)
        try:
            rows = con.execute(
                "SELECT issue, source, region, winners FROM prize_region WHERE lottery=?",
                (code,)).fetchall()
        except sqlite3.Error:
            rows = []
        # 每期：按源累计，同时记下该期覆盖了哪些省
        per: dict = {}
        for iss, src, region, n in rows:
            d = per.setdefault(iss, {"sum": 0, "sources": set()})
            d["sum"] += int(n or 0)
            d["sources"].add(src)
        # 原文（用于识别「积分换彩」这类没有省份口径的中奖）
        raws: dict = {}
        try:
            for iss, raw in con.execute(
                    "SELECT issue, first_region_raw FROM draw WHERE lottery=?"
                    " AND first_region_raw IS NOT NULL AND first_region_raw<>''",
                    (code,)):
                raws.setdefault(iss, raw)
        except sqlite3.Error:
            raws = {}

        st = dict(issues=0, ok=0, ok_explained=0, src_conflict=0, low=0, high=0, noref=0,
                  multi_source=0, ok_multi=0,
                  sum_region=0, sum_ref=0, low_samples=[], high_samples=[],
                  multi_samples=[], conflict_samples=[])
        for iss, d in per.items():
            st["issues"] += 1
            multi = len(d["sources"]) > 1
            if multi:
                st["multi_source"] += 1
                if len(st["multi_samples"]) < sample:
                    st["multi_samples"].append((iss, sorted(d["sources"]), d["sum"]))
            r = ref.get(iss, None)
            if r is None:
                st["noref"] += 1
                continue
            st["sum_region"] += d["sum"]
            st["sum_ref"] += r
            raw = raws.get(iss, "")
            # 没有省份口径的中奖（积分换彩）算进「已解释」
            bucket = sum(n for _, n in sources.parse_non_region_buckets(raw))
            declared = sources.parse_total_from_text(raw)
            if d["sum"] == r:
                st["ok"] += 1
                if multi:
                    # ★两源相加恰好等于参考值 ⇒ 数字「对」了，但口径是错的（同一批注算了两遍）
                    st["ok_multi"] += 1
            elif bucket and d["sum"] + bucket == r:
                st["ok_explained"] += 1
            elif declared is not None and d["sum"] == declared:
                # 解析忠实于原文，但原文与奖级表打架 ⇒ 不是我们的错，单独报
                st["src_conflict"] += 1
                if len(st["conflict_samples"]) < sample:
                    st["conflict_samples"].append((iss, d["sum"], r, declared))
            elif d["sum"] < r:
                st["low"] += 1
                if len(st["low_samples"]) < sample:
                    st["low_samples"].append((iss, d["sum"], r))
            else:
                st["high"] += 1
                if len(st["high_samples"]) < sample:
                    st["high_samples"].append((iss, d["sum"], r))
        out[code] = st
        if verbose:
            print("  [region-check] %-4s %5d 期 → 相符 %5d / 非地域已解释 %d / 源内矛盾 %d"
                  " / 偏少 %5d / 偏多 %5d / 无参考 %d"
                  "　(分省合计 %d，参考合计 %d%s)"
                  % (code, st["issues"], st["ok"], st["ok_explained"], st["src_conflict"],
                     st["low"], st["high"], st["noref"],
                     st["sum_region"], st["sum_ref"],
                     "" if not st["ok_multi"]
                     else "，其中多源假相符 %d" % st["ok_multi"]), flush=True)
            if st["conflict_samples"]:
                print("    源内矛盾（分省合计 == 原文自述「共N注」≠ 奖级表）: %s"
                      % [("期%s" % i, "分省%d" % a, "奖级%d" % b, "原文共%d" % c)
                         for i, a, b, c in st["conflict_samples"]], flush=True)
            if st["high_samples"]:
                print("    ★偏多（疑似重复计数/追加当额外注）: %s"
                      % [(i, "分省%d" % a, "参考%d" % b)
                         for i, a, b in st["high_samples"]], flush=True)
            if st["low_samples"]:
                print("    偏少样例: %s"
                      % [("期%s" % i, "分省%d" % a, "参考%d" % b)
                         for i, a, b in st["low_samples"]], flush=True)
            if st["multi_samples"]:
                print("    ★同一期多源分省行（汇总会双计）: %s"
                      % [(i, s) for i, s, _ in st["multi_samples"]], flush=True)
    return out


def reparse_region_all(con, codes=None, verbose=True, dry_run=False,
                       fill_missing=True) -> dict:
    """解析器改进后，用**已存原文**重建**所有源**的 `prize_region`（不发网络请求）。

    与 `reparse_zhcw_region` 的分工：
      - `reparse_zhcw_region` 只重建 `source='zhcw'` 的行（中彩网回填那批）。
      - 本函数按「该期现有行属于哪个源」原样重建，覆盖 `cwl` 的福彩老数据。

    ★为什么需要（2026-11-01 自检发现，见 `check_zhcw_region`）：
      1. 「其中复式投注为…/其中复式或胆拖投注为…/其中一等奖特别奖为…」是**子集细分**，
         旧解析把这一段也累加 ⇒ 双色球 176 期、七乐彩 21 期虚高（2016132 分省 51 / 官方 37）。
      2. 「深圳」不在省名表 ⇒ 与「广东」并排的深圳注数整条丢失 ⇒ 双色球 301 期偏少。
      两处都已修在 `sources.parse_region_text`，原文完整在库，离线重建即可，不必重抓。

    ★保留每期原有的 `source` 标签、且**只在「该期恰好一个源、一份原文」时动**
      （多源/多原文说明口径不明，宁可跳过并点名，也不猜）。
    ★`fill_missing=True`：还要补「**有原文但一条行都没有**」的期。
      这类期是解析器旧版漏掉的（例：2014 年前后大乐透用全角括号「上海（基本1 注）」，
      旧正则只认半角 ⇒ 整期解析成空），它们填不进 prize_region 就会被 `only_missing`
      当缺口**永久重抓**。源标签按彩种定：福彩三彩种记 `cwl`（原文来自福彩官网），
      大乐透记 `zhcw`（原文来自中彩网详情接口）——与既有行的标法一致。
      ★仍然只在该期**没有任何行**时补，跨源双计的那条不变量不变。
    ★`dry_run=True` 只统计不改库，供先看影响面。
    """
    codes = list(codes or ZHCW_REGION_CODES)
    fs_codes = ("ssq", "qlc", "kl8")
    out = {}
    for code in codes:
        try:
            issues = [r[0] for r in con.execute(
                "SELECT DISTINCT issue FROM prize_region WHERE lottery=?", (code,))]
        except sqlite3.Error:
            issues = []
        st = dict(issues=len(issues), changed=0, unchanged=0, ambiguous=0,
                  unparsed=0, samples=[], delta=0, filled_new=0, new_rows=0)
        for issue in issues:
            owners = sorted({r[0] for r in con.execute(
                "SELECT DISTINCT source FROM prize_region WHERE lottery=? AND issue=?",
                (code, issue))})
            raws = sorted({r[0] for r in con.execute(
                "SELECT first_region_raw FROM draw WHERE lottery=? AND issue=?"
                " AND first_region_raw IS NOT NULL AND first_region_raw<>''",
                (code, issue))})
            if len(owners) != 1 or len(raws) != 1:
                st["ambiguous"] += 1
                if len(st["samples"]) < 6:
                    st["samples"].append((issue, owners, len(raws)))
                continue
            src = owners[0]
            new = sources.parse_region_text(raws[0])
            if not new:
                st["unparsed"] += 1
                continue
            old = sorted(con.execute(
                "SELECT region, winners FROM prize_region"
                " WHERE lottery=? AND issue=? AND source=?", (code, issue, src)).fetchall())
            new_sorted = sorted(new)
            if new_sorted == old:
                st["unchanged"] += 1
                continue
            st["changed"] += 1
            st["delta"] += sum(n for _, n in new) - sum(n for _, n in old)
            if dry_run:
                continue
            con.execute("DELETE FROM prize_region WHERE lottery=? AND issue=?",
                        (code, issue))
            for region, n in new_sorted:
                con.execute(
                    "INSERT OR REPLACE INTO prize_region(lottery,issue,source,region,winners)"
                    " VALUES(?,?,?,?,?)", (code, issue, src, region, n))

        # 2) 有原文但一条行都没有的期（旧解析器整期漏掉，如全角括号）→ 补建
        if fill_missing:
            have = set(issues)
            src_label = "cwl" if code in fs_codes else "zhcw"
            try:
                cand = [(r[0], r[1]) for r in con.execute(
                    "SELECT issue, first_region_raw FROM draw WHERE lottery=?"
                    " AND first_region_raw IS NOT NULL AND first_region_raw<>''"
                    " GROUP BY issue", (code,))]
            except sqlite3.Error:
                cand = []
            for issue, raw in cand:
                if issue in have:
                    continue
                rows_new = sources.parse_region_text(raw)
                if not rows_new:
                    st["unparsed"] += 1
                    continue
                st["filled_new"] += 1
                st["new_rows"] += len(rows_new)
                st["delta"] += sum(n for _, n in rows_new)
                if dry_run:
                    continue
                for region, n in sorted(rows_new):
                    con.execute(
                        "INSERT OR REPLACE INTO prize_region"
                        "(lottery,issue,source,region,winners) VALUES(?,?,?,?,?)",
                        (code, issue, src_label, region, n))

        if not dry_run:
            con.commit()
        out[code] = st
        if verbose:
            print("  [region-reparse] %-4s %5d 期 → 修正 %5d（合计 %+d 注）"
                  " / 不变 %5d / 新补 %d 期·%d 行 / 口径不明跳过 %d / 解析不出 %d%s"
                  % (code, st["issues"], st["changed"], st["delta"], st["unchanged"],
                     st["filled_new"], st["new_rows"], st["ambiguous"], st["unparsed"],
                     "　[dry-run 未写库]" if dry_run else ""), flush=True)
            if st["samples"]:
                print("    跳过样例（期, 源, 原文份数）: %s" % st["samples"], flush=True)
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

    # ★分省头奖按彩种汇总：不能只看上表「地区行」列 —— 那一列按 (lottery, source) 配对，
    #   而大乐透的分省行 source='zhcw' 却没有对应的 zhcw draw 行，会显示成 0。
    print()
    print("分省头奖行数（按彩种/源）:")
    any_region = False
    for r in con.execute(
            "SELECT lottery, source, COUNT(*), COUNT(DISTINCT issue) FROM prize_region"
            " GROUP BY lottery, source ORDER BY lottery, source"):
        print("  %-4s %-6s %6d 行 / %5d 期" % r)
        any_region = True
    if not any_region:
        print("  （无）")

    # ★「查过、源头没有」与「还没查过」必须分开报：都表现为「没有分省行」，
    #   但前者不该再抓（否则永久重抓），后者才是真实缺口。
    try:
        probed = con.execute(
            "SELECT lottery, SUM(outcome='empty'), SUM(outcome='filled'), COUNT(*)"
            " FROM region_probe GROUP BY lottery").fetchall()
    except sqlite3.Error:
        probed = []
    if probed:
        print()
        print("分省抓取探针（查过但源头无分省 / 查过有数据 / 合计）:")
        for code, n_empty, n_filled, n_all in probed:
            print("  %-4s 源头无分省 %5d / 有数据 %5d / 合计 %5d"
                  % (code, n_empty or 0, n_filled or 0, n_all))

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
    ap.add_argument("--zhcw-region", action="store_true",
                    help="中彩网详情接口：补头奖分省（prize_region + first_region_raw）")
    ap.add_argument("--zhcw-region-codes", metavar="CODES",
                    help="--zhcw-region 只补这些彩种，逗号分隔（默认 dlt,ssq,qlc,kl8）")
    ap.add_argument("--zhcw-region-limit", type=int, default=None,
                    help="--zhcw-region 每彩种最多补多少期（默认全部；0 视为全部）")
    ap.add_argument("--zhcw-region-force", action="store_true",
                    help="--zhcw-region 连已有分省行的期也重抓（会与 cwl 双计，仅调试用）")
    ap.add_argument("--zhcw-region-reparse", action="store_true",
                    help="用已存的 first_region_raw 离线重建 prize_region(zhcw)（解析器改进后修数）")
    ap.add_argument("--zhcw-region-sleep", type=float, default=0.2,
                    help="--zhcw-region 每次请求后的间隔秒（冷却期建议 2~3）")
    ap.add_argument("--zhcw-region-maxfail", type=int, default=20,
                    help="--zhcw-region 连续失败多少次就中止（默认 20，防限流）")
    ap.add_argument("--zhcw-region-check", action="store_true",
                    help="分省自检：逐期比对分省合计 vs 参考一等奖注数，并查同源双计")
    ap.add_argument("--region-reparse-all", action="store_true",
                    help="用已存原文离线重建**所有源**的 prize_region（解析器改进后修数）")
    ap.add_argument("--region-dry-run", action="store_true",
                    help="配合 --region-reparse-all：只统计影响面，不写库")
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

    if args.zhcw_region:
        codes = ([c.strip() for c in args.zhcw_region_codes.split(",") if c.strip()]
                 if args.zhcw_region_codes else None)
        limit = args.zhcw_region_limit or None
        print("[zhcw-region] 中彩网详情接口补头奖分省")
        st = build_zhcw_region(con, codes=codes, limit_per_code=limit,
                               only_missing=not args.zhcw_region_force,
                               skip_no_winner=not args.zhcw_region_force,
                               skip_checked=not args.zhcw_region_force,
                               sleep=args.zhcw_region_sleep,
                               max_consecutive_failures=args.zhcw_region_maxfail)
        for code, s in st.items():
            print("  %-4s 检查 %d 期，补 %d 期 / %d 行，源头无分省 %d，失败 %d，"
                  "无人中跳过 %d，已探过跳过 %d%s"
                  % (code, s["checked"], s["filled_issues"], s["region_rows"],
                     s["empty"], s["failed"], s["skipped_no_winner"],
                     s["skipped_checked"],
                     "" if not s.get("aborted") else "　★中止：" + s.get("abort_reason", "")))

    if args.zhcw_region_reparse:
        codes = ([c.strip() for c in args.zhcw_region_codes.split(",") if c.strip()]
                 if args.zhcw_region_codes else None)
        print("[zhcw-reparse] 依据已存原文离线重建分省")
        st = reparse_zhcw_region(con, codes=codes)
        for code, s in st.items():
            print("  %-4s 原文 %d 期，修正 %d 期" % (code, s["issues"], s["changed"]))

    if args.zhcw_region_check:
        codes = ([c.strip() for c in args.zhcw_region_codes.split(",") if c.strip()]
                 if args.zhcw_region_codes else None)
        print("[region-check] 分省合计 vs 参考一等奖注数")
        st = check_zhcw_region(con, codes=codes)
        bad = sum(s["low"] + s["high"] for s in st.values())
        print("  合计：不符 %d 期%s" % (bad, "" if not bad else "（见上文明细）"))

    if args.region_reparse_all:
        codes = ([c.strip() for c in args.zhcw_region_codes.split(",") if c.strip()]
                 if args.zhcw_region_codes else None)
        print("[region-reparse] 依据已存原文离线重建分省（全部源）")
        reparse_region_all(con, codes=codes, dry_run=args.region_dry_run)

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
                args.zhcw_region, args.zhcw_region_reparse, args.zhcw_region_check,
                args.region_reparse_all,
                args.mof, args.mof_all, args.mof_months, args.init]):
        ap.print_help()
        return 0

    print("\n完成，用时 %.1fs" % (time.time() - t0))
    status(con)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
