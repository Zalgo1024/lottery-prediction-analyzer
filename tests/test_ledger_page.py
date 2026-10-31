"""
盈利账本看板（`/ledger`）的牙齿测试。

★ 这一页最容易出的两种错，都不是"报错"，而是"数字合法但错得离谱"：
  ① 没有账本时把金额显示成 ¥0.00 —— 看起来像"这期没花钱也没中奖"，
     实际是"后端根本没给这个字段"。必须显示「—」并说明该跑哪条命令。
  ② 把「中奖金额≥10元的期数」当主指标 —— 它随注数机械上升（买得越多越必然
     有一注过 10 元），拿它证明策略有效等于用 K 证明 K。

∴ 下面的断言盯的是"称号与口径"，不是"页面长得对不对"。
"""

import json

import pytest

from web.app import app


@pytest.fixture(scope="module")
def client():
    return app.test_client()


def _get_json(client, path):
    r = client.get(path)
    assert r.status_code == 200, "%s 返回 %s" % (path, r.status_code)
    return json.loads(r.get_data(as_text=True))


# ------------------------------------------------------------------ 页面
def test_page_renders(client):
    r = client.get("/ledger")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for kw in ("盈利账本", "净收益率", "最大回撤", "次级", "影子"):
        assert kw in html, "页面缺关键内容：%s" % kw


def test_page_has_no_stake_controls(client):
    """看板只读：不许出现任何"下注/加注/出票"**控件**。

    ★ 不要用「页面里有没有『加注』这个词」来判——本页的诚实声明正好写着
      「不占预算 · 不触发加注」，按词扫会把否定句当成违规，逼人删掉说明文字，
      删完下一个人就真敢加个按钮进来。判据要落在**结构**上：没有表单、
      没有提交按钮、没有 POST 目标。
    """
    import re

    html = client.get("/ledger").get_data(as_text=True)
    assert "<form" not in html.lower(), "只读看板不该有表单"
    bad = re.findall(r"<button[^>]*>\s*[^<]*(?:投注|加注|出票|下单)", html)
    assert not bad, "只读看板出现了投注类按钮：%s" % bad
    # 页内 JS 只允许 GET（读接口）
    assert not re.search(r"method\s*:\s*['\"]POST['\"]", html), "只读看板不该发 POST"


# ------------------------------------------------------------------ 空态：金额必须是 None，不是 0
def test_missing_ledger_reports_none_not_zero(client):
    """账本文件不存在 ⇒ 金额字段 None + 状态 nodata + 给出重建命令。

    ★ 这是 `¥0.00` 事故的复现哨兵：0 是合法数字，页面会把它当"这期没花钱"。
    """
    d = _get_json(client, "/api/ledger/summary?lottery=双色球")
    s = d["分彩种"][0]
    assert s["状态"] == "nodata"
    for key in ("成本", "税前奖金", "净盈亏", "净收益率", "最大回撤", "参与期数"):
        assert s[key] is None, "%s 在没有账本时必须为 None（不能是 0）" % key
    assert "rebuild_ledger.py" in s["说明"], "空态必须告诉用户跑哪条命令"


def test_periods_missing_ledger_is_empty_not_error(client):
    d = _get_json(client, "/api/ledger/periods/双色球?limit=10")
    assert d["状态"] == "nodata"
    assert d["期数"] == []


# ------------------------------------------------------------------ 有数据时的口径
def _seed_rows(tmp_ledger_dir):
    from data import ledger as L

    rows = [
        {"期号": "1", "开奖日期": "2026-01-01", "参与": True, "参与原因": "已出号",
         "批次号": "b1", "出号时刻": "2025-12-31T10:00:00", "请求注数": 3, "实际注数": 3,
         "单价": 2.0, "成本": 6.0, "税前奖金": 20.0, "中奖注数": 4, "中奖期": True,
         "金额未知注数": 0, "中奖注数_ge10": 0, "中奖期_ge10": False},
        {"期号": "2", "开奖日期": "2026-01-03", "参与": False, "参与原因": "未出号",
         "批次号": None, "出号时刻": None, "请求注数": 0, "实际注数": 0,
         "单价": 2.0, "成本": 0.0, "税前奖金": 0.0, "中奖注数": 0, "中奖期": False,
         "金额未知注数": 0, "中奖注数_ge10": 0, "中奖期_ge10": False},
    ]
    L._write("双色球", L.recompute(rows))


def test_summary_denominators_add_up(client, tmp_path, monkeypatch):
    from data import ledger as L
    monkeypatch.setattr(L, "LEDGER_DIR", tmp_path / "ledger")
    _seed_rows(tmp_path)

    d = _get_json(client, "/api/ledger/summary?lottery=双色球")
    s = d["分彩种"][0]
    assert s["状态"] == "ok"
    assert s["开奖期数"] == s["参与期数"] + s["未参与期数"] == 2
    assert s["参与期数"] == 1, "没出号的那期必须进样本但不算参与"
    # 占比分母是参与期数（1），不是开奖期数（2）
    assert s["中奖期占比"] == pytest.approx(1.0)


def test_ge10_is_per_ticket_not_per_period(client, tmp_path, monkeypatch):
    """「中奖金额≥10元」必须是**逐票**口径：期合计 20 元但单注都是 5 元 ⇒ 不算。

    ★ 按当期合计算的话，注数一大就必然"超过 10 元"，那是用 K 证明 K。
    """
    from data import ledger as L
    monkeypatch.setattr(L, "LEDGER_DIR", tmp_path / "ledger")

    recs = []
    for i in range(4):
        recs.append({"期号": "9", "批次号": "b", "批次规模": 4,
                     "中奖等级": "六等", "税前奖金": 5.0, "金额未知": False})
    rows = L.build_rows("双色球", records=recs)
    assert rows[0]["税前奖金"] == pytest.approx(20.0)      # 期合计 20 ≥ 10
    assert rows[0]["中奖注数_ge10"] == 0                   # 但没有一注 ≥10
    assert rows[0]["中奖期_ge10"] is False

    recs2 = [{"期号": "10", "批次号": "b", "批次规模": 1,
              "中奖等级": "五等", "税前奖金": 10.0, "金额未知": False}]
    rows2 = L.build_rows("双色球", records=recs2)
    assert rows2[0]["中奖注数_ge10"] == 1
    assert rows2[0]["中奖期_ge10"] is True


def test_unknown_amount_excluded_from_sum(client, tmp_path, monkeypatch):
    """浮动奖取不到金额 ⇒ 不计入求和、单独计数，并写进备注。"""
    from data import ledger as L
    monkeypatch.setattr(L, "LEDGER_DIR", tmp_path / "ledger")
    recs = [
        {"期号": "11", "批次号": "b", "批次规模": 2, "中奖等级": "一等", "税前奖金": None},
        {"期号": "11", "批次号": "b", "批次规模": 2, "中奖等级": "未中", "税前奖金": 0.0},
    ]
    rows = L.recompute(L.build_rows("双色球", records=recs))
    L._write("双色球", rows)
    s = _get_json(client, "/api/ledger/summary?lottery=双色球")["分彩种"][0]
    assert s["金额未知注数"] == 1
    assert s["税前奖金"] == 0.0, "未知金额不许按 0 参与求和以外的任何摊派"
    assert "排除在求和之外" in s["备注"]["金额未知"]


# ------------------------------------------------------------------ 指标称号
def test_secondary_metrics_are_declared_secondary(client):
    d = _get_json(client, "/api/ledger/summary?lottery=双色球")
    assert d["主指标"] == ["净收益率", "最大回撤"]
    assert "中奖期数_ge10" in d["次级指标"]
    assert "中奖注数" in d["次级指标"]
    assert "中奖注数" not in d["主指标"], "中奖注数不许当主指标"


def test_ge10_note_says_not_comparable_across_k(client, tmp_path, monkeypatch):
    from data import ledger as L
    monkeypatch.setattr(L, "LEDGER_DIR", tmp_path / "ledger")
    _seed_rows(tmp_path)
    s = _get_json(client, "/api/ledger/summary?lottery=双色球")["分彩种"][0]
    note = s["备注"]["中奖期数_ge10"]
    assert "次级" in note
    assert "不可跨 K 比较" in note or "机械上升" in note


# ------------------------------------------------------------------ 其余接口
def test_periods_limit_is_clamped(client, tmp_path, monkeypatch):
    from data import ledger as L
    monkeypatch.setattr(L, "LEDGER_DIR", tmp_path / "ledger")
    rows = [{"期号": str(i), "参与": True, "成本": 0.0, "净盈亏": 0.0,
             "税前奖金": 0.0} for i in range(600)]
    L._write("双色球", L.recompute(rows))
    d = _get_json(client, "/api/ledger/periods/双色球?limit=99999")
    assert d["返回"] <= 500, "limit 必须被夹到 500 以内，否则一次能拉垮页面"


def test_shadow_endpoint_says_not_booked(client):
    d = _get_json(client, "/api/ledger/shadow")
    assert "诚实边界" in d
    assert "影子" in d["诚实边界"] or "同预算" in d["诚实边界"]


def test_glossary_reuses_narrative_constants(client):
    """术语的诚实边界必须**引用** narrative 常量，不许手抄。"""
    from ev.mechanism.narrative import EV_NEGATIVE_CAVEAT

    d = _get_json(client, "/api/ledger/glossary")
    assert d["诚实声明"] == EV_NEGATIVE_CAVEAT
    ids = {c["id"] for c in d["卡片"]}
    assert {"decide", "book", "denom", "secondary", "shadow"} <= ids
    # 决策卡必须挂预计为负的诚实边界
    decide = [c for c in d["卡片"] if c["id"] == "decide"][0]
    assert EV_NEGATIVE_CAVEAT in decide["诚实边界"]
