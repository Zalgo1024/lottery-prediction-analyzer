"""
ev/rollover.py —— 双色球/大乐透 Rollover-EV 计算器（任务 2.3）

彩票式（双/大）每期独立不可预测，唯一杠杆 = 奖池滚动(Rollover)EV：
- 奖池滚得足够高、且预估头奖不爆 split 时，头奖单注奖金处于高位 → 参与窗口。
- 数据直接读 双色球/大乐透 历史 CSV 现有列（奖池奖金/一等奖注数/总投注额，已验证存在）。

⚠️ 2026-02-01 起双色球新规（cwl.gov.cn 游戏规则核实）：
- 一等奖单注最高封顶 **1000 万**（奖池>15亿特别规定：奖池部分封顶500万 + 当期浮动20%部分封顶500万）。
- 触发封顶：一等奖总奖金超 1 亿 → 单注 = 1亿/中奖注数。
- 注数 1-5 注 → 单注 784万~1000万；>20 注 → 触发 1 亿封顶摊薄。
- 大乐透一等奖基本投注封顶 1000 万（追加 1800 万），MVP 用 1000 万。

模型（MVP，明确近似）：
  总注数 N = 总投注额 / 2（单注 2 元）
  P(一等奖) = 1 / 组合数（双色球 1/17,721,088；大乐透 1/21,425,712）
  期望一等奖注数 λ = N × P(一等奖)（泊松均值）
  头奖单注奖金 ≈ min(1000万, (奖池×0.7 + 当期浮动20%≈销量×0.51×0.2) / λ)
  头奖期望贡献 = P(一等奖) × 头奖单注奖金
  固定小奖贡献 = Σ 固定奖 payout × p（精确组合概率）
  总 EV ≈ -2 + 头奖贡献 + 小奖贡献
  **参与信号 = EV 天花板语义**（2026-09-04 P1-2 修正）：头奖单注估计是否"顶格"
  （≥99.9% 封顶 1000 万）。旧规则「> 历史 P75」有先天缺陷：大乐透历史 P75 已顶格
  → 信号恒 False（死锁）；双色球顶格常态化 → 恒 True（饱和）。新语义下：
  池厚到可让单注拿满封顶（split_margin = 池可分池/(封顶×λ) ≥1）才判"参与窗口"；
  无池/无销售数据时 λ 不可估 → 不参与（诚实）。

不做的事：不预测开奖号码，不承诺中奖率。
"""
import math
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "lottery_data"

# P(一等奖)：双色球 C(33,6)×16；大乐透 C(35,5)×C(12,2)
_P1 = {
    "双色球": 1.0 / (math.comb(33, 6) * 16),
    "大乐透": 1.0 / (math.comb(35, 5) * math.comb(12, 2)),
}

# 一等奖单注封顶（2026 现行规则：1000 万）
_JACKPOT_CAP = 10_000_000
# 一等奖可分配池近似：奖池×系数（MVP 0.7）
_POOL_SHARE = 0.7


def _ssq_fixed():
    """双色球固定奖概率（红球 C(33,6)，蓝球 1/16）"""
    denom = math.comb(33, 6) * 16
    c = math.comb
    return {
        "三等奖": (3000, c(6, 5) * c(27, 1) / denom),                                          # 5+1
        "四等奖": (200, (c(6, 5) * c(27, 1) * 15 + c(6, 4) * c(27, 2)) / denom),               # 5+0, 4+1
        "五等奖": (10, (c(6, 4) * c(27, 2) * 15 + c(6, 3) * c(27, 3)) / denom),               # 4+0, 3+1
        "六等奖": (5, (c(6, 2) * c(27, 4) + c(6, 1) * c(27, 5) + c(27, 6)) / denom),         # 2+1, 1+1, 0+1
    }


def _dlt_fixed():
    """大乐透固定奖概率（2023 新规：前区 C(35,5)，后区 C(12,2)）"""
    denom = math.comb(35, 5) * math.comb(12, 2)
    c = math.comb
    f5, f4, f3, f2, f1, f0 = (c(5, 5), c(5, 4) * c(30, 1), c(5, 3) * c(30, 2),
                              c(5, 2) * c(30, 3), c(5, 1) * c(30, 4), c(30, 5))
    b2, b1, b0 = c(2, 2), c(2, 1) * c(10, 1), c(10, 2)
    return {
        "三等奖": (10000, f5 * b0 / denom),                       # 5+0
        "四等奖": (3000, f4 * b2 / denom),                        # 4+2
        "五等奖": (300, f4 * b1 / denom),                         # 4+1
        "六等奖": (200, f3 * b2 / denom),                         # 3+2
        "七等奖": (100, f4 * b0 / denom),                         # 4+0
        "八等奖": (15, (f3 * b1 + f2 * b2) / denom),              # 3+1, 2+2
        "九等奖": (5, (f3 * b0 + f2 * b1 + f1 * b2 + f0 * b2) / denom),  # 3+0, 2+1, 1+2, 0+2
    }


# 固定小奖（名义赔付，概率为组合精确值）
_FIXED_PRIZES = {
    "双色球": _ssq_fixed(),
    "大乐透": _dlt_fixed(),
}

# 一等奖分配系数（奖池×系数 进入头奖池，MVP 近似）


def _load_history(name: str) -> pd.DataFrame:
    """读历史 CSV（含 奖池奖金/一等奖注数/总投注额 列），按 期号 降序"""
    path = DATA_DIR / f"{name}历史数据_cleaned.csv"
    if not path.exists():
        path = DATA_DIR / f"{name}历史数据.csv"
    df = pd.read_csv(path, encoding="utf-8-sig", dtype={"期号": str})
    df = df.sort_values("期号", key=lambda s: s.str.len().mul(100000) + s.astype(int), ascending=False)
    return df


def _head_payout_history(name: str) -> pd.Series:
    """历史实际一等奖单注奖金序列（清洗：去 0/NaN）"""
    df = _load_history(name)
    if "一等奖奖金" not in df.columns:
        return pd.Series(dtype=float)
    s = pd.to_numeric(df["一等奖奖金"], errors="coerce")
    s = s[(s > 0) & s.notna()]
    return s


def fixed_prize_ev(name: str) -> float:
    """固定小奖单注期望贡献（元）"""
    return sum(payout * p for payout, p in _FIXED_PRIZES[name].values())


def rollover_ev(name: str, issue: str = None, pool_share: float = _POOL_SHARE,
                cap: float = _JACKPOT_CAP) -> dict:
    """
    Rollover-EV 计算。
    issue: 期号（默认最新一期）。
    返回：{期号, 奖池, 总投注额, 总注数, P(一等), 期望一等注数λ,
          头奖单注奖金估计, 头奖期望贡献, 固定奖贡献, 总EV, 参与信号, 备注}
    参与信号 = EV 天花板语义：头奖单注估计顶格（≥99.9% 封顶）才算参与窗口
    （旧 P75 规则：大乐透死锁恒 False / 双色球饱和恒 True，见模块 docstring）。
    """
    df = _load_history(name)
    if issue is None:
        row = df.iloc[0]
    else:
        hit = df[df["期号"] == str(issue)]
        if hit.empty:
            return {"error": f"未找到 {name} 期号 {issue}"}
        row = hit.iloc[0]

    pool = float(row["奖池奖金"]) if row.get("奖池奖金") == row.get("奖池奖金") else 0.0
    sales = float(row["总投注额"]) if row.get("总投注额") == row.get("总投注额") else 0.0
    n_tickets = sales / 2.0 if sales else 0.0
    p1 = _P1[name]
    lam = n_tickets * p1  # 期望一等奖注数

    # 头奖可分配池 ≈ 奖池×share + 当期浮动20%（MVP 用奖池×share）
    head_pool = pool * pool_share if pool > 0 else 0.0
    # 头奖单注奖金估计：总池 / 期望注数，上限 1000 万
    jackpot_per_win = min(cap, head_pool / max(lam, 1e-9)) if head_pool > 0 else 0.0
    head_contrib = p1 * jackpot_per_win
    fixed_contrib = fixed_prize_ev(name)
    total_ev = -2.0 + head_contrib + fixed_contrib

    # 参与信号：EV 天花板语义（2026-09-04 P1-2 修正 P75 死锁/饱和缺陷）
    # split_margin = 池可分池 / (封顶 × λ)：池相对期望注数的"厚度"（>1 → 可顶格）
    split_margin = (head_pool / cap) / max(lam, 1e-9) if head_pool > 0 else 0.0
    at_cap = jackpot_per_win >= cap * 0.999
    # λ 不可估（无销售数据）时顶格判定无意义 → 诚实不参与（防 λ=0 恒顶格假象）
    play_signal = at_cap and lam > 0
    baseline = (f"EV天花板判定(split_margin={split_margin:.2f})" if head_pool > 0
                else "无池/销售数据")

    note = []
    if not pool:
        note.append("无奖池数据")
    if lam <= 0:
        note.append("无销售数据 → λ 不可估，按名义EV")
    split_hit = head_pool > 0 and (head_pool / max(lam, 1e-9)) < cap
    if split_hit:
        note.append(f"λ={lam:.1f} 总池/λ={head_pool/max(lam,1e-9):,.0f}元 < {cap:,}元 → 头奖被多注分摊(爆split)")
    if play_signal:
        note.append(f"EV天花板窗口（{baseline}），可考虑小额参与（仍为负EV总期望，仅娱乐级）")
    else:
        note.append(f"未到EV天花板窗口（{baseline}），不参与")

    return {
        "彩种": name,
        "期号": str(row["期号"]),
        "奖池": round(pool, 2),
        "总投注额": round(sales, 2),
        "总注数": int(n_tickets),
        "P(一等奖)": f"{p1:.3e}",
        "期望一等奖注数λ": round(lam, 4),
        "头奖单注奖金估计": round(jackpot_per_win, 2),
        "头奖期望贡献": round(head_contrib, 6),
        "固定奖贡献": round(fixed_contrib, 6),
        "总EV(每注)": round(total_ev, 6),
        "参与信号": play_signal,
        "备注": "；".join(note) or "-",
    }


def rollover_history(name: str, n: int = 30) -> list:
    """最近 n 期的 Rollover-EV 序列（看奖池滚动趋势；参与信号=EV天花板语义）"""
    df = _load_history(name)
    out = []
    for _, row in df.head(n).iterrows():
        pool = float(row["奖池奖金"]) if row.get("奖池奖金") == row.get("奖池奖金") else 0.0
        sales = float(row["总投注额"]) if row.get("总投注额") == row.get("总投注额") else 0.0
        n_t = sales / 2.0
        lam = n_t * _P1[name]
        jpw = min(_JACKPOT_CAP, pool * _POOL_SHARE / max(lam, 1e-9)) if pool > 0 else 0.0
        out.append({
            "期号": str(row["期号"]),
            "奖池": round(pool, 0),
            "头奖单注估计": round(jpw, 0),
            "参与信号": bool(jpw >= _JACKPOT_CAP * 0.999 and lam > 0),
        })
    return out


def cap_ref(name: str) -> float:
    return _JACKPOT_CAP * 0.8


if __name__ == "__main__":
    import json, sys
    for name in (sys.argv[1:] or ["双色球", "大乐透"]):
        r = rollover_ev(name)
        print(json.dumps(r, ensure_ascii=False, indent=1))
