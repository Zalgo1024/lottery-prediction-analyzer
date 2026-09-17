"""
discovery/info_theory.py —— P2-4 传递熵 / 条件互信息量化（路线图 L2）

要回答的问题："历史开奖对下一期到底传递了多少信息？" → 实测 ≈ 0 bits。
以后再有人说"冷热号/走势有用"，拿这个数字回应。

方法（诚实口径，与 credibility 一致，不引重依赖）：
- 离散插件估计（bincount 计数，nats）。
- 量 1  MI1  = I(X_{t-1} ; X_t)            一阶：上一期对本期。
- 量 2  CMIk = I(X_{t-k} ; X_t | X_{t-1})  k=2..K：给定上一期后，更早过去
        是否携带"附加"信息（高阶/长程依赖）。这就是(自)传递熵的离散版本。
- 零分布：对时间顺序做 B 次随机置换（保留边际、破坏一切时序依赖），
  观测统计量在零分布中的位置 = 置换 p 值；跨 k 用 BH-FDR。
- ⚠️ 七星彩第 7 位只用 2020-10-11 起现行段（旧 0-9 → 新 0-14 边际不同，
  段混合会在置换下制造伪"可预测性"）——与 credibility/data_quality 同口径。

判定口径（诚实）：
- zone 级结论 = p < 0.05 且 BH q < 0.05 才算"检测到时序依赖"；
  检测到时先按"数据/规则异常"处理（开奖机制理论上不依赖历史），
  而不是宣称"可预测"——除非跨支柱复现。
"""
import logging
from datetime import date
from pathlib import Path

import numpy as np

from data.schema import record_zone_numbers

logger = logging.getLogger(__name__)

# 七星彩第7位规则变更日（后区 0-9 → 0-14），与 credibility/data_quality.py 一致
_QXC_CUTOFF = date(2020, 10, 11)


def _bh_fdr(pvals: np.ndarray, alpha: float = 0.05) -> np.ndarray:
    """Benjamini-Hochberg 校正（复刻 train/search.py::_bh_fdr，避免跨层 import）"""
    p = np.asarray(pvals, dtype=float)
    n = len(p)
    if n == 0:
        return np.array([], dtype=float)
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(1, n + 1))
    qs = np.empty(n)
    qs[order] = np.minimum.accumulate(ranked[::-1])[::-1]
    return np.minimum(qs, 1.0)


# ---------------------------------------------------------------- 插件估计
def _counts(x: np.ndarray, kx: int) -> np.ndarray:
    return np.bincount(x, minlength=kx)[:kx]


def _counts2(x: np.ndarray, y: np.ndarray, kx: int, ky: int) -> np.ndarray:
    return np.bincount(x * ky + y, minlength=kx * ky)[:kx * ky].reshape(kx, ky)


def _counts3(x, y, z, kx, ky, kz) -> np.ndarray:
    idx = (x * ky + y) * kz + z
    return np.bincount(idx, minlength=kx * ky * kz)[:kx * ky * kz] \
        .reshape(kx, ky, kz)


def _mi_from_counts(c: np.ndarray, total: int) -> float:
    """I(X;Y) = Σ p(xy) log p(xy)/(p(x)p(y))，nats。c: (kx,ky)"""
    px = c.sum(axis=1)
    py = c.sum(axis=0)
    out = 0.0
    for ix, iy in zip(*np.nonzero(c)):
        pxy = c[ix, iy] / total
        out += pxy * np.log(pxy / max((px[ix] / total) * (py[iy] / total), 1e-300))
    return out


def _cmi(x: np.ndarray, y: np.ndarray, z: np.ndarray,
         kx: int, ky: int, kz: int) -> float:
    """I(X;Y|Z) nats = Σ p(xyz) log [p(xyz)p(z)/(p(xz)p(yz))]"""
    total = len(x)
    c3 = _counts3(x, y, z, kx, ky, kz)
    c_xz = c3.sum(axis=1)      # (kx,kz)
    c_yz = c3.sum(axis=2)      # (ky,kz)? 注意轴：c3[ix,iy,iz] → sum over iy = xz
    # c3[ix,iy,iz]: xz 边际 = sum over y → c3.sum(axis=1) (kx,kz) ✓
    # yz 边际 = sum over x → c3.sum(axis=0) (ky,kz) ✓
    c_yz = c3.sum(axis=0)
    pz = c3.sum(axis=(0, 1))
    out = 0.0
    eps = 1e-300
    for ix, iy, iz in zip(*np.nonzero(c3)):
        c = c3[ix, iy, iz]
        num = (c / total) * (pz[iz] / total)
        den = (c_xz[ix, iz] / total) * (c_yz[iy, iz] / total)
        out += (c / total) * np.log(num / max(den, eps))
    return out


# ---------------------------------------------------------------- 序列统计
def _seq_stats(seq: np.ndarray, kmax: int, size: int) -> list:
    """观测统计量：[MI1, CMI_2..CMI_kmax]（序列 0..size-1，时间升序）。

    公共窗口对齐：x_t = seq[kmax:]；对 lag L ∈ 1..kmax，
    x_lagL = seq[kmax-L : -L]（长度均 N-kmax，元素一一对应）。
    """
    N = len(seq)
    x_t = seq[kmax:]
    x_lag1 = seq[kmax - 1:-1]
    out = [_mi_from_counts(_counts2(x_t, x_lag1, size, size), len(x_t))]
    for lag in range(2, kmax + 1):
        x_lagk = seq[kmax - lag:-lag]
        out.append(_cmi(x_t, x_lagk, x_lag1, size, size, size))
    return out


def analyze_seq(seq, size: int, kmax: int = 5, B: int = 200,
                seed: int = 0) -> dict:
    """对单一离散序列做 MI1 + CMI_{2..kmax} 置换检验。

    seq: 0..size-1 的整数数组（时间升序）。返回每 lag 的观测值、p、BH q。
    """
    seq = np.asarray(seq, dtype=int)
    if len(seq) < size * 20 or len(seq) < 200:
        return {"error": f"样本不足 {len(seq)}"}
    obs = _seq_stats(seq, kmax, size)
    rng = np.random.default_rng(seed)
    n_lag = len(obs)
    perm = np.zeros((B, n_lag))
    for b in range(B):
        s = seq[rng.permutation(len(seq))]
        perm[b] = _seq_stats(s, kmax, size)
    # p = (1 + #{perm >= obs}) / (1+B)（右尾：信息越大越异常）
    pvals = (1.0 + (perm >= np.array(obs)).sum(axis=0)) / (1.0 + B)
    qs = _bh_fdr(np.asarray(pvals))
    labels = ["MI1(上期→本期)"] + [f"CMI lag{lag}(给定上期)" for lag in range(2, kmax + 1)]
    # 参考：零分布 99.9% 分位（效应量参照）
    hi = np.quantile(perm, 0.999, axis=0)
    return {
        "样本数": int(len(seq)),
        "检验": [
            {"量": labels[i], "nats": round(float(obs[i]), 6),
             "零分布p99.9": round(float(hi[i]), 6),
             "p": round(float(pvals[i]), 4), "q(BH)": round(float(qs[i]), 4),
             "显著": bool(pvals[i] < 0.05 and qs[i] < 0.05)}
            for i in range(n_lag)
        ],
    }


# ---------------------------------------------------------------- 彩种级报告
def _zone_series(records, zone):
    """该分区逐期主值序列（时间升序）。七星彩第7位用现行段。"""
    recs = records
    if zone.max == 14 and records and hasattr(records[0], "开奖日期"):
        recs = [r for r in records if r.开奖日期 and r.开奖日期 >= _QXC_CUTOFF]
    out = []
    for r in recs:
        v = list(record_zone_numbers(r, zone))
        if v:
            out.append(v[0])
    return out, len(recs)


def lottery_report(name: str, kmax: int = 5, B: int = 200) -> dict:
    """6 彩种任一的传递熵报告：逐分区 MI1/CMI 置换检验。"""
    from data.loader import load_lottery
    from data.schema import get_schema
    data = load_lottery(name)
    schema = get_schema(name)
    chron = sorted(data.records, key=lambda r: r.期号 or 0)
    zones_out = []
    any_sig = False
    for zone in schema.zones:
        seq, n_used = _zone_series(chron, zone)
        # zone 值域 0..max（数字型 min=0；乐透 min=1 → 减去 min 归零基）
        vals = np.array(seq, dtype=int) - zone.min
        vals = vals[(vals >= 0) & (vals < zone.size)]
        seed = int.from_bytes(zone.name.encode("utf-8")[:4], "little") + 7
        r = analyze_seq(vals, size=zone.size, kmax=kmax, B=B, seed=seed)
        sig = any(t["显著"] for t in r.get("检验", []))
        any_sig = any_sig or sig
        zones_out.append({
            "分区": zone.name, "值域": f"{zone.min}-{zone.max}",
            "期数(用)": n_used, "显著时序依赖": bool(sig), **r,
        })
    return {
        "彩种": name,
        "期数(总)": len(chron),
        "滞后上限": kmax, "置换次数": B,
        "结论": ("⚠️ 检出分区内时序依赖——先按数据/规则异常排查（开奖机制理论上与历史无关）"
                if any_sig else
                "历史 → 下一期的信息传递与 0 无显著差异（MI/CMI 置换 p 均不显著）"),
        "分区": zones_out,
    }


def run_all(kmax: int = 5, B: int = 150) -> list:
    """6 彩种批量摘要（最快出对外结论）"""
    names = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]
    return [lottery_report(nm, kmax=kmax, B=B) for nm in names]


if __name__ == "__main__":
    import json, sys
    logging.basicConfig(level=logging.INFO)
    args = sys.argv[1:]
    if args:
        for nm in args:
            print(json.dumps(lottery_report(nm), ensure_ascii=False, indent=1))
    else:
        for rep in run_all():
            # 精简打印：结论 + 每分区 max p/q
            print(f"[{rep['彩种']}] {rep['结论']}")
            for z in rep["分区"]:
                sigs = [t for t in z["检验"] if t["显著"]]
                worst = max((t["q(BH)"] for t in z["检验"]), default=1.0)
                mark = "★" if z["显著时序依赖"] else " "
                print(f"  {mark} {z['分区']}: 最小 q={worst} "
                      f"{('显著项:' + str([t['量'] for t in sigs])) if sigs else ''}")
