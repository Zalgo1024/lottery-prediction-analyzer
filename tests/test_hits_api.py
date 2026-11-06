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
  7. `strategy` 筛选：**默认「全部」= 逐位保持加参数前的行为**；指定了才按
     `normalize_strategy_name` 归一名比对（ML(logistic) → ML策略），
     便于「低拥挤策略」这类研究性独立入口被单独筛出来看。

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
# 2.5 累计中奖金额（跟着筛选走，且两个口径都要点名）
# ------------------------------------------------------------

def test_amount_all_sums_known_payouts_only(client):
    """全部命中口径：5+5+30=40，一等奖「金额未知」单独计数，不按 0 也不按名义上限计入。"""
    d = _get(client, "?limit=0")
    assert d["amount_all"] == 40.0
    assert d["amount_all_unknown"] == 1      # 七星彩一等奖：浮动且无当期数据
    # 未筛选时「当前筛选」口径与全部口径必须相等
    assert d["amount_sum"] == 40.0
    assert d["amount_sum_unknown"] == 1


def test_amount_sum_follows_filters(client):
    """累计金额必须跟着筛选走 —— 三个不同筛选给出三个不同的合计。"""
    assert _get(client, "?lottery=双色球&limit=0")["amount_sum"] == 10.0     # 六等 5×2
    assert _get(client, "?min_amount=10&limit=0")["amount_sum"] == 30.0      # 只剩五等奖 30
    august = _get(client, "?date_to=2026-08&limit=0")                        # 七星彩 2 注
    assert august["amount_sum"] == 30.0
    assert august["amount_sum_unknown"] == 1


def test_amount_all_never_changes_with_filters(client):
    """分母口径的金额是稳定值：任何筛选都不能动它（否则「累计」会随点击跳动）。"""
    baseline = _get(client, "?limit=0")["amount_all"]
    assert baseline == 40.0
    for q in ("?lottery=双色球&limit=0", "?min_amount=10&limit=0",
              "?date_to=2026-08&limit=0", "?strategy=低拥挤策略&limit=0",
              "?min_amount=1000000&limit=0"):
        d = _get(client, q)
        assert d["amount_all"] == baseline, q
        assert d["amount_all_unknown"] == 1, q


def test_unknown_amount_never_silently_zero(client):
    """★ 金额未知的注不得消失：合计里不含它，但必须有一处计数说出来。

    这条锁的是「宁可少报也不虚报，但少了多少要说」——
    如果哪天有人图省事把 None 当 0，合计数看着更"完整"了，但那是错的。
    """
    d = _get(client, "?limit=0")
    known = [r for r in d["records"] if r["奖金"]["金额"] is not None]
    unknown = [r for r in d["records"] if r["奖金"]["金额"] is None]
    assert sum(r["奖金"]["金额"] for r in known) == d["amount_all"]   # 合计 == 已知之和
    assert len(unknown) == d["amount_all_unknown"]                    # 未知的都在计数里


def test_amount_rounding_is_cent_precise(client, monkeypatch):
    """合计保留到分：浮点求和不能把 5.55×3 算成 16.649999。"""
    hist = {"双色球": [_win("双色球", "六等") for _ in range(3)]}
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: hist.get(name, []))
    monkeypatch.setattr(pt, "prize_payout",
                        lambda lottery, grade, issue=None: {"金额": 5.55, "浮动": False})
    d = _get(client, "?limit=0")
    assert d["amount_all"] == 16.65
    assert d["amount_sum"] == 16.65


def test_frontend_renders_amount_following_filters(client):
    """前端必须把金额渲染出来，并把「未筛选时累计」作为另一个口径点名。"""
    js = client.get("/static/js/main.js").get_data(as_text=True)
    assert "_fmtMoney" in js
    assert "累计中奖金额" in js
    assert js.count("_renderHitsStat") >= 2          # 定义 + loadHits 真调用
    # loadHits 必须把后端四个金额字段都传进去（少一个就会静默显示 ¥0.00）
    for key in ("amount_sum", "amount_sum_unknown", "amount_all", "amount_all_unknown"):
        assert key in js, key
    # 未知金额要有一句人话解释（不是只给个裸数字）
    assert "金额未知未计入" in js


def test_frontend_never_shows_zero_for_missing_amount_field(client):
    """★ 哨兵：后端没给金额字段时，前端必须显示「—」，不许显示 ¥0.00。

    2026-10-27 实踩：`web/routes/hits.py` 加了 amount_sum/amount_all，但生产实例没重启
    （改 Python 必须重启；改 static/*.js 不用重启）⇒ 浏览器拿到**新 JS + 旧后端**，
    `data.amount_all` 是 undefined，`Number(undefined || 0)` = 0 ⇒ 页面理直气壮地报
    「333 注全部命中 · 累计中奖金额 ¥0.00」。这种「静默显示 0」比报错难查得多：
    数字看起来是个合法读数，只是错得离谱。

    所以前端必须区分「后端说 0」与「后端没说」：前者显示 ¥0.00，后者显示「—」。
    """
    js = client.get("/static/js/main.js").get_data(as_text=True)

    # 1) _fmtMoney 自身先挡 null/undefined/""，不能直接 Number()（Number(null)===0）
    fm = js.split("function _fmtMoney")[1].split("\n}")[0]
    assert "n === null" in fm and "n === undefined" in fm, "_fmtMoney 必须先判 null/undefined"
    assert fm.index("n === null") < fm.index("Number(n)"), "判空要在 Number() 之前"

    # 2) 渲染侧要显式识别「字段缺失」，不能 `Number(money.all) || 0`
    rs = js.split("function _renderHitsStat")[1].split("\n}")[0]
    assert "sumMissing" in rs, "_renderHitsStat 必须区分「缺失」与「值为 0」"
    assert "money.all) || 0" not in rs, "不许把缺失字段用 || 0 抹成 0"


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
# 3.8 策略筛选（strategy）
# ------------------------------------------------------------

# 三条双色球命中：高频策略 / 低拥挤策略 / ML(logistic)（归一后 = ML策略）
FAKE_HISTORY_STRAT = {
    "双色球": [
        _win("双色球", "六等"),
        dict(_win("双色球", "六等"), 策略="低拥挤策略"),
        dict(_win("双色球", "六等"), 策略="ML(logistic)"),
    ],
}


@pytest.fixture
def client_strat(monkeypatch):
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: FAKE_HISTORY_STRAT.get(name, []))

    def _fake_payout(lottery, grade, issue=None):
        return {"金额": 5.0, "浮动": False, "来源": "当期实际单注奖金", "文本": "¥5", "备注": ""}

    monkeypatch.setattr(pt, "prize_payout", _fake_payout)
    monkeypatch.setattr(hits_route, "feedback_record_view",
                        lambda name, h: {"zones": [], "total_hit": 0, "total_choose": 0})
    app.config["TESTING"] = True
    return app.test_client()


def test_strategy_default_is_bitwise_noop(client_strat):
    """默认「全部」与不传 strategy 必须逐位一致——加参数不能改既有行为。"""
    a = _get(client_strat, "?limit=0")
    b = _get(client_strat, "?strategy=全部&limit=0")
    assert a["total"] == b["total"] == 3
    assert [r["策略"] for r in a["records"]] == [r["策略"] for r in b["records"]]


def test_strategy_filter_picks_only_that_strategy(client_strat):
    d = _get(client_strat, "?strategy=低拥挤策略&limit=0")
    assert d["total"] == 1
    assert d["universe"] == 3              # 策略是筛选，分母不动
    assert d["records"][0]["策略"] == "低拥挤策略"
    assert d["strategy"] == "低拥挤策略"


def test_strategy_filter_uses_normalized_name(client_strat):
    """ML(logistic) 必须归一成 ML策略，否则同名策略会被拆成两拨。"""
    d = _get(client_strat, "?strategy=ML策略&limit=0")
    assert d["total"] == 1
    # 返回体里仍是原始票面名，前端展示不改写事实
    assert d["records"][0]["策略"] == "ML(logistic)"


def test_record_carries_lowcrowd_metadata_when_present(client_strat, monkeypatch):
    """低拥挤元数据（λ / 门控 / 覆盖键 / 循环性声明）要随记录返回，否则命中页无从展示。

    用户要的是「记录每期拥挤度分数、策略参数」—— 光能筛出来还不够，参数得看得见。
    """
    meta = {"λ": 0.5, "门控": {"生效分位": 0.3},
            "覆盖": {"选中覆盖键": 6868, "理论上限": 7000},
            "循环性声明": "代理分数"}
    hist = {"双色球": [
        dict(_win("双色球", "六等"), 策略="低拥挤策略", 低拥挤=meta),
        dict(_win("双色球", "六等"), 策略="高频策略"),
    ]}
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: hist.get(name, []))
    d = _get(client_strat, "?strategy=低拥挤策略&limit=0")
    assert d["total"] == 1
    assert d["records"][0]["低拥挤"]["λ"] == 0.5
    assert d["records"][0]["低拥挤"]["覆盖"]["选中覆盖键"] == 6868
    # 没有该字段的记录必须是 None（旧记录不粉饰成 0 分）
    d2 = _get(client_strat, "?limit=0")
    assert [r.get("低拥挤") for r in d2["records"]].count(None) == 1


def test_strategy_filter_misses_are_zero_not_error(client_strat):
    d = _get(client_strat, "?strategy=规则优选&limit=0")
    assert d["total"] == 0
    assert d["universe"] == 3
    assert d["records"] == []


def test_page_has_strategy_filter(client):
    """命中页 + 战绩页都要有「策略」下拉，且前端真的把参数发出去。"""
    hits_html = client.get("/hits").get_data(as_text=True)
    assert 'id="hits-strategy"' in hits_html
    assert "低拥挤策略" in hits_html
    rec_html = client.get("/records").get_data(as_text=True)
    assert 'id="rec-strategy"' in rec_html
    js = client.get("/static/js/main.js").get_data(as_text=True)
    assert "hits-strategy" in js and "rec-strategy" in js
    assert "&strategy=" in js                       # loadHits 真的拼进 URL
    assert js.count("_strategyBadge") >= 2          # 定义 + 战绩榜真调用
    # 低拥挤策略要有自己的徽标分支（研究入口，需注明不参与生产轮换）
    assert "低拥挤策略" in js


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


# ------------------------------------------------------------
# 8. 受规则约束档（2026-11-06 快乐8 实踩）
# ------------------------------------------------------------

def test_regulated_amount_reaches_hits_api(monkeypatch):
    """端到端：快乐8 中奖注经真实 prize_payout 计入合计，不再显示 ¥0.00 / ¥ —。

    与 client fixture 不同，这里**不**替换 prize_payout，只把取值链换成
    可控的「当期实测」，否则测不到展示层缺分支这个根因。
    """
    import ev.profit.amounts as amounts
    pt.clear_cache()
    monkeypatch.setattr(amounts, "grade_amount",
                        lambda lottery, issue, grade, **kw: (3.0, "实测"))
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: [_win("快乐8", "选十中五")]
                        if name == "快乐8" else [])
    monkeypatch.setattr(hits_route, "feedback_record_view",
                        lambda name, h: {"zones": [], "total_hit": 0, "total_choose": 0})
    app.config["TESTING"] = True
    d = _get(app.test_client(), "?lottery=快乐8&limit=0")
    assert d["total"] == 1
    assert d["amount_sum"] == 3.0
    assert d["amount_sum_unknown"] == 0
    assert d["records"][0]["奖金"]["文本"] == "¥3"
    assert d["records"][0]["奖金"]["来源"] == "当期实际单注奖金"
    pt.clear_cache()


def test_grade_filter_inapplicable_is_disclosed(client, monkeypatch):
    """「N 等奖及以上」对快乐8 玩法档不适用 ⇒ 排除数必须显式给出，不能静默漏计。"""
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: [_win("快乐8", "选十中五")]
                        if name == "快乐8" else [])
    d = _get(client, "?lottery=快乐8&min_prize=一等&limit=0")
    assert d["total"] == 0
    assert d["universe"] == 1
    assert d["grade_filter_inapplicable"] == 1
    assert d["grade_filter_lotteries"] == ["快乐8"]


def test_grade_filter_inapplicable_zero_without_filter(client):
    """没有等级筛选时不得凭空报「不适用」；前端必须真渲染这个披露。"""
    d = _get(client, "?limit=0")
    assert d["grade_filter_inapplicable"] == 0
    assert d["grade_filter_lotteries"] == []
    js = client.get("/static/js/main.js").get_data(as_text=True)
    assert "grade_filter_inapplicable" in js
    assert "奖级不适用本次等级筛选" in js
