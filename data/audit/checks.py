# -*- coding: utf-8 -*-
"""审计检查项 —— 只读、只记、不改任何出号状态。

★ 本文件**不得** import `prediction` 或 `ev.budget_gate`，也不得出现
  `apply_dynamic_result` / `record_spend` / `clamp` / `resolve_groups`
  （`tests/test_audit_no_stake.py` 扫源码强制）。原因见包 docstring：
  一旦异常能改注数，监测就退化成「看见不利结果就加码」的反馈回路。

每条检查返回 `{项, 级别, 描述, 证据}`，级别 ∈ {待查, 提示}。
措辞纪律：只有「未检出」与「检出 N 条待查」，没有「已证实」。
"""

from __future__ import annotations

from collections import Counter
from typing import Any, Dict, List, Optional

from data.audit import thresholds as TH
from data.audit import CONCLUSION_DISCIPLINE

LOTS = ("双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩",
        "七乐彩", "快乐8")


def _num(x: Any) -> float:
    try:
        return float(str(x).strip())
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ 各项检查
def check_config_health() -> List[Dict[str, Any]]:
    """配置健康度。2026-10-31 的事故就是从这里漏过去的。"""
    from data import ticket_size as TS

    out = []
    try:
        hc = TS.health()
    except Exception as e:                                    # noqa: BLE001
        return [{"项": "配置健康度", "级别": "待查",
                 "描述": f"哨兵自身失败：{type(e).__name__}", "证据": str(e)}]
    if not hc.get("安全"):
        out.append({"项": "配置健康度", "级别": "待查",
                    "描述": "出号配置不可信（模式/锁定/limits/文件存在性）",
                    "证据": "；".join(hc.get("原因") or []) or "原因未知"})
    return out


def check_data_files() -> List[Dict[str, Any]]:
    """开奖历史文件是否存在且非空。

    ★ 起因（2026-10-31）：`lottery_data/` 下 12 个 CSV 在同一分钟被删空，
      仓库里**没有任何代码路径会删它们**（全仓 grep 过 rmtree/unlink/remove），
      日志也没有对应记录 ⇒ 追溯不到进程。既然查不出「谁删的」，就退一步做到
      「下次一删就立刻看见」：文件缺失/空文件在这里成为一条待查。

    ★ 只报「缺失 / 空 / 表头行数异常」，不猜原因，也不动文件。
    """
    import csv as _csv
    from data.loader import _file_path

    out: List[Dict[str, Any]] = []
    for lot in LOTS:
        try:
            p = _file_path(lot)
        except Exception as e:                                # noqa: BLE001
            out.append({"项": "开奖数据文件", "级别": "待查",
                        "彩种": lot, "描述": f"路径解析失败：{type(e).__name__}",
                        "证据": str(e)})
            continue
        if not p.exists():
            out.append({"项": "开奖数据文件", "级别": "待查", "彩种": lot,
                        "描述": "原始 CSV 不存在（被删/被移走/盘符变了）",
                        "证据": str(p)})
            continue
        try:
            size = p.stat().st_size
        except OSError as e:
            out.append({"项": "开奖数据文件", "级别": "待查", "彩种": lot,
                        "描述": "文件存在但读不到状态（可能被独占）", "证据": str(e)})
            continue
        if size <= 0:
            out.append({"项": "开奖数据文件", "级别": "待查", "彩种": lot,
                        "描述": "文件是空的（0 字节）", "证据": str(p)})
            continue
        try:
            with p.open("r", encoding="utf-8", errors="replace") as f:
                rows = sum(1 for _ in _csv.reader(f))
        except OSError as e:
            out.append({"项": "开奖数据文件", "级别": "待查", "彩种": lot,
                        "描述": "打开失败（可能被独占）", "证据": str(e)})
            continue
        if rows <= 1:
            out.append({"项": "开奖数据文件", "级别": "待查", "彩种": lot,
                        "描述": f"只有表头没有数据（{rows} 行）", "证据": str(p)})
    return out


def check_missing_periods(lottery: str, *, issues: Optional[List[Any]] = None,
                          th: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """期号序列的连续缺失段。

    ★ 必须先用 `data/issue_key` 拆成 (年, 序号) 再比。**期号每年重编号**：
      双色球 `3089` = 2003 年第 89 期、`4001` = 2004 年第 1 期 —— 直接按数值差
      会得出「缺 911 期」，全是假阳性（实测一次跑出 126 条，审计直接变成噪声）。
      跨年衔接处不判缺失。
    """
    th = th or TH.load()
    limit = int((th.get("阈值") or {}).get("缺失期段上限", 1))
    if issues is None:
        from data.loader import load_lottery
        issues = [getattr(r, "期号", None) for r in load_lottery(lottery).records]

    from data.issue_key import IssueKeyError, issue_key

    keys = []
    for i in issues:
        if i is None:
            continue
        try:
            keys.append((issue_key(lottery, i), i))
        except IssueKeyError:
            continue
    keys.sort(key=lambda t: t[0])

    out = []
    for (k1, raw1), (k2, raw2) in zip(keys, keys[1:]):
        y1, s1 = k1
        y2, s2 = k2
        if y1 != y2:              # 跨年 ⇒ 序号不连续是正常的
            continue
        gap = s2 - s1 - 1
        if gap > limit:
            out.append({"项": "缺失期", "级别": "待查",
                        "描述": f"{lottery} {y1} 年 第{s1}期→第{s2}期 之间缺 {gap} 期",
                        "证据": f"{raw1} → {raw2}"})
    return out


def check_lookahead(lottery: str, *, records: Optional[List[dict]] = None,
                    th: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """时间戳错位：**出号时刻晚于开奖日** ⇒ 字面上就是「开奖后才给号」。

    ★ 判据只认 `批次时间`（= 出号时刻）。`预测日期` 是目标开奖日的派生字段，
      恒等于开奖日期，拿它判前视永远判不出来（历史踩过：173 条真预测被误标）。
    """
    th = th or TH.load()
    allow = int((th.get("阈值") or {}).get("允许前视条数", 0))
    if records is None:
        from data.feedback import load_feedback_history
        records = load_feedback_history(lottery) or []

    bad = []
    for r in records:
        bt = str(r.get("批次时间") or "")
        dd = str(r.get("开奖日期") or "")
        if len(bt) >= 10 and len(dd) >= 10 and bt[:10] > dd:
            bad.append({"期号": r.get("期号"), "批次时间": bt, "开奖日期": dd})
    if len(bad) > allow:
        return [{"项": "时间戳错位", "级别": "待查",
                 "描述": f"{lottery} 有 {len(bad)} 条记录的出号时刻晚于开奖日",
                 "证据": bad[:5]}]
    return []


def check_amount_unknown(lottery: str, *, records: Optional[List[dict]] = None,
                         th: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """「金额未知」占比。它不是异常成因的证据，而是**读数可信度**的指标。"""
    th = th or TH.load()
    cap = float((th.get("阈值") or {}).get("金额未知占比上限", 0.5))
    if records is None:
        from data.feedback import load_feedback_history
        records = load_feedback_history(lottery) or []
    if not records:
        return []
    unknown = sum(1 for r in records if r.get("金额未知"))
    ratio = unknown / len(records)
    if ratio > cap:
        return [{"项": "金额未知占比", "级别": "待查",
                 "描述": f"{lottery} 有 {unknown}/{len(records)} 注（{ratio:.1%}）"
                         f"奖金未知，超过阈值 {cap:.0%}",
                 "证据": "金额未知的注不参与求和，账面净盈亏会偏保守/不可比"}]
    return []


def check_rule_version_switches(lottery: str, *, records: Optional[List[dict]] = None
                                ) -> List[Dict[str, Any]]:
    """相邻期出现规则版本切换 ⇒ 提示（不是异常，但会让金额口径不可比）。"""
    if records is None:
        from data.feedback import load_feedback_history
        records = load_feedback_history(lottery) or []
    from ev.profit.rules import rules_for

    vers = Counter()
    for r in records:
        try:
            vers[rules_for(lottery, r.get("期号")).rule_version] += 1
        except Exception:                                     # noqa: BLE001
            vers["(不可解析)"] += 1
    if len(vers) > 1:
        return [{"项": "规则版本", "级别": "提示",
                 "描述": f"{lottery} 的记录跨 {len(vers)} 个规则版本",
                 "证据": dict(vers.most_common()),
                 "备注": "同名奖级在不同版本下含义可能不同（大乐透「四等」差 35 倍），"
                         "跨版本求和是错口径"}]
    return []


def run(lotteries: Optional[List[str]] = None) -> Dict[str, Any]:
    """跑全部检查。返回结构化结果 + 一句结论。"""
    th = TH.load()
    lots = lotteries or list(LOTS)
    findings: List[Dict[str, Any]] = []

    findings.extend(check_config_health())
    findings.extend(check_data_files())
    for lot in lots:
        findings.extend(check_missing_periods(lot, th=th))
        findings.extend(check_lookahead(lot, th=th))
        findings.extend(check_amount_unknown(lot, th=th))
        findings.extend(check_rule_version_switches(lot))

    pending = [f for f in findings if f.get("级别") == "待查"]
    return {
        "阈值版本": th.get("版本"),
        "阈值设定日期": th.get("设定日期"),
        "阈值哈希": TH.content_hash(th),
        "阈值锁定": bool(th.get("锁定")),
        "彩种": lots,
        "检出": findings,
        "待查条数": len(pending),
        "结论": ("未检出" if not pending else f"检出 {len(pending)} 条待查"),
        "纪律": CONCLUSION_DISCIPLINE,
    }


def render_markdown(result: Dict[str, Any]) -> str:
    md = ["# 异常监测报告", ""]
    md.append(f"阈值：{result.get('阈值版本')}（设定 {result.get('阈值设定日期')}，"
              f"哈希 {result.get('阈值哈希')}，锁定={result.get('阈值锁定')}）")
    md.append("")
    md.append(f"**结论：{result.get('结论')}**")
    md.append("")
    if result.get("检出"):
        md.append("| 项 | 级别 | 描述 |")
        md.append("|---|---|---|")
        for f in result["检出"]:
            md.append(f"| {f.get('项')} | {f.get('级别')} | {f.get('描述')} |")
        md.append("")
    else:
        md.append("本窗口内未检出需要人工查看的条目。")
        md.append("")
    md.append(f"> {result.get('纪律')}")
    return "\n".join(md)
