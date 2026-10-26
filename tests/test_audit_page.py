"""
`/audit` 测量与证据看板 —— 页面与术语接口的守卫测试。

★ 为什么要有这个文件
  本轮把页面从「数据体检看板」重做成「测量与证据看板」，同时把原本**手抄**在模板里的
  四处免责文案、以及卡片的名词解释，统一收口到 `web/audit_glossary.py`。
  这类改动最容易出的两类回归：
    1. 有人又把文案抄回模板或 JS —— 两份各自过期；
    2. 有人把导航改回「数据体检」，让界面重新开始承诺一件它不做的事。
  下面两组断言分别锁死这两件事。

★ 全部断言都是**纯字符串 / dict 检查**，不触发任何真实拟合，
  因此不会被 `pytest.ini` 的 realdata / statsmodels / winfont 标记 skip 成假绿。

不承诺任何中奖率，仅是界面与文案结构校验。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from web.app import app
from web.nav import nav_lookup

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"

CARD_IDS = (
    "hn-quality", "hn-ev", "hn-pl", "hn-distance",
    "hn-payout", "hn-trials", "hn-attribution", "hn-gap",
)
# 旧页面名（改名后不许在 web/ 里再出现）
OLD_PAGE_NAMES = ("数据体检看板",)


@pytest.fixture(scope="module")
def client():
    return app.test_client()


@pytest.fixture(scope="module")
def glossary(client):
    r = client.get("/api/audit/glossary")
    assert r.status_code == 200
    return r.get_json()


# ---------------------------------------------------------------- 页面骨架
def test_audit_page_renders_new_structure(client):
    html = client.get("/audit").get_data(as_text=True)
    assert "测量与证据看板" in html
    # 八张卡的容器 id 一个都不能改（loader 与 explain 层都按它定位）
    for cid in CARD_IDS:
        assert 'id="%s"' % cid in html, "少了容器 %s" % cid
    for node in ("audit-sum-grid", "audit-anchors-list", "audit-page-caveat",
                 "btn-explain", "btn-glossary", "audit-cards"):
        assert 'id="%s"' % node in html, "少了节点 %s" % node
    # 抽出来的两份静态资源必须真的被引进来
    assert "css/audit.css" in html
    assert "js/audit_explain.js" in html


def test_audit_page_has_no_leftover_inline_card_boxes(client):
    """八张卡曾经把同一段盒子样式内联抄了八遍。改成 CSS 类后，这种串必须是 0 次。"""
    html = client.get("/audit").get_data(as_text=True)
    assert "border:1px solid var(--color-border-secondary" not in html
    assert '#534AB7' not in html and '#B4B2A9' not in html
    # `.badge` 在 style.css 里从未定义过，页面上不该再出现
    assert 'class="badge ' not in html


def test_static_audit_assets_are_served(client):
    for rel in ("css/audit.css", "js/audit_explain.js"):
        r = client.get("/static/" + rel)
        assert r.status_code == 200, "静态资源取不到：%s" % rel


# ---------------------------------------------------------------- 术语接口
def test_glossary_api_contract(glossary):
    """沿用项目三键惯例：`口径说明` / `诚实边界` / `诚实声明`。"""
    for key in ("术语", "卡片", "状态口径", "口径说明", "诚实边界", "诚实声明"):
        assert key in glossary, "glossary 缺键 %s" % key
    assert len(glossary["卡片"]) == 8
    assert [c["id"] for c in glossary["卡片"]] == list(CARD_IDS)


def test_glossary_card_terms_are_not_dangling(glossary):
    """每张卡引用的名词必须真的存在于术语表，否则前端会渲染出空词条。"""
    terms = glossary["术语"]
    for c in glossary["卡片"]:
        for name in c["名词"]:
            assert name in terms, "%s 引用了不存在的术语：%s" % (c["序号"], name)


def test_glossary_terms_have_required_fields(glossary):
    for key, t in glossary["术语"].items():
        assert t.get("名") and t.get("类") and t.get("一句话"), key
        assert t["类"] in ("数据", "统计"), key
        # 单字符/极易误伤的词必须限 scope，否则整页都是虚线
        if len(t["名"]) <= 2:
            assert t.get("scope") and t["scope"] != ["*"], "%s 未限定 scope" % key


def test_glossary_cites_single_source_not_copies(glossary):
    """诚实边界必须是 narrative 常量的**引用**，不是近似重写。

    ★ 此前 ⑤⑥⑦⑧ 四张卡的免责文案是手抄的，与后端原文逐字不同。
      一旦有人在某个表扬/警告分支里改了措辞，这份漂移就再也看不出来。
    """
    from ev.attribution import HONEST_NOTE as ATTRIBUTION_HONEST_NOTE
    from ev.mechanism.narrative import (
        EV_NEGATIVE_CAVEAT,
        NO_SIGNAL_CAVEAT,
        RANDOMNESS_PREMISE_CAVEAT,
    )

    blob = json.dumps([c["诚实边界"] for c in glossary["卡片"]], ensure_ascii=False)
    assert NO_SIGNAL_CAVEAT in blob, "缺少 NO_SIGNAL_CAVEAT 原文"
    assert EV_NEGATIVE_CAVEAT in blob, "缺少 EV_NEGATIVE_CAVEAT 原文"
    assert RANDOMNESS_PREMISE_CAVEAT in blob, "缺少 RANDOMNESS_PREMISE_CAVEAT 原文"
    assert ATTRIBUTION_HONEST_NOTE in blob, "缺少归因 HONEST_NOTE 原文"


def test_glossary_text_passes_banned_scan(glossary):
    """术语文案同样在 web/ 目录下 ⇒ 必须过那把「禁止肯定式断言」的尺。"""
    from tests.test_narrative_unbinding import _is_hard_assertion

    lines = []
    for t in glossary["术语"].values():
        lines += [t.get("一句话", ""), t.get("口径", "")]
    for c in glossary["卡片"]:
        lines += [c.get("在测什么", ""), c.get("口径说明", "")]
        lines += (c.get("诚实边界") or "").splitlines()
    for i, ln in enumerate(lines):
        assert not _is_hard_assertion(ln), "第 %d 行触到禁词：%s" % (i, ln)


def test_status_hint_covers_every_status(glossary):
    """六个状态各自都要有一句话人话解释，且不许留空占位。"""
    hint = glossary["状态口径"]
    for st in ("ok", "warn", "none", "nodata", "unmeasured", "error"):
        assert hint.get(st, "").strip(), "状态 %s 没有解释" % st


# ---------------------------------------------------------------- 改名 + 去红
def test_audit_nav_entry_is_renamed_and_not_flagged_danger():
    it = nav_lookup()["/audit"]
    assert it["label"] == "证据看板"
    assert it["side"] == "测量与证据看板"
    assert len(it["label"]) <= 6, "顶栏短名超 6 字会挤换行"
    # ★ 本页是只读看板，红色（danger）会误导成"有异常需要处理"
    assert it["danger"] is False, "/audit 不该再被标红"


def test_old_page_name_gone_from_web():
    """旧的**页面名**在 web/ 里必须清零。

    ★ 「数据体检」这个词本身可以留下，但只能作为指向离线脚本的边界说明
      （它确实是 `scripts/health_check.py` 干的事），不能作为本页的名字。
    """
    offenders, boundary_ok = [], []
    for p in WEB.rglob("*"):
        if p.suffix not in (".py", ".html", ".js") or "__pycache__" in p.parts:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        for bad in OLD_PAGE_NAMES:
            if bad in text:
                offenders.append((p.relative_to(ROOT), bad))
        if "数据体检" in text:
            # 允许留下的唯一理由：明确指向离线体检脚本
            if "health_check.py" in text or "scan_data_integrity.py" in text:
                boundary_ok.append(p.relative_to(ROOT))
            else:
                offenders.append((p.relative_to(ROOT), "裸用了「数据体检」却没指向离线脚本"))
    assert not offenders, "改名没改干净：%s" % offenders
    assert boundary_ok, "至少应有一处说明「数据体检」在别处"


def test_offline_health_check_still_owns_the_word():
    """改名是为了让出这个词，不是删掉这个功能。"""
    src = (ROOT / "scripts" / "health_check.py").read_text(encoding="utf-8", errors="ignore")
    assert "数据体检" in src
    # 体检报告里的字段名不能被顺手改掉
    assert "已隔离" in src


# ---------------------------------------------------------------- 渐变保卫
def test_explain_layer_exposes_the_documented_api():
    """`audit_explain.js` 的公开 API 必须与模板里的调用一致（改名会立刻炸）。"""
    src = (WEB / "static" / "js" / "audit_explain.js").read_text(encoding="utf-8")
    tpl = (WEB / "templates" / "audit.html").read_text(encoding="utf-8")
    for fn in ("init", "toggleExplain", "onCardRendered", "toggleCard",
               "scrollToCard", "openDrawer", "expandAll", "collapseAll"):
        assert fn + ":" in src, "audit_explain.js 少了 %s" % fn
    for call in ("EXPL.init()", "EXPL.onCardRendered", "EXPL.toggleCard",
                 "EXPL.toggleExplain()", "EXPL.openDrawer()"):
        assert call in tpl, "模板没调用 %s" % call


def test_disclaimer_text_has_no_second_copy():
    """说明文案不许出现第二份。

    ★ 判据：取 narrative 常量里一段**足够长**的特征串，要求它在 JS 与模板里
      都不出现。用长串是为了不误伤字段名的正常引用（比如 status 字面量 "未检出"）。
    """
    from ev.mechanism.narrative import NO_SIGNAL_CAVEAT

    # 取常量中部一段 20 字的特征片段（避开首尾，防止只改标点就绕过）
    core = NO_SIGNAL_CAVEAT[8:28]
    assert len(core) >= 15
    for rel in ("static/js/audit_explain.js", "templates/audit.html"):
        text = (WEB / rel).read_text(encoding="utf-8")
        assert core not in text, "%s 里又抄了一份免责文案" % rel
    src = (WEB / "static" / "js" / "audit_explain.js").read_text(encoding="utf-8")
    assert re.search(r"fetch|XMLHttpRequest", src), "解释层必须去后端取文案"


# ---------------------------------------------------------------- 文档同步
def test_report_glossary_rows_match_terms_verbatim():
    """结题报告 8.5.1 表的「大白话」列必须与 `terms()` 的 `一句话` **逐字一致**。

    ★ 为什么锁这条：`docs/结题报告.md` 里的这一列本质上是术语源的**第二份拷贝**，
      而拷贝的宿命就是漂移——改了 `web/audit_glossary.py` 忘了改文档，报告就开始
      讲一套、页面上讲另一套。这里用解析 markdown 表格的方式逐格比对，
      少一个字、多一个标点都会红。

    ★ 解析规则与文档写法配套：表格单元格里**不许出现竖线**（写成「反斜杠 + 竖线」的
      转义会让这里的朴素切分多出假列，本测试就会静默少读一行，反而假绿 ⇒ 文档里一律
      改成「统计量绝对值≥3」这种不含竖线的写法）。
    """
    from web.audit_glossary import terms

    doc_path = ROOT / "docs" / "结题报告.md"
    assert doc_path.exists(), "结题报告不见了"
    doc = doc_path.read_text(encoding="utf-8")

    marker = "#### 8.5.1"
    assert marker in doc, "结题报告缺 8.5.1 看板术语小节"
    block = doc[doc.index(marker):]
    nxt = block.find("\n### ", 10)
    if nxt > 0:
        block = block[:nxt]

    T = terms()
    rows = 0
    for line in block.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != 3:
            continue
        name, _calib, plain = cells
        if name in ("术语", "") or set(name) <= set("- "):
            continue
        assert "|" not in _calib, "单元格里不许出现竖线（会让表格解析错位）: %s" % name
        hit = name if name in T else next(
            (k for k, v in T.items() if name in (v.get("别名") or [])), None)
        assert hit is not None, "报告里的术语 %r 在术语表里找不到（改名要同步）" % name
        assert T[hit]["一句话"] == plain, (
            "术语 %r 与术语源不一致：\n  报告: %r\n  术语源: %r"
            % (hit, plain, T[hit]["一句话"]))
        rows += 1

    # 数量下限：防止有人整段删掉后本测试因为循环 0 次而假绿
    assert rows >= 30, "8.5.1 表只解析出 %d 行，疑似被删或格式被改坏" % rows
