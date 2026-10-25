# -*- coding: utf-8 -*-
"""预测数据完整性 + 前视偏差（look-ahead）审计。

三块独立检查，任一不过即非零退出：

A. **文件完整性**
   反馈/待开奖 JSON 必须可解析且不含 NUL 空洞。
   （2026-10-24 实测事故：`排列3_feedback_history.json` 前 368,451 字节被
    `[]` + NUL 覆盖，根因是 `_safe_write_json` 非原子写；现已改原子写，
   本检查作为常备哨兵。）

B. **前视判据 J1/J2/J3**
   J1 出号时刻 `批次时间` <= 开奖日 21:00（与 `data.feedback._is_valid_prediction` 同口径）
   J2 结算时刻 `评估时间` >= 出号时刻
   J3 同一 `批次号` 内所有记录的出号时刻一致（否则是拼接/重算）

C. **命中率指纹**
   若出号时读过开奖号，命中率必然显著高于理论。反向验证：
   实测中奖率与 i.i.d. 理论值的 z 必须落在 [-4, +4]。

用法：
    python scripts/audit_data_integrity.py                # 近 9 天
    python scripts/audit_data_integrity.py --days 30
    python scripts/audit_data_integrity.py --json out.json
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from config import LOTTERY_CONFIG                         # noqa: E402
from data.feedback import DRAW_CUTOFF, FEEDBACK_DIR       # noqa: E402

CUT = dtime.fromisoformat(DRAW_CUTOFF)
Z_LIMIT = 4.0


# ---------------------------------------------------------------- 工具
def _dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s))
    except (ValueError, TypeError):
        return None


def _d(s):
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _load(path: Path):
    """返回 (数据, 错误说明)。数据为 list。"""
    if not path.exists():
        return None, "文件不存在"
    raw = path.read_bytes()
    if b"\x00" in raw:
        return None, f"NUL 空洞（{raw.count(0)} 字节）"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        return None, f"JSON 解析失败: {type(e).__name__}"
    if isinstance(data, list):
        return data, ""
    if isinstance(data, dict):
        return [data], ""
    return [], "未知结构"


# ---------------------------------------------------------------- A
def check_files() -> dict:
    rows, bad = [], []
    for f in sorted(Path(FEEDBACK_DIR).glob("*_pending.json")) + \
            sorted(Path(FEEDBACK_DIR).glob("*_feedback_history.json")):
        data, err = _load(f)
        ok = not err
        rows.append({"文件": f.name, "字节": f.stat().st_size,
                     "记录数": len(data) if data is not None else None,
                     "状态": "OK" if ok else err})
        if not ok:
            bad.append(f.name)
    return {"通过": not bad, "明细": rows, "损坏文件": bad}


# ---------------------------------------------------------------- B
def check_lookahead(days: int) -> dict:
    since = (date.today() - timedelta(days=days)).isoformat()
    out, total = {}, Counter()
    for lot in LOTTERY_CONFIG:
        rows, _err = _load(Path(FEEDBACK_DIR) / f"{lot}_feedback_history.json")
        if rows is None:
            continue
        sel = [r for r in rows if str(r.get("开奖日期") or "") >= since]
        bad1, bad2 = [], []
        batch_times = defaultdict(set)
        n_by_batch, scale = Counter(), {}
        hours = Counter()
        for r in sel:
            gen, ev = _dt(r.get("批次时间")), _dt(r.get("评估时间"))
            dd = _d(r.get("开奖日期"))
            bno = r.get("批次号") or ""
            if gen and bno:
                batch_times[bno].add(gen.isoformat())
            if bno:
                n_by_batch[bno] += 1
                if r.get("批次规模"):
                    scale[bno] = int(r["批次规模"])
            if gen:
                hours[gen.hour] += 1
            if gen and dd and gen > datetime.combine(dd, CUT):
                bad1.append(f"{r.get('期号')}@{gen.isoformat(timespec='seconds')}")
            if gen and ev and ev < gen:
                bad2.append(f"{r.get('期号')}")
        bad3 = [b for b, ss in batch_times.items() if len(ss) > 1]
        mismatched = [b for b in n_by_batch if b in scale and scale[b] != n_by_batch[b]]
        out[lot] = {"记录数": len(sel), "J1_出号晚于开奖": len(bad1),
                    "J2_结算早于出号": len(bad2),
                    "J3_批内出号时刻不一致": len(bad3),
                    "批次规模不符": len(mismatched),
                    "缺批次时间": sum(1 for r in sel if not _dt(r.get("批次时间"))),
                    "出号时刻分布": dict(sorted(hours.items())),
                    "J1样例": bad1[:5], "J2样例": bad2[:5], "J3样例": bad3[:5]}
        total["J1"] += len(bad1)
        total["J2"] += len(bad2)
        total["J3"] += len(bad3)
        total["n"] += len(sel)
    return {"通过": not (total["J1"] or total["J2"] or total["J3"]),
            "合计": dict(total), "明细": out}


# ---------------------------------------------------------------- C
def check_hitrate(days: int) -> dict:
    since = (date.today() - timedelta(days=days)).isoformat()
    out, flagged = {}, []
    for lot in LOTTERY_CONFIG:
        rows, _err = _load(Path(FEEDBACK_DIR) / f"{lot}_feedback_history.json")
        if rows is None:
            continue
        sel = [r for r in rows
               if str(r.get("开奖日期") or "") >= since
               and bool(r.get("valid_prediction", True))
               and r.get("记录类型", "预测") == "预测"]
        if not sel:
            continue
        k = sum(1 for r in sel
                if r.get("中奖等级") and str(r["中奖等级"]) not in ("未中", ""))
        n = len(sel)
        rate = k / n
        try:
            from ev.payout import grade_probabilities
            exp = sum(float(v) for v in (grade_probabilities(lot) or {}).values())
        except Exception:                                  # noqa: BLE001
            exp = None
        z = None
        if exp and 0 < exp < 1:
            se = math.sqrt(exp * (1 - exp) / n)
            if se > 0:
                z = (rate - exp) / se
        verdict = ("样本/理论不可用" if z is None else
                   "偏高（需人工复核）" if z > Z_LIMIT else
                   "偏低（正常波动范围）" if z < -Z_LIMIT else "与理论相容")
        item = {"样本": n, "中奖": k, "实测率": round(rate, 6),
                "理论率": None if exp is None else round(exp, 6),
                "z": None if z is None else round(z, 3), "判定": verdict}
        out[lot] = item
        if z is not None and abs(z) > Z_LIMIT:
            flagged.append(lot)
    return {"通过": not flagged, "超阈值彩种": flagged, "明细": out}


# ---------------------------------------------------------------- 渲染
def render(rep: dict, days: int) -> str:
    L = [f"# 预测数据完整性与前视偏差审计", "",
         f"> 生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}　|　"
         f"时间窗：最近 {days} 天", ""]

    a = rep["文件完整性"]
    L += ["## A. 文件完整性", "",
          "| 文件 | 字节 | 记录数 | 状态 |", "|---|---|---|---|"]
    for r in a["明细"]:
        L.append(f"| {r['文件']} | {r['字节']:,} | {r['记录数']} | {r['状态']} |")
    L += ["", f"**结论：{'通过' if a['通过'] else '存在损坏 —— ' + '、'.join(a['损坏文件'])}**", ""]

    b = rep["前视判据"]
    L += ["## B. 前视判据（出号时刻 vs 开奖时刻）", "",
          "| 彩种 | 记录数 | J1 出号晚于开奖 | J2 结算早于出号 | J3 批内时刻不一致 | 批次规模不符 |",
          "|---|---|---|---|---|---|"]
    for lot, r in b["明细"].items():
        L.append(f"| {lot} | {r['记录数']} | {r['J1_出号晚于开奖']} | "
                 f"{r['J2_结算早于出号']} | {r['J3_批内出号时刻不一致']} | "
                 f"{r['批次规模不符']} |")
    t = b["合计"]
    L += ["", f"合计 {t.get('n', 0)} 条：J1={t.get('J1', 0)}　J2={t.get('J2', 0)}　"
              f"J3={t.get('J3', 0)}", "",
          f"**结论：{'通过（无一例开奖后出号）' if b['通过'] else '存在违例'}**", ""]

    c = rep["命中率指纹"]
    L += ["## C. 命中率指纹（反向验证）", "",
          "若出号时读过开奖号，命中率必然显著高于理论期望。", "",
          "| 彩种 | 样本 | 中奖 | 实测率 | 理论率 | z | 判定 |", "|---|---|---|---|---|---|---|"]
    for lot, r in c["明细"].items():
        thr = "—" if r["理论率"] is None else "%.2f%%" % (r["理论率"] * 100)
        zz = "—" if r["z"] is None else "%+.2f" % r["z"]
        L.append(f"| {lot} | {r['样本']} | {r['中奖']} | {r['实测率']:.2%} | "
                 f"{thr} | {zz} | {r['判定']} |")
    L += ["", f"**结论：{'通过' if c['通过'] else '超阈值 —— ' + '、'.join(c['超阈值彩种'])}**", ""]

    ok = a["通过"] and b["通过"] and c["通过"]
    L += ["---", "", f"## 总判定：{'✅ 全部通过' if ok else '❌ 存在不通过项'}", ""]
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=9)
    ap.add_argument("--json", default="")
    ap.add_argument("--md", default="")
    args = ap.parse_args()

    rep = {"文件完整性": check_files(),
           "前视判据": check_lookahead(args.days),
           "命中率指纹": check_hitrate(args.days)}

    md = render(rep, args.days)
    out_md = Path(args.md) if args.md else BASE / "output" / "_audit_integrity.md"
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(md, encoding="utf-8")
    if args.json:
        Path(args.json).write_text(
            json.dumps(rep, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

    ok = rep["文件完整性"]["通过"] and rep["前视判据"]["通过"] and rep["命中率指纹"]["通过"]
    print(md)
    print(f"\n[写出] {out_md}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
