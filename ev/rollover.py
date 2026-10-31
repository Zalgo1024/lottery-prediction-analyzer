"""
ev/rollover.py —— 双色球/大乐透 Rollover-EV 计算器（任务 2.3）

彩票式（双/大）在**本项目已测维度上未检出可利用的时序依赖**，因此不作号码预测；
唯一杠杆 = 奖池滚动(Rollover)EV：
（措辞说明 2026-10-03：原文用的是「不可能」式表述——把「没测到」说成了
「世界不可能」，属本体论断言，已由 P3 叙事解绑移除。开奖是否系统性偏离等可能，
由 `ev/mechanism/` 四轨测量负责，见 `python cli.py gap`。
本模块的杠杆结论不依赖那句断言，故可直接换成本表述。）
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
from typing import Any, Dict, Tuple

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


# 红蓝型几何：(外层池, 外层选, 内层池, 内层选) —— 与 ev/profit/rules 格表配套
_RB_GEOM = {
    "双色球": (33, 6, 16, 1),
    "大乐透": (35, 5, 12, 2),
}


def fixed_prizes_for(name: str, issue: Any = None) -> Dict[str, Tuple[float, float]]:
    """固定小奖 {奖级: (单注金额, 组合精确概率)}，**由 rules_for 按期生成**。

    取代旧的手写 `_ssq_fixed` / `_dlt_fixed`（后者是九级硬编码副本，大乐透
    2026-02 起奖级与金额全错）。按期传 issue ⇒ 用当期规则版本（大乐透五段
    8/6/9/7 级）；issue=None ⇒ 现行版本。浮动奖级（大乐透一/二/三等在
    2014 及以前为浮动）不进本表。
    """
    if name not in _RB_GEOM:
        raise KeyError(f"{name} 非红蓝乐透型，无固定小奖表")
    ot, op, it, ip = _RB_GEOM[name]
    from ev.profit.rules import rules_for

    rules = rules_for(name, issue)
    denom = math.comb(ot, op) * math.comb(it, ip)
    out: Dict[str, Tuple[float, float]] = {}
    for g, r in rules.grades.items():
        if r.kind != "fixed" or r.fixed is None:
            continue
        cells = 0
        for (h, j), gg in rules.grade_table.items():
            if gg != g:
                continue
            cells += (math.comb(op, h) * math.comb(ot - op, op - h)
                      * rules.inner_multiplicity[j])
        out[g] = (float(r.fixed), cells / denom)
    return out


def __getattr__(name: str) -> Any:
    """PEP 562 惰性属性：只服务于 `_FIXED_PRIZES`（现行版本固定小奖）。

    ★★★ 为什么不能写成模块级常量
    `_FIXED_PRIZES = {n: fixed_prizes_for(n) for n in _RB_GEOM}` 会在**导入本模块期间**
    执行 `fixed_prizes_for()` 内部的 `from ev.profit.rules import rules_for`，
    而 `ev/__init__.py` 又 eager import 了 `.rollover` ⇒ 形成导入环：

        ev/__init__ → .rollover → ev.profit.rules → config → ev.budget_gate → ev

    只要有任何一条 import 链从 `ev.profit.rules` 或 `ev.payout` 侧进入（例如
    `web/routes/settings.py` 的 `from ev.payout import grade_probabilities`），
    就会抛 `_frozen_importlib._DeadlockError: deadlock detected by
    _ModuleLock('ev.profit.rules')`。

    后果不只是 500：异常逃出请求线程 ⇒ 应用崩溃 ⇒ supervisor 重启 ⇒
    启动自愈发现 pending 注数 ≠ 滑轨 ⇒ **按新注数重生成预测**。
    2026-11-01 01:01 就是这样在用户表态之前花掉了 3,200 元；
    日志显示同类崩溃从 2026-10-25 起已发生 5 次。

    惰性化后，导入本模块**不再触碰 `ev.profit.rules`**，环断掉；
    `from ev.rollover import _FIXED_PRIZES` 仍可用（首次访问时现算并写回模块
    `__dict__`，后续不再经过本函数）。
    """
    if name == "_FIXED_PRIZES":
        value = {n: fixed_prizes_for(n) for n in _RB_GEOM}
        globals()["_FIXED_PRIZES"] = value      # 缓存，后续查找直接命中 __dict__
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def fixed_prizes_robust(name: str, issue: Any) -> Dict[str, Tuple[float, float]]:
    """fixed_prizes_for 的容错包装：期号不可解析/早于最早版本 → 现行版本。"""
    try:
        return fixed_prizes_for(name, issue)
    except ValueError:                          # IssueKeyError / RuleVersionError
        return fixed_prizes_for(name)


def fixed_prize_ev(name: str, issue: Any = None) -> float:
    """固定小奖单注期望贡献（元）。issue 给定 ⇒ 按该期规则版本结算。"""
    return sum(payout * p for payout, p in fixed_prizes_for(name, issue).values())


def rules_version_of(name: str, issue: Any) -> str:
    """该期生效的规则版本名（仅用于口径标注，不参与计算）。"""
    from ev.profit.rules import rules_for
    return rules_for(name, issue).rule_version


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


def _mu_cold_state(name: str, q_cold: float = 0.1) -> tuple:
    """
    冷门口径形态因子（策略升级 A）：exp(β·f(q10 分位组合))。

    返回 (因子 | None, 口径说明)。流行度模型未 Go / 不可用 → None + 回退原因
    （显式降级不许静默；回退口径 = λ 均摊，号码无关）。
    μ_冷门 = λ × 因子（λ = N×P(一等) 为全市场平均；冷门组合因子 <1 → 分摊更少）。
    """
    try:
        import numpy as np

        from ev.popularity import cold_hot_combo, features_matrix, get_popularity_model
        params = get_popularity_model(name)
        if not params.get("go"):
            return None, "流行度模型未达 Go 门槛 → 回退 λ 均摊口径（号码无关）"
        ch = cold_hot_combo(name, q_cold)
        if ch is None:
            return None, "冷/热参考组合不可得 → 回退 λ 均摊口径"
        f = features_matrix(np.array([ch[0]], dtype=np.int16))[0]
        z = (f - params["m_"]) / params["s_"]
        factor = float(np.exp(float(z @ params["beta"])))
        if not (factor > 0):
            return None, "冷门形态因子非正 → 回退 λ 均摊口径"
        return factor, f"μ_冷门(q{max(1, int(round(q_cold * 100)))}分位组合)"
    except Exception as e:
        return None, f"流行度模型不可用({type(e).__name__}) → 回退 λ 均摊口径"


def rollover_ev(name: str, issue: str = None, pool_share: float = _POOL_SHARE,
                cap: float = _JACKPOT_CAP) -> dict:
    """
    Rollover-EV 计算。
    issue: 期号（默认最新一期）。
    返回：{期号, 奖池, 总投注额, 总注数, P(一等), 期望一等注数λ,
          头奖单注奖金估计, 头奖期望贡献, 固定奖贡献, 总EV, 参与信号, 备注}
    参与信号 = EV 天花板语义：头奖单注估计顶格（≥99.9% 封顶）才算参与窗口
    （旧 P75 规则：大乐透死锁恒 False / 双色球饱和恒 True，见模块 docstring）。

    冷门口径（策略升级 A）：λ 是「随机号」面对的平均分摊人数；冷门号面对的
    μ_冷门 = λ×exp(β·f(q10组合)) 更低 → 参与门槛更低。既有键保持 λ 口径不变
    （payout.py 兜底与前端语义依赖既有键名），冷门口径全部走新增键。
    """
    if name not in _P1:
        return {"error": f"{name} 非乐透型（双/大），无 rollover 概念"
                         "（固定赔率彩种单注 EV 恒定；七星彩请用 ev.engine.net_ev）"}
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
    # 固定奖按**当期规则版本**结算（大乐透 2026-02 前是九级表，金额全不同）
    issue_actual = str(row["期号"])
    try:
        fixed_prizes = fixed_prizes_for(name, issue_actual)
        fixed_basis = f"规则版本 {rules_version_of(name, issue_actual)}"
    except ValueError:
        fixed_prizes = fixed_prizes_for(name)
        fixed_basis = "现行版本（期号不可解析/越界，显式降级）"
    fixed_contrib = sum(payout * p for payout, p in fixed_prizes.values())
    total_ev = -2.0 + head_contrib + fixed_contrib

    # 参与信号：EV 天花板语义（2026-09-04 P1-2 修正 P75 死锁/饱和缺陷）
    # split_margin = 池可分池 / (封顶 × λ)：池相对期望注数的"厚度"（>1 → 可顶格）
    split_margin = (head_pool / cap) / max(lam, 1e-9) if head_pool > 0 else 0.0
    at_cap = jackpot_per_win >= cap * 0.999
    # λ 不可估（无销售数据）时顶格判定无意义 → 诚实不参与（防 λ=0 恒顶格假象）
    play_signal = at_cap and lam > 0
    baseline = (f"EV天花板判定(split_margin={split_margin:.2f})" if head_pool > 0
                else "无池/销售数据")

    # ---- 冷门口径（策略升级 A）：门槛按冷门号实际面对的分摊人数 μ_冷门 = λ×因子 ----
    cold_factor, cold_basis = _mu_cold_state(name)
    mu_cold = lam * cold_factor if (cold_factor and lam > 0) else None
    jpw_cold = min(cap, head_pool / mu_cold) if (mu_cold and head_pool > 0) else None
    split_margin_cold = ((head_pool / cap) / mu_cold
                         if (mu_cold and head_pool > 0) else None)
    play_cold = (jpw_cold >= cap * 0.999) if jpw_cold is not None else None

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
    if mu_cold is not None:
        note.append(f"冷门口径可用（{cold_basis}：μ_冷={mu_cold:.2f} vs λ={lam:.2f}，门槛更低）")
    else:
        note.append(cold_basis)

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
        "固定奖规则版本": fixed_basis,
        "总EV(每注)": round(total_ev, 6),
        "参与信号": play_signal,
        "分摊口径": (f"{cold_basis}（μ_冷门口径）" if mu_cold is not None
                     else "λ均摊（号码无关）"),
        "μ_冷门": round(mu_cold, 4) if mu_cold is not None else None,
        "头奖单注估计(冷门口径)": round(jpw_cold, 2) if jpw_cold is not None else None,
        "split_margin(冷门)": round(split_margin_cold, 4) if split_margin_cold is not None else None,
        "参与信号(冷门口径)": play_cold,
        "备注": "；".join(note) or "-",
    }


def rollover_history(name: str, n: int = 30) -> list:
    """最近 n 期的 Rollover-EV 序列（看奖池滚动趋势；参与信号=EV天花板语义）。

    冷门口径（策略升级 A）：模型参数/组合只取一次，μ_冷门 逐行算（各期销量不同）；
    既有键（头奖单注估计/参与信号）保持 λ 口径不动。
    """
    df = _load_history(name)
    cold_factor, cold_basis = _mu_cold_state(name)
    out = []
    for _, row in df.head(n).iterrows():
        pool = float(row["奖池奖金"]) if row.get("奖池奖金") == row.get("奖池奖金") else 0.0
        sales = float(row["总投注额"]) if row.get("总投注额") == row.get("总投注额") else 0.0
        n_t = sales / 2.0
        lam = n_t * _P1[name]
        jpw = min(_JACKPOT_CAP, pool * _POOL_SHARE / max(lam, 1e-9)) if pool > 0 else 0.0
        mu_cold = lam * cold_factor if (cold_factor and lam > 0) else None
        jpw_cold = (min(_JACKPOT_CAP, pool * _POOL_SHARE / mu_cold)
                    if (mu_cold and pool > 0) else None)
        out.append({
            "期号": str(row["期号"]),
            "奖池": round(pool, 0),
            "头奖单注估计": round(jpw, 0),
            "固定奖贡献": round(sum(payout * p for payout, p in
                                    fixed_prizes_robust(name, str(row["期号"])).values()), 6),
            "参与信号": bool(jpw >= _JACKPOT_CAP * 0.999 and lam > 0),
            "μ_冷门": round(mu_cold, 3) if mu_cold is not None else None,
            "头奖单注估计(冷门口径)": round(jpw_cold, 0) if jpw_cold is not None else None,
        })
    return out


def cap_ref(name: str) -> float:
    return _JACKPOT_CAP * 0.8


if __name__ == "__main__":
    import json, sys
    for name in (sys.argv[1:] or ["双色球", "大乐透"]):
        r = rollover_ev(name)
        print(json.dumps(r, ensure_ascii=False, indent=1))
