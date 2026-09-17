"""
credibility/changepoint.py —— P2-2 变点检测（路线图 L2）

目标：开奖数据/机制可能在某个时间点突变（换摇奖机、改规则、数据源口径变化、
甚至修数据）。变点检测自动找"哪里变了"，结果供训练/回测窗口切界参考——
否则拿旧机制的数据学新机制，白学。

方法（单变点扫描 + 条件置换，轻依赖，口径与 credibility 一致）：
1. 每分区把历史取值展成时间序列 V（0..S-1，含每期多值；日期逐值对齐）。
2. 对每个候选切点 c（min_seg ≤ c ≤ N-min_seg）算两段（[0,c) vs [c,N)）
   分布差异的 χ²；取最大 χ² 的 c* = 最佳单变点。
3. 显著性：固定 c* 做 B 次置换（打乱 V 保留边际），χ²(c*) 在零分布中的
   位置 = p（探索性：条件于已发现切点，乐观偏置靠效应量门控压制）。
4. 效应量 Cramér V ≥ v_gate 且 p<0.05 才算"实质变点"；跨分区 BH-FDR。
5. 结论先按"机制/数据异常"处理，而不是宣称规则改了——除非已知规则
   变更对应（如七星彩第 7 位 0-9→0-14 在 2020-10 有官方变更）。

落点：CLI `changepoint`；结果写 discovery/changepoints.json 供 rolling 切窗参考。
"""
import json
import logging
from datetime import date
from pathlib import Path

import numpy as np

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
OUT_JSON = BASE_DIR / "discovery" / "changepoints.json"


# ---------------------------------------------------------------- 基础统计
def _chi2_2s(f1, e1, f2, e2) -> float:
    """两段 χ² = Σ (f1-e1)²/e1 + (f2-e2)²/e2（e 为 0 的 bin 跳过）"""
    return float(
        np.sum(np.divide((f1 - e1) ** 2, e1,
                         out=np.zeros_like(e1, dtype=float), where=e1 > 0))
        + np.sum(np.divide((f2 - e2) ** 2, e2,
                           out=np.zeros_like(e2, dtype=float), where=e2 > 0)))


def _best_split(vals: np.ndarray, S: int, min_seg: int):
    """最佳单变点：两段 χ² 最大。返回 (c*, chi2)。"""
    N = len(vals)
    pref = np.zeros((N + 1, S), dtype=np.int64)
    for i, v in enumerate(vals):
        pref[i + 1] = pref[i]
        pref[i + 1, v] += 1
    tot = pref[N].astype(float)
    best_c, best_chi = None, -1.0
    for c in range(min_seg, N - min_seg + 1):
        f1 = pref[c].astype(float)
        f2 = tot - f1
        pj = tot / N
        chi = _chi2_2s(f1, c * pj, f2, (N - c) * pj)
        if chi > best_chi:
            best_chi, best_c = chi, c
    return best_c, best_chi


def _split_p(vals: np.ndarray, c: int, S: int, B: int, seed: int) -> float:
    """固定切点 c 的置换 p（打乱保留边际，两段同分布零假设）"""
    N = len(vals)
    tot = np.bincount(vals, minlength=S).astype(float)
    pj = tot / N
    f1_obs = np.bincount(vals[:c], minlength=S).astype(float)
    e1 = c * pj
    e2 = (N - c) * pj
    obs = _chi2_2s(f1_obs, e1, tot - f1_obs, e2)
    rng = np.random.default_rng(seed)
    ge = 1
    for _ in range(B):
        s = rng.permutation(vals)
        f1 = np.bincount(s[:c], minlength=S).astype(float)
        if _chi2_2s(f1, e1, tot - f1, e2) >= obs:
            ge += 1
    return ge / (B + 1)


def _cramers_v(chi2: float, N: int, S: int) -> float:
    """两样本 Cramér V（2×S 表）≈ sqrt(χ²/(N·min(2,S)-1)) = sqrt(χ²/N)"""
    return float(np.sqrt(chi2 / N)) if N > 0 else 0.0


# ---------------------------------------------------------------- 彩种级扫描
def _series_by_zone(records, schema):
    """每分区取值序列（时间升序，含每期多值）+ 逐值日期/期号"""
    from data.schema import record_zone_numbers
    out = []
    for zone in schema.zones:
        vals, dates, issues = [], [], []
        for r in records:
            v = [x for x in record_zone_numbers(r, zone)]
            if not v:
                continue
            d = getattr(r, "开奖日期", None)
            iss = getattr(r, "期号", None)
            for x in v:
                vals.append(int(x) - zone.min)
                dates.append(d)
                issues.append(iss)
        out.append({
            "zone": zone,
            "vals": np.array([x for x in vals if 0 <= x < zone.size], dtype=int),
            "dates": dates,
            "issues": issues,
        })
    return out


def _fmt_date(d):
    return str(d) if d is not None else "-"


def change_point_scan(lottery: str, B: int = 1000, min_seg: int = 200,
                      v_gate: float = 0.1, write_json: bool = True) -> dict:
    """单变点扫描（每分区最强切点）。返回报告 + 可选写 discovery/changepoints.json"""
    from data.loader import load_lottery
    from data.schema import get_schema
    data = load_lottery(lottery)
    schema = get_schema(lottery)
    chron = sorted(data.records, key=lambda r: r.期号 or 0)
    zones = _series_by_zone(chron, schema)

    rows = []
    for it in zones:
        vals, S = it["vals"], it["zone"].size
        N = len(vals)
        if N < min_seg * 2 + 100:
            rows.append({"分区": it["zone"].name, "期数": N,
                         "注": "样本不足，跳过"})
            continue
        c, chi2 = _best_split(vals, S, min_seg)
        if c is None:
            continue
        p = _split_p(vals, c, S, B=B, seed=int.from_bytes(
            it["zone"].name.encode("utf-8")[:4], "little") + 11)
        V = _cramers_v(chi2, N, S)
        d = it["dates"][c]
        rows.append({
            "分区": it["zone"].name,
            "值域": f"{it['zone'].min}-{it['zone'].max}",
            "样本": N,
            "变点位置": int(c),
            "位置占比": round(c / N, 3),
            "变点日期": _fmt_date(d),
            "变点期号": it["issues"][c],
            "χ²": round(float(chi2), 1),
            "CramérV": round(float(V), 4),
            "p": round(float(p), 4),
            "实质变点": bool(p < 0.05 and V >= v_gate),
        })

    sig_rows = [r for r in rows if r.get("实质变点")]
    # BH-FDR 跨分区（仅对有 p 的行）
    rows_with_p = [r for r in rows if "p" in r]
    if rows_with_p:
        ps = np.array([r["p"] for r in rows_with_p])
        order = np.argsort(ps)
        n = len(ps)
        qs = np.empty(n)
        ranked = ps[order] * n / (np.arange(1, n + 1))
        qs[order] = np.minimum.accumulate(ranked[::-1])[::-1]
        for r, q in zip(rows_with_p, np.minimum(qs, 1.0)):
            r["q(BH)"] = round(float(q), 4)
            if r.get("实质变点"):
                r["实质变点"] = bool(r["q(BH)"] < 0.05 and r["CramérV"] >= v_gate)
    sig_rows = [r for r in rows if r.get("实质变点")]

    report = {
        "彩种": lottery,
        "期数(总)": len(chron),
        "扫描口径": f"单变点(两段χ²) + 固定切点置换p(B={B}) + CramérV≥{v_gate} 门控 + BH",
        "检出实质变点": [r for r in sig_rows],
        "结论": ("⚠️ 检出分区统计特性的突变点——先按数据/规则异常排查"
                "（开奖机制理论上平稳；七星彩第 7 位 2020-10 官方 0-9→0-14 属已知规则变更）"
                if sig_rows else
                "各分区未检出实质统计变点（号码机制/数据口径在样本窗口内平稳）"),
        "全部扫描": rows,
    }
    if write_json:
        try:
            payload = json.loads(OUT_JSON.read_text(encoding="utf-8")) \
                if OUT_JSON.exists() else {}
            payload[lottery] = {
                "updated_at": date.today().isoformat(),
                "检出": [{"分区": r["分区"], "日期": r.get("变点日期"),
                          "期号": r.get("变点期号"), "p": r.get("p"),
                          "q": r.get("q(BH)"), "CramérV": r.get("CramérV")}
                         for r in sig_rows],
                "note": "供 rolling 训练/回测切窗参考；无检出=窗口内平稳",
            }
            OUT_JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                                encoding="utf-8")
        except Exception as e:  # 写失败不阻断报告
            logger.warning(f"写 {OUT_JSON} 失败: {e}")
    return report


if __name__ == "__main__":
    import json as _json
    import sys
    logging.basicConfig(level=logging.INFO)
    for nm in (sys.argv[1:] or ["七星彩", "大乐透"]):
        rep = change_point_scan(nm)
        print(f"[{nm}] {rep['结论']}")
        for r in rep["全部扫描"]:
            mark = "★" if r.get("实质变点") else " "
            if "注" in r:
                print(f"  {r['分区']}: {r['注']}")
                continue
            print(f"  {mark} {r['分区']}: 变点@位置{r['变点位置']}({r['变点日期']},期{r['变点期号']})"
                  f" V={r['CramérV']} p={r['p']} q={r.get('q(BH)')}")
