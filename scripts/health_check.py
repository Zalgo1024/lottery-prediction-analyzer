# -*- coding: utf-8 -*-
"""
项目数据体检总入口（只读）。

把散落在多个脚本里的检查汇总成一份报告，便于定期/改动后一键复核：
  1) 开奖号码完整性  —— 缺期 / 越界 / 空值 / 重复期号 / 销售对齐
  2) 记录审计        —— feedback 历史 + pending 的号码/命中/奖级/valid 独立重算
  3) pending 新鲜度  —— 各彩种目标期号是否 = 本地最新期 +1、组数是否达到出号规模
  4) 关键字段完整性  —— 双/大 奖池奖金；数字型 销售数据最新期是否跟上开奖
  5) 流水线新鲜度    —— logs/automation/status.json 快照是否落后于 CSV 最新期
  6) 写保护自检      —— pytest 生产写入守卫是否生效

用法: E:/Python/python.exe scripts/health_check.py
产物: logs/项目体检_<日期>.md + .json
本脚本**只读**，不修改任何数据文件。
"""
import json
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import (LOTTERY_CONFIG, PRUNE_MIN_PER_STRATEGY, resolve_groups)  # noqa: E402
from data.loader import load_lottery  # noqa: E402
from scripts.scan_data_integrity import scan_lottery  # noqa: E402
from scripts.audit_records import (  # noqa: E402
    draw_index, audit_history, audit_pending,
)

BASE = Path(__file__).resolve().parent.parent
FB = BASE / "training" / "feedback"
DIGITAL = {"排列5", "福彩3D", "排列3", "七星彩"}
# 源数据本身就没有、补不回来的缺口（勿再当 bug 排查）
KNOWNS = {("排列5", 4006), ("排列5", 4007)}


def _local_issue(name, raw):
    """把体检报告里的「年份+期号」还原成本地 CSV 的期号口径。

    数字型报告缺号形如 2004006（= 2004 年 + 006 期），而本地 CSV 期号是 4006。
    """
    s = "".join(ch for ch in str(raw) if ch.isdigit())
    if not s:
        return None
    v = int(s)
    if name == "排列5" and len(s) >= 7:      # 2004006 → 04006 → 4006
        v = int(s[2:])
    return v


def check_pending_freshness():
    out = []
    for name in LOTTERY_CONFIG:
        latest = max((r.期号 for r in load_lottery(name).records if r.期号), default=None)
        pf = FB / f"{name}_pending.json"
        pend = json.load(open(pf, encoding="utf-8")) if pf.exists() else []
        active = [p for p in pend if p.get("状态") == "pending"]
        targets = sorted({p.get("目标期号") for p in active if p.get("目标期号") is not None})
        groups = sum(len(p.get("预测号码") or []) for p in active)
        issues = []
        if not active:
            issues.append("无 pending（下一期没有任何预测）")
        else:
            if targets and latest is not None and min(targets) <= latest:
                issues.append(f"目标期号 {targets} 已开奖（马后炮）")
            elif targets and latest is not None and min(targets) != latest + 1:
                issues.append(f"目标期号 {targets} ≠ 本地最新期+1({latest + 1})")
            # 出号注数期望值按彩种分流（2026-09-13）：数字型固定 5 注；
            # 乐透型名义 100 注、启用低重叠剪枝后可能更少 → 只守"每策略保底"下限。
            if name in ("福彩3D", "排列3", "排列5"):
                expected = resolve_groups(name)
                if groups != expected:
                    issues.append(f"数字型出号 {groups} 注（期望 {expected}）")
            elif groups < PRUNE_MIN_PER_STRATEGY:
                issues.append(
                    f"乐透型出号仅 {groups} 注（低于每策略保底 {PRUNE_MIN_PER_STRATEGY}）")
        out.append({"彩种": name, "本地最新期": latest, "pending条数": len(active),
                    "目标期": targets, "出号注数": groups, "问题": issues})
    return out


def check_key_fields():
    """双/大 奖池奖金缺失；数字型 销售数据是否落后于开奖。"""
    out = []
    for name in ("双色球", "大乐透"):
        recs = load_lottery(name).records
        miss = [r.期号 for r in recs if not getattr(r, "奖池奖金", 0)]
        out.append({"彩种": name, "检查项": "奖池奖金缺失期数", "值": len(miss),
                    "样例": sorted(miss)[-12:]})
    for name in ("排列5", "福彩3D", "排列3", "七星彩"):
        try:
            from data.fetch_sales import load_sales
            df = load_sales(name)
            sales_latest = max(int(x) for x in df["期号"].tolist()) if df is not None and len(df) else None
        except Exception:
            sales_latest = None
        hist_latest = max((r.期号 for r in load_lottery(name).records if r.期号), default=None)
        out.append({"彩种": name, "检查项": "销售数据最新期",
                    "值": f"销售 {sales_latest} / 开奖 {hist_latest}",
                    "落后": (hist_latest - sales_latest) if (sales_latest and hist_latest) else None})
    return out


def check_pipeline_freshness():
    sp = BASE / "logs" / "automation" / "status.json"
    if not sp.exists():
        return [{"彩种": "-", "问题": "status.json 不存在"}]
    snap = (json.load(open(sp, encoding="utf-8")) or {}).get("last_run_per_lottery", {}) or {}
    out = []
    for name in LOTTERY_CONFIG:
        latest = max((r.期号 for r in load_lottery(name).records if r.期号), default=None)
        v = snap.get(name) or {}
        msg = str(v.get("message") or "")
        # 简报里的「最新开奖期号」直接取 draw_numbers.期号（最可靠）；
        # 老快照没有则从 message「最新 NNNNN，…」里用正则提取（勿直接吞数字，"5 步"会串进来）。
        snap_issue = (v.get("draw_numbers") or {}).get("期号")
        if snap_issue is None:
            import re
            m = re.search(r"最新\s*(\d+)", msg)
            snap_issue = int(m.group(1)) if m else None
        else:
            try:
                snap_issue = int(snap_issue)
            except (TypeError, ValueError):
                snap_issue = None
        out.append({"彩种": name, "快照时间": v.get("time"), "快照期号": snap_issue,
                    "CSV最新期": latest,
                    "落后": (latest - snap_issue) if (snap_issue and latest and latest > snap_issue) else 0})
    return out


def check_write_guard():
    """自检：生产写保护在「pytest 语境」下确实拦截，且路径判定正确。

    本脚本自身不在 pytest 进程内，所以不能靠"真写一次看有没有被拦"来判定；
    改为临时把 `_is_pytest` 置真后测试（测完还原，不污染任何数据）。
    """
    import data.feedback as fb
    probe = fb.FEEDBACK_DIR / "_health_check_probe.json"
    if probe.exists():
        return {"可用": False, "说明": "探针文件已存在（历史残留），请人工确认后删除"}
    path_ok = fb._is_prod_feedback_path(probe) and not fb._is_prod_feedback_path(BASE / "logs" / "x.json")
    orig = fb._is_pytest
    blocked = False
    try:
        fb._is_pytest = lambda: True
        fb._safe_write_json(probe, {"probe": 1})
        blocked = not probe.exists()
    finally:
        fb._is_pytest = orig
    return {"可用": bool(path_ok and blocked),
            "说明": ("pytest 语境下已阻止写生产 feedback 目录，路径判定正确" if path_ok and blocked
                     else f"⚠️ 写保护异常（路径判定={path_ok}, 拦截={blocked}）——生产数据可能被测试覆盖")}


def _quarantined(tag: str, hist: list, pend: list) -> bool:
    """判断某条审计问题是否落在「已隔离记录」上。

    已隔离 = valid_prediction=False（按设计不参与策略权重/战绩统计）。
    这类记录上的号码问题属历史污染痕迹，保留可追溯即可，不算待处理。
    定位串形如 "历史#5 期26090" / "pending#0 目标期26106"。
    """
    import re
    m = re.match(r"历史#(\d+)", tag)
    if m:
        i = int(m.group(1))
        if 0 <= i < len(hist):
            return not bool(hist[i].get("valid_prediction", True))
        return False
    return False


def main():
    today = date.today()
    integrity = [scan_lottery(n, today) for n in LOTTERY_CONFIG]
    record_issues = {}
    for name in LOTTERY_CONFIG:
        draws, _ = draw_index(name)
        hf = FB / f"{name}_feedback_history.json"
        pf = FB / f"{name}_pending.json"
        hist = json.load(open(hf, encoding="utf-8")) if hf.exists() else []
        pend = json.load(open(pf, encoding="utf-8")) if pf.exists() else []
        hi, hs = audit_history(name, draws, hist)
        pi, ps = audit_pending(name, draws, pend)
        hi_real = [x for x in hi if not _quarantined(x[0], hist, pend)]
        hi_quar = [x for x in hi if _quarantined(x[0], hist, pend)]
        record_issues[name] = {"历史": len(hi_real), "pending": len(pi),
                               "已隔离": len(hi_quar),
                               "统计": dict(hs) | dict(ps),
                               "样例": [{"定位": a, "项": b, "详情": c} for a, b, c in (hi_real + pi)[:8]],
                               "已隔离样例": [{"定位": a, "项": b, "详情": c} for a, b, c in hi_quar[:5]]}

    real_gaps = []
    for r in integrity:
        for p in r.get("问题", []):
            if p.get("级别") != "严重":
                continue
            for m in r.get("缺期明细", []):
                for q in m.get("缺号", []):
                    qi = _local_issue(r["彩种"], q)
                    if qi is None:
                        continue
                    if (r["彩种"], qi) not in KNOWNS:
                        real_gaps.append((r["彩种"], q))

    pending = check_pending_freshness()
    fields = check_key_fields()
    pipeline = check_pipeline_freshness()
    guard = check_write_guard()

    real_problems = (
        [(p["彩种"], "|".join(p["问题"])) for p in pending if p["问题"]]
        + [(g[0], f"真缺期 {g[1]}") for g in real_gaps]
        + [(n, f"记录问题 {v['历史'] + v['pending']} 条")
           for n, v in record_issues.items() if v["历史"] + v["pending"] > 0]
        + [(p["彩种"], f"流水线快照落后 {p['落后']} 期")
           for p in pipeline if p.get("落后")]
        + ([("系统", guard["说明"])] if not guard["可用"] else [])
    )

    stamp = f"{today:%Y%m%d}"
    (BASE / "logs" / f"项目体检_{stamp}.json").write_text(
        json.dumps({"日期": str(today), "完整性": integrity, "记录审计": record_issues,
                    "pending": pending, "关键字段": fields, "流水线": pipeline,
                    "写保护": guard, "真实问题": real_problems},
                   ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    L = [f"# 项目数据体检 {today}", "",
         f"**真实待处理问题：{len(real_problems)} 项**", ""]
    L.append("## 1. 开奖数据完整性")
    L.append("| 彩种 | 行数 | 最新期 | 滞后 | 缺期 | 越界 | 空值 | 重复 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for r in integrity:
        L.append(f"| {r['彩种']} | {r.get('行数', 0)} | {r.get('最新一期', {}).get('期号', '-')} | "
                 f"{r.get('滞后天数', '-')}天 | {r.get('缺期总数', 0)} | {r.get('号码越界/非法', 0)} | "
                 f"{r.get('号码空值', 0)} | {len(r.get('重复期号', {}))} |")
    L.append("")
    L.append("## 2. 记录审计（feedback / pending）")
    L.append("| 彩种 | 历史问题 | pending问题 | 已隔离(不计) | 明细 |")
    L.append("|---|---|---|---|---|")
    for n, v in record_issues.items():
        L.append(f"| {n} | {v['历史']} | {v['pending']} | {v.get('已隔离', 0)} | "
                 f"{'; '.join(d['项'] + '@' + d['定位'] for d in v['样例']) or '✅'} |")
    L.append("")
    L.append("## 3. pending 新鲜度")
    L.append("| 彩种 | 本地最新期 | 目标期 | 出号注数 | 问题 |")
    L.append("|---|---|---|---|---|")
    for p in pending:
        L.append(f"| {p['彩种']} | {p['本地最新期']} | {p['目标期']} | {p['出号注数']} | {'; '.join(p['问题']) or '✅'} |")
    L.append("")
    L.append("## 4. 关键字段完整度")
    for f in fields:
        L.append(f"- {f['彩种']}：{f['检查项']} = {f['值']}"
                 + (f"（样例 {f['样例']}）" if f.get("样例") else "")
                 + (f"（落后 {f['落后']} 期）" if f.get("落后") is not None else ""))
    L.append("")
    L.append("## 5. 流水线快照新鲜度")
    for p in pipeline:
        L.append(f"- {p['彩种']}：快照 {p.get('快照时间')} 期{p.get('快照期号')} vs CSV 最新 {p.get('CSV最新期')}"
                 + (f" → 落后 {p['落后']} 期" if p.get("落后") else " ✅"))
    L.append("")
    L.append("## 6. 写保护自检")
    L.append(f"- {guard['说明']}")
    if real_problems:
        L.append("")
        L.append("## ⚠️ 真实待处理问题")
        for n, d in real_problems:
            L.append(f"- **{n}**：{d}")
    md = BASE / "logs" / f"项目体检_{stamp}.md"
    md.write_text("\n".join(L), encoding="utf-8")

    # 自检推送（2026-09-18）：体检跑完把「真实待处理问题 N 项」摘要推给企业微信群。
    # 未启用/失败一律降级，绝不影响体检本身。
    try:
        from data.push_notify import push_health_summary
        push_health_summary(real_problems, report_path=str(md))
    except Exception as e:  # pragma: no cover
        print(f"  体检推送失败(降级): {e}")

    print(f"体检完成：真实待处理问题 {len(real_problems)} 项")
    for n, d in real_problems:
        print(f"  - {n}: {d}")
    print(f"报告: {md}")


if __name__ == "__main__":
    main()
