"""market.db 每日更新编排（调度器 / 页面按钮共用的唯一入口）

用法：
    python -m market.update                 # 全量四源
    python -m market.update --only csv      # 只跟本地 CSV（约 2 秒）
    python -m market.update --no-cache      # 不通知数据浏览器重建缓存

设计要点（为什么不是直接跑四遍 build）：
  1. **单源失败不中断**：网络抓取随时可能超时/改版，cwl 挂了不该让 csv 的成果白跑。
     每个源独立 try/except，结果逐源记录，最后汇总 ok/partial。
  2. **文件锁防并发**：调度器 23:10 自动跑，页面按钮随时可能被人点 —— 两路同时写
     同一个库会互相等锁（WAL 下能过但会拖慢），更重要的是日志会串。用一把
     `.update.lock` 串行化；超过 40 分钟视为上次进程被杀留下的陈旧锁，允许接管。
  3. **写完通知数据浏览器重建缓存**：cache.db 是物化视图，不重建的话界面还显示旧数据。
     数据浏览器没运行就跳过（它下次启动会按指纹自己重建）。
  4. **幂等**：底层全是 INSERT OR REPLACE，重复跑只是覆盖，不会重复计数。

边界：只写 market/market.db 与通知 5001，不碰 lottery_data/*.csv、不碰主库、不碰预测。
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
DB_PATH = os.path.join(HERE, "market.db")
LOCK_PATH = os.path.join(HERE, ".update.lock")
VIEWER_REBUILD = "http://127.0.0.1:5001/api/rebuild"

if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

DEFAULT_SOURCES = ("csv", "cwl", "sporttery", "zhcw", "mof")

# 增量模式的翻页深度。官方源都是「最新在前」，所以第 1 页就是最近的期：
#   福彩官方 pageSize=500 → 1 页 = 最近 500 期（覆盖双色球 3 年）
#   体彩官方 pageSize=100 → 1 页 = 最近 100 期（覆盖数字型 3 个月）
#   中彩网一次取 count 条 → 200 条足够
# 全量翻页实测很慢：体彩单彩种要翻 78 页 × sleep 0.5s，晚间高峰能拖到 8 分钟/彩种，
# 四源合计 20 分钟以上。日常只是补最新几期，没必要每次都重扫二十年历史。
INCREMENTAL_PLAN = {
    "csv": {},                       # CSV 本身就是全量重读，只要 2 秒
    "cwl": {"max_pages": 1},
    "sporttery": {"max_pages": 1},
    "zhcw": {"count": 200},
    # 财政部只在每月下旬发上一月的数（如 10-19 发 9 月），所以日常只需看第 1 页；
    # 库里已有的月份由 build_mof(force=False) 跳过 ⇒ 抓到清单也几乎不花时间。
    "mof": {"max_pages": 1},
}

# 陈旧锁判定：正常全量约 3~5 分钟，留足余量
LOCK_STALE_SEC = 40 * 60

# 单次更新总时限（秒）。官方接口在晚间高峰会慢到早上的 10 倍（实测 sporttery
# 单彩种 28s → 5min），四个源叠起来能拖过一小时。23:10 起跑，25 分钟封顶，
# 保证最晚 23:35 收工、赶得上 23:40 的体检日报。
DEADLINE_SEC = 25 * 60

# 进程内防重入（文件锁之外的一道保险，避免同进程内两次点击都进到 Popen 级别）
_RUNNING = False


# ------------------------------------------------------------------ 锁

def _acquire_lock() -> bool:
    """拿更新锁。陈旧锁（上次进程被杀）允许接管。"""
    if os.path.exists(LOCK_PATH):
        try:
            age = time.time() - os.path.getmtime(LOCK_PATH)
        except OSError:
            age = LOCK_STALE_SEC + 1
        if age < LOCK_STALE_SEC:
            return False
        try:
            os.remove(LOCK_PATH)  # 陈旧锁，接管
        except OSError:
            return False
    try:
        fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:
        return False
    try:
        os.write(fd, ("%d %s" % (os.getpid(), datetime.now().isoformat(timespec="seconds"))).encode())
    finally:
        os.close(fd)
    return True


def _release_lock() -> None:
    try:
        if os.path.exists(LOCK_PATH):
            os.remove(LOCK_PATH)
    except OSError:
        pass


def is_running() -> bool:
    """当前是否有更新在跑（进程内标志或未过期的锁文件）。"""
    if _RUNNING:
        return True
    if not os.path.exists(LOCK_PATH):
        return False
    try:
        return (time.time() - os.path.getmtime(LOCK_PATH)) < LOCK_STALE_SEC
    except OSError:
        return False


# ------------------------------------------------------------------ 统计

def db_stats(path: str = None) -> dict:
    """各表行数 + 数据截止日期。库不存在时返回空结构。

    ★ 默认值写成 None 而不是 DB_PATH：默认参数在**函数定义时**就绑定了，
      写成 `path=DB_PATH` 的话，之后 monkeypatch / 测试改模块级 DB_PATH 根本不生效
      （会静默去读真实库）。这类 bug 在生产里表现为"改了路径但还是读老库"。
    """
    path = path or DB_PATH
    if not os.path.exists(path):
        return {}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=10)
        try:
            out = {}
            for t in ("lottery", "draw", "prize", "prize_region", "region_sales", "reconcile", "sync_log"):
                try:
                    out[t] = con.execute("SELECT COUNT(*) FROM %s" % t).fetchone()[0]
                except sqlite3.Error:
                    out[t] = None
            row = con.execute("SELECT MAX(draw_date), MAX(fetched_at) FROM draw").fetchone()
            out["latest_draw_date"] = row[0]
            out["latest_fetch"] = row[1]
            return out
        finally:
            con.close()
    except sqlite3.Error:
        return {}


def last_update(path: str = None) -> dict:
    """最近一次 update:all 的记录（给日报判断新鲜度用）。"""
    path = path or DB_PATH  # 同 db_stats：不能用 DB_PATH 当默认参数
    if not os.path.exists(path):
        return {"available": False, "reason": "market.db 不存在"}
    try:
        con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True, timeout=10)
        try:
            row = con.execute(
                "SELECT started_at, finished_at, rows, status, note FROM sync_log"
                " WHERE task='update:all' ORDER BY id DESC LIMIT 1").fetchone()
            latest = con.execute("SELECT MAX(draw_date), MAX(fetched_at) FROM draw").fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"available": False, "reason": "读库失败: %s" % e}

    if not row:
        return {"available": False, "reason": "从未全量更新过",
                "latest_draw_date": latest[0], "latest_fetch": latest[1]}
    note = {}
    try:
        note = json.loads(row[4] or "{}")
    except Exception:
        note = {"raw": row[4]}
    return {
        "available": True,
        "started_at": row[0],
        "finished_at": row[1],
        "rows": row[2],
        "status": row[3],
        "failed": note.get("failed") or [],
        "per_source": note.get("sources") or {},
        "latest_draw_date": latest[0],
        "latest_fetch": latest[1],
        "age_hours": _age_hours(row[1]),
    }


def _age_hours(ts: str):
    try:
        dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return round((datetime.now() - dt).total_seconds() / 3600, 1)
    except Exception:
        return None


# ------------------------------------------------------------------ 通知数据浏览器

def notify_viewer(timeout: float = 180.0):
    """让数据浏览器重建 cache.db。返回其响应 JSON，未运行返回 None。

    不重建的后果：库已经是最新，但界面还显示旧缓存（cache.db 是物化视图）。
    """
    try:
        with urllib.request.urlopen(VIEWER_REBUILD, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


# ------------------------------------------------------------------ 主流程

def run_update(sources=None, rebuild_cache: bool = True, log=print,
               deadline_sec: float = None, mode: str = "incremental") -> dict:
    """串行跑各源并汇总。返回结构化结果（不抛异常，失败体现在返回值里）。

    mode：
      - "incremental"（默认，日常）：只抓各源最近一页，十几秒完成
      - "full"（每周日 / 手动）：翻到底，重建完整历史（约 10~20 分钟）
    两者都是 INSERT OR REPLACE，所以增量只是少抓、不会写错；历史缺口靠周日全量兜。

    deadline_sec：整次更新的总时限。超过后不再启动新源，把剩余的标为
    `skipped`（不算失败，也不算成功），避免一次网络龟速把任务拖过一小时。
    """
    global _RUNNING

    import market.build as B

    mode = "full" if str(mode).lower() == "full" else "incremental"
    deadline = DEADLINE_SEC if deadline_sec is None else deadline_sec
    picked = tuple(sources or DEFAULT_SOURCES)
    unknown = [s for s in picked if s not in DEFAULT_SOURCES]
    if unknown:
        return {"ok": False, "error": "未知数据源: %s" % unknown,
                "allowed": list(DEFAULT_SOURCES)}

    if _RUNNING:
        return {"ok": False, "busy": True, "error": "本进程已有更新任务在运行，本次跳过"}

    # 只靠 _acquire_lock 判定跨进程占用：它内部会把「超过 40 分钟的陈旧锁」当作
    # 上次进程被杀的遗留并接管。若这里再叠一道 is_running()，陈旧锁就永远接管不了，
    # 会把自己锁死 40 分钟（踩过这个逻辑）。
    if not _acquire_lock():
        return {"ok": False, "busy": True,
                "error": "另一个更新任务正在运行，本次跳过（锁：%s）" % LOCK_PATH}

    _RUNNING = True
    started = datetime.now()
    t0 = time.time()
    results: dict = {}
    total_rows = 0

    try:
        if not os.path.exists(DB_PATH):
            log("market.db 不存在，先建空库")
            B.init_db().close()

        con = B.connect()
        try:
            for src in picked:
                # .get 而不是 []：mof 走下面的专属分支，不需要在表里挂号
                fn = {"csv": B.build_csv, "cwl": B.build_cwl,
                      "sporttery": B.build_sporttery, "zhcw": B.build_zhcw}.get(src)
                ts = time.time()
                # 总时限：只挡「还没开始」的源，已经在跑的不打断（打断会留下
                # 写了一半的中间状态，反而不如让它跑完）
                if deadline and (ts - t0) > deadline:
                    results[src] = {
                        "ok": False, "skipped": True, "rows": 0, "elapsed": 0.0,
                        "lotteries": 0,
                        "error": "超过总时限 %.0f 分钟，本次跳过" % (deadline / 60),
                    }
                    log("[%s] 跳过（已用 %.1f 分钟，超过时限）" % (src, (ts - t0) / 60))
                    continue
                log("[%s] 开始（%s）" % (src, mode))
                try:
                    kwargs = INCREMENTAL_PLAN.get(src, {}) if mode == "incremental" else {}
                    if src == "mof":
                        # ★mof 的返回结构跟其它源不同（{months,rows,failed,incomplete}
                        #   而不是 {彩种: {...}}），硬塞进通用汇总会把 62 行算成 0 期。
                        st = B.build_mof(con, verbose=False, **kwargs)
                        rows = int(st.get("rows") or 0)
                        nfail = len(st.get("failed") or {})
                        ok = bool(st.get("months")) or nfail == 0
                        results[src] = {
                            "ok": ok,
                            "rows": rows,
                            "elapsed": round(time.time() - ts, 1),
                            "months": st.get("months") or 0,
                            "failed_months": sorted((st.get("failed") or {}).keys())[:12],
                            "error": None if ok else "全部月份解析失败",
                        }
                    else:
                        detail = fn(con, verbose=False, **kwargs) or {}
                        rows = sum(int(v.get("draw") or 0) for v in detail.values())
                        # 一个彩种都没拿到 = 这次没成功（网络失败通常表现为抛异常，
                        # 但也可能是返回空 dict），不能记成 ok
                        ok = bool(detail)
                        results[src] = {
                            "ok": ok,
                            "rows": rows,
                            "elapsed": round(time.time() - ts, 1),
                            "lotteries": len(detail),
                            "error": None if ok else "未取到任何彩种数据",
                        }
                    total_rows += rows
                except Exception as e:
                    results[src] = {
                        "ok": False,
                        "rows": 0,
                        "elapsed": round(time.time() - ts, 1),
                        "lotteries": 0,
                        "error": "%s: %s" % (type(e).__name__, str(e)[:250]),
                    }
                if src == "mof":
                    log("[%s] %s，%.1fs，%d 个月 / %d 行" % (
                        src, "ok" if results[src]["ok"] else "失败",
                        results[src]["elapsed"], results[src].get("months", 0),
                        results[src]["rows"]))
                else:
                    log("[%s] %s，%.1fs，%d 期" % (
                        src, "ok" if results[src]["ok"] else "失败",
                        results[src]["elapsed"], results[src]["rows"]))

            failed = [s for s, r in results.items()
                      if not r["ok"] and not r.get("skipped")]
            skipped = [s for s, r in results.items() if r.get("skipped")]
            if failed:
                # 全部源都失败才算 fail；有源成功过就是 partial
                status = "fail" if len(failed) == len(results) else "partial"
            elif skipped:
                # 有源没轮到（超时）——不能报 ok，那会让人以为数据全了
                status = "partial"
            else:
                status = "ok"
            summary = {"mode": mode, "failed": failed, "skipped": skipped, "sources": results}
            try:
                B.log_sync(con, "update:all", started.strftime("%Y-%m-%d %H:%M:%S"),
                           total_rows, status,
                           json.dumps(summary, ensure_ascii=False)[:2000])
            except Exception as e:
                log("写 sync_log 失败（不影响数据）: %s" % e)
        finally:
            con.close()

        cache = None
        if rebuild_cache:
            cache = notify_viewer()
            log("数据浏览器缓存：%s" % ("已重建" if cache else "未运行/未重建"))

        return {
            "ok": status == "ok",
            "status": status,
            "mode": mode,
            "busy": False,
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.time() - t0, 1),
            "rows": total_rows,
            "failed": [s for s, r in results.items()
                       if not r["ok"] and not r.get("skipped")],
            "skipped": [s for s, r in results.items() if r.get("skipped")],
            "sources": results,
            "cache": cache,
            "db": db_stats(),
        }
    except Exception as e:
        # 走到这里是建库/连库级别的意外，如实返回而不是让调度线程崩掉
        return {
            "ok": False,
            "status": "fail",
            "busy": False,
            "started_at": started.strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed": round(time.time() - t0, 1),
            "rows": total_rows,
            "failed": list(results),
            "sources": results,
            "error": "%s: %s" % (type(e).__name__, str(e)[:300]),
        }
    finally:
        _RUNNING = False
        _release_lock()


# ------------------------------------------------------------------ CLI

def main() -> None:
    import argparse

    ap = argparse.ArgumentParser(description="更新 market.db（默认全量四源）")
    ap.add_argument("--only", default="", help="逗号分隔，只跑指定源：csv,cwl,sporttery,zhcw")
    ap.add_argument("--no-cache", action="store_true", help="不通知数据浏览器重建缓存")
    ap.add_argument("--full", action="store_true",
                    help="全量翻页（重建完整历史，约 10~20 分钟）；默认只抓各源最近一页")
    ap.add_argument("--json", action="store_true", help="只输出结果 JSON")
    a = ap.parse_args()

    picked = [x.strip() for x in a.only.split(",") if x.strip()] or None
    log = (lambda m: None) if a.json else (lambda m: print(m, flush=True))
    r = run_update(sources=picked, rebuild_cache=not a.no_cache, log=log,
                   mode="full" if a.full else "incremental")
    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2))
    else:
        print("\n结果：%s（%s，%s 模式，%.1fs，共 %s 期）"
              % (r.get("status"), "成功" if r.get("ok") else "有失败",
                 r.get("mode"), r.get("elapsed") or 0, r.get("rows")))
        if r.get("failed"):
            print("失败源：%s" % ", ".join(r["failed"]))
        if r.get("db"):
            d = r["db"]
            print("库：draw %s 期 / prize %s 行 / 分省头奖 %s 行 / 截止 %s"
                  % (d.get("draw"), d.get("prize"), d.get("prize_region"),
                     d.get("latest_draw_date")))
    sys.exit(0 if r.get("ok") else 1)


if __name__ == "__main__":
    main()
