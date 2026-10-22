"""market.db 常用查询。

这些查询只用新数据（分省中奖、分省销量、出球顺序），
现有 CSV 与流水线完全用不到它们——它们回答的是以前答不了的问题。
"""

from __future__ import annotations

import json
import sqlite3


def region_prize_board(con: sqlite3.Connection, lottery: str, top: int = 34):
    """分省头奖排行：哪个省中出的一等奖注数最多。

    口径：只统计 prize_region（源原文「XX N 注」），不含二等奖。
    """
    return con.execute(
        "SELECT region, SUM(winners) 注数, COUNT(*) 有奖期数, "
        "       ROUND(AVG(winners),2) 平均每期 "
        "FROM prize_region WHERE lottery=? "
        "GROUP BY region ORDER BY 注数 DESC LIMIT ?", (lottery, top)).fetchall()


def region_vs_sales(con: sqlite3.Connection, lottery: str, ym: str,
                    agency: str = "福彩"):
    """★销量占比 vs 头奖注数占比。

    这是「特定地区被安排中奖」这个假设能拿到的最直接的检验：
    若某省的注数占比长期显著高于其销量占比，才有话说；否则两者应大致同阶。
    注意：只能做全省汇总的粗检验，因为库里没有「哪一注出自哪个站」的数据。
    """
    return con.execute(
        """
        WITH s AS (
          SELECT region, sales_wan FROM region_sales
          WHERE ym=? AND agency=?
        ), p AS (
          SELECT region, SUM(winners) AS w FROM prize_region
          WHERE lottery=? GROUP BY region
        )
        SELECT s.region, s.sales_wan,
               ROUND(100.0*s.sales_wan/(SELECT SUM(sales_wan) FROM s),2) AS 销量占比,
               COALESCE(p.w,0) AS 头奖注数,
               ROUND(100.0*COALESCE(p.w,0)/(SELECT SUM(COALESCE(w,0)) FROM p),2) AS 注数占比
        FROM s LEFT JOIN p ON p.region = s.region
        ORDER BY 头奖注数 DESC
        """, (ym, agency, lottery)).fetchall()


def seq_first_ball(con: sqlite3.Connection, lottery: str, source: str | None = None):
    """★出球顺序：第一个被摇出的号码分布。

    若摇奖机/球存在物理偏差，最先出球的号码会偏离均匀——这是排序后号码里看不到的。
    返回 [(首球, 出现次数)]，理论期望 = 总期数 / 号码池大小。
    """
    sql = ("SELECT seq_front, issue FROM draw WHERE lottery=? AND seq_front IS NOT NULL")
    args: list = [lottery]
    if source:
        sql += " AND source=?"
        args.append(source)
    counts: dict[int, int] = {}
    total = 0
    for seq_json, _issue in con.execute(sql, args):
        try:
            seq = json.loads(seq_json)
        except (TypeError, ValueError):
            continue
        if not seq:
            continue
        total += 1
        counts[seq[0]] = counts.get(seq[0], 0) + 1
    return sorted(counts.items()), total


def seq_position_entropy(con: sqlite3.Connection, lottery: str):
    """各出球位置上的号码均值，用于粗看是否存在位置性偏差。"""
    sql = ("SELECT seq_front FROM draw WHERE lottery=? AND seq_front IS NOT NULL")
    sums: dict[int, list[int]] = {}
    for (seq_json,) in con.execute(sql, (lottery,)):
        try:
            seq = json.loads(seq_json)
        except (TypeError, ValueError):
            continue
        for pos, v in enumerate(seq):
            sums.setdefault(pos, []).append(v)
    out = []
    for pos in sorted(sums):
        vals = sums[pos]
        out.append((pos + 1, len(vals), round(sum(vals) / len(vals), 3),
                    min(vals), max(vals)))
    return out


def coverage(con: sqlite3.Connection):
    """各彩种各源的数据覆盖，一眼看出哪块还缺。"""
    return con.execute(
        """
        SELECT d.lottery, l.name, d.source, COUNT(*) 期数,
               MIN(d.draw_date) 起, MAX(d.draw_date) 止,
               SUM(CASE WHEN d.seq_front IS NOT NULL THEN 1 ELSE 0 END) 有出球序,
               SUM(CASE WHEN d.first_region_raw IS NOT NULL
                        AND d.first_region_raw <> '共0注。' THEN 1 ELSE 0 END) 有地区原文
        FROM draw d JOIN lottery l ON l.code = d.lottery
        GROUP BY d.lottery, d.source
        ORDER BY d.lottery, d.source
        """).fetchall()


def latest_regions(con: sqlite3.Connection, lottery: str, n: int = 10):
    """最近 n 期头奖都出在哪些省——最直观的「哪些地方有中奖」。"""
    return con.execute(
        """
        SELECT d.issue, d.draw_date, d.first_region_raw
        FROM draw d
        WHERE d.lottery=? AND d.first_region_raw IS NOT NULL
          AND d.first_region_raw <> '共0注。'
        ORDER BY d.draw_date DESC LIMIT ?
        """, (lottery, n)).fetchall()


def region_year_trend(con: sqlite3.Connection, lottery: str, region: str):
    """某省按年的一等奖注数，看是否某年突然集中。"""
    return con.execute(
        """
        SELECT substr(d.draw_date,1,4) 年, SUM(r.winners) 注数, COUNT(*) 期数
        FROM prize_region r JOIN draw d
          ON d.lottery=r.lottery AND d.issue=r.issue AND d.source=r.source
        WHERE r.lottery=? AND r.region=?
        GROUP BY 年 ORDER BY 年
        """, (lottery, region)).fetchall()
