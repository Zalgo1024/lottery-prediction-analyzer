# -*- coding: utf-8 -*-
"""命中历史「奖金筛选 + 计数（筛出 xx 注 / xxxx 注（总数））」契约测试。

锁住的行为：
  1. `universe` = **不加任何筛选**的全部命中注数，只随数据变化，不随筛选变化；
  2. `total`    = 命中本次筛选的注数（分页前），`returned` = 实际返回条数；
  3. 奖金筛选按单注奖金数值比较；**浮动奖级金额未知时不参与筛选**并单独计数
     （`amount_unknown`），不得静默当成「不满足」——否则筛选结果会被低估；
  4. `lottery` 是范围而非筛选：收窄它只影响 total，不影响 universe；
  5. 页面必须带出三个控件：奖金筛选、**工具条内的「筛出 xx 注 / 共 xxxx 注（总数）」计数**、
     以及下方的「目前已中奖 xx 注」常驻行；
  6. **HTML 页面必须禁浏览器缓存**（`Cache-Control: no-store`）——否则浏览器会对
     /hits 这类动态页做启发式缓存，改了模板刷新后仍是旧版。

为了确定性，测试隔离掉 feedback 读取、奖金表与记录视图，全部用合成数据。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.feedback as fb
import data.prize_table as pt
from web.app import app
import web.routes.hits as hits_route


def _win(lottery, grade, amount_grade=None, evaluated="2026-09-02T21:00:00"):
    """构造一条「已中奖」的 feedback 记录（只保留路由会读的字段）。"""
    return {
        "期号": 26100,
        "中奖等级": grade,
        "valid_prediction": True,
        "红球命中": 1 if lottery in ("双色球", "大乐透") else 0,
        "蓝球命中": 1 if lottery in ("双色球", "大乐透") else 0,
        "总命中": 3,
        "预测日期": "2026-09-01",
        "开奖日期": "2026-09-02",
        "评估时间": evaluated,
        "来源": "predict",
        "策略": "高频策略",
        "预测红球": [1, 2, 3, 4, 5, 6],
        "预测蓝球": [7],
        "实际红球": [1, 2, 3, 4, 5, 6],
        "实际蓝球": [7],
    }


# 双色球 2 注中奖(9 月) / 七星彩 2 注中奖(8 月，其中一等奖「金额未知」) → universe = 4
FAKE_HISTORY = {
    "双色球": [_win("双色球", "六等"), _win("双色球", "六等"), {"中奖等级": "未中"}],
    "七星彩": [_win("七星彩", "一等奖", evaluated="2026-08-15T21:00:00"),
               _win("七星彩", "五等奖", evaluated="2026-08-15T21:00:00")],
}

# 单注奖金：六等 5 元 / 五等奖 30 元 / 一等奖 浮动且无当期数据 → 金额 None
FAKE_AMOUNT = {"双色球": {"六等": 5.0}, "七星彩": {"五等奖": 30.0, "一等奖": None}}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: FAKE_HISTORY.get(name, []))

    def _fake_payout(lottery, grade, issue=None):
        amt = FAKE_AMOUNT.get(lottery, {}).get(grade)
        return {"金额": amt, "浮动": amt is None, "来源": "当期实际单注奖金" if amt else "名义参考",
                "文本": f"¥{amt:,.0f}" if amt else "浮动（无当期数据）", "备注": ""}

    monkeypatch.setattr(pt, "prize_payout", _fake_payout)
    monkeypatch.setattr(hits_route, "feedback_record_view",
                        lambda name, h: {"zones": [], "total_hit": 0, "total_choose": 0})
    app.config["TESTING"] = True
    return app.test_client()


def _get(client, query=""):
    resp = client.get(f"/api/hits{query}")
    assert resp.status_code == 200, resp.get_data(as_text=True)[:300]
    return resp.get_json()


# ------------------------------------------------------------
# 1. 计数口径
# ------------------------------------------------------------

def test_universe_is_unfiltered_win_count(client):
    d = _get(client, "?limit=0")
    assert d["universe"] == 4          # 双 2 + 七 2
    assert d["total"] == 4
    assert d["returned"] == 4
    assert d["amount_unknown"] == 0    # 未启用奖金筛选 → 不统计未知金额


def test_lottery_is_scope_not_filter(client):
    """收窄彩种只影响 total，不影响 universe（分母要保持稳定）。"""
    d = _get(client, "?lottery=双色球&limit=0")
    assert d["total"] == 2
    assert d["universe"] == 4


def test_total_counts_before_limit_returned_after(client):
    d = _get(client, "?limit=1")
    assert d["total"] == 4             # 分页前命中总数（分子）
    assert d["returned"] == 1
    assert len(d["records"]) == 1
    assert d["limit"] == 1


# ------------------------------------------------------------
# 2. 奖金筛选
# ------------------------------------------------------------

def test_min_amount_filters_by_single_ticket_amount(client):
    d = _get(client, "?min_amount=10&limit=0")
    # 双色球六等 5 元 ×2 被筛掉；七星彩五等奖 30 元保留
    assert d["total"] == 1
    assert d["universe"] == 4
    assert {r["中奖等级"] for r in d["records"]} == {"五等奖"}


def test_unknown_amount_is_counted_not_silently_dropped(client):
    """浮动奖级当期金额未知 → 不参与金额筛选，但必须单独计数。"""
    d = _get(client, "?min_amount=10&limit=0")
    assert d["amount_unknown"] == 1    # 七星彩一等奖


def test_min_amount_zero_equals_no_filter(client):
    for q in ("?min_amount=&limit=0", "?min_amount=0&limit=0", "?min_amount=abc&limit=0"):
        d = _get(client, q)
        assert d["total"] == 4, q
        assert d["amount_unknown"] == 0, q


def test_high_threshold_yields_zero_with_stable_universe(client):
    d = _get(client, "?min_amount=1000000&limit=0")
    assert d["total"] == 0
    assert d["universe"] == 4


def test_max_amount_keeps_everything_known(client):
    d = _get(client, "?min_amount=30&limit=0")
    # 六等 5 元被筛掉（2 注），30 元保留 1 注，一等奖未知 1 注 → 只 1 注入选
    assert d["total"] == 1


# ------------------------------------------------------------
# 3. 排序
# ------------------------------------------------------------

def test_sort_by_amount_desc_puts_unknown_last(client):
    d = _get(client, "?sort_by=amount&sort_order=desc&limit=0")
    amounts = [r["奖金"]["金额"] for r in d["records"]]
    assert amounts[0] == 30.0
    assert amounts[-1] is None         # 金额未知排到最后


# ------------------------------------------------------------
# 3.5 时间筛选（按结算/评估时间，支持 YYYY / YYYY-MM / YYYY-MM-DD）
# ------------------------------------------------------------

def test_time_filter_month_granularity(client):
    """'2026-09' 展开成月初 09-01：只留 9 月的双色球 2 注；未给的另一端不设限。"""
    d = _get(client, "?date_from=2026-09&limit=0")
    assert d["total"] == 2
    assert d["universe"] == 4          # 时间是筛选：universe 分母不动
    assert d["date_from"] == "2026-09-01"
    assert d["date_to"] == ""          # 只给下界 = 从该时间起
    assert {r["lottery"] for r in d["records"]} == {"双色球"}

    # 只给上界 = 到该时间为止：8 月末 → 只剩两注七星彩
    d_only_aug = _get(client, "?date_to=2026-08&limit=0")
    assert d_only_aug["total"] == 2
    assert d_only_aug["date_to"] == "2026-08-31"
    assert {r["lottery"] for r in d_only_aug["records"]} == {"七星彩"}


def test_time_filter_range_and_year_granularity(client):
    d = _get(client, "?date_from=2026-08-01&date_to=2026-08&limit=0")
    assert d["total"] == 2             # 8 月的两注七星彩
    assert d["date_to"] == "2026-08-31"

    d2 = _get(client, "?date_from=2026&date_to=2026&limit=0")
    assert d2["total"] == 4            # 整年 = 不设限
    assert d2["date_from"] == "2026-01-01"
    assert d2["date_to"] == "2026-12-31"


def test_time_filter_narrow_window_and_invalid_values(client):
    d = _get(client, "?date_from=2026-09-03&date_to=2026-09-05&limit=0")
    assert d["total"] == 0             # 记录都在 9-02 / 8-15，窗口内为空

    # 非法日期必须被忽略（不筛选），而不是把全部记录筛没
    for q in ("?date_from=garbage&limit=0", "?date_to=2026-13-99&limit=0",
              "?date_from=&date_to=&limit=0"):
        d2 = _get(client, q)
        assert d2["total"] == 4, q


# ------------------------------------------------------------
# 4. 页面控件
# ------------------------------------------------------------

def test_page_has_amount_filter_and_counter(client):
    html = client.get("/hits").get_data(as_text=True)
    assert 'id="hits-amount"' in html          # 奖金筛选
    assert 'id="hits-stat"' in html            # 「目前已中奖 xx 注」常驻计数
    assert '奖金 ≥ 100 元' in html
    assert "全部奖金" in html


def test_page_has_time_filter(client):
    """时间筛选 = 日历选择器：按钮 + 弹出日历 + 快捷预设；区间落两个隐藏 input。"""
    html = client.get("/hits").get_data(as_text=True)
    assert 'id="hits-time-btn"' in html            # 打开日历的按钮
    assert 'id="hits-time-label"' in html          # 按钮上的当前区间文案
    assert 'id="hits-calendar"' in html            # 日历弹出层
    assert 'id="hits-date-from"' in html and 'id="hits-date-to"' in html
    js = client.get("/static/js/main.js").get_data(as_text=True)
    # 日历核心函数必须存在且被真正接线
    for fn in ("_toggleCalendar", "_renderCalendar", "_calPick", "_calPreset", "_calSyncLabel"):
        assert fn in js, fn
    assert js.count("_hitsTimeBounds") >= 2        # 定义 + loadHits 里真调用
    # 快捷预设（渲染在日历里，所以断言在 JS 里）
    for token in ("全部", "本月", "上月", "近 7 天", "近 30 天"):
        assert token in js, token
    # loadHits 仍要把区间发给后端
    assert "date_from" in js and "date_to" in js


def test_page_has_toolbar_counter(client):
    """工具条内必须有「筛出 xx 注 / 共 xxxx 注（总数）」——用户明确要求它出现在筛选条上。"""
    html = client.get("/hits").get_data(as_text=True)
    assert 'id="hits-count"' in html
    assert 'id="hits-count-match"' in html     # 分子：筛出注数
    assert 'id="hits-count-total"' in html     # 分母：全部中奖注数（总数）
    assert "（总数）" in html
    # 渲染函数必须真的存在于前端脚本里，否则计数永远停在「—」
    js = client.get("/static/js/main.js").get_data(as_text=True)
    assert "_renderHitsCount" in js
    assert "_renderHitsStat" in js
    # loadHits 里必须调用它（定义而不调用 = 死代码）
    assert js.count("_renderHitsCount") >= 2


def test_html_pages_are_not_browser_cached(client):
    """动态 HTML 必须显式禁缓存，否则「改了模板刷新还是旧版」。"""
    for path in ("/hits", "/"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.mimetype == "text/html", path
        cc = (r.headers.get("Cache-Control") or "").lower()
        assert "no-store" in cc and "no-cache" in cc, f"{path} -> {cc!r}"
    # 静态资源同样禁缓存（既有行为，防回归）
    r = client.get("/static/js/main.js")
    assert "no-store" in (r.headers.get("Cache-Control") or "").lower()
