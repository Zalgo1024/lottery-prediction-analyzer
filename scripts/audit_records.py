# -*- coding: utf-8 -*-
"""
号码记录审计（只读）：把 feedback 历史 + pending 里的「号码」全部独立重算一遍。

权威基准 = lottery_data/ 真实开奖 CSV（不是记录里自带的一份）。
三件事：
  1) 记录自带的「实际号码」是否与真实开奖一致（数据污染检测）
  2) 记录里的「命中数 / 奖级」独立重算后是否一致（判级 bug 检测）
  3) 预测号码本身是否合法（个数、范围、去重、期号/日期一致性）

用法: E:/Python/python.exe scripts/audit_records.py
产物: logs/号码记录审计_<日期>.md + .json
"""
import json
import sys
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LOTTERY_CONFIG
from data.loader import load_lottery
from data.schema import schema_from_cfg, normalize_zone_names, zone_name_aliases
from data.feedback import (
    _classify_prize, _classify_dlt_prize, _qxc_prize, _group_winning,
    _parse_iso_date,
)

BASE = Path(__file__).resolve().parent.parent
FB = BASE / "training" / "feedback"
LOTTERIES = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]
DIGITAL = {"排列5", "福彩3D", "排列3", "七星彩"}

# 数字型早期记录使用「位名」分区（万/千/百/十/个），现 schema 为「第N位」。
# 归一化后再比对，否则会把"换了名字"误报成"号码/命中错误"。
# 别名表统一由 data.schema 提供，避免多处映射漂移。
LEGACY_STAT = Counter()


def canon(name, zones):
    """把旧分区名归一化为现行 schema 名。"""
    schema = schema_from_cfg(LOTTERY_CONFIG[name], name)
    out = normalize_zone_names(zones or {}, schema)
    alias = zone_name_aliases(schema)
    for k in (zones or {}):
        nk = alias.get(k)
        if nk and nk != k:
            LEGACY_STAT[f"{name}:{k}→{nk}"] += 1
    return out


def draw_index(name):
    """期号 -> 开奖记录（真实 CSV）。"""
    data = load_lottery(name)
    return {r.期号: r for r in data.records if r.期号}, data.records


def norm_zones(rec, is_redblue):
    """从记录取「预测号码」统一成 {区名:[数字]}；红蓝型走扁平字段。"""
    if is_redblue:
        return {
            "红球": rec.get("预测红球") or [],
            "蓝球": rec.get("预测蓝球") or [],
        }
    return rec.get("预测号码") or {}


def recompute_hits(pred_zones, actual_zones, schema):
    """独立重算逐区命中：有序按位精确，无序按集合交集。"""
    hits = {}
    for z in schema.zones:
        pn = pred_zones.get(z.name, []) or []
        an = actual_zones.get(z.name, []) or []
        if z.ordered:
            hits[z.name] = sum(1 for i in range(min(len(pn), len(an))) if pn[i] == an[i])
        else:
            hits[z.name] = len(set(pn) & set(an))
    return hits


def check_numbers(name, zones, schema):
    """号码合法性：个数 / 范围 / 无序区重复。返回问题列表。"""
    bad = []
    for z in schema.zones:
        vals = zones.get(z.name, []) or []
        if len(vals) != z.choose:
            bad.append(f"{z.name} 个数={len(vals)}≠{z.choose}")
            continue
        for v in vals:
            if not isinstance(v, int) or not (z.min <= v <= z.max):
                bad.append(f"{z.name} 越界={v!r}(应在{z.min}-{z.max})")
        if not z.repeatable and len(set(vals)) != len(vals):
            bad.append(f"{z.name} 有重复={vals}")
    # 分区名是否与 schema 完全对齐
    extra = set(zones) - {z.name for z in schema.zones}
    if extra:
        bad.append(f"未知分区={sorted(extra)}")
    return bad


def expected_grade(name, hits, schema):
    """红蓝型按官方规则独立判级。"""
    if name in ("双色球", "大乐透"):
        rh, bh = hits.get("红球", 0), hits.get("蓝球", 0)
        return _classify_dlt_prize(rh, bh) if name == "大乐透" else _classify_prize(rh, bh)
    return None


def group_expected(name, zones, actual_zones, issue):
    """数字型独立判级（返回 玩法名 或 None）。"""
    def _ord(k):
        d = "".join(ch for ch in str(k) if ch.isdigit())
        return int(d) if d else 999
    order = sorted(zones.keys(), key=_ord)
    oa = sorted(actual_zones.keys(), key=_ord)
    pd = [int(zones[k][0]) for k in order if zones.get(k)]
    ad = [int(actual_zones[k][0]) for k in oa if actual_zones.get(k)]
    if name in ("福彩3D", "排列3", "排列5"):
        lvl, _pay, _mode = _group_winning(name, pd, ad, issue)
        return lvl
    if name == "七星彩":
        # 复用官方判级：构造分区命中
        zh = {}
        for k in order:
            zh[k] = 1 if (zones.get(k) and actual_zones.get(k) and zones[k][0] == actual_zones[k][0]) else 0
        lvl, _pay = _qxc_prize({"分区命中": zh})
        return lvl
    return None


def audit_history(name, draws, records):
    schema = schema_from_cfg(LOTTERY_CONFIG[name], name)
    is_rb = name in ("双色球", "大乐透")
    real = draws
    issues = []
    stat = Counter()
    for i, r in enumerate(records):
        issue = r.get("期号")
        tag = f"历史#{i} 期{issue}"
        # 1) 记录自带实际号码 vs 真实开奖
        draw = real.get(issue)
        if draw is None:
            issues.append((tag, "期号不存在于开奖数据", ""))
            stat["期号对不上"] += 1
            continue
        act_zones = canon(name, getattr(draw, "zone_numbers", {}) or {})
        rec_act = canon(name, ({"红球": r.get("实际红球") or [], "蓝球": r.get("实际蓝球") or []}
                               if is_rb else (r.get("实际号码") or {})))
        if rec_act != act_zones:
            issues.append((tag, "记录实际号码 ≠ CSV 开奖", f"记录={rec_act} CSV={act_zones}"))
            stat["实际号码不符"] += 1
        # 2) 命中重算
        pred_zones = canon(name, norm_zones(r, is_rb))
        bad = check_numbers(name, pred_zones, schema)
        for b in bad:
            issues.append((tag, "预测号码非法", b))
            stat["预测号码非法"] += 1
        hits = recompute_hits(pred_zones, act_zones, schema)
        if is_rb:
            exp_rh, exp_bh = hits.get("红球", 0), hits.get("蓝球", 0)
            if (r.get("红球命中"), r.get("蓝球命中")) != (exp_rh, exp_bh):
                issues.append((tag, "命中数不符",
                               f"记录=({r.get('红球命中')},{r.get('蓝球命中')}) 重算=({exp_rh},{exp_bh})"))
                stat["命中数不符"] += 1
            if r.get("总命中") not in (None, exp_rh + exp_bh):
                issues.append((tag, "总命中不符", f"记录={r.get('总命中')} 重算={exp_rh+exp_bh}"))
                stat["总命中不符"] += 1
        else:
            exp_zh = hits
            stored_zh = canon(name, r.get("分区命中") or {})
            if stored_zh != exp_zh:
                issues.append((tag, "分区命中不符", f"记录={r.get('分区命中')} 重算={exp_zh}"))
                stat["分区命中不符"] += 1
        # 3) 奖级重算
        if is_rb:
            # 非法票（红/蓝个数不符 schema）根本不判奖级 → 期望「未中」，
            # 与 data/feedback.py 生产口径一致（否则会误报"奖级不符"）。
            illegal = any("个数=" in b for b in bad)
            exp_grade = "未中" if illegal else expected_grade(name, hits, schema)
        else:
            exp_grade = group_expected(name, pred_zones, act_zones, issue) or "未中"
        if (r.get("中奖等级") or "未中") != exp_grade:
            issues.append((tag, "奖级不符", f"记录={r.get('中奖等级')} 重算={exp_grade} 命中={hits}"))
            stat["奖级不符"] += 1
        # 4) valid_prediction 标记
        exp_valid = True
        pd = _parse_iso_date(r.get("预测日期"))
        ad = getattr(draw, "开奖日期", None)
        if pd and ad and pd > ad:
            exp_valid = False
        if r.get("来源") in ("train", "训练"):
            exp_valid = False
        # 生产口径：红蓝型若红/蓝球个数不符 schema，直接判非真预测
        if is_rb and any("个数=" in b for b in bad):
            exp_valid = False
        if bool(r.get("valid_prediction", True)) != exp_valid:
            issues.append((tag, "valid标记不符",
                           f"记录={r.get('valid_prediction')} 应为={exp_valid} 预测日={r.get('预测日期')} 开奖日={ad}"))
            stat["valid标记不符"] += 1
    return issues, stat


def audit_pending(name, draws, pendings):
    schema = schema_from_cfg(LOTTERY_CONFIG[name], name)
    issues = []
    stat = Counter()
    latest_issue = max(draws) if draws else None
    for i, p in enumerate(pendings):
        tag = f"pending#{i} 目标期{p.get('目标期号')}"
        if p.get("状态") != "pending":
            continue
        tickets = p.get("预测号码", [])
        n_decl = p.get("号码组数")
        if n_decl is not None and n_decl != len(tickets):
            issues.append((tag, "号码组数不符", f"声明={n_decl} 实际={len(tickets)}"))
            stat["号码组数不符"] += 1
        seen = set()
        for j, t in enumerate(tickets):
            zones = t.get("号码") or ({"红球": t.get("红球"), "蓝球": t.get("蓝球")} if name in ("双色球", "大乐透") else {})
            zones = canon(name, zones)
            bad = check_numbers(name, zones, schema)
            for b in bad:
                issues.append((tag, f"第{j+1}注号码非法", b))
                stat["pending号码非法"] += 1
            # 票间去重（乐透型无序票）
            if name in ("双色球", "大乐透") and not bad:
                key = tuple(sorted(zones.get("红球", []))) + tuple(sorted(zones.get("蓝球", [])))
                if key in seen:
                    issues.append((tag, f"第{j+1}注与前面重复", str(key)))
                    stat["pending重复票"] += 1
                seen.add(key)
        ti = p.get("目标期号")
        if ti is not None and latest_issue is not None and ti <= latest_issue:
            issues.append((tag, "目标期号已开奖(马后炮)", f"本地最新期={latest_issue}"))
            stat["pending目标已开奖"] += 1
    return issues, stat


def main():
    report = []
    all_stat = {}
    total_issues = 0
    for name in LOTTERIES:
        draws, _recs = draw_index(name)
        hf = FB / f"{name}_feedback_history.json"
        pf = FB / f"{name}_pending.json"
        hist = json.load(open(hf, encoding="utf-8")) if hf.exists() else []
        pend = json.load(open(pf, encoding="utf-8")) if pf.exists() else []
        hi, hs = audit_history(name, draws, hist)
        pi, ps = audit_pending(name, draws, pend)
        issues = hi + pi
        total_issues += len(issues)
        all_stat[name] = {"历史": dict(hs), "pending": dict(ps), "问题数": len(issues)}
        report.append({
            "彩种": name, "历史条数": len(hist), "pending条数": len(pend),
            "历史问题": [{"定位": a, "项": b, "详情": c} for a, b, c in hi[:50]],
            "pending问题": [{"定位": a, "项": b, "详情": c} for a, b, c in pi[:50]],
            "问题数": len(issues),
        })
        print(f"[{name}] 历史{len(hist)} pending{len(pend)} → 问题 {len(issues)} 条")
        cnt = Counter(b for _a, b, _c in issues)
        for b, c in cnt.most_common():
            print(f"    {b}: {c}")

    if LEGACY_STAT:
        print("\n[旧分区名归一化统计]（非错误，仅格式历史遗留）")
        for k, v in LEGACY_STAT.most_common():
            print(f"    {k}: {v}")

    out_md = BASE / "logs" / f"号码记录审计_{date.today():%Y%m%d}.md"
    out_json = BASE / "logs" / f"号码记录审计_{date.today():%Y%m%d}.json"
    out_json.write_text(json.dumps({"统计": all_stat, "明细": report}, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    lines = [f"# 号码记录审计 {date.today()}", "", f"问题合计：**{total_issues}** 条", ""]
    for r in report:
        lines.append(f"## {r['彩种']}（历史 {r['历史条数']} / pending {r['pending条数']}）— {r['问题数']} 条")
        for p in r["历史问题"][:20]:
            lines.append(f"- [历史] {p['项']} @ {p['定位']} :: {p['详情']}")
        for p in r["pending问题"][:20]:
            lines.append(f"- [pending] {p['项']} @ {p['定位']} :: {p['详情']}")
        if not r["历史问题"] and not r["pending问题"]:
            lines.append("- ✅ 无问题")
        lines.append("")
    out_md.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n问题合计 {total_issues} 条 → {out_md.name} / {out_json.name}")
    return total_issues


if __name__ == "__main__":
    main()
