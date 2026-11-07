# -*- coding: utf-8 -*-
"""出号设置页（`/gen`）的契约锁。

为什么单独一个页面 + 单独一份测试
--------------------------------
「出号设置」的信息量已经超过首页那张卡的承载能力：注数、上限、配置锚点、
动态值、实际出号、目标期号、目标开奖日、批次与重出批号、预算包络，
再加上「设置 ≠ 实际」的差额。挤在一张卡里迟早会有某一列被省略 ——
而省略的那一列恰恰是判据（例如「实际出号」被省略后，「注数被换掉」这件事
在界面上就彻底不可见了）。

页面组成：`web/templates/gen.html`（外壳 + 样式，走 base.html 带侧栏）
+ `web/static/js/gen.js`（全部逻辑，只调**既有**接口）。

本文件钉住的是「这页不会静默失真」的几条：
  ① 挂在 `/gen` 且渲染应用外壳（进导航后不能掉出侧栏）；旧静态地址只留跳转桩；
  ② 不写死彩种清单（清单必须来自后端注册表）；
  ③ 读不到就写「—」，不许把缺失渲染成 0；
  ④ 期号是标识符，不许被千分位格式化（2026297 → "2,026,297" 是真实踩过的 bug）；
  ⑤ 重出必须带卡片当前值，否则会重现「拖到 1w 又变回 1000」；
  ⑥ 批量重出必须串行（`record_spend` 是读盘 → 改 → 原子写）；
  ⑦ 保存只写当前模式那一个字段（另一个字段是用户意图锚点）。
"""
import re
from pathlib import Path

import pytest

from config import LOTTERY_CONFIG
from web.app import app

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "web" / "templates" / "gen.html"
JS = ROOT / "web" / "static" / "js" / "gen.js"


@pytest.fixture(scope="module")
def src() -> str:
    """页面逻辑的真源（JS）。模板只负责外壳与样式，行为断言一律对 JS 做。"""
    return JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def tpl() -> str:
    return TPL.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def client():
    return app.test_client()


# ---------------------------------------------------------------- ① 可访问 / 只走既有接口
def test_page_served_at_gen(client):
    """页面必须挂在 `/gen` 上并渲染应用外壳（进导航后不能掉出侧栏）。"""
    r = client.get("/gen")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "出号设置" in html
    assert 'id="rows"' in html, "页面没有表格容器"
    assert "sidebar" in html, "没有渲染应用侧栏（说明走了裸静态页）"


def test_page_uses_only_existing_endpoints(src):
    """页面只允许调这四个既有接口；出现别的 `/api/` 路径就说明偷偷依赖了新路由。"""
    used = set(re.findall(r'"(/api/[^"?#]+)', src))
    allowed_prefix = (
        "/api/settings/ticket-size",
        "/api/system/invariants",
        "/api/data-status",
        "/api/feedback/",
        "/api/predict/regenerate",
        "/api/predict/status/",
    )
    unknown = [u for u in used if not u.startswith(allowed_prefix)]
    assert not unknown, f"出号页调用了未登记的接口（可能要求新路由）：{unknown}"


# ---------------------------------------------------------------- ② 彩种清单唯一真源
def test_no_handwritten_lottery_list(src):
    """彩种清单必须来自 `/api/settings/ticket-size` 的 `lotteries`。

    与 main.js 的判据同一把尺子：扫描所有含 ≥2 个彩种名的数组字面量。
    新增彩种时手写清单会静默漏掉它（本项目已踩过三次）。
    """
    names = set(LOTTERY_CONFIG)
    bad = []
    for m in re.finditer(r"\[([^\[\]]*)\]", src):
        quoted = re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
        hit = [q for q in quoted if q in names]
        if len(hit) >= 2:
            bad.append(tuple(hit))
    assert not bad, f"出号页出现了手写彩种清单：{bad}"


# ---------------------------------------------------------------- ③ 缺失 ≠ 0
def test_missing_value_renders_dash_not_zero(src):
    """`DASH = "—"` 且 `num()/v()` 对 null/undefined/空串返回破折号。

    ★ 这条不是样式偏好：接口失败时若渲染成 0，读者会把它读成「本期 0 注」，
      而真实含义是「读不到」。
    """
    assert 'var DASH = "—"' in src
    assert "null || x === undefined" in src or "x === null || x === undefined" in src


def test_issue_number_is_not_thousands_separated(src):
    """期号是标识符：`2026297` 被格式化后会变成 "2,026,297"（真实踩过）。

    判据：渲染「目标期号」必须走 `idf()`（纯 String），不许走 `num()`。
    """
    assert "function idf(" in src, "缺少标识符专用的 idf()"
    assert 'idf(inv ? inv["目标期号"] : null)' in src, \
        "目标期号没有走 idf()（会被千分位格式化，与官方期号对不上）"
    assert 'num(inv ? inv["目标期号"]' not in src


# ---------------------------------------------------------------- ④ 重出必须带卡片值
def test_regenerate_sends_current_card_value(src):
    """重出必须把输入框当前值作为 `出号数量` 提交。

    2026-11-07 事故：只发彩种名时后端按磁盘旧值出号，用户把滑块拖到 10000
    但没点保存就直接重出 ⇒ 出 1000 注，界面重绘后又变回 1000，
    看起来像「系统把我的注数改回去了」。
    """
    assert '"出号数量"' in src or "'出号数量'" in src
    m = re.search(r"body\[\"出号数量\"\]\s*=\s*n", src)
    assert m, "重出请求没有带上卡片当前值"


def test_batch_regenerate_is_sequential(src):
    """批量重出必须串行：`record_spend` 是「读盘 → 改 → 原子写」，并发会互相覆盖。"""
    assert "Promise.all(" not in src, "批量重出用了 Promise.all（并发写台账）"
    assert "await regenOne(" in src
    # 必须是「逐个 await 的 for 循环」
    assert re.search(r"for \(var k = 0; k < targets\.length; k\+\+\)\{[\s\S]{0,200}?await regenOne",
                     src), "批量重出不是串行的 for + await 结构"


# ---------------------------------------------------------------- ⑤ 保存只写当前模式字段
def test_save_writes_only_current_mode_field(src):
    """`{mode, [mode]: {彩种: 注数}}` —— 另一个字段是用户意图锚点，不许被覆盖。

    写错会把动态模式的值覆盖进 fixed 锚点，注数从此单向漂移。
    """
    assert "body[mode] = {}" in src
    assert "body[mode][lot] = n" in src
    assert "body.fixed = " not in src and "body.dynamic = " not in src


def test_empty_input_is_not_submitted_as_zero(src):
    """输入框清空时必须拒绝提交，而不是把空串当 0 发出去（那等于停发）。"""
    assert 'if (n === null){ logLine(lot + "：注数输入为空' in src


def test_loading_state_is_not_rendered_as_error(src):
    """「读取中」不能画成红色的「读取失败」。

    首版把加载态存成 `{ok:false, error:"加载中…"}`，于是点开的瞬间会先闪一个
    红框「批次明细读取失败：加载中…」—— 用户第一眼看到的永远是「坏了」。
    """
    assert "loading: true" in src
    assert "b.loading" in src
    assert "正在读取批次明细" in src


def test_unsaved_edit_survives_rerender(src):
    """保存任意彩种会重绘整表 —— 别的彩种「正在输入但未保存」的值必须留住。

    否则用户改完 A 去点 B 的保存，A 的输入被静默冲回旧值，
    而界面上看不出任何异常（下一次出号才按旧值跑）。
    """
    assert 'S.edit[t.getAttribute("data-lot")] = t.value' in src, "输入没有进入暂存表"
    assert "delete S.edit[lot]" in src, "保存成功后没有清掉暂存（值会被旧输入钉住）"
    assert "pendingEdit" in src, "重绘没有读暂存值"


# ---------------------------------------------------------------- ⑥ 停发 / 不一致要说人话
def test_stop_and_mismatch_states_are_explicit(src):
    """三种状态各自有文案：一致 / 不一致 / 停发（预算包络）。

    「停发」必须与「故障」区分开：它是既定行为，不是错误；反之「不一致」
    必须给出差额，否则读者会把「注数被换掉」当成正常。
    """
    assert "停发（预算包络）" in src
    assert "不一致" in src
    assert "无预测" in src
    assert "旁路在覆盖注数" in src, "不一致时没有给出「长期不变说明有旁路」的判读"


def test_displays_real_output_vs_setting(src):
    """核心读数：设置注数 vs 实际出号必须同屏（缺一列就退化成「看起来正常」）。"""
    assert 'inv["组数"]' in src
    assert "实际出号" in src


# ---------------------------------------------------------------- ⑦ 显隐与结构约束
def test_no_inline_display_toggle_pattern(src):
    """显隐一律走展开/收起重渲染，不用 `element.style.display`。

    项目既有坑：元素显隐由内联 style 控制时，CSS 类里再写 `display:none`
    会把元素永久藏住（类优先级盖过内联空值）。
    """
    assert ".style.display" not in src


def test_page_links_from_dashboard_and_predict():
    """入口必须在两个用户会经过的页面里，且指向**可路由**的 `/gen`。

    导航 href 必须能被 `app.url_map` 解析（`test_nav_consistency` 会查），
    而 `/static/gen.html` 是静态匹配、不在规则表里 —— 所以页面入口统一走 `/gen`，
    由 `web/routes/settings.py::gen_page` 转发同一份静态文件（内容不复制）。
    """
    tpl = ROOT / "web" / "templates"
    for name in ("dashboard.html", "predict.html"):
        text = (tpl / name).read_text(encoding="utf-8")
        assert 'href="/gen"' in text, f"{name} 没有指向出号设置页的入口"


def test_legacy_static_url_is_only_a_redirect_stub(client):
    """`/static/gen.html` 只留跳转桩，不许存在第二份实现（两份必然漂移）。"""
    r = client.get("/static/gen.html")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    assert "/gen" in html
    assert 'id="rows"' not in html, "旧静态路径里还留着页面本体（两份实现会漂移）"
    assert len(html) < 2000, "跳转桩不该有这么长的内容"
