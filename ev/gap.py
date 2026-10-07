# -*- coding: utf-8 -*-
"""ev/gap.py —— 机制缝隙测量的门面（facade）。

对外只暴露四件事：
    measure(lottery)          单彩种四轨测量 dict
    measure_all(...)          跨彩种汇总 + BH
    report_text(...)          人读的报告（CLI 与看板共用同一份措辞）
    mechanism_gamma(lottery)  给选号倾斜用的 γ（模型未 Go / 无缝隙 → 0.0）

用法：
    from ev import gap
    print(gap.report_text())
"""
from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

from ev.mechanism.common import ALL_LOTTERIES, DIGITAL_LOTTERIES, LOTTO_LOTTERIES
from ev.mechanism.gap_verdict import measure_gap, measure_gap_global

__all__ = ["measure", "measure_all", "report_text", "report_markdown",
           "mechanism_gamma",
           "LOTTO_LOTTERIES", "DIGITAL_LOTTERIES", "ALL_LOTTERIES"]


def measure(lottery: str, *, use_cache: bool = True, force: bool = False,
            **kw) -> Dict[str, Any]:
    """单彩种四轨测量（A/A′ + B + C + D）。"""
    return measure_gap(lottery, use_cache=use_cache, force=force, **kw)


def measure_all(lotteries: Sequence[str] = ALL_LOTTERIES, *,
                use_cache: bool = True, force: bool = False, **kw) -> Dict[str, Any]:
    """跨彩种汇总 + BH 控 FDR。"""
    return measure_gap_global(lotteries, use_cache=use_cache, force=force, **kw)


def mechanism_gamma(lottery: str, *, use_cache: bool = True) -> float:
    """
    选号倾斜要用的 γ。**取不到就返回 0.0**（等价于"不做任何倾斜"）。

    这是 γ=0 红线的实现点：模型未 Go、数据不足、γ CI 含 0、数字型无 γ —— 一律 0.0，
    让下游逐位退化成今天的行为。
    """
    try:
        r = measure(lottery, use_cache=use_cache)
    except Exception:                                    # noqa: BLE001
        return 0.0
    g = (r.get("gamma") or {}).get("gamma_合并") or {}
    if not g.get("显著"):
        return 0.0
    return float(g.get("gamma") or 0.0)


def _fmt(v: Any, nd: int = 4) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.{nd}f}"
    return str(v)


def _ci(ci, nd: int = 4) -> str:
    if not ci:
        return "—"
    return "[" + _fmt(ci[0], nd) + "," + _fmt(ci[1], nd) + "]"


# 「口径与限制」——CLI 文本报告与 Markdown 报告**共用同一份**，避免两处漂移。
_CAVEAT_LINES = [
    "  · A 轨用**免票量**口径（分母 = 二等注数反推票量），对追加/复式/多倍免疫。",
    "    「票量 = 总投注额/2」只对双色球成立（二等反推比 1.0041）；",
    "    大乐透为 1.1324（追加投注），故其销售额口径 R 不可用于判定。",
    "  · A′ 轨只对 排列5 / 七星彩 生效；排列3 / 福彩3D 的销售额覆盖**全部玩法**，",
    "    Σ注数/Σ(N·p) 量的是投注结构占比（实测三玩法份额 0.6766+0.0829+0.2294≈0.99）",
    "    而非缝隙 → 显式降级。七星彩只用现行规则段（≥2020-10-11，组合空间 1.5e7）。",
    "  · 标准误一律用**经验离散度**，不用模型隐含方差",
    "    （双色球经验 se 0.0203 是模型隐含 se 0.0030 的 6.7 倍）。",
    "    A/B/γ 三处口径现已统一为长块 bootstrap；实测真实 u 序列没有可检出的",
    "    块结构，长块 se 并不更宽 —— 统一口径是为了可复现，不是为了更保守。",
    "  · 原方案里的「条件口径 R_cond」**不是检验**：",
    "    R_cond = Σy/Σ(N·p1·e^{βz}) 在 q̂=p1·e^{βz}/c 下恒等于 c，",
    "    实测在偏置 θ∈[0,4] 上恒为常数（sd=0）。已降级为模型校准诊断。",
    "  · B 轨每期分位只用其之前的数据拟合 β（n_leak=0 自检）。",
    "    判定用的 se 是**长块移动块 bootstrap 的经验离散度**（与 A 轨同一估计量）；",
    "    解析 se=1/√(12n) 假定 u 序列 i.i.d.，**只作参照、不参与判定**。",
    "  · C 轨对红球形态设「分段同质闸门」：全样本显著但前半/后半段均值断裂的特征，",
    "    一律判为数据/规则分段问题、不参与判定（本次实测见上方线索区）。",
    "  · 阈值参与缓存指纹：改 --min-effect / --min-train / --step 会自动重算，",
    "    不会静默沿用旧阈值的缓存结果。",
    "  · 判定轨只有 A/A′/B；C/D 一律不参与判定。",
    "  · 「未检出缝隙」≠「已证明开奖公平」；本报告只给效应量、CI 与判定。",
]


def report_text(lotteries: Sequence[str] = ALL_LOTTERIES, *,
                use_cache: bool = True, force: bool = False,
                min_train: int = 1200, step: int = 50,
                min_effect: float = 0.03, c_mc: int = 100_000) -> str:
    """
    生成四轨测量报告（CLI `cli.py gap` 与 audit 卡片共用，保证措辞一致）。
    """
    glob = measure_gap_global(lotteries, use_cache=use_cache, force=force,
                              min_train=min_train, step=step,
                              min_effect=min_effect, c_mc=c_mc)
    L: list = []
    L.append("机制缝隙测量 · 四轨")
    L.append("约定：")
    L.append("  A  轨  乐透型注数比 R = 实际一等奖注数 / 期望注数（免票量口径，模型无关，主判据）")
    L.append("  A′ 轨  数字型/七星彩的同一口径（票量 = 销售额÷2；直读销售奖级 CSV）")
    L.append("  B  轨  ū = 真实开出组合的流行度分位均值（严格 walk-forward；公平开奖 = 0.5）")
    L.append("  C  轨  位级/分区结构偏差（MC 校准零分布 + 族内 BH）—— **不判缝隙**，只作线索")
    L.append("  D  轨  派奖期 vs 平常期（「特别时期」的代理）—— 样本小，只报差异不判缝隙")
    L.append("  γ      冷门偏置强度（>0 = 偏向冷门；依赖流行度模型，见各彩种 scale）")
    L.append("")
    hdr = (f"{'彩种':<5}{'型':<3}{'n':>6} {'R':>9} {'R CI95':>19} {'判定轨':>7} "
           f"{'ū':>8} {'ū CI95':>19} {'B':>3} {'γ':>9} {'裁决':>7}")
    L.append(hdr)
    L.append("-" * 118)
    for r in glob["逐彩种"]:
        if not r.get("applicable"):
            L.append(f"{r['lottery']:<5} {r.get('note', '不适用')}")
            continue
        t = r.get("track结果") or {}
        dig = r.get("轨类型") == "数字型"
        a = t.get("A′" if dig else "A") or {}
        b = t.get("B") or {}
        v = r.get("裁决", {})
        R = (a.get("主口径") or {}).get("R")
        Rci = (a.get("主口径") or {}).get("ci95")
        u = b.get("mean_u")
        uci = b.get("ci95")
        g = ((r.get("gamma") or {}).get("gamma_合并") or {})
        nj = v.get("n_判定轨") or r.get("n")
        st = {"no-track": "无判定轨"}.get(v.get("status"), v.get("status", "—"))
        # 判定轨列必须显示**本次真的判过的轨**（`判定轨`），不能按彩种类型硬打印 ——
        # 否则排列3/福彩3D 会在同一行里同时出现「A′」与「无判定轨」，自相矛盾。
        # 宽度仍 7（原 `A:>3` + `/B:>4` = 7 ⇒ 单字段 `>7`），表头与总宽都不变。
        tracks_disp = "/".join(v.get("判定轨") or []) or "无"
        L.append(
            f"{r['lottery']:<5}{'数字' if dig else '乐透':<3}{nj!s:>6} "
            f"{_fmt(R, 5):>9} {_ci(Rci, 4):>19} {tracks_disp:>7}"
            f"{_fmt(u, 5):>8} {_ci(uci, 4):>19} "
            f"{('Y' if v.get('B轨') else ('n' if b else '—')):>3} "
            f"{_fmt(g.get('gamma')):>9} {st:>7}")
    L.append("")

    # —— C / D 轨（不参与判定）——
    hdr2 = (f"{'彩种':<5}{'C:检验数':>9}{'C:统计显著':>10}{'C:实质偏离':>10}"
            f"{'C:分段不一致':>12}  {'C 结论':<34}{'D':>6}")
    L.append(hdr2)
    L.append("-" * 118)
    for r in glob["逐彩种"]:
        if not r.get("applicable"):
            continue
        c = (r.get("track结果") or {}).get("C") or {}
        d = (r.get("track结果") or {}).get("D") or {}
        stat = sum(x["统计显著"] for x in (c.get("族") or {}).values())
        pl = len(c.get("实质偏离") or [])
        ih = len(c.get("分段不一致") or [])
        concl = (c.get("结论") or "不适用")
        dtag = ("有差异" if d.get("gap") else
                ("未检出" if d.get("applicable") else "不适用"))
        L.append(f"{r['lottery']:<5}{c.get('n检验', 0):>9}{stat:>10}{pl:>10}"
                 f"{ih:>12}  {concl[:32]:<34}{dtag:>6}")
    L.append("")

    for r in glob["逐彩种"]:
        if r.get("applicable"):
            L.append(f"[{r['lottery']}] {r.get('裁决', {}).get('note')}")
    L.append("")
    L.append(f"跨彩种 BH(FDR) 检出：{glob['检出彩种'] or '无'}")
    L.append("")

    # 值得跟的线索
    leads = []
    for r in glob["逐彩种"]:
        if not r.get("applicable"):
            continue
        t = r.get("track结果") or {}
        a = t.get("A") or {}
        for w in (a.get("判定", {}) or {}).get("警告") or []:
            leads.append(f"[{r['lottery']}] {w}")
        b = t.get("B") or {}
        if b.get("applicable") and b.get("模型Go") is False:
            leads.append(f"[{r['lottery']}] B 轨分位轴所用模型 5 折 CV 未过 Go 门槛 → "
                         f"检验水准正确但功效低，其 z 不作强证据。")
        ap = t.get("A′") or {}
        if ap.get("applicable") and len(ap.get("各奖级") or {}) > 1:
            ds = [lv.get("R") for lv in (ap.get("各奖级") or {}).values()
                  if lv.get("usable")]
            if ds and ap.get("各奖级R极差") is not None:
                leads.append(f"[{r['lottery']}] A′ 轨 {len(ds)} 个奖级独立观测同一机制，"
                             f"R 极差 {ap['各奖级R极差']:.4f}、"
                             f"{'全部 <1' if ap.get('全奖级低于1') else '并非全部 <1'}"
                             f" → 互为交叉验证。")
        c = t.get("C") or {}
        for x in (c.get("分段不一致") or []):
            leads.append(f"[{r['lottery']}] C 轨「{x['特征']}」全样本显著但**分段不一致**"
                         f"（前 {x['分段']['前段均值']} → 后 {x['分段']['后段均值']}，"
                         f"z={x['分段']['分段差_z']}）→ 数据/规则分段问题，非机制。")
        d = t.get("D") or {}
        lim = (d.get("功效") or {}).get("检出下限")
        if d.get("applicable") and lim:
            leads.append(f"[{r['lottery']}] D 轨派奖期仅 {d.get('n_派奖期')} 期，"
                         f"可检出 |ΔR| 下限 ≈ {lim.get('可检出|ΔR|下限')}"
                         f"（{lim.get('奖级', '—')}口径）→ 未检出差异不等于没有差异。")
    if leads:
        L.append("线索（不参与判定）：")
        for x in leads:
            L.append("  · " + x)
        L.append("")

    L.append("口径与限制：")
    L.extend(_CAVEAT_LINES)
    return "\n".join(L)


# ------------------------------------------------------------------ Markdown 版
def _md_escape(s: str) -> str:
    """表格单元格里的 `|` 会撑破表格，换掉；换行折成空格。"""
    return str(s).replace("|", "\\|").replace("\n", " ").strip()


def _degradations(r: Dict[str, Any]) -> list:
    """收集该彩种**所有**降级标注——不许静默降级，全部要在报告里露出来。"""
    out = []
    t = r.get("track结果") or {}
    lot = r.get("lottery", "—")

    if not r.get("applicable"):
        out.append(f"整条不适用：{r.get('note', '—')}")
        return out

    v = r.get("裁决") or {}
    if v.get("status") == "no-track":
        out.append("无判定轨（该彩种结构上不可判定，非「测过没测到」）")
    if v.get("应有判定轨") and not v.get("判定轨"):
        out.append(f"应有判定轨 {v['应有判定轨']}，本次全部不可用")

    a = (t.get("A′") or t.get("A")) or {}
    lab = "A′" if t.get("A′") else "A"
    if a and not a.get("applicable"):
        out.append(f"{lab} 轨不可用：{a.get('note', '—')}")
    elif a and a.get("applicable"):
        main = a.get("主口径") or {}
        if main and main.get("usable") is False:
            out.append(f"{lab} 轨主口径不可用（销售额口径不参与判定）："
                       f"{main.get('note', '—')}")

    b = t.get("B") or {}
    if b and not b.get("applicable"):
        out.append(f"B 轨不可用：{b.get('note', '—')}")
    elif b and b.get("模型Go") is False:
        out.append("B 轨模型未过 Go 门槛 → 检验水准正确但功效低，z 不作强证据")

    c = t.get("C") or {}
    if c and not c.get("applicable"):
        out.append(f"C 轨不可用：{c.get('note', '—')}")
    d = t.get("D") or {}
    if d and not d.get("applicable"):
        out.append(f"D 轨不可用：{d.get('note', '—')}")

    gm = (r.get("gamma") or {}).get("gamma_合并") or {}
    if gm and gm.get("gamma") is not None and not gm.get("显著"):
        out.append(f"γ={gm.get('gamma')} 但 CI 含 0（未过显著门）→ 选号倾斜按 γ=0 处理")
    if not out:
        out.append("无")
    return out


def report_markdown(lotteries: Sequence[str] = ALL_LOTTERIES, *,
                    use_cache: bool = True, force: bool = False,
                    min_train: int = 1200, step: int = 50,
                    min_effect: float = 0.03, c_mc: int = 100_000) -> str:
    """
    四轨测量的 Markdown 报告（与 `report_text` 同源同测量，措辞共用 _CAVEAT_LINES）。

    比文本版多三块，都是**决策要用的**：
    ① 自检表：n_leak（B 轨前视泄漏）必须全 0，缓存命中与指纹可复现性；
    ② 降级标注：任何一条轨不可用/不参与判定都必须显式列出（不许静默降级）；
    ③ 选号倾斜状态：γ 是否真的驱动了出号（P4 管道）。
    """
    from datetime import datetime

    glob = measure_gap_global(lotteries, use_cache=use_cache, force=force,
                              min_train=min_train, step=step,
                              min_effect=min_effect, c_mc=c_mc)
    rows = [r for r in glob["逐彩种"]]
    cmd = ("python cli.py gap all" + (" --no-cache" if force else "")
           + f" --min-train {min_train} --step {step} --min-effect {min_effect}")

    L = [
        "# 机制缝隙测量 · 四轨报告",
        "",
        f"- 生成时间：{datetime.now().isoformat(timespec='seconds')}",
        f"- 复现命令：`{cmd}`",
        f"- 数据来源：`lottery_data/`（缓存命中则直接读 `cache/mechanism/`，指纹不符自动重算）",
        "",
        "**约定**：A 轨=乐透型注数比 R（免票量口径，模型无关）；A′ 轨=数字型同一口径；"
        "B 轨=真实开出号流行度分位均值 ū（公平开奖=0.5）；"
        "**C 轨（结构偏差）与 D 轨（派奖期）一律不参与判定，只作诊断**；"
        "γ=冷门偏置强度（>0 偏向冷门）。",
        "",
        "## 一、主表（判定轨）",
        "",
        "| 彩种 | 型 | n | R | R CI95 | 判定轨 | ū | ū CI95 | γ | γ显著 | 裁决 |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if not r.get("applicable"):
            L.append(f"| {_md_escape(r.get('lottery'))} | — | — | — | — | — | — | — | — |"
                     f" — | {_md_escape(r.get('note', '不适用'))} |")
            continue
        t = r.get("track结果") or {}
        dig = r.get("轨类型") == "数字型"
        a = t.get("A′" if dig else "A") or {}
        b = t.get("B") or {}
        v = r.get("裁决", {})
        main = a.get("主口径") or {}
        g = ((r.get("gamma") or {}).get("gamma_合并") or {})
        st = {"no-track": "无判定轨", "none": "未检出"}.get(
            v.get("status"), v.get("status", "—"))
        tracks = "/".join(v.get("判定轨") or []) or "无"
        # ★ γ 为 None 是「本彩种无 γ 估计」，不是「估计了但不显著」——显示成"否"会误导
        sig_disp = "—" if g.get("gamma") is None else ("是" if g.get("显著") else "否")
        L.append(
            f"| {_md_escape(r['lottery'])} | {'数字' if dig else '乐透'} | "
            f"{_md_escape(v.get('n_判定轨') or r.get('n'))} | "
            f"{_fmt(main.get('R'), 5)} | {_ci(main.get('ci95'), 4)} | {tracks} | "
            f"{_fmt(b.get('mean_u'), 5)} | {_ci(b.get('ci95'), 4)} | "
            f"{_fmt(g.get('gamma'))} | {sig_disp} | {st} |")
    L += ["", "> 表中 n = **判定轨**覆盖的期数；第二节指纹里的 n 是参与测量的**总期数**"
               "（含被降级/不适用的轨），两者本就不同，勿对不上就以为出错。"]

    L += ["", "## 二、自检（可复现性与前视泄漏）", "",
          "| 彩种 | B轨 n_leak | walk_forward | 缓存命中 | 指纹（含总 n） |",
          "|---|---|---|---|---|"]
    leak_bad = []
    for r in rows:
        b = (r.get("track结果") or {}).get("B") or {}
        wf = b.get("walk_forward")
        nl = b.get("n_leak")
        if nl not in (0, None):
            leak_bad.append((r.get("lottery"), nl))
        # 指纹里含 `|`，但它在 code span 内：`\|` 会被原样渲染成反斜杠，很难看
        # → 换成 `/`，视觉上仍是分隔符且不破坏表格。
        sig = str(r.get("sig", "—")).replace("|", "/")
        L.append(f"| {_md_escape(r.get('lottery'))} | "
                 f"{'—' if nl is None else nl} | "
                 f"{'—' if wf is None else ('是' if wf else '否')} | "
                 f"{'是' if r.get('_from_cache') else '否（本次重算）'} | "
                 f"`{sig[:22]}…` |")
    L += ["", f"- **n_leak 自检**：{'全部为 0 ✅' if not leak_bad else f'⚠️ 非 0：{leak_bad}'}"
               "（B 轨每期分位只用其之前的数据拟合 β，前视泄漏必须为 0）。"
               "数字型无 B 轨 → 该列显示 —，不是缺失。"]

    L += ["", "## 三、降级标注（不许静默降级）", ""]
    for r in rows:
        L.append(f"**{_md_escape(r.get('lottery'))}**")
        for x in _degradations(r):
            L.append(f"- {_md_escape(x)}")
        L.append("")

    L += ["## 四、C / D 轨（诊断，不参与判定）", "",
          "| 彩种 | C检验数 | C统计显著 | C实质偏离 | C分段不一致 | C结论 | D |",
          "|---|---|---|---|---|---|---|"]
    for r in rows:
        if not r.get("applicable"):
            continue
        c = (r.get("track结果") or {}).get("C") or {}
        d = (r.get("track结果") or {}).get("D") or {}
        stat = sum(x["统计显著"] for x in (c.get("族") or {}).values())
        dtag = ("有差异" if d.get("gap") else
                ("未检出" if d.get("applicable") else "不适用"))
        L.append(f"| {_md_escape(r['lottery'])} | {c.get('n检验', 0)} | {stat} | "
                 f"{len(c.get('实质偏离') or [])} | {len(c.get('分段不一致') or [])} | "
                 f"{_md_escape((c.get('结论') or '不适用')[:60])} | {dtag} |")

    L += ["", "## 五、裁决说明", ""]
    for r in rows:
        if r.get("applicable"):
            L.append(f"- **{_md_escape(r['lottery'])}**：{_md_escape((r.get('裁决') or {}).get('note'))}")
    L += ["", f"**跨彩种 BH(FDR) 检出**：{glob['检出彩种'] or '无'}", ""]

    L += ["## 六、选号倾斜状态（P4 管道）", "",
          "| 彩种 | γ | 显著 | 倾斜激活 | 原因 |", "|---|---|---|---|---|"]
    try:
        from ev.mechanism.gap_select import tilt_state as _ts
    except Exception:                                     # noqa: BLE001
        _ts = None
    for r in rows:
        g = ((r.get("gamma") or {}).get("gamma_合并") or {})
        if _ts is None:
            L.append(f"| {_md_escape(r.get('lottery'))} | — | — | — | tilt_state 不可用 |")
            continue
        st = _ts(r.get("lottery"), gamma=g.get("gamma"), significant=g.get("显著"))
        L.append(f"| {_md_escape(r.get('lottery'))} | {_fmt(g.get('gamma'))} | "
                 f"{'—' if g.get('gamma') is None else ('是' if g.get('显著') else '否')} | "
                 f"{'是' if st.get('倾斜') else '否'} | {_md_escape(st.get('原因', '—'))} |")
    L += ["", "> 倾斜未激活时，出号链路是**恒等映射**（与不做倾斜逐位相同）。",
          "> 这不是「已证明公平」，而是「当前测量不支持非零倾斜」。", ""]

    L += ["## 七、口径与限制", ""]
    for x in _CAVEAT_LINES:
        s = x.strip()
        if s.startswith("· "):
            L.append("- " + s[2:])
        elif L and L[-1].startswith("- "):
            # 文本版里靠缩进表示「上一条的续行」；md 里缩进会变成代码块，
            # 而每行加 `-` 会把它拆成假条目 —— 合并回上一条。
            L[-1] = L[-1] + " " + s
        else:
            L.append("- " + s)
    return "\n".join(L) + "\n"
