#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把用户误标名的 5 位排列5数据（D:/707/七星彩历史数据.csv）并入项目排列5文件。

源文件格式：期号,中奖号码1..5,总和,总销售额(元),开奖日期(日/月/年),,,,,,
目标格式：期号,开奖日期,号码1..5  （年-月-日，utf-8-sig）

合并规则：以 期号(5位补零) 为键去重；项目已有行优先，用户文件只补缺/补最新。
"""
import csv
import shutil
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = Path(r"D:/707/七星彩历史数据.csv")
DST = PROJECT_ROOT / "lottery_data" / "排列5历史数据.csv"
BAK = PROJECT_ROOT / "lottery_data" / "排列5历史数据.csv.bak"

DIGITS = 5


def norm_period(p: str) -> str:
    """期号统一成 5 位补零字符串，作为去重键。"""
    return str(int(p.strip())).zfill(5)


def parse_date(d: str) -> str:
    """日/月/年 -> 年-月-日；解析失败原样返回。"""
    d = (d or "").strip()
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(d, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return d


def read_project(path: Path) -> dict:
    data = {}
    with path.open(encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            pid = norm_period(row["期号"])
            data[pid] = [pid, row["开奖日期"].strip()] + [
                row[f"号码{i}"].strip() for i in range(1, DIGITS + 1)
            ]
    return data


def read_src(path: Path) -> dict:
    data = {}
    with path.open(encoding="gb18030", newline="") as f:
        for row in csv.reader(f):
            if not row or not row[0].strip():
                continue
            # 列：期号, 中奖号码1..5, 总和, 总销售额, 开奖日期, (若干空列)
            try:
                pid = norm_period(row[0])
                nums = [row[i].strip() for i in range(1, 1 + DIGITS)]
                draw_date = parse_date(row[1 + DIGITS + 2])  # 总和+销售额 之后是日期
            except (IndexError, ValueError):
                continue
            if any(n == "" for n in nums):
                continue
            data[pid] = [pid, draw_date] + nums
    return data


def main():
    assert SRC.exists(), f"源文件不存在: {SRC}"
    assert DST.exists(), f"项目排列5文件不存在: {DST}"

    # 备份项目原文件（copy，不涉及删除）
    shutil.copy(str(DST), str(BAK))
    print(f"[backup] 已备份原文件 -> {BAK.name}")

    proj = read_project(DST)
    src = read_src(SRC)
    print(f"[read] 项目原有期数: {len(proj)}，用户文件期数: {len(src)}")

    added = 0
    for pid, row in src.items():
        if pid not in proj:
            proj[pid] = row
            added += 1
    print(f"[merge] 新增补缺期数: {added}")

    ordered = [proj[k] for k in sorted(proj.keys())]
    with DST.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["期号", "开奖日期"] + [f"号码{i}" for i in range(1, DIGITS + 1)])
        w.writerows(ordered)
    print(f"[write] 合并后总行数: {len(ordered)} -> {DST.name}")
    print(f"[latest] 最新一期: {ordered[-1][0]}  {ordered[-1][1]}  {ordered[-1][2:]}")


if __name__ == "__main__":
    main()
