"""
P3 叙事解绑（`ev/mechanism/narrative.py`）—— 文案契约锁。

——
为什么单独立一个测试文件
——
P3 改的是**文案**，不是逻辑。这类改动最容易被后来者当"啰嗦"精简掉，
而精简之后项目会静默退回到「已证明不可预测」的旧叙事——四轨测量能力
（`ev/mechanism/`）在逻辑上会被自己的文案提前否掉。所以这里逐条锁死。

——
改文案时该怎么动本文件
——
  · 原句被**别处**的测试锁定（见 `_LOCKED`），动一个字那边就会红 —— 那是故意的。
  · 从句来自 `ev.mechanism.narrative` 的常量，改措辞请改常量（单一真源），
    不要在各模块里就地改写。
  · 加新的 HONEST_NOTE 时，往 `_NOTES` 里补一项即可自动纳入第 2、3 条断言。

——
刻意不测的东西
——
不测「文案读起来通顺」，也不测具体中文措辞 —— 那是人该审的。
这里只锁**结构性契约**：从句在不在、原子串还在不在、有没有时点状态、
有没有冒出本体论硬断言。
"""
import inspect
import re
from pathlib import Path

import pytest

from ev import attribution as AT
from ev import coverage as CV
from ev import stake as SK
from ev import threshold as TH
from ev import total_ev as TE
from ev.mechanism import narrative as NR
from prediction import robust_tiers as RT

# ------------------------------------------------------------------ 常量：被别处测试锁定的原子串
# 这些子串丢失 = 别处的诚实边界被一起丢掉。P3 要丢的是「不可能」那层意思，不是边界。
_LOCKED = {
    "coverage": (CV.HONEST_NOTE, "不提升任何单注中奖概率"),
    "attribution": (AT.HONEST_NOTE, "不改变任何预测逻辑"),
    "robust_tiers": (RT.HONEST_NOTE, "不代表号码中奖概率"),
    "stake": (SK._HONEST_NOTE, "纪律不下注"),
    "threshold": (TH._HONEST_NOTE, "不因门槛达成而改变"),
    "total_ev": (TE._HONEST_NOTE, "不预测号码"),
    # 奖级达标策略（Phase B/C/D）。它自带作用域说明并以 EV_NEGATIVE_CAVEAT 收尾，
    # 因此自动满足第 ② 条「每个 HONEST_NOTE 都带解绑从句」。
    "tier_coverage": (NR.TIER_COVERAGE_CAVEAT, "不改变任何一张票自己的中奖概率"),
}

# 七处（模块名 → 文案）。新增 HONEST_NOTE 请在这里登记。
# ★ 三件套 _CAVEATS 仍是 3 条，别把「奖级达标」那条塞进去 —— 它有自己的作用域说明。
_NOTES = {k: v[0] for k, v in _LOCKED.items()}

_CAVEATS = (NR.RANDOMNESS_PREMISE_CAVEAT, NR.NO_SIGNAL_CAVEAT,
            NR.EV_NEGATIVE_CAVEAT)


# ------------------------------------------------------------------ ① 从句单一真源
def test_caveats_are_strings_and_share_one_tracks_line():
    """三个从句都是非空字符串，且共用同一份「四轨职责」说明（避免各写各的）。"""
    for c in _CAVEATS:
        assert isinstance(c, str) and len(c) > 30
    line = NR._TRACKS_LINE
    for c in _CAVEATS:
        assert line in c, "从句没有引用 _TRACKS_LINE，四轨职责会被复制成多份"


def test_caveats_point_to_the_measure_command():
    """从句必须指明去哪儿看当前状态，否则读者无从更新。"""
    for c in _CAVEATS:
        assert NR.MEASURE_COMMAND in c


def test_caveats_separate_judging_from_diagnostic_tracks():
    """★ 从句必须写明 C/D 只作诊断、不参与判定。

    若含糊成「四轨都判」，读者会把 C 轨的结构偏差当缝隙证据——而 C 轨 `gap`
    恒为 False 正是因为它不参与判定。
    """
    assert "参与判定" in NR._TRACKS_LINE
    assert "不参与判定" in NR._TRACKS_LINE
    for c in _CAVEATS:
        assert "只作诊断" in c


# ------------------------------------------------------------------ ② 六处都带上了从句
@pytest.mark.parametrize("name", sorted(_NOTES))
def test_every_honest_note_carries_a_caveat(name):
    note = _NOTES[name]
    assert any(c in note for c in _CAVEATS), (
        f"{name} 的 HONEST_NOTE 没带解绑从句 —— P3 漏改或被精简掉了")


# ------------------------------------------------------------------ ③ 原子串一个都不能丢
@pytest.mark.parametrize("name", sorted(_LOCKED))
def test_locked_substrings_survive(name):
    note, sub = _LOCKED[name]
    assert sub in note, (
        f"{name} 丢掉了被别处测试锁定的子串「{sub}」——那是诚实边界，不是钢印")


# ------------------------------------------------------------------ ④ 从句不含时点状态
@pytest.mark.parametrize("c", _CAVEATS, ids=["premise", "no_signal", "ev"])
def test_caveat_has_no_point_in_time_claim(c):
    """★ 从句不得写「当前未检出 / 至今未测到」这类时点状态。

    理由与 C 轨 p 地板字段同款：写进静态文案后，数据一更新它就开始说谎，
    而**崩报告比降级更糟**。当前状态请跑 `python cli.py gap`（P5 起由看板带出）。
    """
    for bad in ("当前未检出", "至今未检出", "目前已检出", "当前测量未", "本次测量未检出"):
        assert bad not in c, f"从句含时点状态「{bad}」，数据更新后会说谎"


# ------------------------------------------------------------------ ⑤ 判定层文案
def test_anti_crowd_negative_branch_carries_no_signal_caveat():
    """anti_crowd 的「未检出」分支必须带从句，且**判定逻辑一点没动**。

    ⚠️ 断言的是源码里对常量的**引用**（`+ NO_SIGNAL_CAVEAT`），不是拼接后的文本
    —— 拼接只在运行时发生，源码里看到的是变量名。
    """
    import credibility.anti_crowd as AC
    src = inspect.getsource(AC)
    assert "NO_SIGNAL_CAVEAT" in AC.__dict__, "常量没 import 进来"
    assert "+ NO_SIGNAL_CAVEAT" in src, "未检出分支没接上从句"
    # 判定逻辑不许动：分支条件与分支数保持原样（P3 只改文案）
    assert "if mw_p < 0.05 and pop_med > non_med:" in src, "判定条件被动了"
    assert src.count("interp = (") == 2, "分支数变了（应仍是 检出 / 未检出 两条）"


def test_report_verdict_negative_branch_drops_ontology():
    """★ credibility/report.py 的 else 分支：本体论断言必须已移除，边界必须还在。

    原句「与『公平随机开奖不可被稳定预测』一致」把「本次没检出」说成了「世界
    不可能」——这是 P3 要拆的那枚钢印。但「价值在纪律化投注与诚实自检，
    而非提高中奖率」是有价值的边界，必须保留。
    """
    import credibility.report as RP
    src = inspect.getsource(RP)
    assert "不可被稳定预测" not in src, "本体论断言又回来了"
    assert "纪律化投注与诚实自检" in src, "诚实边界被一起删掉了"
    assert "+ NO_SIGNAL_CAVEAT" in src, "verdict 的 else 分支没接上从句"
    # 判定逻辑不许动：四档分支的判定式保持原样
    for cond in ("elif any_pract:", "elif any_stat:", "else:"):
        assert cond in src, f"分支结构被动了：缺 {cond}"


# ------------------------------------------------------------------ ⑥ 全仓不得冒出本体论硬断言
_BANNED = ("不可预测", "不可能预测", "无法预测", "不可被稳定预测",
           "不可稳定预测", "无从预测", "已被证明公平", "已证明公平")
# 同行出现这些标记说明是**否定式引用/历史描述**，放行
_EXEMPT = ("≠", "不表示", "未检出", "不代表", "不构成", "原句", "旧文案",
           "已废弃", "不是数学必然", "不等于",
           # 「这不是『已证明公平』，而是…」——否定式引用，语义正是我们要的。
           # 扫描器抓的是**肯定式**断言，不能把纠正它的句子一起抓了。
           "这不是", "并非", "不意味着",
           # 「绝不能写成『已证明公平』」——这是**纪律声明**，不是断言本身。
           "绝不能", "不许", "不得写")
# narrative.py 是「元文案」模块，它的 docstring 描述旧文案长什么样，属于被引用的样本
_SKIP_FILES = {"narrative.py"}


def _py_files():
    """★ 不只扫 .py —— 前端模板/JS 里同样会冒出钢印。

    实测漏网：`web/templates/robustness.html` 长期印着「号码不可预测，L0 结论不变」，
    因为扫描器只 rglob("*.py")。文案纪律不能以文件后缀为边界。
    """
    root = Path(__file__).resolve().parent.parent
    for d in ("ev", "prediction", "credibility", "web"):
        for ext in ("*.py", "*.html", "*.js"):
            for p in sorted((root / d).rglob(ext)):
                if "__pycache__" in p.parts or "node_modules" in p.parts:
                    continue
                yield p


def _is_hard_assertion(line: str) -> bool:
    """命中黑名单**且**没有否定/历史标记 ⇒ 肯定式本体论硬断言。

    抽成函数是为了能被单独测：加豁免词时若不补一条「肯定式仍被抓到」的用例，
    豁免表会越加越宽，最后整条契约变成假绿。
    """
    return (any(b in line for b in _BANNED)
            and not any(e in line for e in _EXEMPT))


def test_no_ontological_hard_assertions_in_code():
    """★ 源码里不得出现**肯定式**的「开奖不可预测 / 已证明公平」断言。

    这是 P3 的主契约：允许写「未检出」「不代表」「限于已测维度」，
    不允许写「不可能」。命中即失败（除非同行含否定标记，或位于 narrative.py）。
    """
    hits = []
    for p in _py_files():
        if p.name in _SKIP_FILES:
            continue
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeDecodeError):      # noqa: B014
            continue
        for i, ln in enumerate(lines, 1):
            if _is_hard_assertion(ln):
                hits.append(f"{p.relative_to(Path(__file__).resolve().parent.parent)}:{i}: {ln.strip()}")
    assert not hits, "出现本体论硬断言（肯定式）：\n  " + "\n  ".join(hits)


def test_exempt_list_does_not_swallow_real_assertions():
    """豁免表自检：放行否定式，但**肯定式**一个都不许漏。"""
    # 肯定式 —— 必须被抓。★ 用**能连续命中 _BANNED** 的串：
    # 匹配是子串匹配，「已证明开奖是公平的」（中间插字）本就抓不到，
    # 那是黑名单的既有覆盖面问题，不该靠这条用例假装它被覆盖了。
    for bad in ("彩票开奖不可预测。",
                "已证明公平，无需再测。",
                "中奖号码无从预测，不必再测。"):
        assert _is_hard_assertion(bad), f"肯定式漏网：{bad}"
    # 否定式 —— 必须放行
    for ok in ("这不是「已证明公平」，而是当前测量不支持非零倾斜。",
               "未检出偏离 ≠ 证明开奖公平。",
               "这不代表号码中奖概率被改变。"):
        assert not _is_hard_assertion(ok), f"否定式被误抓：{ok}"


def test_ban_list_is_not_trivially_empty():
    """扫描器自检：若 _py_files() 什么都没扫到，上一条就是假绿。"""
    files = list(_py_files())
    assert len(files) > 30, f"只扫到 {len(files)} 个文件，扫描范围写错了"


# ------------------------------------------------------------------ ⑦ 规划文档的作用域补记
_PLAN = (Path(__file__).resolve().parent.parent
         / "credibility" / "整合可执行方案_2026-08-25.md")


def test_planning_md_drops_write_in_stone_wording():
    """内部规划文档里的「（写死，不再讨论）」「（写死）」必须已换成作用域限定。"""
    txt = _PLAN.read_text(encoding="utf-8")
    assert "（写死，不再讨论）" not in txt
    assert "（写死）" not in txt
    assert txt.count("作用域补记") >= 2, "两处作用域补记缺了"
    for kw in ("未检出偏离 ≠ 证明开奖公平", "名义返奖率"):
        assert kw in txt, f"补记里缺关键口径「{kw}」"


def test_planning_md_scope_notes_cite_the_measure_command():
    txt = _PLAN.read_text(encoding="utf-8")
    assert NR.MEASURE_COMMAND in txt


# ------------------------------------------------------------------ ⑧ 模块导出
def test_narrative_is_exported_by_the_package():
    """`ev.mechanism.__all__` 必须含 narrative，否则它会变成新的悬空件。"""
    from ev import mechanism as M
    assert "narrative" in M.__all__
    import importlib
    for name in M.__all__:
        assert importlib.util.find_spec(f"ev.mechanism.{name}") is not None, name


def test_no_duplicate_caveat_text_across_modules():
    """★ 从句必须在**模块加载时**拼进文案，而不是各自手抄一份。

    手抄会导致日后改常量时各模块文案漂移（正是 P3 要治的病）。
    """
    for name, note in _NOTES.items():
        hit = [c for c in _CAVEATS if c in note]
        assert len(hit) == 1, f"{name} 的从句匹配了 {len(hit)} 个常量，拼接方式可疑"
        # 从句必须**整段**出现（不是被改写过）
        assert note.endswith(hit[0]), f"{name} 的从句不在末尾或被改写过"


def test_caveat_regex_does_not_leak_unbalanced_backticks():
    """文案里 ` 必须成对（渲染成 markdown 时不会破版）。"""
    for c in _CAVEATS:
        assert c.count("`") % 2 == 0, f"反引号未成对：{c[:40]}…"
