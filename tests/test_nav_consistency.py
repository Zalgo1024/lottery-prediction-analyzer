"""
导航同源锁 —— 防止顶栏与侧栏再次漂移

★ 事故背景：侧栏曾漏掉「对比」「鲁棒性」两项，顶栏又漏掉「命中历史」「数据体检看板」，
  两边各写一份 HTML 是根因。现在两边都由 web/nav.py 的 NAV_GROUPS 渲染，
  本文件把「同源」这件事锁死：以后加页面只改 NAV_GROUPS；若有人回退成硬编码，
  下面这些断言会立刻失败。

不承诺任何中奖率，仅是界面结构校验。
"""

import re

import pytest

from web.app import app
from web.nav import NAV_GROUPS, nav_flat, nav_lookup


@pytest.fixture(scope="module")
def client():
    return app.test_client()


def _page_html(client, path="/"):
    r = client.get(path)
    assert r.status_code == 200, "%s 返回 %s" % (path, r.status_code)
    return r.get_data(as_text=True)


def _nav_hrefs(html):
    m = re.search(r"<nav>(.*?)</nav>", html, re.S)
    assert m, "模板里找不到 <nav>"
    return re.findall(r'<a href="([^"]+)"', m.group(1))


def _side_hrefs(html):
    m = re.search(r'<div class="sidebar">(.*?)</div>\s*</div>', html, re.S)
    assert m, "模板里找不到 .sidebar"
    return re.findall(r'<a href="([^"]+)"', m.group(1))


# ---------------------------------------------------------------- 清单本身
def test_items_have_required_fields():
    for it in nav_flat():
        for key in ("href", "label", "side", "icon"):
            assert it.get(key), "菜单项 %s 缺字段 %s" % (it.get("href"), key)


def test_no_duplicate_href():
    hrefs = [it["href"] for it in nav_flat()]
    assert len(hrefs) == len(set(hrefs)), "NAV_GROUPS 里有重复 href: %s" % hrefs


def test_groups_are_named_and_non_empty():
    assert len(NAV_GROUPS) >= 2
    for g in NAV_GROUPS:
        assert g["title"].strip(), "分组标题不能为空"
        assert g["items"], "分组 %s 没有条目" % g["title"]


def test_label_short_enough_for_topbar():
    """顶栏靠 label（短名），过长会把「重启服务」按钮挤换行。"""
    for it in nav_flat():
        assert len(it["label"]) <= 6, "%s 的顶栏名过长：%s" % (it["href"], it["label"])


# ---------------------------------------------------------------- 同源渲染
def test_topbar_and_sidebar_identical(client):
    """核心断言：两个导航必须渲染出同一串 href，且顺序一致。"""
    html = _page_html(client, "/")
    nav = _nav_hrefs(html)
    side = _side_hrefs(html)
    assert nav == side, "顶栏与侧栏不一致\n  顶栏=%s\n  侧栏=%s" % (nav, side)


def test_all_pages_present_in_both_navs(client):
    """每个已注册页面都在两个导航里各出现一次。"""
    html = _page_html(client, "/")
    nav, side = _nav_hrefs(html), _side_hrefs(html)
    for it in nav_flat():
        assert nav.count(it["href"]) == 1, "顶栏缺/重复 %s" % it["href"]
        assert side.count(it["href"]) == 1, "侧栏缺/重复 %s" % it["href"]


def test_previously_missing_items_are_back(client):
    """回归点：这些页面曾经只在一边有。"""
    html = _page_html(client, "/")
    nav, side = _nav_hrefs(html), _side_hrefs(html)
    # 只在顶栏有的
    for h in ("/compare", "/robustness"):
        assert h in side, "%s 又掉出侧栏了" % h
        assert h in nav
    # 只在侧栏有的
    for h in ("/hits", "/audit"):
        assert h in nav, "%s 又掉出顶栏了" % h
        assert h in side


# ---------------------------------------------------------------- 链接有效性
def test_every_nav_href_is_routable():
    """没有死链：每个 href 都能被 Flask 路由解析。"""
    rules = {str(r.rule) for r in app.url_map.iter_rules()}
    for it in nav_flat():
        assert it["href"] in rules, "导航里有死链：%s（无对应路由）" % it["href"]


def test_sidebar_has_section_titles(client):
    html = _page_html(client, "/")
    m = re.search(r'<div class="sidebar">(.*?)</div>\s*</div>', html, re.S)
    titles = re.findall(r'<div class="section-title">([^<]+)</div>', m.group(1))
    assert len(titles) == len(NAV_GROUPS), "分组标题数 %d ≠ 分组数 %d" % (len(titles), len(NAV_GROUPS))


# ---------------------------------------------------------------- 开奖数据库入口
def test_market_page_renders(client):
    html = _page_html(client, "/market")
    assert "开奖数据库" in html
    assert "mk-frame" in html, "缺 iframe 容器"
    assert "127.0.0.1:5001" in html, "未指向数据浏览器端口"


def test_market_page_has_offline_fallback(client):
    """数据浏览器是独立进程，没起时必须有兜底，不能只给一片空白。"""
    html = _page_html(client, "/market")
    assert "mk-offline" in html
    assert "/api/market/start" in html


def test_market_status_api(client):
    r = client.get("/api/market/status")
    assert r.status_code == 200
    d = r.get_json()
    for key in ("online", "viewer", "port", "db_exists"):
        assert key in d, "状态接口缺字段 %s" % key
    assert d["port"] == 5001


def test_market_nav_entry_label(client):
    """入口在导航里叫「开奖数据库」，不要让用户找不到。"""
    lookup = nav_lookup()
    assert "/market" in lookup
    assert lookup["/market"]["side"] == "开奖数据库"
