# -*- coding: utf-8 -*-
"""viewer 自检：不启服务，用 Flask test client 打所有接口。

覆盖：
  - 总数是否与库里一致（38252）
  - 各筛选条件是否真的生效（逐条断言，不看“有没有报错”）
  - 号码精确匹配不能误命（13 不应命中只含 31 的期）
  - 排序是否真的有序
  - 导出 CSV 行数与筛选总数一致
  - 详情页字段是否齐全
"""
import io
import json
import sys

sys.path.insert(0, r"E:\707")
from market.viewer import app, ensure_cache, cache_info  # noqa: E402

print("构建查询缓存…")
ensure_cache()
print("缓存:", cache_info())

OK = FAIL = 0


def check(name, cond, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print("  PASS  %s" % name)
    else:
        FAIL += 1
        print("  FAIL  %s  %s" % (name, extra))


def get(path):
    with app.test_client() as c:
        r = c.get(path)
        return r.status_code, r.data


c = app.test_client()

print("=== 1. 元数据 ===")
code, body = get("/api/meta")
m = json.loads(body)
check("meta 200", code == 200)
check("总期数 38252", m["total"] == 38252, "实际 %s" % m["total"])
check("8 个彩种", len(m["lotteries"]) == 8, "实际 %d" % len(m["lotteries"]))
check("彩种期数合计 = 总数",
      sum(x["n"] for x in m["lotteries"]) == m["total"],
      "%d vs %d" % (sum(x["n"] for x in m["lotteries"]), m["total"]))
check("ssq 3517 期", [x for x in m["lotteries"] if x["code"] == "ssq"][0]["n"] == 3517)
check("源列表含 csv500", any(s["source"] == "csv500" for s in m["sources"]))

print("=== 2. 分页 ===")
d = json.loads(c.get("/api/draws?size=50&page=1").data)
check("无筛选 total=38252", d["total"] == 38252, "实际 %s" % d["total"])
check("首页 50 行", len(d["rows"]) == 50, "实际 %d" % len(d["rows"]))
check("pages 正确", d["pages"] == (38252 + 49) // 50, "实际 %d" % d["pages"])
d2 = json.loads(c.get("/api/draws?size=50&page=2").data)
check("第 2 页与第 1 页不重复",
      d["rows"][0]["issue"] != d2["rows"][0]["issue"]
      or d["rows"][0]["lottery"] != d2["rows"][0]["lottery"])
check("末页不越界", json.loads(c.get("/api/draws?size=50&page=%d" % d["pages"]).data)["page"] == d["pages"])

print("=== 3. 彩种筛选 ===")
for lot, want in (("ssq", 3517), ("pl3", 7754), ("kl8", 2100), ("qxc", 3404)):
    d = json.loads(c.get("/api/draws?lottery=%s&size=10" % lot).data)
    check("彩种 %s = %d 期" % (lot, want), d["total"] == want, "实际 %s" % d["total"])
d = json.loads(c.get("/api/draws?lottery=ssq,pl3&size=10").data)
check("多选 ssq+pl3 = 11271", d["total"] == 3517 + 7754, "实际 %s" % d["total"])

print("=== 4. 号码检索（精确性）===")
d = json.loads(c.get("/api/draws?lottery=ssq&q=13&size=10").data)
check("q=13 有结果", d["total"] > 0, "实际 %s" % d["total"])
bad = 0
for r in d["rows"]:
    if 13 not in (r["front"] or []) and 13 not in (r["back"] or []) \
       and 13 not in (r["seq"] or []):
        bad += 1
check("q=13 每行都真含 13", bad == 0, "%d 行不含" % bad)

d = json.loads(c.get("/api/draws?lottery=ssq&q=13,14,16&size=10").data)
bad = 0
for r in d["rows"]:
    s = set(r["front"] or []) | set(r["back"] or []) | set(r["seq"] or [])
    if not {13, 14, 16} <= s:
        bad += 1
check("多号码 AND 生效", bad == 0, "%d 行不满足" % bad)
check("多号码结果少于单号码", d["total"] < json.loads(
    c.get("/api/draws?lottery=ssq&q=13&size=10").data)["total"])

d = json.loads(c.get("/api/draws?lottery=ssq&q=2026120&size=10").data)
check("期号检索命中", d["total"] >= 1 and d["rows"][0]["issue"] in ("2026120", "26120"),
      "实际 %s" % d["total"])

print("=== 5. 日期筛选 ===")
d = json.loads(c.get("/api/draws?lottery=ssq&date_from=2026-01-01&date_to=2026-12-31&size=500").data)
check("2026 年 ssq 有 120 期左右", 100 <= d["total"] <= 160, "实际 %s" % d["total"])
check("日期边界内", all("2026-01-01" <= r["draw_date"] <= "2026-12-31" for r in d["rows"]))

print("=== 6. 高级筛选 ===")
d = json.loads(c.get("/api/draws?has_region=1&size=200").data)
check("有分省中奖 4989 期", d["total"] == 4989, "实际 %s" % d["total"])
check("分省字段都非空", all(r["region_raw"] for r in d["rows"]))

d = json.loads(c.get("/api/draws?has_seq=1&size=200").data)
check("有出球序 16576 期", d["total"] == 16576, "实际 %s" % d["total"])
check("出球序都非空", all(r["seq"] for r in d["rows"]))

d = json.loads(c.get("/api/draws?lottery=ssq&sales_min=40000&size=10").data)
check("销售额≥4亿 有结果", d["total"] > 0, "实际 %s" % d["total"])
check("销售额筛选生效", all((r["sales"] or 0) >= 400000000 for r in d["rows"]),
      str([r["sales"] for r in d["rows"][:3]]))

d = json.loads(c.get("/api/draws?lottery=ssq&fw_min=10&size=10").data)
check("一等奖≥10注 有结果", d["total"] > 0, "实际 %s" % d["total"])
check("头奖注数筛选生效", all((r["fw"] or 0) >= 10 for r in d["rows"]))

print("=== 7. 排序 ===")
d = json.loads(c.get("/api/draws?lottery=ssq&sort=sales&order=desc&size=20").data)
vals = [r["sales"] for r in d["rows"] if r["sales"]]
check("销售额降序", vals == sorted(vals, reverse=True), str(vals[:3]))
d = json.loads(c.get("/api/draws?lottery=ssq&sort=draw_date&order=asc&size=20").data)
dates = [r["draw_date"] for r in d["rows"]]
check("日期升序", dates == sorted(dates), str(dates[:2]))
d = json.loads(c.get("/api/draws?sort=draw_date&order=desc&size=20").data)
dates = [r["draw_date"] for r in d["rows"]]
check("日期降序", dates == sorted(dates, reverse=True), str(dates[:2]))

print("=== 8. 详情 ===")
d = json.loads(c.get("/api/detail?lottery=ssq&issue=2026120").data)
check("详情 200", "error" not in d, str(d.get("error")))
check("有号码", bool(d["merged"]["front"]), str(d["merged"]["front"]))
check("有出球序（跨源合并）", bool(d["merged"]["seq_front"]),
      str(d["merged"].get("seq_front")))
check("有分省", len(d["regions"]) > 0, "实际 %d" % len(d["regions"]))
check("有奖级", len(d["prizes"]) > 0, "实际 %d" % len(d["prizes"]))
check("多源", len(d["sources"]) >= 2, "实际 %d" % len(d["sources"]))

d = json.loads(c.get("/api/detail?lottery=kl8&issue=2026279").data)
check("快乐8 详情 39 档奖级", len(d["prizes"]) >= 39, "实际 %d" % len(d["prizes"]))

code, _ = get("/api/detail?lottery=ssq&issue=9999999")
check("不存在的期返回 404", code == 404, "实际 %s" % code)

print("=== 9. 导出 ===")
code, body = get("/api/export?lottery=kl8")
lines = body.decode("utf-8-sig").splitlines()
check("导出 200", code == 200)
check("导出含 BOM", body[:3] == b"\xef\xbb\xbf")
check("导出行数 = 2100+1", len(lines) == 2101, "实际 %d" % len(lines))
check("表头正确", lines[0].startswith("彩种,期号,开奖日"), lines[0][:40])
check("首行有号码", len(lines[1].split(",")) >= 10, lines[1][:60])

print("=== 10. 页面与只读性 ===")
code, body = get("/")
check("首页 200", code == 200)
check("页面含关键元素", b'id="tbody"' in body and b'id="q"' in body and b'id="drawer"' in body)
check("页面含分组样式与入口", b"tr.dayhead" in body and b'id="btnDateOrder"' in body)
check("healthz", json.loads(c.get("/healthz").data)["ok"] is True)


print("=== 11. 按天分组的计数口径 ===")
# ★ 一天 5~8 期、一页 50 期 ⇒ 一页约 7~9 天，首尾那天必然被页边界切开。
#   前端只拿得到本页的行，自己数出来的「当天 N 期」在跨页那天会少算，
#   所以 day_counts 必须由服务端按同一套筛选条件 GROUP BY 出来。
d = json.loads(c.get("/api/draws?size=50&page=1").data)
dc = d.get("day_counts") or {}
check("返回 day_counts", bool(dc), "类型 %s" % type(dc).__name__)

page_days = {}
for r in d["rows"]:
    page_days[r["draw_date"]] = page_days.get(r["draw_date"], 0) + 1
check("本页每个日期都有计数", set(page_days) == set(dc),
      "缺 %s / 多 %s" % (set(page_days) - set(dc), set(dc) - set(page_days)))
check("每天计数 ≥ 本页行数（跨页只多不少）",
      all(dc.get(k, 0) >= v for k, v in page_days.items()),
      str({k: (dc.get(k), v) for k, v in page_days.items() if dc.get(k, 0) < v}))
split = [k for k, v in page_days.items() if dc.get(k, 0) > v]
check("首/尾那天被页边界切开且如实给出全量期数", len(split) >= 1,
      "跨页 %d 天：%s" % (len(split), split))
check("未跨页的那天计数 == 本页行数",
      any(dc.get(k) == v for k, v in page_days.items()),
      str(list(page_days.items())[:3]))

# 筛选条件必须同样作用到 day_counts 上（不能拿全库口径来对某一天）
d = json.loads(c.get("/api/draws?lottery=pl3&date_from=2026-10-18"
                     "&date_to=2026-10-20&size=50").data)
check("筛选后当日期数按筛选口径算（排3 一天 1 期）",
      d["day_counts"] == {"2026-10-18": 1, "2026-10-19": 1, "2026-10-20": 1},
      str(d["day_counts"]))

# 空结果不能炸（dates 为空时直接返回 {}）
d = json.loads(c.get("/api/draws?q=2099-01-01&size=10").data)
check("空结果 day_counts 为空字典", d["day_counts"] == {} and d["rows"] == [],
      str(d["day_counts"]))

print("=== 12. 只读性 ===")

import sqlite3  # noqa: E402
from market.viewer import DB_PATH  # noqa: E402
try:
    ro = sqlite3.connect("file:%s?mode=ro" % DB_PATH.replace("\\", "/"), uri=True)
    ro.execute("DELETE FROM draw WHERE 0")
    check("只读连接拒绝写入", False, "居然写成功了")
except sqlite3.OperationalError as e:
    check("只读连接拒绝写入", "readonly" in str(e).lower() or "attempt" in str(e).lower(), str(e))

print()
print("=" * 46)
print("PASS %d   FAIL %d" % (OK, FAIL))
sys.exit(1 if FAIL else 0)
