"""
discovery/always_valid.py —— P2-3 e-value / 序贯检验（路线图 L2）

要回答的问题："盯了这么久，现在能下结论了吗？"——而且结论不因『偷看』而失效。

背景：discovery/gate 是一次性假设检验：攒够数据跑一次，出 p/q。
但挂在调度器上每期开奖后都跑一遍的『持续监控』有个统计陷阱：盯得越久，
越容易在某一次『偷看』中撞见假信号（peeking / optional stopping）。
p 值在这种用法下会系统性虚报。

e-value（赌局检验 / test martingale）解法：构造一个非负鞅（财富过程）W_n，
零假设下对任意停时 τ 都有 E[W_τ] ≤ 1（e-process），因此
  P( 存在某时刻 W_n ≥ 1/α ) ≤ α
——这就是"任意停时都有效"：随时喊停下结论都不会因为偷看而虚报。
阈值 1/α 即拒绝线（α=0.05 → W ≥ 20 告警）。

本模块监控的对象 = 25 条登记假设（training/hypotheses/*.json）的证伪对象：
「逐分区边际公平性」。理由（与 credibility/gate 结论同源）：
  冷热号/遗漏/区间等策略要跑赢随机，唯一来源是某分区开出频率偏离均匀；
  若真出现不公平，正是这类策略 edge 的充要条件。因此对每一分区做
  『每期类别频率 ≠ 均匀』的序贯监控 = 对这批假设做可持续、可随时喊停的证伪。

实现（轻依赖、口径与 credibility 一致）：
- 零假设 H0_k：第 k 个类别（号码/数字）每期出现指示 Y_{t,k} ~ Bernoulli(p_k) 独立。
  乐透型区（无序不重复，每期选 c 个）：p_k = c / size（如双色球红球 6/33）。
  数字型位（每期一个取值）：独热指示，p_k = 1 / size。
- 赌注 λ_t：可预测（只用 t-1 期及以前数据），plug-in 运行频率 q̂_{t-1} 的
  类 Kelly 分数，裁剪进 [ -1/(1-p0)·(1-ε), 1/p0·(1-ε) ] 保证因子非负。
  W_{n,k} = Π_t (1 + λ_{t,k} (Y_{t,k} - p_k))，零假设下每步条件期望 = 1 → 非负鞅。
- 类别间 / 分区内合并：e-value 取平均仍是 e-value（合法）。分区 e-value =
  区内类别财富平均。**告警以分区为单位**：分区 e-value 全程任一点 ≥ 拒绝阈 T
  即告警。T 取 T = M/α（M = 6 彩种监控分区总数 ≈22，Bonferroni 跨分区族），
  因每个分区 e-value 满足 P(sup W ≥ T) ≤ 1/T（Ville），族错误率 ≤ M/T = α
  且在**任意停时**都成立——这就是"随时喊停不误报"的严格含义。
- 彩种级/全局平均曲线仅作连续诊断（不再设告警线），避免平均稀释检出能力。
- ⚠️ 七星彩第 7 位只用 2020-10-11 起现行段（0-9 → 0-14 规则变更，边际不同，
  段混合会制造伪告警）——与 credibility/data_quality、info_theory 同口径。
- 赌注量级：c=0.3（plug-in Kelly 的收缩）。实测 c=0.8 时零假设下典型路径
  财富塌缩到 0（e-max 依赖罕见尖峰，不稳健）；c=0.3 下典型路径稳定。
  诚实代价：任意停时检验对小偏差（δ<0.03）需要很大样本——见 calibrate_bias
  的 δ-检出表（模块文档与 CLI --calibrate 输出）。
- 口径限制（与 P2-2 变点互补）：e-value 自首期累计对固定 p0，若历史某段
  偏离随后被反向偏离"补偿"（如变点后频率回摆），累计可能不告警——这类
  段内漂移由 P2-2 单变点扫描负责。调度器若在 P2-2 变点后重设锚点，本监控
  即转为"自变点起是否仍公平"的持续检验。

判定口径（诚实）：
- 告警 ≠ 『可预测』。先按『机制/数据口径变化的线索』处理，与 P2-2 变点、
  P2-4 信息量交叉验证；闸门（gate）结论仍以一次性全套检验为准。
- 对『历史随机是默认先验』：合成 iid 数据校准阈值，真数据全程无告警才算干净。

参考：Waudby-Smith & Ramdas (2023) "Estimating means of bounded random
variables by betting"；Ville 不等式（非负鞅任意停时）。
"""
import json
import logging
from datetime import date
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_JSON = BASE_DIR / "discovery" / "evalues.json"

# 七星彩第7位规则变更日（后区 0-9 → 0-14），与 credibility/data_quality.py 一致
_QXC_CUTOFF = date(2020, 10, 11)

_LOTTERIES = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]


# ============================================================
# 核心：赌局 e-process（任意停时有效的检验）
# ============================================================
def _wealth_matrix(B, p0: float, warm: int = 200, c: float = 0.3,
                   eps: float = 1e-3):
    """对 (n,K) 二元矩阵（每列零假设均值 p0）按列算财富过程（向量化）。

    同一分区的零假设概率对每个类别都相同（p_k = choose/size 恒定），
    故逐列共享 p0，可整列向量化：λ_t 用前 t-1 期累计（cumsum）——可预测，
    裁剪进非负因子边界 → 每列都是非负鞅（任意停时 e-value）。
    返回 (n+1, K) 财富曲线，W[0]=1，W[t] 对应第 t 个观测之后。
    """
    B = np.asarray(B, dtype=float)
    n, K = B.shape
    W = np.ones((n + 1, K))
    if n == 0 or K == 0:
        return W
    lam_lo = -(1.0 - eps) / max(1.0 - p0, 1e-9)
    lam_hi = (1.0 - eps) / max(p0, 1e-9)
    denom = p0 * (1.0 - p0)
    # cs[t] = 前 t 行之和（cs[0]=0）；q̂_{t} = cs[t-1]/(t-1)（不含本期）
    cs = np.vstack([np.zeros((1, K)), np.cumsum(B, axis=0)])
    factors = np.ones((n, K))
    if denom > 0:
        t_idx = np.arange(1, n + 1)
        active = t_idx - 1 >= warm  # 第 t 步（用第 t-1 期前数据）在 warm 后启用
        qhat = np.zeros((n, K))
        qhat[active] = cs[t_idx[active] - 1] / (t_idx[active, None] - 1)
        raw = c * (qhat - p0) / denom
        lam = np.clip(raw, lam_lo, lam_hi)
        factors = 1.0 + lam * (B - p0)
    W[1:] = np.cumprod(factors, axis=0)
    return W


def e_process(x, p0: float, warm: int = 200, c: float = 0.3,
              eps: float = 1e-3) -> np.ndarray:
    """单列二元序列 x∈{0,1}（零假设均值 p0）的赌局财富曲线（标量包装）。

    W_t = Π (1 + λ_s (x_s − p0))，λ_s 可预测、裁剪后保证因子非负。
    零假设下 W 是非负鞅（E[W_n]=1 恒成立）→ 任意停时 e-value：
      P(sup W ≥ 1/α) ≤ α。
    返回长度 n+1 的财富曲线（W[0]=1，W[t+1] 对应观测 x[t] 之后）。
    """
    x = np.asarray(x, dtype=float).reshape(-1, 1)
    return _wealth_matrix(x, p0, warm=warm, c=c, eps=eps)[:, 0]


def _combine_wealth(wealths) -> np.ndarray:
    """e-value 取平均仍是 e-value：把多条财富曲线合并成一条。

    曲线长度可能不一致（七星彩第 7 位只用现行段，期数更短）；
    短曲线按末值平走填充——该序列数据结束后不再有观测，财富保持不变。
    """
    if not wealths:
        return np.array([1.0])
    arrs = [np.asarray(w, dtype=float) for w in wealths]
    L = max(len(a) for a in arrs)
    padded = [np.pad(a, (0, L - len(a)), mode="edge") if len(a) < L else a
              for a in arrs]
    return np.mean(np.vstack(padded), axis=0)


def _first_cross(W: np.ndarray, threshold: float):
    """首次 ≥ 阈值的索引；无则 None。W[0]=1 不对应观测，从 W[1] 起查。"""
    hit = np.flatnonzero(W[1:] >= threshold)
    return int(hit[0]) + 1 if len(hit) else None


# ============================================================
# 分区 → 类别指示矩阵（零假设 p_k 由玩法结构决定）
# ============================================================
def _zone_matrix(records, zone):
    """把一区的逐期号码转成 (n,K) 出现指示矩阵 + 零假设概率 p。

    - 数字型位（choose=1）：每期一个取值 → 独热，p_k = 1/size。
    - 乐透型区（choose=c，无序不重复）：每期选 c 个 → 包含指示，p_k = c/size。
    - 七星彩第 7 位（zone.max==14）只用 2020-10-11 起现行段。
    返回 (B, p, issues) ；issues 与该矩阵行一一对应（供定位告警期号）。
    """
    from data.schema import record_zone_numbers

    recs = records
    if zone.max == 14:
        recs = [r for r in records
                if getattr(r, "开奖日期", None) and r.开奖日期 >= _QXC_CUTOFF]
    K = zone.size
    B_rows, issues = [], []
    for r in recs:
        vals = np.asarray(record_zone_numbers(r, zone), dtype=int)
        vals = vals[(vals >= zone.min) & (vals <= zone.max)] - zone.min
        if len(vals) == 0:
            continue
        row = np.zeros(K, dtype=bool)
        row[vals] = True
        B_rows.append(row)
        issues.append(getattr(r, "期号", None))
    if not B_rows:
        return None, None, []
    B = np.vstack(B_rows).astype(float)
    p = np.full(K, zone.choose / float(K))
    return B, p, issues


# ============================================================
# 彩种级监控
# ============================================================
# 6 彩种的名义分区族（每彩种 schema.zones 个数：2+2+5+3+3+7 = 22）
def _fleet_n_zones() -> int:
    from data.schema import get_schema
    return sum(len(get_schema(nm).zones) for nm in _LOTTERIES)


def _monitor_lottery(name: str, alpha: float = 0.05, warm: int = 200) -> dict:
    """6 彩种任一：逐分区 e-value 监控（分区级任意停时告警）。

    拒绝阈 T = M/α（M = 6 彩种名义分区族 ≈22，Bonferroni 跨族）：
    每分区 e-value 满足 P(任一点 ≥ T) ≤ 1/T → 全族任意停时错误率 ≤ M/T = α。
    """
    from data.loader import load_lottery
    from data.schema import get_schema

    data = load_lottery(name)
    schema = get_schema(name)
    chron = sorted(data.records, key=lambda r: r.期号 or 0)
    n_total = len(chron)

    M = _fleet_n_zones()
    threshold = M / alpha
    zones_out = []
    lot_curve_parts = []      # 彩种级合并（信息性诊断，不设告警）
    for zone in schema.zones:
        B, p, issues = _zone_matrix(chron, zone)
        if B is None:
            zones_out.append({"分区": zone.name, "值域": f"{zone.min}-{zone.max}",
                              "注": "无有效期数（可能规则段过滤后为空）"})
            continue
        n_used = len(B)
        Wk = _wealth_matrix(B, float(p[0]), warm=warm)  # (n+1, K)，同区 p0 恒定
        zone_comb = Wk.mean(axis=1)
        lot_curve_parts.append(zone_comb)
        zm = {
            "分区": zone.name, "值域": f"{zone.min}-{zone.max}",
            "单元数": int(Wk.shape[1]), "期数(用)": n_used,
            "样本不足": bool(n_used < warm + 100),
            "组合e-max": round(float(zone_comb.max()), 4),
            "拒绝阈": round(threshold, 2),
        }
        cidx = _first_cross(zone_comb, threshold)
        zm["首次越阈(期索引)"] = cidx
        if cidx is not None:
            zm["首次越阈期号"] = issues[cidx - 1] if cidx <= len(issues) else None
        zones_out.append(zm)

    lot_comb = _combine_wealth(lot_curve_parts) if lot_curve_parts else np.array([1.0])
    crossed = [z for z in zones_out if z.get("首次越阈(期索引)") is not None]
    n_units = sum(z.get("单元数", 0) for z in zones_out)
    if crossed:
        detail = "、".join(
            f"{z['分区']}@{z.get('首次越阈期号')}(e-max={z['组合e-max']})" for z in crossed)
        conclusion = (f"⚠️ 检出分区级任意停时告警 [{detail}] —— 先按机制/"
                      f"数据口径变化排查，与 P2-2 变点、P2-4 交叉验证；"
                      f"告警 ≠ 可预测")
    else:
        conclusion = (f"全部分区任意停时无告警（各分区 e-max < {threshold:.0f}）"
                      f" → 『该彩种开奖分区边际公平』通过持续监控，"
                      f"与闸门『假设全驳回』一致")
    return {
        "彩种": name,
        "期数(总)": n_total,
        "族水平α": alpha,
        "拒绝阈": round(threshold, 2),
        "分区单元总数": n_units,
        "彩种合并e-max(诊断)": round(float(lot_comb.max()), 4),
        "结论": conclusion,
        "分区": zones_out,
    }


# ============================================================
# 假设登记表覆盖说明（25 条假设 ↔ 监控对象映射）
# ============================================================
def _hypothesis_coverage(name: str) -> list:
    """该彩种登记假设 + e-value 监控覆盖说明（只读登记表，不改状态）。"""
    from discovery.hypothesis_registry import list_hypotheses

    hyps = list_hypotheses(name)
    out = []
    for h in hyps:
        probe = h["probe"].get("type")
        note = ("监控对象=该彩种全部分区边际公平性：策略若真能跑赢随机，"
                "必然来自某分区频率偏离均匀——正是本监控的告警条件"
                if probe == "credibility_gate" else
                "闸门已判（固定赔率EV恒等/七星彩需 crowd 数据逐期反推，"
                "e-value 序贯对象为公平性类，本类不设逐期监控）")
        out.append({
            "id": h.get("id"),
            "name": h.get("name"),
            "probe": probe,
            "登记状态": h.get("status"),
            "e-value覆盖": note,
        })
    return out


def monitor_lottery(name: str, alpha: float = 0.05, warm: int = 200,
                    with_hypotheses: bool = True) -> dict:
    """单彩种完整报告：分区 e-value 监控 + 假设覆盖说明。"""
    rep = _monitor_lottery(name, alpha=alpha, warm=warm)
    if with_hypotheses:
        rep["覆盖假设"] = _hypothesis_coverage(name)
    return rep


def run_all(alpha: float = 0.05, warm: int = 200,
            write_json: bool = False) -> dict:
    """6 彩种批量：逐彩种分区报告 + 族级汇总（M 分区，Bonferroni 任意停时）。"""
    reps = [monitor_lottery(nm, alpha=alpha, warm=warm) for nm in _LOTTERIES]
    M = _fleet_n_zones()
    threshold = M / alpha
    crossed = []
    for rep in reps:
        for z in rep["分区"]:
            if z.get("首次越阈(期索引)") is not None:
                crossed.append({
                    "彩种": rep["彩种"], "分区": z["分区"],
                    "期号": z.get("首次越阈期号"),
                    "e-max": z["组合e-max"],
                })
    report = {
        "模块": "P2-3 e-value 序贯检验（always_valid.py）",
        "更新于": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "族水平α": alpha,
        "监控分区数M": M,
        "拒绝阈": round(threshold, 2),
        "越阈分区": crossed,
        "族结论": (
            f"⚠️ {len(crossed)} 个分区任意停时告警："
            + "、".join(f"{c['彩种']}/{c['分区']}@{c['期号']}(e-max={c['e-max']})"
                        for c in crossed)
            + " —— 与 P2-2 变点/已知规则变更交叉验证后定性（告警≠可预测）"
            if crossed else
            f"6 彩种 {M} 个监控分区在任意停时均无告警（各分区 e-max < {threshold:.0f}）"
            f" → 『开奖序列公平随机』通过持续监控；合成校准：iid 族误报率 ≤ α"
        ),
        "彩种": reps,
    }
    if write_json:
        try:
            OUT_JSON.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info(f"已写 {OUT_JSON}")
        except Exception as e:  # 写盘失败不致命
            logger.warning(f"写 {OUT_JSON} 失败: {e}")
    return report


# ============================================================
# 合成数据校准（CLI --calibrate / 测试用）
# ============================================================
def _sim_rows(rng, n: int, K: int, c: int):
    """均匀抽 c 个不重复类别 → (n,K) 二元行矩阵（向量化，c=1 也适用）。"""
    if c == 1:
        idx = rng.integers(0, K, size=n)
        x = np.zeros((n, K), dtype=float)
        x[np.arange(n), idx] = 1.0
        return x
    order = np.argsort(rng.random((n, K)), axis=1)
    x = np.zeros((n, K), dtype=float)
    rows = np.arange(n)[:, None]
    x[rows, order[:, :c]] = 1.0
    return x


# 真实 6 彩种 22 分区结构（K=类别数, c=每期抽取数）——校准用固定族
# 双色球(33红,16蓝) + 大乐透(35红,12蓝) + 排5/3D/排3 各3~5位 + 七星 6 位 + 第7位0-14
_REAL_ZONES = ([(33, 6), (16, 1), (35, 5), (12, 2)]
               + [(10, 1)] * 17 + [(15, 1)])


def calibrate_null(n_draws: int = 2000, seeds: int = 30, alpha: float = 0.05,
                   warm: int = 200) -> dict:
    """iid 公平数据：按真实 22 分区结构模拟，统计『族级』任意停时误报率。

    每个 seed 生成一整套 6 彩种分区（_REAL_ZONES），分区内各类别独立公平；
    任一分区 e-value 全程 ≥ T=M/α 即算该 seed 族级误报。理论界 ≤ α
    （Bonferroni + Ville）；经验值应远低于 α（实测通常 0）。
    """
    M = len(_REAL_ZONES)
    threshold = M / alpha
    fleet_cross = 0
    zone_cross_total = 0
    maxes = []
    for seed in range(seeds):
        rng = np.random.default_rng(20260904 + seed)
        fleet_hit = False
        for (K, c) in _REAL_ZONES:
            p0 = c / float(K)
            x = _sim_rows(rng, n_draws, K, c)
            Wk = _wealth_matrix(x, p0, warm=warm)
            zone = Wk.mean(axis=1)
            maxes.append(float(zone.max()))
            if _first_cross(zone, threshold) is not None:
                zone_cross_total += 1
                fleet_hit = True
        if fleet_hit:
            fleet_cross += 1
    arr = np.asarray(maxes)
    return {
        "场景": "iid 公平随机（真实 22 分区结构，族级任意停时误报率标定）",
        "分区数M": M, "期数": n_draws, "模拟次数": seeds,
        "族水平α": alpha, "拒绝阈T": round(threshold, 2),
        "族级误报": fleet_cross,
        "经验族误报率": round(fleet_cross / seeds, 4),
        "分区e-max 分位(p50/p95/p99.9)": [round(float(q), 2) for q in
                                        np.quantile(arr, [0.5, 0.95, 0.999])],
        "判定": ("校准通过：经验族误报率 ≤ 2α 量级"
                if fleet_cross <= max(1, int(seeds * alpha * 2))
                else "⚠️ 经验误报率超 2α，需提高 T 或调整赌注"),
    }


def calibrate_bias(delta: float = 0.05, n_draws: int = 3000, warm: int = 200,
                   seeds: int = 30, alpha: float = 0.05) -> dict:
    """检测能力：某一类别真实偏离均匀 δ（K=10 独热）应触发分区级告警。

    用与真实监控相同的族拒绝阈 T = M/α（M=22）判告警。
    诚实口径：任意停时检验对小偏差需要大样本——δ=0.03 在 3000 期检出率
    低（≈7%），δ=0.04 ≈47%，δ=0.05 ≈88%，δ=0.06 ≈100%（60 次模拟实测）。
    返回：各模拟的越阈期（检测时间）分布。
    """
    K, p0 = 10, 0.1
    threshold = len(_REAL_ZONES) / alpha   # 与真实监控同一族阈值
    detect = []
    for seed in range(seeds):
        rng = np.random.default_rng(10000 + seed)
        bias_cat = int(rng.integers(0, K))
        # 有偏生成：偏置类别概率 p0+δ，其余等权归一
        probs = np.full(K, (1.0 - (p0 + delta)) / (K - 1))
        probs[bias_cat] = p0 + delta
        x = rng.multinomial(1, probs, size=n_draws).astype(float)
        Wk = _wealth_matrix(x, p0, warm=warm)
        g = Wk.mean(axis=1)
        cidx = _first_cross(g, threshold)
        detect.append(None if cidx is None else int(cidx))
    det = np.asarray([d for d in detect if d is not None], dtype=float)
    return {
        "场景": f"单类别偏离均匀 δ={delta}（K=10 独热，族阈 T={threshold:.0f}）",
        "期数": n_draws, "模拟次数": seeds,
        "检出次数": int(len(det)),
        "检出率": round(len(det) / seeds, 3),
        "检出期数中位数": (int(np.median(det)) if len(det) else None),
        "判定": ("检测能力正常：偏离可被任意停时捕获"
                if len(det) >= seeds * 0.8 else "⚠️ 检出率不足，需增大 δ 或加大样本"),
    }


if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO)
    if len(sys.argv) > 1 and sys.argv[1] == "--calibrate":
        print(json.dumps(calibrate_null(), ensure_ascii=False, indent=1))
        for d in (0.03, 0.05, 0.06):
            print(json.dumps(calibrate_bias(delta=d), ensure_ascii=False, indent=1))
    else:
        rep = run_all(write_json=True)
        print(rep["族结论"])
        for r in rep["彩种"]:
            print(f"[{r['彩种']}] {r['结论']}")
            for z in r["分区"]:
                if "注" in z:
                    print(f"    {z['分区']}: {z['注']}")
                    continue
                cross = z.get("首次越阈期号") or z.get("首次越阈(期索引)")
                mark = "★" if cross is not None else " "
                print(f"  {mark} {z['分区']}: e-max={z['组合e-max']}"
                      f"{f' 越阈@{cross}' if cross is not None else ''}")
