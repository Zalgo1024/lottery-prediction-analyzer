# -*- coding: utf-8 -*-
"""tests/test_renumber_regenerate.py —— 「重新出号」的台账契约（2026-11-02 用户需求）。

需求原文：
  「点了之后重新出一批号，但之前的号也不要删除，要储存起来随时可以查询，
    新出的号码在命中历史等地方优先采用新号，但如果旧号那边也有中的也要拉出来。」

拆成可验证的四条：
  1. 重出后**旧批仍在 pending 里**（`_upsert_pending` 的 key 含「重出批号」）；
  2. 当期展示/推送取**最新一批**（`latest_production_pending` 按记录时间取最大）；
  3. 开奖后**两批都结算**，旧批中的奖也进命中历史（不是只结算最新的）；
  4. 重出批号只在 `renumber_scope` 内生效，且首出记录**不带该字段**
     （老记录形态不变，不能让「缺字段」和「写了 0」被当成两批）。

★ 这里锁的仍然不是「策略有效」。重新出号只是换一批票面，单注中奖概率不变、
  整体 EV 仍为负 —— 多批的代价是中奖事件的**重复计数**（同一期买了两套票），
  所以每一批都必须可区分、可追溯，不能悄悄合并成一批。
"""
from datetime import date
from types import SimpleNamespace

import pytest

import data.feedback as fb
from data.feedback import (
    DOMAIN_PROD,
    _pending_key,
    latest_production_pending,
    load_pending,
    next_renumber,
    record_pending_prediction,
    renumber_scope,
)

LOT = "双色球"
ISSUE = 26110
DRAW_DATE = date(2026, 10, 22)


@pytest.fixture
def tmp_fb(tmp_path, monkeypatch):
    monkeypatch.setattr(fb, "_pending_path", lambda name: tmp_path / f"{name}_pending.json")
    monkeypatch.setattr(fb, "_history_path", lambda name: tmp_path / f"{name}_history.json")
    monkeypatch.setattr(fb, "_weights_path", lambda name: tmp_path / f"{name}_weights.json")
    # 隔离「马后炮」防御（另有独立测试）：它按真实开奖日历判断，
    # 不隔离会随运行日期漂移到「该期已开奖」而随机失败。
    monkeypatch.setattr(fb, "load_lottery", lambda name: SimpleNamespace(records=[]))
    monkeypatch.setattr(fb, "_target_draw_date", lambda lot, issue: None)
    return tmp_path


def _tickets(reds_list, strategy="高频策略", blue=1):
    """用给定的红球列表造票（每张必须不同，否则会被重复压缩掉）。"""
    return [{"红球": list(r), "蓝球": [blue], "策略": strategy, "置信度": 0.3}
            for r in reds_list]


def _pred(reds_list, *, rn=None, strategy="高频策略", issue=ISSUE):
    p = {
        "目标期号": issue,
        "预测日期": str(DRAW_DATE),
        "生成时间": f"{DRAW_DATE}T09:00:00",   # 开奖日 21:00 之前 ⇒ 真预测
        "来源": "predict",
        "目标注数": len(reds_list),
        "预测号码": _tickets(reds_list, strategy),
    }
    if rn is not None:
        p["重出批号"] = rn
    return p


WIN_REDS = [1, 2, 3, 4, 5, 6]
LOSE_REDS = [10, 11, 12, 13, 14, 15]


# ---------------------------------------------------------------- ① 旧批不删
def test_regenerate_keeps_previous_batch(tmp_fb):
    record_pending_prediction(LOT, _pred([WIN_REDS, LOSE_REDS]))
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([LOSE_REDS, [20, 21, 22, 23, 24, 25]]))

    rows = [r for r in load_pending(LOT) if str(r.get("目标期号")) == str(ISSUE)]
    assert len(rows) == 2, f"重出把旧批覆盖掉了：{[(r.get('重出批号'), len(r.get('预测号码') or [])) for r in rows]}"
    rns = sorted(int(r.get("重出批号") or 0) for r in rows)
    assert rns == [0, 1]
    # 旧批的号码还在（用户要「随时可以查询」）
    old = next(r for r in rows if int(r.get("重出批号") or 0) == 0)
    assert any(t["红球"] == WIN_REDS for t in old["预测号码"])


def test_first_publish_has_no_renumber_field(tmp_fb):
    """首出不写该字段 —— 与历史记录形态一致，避免「缺字段 vs 0」拆成两批。"""
    record_pending_prediction(LOT, _pred([WIN_REDS]))
    rec = latest_production_pending(load_pending(LOT), target_issue=ISSUE)
    assert rec is not None
    assert "重出批号" not in rec


def test_renumber_scope_does_not_leak(tmp_fb):
    with renumber_scope(3):
        record_pending_prediction(LOT, _pred([WIN_REDS]))
    record_pending_prediction(LOT, _pred([LOSE_REDS]))     # 作用域外 ⇒ 首出
    rns = sorted(int(r.get("重出批号") or 0) for r in load_pending(LOT))
    assert rns == [0, 3], f"作用域泄漏或未生效：{rns}"


# ---------------------------------------------------------------- ② 新号优先
def test_latest_pending_prefers_new_batch(tmp_fb):
    record_pending_prediction(LOT, _pred([WIN_REDS]))
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([LOSE_REDS]))
    rec = latest_production_pending(load_pending(LOT), target_issue=ISSUE)
    assert int(rec.get("重出批号") or 0) == 1, "当期展示没有优先采用新号"
    assert rec["预测号码"][0]["红球"] == LOSE_REDS


def test_next_renumber_counts_only_this_issue_production(tmp_fb):
    assert next_renumber(LOT, ISSUE) == 1            # 还没有任何批 ⇒ 首个重出是 1
    record_pending_prediction(LOT, _pred([WIN_REDS]))
    assert next_renumber(LOT, ISSUE) == 1            # 有首出(0) ⇒ 重出 1
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([LOSE_REDS]))
    assert next_renumber(LOT, ISSUE) == 2
    # 别的期号不受影响
    assert next_renumber(LOT, ISSUE + 1) == 1


def test_next_renumber_without_issue_does_not_collapse_to_one(tmp_fb, monkeypatch):
    """推不出期号时必须继续递增，不许退回 1。

    「返回 1」看起来是安全默认值，实际上会与已有的第一批撞去重键 ⇒
    第二次重出把第一批**覆盖**掉，用户就查不到旧号了。这条测试盯着它。
    """
    monkeypatch.setattr(fb, "_next_issue", lambda name: None)
    record_pending_prediction(LOT, _pred([WIN_REDS]))
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([LOSE_REDS]))
    assert next_renumber(LOT) == 2
    assert len(load_pending(LOT)) == 2, "期号未知时重出把旧批覆盖掉了"


# ---------------------------------------------------------------- ③ 流水线重出
def test_pipeline_regenerate_bumps_batch_only_when_recording(tmp_fb):
    """调度器那条路（改完滑轨后自动重出）也必须另起一批，不许覆盖旧批。

    三个分支都要盯：
      · 首次出号（本期还没有批）⇒ 0，不要平白造一个「第 1 批」；
      · 试算（record_pending=False）⇒ 0，否则点几次试算就把批号推高，
        真正第一版的号码就顶着「第 3 批」的名字；
      · 已有批 + 真要落库 ⇒ 1（在旧批基础上递增）。
    """
    from web.utils import _renumber_for_regenerate

    assert _renumber_for_regenerate(LOT, ISSUE, None, True) == 0
    record_pending_prediction(LOT, _pred([WIN_REDS]))
    existing = latest_production_pending(load_pending(LOT), target_issue=ISSUE)
    assert existing is not None
    assert _renumber_for_regenerate(LOT, ISSUE, existing, False) == 0
    assert _renumber_for_regenerate(LOT, ISSUE, existing, True) == 1
    # 走一遍真作用域，确认旧批（0）与新批（1）在 pending 里并存
    with renumber_scope(_renumber_for_regenerate(LOT, ISSUE, existing, True)):
        record_pending_prediction(LOT, _pred([LOSE_REDS]))
    rns = sorted(int(r.get("重出批号") or 0) for r in load_pending(LOT))
    assert rns == [0, 1], f"流水线重出没有并存两批：{rns}"


# ---------------------------------------------------------------- ③b CLI 同口径
def test_cli_auto_regenerate_also_appends_batch():
    """`cli.py auto` 那条路也必须另起一批，不许按覆盖写盘。

    ★ 这条是**源码级契约**（不跑 `_cmd_auto`，它要抓数据/评估/训练，代价过大）。
      它只能保证「接线还在」，证明不了运行行为 —— 运行证据由上面的
      `test_pipeline_regenerate_bumps_batch_only_when_recording`（web 路径，真跑）
      与端到端实测共同承担。两者说的不是同一件事，不能互相顶替。
    """
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "cli.py").read_text(encoding="utf-8-sig")
    start = src.find("def _cmd_auto(")
    assert start >= 0, "cli.py 里找不到 _cmd_auto"
    body = src[start:]
    assert "renumber_scope" in body, \
        "cli.py auto 出号没走重出批号作用域 ⇒ 改滑轨后重生成会把上一批号码覆盖掉"
    assert "next_renumber" in body, "cli.py auto 没有申请重出批号"
    # 试算不得把批号推高
    assert "not args.no_record" in body, \
        "cli.py auto 没有按「是否真落盘」决定是否另起批次（试算会推高批号）"


# ---------------------------------------------------------------- ③c 两批 = 两笔钱
def test_two_batches_are_two_spend_records(tmp_fb, tmp_path, monkeypatch):
    """同一期两批票必须记成**两笔**预算，不能按幂等键合并成一笔。

    ★ 这是钱，不是显示：台账幂等键若不含「重出批号」，第 2 批会覆盖第 1 批那一行，
      「本月已投」偏低 ⇒ 预算闸门比配置更松，而且不会有任何报错。
    """
    import ev.budget_gate as bg

    monkeypatch.setattr(bg, "SPEND_PATH", tmp_path / "budget_spend.json")
    record_pending_prediction(LOT, _pred([WIN_REDS]))
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([LOSE_REDS]))

    doc = bg.load_spend()
    rows = [r for r in doc["记录"] if r.get("彩种") == LOT]
    assert len(rows) == 2, f"两批票只记了 {len(rows)} 笔钱：{rows}"
    assert sorted(int(r.get("重出批号") or 0) for r in rows) == [0, 1]
    price = float(bg.ticket_price(LOT))
    assert doc["已投"] == pytest.approx(2 * 1 * price), \
        f"已投 {doc['已投']} 与「2 批 × 1 注 × {price} 元」不符"

    # 同一批重写（自愈/重生成）仍然只留一条 —— 幂等不能被破坏
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([LOSE_REDS]))
    doc2 = bg.load_spend()
    assert len([r for r in doc2["记录"] if r.get("彩种") == LOT]) == 2, \
        "同批复写被当成了新的一笔（幂等失效 ⇒ 金额虚增）"


# ---------------------------------------------------------------- ④ 两批都结算
def test_old_batch_hits_are_still_settled(tmp_fb, monkeypatch):
    """旧批（重出前）中的奖必须照常进历史 —— 这是需求的「也要拉出来」。"""
    draw = SimpleNamespace(
        期号=ISSUE, 开奖日期=DRAW_DATE,
        红球=WIN_REDS, 蓝球=[1],
        zone_numbers={"红球": WIN_REDS, "蓝球": [1]},
    )
    monkeypatch.setattr(fb, "_find_draw_by_issue", lambda lot, issue: draw)
    monkeypatch.setattr(fb, "_find_draw_by_date", lambda lot, d: draw)

    record_pending_prediction(LOT, _pred([WIN_REDS]))        # 旧批：中一等奖
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([LOSE_REDS]))   # 新批：不中

    out = fb.evaluate_pending_predictions(LOT)
    hist = fb.load_feedback_history(LOT)
    assert len(hist) == 2, f"只结算了 {len(hist)} 批（两批都在待开奖里）"

    grades = [h.get("中奖等级") for h in hist]
    assert any(g and g != "未中" for g in grades), f"旧批中的奖没被拉出来：{grades}"
    # 旧批那条必须能认出来（批号 0 即重出前），否则事后无法区分「哪套票中的」
    by_rn = {}
    for h in hist:
        by_rn[int(h.get("重出批号") or 0)] = h.get("中奖等级")
    assert by_rn.get(0, "未中") != "未中", f"重出前批次被判未中：{by_rn}"
    assert out["new_feedback_count"] == 2


def test_identical_ticket_in_two_batches_counts_twice(tmp_fb, monkeypatch):
    """两批里出现**同一张票**，两条都得进历史。

    历史去重键原本是 (域,档位,类型,期号,策略,号码) —— 不含批号。数字型号码空间很小
    （排列3 只有 1000 种），两批必然撞号；把撞号的票当「重复预测」丢掉，就等于
    「旧号那批中了却没拉出来」。所以批号必须进历史去重键。
    """
    draw = SimpleNamespace(期号=ISSUE, 开奖日期=DRAW_DATE, 红球=WIN_REDS, 蓝球=[1],
                           zone_numbers={"红球": WIN_REDS, "蓝球": [1]})
    monkeypatch.setattr(fb, "_find_draw_by_issue", lambda lot, issue: draw)
    monkeypatch.setattr(fb, "_find_draw_by_date", lambda lot, d: draw)

    record_pending_prediction(LOT, _pred([WIN_REDS]))
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([WIN_REDS]))    # 票面完全相同，但是另一批

    out = fb.evaluate_pending_predictions(LOT)
    hist = fb.load_feedback_history(LOT)
    assert out["new_feedback_count"] == 2, "两批里同一张票被当成重复丢了一条"
    assert sorted(int(h.get("重出批号") or 0) for h in hist) == [0, 1]


# ---------------------------------------------------------------- ④ 键结构
def test_pending_key_distinguishes_batches_but_not_missing_field():
    base = {"目标期号": ISSUE, "策略": "高频策略"}
    assert _pending_key(base) == _pending_key(dict(base, **{"重出批号": 0}))
    assert _pending_key(base) != _pending_key(dict(base, **{"重出批号": 1}))


# ---------------------------------------------------------------- ⑤ 接口编排
def test_regenerate_api_rejects_unknown_lottery():
    """未知彩种必须 400 —— 不能悄悄按默认彩种出一批号。

    这台机器上「静默按默认值继续」已经踩过好几次（argparse choices 吃掉拒绝分支、
    前端漏登记彩种导致注数保存不上去），所以入口处的拒绝分支要有测试盯着它**能走到**。
    """
    from web.app import app
    c = app.test_client()
    r = c.post("/api/predict/regenerate", json={"lottery": "不存在的彩种"})
    assert r.status_code == 400
    body = r.get_json()
    assert body["ok"] is False and "未知彩种" in body["error"]


def test_regenerate_api_starts_task_with_renumber(tmp_fb, monkeypatch):
    """路由必须：注数交给滑轨（groups=None）、写 pending、带上递增的重出批号。"""
    import web.routes.predict as P
    from web.app import app

    calls = {}
    monkeypatch.setattr(P, "start_predict",
                        lambda lot, params: calls.update(lot=lot, params=params) or "task-x")

    c = app.test_client()
    r1 = c.post("/api/predict/regenerate", json={"lottery": LOT})
    assert r1.status_code == 200
    d1 = r1.get_json()
    assert d1["ok"] and d1["task_id"] == "task-x"
    assert d1["重出批号"] == 1
    # ★ groups 必须是 None：否则会绕过「出号数量」滑轨，
    #   出现「滑轨改成 30、点重出还是 5 注」的自相矛盾。
    assert calls["params"]["groups"] is None
    assert calls["params"]["record_pending"] is True
    assert calls["params"]["renumber"] == 1
    assert calls["lot"] == LOT

    # 模拟这一批已落账 ⇒ 下一次重出必须递增
    with renumber_scope(1):
        record_pending_prediction(LOT, _pred([WIN_REDS]))
    r2 = c.post("/api/predict/regenerate", json={"lottery": LOT})
    assert r2.get_json()["重出批号"] == 2
    assert r2.get_json()["上一批注数"] >= 1
