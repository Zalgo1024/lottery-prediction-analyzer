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
    "hn-robustness",                    # ⑨ 策略鲁棒性（11-04 新增）
    "hn-overlap",                       # ⑩ 臂间选号重合（11-07 新增）
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
    for key in ("术语", "卡片", "状态口径", "悬停提示", "口径说明", "诚实边界", "诚实声明"):
        assert key in glossary, "glossary 缺键 %s" % key
    assert glossary["悬停提示"], "「悬停提示」不能是空的（否则用户不知道要悬停 2 秒）"
    assert len(glossary["卡片"]) == 10
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


# ---------------------------------------------------------------- 悬停解释
def test_hover_uses_delayed_ring_not_instant_pop(client):
    """术语释义必须是「悬停读秒 → 圆环走满 → 才弹」，不是一碰就弹。

    ★ 为什么锁这条：一碰就弹的 tooltip 在鼠标扫过整屏时会疯狂闪烁，
      而**加了延迟却没有任何进度反馈**会被当成「功能坏了」。
      圆环进度条 + 词条底色扫描就是那个反馈，三者必须同时存在。
    """
    js = (WEB / "static" / "js" / "audit_explain.js").read_text(encoding="utf-8")
    css = (WEB / "static" / "css" / "audit.css").read_text(encoding="utf-8")
    html = client.get("/audit").get_data(as_text=True)

    # 1. 延迟时长：JS 里是唯一真源，且要写进 CSS 变量（CSS 不另写一个数）
    assert re.search(r"POP_DELAY\s*=\s*2000\b", js), "悬停延迟应为 2000ms"
    assert "--ring-dur" in js and "setProperty" in js, "延迟必须写进 CSS 变量 --ring-dur"
    assert re.search(r"var\(--ring-dur", css), "CSS 必须用 --ring-dur，不许另写一份时长"

    # 2. 圆环：元素 + 进度动画 + 走满关键帧
    assert 'id = "audit-ring"' in js and "startRing" in js and "stopRing" in js
    assert ".audit-ring" in css and ".ring-fill" in css
    assert "@keyframes audit-ring-fill" in css

    # 3. 词条在等待期间要有可见反馈（底色扫描），不能只是「没反应」
    assert "term--loading" in js and ".term--loading" in css
    assert "@keyframes audit-term-scan" in css

    # 4. mouseover 必须走读秒通道，不能绕过直接弹
    assert re.search(r"mouseover[\s\S]{0,200}armPop", js), "mouseover 没有走 armPop 读秒"
    assert re.search(r"S\.hoverTimer\s*=\s*setTimeout", js)
    # 移开要能取消，否则延迟到了会在错误的位置弹出
    assert "clearHoverTimer" in js

    # 5. 页面上要有个位置告诉用户「要悬停 2 秒」
    assert 'id="audit-hover-hint"' in html
    assert 'id="audit-drawer-hint"' in js

    # 6. 无障碍：读秒期间不能只靠鼠标（点击/键盘仍可立即弹出）
    assert re.search(r"focusin[\s\S]{0,160}showPop", js), "键盘聚焦应直接弹出，不走 2 秒延迟"
    assert "prefers-reduced-motion" in css

# ---------------------------------------------------------------- ⑨ 策略鲁棒性
def _write_rob_cache(path, *, age_days=0.0, retention=0.0):
    from datetime import datetime as _dt, timedelta as _td
    gen = (_dt.now() - _td(days=age_days)).isoformat(timespec="seconds")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "生成时间": gen,
        "彩种": {"双色球": {
            "判决": "退化", "基准优势_元每注": 0.0827, "前视检查": True,
            "期号": "26127", "K": 20, "判据版本": "2026-11-04-A", "原因": "无信息即无优势",
            "情景": [{"情景": "热度噪声σ=0.5", "优势_元每注": 0.0296, "保留率": 0.36,
                      "仍优于随机": True},
                     {"情景": "拥挤度缺失", "优势_元每注": 0.0, "保留率": retention,
                      "仍优于随机": False}]}},
    }, ensure_ascii=False), encoding="utf-8")


def test_robustness_api_未运行时不补零(client, monkeypatch, tmp_path):
    """缓存不存在 ⇒ 明确说「尚未运行」，**绝不返回一串 0**。

    ★ 0% 与「没测」是两件事：前者是结论（优势归零），后者是缺数据。
      混用会让读者以为策略「测得退化」，实际是压根没跑过。
    """
    import web.health_digest as H

    monkeypatch.setattr(H, "ROBUSTNESS_PATH", tmp_path / "none.json")
    d = client.get("/api/audit/robustness").get_json()
    assert d["可用"] is False
    assert "尚未运行" in d["原因"]
    assert "彩种" not in d, "没测过时不许带一份空/零的结果表"


def test_robustness_api_reads_cache_with_scenarios(client, monkeypatch, tmp_path):
    import web.health_digest as H

    p = tmp_path / "rob.json"
    _write_rob_cache(p)
    monkeypatch.setattr(H, "ROBUSTNESS_PATH", p)
    d = client.get("/api/audit/robustness").get_json()
    assert d["可用"] is True and d["生成时间"]
    it = d["彩种"]["双色球"]
    assert it["判决"] == "退化" and it["最差保留率"] == 0.0
    assert it["前视检查"] is True and it["期号"] == "26127"
    # 情景曲线要能透传（页面靠它展示「优势随失真衰减」，只有一个最差值无法判断）
    assert [s["情景"] for s in it["情景"]][:2] == ["热度噪声σ=0.5", "拥挤度缺失"]


def test_robustness_api_stale_cache_says_so(client, monkeypatch, tmp_path):
    """超过 3 天判陈旧 —— 陈旧数据不许冒充当前结论。"""
    import web.health_digest as H

    p = tmp_path / "old.json"
    _write_rob_cache(p, age_days=10)
    monkeypatch.setattr(H, "ROBUSTNESS_PATH", p)
    d = client.get("/api/audit/robustness").get_json()
    assert d["可用"] is False
    assert "未更新" in d["原因"]


def test_robustness_card_wired_into_page(client):
    """新卡必须真的被模板渲染、且被 loadAll 调用（只加术语不加载 = 永远空白）。"""
    html = client.get("/audit").get_data(as_text=True)
    assert 'id="hn-robustness"' in html
    assert 'data-card="robustness"' in html
    assert "loadRobustness(lot)" in html
    assert 'id="oneline-robustness"' in html

def test_robustness_api_separates_tested_from_skipped(client, monkeypatch, tmp_path):
    """已检验 / 未纳入必须分开给 —— 未纳入的彩种要带理由，不能凭空消失。"""
    import web.health_digest as H

    p = tmp_path / "mix.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({
        "生成时间": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "彩种": {
            "双色球": {"判决": "退化", "已测": True, "基准优势_元每注": 0.08,
                       "前视检查": True, "情景": [{"情景": "拥挤度缺失", "保留率": 0.0}]},
            "排列3": {"判决": "不适用", "已测": False, "情景": [],
                      "原因": "无浮动奖 ⇒ 优势只可能来自分奖"},
        },
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(H, "ROBUSTNESS_PATH", p)

    d = client.get("/api/audit/robustness").get_json()
    assert d["已检验"] == ["双色球"]
    assert d["未纳入"] == ["排列3"]
    assert d["彩种"]["排列3"]["已测"] is False
    assert "无浮动奖" in d["彩种"]["排列3"]["原因"]
    # 未纳入的行不许伪造数值
    assert d["彩种"]["排列3"]["最差保留率"] is None
