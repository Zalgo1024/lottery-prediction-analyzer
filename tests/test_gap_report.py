"""
P7 测试：四轨测量的 Markdown 报告（ev/gap.py report_markdown）

★ 全程 monkeypatch `ev.gap.measure_gap_global` 注入假测量：
根 conftest 把 gap_cache.CACHE_DIR 隔离到 tmp ⇒ 真跑会触发 6 彩种 × 十几秒的
完整测量，测试会又慢又依赖真数据。而且本报告测的是**渲染与披露**，
不是测量本身（测量有 tests/test_gap_*.py 覆盖）。

重点锁三块，都是文本版没有、但决策要用的：
① n_leak 自检（前视泄漏必须为 0，非 0 必须显式告警，不许悄悄过去）；
② 降级标注（任何轨不可用都必须列出，不许静默降级）；
③ 口径与限制与 report_text **同源**（_CAVEAT_LINES 共用，不两处各写各的）。
"""
import re

import pytest

from ev import gap as G

LOTS = ["双色球", "大乐透", "排列5", "七星彩", "排列3", "福彩3D"]


def _row(lot, *, applicable=True, note=None, digital=False, tracks=("A", "B"),
         status="none", R=0.99, ci=(0.96, 1.02), u=0.5, uci=(0.49, 0.51),
         gamma=-0.01, gamma_sig=False, n_leak=0, wf=True, model_go=True,
         a_applicable=True, c_applicable=True, d_applicable=True):
    t = {}
    if applicable:
        a = {"applicable": a_applicable,
             "主口径": {"R": R, "ci95": ci, "usable": a_applicable},
             "判定": {"gap": False}}
        if not a_applicable:
            a["note"] = "销售额覆盖全部玩法"
        t["A′" if digital else "A"] = a
        if not digital:
            t["B"] = {"applicable": True, "walk_forward": wf, "n_leak": n_leak,
                      "mean_u": u, "ci95": uci, "模型Go": model_go, "gap": False}
        t["C"] = {"applicable": c_applicable, "n检验": 3, "族": {}, "实质偏离": [],
                  "分段不一致": [], "结论": "未发现实质结构偏离"}
        t["D"] = {"applicable": d_applicable, "gap": False}
    return {
        "lottery": lot, "applicable": applicable,
        "note": note or ("不适用" if not applicable else ""),
        "n": 3000, "sig": f"v3|{lot}|n=3000|last=x", "_from_cache": True,
        "轨类型": "数字型" if digital else "乐透型",
        "track结果": t,
        "裁决": {"status": status, "判定轨": list(tracks) if applicable else [],
                 "应有判定轨": list(tracks), "n_判定轨": 3000},
        "gamma": {"gamma_合并": {"gamma": gamma, "显著": gamma_sig}},
    }


def _glob(rows):
    return {"逐彩种": rows, "检出彩种": []}


@pytest.fixture
def fake(monkeypatch):
    """注入假测量，返回可控的假数据构造器。"""
    holder = {}

    def _set(rows):
        holder["rows"] = rows

    monkeypatch.setattr(G, "measure_gap_global",
                        lambda *a, **k: _glob(holder["rows"]))
    _set([_row(l, digital=(l in ("排列5", "七星彩", "排列3", "福彩3D"))) for l in LOTS])
    return holder


# ------------------------------------------------------------------ ① 结构
def test_report_has_all_sections(fake):
    md = G.report_markdown()
    for sec in ("## 一、主表", "## 二、自检", "## 三、降级标注",
                "## 四、C / D 轨", "## 五、裁决说明", "## 六、选号倾斜状态",
                "## 七、口径与限制"):
        assert sec in md, f"缺章节 {sec}"


def test_report_has_reproduce_command(fake):
    """报告必须自带复现命令——否则读者拿到一堆数字不知道怎么重算。"""
    md = G.report_markdown()
    assert "python cli.py gap" in md
    assert "--min-train" in md and "--min-effect" in md


def test_reproduce_command_reflects_no_cache(fake):
    md = G.report_markdown(force=True)
    assert "--no-cache" in md


def test_all_lotteries_present(fake):
    md = G.report_markdown()
    for l in LOTS:
        assert l in md


# ------------------------------------------------------------------ ② n_leak 自检
def test_n_leak_zero_is_reported(fake):
    md = G.report_markdown()
    assert "全部为 0 ✅" in md
    assert "⚠️" not in md


def test_n_leak_nonzero_is_flagged(fake):
    """★ n_leak 非 0 必须显式告警——前视泄漏会让 B 轨结论失效，不能悄悄过去。"""
    rows = [_row("双色球", n_leak=7)]
    fake["rows"] = rows
    md = G.report_markdown(["双色球"])
    assert "⚠️" in md and "7" in md
    assert "全部为 0 ✅" not in md


def test_n_leak_dash_for_digital(fake):
    """数字型无 B 轨 → 显示 —，并说明不是缺失。"""
    fake["rows"] = [_row("排列5", digital=True)]
    md = G.report_markdown(["排列5"])
    assert "数字型无 B 轨" in md


# ------------------------------------------------------------------ ③ 降级标注
def test_no_track_lottery_is_degraded_explicitly(fake):
    """排列3/福彩3D 无判定轨，必须写明是「结构上不可判定」，不是「测过没测到」。"""
    fake["rows"] = [_row("排列3", digital=True, tracks=(), status="no-track")]
    md = G.report_markdown(["排列3"])
    assert "结构上不可判定" in md
    assert "无判定轨" in md


def test_inapplicable_track_is_degraded(fake):
    fake["rows"] = [_row("双色球", a_applicable=False)]
    md = G.report_markdown(["双色球"])
    assert "A 轨不可用" in md
    assert "销售额覆盖全部玩法" in md


def test_model_not_go_is_degraded(fake):
    fake["rows"] = [_row("双色球", model_go=False)]
    md = G.report_markdown(["双色球"])
    assert "未过 Go 门槛" in md


def test_gamma_insignificant_is_degraded(fake):
    """γ 非零但不显著 → 必须写明按 γ=0 处理（这是 P4 红线）。"""
    fake["rows"] = [_row("双色球", gamma=-0.0185, gamma_sig=False)]
    md = G.report_markdown(["双色球"])
    assert "未过显著门" in md and "按 γ=0 处理" in md


def test_no_degradation_says_so(fake):
    fake["rows"] = [_row("七星彩", digital=True, tracks=("A′",),
                         d_applicable=False)]
    md = G.report_markdown(["七星彩"])
    assert "**七星彩**" in md


def test_whole_lottery_inapplicable(fake):
    fake["rows"] = [{"lottery": "排列3", "applicable": False, "note": "数据缺失"}]
    md = G.report_markdown(["排列3"])
    assert "整条不适用" in md and "数据缺失" in md


# ------------------------------------------------------------------ ④ 表格完整性
def _table_rows(md, header_prefix):
    """取某表头之后的所有 | 开头行。"""
    lines = md.splitlines()
    out, on = [], False
    for ln in lines:
        if ln.startswith(header_prefix):
            on = True
            continue
        if on:
            if ln.startswith("|"):
                out.append(ln)
            elif out:
                break
    return out


def test_tables_have_consistent_columns(fake):
    """★ 单元格里的 `|` 会撑破表格——逐表检查列数一致。"""
    md = G.report_markdown()
    for hdr in ("| 彩种 | 型 |", "| 彩种 | B轨 n_leak |", "| 彩种 | C检验数 |",
                "| 彩种 | γ |"):
        rows = _table_rows(md, hdr)
        assert rows, f"表 {hdr} 没取到行"
        widths = {len(r.strip().strip("|").split("|")) for r in rows}
        assert len(widths) == 1, f"表 {hdr} 列数不一致：{widths}"


def test_no_escaped_pipe_inside_code_span(fake):
    """指纹里的 `|` 已换成 `/`，不该出现 `\\|`（反斜杠会被原样渲染）。"""
    md = G.report_markdown()
    assert "\\|" not in md


def test_no_raw_pipe_in_fingerprint(fake):
    """指纹里的 `|` 已换成 `/`（code span 内转义会原样显示反斜杠）。

    ★ 字符类必须排除换行：`[^`]` 会跨行把「末尾反引号…下一段开头反引号」
    当成一个 code span，误报。
    """
    md = G.report_markdown()
    assert "v3/" in md, "指纹里的 | 未换成 /"
    assert re.search(r"`[^`\n]*\|[^`\n]*`", md) is None


# ------------------------------------------------------------------ ⑤ 与文本版同源
def test_caveats_shared_with_text_report(fake):
    """★ 口径与限制必须两版同源——否则改一处就会两处说法不一致。"""
    md = G.report_markdown()
    txt = G.report_text()
    # 文本版每一条口径都要出现在 md 版里（去掉项目符号与缩进差异后比对）
    for c in G._CAVEAT_LINES:
        core = c.strip().lstrip("· ").strip()
        assert core in txt, f"文本版缺：{core}"
        assert core.split("（")[0][:20] in md.replace("- ", ""), f"md 版缺：{core}"


def test_caveat_continuation_lines_are_merged(fake):
    """文本版靠缩进表示续行；md 版每行加 `-` 会拆成假条目。"""
    md = G.report_markdown()
    body = md.split("## 七、口径与限制", 1)[1]
    bullets = [l for l in body.splitlines() if l.startswith("- ")]
    # 续行合并后条目数应等于「· 」开头的条数
    assert len(bullets) == sum(1 for c in G._CAVEAT_LINES if c.strip().startswith("· "))


def test_verdict_displayed_in_chinese(fake):
    """status='none' 应显示成中文，不能把内部枚举直接印出来。"""
    fake["rows"] = [_row("排列3", digital=True, tracks=(), status="no-track"),
                    _row("双色球", status="none")]
    md = G.report_markdown(["排列3", "双色球"])
    assert "无判定轨" in md
    assert "未检出" in md
    assert "| none |" not in md


def test_gamma_none_shows_dash_not_insignificant(fake):
    """γ 缺失 = 本彩种无估计；显示成「不显著」会让人以为估计过了。"""
    fake["rows"] = [_row("大乐透", gamma=None)]
    md = G.report_markdown(["大乐透"])
    row = [l for l in md.splitlines() if l.startswith("| 大乐透 |")][0]
    assert "—" in row


# ------------------------------------------------------------------ ⑥ CLI
def test_cli_gap_has_out_option():
    import inspect
    src = inspect.getsource(__import__("cli"))
    assert '"--out"' in src or "'--out'" in src
