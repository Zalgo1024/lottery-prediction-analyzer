"""
可信度层 · 数据质量评分（异常 = 开奖质量监控）

不预设"开奖不公平"，只做可证伪的随机性/完整性检验：
  1. 均匀性 χ²（每分区）：各号码出现频率是否偏离均匀。七星彩第7位仅取 2020-10-11
     规则变更后的现代期（避免新旧两期混算的假象，详见项目记忆）。
  2. 游程检验（Wald-Wolfowitz，每分区）：序列是否呈"聚簇/震荡"偏离独立随机。
  3. 完整性：重复开奖、期号断裂、日期大间隔。
  4. 多重比较：所有 χ² + 游程 p 值经 Benjamini-Hochberg 校正。

产出单一"数据可信度评分"(0-100) 与评级，以及逐条可复核的检验明细。
"""

from datetime import date
import math
from typing import Dict, List

import numpy as np
from scipy import stats

from data.loader import load_lottery
from data.schema import get_schema, record_zone_numbers

# 七星彩第7位规则变更日（后区由 0-9 扩为 0-14）
_QXC_CUTOFF = date(2020, 10, 11)


def _ww_runs_z(binary) -> tuple:
    """Wald-Wolfowitz 游程检验（scipy<1.16 无 runs_test，手写标准式）。
    返回 (z, p)；退化序列返回 (None, 1.0)。"""
    n = len(binary)
    n1 = int(np.sum(binary))
    n0 = n - n1
    if n0 == 0 or n1 == 0 or n < 2:
        return None, 1.0
    b = np.asarray(binary)
    r = int(1 + np.sum(b[1:] != b[:-1]))
    exp_r = 2.0 * n0 * n1 / n + 1.0
    var_r = 2.0 * n0 * n1 * (2.0 * n0 * n1 - n) / (n * n * (n - 1.0))
    if var_r <= 0:
        return None, 1.0
    z = (r - exp_r) / math.sqrt(var_r)
    p = float(2.0 * (1.0 - stats.norm.cdf(abs(z))))
    return float(z), p


def _draw_key(schema, rec) -> tuple:
    """开奖指纹：各分区号码（无序区排序、有序区保序）拼成元组，用于重复检测。"""
    parts = []
    for zone in schema.zones:
        nums = list(record_zone_numbers(rec, zone))
        if zone.ordered:
            parts.append(tuple(nums))
        else:
            parts.append(tuple(sorted(nums)))
    return tuple(parts)


def data_quality_report(lottery_name: str, fdr_alpha: float = 0.05) -> Dict:
    data = load_lottery(lottery_name)
    schema = get_schema(lottery_name)
    chron = sorted(data.records, key=lambda r: r.期号 or 0)
    n = len(chron)

    tests: List[Dict] = []

    # —— 均匀性 χ² + 游程检验（每分区）——
    for zone in schema.zones:
        # 七星彩第7位：仅用现代期（0-9→0-14 规则变更，全史混用会在相邻自相关上
        # 造出假异常——前段基准同号率 1/10 被拿去比后段期望 1/15，p≈1e-10 是
        # 口径伪影而非机制异常，见 discovery/randomness_audit.py P2-1 定位）
        base = chron
        if zone.max == 14 and lottery_name == "七星彩":
            base = [rec for rec in chron
                    if rec.开奖日期 and rec.开奖日期 >= _QXC_CUTOFF]

        nums = [rec for rec in base]
        # 取值序列（该分区每期一个值；乐透型每期多个值则展开为多期观测）
        seq = []  # 逐期主值（乐透型取首值代表性较弱，故乐透型改用展开计数）
        values = []
        for rec in base:
            v = list(record_zone_numbers(rec, zone))
            values.extend(v)
            if v:
                seq.append(v[0])
        values = np.array(values)
        size = zone.size

        # 均匀性 χ²（观测 vs 均匀期望）—— 用蒙特卡洛公平零分布校准
        # 说明：全历史观测量极大（双色球红球约 2 万次），渐近 χ² 会把公平随机的
        # 微小抽样扰动也判为"显著"，属于大样本过度拒绝。改用 MC 零分布：
        # 模拟 B 次同等规模的公平独立抽取，比较观测 χ² 在其中的分位。
        # 注意：bincount 按"数值"建索引，必须减去 zone.min 归一到 0..size-1，
        # 否则红/蓝球(1-33/1-16)的最大号会被截断、且出现空 bin 0 制造虚假大 χ²。
        idx = values.astype(int) - zone.min
        idx = idx[(idx >= 0) & (idx < size)]
        counts = np.bincount(idx, minlength=size)[:size]
        total = int(counts.sum())
        expected = total / size
        if expected > 0 and total > size:
            obs_chi2 = float(np.sum((counts - expected) ** 2 / expected))
            # Cramér's V：效应量（与 N 无关），用于判断偏离"实际有多大"
            v = float(np.sqrt(obs_chi2 / (total * (size - 1))))
            max_rel = float(np.max(np.abs(counts - expected) / expected))
            B = 2000
            rng = np.random.default_rng(1000 + zone.min)
            sim = rng.integers(0, size, size=(B, total))
            # 逐次模拟分别计数（注意：必须按行 bincount，不能 ravel 合并）
            sim_counts = np.zeros((B, size), dtype=np.int64)
            for cat in range(size):
                sim_counts[:, cat] = np.count_nonzero(sim == cat, axis=1)
            sim_chi2 = np.sum((sim_counts - expected) ** 2 / expected, axis=1)
            p = float((1 + int(np.sum(sim_chi2 >= obs_chi2))) / (B + 1))
            tests.append({
                "检验": "均匀性χ²(MC)", "分区": zone.name,
                "统计量": round(obs_chi2, 3), "p值": round(p, 4),
                "效应量": round(v, 4),
                "明细": f"MC-B={B}, CramérV={v:.4f}, 最大相对偏差={max_rel:.2%}",
            })

        # 游程检验（中位二值化）
        if len(seq) >= 10:
            med = np.median(seq)
            binary = (np.array(seq) >= med).astype(int)
            # 去除全相等退化
            if binary.sum() > 0 and binary.sum() < len(binary):
                z, p_run = _ww_runs_z(binary)
                if z is not None:
                    tests.append({
                        "检验": "游程检验", "分区": zone.name,
                        "统计量": round(z, 3),
                        "p值": round(p_run, 4),
                        "明细": f"中位数={med}",
                    })

        # 相邻自相关（随机性/完整性监控）：相邻两期是否异常"重复"
        # 这是捕捉"假边缘"的关键——例如七星彩第7位若相邻期高度重复，
        # 会让"热门策略"凭空占优，但这是数据/摇奖机制异常，而非可投注信号。
        # ⚠️ 必须用与 χ² 相同的 base（现代段），否则规则变更的基准率差异
        # 会制造伪异常（P2-1 已定位并修复）。
        draws = [list(record_zone_numbers(rec, zone)) for rec in base]
        if len(draws) >= 10:
            npairs = len(draws) - 1
            if zone.ordered:
                adj = sum(1 for a, b in zip(draws, draws[1:])
                          if a and b and a[0] == b[0])
                e = 1.0 / size
            else:
                adj = sum(1 for a, b in zip(draws, draws[1:])
                          if a and b and (set(a) & set(b)))
                denom = math.comb(size, zone.choose)
                # 共享≥1号的组合数 = 总组合 - 完全不共享的组合数(comb(size-choose, choose))
                c = max(0, denom - math.comb(size - zone.choose, zone.choose))
                e = (c / denom) if denom > 0 else 0.0  # 相邻两期共享≥1号的期望概率
            p_hat = adj / npairs
            se = (e * (1 - e) / npairs) ** 0.5
            z = (p_hat - e) / se if se > 0 else 0.0
            p = float(2 * (1 - stats.norm.cdf(abs(z)))) if se > 0 else 1.0
            tests.append({
                "检验": "相邻自相关", "分区": zone.name,
                "统计量": round(z, 3), "p值": round(p, 4),
                "效应量": round(abs(p_hat - e) / e, 4),
                "明细": f"观测相邻相同率={p_hat:.3f}, 期望={e:.3f}",
            })

    # —— 完整性 ——
    # 重复开奖
    seen = {}
    dup = 0
    for rec in chron:
        key = _draw_key(schema, rec)
        if key in seen:
            dup += 1
        else:
            seen[key] = rec.期号

    # 期号断裂
    issues = sorted(r.期号 for r in chron if r.期号 is not None)
    gaps = sum(1 for a, b in zip(issues, issues[1:]) if (b - a) > 1) if len(issues) > 1 else 0

    # 日期大间隔（>7 天视为异常间隔，节假日除外需人工判断）
    dates = [r.开奖日期 for r in chron if r.开奖日期]
    date_gaps = []
    for a, b in zip(dates, dates[1:]):
        date_gaps.append((b - a).days)
    max_date_gap = max(date_gaps) if date_gaps else 0
    big_gaps = sum(1 for g in date_gaps if g > 7)

    # —— FDR 校正 ——
    pvals = [t["p值"] for t in tests]
    if pvals:
        m = len(pvals)
        order = np.argsort(pvals)
        ranked = np.array(pvals)[order]
        q = np.empty(m)
        prev = 1.0
        for i in range(m - 1, -1, -1):
            q[i] = min(ranked[i] * m / (i + 1), prev)
            prev = q[i]
        qvals = np.empty(m)
        for pos, orig in enumerate(order):
            qvals[orig] = q[pos]
        for i, t in enumerate(tests):
            t["q值(FDR)"] = round(float(qvals[i]), 4)
            t["统计显著(α=%.2f)" % fdr_alpha] = bool(qvals[i] <= fdr_alpha)
    # 实质显著性：大样本下统计显著极易由微小偏差触发，评分只看"效应量是否实质"
    #   均匀性 CramérV ≥ 0.10 视为实质偏离；相邻自相关 相对偏差 ≥ 5% 视为实质。
    def _practical(t: dict) -> bool:
        if not t.get("统计显著(α=%.2f)" % fdr_alpha, False):
            return False
        if t["检验"].startswith("均匀性"):
            return t.get("效应量", 0.0) >= 0.10
        if t["检验"] == "相邻自相关":
            return t.get("效应量", 0.0) >= 0.05
        if t["检验"] == "游程检验":
            return abs(t.get("统计量", 0.0)) >= 3.0
        return True
    for t in tests:
        t["实质显著"] = _practical(t)
    n_rejected = sum(1 for t in tests if t["实质显著"])

    # —— 可信度评分 ——
    total = len(tests)
    fail_ratio = (n_rejected / total) if total else 0.0
    score = 100.0 * (1 - fail_ratio)
    # 完整性惩罚
    if dup > 0:
        score -= min(15.0, dup * 5.0)
    if gaps > 0:
        score -= min(5.0, gaps * 0.5)
    if big_gaps > 0:
        score -= min(5.0, big_gaps * 0.3)
    score = max(0.0, min(100.0, score))

    if score >= 90:
        grade = "A（数据随机性/完整性良好）"
    elif score >= 75:
        grade = "B（基本可信，存在个别告警）"
    elif score >= 60:
        grade = "C（存在需注意的偏离）"
    else:
        grade = "D（存在显著异常，谨慎使用）"

    notes = []
    if dup > 0:
        notes.append(f"发现 {dup} 期与历史某期完全相同的开奖组合（相隔多年，概率约 1/千万级，"
                     f"偶有巧合，不必然为数据源错误）。")
    if gaps > 0:
        notes.append(f"期号断裂 {gaps} 处（可能缺期或数据源跳号）。")
    if big_gaps > 0:
        notes.append(f"日期间隔 >7 天 {big_gaps} 处（多为休市/节假日，需人工确认）。")
    practical = [t for t in tests if t["实质显著"]]
    if practical:
        names = [f"{t['分区']}/{t['检验']}" for t in practical]
        notes.append("实质偏离随机（计分）：" + "、".join(names[:6]) + ("…" if len(names) > 6 else ""))
    # 统计显著但效应量微小：仅提示，不计分（避免大样本过度拒绝误判）
    only_stat = [t for t in tests
                 if t.get("统计显著(α=%.2f)" % fdr_alpha, False) and not t["实质显著"]]
    if only_stat:
        names = [f"{t['分区']}/{t['检验']}" for t in only_stat]
        notes.append("统计显著但效应量微小（不计分，视为抽样噪声）：" + "、".join(names[:6]) +
                     ("…" if len(names) > 6 else ""))

    return {
        "lottery_name": lottery_name,
        "n_records": n,
        "tests": tests,
        "fdr_alpha": fdr_alpha,
        "n_tests": total,
        "n_rejected": n_rejected,
        "duplicates": dup,
        "issue_gaps": gaps,
        "max_date_gap_days": max_date_gap,
        "big_date_gaps": big_gaps,
        "trust_score": round(score, 1),
        "grade": grade,
        "notes": notes,
    }
