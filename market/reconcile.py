"""多源对账：官方源 vs 现有 CSV。

目的不是"以哪个为准"，而是**把差异暴露出来**。现有 CSV 只有一份，冲突时
只能覆盖、覆盖完就查不到丢了什么；这个库同时留着两边，差异进 reconcile 表。

★对齐键用 (issue_year, issue_seq) 而不是期号字符串：
  官方双色球是 2026120，CSV 是 26120，直接 JOIN 永远对不上。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

FIELDS = ("front", "back", "sales", "pool")


def _norm(v):
    """比较前归一：JSON 号码数组解析成 list，金额变 int，None 统一成 None。"""
    if v is None:
        return None
    if isinstance(v, str) and v.startswith("["):
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def reconcile_lottery(con: sqlite3.Connection, lottery: str,
                      src_a: str = "cwl", src_b: str = "csv500") -> dict:
    """比对两个源，返回 {checked, agree, diff, missing}。"""
    # 官方源在不同彩种下名字不同，调用方传准
    q = """
    SELECT a.issue, a.issue_year, a.issue_seq, a.front, a.back, a.sales, a.pool,
           b.issue, b.front, b.back, b.sales, b.pool
    FROM draw a JOIN draw b
      ON a.lottery=b.lottery AND a.issue_year=b.issue_year
     AND a.issue_seq=b.issue_seq
    WHERE a.lottery=? AND a.source=? AND b.source=?
      AND a.issue_year IS NOT NULL
    """
    # 重跑前清掉该彩种旧的对账记录，否则每次跑都会累加
    con.execute("DELETE FROM reconcile WHERE lottery=?", (lottery,))
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    checked = agree = 0
    skipped = 0
    diffs = []
    for row in con.execute(q, (lottery, src_a, src_b)):
        (issue_a, _y, _s, fa, ba, sa, pa,
         issue_b, fb, bb, sb, pb) = row
        checked += 1
        row_ok = True
        for field, va, vb in (("front", fa, fb), ("back", ba, bb),
                              ("sales", sa, sb), ("pool", pa, pb)):
            na, nb = _norm(va), _norm(vb)
            # ★一边为空 = 不可比，不算差异。
            #   现有 CSV 里排列3/排列5/3D/七星彩根本没有销售额与奖池列，
            #   若把 None 当差异，这几万条"差异"会淹没真正的号码差异。
            if na is None or nb is None:
                skipped += 1
                continue
            if na == nb:
                continue
            # 金额允许 1 元以内的四舍五入差
            if field in ("sales", "pool") and isinstance(na, int) and isinstance(nb, int):
                if abs(na - nb) <= 1:
                    continue
            row_ok = False
            # agree=-1 表示「一边是 0」，通常是源端早期缺值（体彩 2007 年奖池全为 0、
            # 微博 CSV 未更新的末期），不是两源数据打架，不能和真冲突混在一起。
            zero_side = (na in (0, []) or nb in (0, []))
            diffs.append((lottery, issue_a, field, src_a, str(na)[:60],
                          src_b, str(nb)[:60], -1 if zero_side else 0, ts))
        if row_ok:
            agree += 1

    con.executemany(
        "INSERT INTO reconcile(lottery,issue,field,src_a,val_a,src_b,val_b,"
        "agree,checked_at) VALUES(?,?,?,?,?,?,?,?,?)", diffs)
    con.commit()
    # 「只在一边有」的期数：也是需要看见的差异
    only_a = con.execute(
        "SELECT COUNT(*) FROM draw a WHERE a.lottery=? AND a.source=? AND NOT EXISTS("
        " SELECT 1 FROM draw b WHERE b.lottery=a.lottery AND b.source=?"
        " AND b.issue_year=a.issue_year AND b.issue_seq=a.issue_seq)",
        (lottery, src_a, src_b)).fetchone()[0]
    only_b = con.execute(
        "SELECT COUNT(*) FROM draw b WHERE b.lottery=? AND b.source=? AND NOT EXISTS("
        " SELECT 1 FROM draw a WHERE a.lottery=b.lottery AND a.source=?"
        " AND a.issue_year=b.issue_year AND a.issue_seq=b.issue_seq)",
        (lottery, src_b, src_a)).fetchone()[0]
    conflict = sum(1 for d in diffs if d[7] == 0)
    zero_side = sum(1 for d in diffs if d[7] == -1)
    return dict(checked=checked, agree=agree, diff=len(diffs),
                conflict=conflict, zero_side=zero_side,
                skipped=skipped, only_a=only_a, only_b=only_b)


def report(con: sqlite3.Connection, pairs=None) -> None:
    from market.sources import LOTTERIES

    if pairs is None:
        pairs = []
        for code, meta in LOTTERIES.items():
            if meta["family"] == "cwl" and code in ("ssq", "qlc", "sd"):
                pairs.append((code, "cwl"))
            elif meta["family"] == "sporttery":
                pairs.append((code, "sporttery"))

    print("%-4s %-10s %8s %8s %8s %8s %8s" %
          ("彩种", "对比源", "可比期数", "完全一致", "真冲突", "缺值/为0", "仅官方"))
    total = dict(checked=0, agree=0, diff=0)
    for code, src in pairs:
        r = reconcile_lottery(con, code, src_a=src, src_b="csv500")
        total["checked"] += r["checked"]
        total["agree"] += r["agree"]
        total["diff"] += r["diff"]
        total["conflict"] = total.get("conflict", 0) + r["conflict"]
        print("%-4s %-10s %8d %8d %8d %8d %8d" %
              (code, src, r["checked"], r["agree"], r["conflict"],
               r["zero_side"], r["only_a"]))
    print("-" * 62)
    pct = (100.0 * total["agree"] / total["checked"]) if total["checked"] else 0.0
    print("合计可比 %d 期，完全一致 %d 期（%.2f%%）；真冲突 %d 条"
          % (total["checked"], total["agree"], pct, total.get("conflict", 0)))
    print("（一边为空视为不可比，不计差异；现有 CSV 的排列3/5/3D/七星彩没有金额列）")
