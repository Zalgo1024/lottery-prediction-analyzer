"""
credibility/randomness_audit.py —— P2-1 随机性审计（路线图 L2，NIST SP 800-22 思路）

要回答的问题："开奖数据干净吗？有机制异常吗？"——把每个彩种的开奖序列当
随机数发生器输出做权威审计。与 data_quality.py 的关系：data_quality 面向
"可信度评分/闸门联动"，本模块是独立的 NIST 风格逐检验报告（频率/游程/
最长游程/序列/累积和/块内频率），六彩种各出一份，写 discovery/randomness_audit.json。

口径（与 credibility 一致，防止大样本过度拒绝 / 规则段混用伪异常）：
- 数字型位（每期一个取值，iid 均匀零假设）：全套时序检验适用。
- 乐透型区（每期不放回抽 c 个）：只审计边际均匀（时序类零假设不成立——
  区内不放回抽取使展开流非 iid，硬套会假告警）。
- 七星彩第 7 位只用 2020-10-11 起现行段（0-9→0-14 规则变更）：
  **P2-1 定位结论（校准用例）**：data_quality 曾对全史混用报出该区
  "相邻自相关 p≈1e-10 实质异常"→ 实测前段(0-9)同号率 0.101≈1/10、
  后段(0-14) 0.074≈1/15 各自干净，异常纯为"前段基准率 1/10 被拿去比
  后段期望 1/15"的口径伪影（已修 data_quality.py 统一按现代段过滤）。
  即：七星彩第 7 位所谓"异常"是数据口径问题，不是机制/数据真异常。
- p 值口径：大样本均匀性 χ² 用 MC 公平零分布（渐近 χ² 会过度拒绝）；
  最长游程/序列/累积和同样用 iid 置换零分布（B 次模拟）；跨检验+分区
  BH-FDR 校正；实质显著 = q≤0.05 且过效应量门（均匀性 V≥0.10 等，
  与项目其余裁判同口径，压制扫描/大样本乐观偏置）。

检验清单（每数字型分区）——NIST 名 + 适配说明：
  T1 频率(Monobit→均匀χ² MC)     边际是否均匀（机制层面最硬的检验）
  T2 游程(Runs, Wald-Wolfowitz)   序列是否"聚簇/震荡"（中位二值化）
  T3 最长游程(Longest Run)        同值连号的长度是否异常（粘滞性）
  T4 序列(Serial m=2)             相邻有序对是否均匀（短程依赖）
  T5 累积和(Cumulative Sums)      随机游走最大偏移（局部漂移/趋势）
  T6 块内频率(Block Frequency)    分块边际是否漂移（分段不稳定）
乐透型区只做 T1。全部检验以统一符号流在零假设（公平 iid）下的 MC 零分布定 p。
"""
import json
import logging
from datetime import date
from pathlib import Path

import numpy as np
from scipy import stats

from data.loader import load_lottery
from data.schema import get_schema, record_zone_numbers

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_JSON = BASE_DIR / "discovery" / "randomness_audit.json"

# 七星彩第7位规则变更日（后区 0-9 → 0-14），与 credibility/data_quality.py 一致
_QXC_CUTOFF = date(2020, 10, 11)

_LOTTERIES = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]


def _bh_fdr(pvals, alpha: float = 0.05):
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return np.array([], dtype=float)
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(1, n + 1))
    qs = np.empty(n)
    qs[order] = np.minimum.accumulate(ranked[::-1])[::-1]
    return np.minimum(qs, 1.0)


# ---------------------------------------------------------------- 基础工具
def _base_records(records, zone):
    """该分区审计用记录：七星彩第7位只用 2020-10-11 起现行段。"""
    if zone.max == 14:
        return [r for r in records
                if getattr(r, "开奖日期", None) and r.开奖日期 >= _QXC_CUTOFF]
    return records


def _symbol_stream(records, zone):
    """逐期主符号流（数字型位，每期一个取值）。返回 np 数组(0..K-1)。"""
    out = []
    for rec in records:
        v = list(record_zone_numbers(rec, zone))
        if v:
            out.append(v[0] - zone.min)
    return np.asarray(out, dtype=int)


def _mc_p(obs, sims):
    """单侧 p = (1 + #{sim ≥ obs}) / (1+B)；sims: (B,)"""
    return float((1.0 + int(np.sum(sims >= obs))) / (1.0 + len(sims)))


def _chi2_uniform_p(counts, total, K, B: int, seed: int) -> tuple:
    """均匀性 χ² 的 MC p + Cramér V + iid 99% 分位（大样本防过度拒绝）。"""
    expected = total / K
    obs = float(np.sum((counts - expected) ** 2 / expected))
    v = float(np.sqrt(obs / (total * (K - 1)))) if total > K else 0.0
    rng = np.random.default_rng(seed)
    sim = rng.integers(0, K, size=(B, total))
    sim_counts = np.zeros((B, K), dtype=np.int64)
    for cat in range(K):
        sim_counts[:, cat] = np.count_nonzero(sim == cat, axis=1)
    sim_chi2 = np.sum((sim_counts - expected) ** 2 / expected, axis=1)
    return _mc_p(obs, sim_chi2), v, float(np.quantile(sim_chi2, 0.99))


# ---------------------------------------------------------------- 检验实现
def _test_frequency(seq, K, B: int) -> dict:
    """T1 频率：边际均匀（统一零假设 p_k = 1/K）。"""
    counts = np.bincount(seq, minlength=K)[:K]
    total = int(counts.sum())
    if total <= K:
        return None
    p, v, hi = _chi2_uniform_p(counts, total, K, B=B, seed=1000 + K)
    return {
        "检验": "T1频率(均匀χ² MC)", "统计量": "V=" + f"{v:.4f}",
        "p值": round(p, 4), "MC_B": B,
        "效应门": bool(v >= 0.10), "_p": p,
    }


def _ww_runs_z(binary) -> tuple:
    """Wald-Wolfowitz 游程检验（scipy<1.16 无 runs_test，手写标准式）。
    返回 (z, p)；退化序列返回 (None, 1.0)。"""
    n = len(binary)
    n1 = int(binary.sum())
    n0 = n - n1
    if n0 == 0 or n1 == 0 or n < 2:
        return None, 1.0
    r = int(1 + np.sum(binary[1:] != binary[:-1]))
    exp_r = 2.0 * n0 * n1 / n + 1.0
    var_r = 2.0 * n0 * n1 * (2.0 * n0 * n1 - n) / (n * n * (n - 1.0))
    if var_r <= 0:
        return None, 1.0
    z = (r - exp_r) / np.sqrt(var_r)
    p = float(2.0 * (1.0 - stats.norm.cdf(abs(z))))
    return float(z), p


def _test_runs(seq) -> dict:
    """T2 游程（Wald-Wolfowitz，中位二值化；|z|≥3 视为实质）。"""
    if len(seq) < 30:
        return None
    med = np.median(seq)
    binary = (seq >= med).astype(int)
    z, p = _ww_runs_z(binary)
    if z is None:
        return None
    return {"检验": "T2游程", "统计量": f"z={z:.2f}", "p值": round(p, 4),
            "效应门": bool(abs(z) >= 3.0), "_p": p}


def _longest_same_run(seq):
    """同值最长连号长度（任意符号）。"""
    if len(seq) == 0:
        return 0
    best = cur = 1
    for i in range(1, len(seq)):
        cur = cur + 1 if seq[i] == seq[i - 1] else 1
        if cur > best:
            best = cur
    return best


def _test_longest_run(seq, K, B: int, rng) -> dict:
    """T3 最长同值游程：MC 零分布定 p + iid 99% 分位门。"""
    n = len(seq)
    obs = _longest_same_run(seq)
    sims = np.empty(B)
    for b in range(B):
        s = rng.integers(0, K, size=n)
        sims[b] = _longest_same_run(s)
    p = _mc_p(obs, sims)
    hi = float(np.quantile(sims, 0.99))
    return {"检验": "T3最长游程", "统计量": f"obs={obs}",
            "p值": round(p, 4), "MC_B": B,
            "效应门": bool(obs >= hi and p < 0.05), "_p": p}


def _serial_pair_chi2(seq, K, B: int, rng) -> dict:
    """T4 序列（Serial m=2）：相邻有序对频率 vs 均匀 1/K²（MC）。"""
    n = len(seq)
    if n < 30:
        return None
    idx = seq[:-1] * K + seq[1:]
    counts = np.bincount(idx, minlength=K * K)[:K * K]
    total = int(counts.sum())
    expected = total / (K * K)
    if expected <= 1.0:
        return None
    obs = float(np.sum((counts - expected) ** 2 / expected))
    sims = np.empty(B)
    for b in range(B):
        s = rng.integers(0, K, size=n)
        c = np.bincount(s[:-1] * K + s[1:], minlength=K * K)[:K * K]
        sims[b] = float(np.sum((c - expected) ** 2 / expected))
    p = _mc_p(obs, sims)
    hi = float(np.quantile(sims, 0.99))
    return {"检验": "T4序列(相邻对)", "统计量": f"χ²={obs:.1f}",
            "p值": round(p, 4), "MC_B": B,
            "效应门": bool(obs >= hi and p < 0.05), "_p": p}


def _test_cumsum(seq, K, B: int, rng) -> dict:
    """T5 累积和：中位二值化 ±1 随机游走 max|partial sum|（MC）。

    ⚠️ 零假设必须与观测同构：先抽 iid 符号再按"该流自己的中位"二值化——
    直接对 ±1 自由硬币游走取 max 会把平衡桥式游走（中位二值化保证正负
    计数近似相等、max|S| 系统性偏大）误判为显著（曾致 10+ 分区全撞最小
    p，纯随机数据上假告警，P2-1 实测校准发现并修复）。
    """
    n = len(seq)
    if n < 30:
        return None
    med = np.median(seq)
    walk = np.where(seq >= med, 1.0, -1.0)
    obs = float(np.abs(np.cumsum(walk)).max())
    sims = np.empty(B)
    for b in range(B):
        s = rng.integers(0, K, size=n)
        m2 = np.median(s)
        w = np.where(s >= m2, 1.0, -1.0)
        sims[b] = float(np.abs(np.cumsum(w)).max())
    p = _mc_p(obs, sims)
    hi = float(np.quantile(sims, 0.99))
    return {"检验": "T5累积和", "统计量": f"max|S|={obs:.0f}",
            "p值": round(p, 4), "MC_B": B,
            "效应门": bool(obs >= hi and p < 0.05), "_p": p}


def _test_block_freq(seq, K, block: int) -> dict:
    """T6 块内频率：每 block 期一块的均匀 χ² 之和（分段漂移，渐近 p）。"""
    n = len(seq)
    if n < block * 2:
        return None
    nblk = n // block
    obs = 0.0
    for b in range(nblk):
        chunk = seq[b * block:(b + 1) * block]
        counts = np.bincount(chunk, minlength=K)[:K]
        exp = block / K
        obs += float(np.sum((counts - exp) ** 2 / exp))
    df = nblk * (K - 1)
    p = float(1.0 - stats.chi2.cdf(obs, df))
    # 效应：平均每块 χ² 相对自由度期望的超出比
    rel = obs / df if df > 0 else 0.0
    return {"检验": "T6块内频率", "统计量": f"χ²={obs:.1f}(df={df})",
            "p值": round(p, 4),
            "效应门": bool(p < 0.05 and rel > 1.5), "_p": p}


# ---------------------------------------------------------------- 彩种级审计
def audit_zone(zone, records, B: int = 200, block: int = 500,
               seed: int = 0) -> dict:
    """单分区审计：返回该区检验明细（数字型全套 / 乐透型仅 T1）。"""
    base = _base_records(records, zone)
    n_used = len(base)
    K = zone.size
    rng = np.random.default_rng(seed + zone.min * 100)
    out = {"分区": zone.name, "值域": f"{zone.min}-{zone.max}",
           "期数(用)": n_used}
    seq = _symbol_stream(base, zone)
    if len(seq) < 30:
        out["注"] = "样本不足 30 期，跳过检验"
        return out

    if zone.ordered:   # 数字型位：iid 均匀 → 全套时序检验
        tests = [t for t in [
            _test_frequency(seq, K, B),
            _test_runs(seq),
            _test_longest_run(seq, K, B, rng),
            _serial_pair_chi2(seq, K, B, rng),
            _test_cumsum(seq, K, B, rng),
            _test_block_freq(seq, K, block),
        ] if t is not None]
    else:              # 乐透型区：每期不放回抽 c 个 → 只审计边际（含指示计数）
        draws = [np.asarray(list(record_zone_numbers(r, zone)), dtype=int)
                 for r in base]
        n_used = len(draws)
        inc = np.zeros((n_used, K), dtype=np.int64)
        for i, dv in enumerate(draws):
            inc[i, dv[dv >= zone.min] - zone.min] = 1
        colsum = inc.sum(axis=0)             # 每号出现总次数
        exp = n_used * zone.choose / float(K)
        obs_chi2 = float(np.sum((colsum - exp) ** 2 / exp))
        # MC 零分布：每期真不放回抽 c 个（保持号间负相关，二项近似会偏）
        sim_chi2 = np.empty(B)
        rows = np.arange(n_used)
        for b in range(B):
            order = np.argsort(rng.random((n_used, K)), axis=1)[:, :zone.choose]
            sm = np.zeros((n_used, K), dtype=np.int64)
            sm[rows[:, None], order] = 1
            cs = sm.sum(axis=0)
            sim_chi2[b] = float(np.sum((cs - exp) ** 2 / exp))
        p = _mc_p(obs_chi2, sim_chi2)
        total = int(colsum.sum())
        v = float(np.sqrt(obs_chi2 / (total * (K - 1)))) if total > K else 0.0
        tests = [{
            "检验": "T1频率(含指示均匀MC)", "统计量": f"V={v:.4f}",
            "p值": round(p, 4), "MC_B": B,
            "效应门": bool(v >= 0.10), "_p": p,
            "注": "乐透型区内不放回抽取，时序类检验零假设不成立，仅审计边际",
        }]
    out["检验"] = tests
    return out


def audit_lottery(name: str, B: int = 200, block: int = 500,
                  write_json: bool = False) -> dict:
    """6 彩种任一：逐分区 NIST 风格审计 + BH 校正 + 实质判定。"""
    data = load_lottery(name)
    schema = get_schema(name)
    chron = sorted(data.records, key=lambda r: r.期号 or 0)
    zones_out = []
    p_rows, row_ids = [], []
    for zone in schema.zones:
        za = audit_zone(zone, chron, B=B, block=block, seed=0)
        for t in za.get("检验", []):
            p_rows.append(t.pop("_p"))
            row_ids.append((za["分区"], t["检验"]))
        zones_out.append(za)
    if p_rows:
        qs = _bh_fdr(np.asarray(p_rows))
        for (zn, tn), q in zip(row_ids, qs):
            for za in zones_out:
                if za.get("分区") != zn:
                    continue
                for t in za.get("检验", []):
                    if t["检验"] == tn:
                        t["q(BH)"] = round(float(q), 4)
                        t["实质显著"] = bool(t.get("效应门", False)
                                             and q <= 0.05)
    flagged = [(za["分区"], t["检验"], t["p值"], t.get("q(BH)"))
               for za in zones_out
               for t in za.get("检验", []) if t.get("实质显著")]
    conclusion = (
        f"⚠️ 检出实质偏离 [{', '.join(f'{z}/{t}(q={q})' for z, t, p, q in flagged)}]"
        f" —— 先按机制/数据口径变化排查（与 P2-2 变点交叉验证）"
        if flagged else
        f"全部分区通过 NIST 风格随机性审计（各检验 q>0.05 或效应量不足）"
        f" → 『{name}开奖数据随机性/完整性良好』获得权威背书"
    )
    return {
        "彩种": name,
        "期数(总)": len(chron),
        "MC_B": B,
        "七星彩第7位口径": ("2020-10-11 起现行段" if name == "七星彩" else None),
        "结论": conclusion,
        "分区": zones_out,
    }


def run_all(B: int = 200, block: int = 500, write_json: bool = False) -> list:
    """6 彩种批量审计，可选写 discovery/randomness_audit.json。"""
    reps = [audit_lottery(nm, B=B, block=block) for nm in _LOTTERIES]
    if write_json:
        payload = {
            "模块": "P2-1 随机性审计（NIST SP 800-22 思路，分类流适配）",
            "更新于": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
            "彩种": reps,
        }
        try:
            OUT_JSON.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"已写 {OUT_JSON}")
        except Exception as e:
            logger.warning(f"写 {OUT_JSON} 失败: {e}")
    return reps


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1:
        for nm in sys.argv[1:]:
            rep = audit_lottery(nm)
            print(f"[{rep['彩种']}] {rep['结论']}")
            for z in rep["分区"]:
                for t in z.get("检验", []):
                    mark = "★" if t.get("实质显著") else " "
                    print(f"  {mark} {z['分区']}/{t['检验']}: p={t['p值']}"
                          f" q={t.get('q(BH)')} {t.get('注', '')}")
    else:
        for rep in run_all():
            print(f"[{rep['彩种']}] {rep['结论']}")
            for z in rep["分区"]:
                for t in z.get("检验", []):
                    mark = "★" if t.get("实质显著") else " "
                    print(f"  {mark} {z['分区']}/{t['检验']}: p={t['p值']}"
                          f" q={t.get('q(BH)')} {t.get('注', '')}")
