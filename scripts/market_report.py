# -*- coding: utf-8 -*-
"""生成 market.db 的可视化浏览页（只读，不改库）。

用途：用户不装数据库工具也能看库里到底有什么。
输出：docs/数据库_浏览.html
"""
from __future__ import annotations

import html
import json
import os
import sqlite3
from datetime import datetime

DB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                  "market", "market.db")
OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                   "docs", "数据库_浏览.html")

NAME = {
    "ssq": "双色球", "dlt": "大乐透", "pl3": "排列3",
    "pl5": "排列5", "sd": "福彩3D", "qxc": "七星彩",
    "qlc": "七乐彩", "kl8": "快乐8",
}
SRC = {"cwl": "福彩官方", "sporttery": "体彩官方",
       "zhcw": "中彩网", "csv500": "现有CSV", "mof": "财政部"}


def esc(v):
    return html.escape("" if v is None else str(v))


def main() -> None:
    con = sqlite3.connect(DB, timeout=30)

    size_mb = os.path.getsize(DB) / 1024 / 1024
    counts = {}
    for t, in con.execute("SELECT name FROM sqlite_master WHERE type='table' "
                          "AND name NOT LIKE 'sqlite_%'"):
        counts[t] = con.execute("SELECT COUNT(*) FROM " + t).fetchone()[0]

    # ---------- 覆盖矩阵 ----------
    cov = con.execute("""
        SELECT d.lottery, d.source, COUNT(*) AS n,
               (SELECT COUNT(*) FROM prize p
                 WHERE p.lottery=d.lottery AND p.source=d.source) AS pz,
               (SELECT COUNT(*) FROM prize_region r
                 WHERE r.lottery=d.lottery AND r.source=d.source) AS rg,
               SUM(CASE WHEN d.seq_front IS NOT NULL
                        AND d.seq_front <> '' THEN 1 ELSE 0 END) AS sq,
               MIN(d.draw_date), MAX(d.draw_date)
        FROM draw d GROUP BY d.lottery, d.source
        ORDER BY d.lottery, d.source
    """).fetchall()

    cov_rows = []
    for lot, src, n, pz, rg, sq, d0, d1 in cov:
        cov_rows.append(
            "<tr><td class='c'><b>%s</b></td><td class='c sub'>%s</td>"
            "<td class='c'>%s</td><td class='n'>%d</td><td class='n'>%d</td>"
            "<td class='n%s'>%d</td><td class='n%s'>%d</td>"
            "<td class='d'>%s ~ %s</td></tr>" % (
                esc(NAME.get(lot, lot)), esc(lot),
                esc(SRC.get(src, src)), n, pz,
                " hot" if rg else "", rg,
                " hot" if sq else "", sq or 0,
                esc((d0 or "")[:10]), esc((d1 or "")[:10])))

    # ---------- 分省头奖 ----------
    def region_board(lot, limit=15):
        return con.execute("""
            SELECT region, SUM(winners) w, COUNT(DISTINCT issue) n
            FROM prize_region WHERE lottery=?
            GROUP BY region ORDER BY w DESC LIMIT ?
        """, (lot, limit)).fetchall()

    def board_html(lot, limit=15):
        rows = region_board(lot, limit)
        if not rows:
            return "<p class='empty'>该彩种没有分省数据</p>"
        top = rows[0][1] or 1
        out = []
        for region, w, n in rows:
            pct = 100.0 * (w or 0) / top
            out.append(
                "<div class='bar'><span class='bl'>%s</span>"
                "<span class='bt'><i style='width:%.1f%%'></i></span>"
                "<span class='bv'>%d 注 <em>%d 期</em></span></div>"
                % (esc(region), pct, w or 0, n))
        return "".join(out)

    # ---------- 最近分省 ----------
    latest = con.execute("""
        SELECT issue, draw_date, first_region_raw FROM draw
        WHERE lottery='ssq' AND first_region_raw IS NOT NULL
          AND first_region_raw NOT IN ('', '共0注。')
        ORDER BY draw_date DESC LIMIT 10
    """).fetchall()
    latest_rows = "".join(
        "<tr><td class='c'>%s</td><td class='c sub'>%s</td><td>%s</td></tr>"
        % (esc(i), esc((d or "")[:10]), esc(t)) for i, d, t in latest)

    # ---------- 分省销量 ----------
    # ★2026-10-21 起库里是 2008-01 起全量（224 个月 × 62 行 ≈ 1.4 万行）。
    #   原来的写法是「所有行平铺」，补齐后光这一段就是 1.8MB HTML，打开会卡死。
    #   改成：近 24 个月可下拉切换明细 + 全量月度汇总折叠。
    all_months = [r[0] for r in con.execute(
        "SELECT ym FROM region_sales GROUP BY ym ORDER BY ym DESC")]
    recent = all_months[:24]
    sales = []
    if recent:
        q = ("SELECT ym, region, agency, sales_wan, yoy, cumulative_wan FROM region_sales "
             # 福彩在前（按 agency 字面排序 '体彩' < '福彩'，会把体彩顶到上面）
             "WHERE ym IN (%s) ORDER BY ym DESC, "
             "CASE agency WHEN '福彩' THEN 0 ELSE 1 END, sales_wan DESC"
             % ",".join("?" * len(recent)))
        sales = con.execute(q, recent).fetchall()
    sales_rows = "".join(
        "<tr data-ym='%s'><td class='c sub'>%s</td><td class='c'>%s</td><td class='c'>%s</td>"
        "<td class='n'>%s</td><td class='n'>%s</td><td class='n'>%s</td></tr>" % (
            esc(ym), esc(ym), esc(r), esc(a),
            ("%.0f" % s) if s is not None else "—",
            ("%.1f%%" % y) if y is not None else "—",
            ("%.0f" % cu) if cu is not None else "—")
        for ym, r, a, s, y, cu in sales)
    ym_opts = "".join("<option value='%s'%s>%s</option>"
                      % (esc(m), " selected" if i == 0 else "", esc(m))
                      for i, m in enumerate(recent))
    # 全量月度汇总（各省当月合计 → 亿元）
    monthly_rows = "".join(
        "<tr><td class='c'>%s</td><td class='n'>%s</td><td class='n'>%s</td>"
        "<td class='n'><b>%s</b></td><td class='c sub'>%d</td></tr>" % (
            esc(ym),
            ("%.1f" % (fc / 10000)) if fc is not None else "—",
            ("%.1f" % (tc / 10000)) if tc is not None else "—",
            ("%.1f" % tot) if tot is not None else "—",
            n)
        for ym, fc, tc, tot, n in con.execute("""
            SELECT ym,
                   SUM(CASE WHEN agency='福彩' THEN sales_wan END),
                   SUM(CASE WHEN agency='体彩' THEN sales_wan END),
                   ROUND(SUM(sales_wan)/10000, 2),
                   COUNT(*)
            FROM region_sales GROUP BY ym ORDER BY ym DESC
        """))
    ym_span = ("%s — %s（共 %d 个月）" % (all_months[-1], all_months[0], len(all_months))
               if all_months else "—")

    # ---------- 出球顺序 ----------
    seq = con.execute("""
        SELECT issue, draw_date, front, seq_front FROM draw
        WHERE lottery='ssq' AND source='zhcw'
          AND seq_front IS NOT NULL AND seq_front <> ''
        ORDER BY draw_date DESC LIMIT 8
    """).fetchall()
    seq_rows = "".join(
        "<tr><td class='c'>%s</td><td class='c sub'>%s</td>"
        "<td class='mono'>%s</td><td class='mono hl'>%s</td></tr>"
        % (esc(i), esc((d or "")[:10]), esc(f), esc(s)) for i, d, f, s in seq)

    # ---------- 奖级样例 ----------
    pz = con.execute("""
        SELECT lottery, issue, source, level, level_name, winners, amount, amount_kind
        FROM prize WHERE lottery IN ('ssq','dlt','kl8')
        ORDER BY lottery, issue DESC, level LIMIT 18
    """).fetchall()
    pz_rows = "".join(
        "<tr><td class='c'>%s</td><td class='c sub'>%s</td><td class='c'>%s</td>"
        "<td class='c'>%s</td><td>%s</td><td class='n'>%s</td>"
        "<td class='n'>%s</td><td class='c sub'>%s</td></tr>" % (
            esc(NAME.get(l, l)), esc(i), esc(SRC.get(s, s)), esc(lv),
            esc(nm or "—"), esc(w if w is not None else "—"),
            esc(a if a is not None else "—"), esc(k or "—"))
        for l, i, s, lv, nm, w, a, k in pz)

    # ---------- 对账 ----------
    rec = con.execute("""
        SELECT lottery, COUNT(*), SUM(agree=1), SUM(agree=0), SUM(agree=-1)
        FROM reconcile GROUP BY lottery ORDER BY lottery
    """).fetchall()
    rec_rows = "".join(
        "<tr><td class='c'><b>%s</b></td><td class='n'>%d</td>"
        "<td class='n ok'>%d</td><td class='n%s'>%d</td>"
        "<td class='n sub'>%d</td></tr>" % (
            esc(NAME.get(l, l)), n, ok or 0,
            " bad" if (cf or 0) else "", cf or 0, z or 0)
        for l, n, ok, cf, z in rec)

    now = datetime.now().strftime("%Y-%m-%d %H:%M")

    doc = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>market.db 数据浏览</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--bd:#e3e6ec;--tx:#1b1f27;--sub:#6b7280;
--acc:#2563eb;--ok:#0f7b4f;--bad:#c0392b;--warm:#b45309;--hl:#eff6ff}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--tx);
font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:28px 20px 64px}
h1{font-size:24px;margin:0 0 4px;letter-spacing:-.3px}
.lead{color:var(--sub);margin:0 0 22px;font-size:13px}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;
padding:18px 20px;margin-bottom:18px}
.card h2{font-size:15px;margin:0 0 14px;display:flex;align-items:center;gap:8px}
.card h2 b{font-weight:600}
.card h2 .tag{font-weight:400;font-size:11px;color:var(--sub);
background:var(--bg);border:1px solid var(--bd);border-radius:20px;padding:1px 8px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;
margin-bottom:18px}
.kpi{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 16px}
.kpi .v{font-size:22px;font-weight:600;letter-spacing:-.5px}
.kpi .k{font-size:12px;color:var(--sub);margin-top:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;font-weight:500;color:var(--sub);font-size:12px;
border-bottom:1px solid var(--bd);padding:6px 8px;white-space:nowrap}
td{padding:6px 8px;border-bottom:1px solid #f0f2f5;vertical-align:top}
tr:last-child td{border-bottom:0}
td.c{text-align:center}td.n{text-align:right;font-variant-numeric:tabular-nums}
td.d{color:var(--sub);font-size:12px;white-space:nowrap}
td.sub{color:var(--sub);font-size:12px}
td.mono{font-family:ui-monospace,Consolas,monospace;font-size:12px}
td.hl{color:var(--acc)}
.hot{color:var(--warm);font-weight:600}
.ok{color:var(--ok);font-weight:600}.bad{color:var(--bad);font-weight:600}
.bar{display:flex;align-items:center;gap:10px;margin-bottom:5px;font-size:13px}
.bl{width:62px;flex:none;color:var(--tx)}
.bt{flex:1;height:16px;background:var(--bg);border-radius:4px;overflow:hidden}
.bt i{display:block;height:100%;background:linear-gradient(90deg,#3b82f6,#60a5fa);
border-radius:4px}
.bv{width:104px;flex:none;text-align:right;font-variant-numeric:tabular-nums;
font-size:12px;color:var(--sub)}
.bv em{font-style:normal;opacity:.7}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:22px}
@media(max-width:820px){.cols{grid-template-columns:1fr}}
.note{background:#fffbeb;border:1px solid #fde68a;color:#78350f;
border-radius:8px;padding:10px 14px;font-size:13px;margin-bottom:18px}
code{background:var(--bg);border:1px solid var(--bd);border-radius:4px;
padding:1px 5px;font-family:ui-monospace,Consolas,monospace;font-size:12px}
pre{background:#1b1f27;color:#e5e7eb;border-radius:8px;padding:14px 16px;
overflow-x:auto;font-size:12.5px;line-height:1.65;margin:0}
pre .c{color:#9ca3af}
.empty{color:var(--sub);font-size:13px;margin:0}
.scroll{max-height:420px;overflow:auto}
</style></head><body><div class="wrap">

<h1>market.db · 旁路补充库</h1>
<p class="lead">位于 <code>E:\\707\\market\\market.db</code> ·
生成时间 __NOW__ · 本页只读，不改库也不碰原有数据</p>

<div class="kpis">
<div class="kpi"><div class="v">__SIZE__ MB</div><div class="k">库文件大小</div></div>
<div class="kpi"><div class="v">__NDRAW__</div><div class="k">开奖源记录（多源并存）</div></div>
<div class="kpi"><div class="v">__NPZ__</div><div class="k">奖级明细行</div></div>
<div class="kpi"><div class="v">__NRG__</div><div class="k">分省中奖行</div></div>
<div class="kpi"><div class="v">__NSALES__</div><div class="k">分省月销量行</div></div>
</div>

<div class="card">
<h2><b>源覆盖矩阵</b><span class="tag">同一期可以有多个源，互不覆盖</span></h2>
<table><thead><tr><th>彩种</th><th>源</th><th>期数</th>
<th>奖级</th><th>分省中奖</th><th>出球顺序</th><th>日期范围</th></tr></thead>
<tbody>__COV__</tbody></table>
</div>

<div class="cols">
<div class="card">
<h2><b>双色球分省头奖</b><span class="tag">2013 至今累计</span></h2>
__BOARD_SSQ__
</div>
<div class="card">
<h2><b>七乐彩分省头奖</b><span class="tag">2013 至今累计</span></h2>
__BOARD_QLC__
</div>
</div>

<div class="card">
<h2><b>双色球最近 10 期头奖落点</b><span class="tag">原文来自福彩官方 content 字段</span></h2>
<table><thead><tr><th>期号</th><th>开奖日</th><th>分省分布</th></tr></thead>
<tbody>__LATEST__</tbody></table>
</div>

<div class="card">
<h2><b>出球顺序</b><span class="tag">现有 CSV 完全没有这个字段</span></h2>
<table><thead><tr><th>期号</th><th>开奖日</th><th>排序后号码</th>
<th>实际出球顺序</th></tr></thead>
<tbody>__SEQ__</tbody></table>
</div>

<div class="card">
<h2><b>奖级明细样例</b><span class="tag">含快乐8 每期 39 档</span></h2>
<table><thead><tr><th>彩种</th><th>期号</th><th>源</th><th>档位</th>
<th>名称</th><th>注数</th><th>金额</th><th>金额口径</th></tr></thead>
<tbody>__PZ__</tbody></table>
</div>

<div class="card">
<h2><b>分省月销量</b><span class="tag">财政部《全国彩票销售情况》附件 · __YMRANGE__</span></h2>
<div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:0 0 10px">
  <label style="font-size:12.5px;color:#666">统计月
    <select id="mof-ym" style="padding:4px 8px;border:1px solid #ddd;border-radius:6px;margin-left:6px">__YMOPTS__</select>
  </label>
  <span style="font-size:12px;color:#888">单位：万元。近 24 个月可切换；2020-02 财政部只发了全国数（疫情停售），无分省表。</span>
</div>
<div class="scroll"><table><thead><tr><th>月份</th><th>地区</th><th>机构</th>
<th>本月</th><th>同比</th><th>本年累计</th></tr></thead><tbody id="mof-tb">__SALES__</tbody></table></div>
<details style="margin-top:14px">
  <summary style="cursor:pointer;font-size:13px;color:#2563eb">全国月度汇总（__YMCOUNT__ 个月，单位亿元）</summary>
  <div class="scroll" style="max-height:420px;margin-top:8px">
    <table><thead><tr><th>月份</th><th>福彩</th><th>体彩</th><th>合计</th><th>省份数</th></tr></thead>
    <tbody>__MONTHLY__</tbody></table>
  </div>
</details>
</div>

<div class="card">
<h2><b>多源对账</b><span class="tag">官方源 vs 现有 CSV，逐期核号码与金额</span></h2>
<table><thead><tr><th>彩种</th><th>可比期数</th><th>完全一致</th>
<th>真冲突</th><th>一边缺值/为0</th></tr></thead><tbody>__REC__</tbody></table>
<div class="note" style="margin:14px 0 0">「真冲突」是两源数据打架；
「一边缺值/为0」是某个源早期没数据（如体彩 2007 年奖池全 0），不算矛盾。</div>
</div>

<div class="card">
<h2><b>自己怎么查</b></h2>
<pre><span class="c"># 看库状态（最常用）</span>
python -m market.build --status

<span class="c"># 增量更新（只增不删，不影响原有 CSV）</span>
python -m market.build --cwl         <span class="c"># 福彩系，含分省中奖</span>
python -m market.build --sporttery   <span class="c"># 体彩系，含出球顺序</span>

<span class="c"># 在 Python 里查</span>
from market import queries as Q
Q.region_prize_board(c, "ssq")      <span class="c"># 分省头奖排行</span>
Q.latest_regions(c, "ssq", 10)      <span class="c"># 最近 10 期出在哪些省</span>
Q.seq_first_ball(c, "ssq", "zhcw")  <span class="c"># 出球顺序首球分布</span>

<span class="c"># 用命令行看任意表</span>
python -c "import sqlite3;c=sqlite3.connect('market/market.db');print(c.execute('SELECT * FROM draw LIMIT 5').fetchall())"

<span class="c"># 想用图形界面：装 DB Browser for SQLite，直接打开 market/market.db</span>
<span class="c"># https://sqlitebrowser.org/</span></pre>
</div>

<script>
// 分省月销量：只按月份显隐行，不做重新请求（报告是纯静态页）
(function () {
  var sel = document.getElementById('mof-ym');
  if (!sel) return;
  var rows = document.querySelectorAll('#mof-tb tr');
  function apply() {
    var ym = sel.value;
    for (var i = 0; i < rows.length; i++) {
      rows[i].style.display = (rows[i].getAttribute('data-ym') === ym) ? '' : 'none';
    }
  }
  sel.addEventListener('change', apply);
  apply();
})();
</script>
</body></html>"""

    doc = (doc.replace("__NOW__", now)
              .replace("__SIZE__", "%.1f" % size_mb)
              .replace("__NDRAW__", "{:,}".format(counts.get("draw", 0)))
              .replace("__NPZ__", "{:,}".format(counts.get("prize", 0)))
              .replace("__NRG__", "{:,}".format(counts.get("prize_region", 0)))
              .replace("__NSALES__", "{:,}".format(counts.get("region_sales", 0)))
              .replace("__COV__", "".join(cov_rows))
              .replace("__BOARD_SSQ__", board_html("ssq", 15))
              .replace("__BOARD_QLC__", board_html("qlc", 12))
              .replace("__LATEST__", latest_rows)
              .replace("__SEQ__", seq_rows)
              .replace("__PZ__", pz_rows)
              .replace("__SALES__", sales_rows)
              .replace("__REC__", rec_rows)
              .replace("__YMRANGE__", esc(ym_span))
              .replace("__YMOPTS__", ym_opts)
              .replace("__YMCOUNT__", "{:,}".format(len(all_months)))
              .replace("__MONTHLY__", monthly_rows))

    with open(OUT, "w", encoding="utf-8") as f:
        f.write(doc)
    print("written:", OUT)
    print("size: %.1f KB" % (os.path.getsize(OUT) / 1024))


if __name__ == "__main__":
    main()
