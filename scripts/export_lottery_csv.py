# -*- coding: utf-8 -*-
"""scripts/export_lottery_csv.py —— 把 market.db 里已有的开奖数据导出成主链路认的 CSV。

背景
----
七乐彩（3005 期，2007-01-03 起）与快乐8（2111 期，2020-10-28 起）的开奖数据
**早就在 `market/market.db` 里**，但主链路只认 `lottery_data/*.csv`
（`data/loader._file_path` 按「<彩种>历史数据.csv」约定推导）。
`market/update.py` 的口径是「只写 market.db，不碰 lottery_data、不碰预测」，
所以两者之间从来没有桥。本脚本就是那座桥。

设计约束
--------
1. **只读 `v_draw` 视图，不读 `draw` 表**：七乐彩的 `draw` 有 cwl(2084) + zhcw(3005)
   两个源共 5089 行，`v_draw` 才按期去重（cwl 优先、缺则 zhcw）到 3005 期。
   直接读 `draw` 会把同期开奖导两遍。
2. **不导出 `seq_front`（出球顺序）**：七乐彩的 seq_front 是整数升序**假象**
   （3005/3005 全序），不是真实摇出次序。`ev/market_backtest._is_draw_order()`
   已据此把它排除出所有出球顺序检验；把它写进 CSV 只会邀请别人误用。
3. **不导出财务列**：`ev/payout._record_settled()` 用「总投注额 > 0」判定该期是否已结算，
   给泛型分支补上财务列会改变既有数字型的"已结算"判定 —— 与本次接入无关的行为变更，不做。
4. **幂等 + 原子**：同样数据连跑两次输出字节一致；写 tmp 后 `os.replace`。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "market" / "market.db"
OUT_DIR = ROOT / "lottery_data"

# 彩种 → (库内代码, 主区个数, 副区个数, 输出文件名)
SPEC: Dict[str, Dict[str, Any]] = {
    "七乐彩": {"code": "qlc", "front": 7, "back": 1, "file": "七乐彩历史数据.csv"},
    "快乐8": {"code": "kl8", "front": 20, "back": 0, "file": "快乐8历史数据.csv"},
}


def _load_rows(code: str, db: Path) -> List[tuple]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT issue, draw_date, front, back FROM v_draw "
            "WHERE lottery=? AND front IS NOT NULL AND front<>'' "
            "ORDER BY issue", (code,)).fetchall()
    finally:
        con.close()


def _parse_nums(raw: Any) -> List[int]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, (list, tuple)):
        vals = raw
    else:
        try:
            vals = json.loads(raw)
        except (TypeError, ValueError):
            return []
    out = []
    for v in vals or []:
        try:
            out.append(int(v))
        except (TypeError, ValueError):
            continue
    return out


def build_rows(name: str, db: Path) -> List[Dict[str, Any]]:
    spec = SPEC[name]
    rows = _load_rows(spec["code"], db)
    out: List[Dict[str, Any]] = []
    for issue, date, front, back in rows:
        fr = sorted(_parse_nums(front))
        bk = _parse_nums(back)
        if len(fr) != spec["front"]:
            # 不静默丢弃：号码数不对的期会让命中数静默偏低，
            # 而"命中偏低"与"这期真的没中"在下游完全同形。
            raise ValueError(
                f"{name} {issue} 主区应有 {spec['front']} 个号，实得 {len(fr)} 个")
        if spec["back"]:
            if len(bk) != spec["back"]:
                raise ValueError(
                    f"{name} {issue} 副区应有 {spec['back']} 个号，实得 {len(bk)} 个")
            if set(fr) & set(bk):
                raise ValueError(f"{name} {issue} 副区号码与主区重复：{sorted(set(fr) & set(bk))}")
        rec: Dict[str, Any] = {"期号": str(issue), "开奖日期": str(date or "")}
        for i, v in enumerate(fr, 1):
            rec[f"号码{i}"] = v
        for j, v in enumerate(bk, 1):
            rec[f"号码{spec['front'] + j}"] = v
        out.append(rec)
    # 与既有 6 个 CSV 一致：按**期号升序**落盘（loader 读完会自行降序排）
    out.sort(key=lambda r: r["期号"])
    return out


def write_csv(name: str, rows: List[Dict[str, Any]], out_dir: Path) -> Path:
    spec = SPEC[name]
    path = out_dir / spec["file"]
    cols = ["期号", "开奖日期"] + [f"号码{i}" for i in range(1, spec["front"] + spec["back"] + 1)]
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    os.replace(tmp, path)
    return path


def _db_fingerprint(db: Path) -> str:
    st = db.stat()
    return hashlib.sha1(f"{st.st_size}:{int(st.st_mtime)}".encode()).hexdigest()[:16]


def export(name: str, *, db: Path = DB, out_dir: Path = OUT_DIR,
           write_meta: bool = True) -> Dict[str, Any]:
    """导出单个彩种，返回元信息。"""
    if name not in SPEC:
        raise KeyError(f"未知彩种：{name}（支持：{'/'.join(SPEC)}）")
    if not db.exists():
        raise FileNotFoundError(f"找不到 {db}（旁路市场库不存在，无法导出）")
    rows = build_rows(name, db)
    path = write_csv(name, rows, out_dir)
    meta = {
        "彩种": name,
        "文件": path.name,
        "期数": len(rows),
        "期号范围": [rows[0]["期号"], rows[-1]["期号"]] if rows else [],
        "日期范围": [rows[0]["开奖日期"], rows[-1]["开奖日期"]] if rows else [],
        "源": f"market/market.db#v_draw（{_db_fingerprint(db)}）",
        "含出球顺序": False,   # 七乐彩 seq_front 是排序假象，见模块 docstring
    }
    if write_meta:
        mp = out_dir / f"_{name}_export_meta.json"
        mp.write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="从 market.db 导出七乐彩/快乐8 历史 CSV")
    ap.add_argument("--lottery", default=None, help="彩种名（省略则导出全部）")
    ap.add_argument("--db", default=str(DB))
    ap.add_argument("--out", default=str(OUT_DIR))
    ap.add_argument("--dry-run", action="store_true", help="只报行数，不落盘")
    args = ap.parse_args(argv)

    names = [args.lottery] if args.lottery else list(SPEC)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    rc = 0
    for n in names:
        try:
            if args.dry_run:
                rows = build_rows(n, Path(args.db))
                print(f"{n}: {len(rows)} 期（dry-run，未落盘）")
                continue
            meta = export(n, db=Path(args.db), out_dir=out_dir)
            print(f"{n}: {meta['期数']} 期  {meta['期号范围'][0]}~{meta['期号范围'][-1]}"
                  f"  -> {meta['文件']}")
        except Exception as e:                       # noqa: BLE001
            print(f"{n}: 失败 —— {e}", file=sys.stderr)
            rc = 1
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
