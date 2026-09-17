# -*- coding: utf-8 -*-
"""批次契约测试 —— 「这条中奖记录是当时出号时的第几批、第几注」。

锁住的行为：
  1. 一次出号 = 一条 pending = 一个批次：`record_pending_prediction` 必须给每次出号
     打上唯一 `批次号`，同批所有注共用；
  2. 归组时**显式批次号优先**；早期没有批次号的记录回退按 (期号 + 评估时间) 聚簇，
     并标注 `derived`，不得与显式批次混为一谈；
  3. **「第几批」按同一期号内、出号时间先后编号 1/2/3**（explicit 与 derived 统一编号，
     derived 的 batch_id 里那个 seq 只统计 derived 组，不能直接复用）；
  4. **「第几注」= 批内序号 1-based**：新数据读出号时写下的原值；9-14 前没有该字段的
     历史记录退化为「组内位置 + 1」，并在 `批次内序号推算` 标 True；
  5. `/api/hits` 的每条记录都必须带上 `批次标签` 等字段供页面渲染。
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.feedback as fb
import data.prize_table as pt
from ev.batch import batch_fields, group_batches
from web.app import app
import web.routes.hits as hits_route


# ------------------------------------------------------------
# 1. 出号时打批次号
# ------------------------------------------------------------

@pytest.fixture
def pending_capture(monkeypatch):
    """隔离 pending 写入，返回写入内容。"""
    saved = {}

    monkeypatch.setattr(fb, "load_pending", lambda name: [])
    monkeypatch.setattr(fb, "save_pending", lambda name, p: saved.update({"name": name, "list": list(p)}))
    monkeypatch.setattr(fb, "_next_issue", lambda name: 26110)
    # 让「马后炮」防线全部放行：本地数据取不到 → local_max=None；无推算开奖日 → 日期闸跳过
    monkeypatch.setattr(fb, "load_lottery", lambda name: (_ for _ in ()).throw(Exception("no data")))
    monkeypatch.setattr(fb, "_target_draw_date", lambda name, issue: None)
    return saved


def test_record_pending_stamps_batch_id_and_size(pending_capture):
    # 来源=train：出号质量压缩只作用于生产预测（来源=predict），
    # 本测试锁的是批次打戳契约，用 train 来源避免与压缩口径纠缠
    fb.record_pending_prediction("双色球", {
        "预测日期": "2026-09-14",
        "生成时间": "2026-09-14T10:00:00",
        "来源": "train",
        "预测号码": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7], "策略": "高频策略"}] * 100,
    })
    rec = pending_capture["list"][0]
    assert rec["批次号"].startswith("双色球-26110-一般-")
    assert rec["批次规模"] == 100


def test_two_runs_get_different_batch_ids(pending_capture):
    for _ in range(2):
        fb.record_pending_prediction("双色球", {
            "预测日期": "2026-09-14",
            "生成时间": "2026-09-14T10:00:00",
            "预测号码": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}] * 5,
        })
    saved = pending_capture["list"]
    # 同 (期号, 档位) 去重只留最新一条，所以这里只剩 1 条；
    # 但两次调用的批次号必须不同（时间精度到秒也不该撞）
    assert len(saved) == 1
    assert saved[0]["批次号"].startswith("双色球-26110-一般-")


def test_tier_is_part_of_batch_id(pending_capture):
    """三档管线各自出号，档位不同 → 批次号必须不同（票面可能完全相同）。"""
    ids = []
    for tier in ("一般", "稳健"):
        fb.record_pending_prediction("双色球", {
            "预测日期": "2026-09-14",
            "生成时间": "2026-09-14T10:00:00",
            "档位": tier,
            "预测号码": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}],
        })
        ids.append(pending_capture["list"][-1]["批次号"])
    assert "一般" in ids[0] and "稳健" in ids[1]
    assert ids[0] != ids[1]


# ------------------------------------------------------------
# 2. 归组：显式优先，缺失则按时间聚簇
# ------------------------------------------------------------

def _rec(issue, t, grade="未中", batch=None, idx=None):
    r = {"期号": issue, "评估时间": t, "中奖等级": grade, "中奖玩法": grade,
         "valid_prediction": True, "策略": "高频策略"}
    if batch:
        r["批次号"] = batch
    if idx:
        r["批次内序号"] = idx
    return r


def test_explicit_batch_id_groups_records():
    recs = [_rec(1, "2026-09-01T10:00:00", "六等", batch="B1"),
            _rec(1, "2026-09-01T10:00:01", "未中", batch="B1"),
            _rec(1, "2026-09-01T10:00:02", "六等", batch="B2")]
    groups = group_batches(recs, "双色球")
    assert len(groups) == 2
    assert all(g.source == "explicit" for g in groups)
    assert sorted(g.size for g in groups) == [1, 2]


def test_derived_batches_split_on_time_gap():
    """同一期、间隔 2 秒 → 同一批；间隔 60 秒 → 切成两批。"""
    near = [_rec(1, "2026-09-01T10:00:00"), _rec(1, "2026-09-01T10:00:02")]
    far = [_rec(1, "2026-09-01T10:00:00"), _rec(1, "2026-09-01T10:01:00")]
    assert len(group_batches(near, "双色球")) == 1
    assert len(group_batches(far, "双色球")) == 2


def test_derived_groups_are_marked_and_separated_from_explicit():
    recs = [_rec(1, "2026-09-01T10:00:00", batch="B1"),
            _rec(1, "2026-09-01T10:00:00")]
    groups = group_batches(recs, "双色球")
    by_src = {g.source for g in groups}
    assert by_src == {"explicit", "derived"}


def test_groups_sorted_newest_first():
    recs = [_rec(1, "2026-09-01T10:00:00", batch="OLD"),
            _rec(2, "2026-09-05T10:00:00", batch="NEW")]
    groups = group_batches(recs, "双色球")
    assert [g.batch_id for g in groups] == ["NEW", "OLD"]


# ------------------------------------------------------------
# 3. 期号内批号 + 批内注号（本次改动的核心语义）
# ------------------------------------------------------------

def test_issue_scoped_batch_index_assigns_1_n_by_time():
    """同一期内多批按出号时间编 1/2/3；只有一批时就是「第 1 批」。"""
    recs = [_rec(99, "2026-09-01T10:00:00", batch="B2"),
            _rec(99, "2026-09-01T09:00:00", batch="B1")]
    by_id = {g.batch_id: g for g in group_batches(recs, "双色球")}
    assert by_id["B1"].issue_batch_index == 1
    assert by_id["B2"].issue_batch_index == 2
    assert by_id["B1"].issue_batch_count == 2
    assert by_id["B2"].issue_batch_count == 2

    only = group_batches([_rec(7, "2026-09-01T10:00:00", batch="ONLY")], "双色球")[0]
    assert only.issue_batch_index == 1
    assert only.issue_batch_count == 1


def test_mixed_explicit_derived_share_issue_numbering():
    """explicit 与 derived 在同一期内连续编号，不各算各的。

    derived 的 batch_id 里那个 seq 只统计 derived 组，若直接复用会出现
    「explicit 第 1 批 + derived 第 1 批」两个第 1 批。
    """
    recs = [_rec(99, "2026-09-01T09:00:00", batch="EX"),
            _rec(99, "2026-09-01T10:00:00")]
    groups = group_batches(recs, "双色球")
    assert len(groups) == 2
    assert sorted(g.issue_batch_index for g in groups) == [1, 2]
    assert all(g.issue_batch_count == 2 for g in groups)


def test_within_batch_note_index_and_estimate_flag():
    """「第几注」= 组内位置 + 1；老记录无字段时标推算。"""
    new_recs = [_rec(1, "2026-09-14T10:00:00", "六等", batch="NB", idx=i) for i in (1, 2, 3)]
    g_new = group_batches(new_recs, "双色球")[0]
    f = [batch_fields(r, g_new) for r in g_new.records]
    assert [x["批次内序号"] for x in f] == [1, 2, 3]
    assert all(x["批次内序号推算"] is False for x in f)

    old_recs = [_rec(1, "2026-09-01T10:00:00") for _ in range(3)]
    g_old = group_batches(old_recs, "双色球")[0]
    f2 = [batch_fields(r, g_old) for r in g_old.records]
    assert [x["批次内序号"] for x in f2] == [1, 2, 3]
    assert all(x["批次内序号推算"] is True for x in f2)
    assert f2[0]["批次标签"] == "第 1 批 · 第 1 注 · 原始 3 注/1 组"
    assert f2[2]["批次标签"] == "第 1 批 · 第 3 注 · 原始 3 注/1 组"


def test_batch_fields_carry_original_size_and_combos():
    """徽章带「原始 N 注/G 组」：未压缩批次 N=登记规模；压缩批次 N=压缩前注数；
    组合数按 5 注/组向上取整（与待开奖弹窗打包口径一致）。"""
    # 未压缩：5 注 → 原始 5 注/1 组（== 登记规模）
    recs = [_rec(1, "2026-09-01T10:00:00") for _ in range(5)]
    g = group_batches(recs, "双色球")[0]
    f = batch_fields(g.records[0], g)
    assert f["原始注数"] == 5 and f["原始组合数"] == 1
    assert f["批次标签"] == "第 1 批 · 第 1 注 · 原始 5 注/1 组"

    # 压缩批次：登记 7 注、原始 16 注 → 4 组
    comp = [_rec(1, "2026-09-02T10:00:00", batch="CP", idx=i) for i in (1, 2)]
    comp[0]["压缩前注数"] = 16
    comp[1]["压缩前注数"] = 16
    g2 = group_batches(comp, "双色球")[0]
    f2 = batch_fields(g2.records[0], g2)
    assert f2["批次规模"] == 2 and f2["原始注数"] == 16 and f2["原始组合数"] == 4
    assert f2["批次标签"] == "第 1 批 · 第 1 注 · 原始 16 注/4 组"


def test_identical_records_still_get_distinct_note_numbers():
    """同批若出现两条完全相同的记录，注号也不能重复（必须按身份而不是按值定位）。"""
    recs = [_rec(1, "2026-09-01T10:00:00") for _ in range(3)]
    g = group_batches(recs, "双色球")[0]
    assert [batch_fields(r, g)["批次内序号"] for r in g.records] == [1, 2, 3]


# ------------------------------------------------------------
# 4. 接口
# ------------------------------------------------------------

API_HISTORY = {
    "双色球": [
        _rec(26100, "2026-09-01T10:00:00", "六等", batch="B1", idx=1),
        _rec(26100, "2026-09-01T10:00:01", "六等", batch="B1", idx=2),
        _rec(26100, "2026-09-01T10:00:02", "五等", batch="B2", idx=1),
    ],
}


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(fb, "load_feedback_history",
                        lambda name, lookback=None: list(API_HISTORY.get(name, [])))
    monkeypatch.setattr(pt, "prize_payout",
                        lambda lot, grade, issue=None: {"金额": 5.0, "浮动": False,
                                                        "来源": "固定奖级", "文本": "¥5", "备注": ""})
    monkeypatch.setattr(hits_route, "feedback_record_view",
                        lambda name, h: {"zones": [], "total_hit": 0, "total_choose": 0})
    app.config["TESTING"] = True
    return app.test_client()


def test_hits_records_carry_batch_fields(client):
    d = client.get("/api/hits?limit=0").get_json()
    assert d["total"] == 3
    for r in d["records"]:
        assert r["批次号"] in ("B1", "B2")
        assert r["批次来源"] == "explicit"
        assert r["批次规模"] == (2 if r["批次号"] == "B1" else 1)
        assert r["批次内序号"] >= 1
        assert r["期号内批号"] >= 1
        assert r["期号内批数"] == 2          # 同期 26100 有 B1/B2 两批
        assert r["批次标签"].startswith("第")
        assert r["批次内序号推算"] is False  # API_HISTORY 带了 idx


def test_hits_has_no_batch_endpoint(client):
    """批次汇总接口已按需求移除，不应再暴露。"""
    assert client.get("/api/hits/batches").status_code == 404


def test_page_has_no_batch_card(client):
    """页面不该再有批次卡片/下拉（只保留每条记录上的「第 X 批 · 第 Y 注」标签）。"""
    html = client.get("/hits").get_data(as_text=True)
    for token in ("hits-batch", "hits-batch-card", "中奖批次", "全部批次"):
        assert token not in html


# ------------------------------------------------------------
# 5. 批次详情：出号时间 + 该批全部号码
# ------------------------------------------------------------

from ev.batch import batch_detail  # noqa: E402


def _num_rec(issue, t, grade, batch, red, blue, extra=None):
    r = _rec(issue, t, grade, batch=batch)
    r["预测红球"] = red
    r["预测蓝球"] = blue
    r["批次时间"] = "2026-09-01T09:59:30"
    r.update(extra or {})
    return r


def test_batch_detail_explicit_time_is_second_precise():
    recs = [
        _num_rec(1, "2026-09-01T10:00:00", "六等", "B1", [1, 2, 3, 4, 5, 6], [7], {"批次时间": "2026-09-01T09:59:30"}),
        _num_rec(1, "2026-09-01T10:00:01", "未中", "B1", [8, 9, 10, 11, 12, 13], [8], {"批次时间": "2026-09-01T09:59:30"}),
        _num_rec(1, "2026-09-01T10:00:02", "未中", "B1", [14, 15, 16, 17, 18, 19], [9], {"批次时间": "2026-09-01T09:59:30"}),
    ]
    g = group_batches(recs, "双色球")[0]
    d = batch_detail(g, "双色球")
    # 出号时间取批次时间、精确到秒；整批 3 注都在（含未中奖的）
    assert d["出号时间"] == "2026-09-01 09:59:30"
    assert d["出号时间精确"] is True
    assert d["注数"] == 3
    assert d["中奖注数"] == 1
    assert d["原始注数"] == 3        # 未压缩批次：原始==登记
    assert [t["序号"] for t in d["号码"]] == [1, 2, 3]
    assert d["号码"][1]["中奖等级"] == "未中"
    assert d["号码"][0]["号码"] == {"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]}


def test_batch_detail_derived_falls_back_to_pred_date():
    recs = [_rec(1, "2026-09-01T10:00:00"), _rec(1, "2026-09-01T10:00:01")]
    for r in recs:
        r["预测日期"] = "2026-09-01"
    g = group_batches(recs, "双色球")[0]
    d = batch_detail(g, "双色球")
    assert d["出号时间"] == "2026-09-01"
    assert d["出号时间精确"] is False


def test_batch_detail_numeric_lottery_uses_zone_dict():
    recs = [{"期号": 1, "评估时间": "2026-09-01T10:00:00", "中奖玩法": "直选",
             "中奖等级": "未中", "预测号码": {"百位": [3], "十位": [5], "个位": [7]},
             "总命中": 2, "批次时间": "2026-09-01T09:00:00"}]
    g = group_batches(recs, "福彩3D")[0]
    d = batch_detail(g, "福彩3D")
    assert d["号码"][0]["号码"] == {"百位": [3], "十位": [5], "个位": [7]}
    assert d["中奖注数"] == 0   # 中奖等级=未中 → 不计入


def test_batch_detail_endpoint_returns_full_batch(client):
    """接口：按批次号取详情，返回该批全部注（含未中奖的）。"""
    resp = client.get("/api/hits/batch-detail?lottery=双色球&batch_id=B1")
    assert resp.status_code == 200
    d = resp.get_json()
    assert d["批次号"] == "B1"
    assert d["期号内批号"] == 1 and d["期号内批数"] == 2
    assert d["注数"] == 2 and d["中奖注数"] == 2
    assert len(d["号码"]) == 2
    # B2 只有 1 注，期号内是第 2 批
    d2 = client.get("/api/hits/batch-detail?lottery=双色球&batch_id=B2").get_json()
    assert d2["期号内批号"] == 2 and d2["注数"] == 1


def test_batch_detail_endpoint_validates_params(client):
    assert client.get("/api/hits/batch-detail?lottery=双色球").status_code == 400
    assert client.get("/api/hits/batch-detail?lottery=双色球&batch_id=NOPE").status_code == 404
    assert client.get("/api/hits/batch-detail?batch_id=B1").status_code == 400
