"""
可信度层 · 数据质量评分（异常 = 开奖质量监控）

不预设"开奖不公平"，只做可检验的随机性/完整性检验：
  1. 均匀性 χ²（每分区）：各号码出现频率是否偏离均匀。七星彩第7位仅取 2020-10-11
     规则变更后的现代期（避免新旧两期混算的假象，详见项目记忆）。
  2. 游程检验（Wald-Wolfowitz，每分区）：序列是否呈"聚簇/震荡"偏离独立随机。
  3. 完整性：重复开奖（对比生日悖论基线）、真缺期（按年分组）、缺开奖日（按休市日历）。
  4. 多重比较：所有 χ² + 游程 p 值经 Benjamini-Hochberg 校正。

★ 完整性三项全部按**基线**判，不按裸计数判（2026-10-23 修，此前三项都在罚"正常现象"）：
  · 期号每年重编号（`26281 → 27001`），直接比整数会把 **22 个跨年**当成 22 处缺期；
  · 重复开奖是**生日悖论**：3D 只有 1000 种组合、7788 期，必然有 6788 期重复；
  · 日期间隔 >7 天几乎都是**春节休市**（另有 2019 国庆、2020 疫情），不是缺数据。
  休市日历真源 `data/holiday.is_draw_day`；期号归一真源 `data/issue_key.issue_key`。

产出单一"数据可信度评分"(0-100) 与评级，以及逐条可复核的检验明细。
"""

from datetime import date
import math
from typing import Dict, List, Optional

import numpy as np
from scipy import stats

from data.issue_key import IssueKeyError, issue_key
from data.loader import load_lottery
from data.schema import get_schema, record_zone_numbers

# 七星彩第7位规则变更日（后区由 0-9 扩为 0-14）
_QXC_CUTOFF = date(2020, 10, 11)

# 重复开奖的「显著高于基线」门限（Poisson 近似的 z）。
# 只在 z>5（p<3e-7）才扣分：生日悖论本身就能产生大量重复，
# 而乐透型出现 1~2 次同号也只是「值得人工核对」，不该直接判成数据错误。
_DUP_PENALTY_Z = 5.0


def _outcome_space(lottery_name: str) -> Optional[int]:
    """单注结局空间大小（组合数）。取不到 ⇒ None（宁可不算也不瞎估）。"""
    try:
        from ev.profit.rules import rules_for           # noqa: PLC0415
        return int(rules_for(lottery_name).outcome_size)
    except Exception:                                   # noqa: BLE001
        return None


def _expected_duplicates(n: int, space: Optional[int]) -> Optional[float]:
    """n 期、结局空间 space 下的**期望重复期数**（生日悖论基线）。

    有放回均匀抽样，独立期数期望 = space·(1−e^(−n/space))，
    故「重复（此前已出现过）的期数」期望 = n − space·(1−e^(−n/space))。
    3D 实测：n=7788, space=1000 ⇒ 基线 6788.4，观测 6788 ⇒ 完全正常。
    """
    if not space or space <= 0 or n <= 0:
        return None
    return n - space * (1.0 - math.exp(-n / space))



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
                # ★ 这里比的是**两期开奖**的重叠，所以用 draw_count 而不是 choose。
                #   七乐彩「特别号」票面 choose=0（draw_only）、开奖 draw=1：
                #   用 choose 会得到 comb(30,0)=1 ⇒ e=0 ⇒ 效应量除零（2026-11-02 实踩，
                #   /api/audit/quality/七乐彩 直接 500）。改用 draw_count 后
                #   e=1/30，正好是「两期特别号相同」的真实概率。
                w = int(zone.draw_count)
                denom = math.comb(size, w)
                # 共享≥1号的组合数 = 总组合 - 完全不共享的组合数(comb(size-w, w))
                c = max(0, denom - math.comb(size - w, w)) if size >= w else 0
                e = (c / denom) if denom > 0 else 0.0  # 相邻两期共享≥1号的期望概率
            p_hat = adj / npairs
            se = (e * (1 - e) / npairs) ** 0.5
            z = (p_hat - e) / se if se > 0 else 0.0
            p = float(2 * (1 - stats.norm.cdf(abs(z)))) if se > 0 else 1.0
            # ★ e=0 时「相对效应量」无定义（不是 0，也不是无穷）：给 None 让界面显示「—」。
            #   宁可显式缺失，也不要拿一个假数字充数 —— 那会被当成"效应量为零"。
            eff = round(abs(p_hat - e) / e, 4) if e > 0 else None
            tests.append({
                "检验": "相邻自相关", "分区": zone.name,
                "统计量": round(z, 3), "p值": round(p, 4),
                "效应量": eff,
                "明细": f"观测相邻相同率={p_hat:.3f}, 期望={e:.3f}",
            })

    # —— 完整性 ——
    # 重复开奖（★ 与生日悖论基线比，不按裸计数判）
    seen = {}
    dup = 0
    for rec in chron:
        key = _draw_key(schema, rec)
        if key in seen:
            dup += 1
        else:
            seen[key] = rec.期号
    space = _outcome_space(lottery_name)
    dup_base = _expected_duplicates(n, space)
    dup_z = None
    if dup_base and dup_base > 0:
        dup_z = (dup - dup_base) / math.sqrt(dup_base)

    # 真缺期：★ 期号每年重编号，必须先归一成 (年, 序号)，只在同一年内判跳号。
    #   直接比整数会把每年 1 次的 `26281 → 27001` 当成缺期：实测 5 个彩种
    #   各虚报 19~23 处，而真实缺期只有排列5 的 2 期。
    keys: List[tuple] = []
    for rec in chron:
        if rec.期号 is None:
            continue
        try:
            keys.append(issue_key(lottery_name, rec.期号))
        except IssueKeyError:
            continue
    keys.sort()
    gaps = 0
    gap_samples: List[Dict] = []
    for (ya, sa), (yb, sb) in zip(keys, keys[1:]):
        if ya != yb:
            continue                                   # 跨年不算断裂
        skip = sb - sa - 1
        if skip > 0:
            gaps += skip
            if len(gap_samples) < 5:
                gap_samples.append({"前": f"{ya}-{sa:03d}", "后": f"{yb}-{sb:03d}", "缺": skip})

    # 日期间隔：只做**原始观测**，不做日历推断。
    #   ★ 此前用「间隔 >7 天 = 异常」当完整性缺陷，实测 22~24 处 ≈ 历史年数 ——
    #   全部是春节休市（另有 2019 国庆 9 天、2020 疫情 51 天），等于每年白扣 5 分。
    #   ★ 试过按 `data/holiday.is_draw_day` 数「应有但缺失的开奖日」，但开奖日程史上有
    #   变更（双色球 2003~2004 只开周日+周四，周二 2004 年年中才加入），拿当前日程
    #   回推会凭空多出 80+ 天；按年自校准也救不回来（2004 年中途变日程）。
    #   ⇒ 缺期只认 `issue_gaps`（按年分组的期号连续性，与开奖日程无关）。
    dates = sorted(r.开奖日期 for r in chron if r.开奖日期)
    date_gaps = [(b - a).days for a, b in zip(dates, dates[1:])]
    max_date_gap = max(date_gaps) if date_gaps else 0
    big_gaps = sum(1 for g in date_gaps if g > 7)      # 只报不罚：绝大多数是休市

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
    # 完整性惩罚（★ 全部按基线判，不按裸计数判）
    #   · 重复开奖：只在显著高于生日悖论基线（z>5）时扣分；
    #   · 缺期：只认 `gaps`（按年分组的期号连续性，与开奖日程无关）；
    #   · 日期间隔 >7 天**不再罚**：那是春节休市（22~24 处 ≈ 历史年数）。
    if dup_z is not None and dup_z > _DUP_PENALTY_Z:
        score -= min(15.0, 5.0 * (dup_z - _DUP_PENALTY_Z + 1.0))
    if gaps > 0:
        score -= min(5.0, gaps * 0.5)
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
        if dup_base is not None:
            if dup_z is not None and dup_z > _DUP_PENALTY_Z:
                notes.append(f"重复开奖 {dup} 期，显著高于生日悖论基线 {dup_base:.1f} 期"
                             f"（组合空间 {space:,}，z={dup_z:.1f}）⇒ 需核对数据源是否录重。")
            elif dup_z is not None and dup_z > 3.0:
                notes.append(f"重复开奖 {dup} 期，略高于生日悖论基线 {dup_base:.1f} 期"
                             f"（组合空间 {space:,}，z={dup_z:.1f}）⇒ 仅提示，不作数据错误判定。")
            else:
                notes.append(f"重复开奖 {dup} 期，与生日悖论基线 {dup_base:.1f} 期一致"
                             f"（组合空间 {space:,}）⇒ 正常现象，非数据错误。")
        else:
            notes.append(f"重复开奖 {dup} 期（组合空间未知，未做基线判定）。")
    if gaps > 0:
        sample = "、".join(f"{g['前']}→{g['后']} 缺{g['缺']}" for g in gap_samples)
        notes.append(f"真缺期 {gaps} 期（按年分组统计，跨年重编号不计）：{sample}"
                     + ("…" if gaps > sum(g["缺"] for g in gap_samples) else ""))
    if big_gaps > 0:
        notes.append(f"开奖日期间隔 >7 天 {big_gaps} 处（≈ 历史年数，多为春节休市；"
                     f"另有 2019 国庆 / 2020 疫情），仅供人工参考，不计入评分。")
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
        "outcome_space": space,
        "expected_duplicates": None if dup_base is None else round(dup_base, 2),
        "dup_z": None if dup_z is None else round(dup_z, 2),
        "issue_gaps": gaps,
        "issue_gap_samples": gap_samples,
        "max_date_gap_days": max_date_gap,
        "big_date_gaps": big_gaps,
        "trust_score": round(score, 1),
        "grade": grade,
        "notes": notes,
    }
