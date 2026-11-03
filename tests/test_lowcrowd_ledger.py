# -*- coding: utf-8 -*-
"""tests/test_lowcrowd_ledger.py —— 低拥挤研究台账的「台账域」契约。

★ 这里锁的不是「策略有效」，而是**研究批不许改变生产口径**：

  1. 生产批与研究批同期待开奖必须**共存**，谁也不能删掉谁
     （修之前：低拥挤没设档位 ⇒ 被归「一般」⇒ 直接把同期生产 pending 抹掉）；
  2. 批次号跨域不许撞号（撞了会让压缩 KPI、命中页「本批 M 注」把两批并成一批）；
  3. 三档对比 / 权重学习 / 命中率安全阀 / 压缩 KPI / 组数体检 / 微信推送
     全部只看生产批 —— 研究批每期 200 行，漏一处就会静默改写生产决策；
  4. 战绩榜与命中页**反过来**要能看到研究行（这是需求本身）。

全部用 tmp_path 隔离，不碰真实反馈数据。
"""
import json

import pytest

import data.feedback as fb
from data.feedback import (
    DOMAIN_KEY,
    DOMAIN_PROD,
    DOMAIN_RESEARCH,
    TIER_GENERAL,
    _domain_of,
    _make_batch_id,
    _pending_key,
    latest_production_pending,
    record_pending_prediction,
    save_pending,
    tier_comparison,
    update_strategy_weights,
    _recent_hit_stats,
    _last_compress_kpi,
)

LOT = "双色球"


@pytest.fixture
def tmp_fb(tmp_path, monkeypatch):
    """pending / history / weights 三个路径全部指到 tmp。"""
    from types import SimpleNamespace

    monkeypatch.setattr(fb, "_pending_path", lambda name: tmp_path / f"{name}_pending.json")
    monkeypatch.setattr(fb, "_history_path", lambda name: tmp_path / f"{name}_history.json")
    monkeypatch.setattr(fb, "_weights_path", lambda name: tmp_path / f"{name}_weights.json")
    # 隔离「马后炮」防御（有独立测试）：它按真实开奖日历判断，
    # 不隔离的话本文件会随日期漂移到「该期已开奖」而随机失败。
    monkeypatch.setattr(fb, "load_lottery", lambda name: SimpleNamespace(records=[]))
    monkeypatch.setattr(fb, "_target_draw_date", lambda lot, issue: None)
    return tmp_path


def _tickets(strategy, n=3, blue=1):
    """n 张**互不相同**的票。

    必须不同：`record_pending_prediction` 会走 `ev.coverage.compress_tickets`，
    重复票会被「重复淘汰」压成 1 张，测试里就会看到注数和预期对不上。
    """
    from itertools import combinations, islice

    return [{"红球": list(c), "蓝球": [blue], "策略": strategy, "置信度": 0.3}
            for c in islice(combinations(range(1, 34), 6), n)]


def _pred(strategy, n=3, issue=26110):
    # 带上「目标注数」（＝引擎的真实行为）：否则 compress_tickets 会按重叠/重复
    # 削减票数，「写入 7 注读出来 5 注」这种噪声会盖掉本文件真正要测的东西。
    return {"目标期号": issue, "预测日期": "2026-10-22", "目标注数": n,
            "预测号码": _tickets(strategy, n)}


# ---------------------------------------------------------------- ① 域推导

def test_domain_derivation_and_backward_compat():
    """无「台账域」的旧记录不许被判成研究批。"""
    assert _domain_of({}) == DOMAIN_PROD
    assert _domain_of({"策略": "高频策略"}) == DOMAIN_PROD
    assert _domain_of({"策略": "低拥挤策略"}) == DOMAIN_RESEARCH
    # 顶层没有策略 → 回退首注票面（低拥挤的历史形态）
    assert _domain_of({"预测号码": [{"策略": "低拥挤策略"}]}) == DOMAIN_RESEARCH
    # 显式字段优先于推导
    assert _domain_of({DOMAIN_KEY: DOMAIN_PROD, "策略": "低拥挤策略"}) == DOMAIN_PROD


def test_pending_key_separates_domains():
    prod = {"目标期号": 1, "策略": "高频策略"}
    res = {"目标期号": 1, "策略": "低拥挤策略"}
    # 第 4 维是「重出批号」（2026-11-02 重新出号）：首出缺省归一化为 0，
    # 不是新开一维 —— 老记录没这个字段也必须与显式 0 判等，否则同一批会被拆成两条。
    assert _pending_key(prod) == (1, TIER_GENERAL, DOMAIN_PROD, 0)
    assert _pending_key(res) == (1, TIER_GENERAL, DOMAIN_RESEARCH, 0)
    assert _pending_key(prod) != _pending_key(res)
    # 重出批号不同 ⇒ 同期待开奖的两批并存（旧批不许被新批覆盖）
    assert _pending_key(prod) != _pending_key(dict(prod, **{"重出批号": 1}))
    assert _pending_key(dict(prod, **{"重出批号": 1})) == (1, TIER_GENERAL, DOMAIN_PROD, 1)
    assert _pending_key({"目标期号": 1, "重出批号": 2}) == (1, TIER_GENERAL, DOMAIN_PROD, 2)


# ---------------------------------------------------------------- ② 共存 / 替换

def test_research_write_does_not_delete_production(tmp_fb):
    """★ 核心回归：先写研究批再写生产批（以及反序），两批都必须在。"""
    record_pending_prediction(LOT, _pred("低拥挤策略", 7))
    record_pending_prediction(LOT, _pred("高频策略", 3))

    pend = fb.load_pending(LOT)
    assert len(pend) == 2
    by_domain = {_domain_of(p): p for p in pend}
    assert len(by_domain[DOMAIN_PROD]["预测号码"]) == 3
    assert len(by_domain[DOMAIN_RESEARCH]["预测号码"]) == 7
    # 生产批没有被研究批改过批次号
    assert "-生产-" in by_domain[DOMAIN_PROD]["批次号"]
    assert "-研究-" in by_domain[DOMAIN_RESEARCH]["批次号"]


def test_same_domain_still_replaces(tmp_fb):
    """同一域内仍然是「只留最新一条」（原有语义不许被放松）。"""
    record_pending_prediction(LOT, _pred("高频策略", 3))
    record_pending_prediction(LOT, _pred("遗漏值策略", 5))
    pend = fb.load_pending(LOT)
    assert len(pend) == 1
    assert len(pend[0]["预测号码"]) == 5


# ---------------------------------------------------------------- ③ 批次号

def test_batch_id_carries_domain_and_stays_prefix_compatible():
    ts = "2026-10-22T22:05:12.483000"
    prod = {"目标期号": 26110, "记录时间": ts, "策略": "高频策略"}
    res = {"目标期号": 26110, "记录时间": ts, "策略": "低拥挤策略"}
    a = _make_batch_id(LOT, prod)
    b = _make_batch_id(LOT, res)
    assert a != b, "同一秒、同档位、不同域必须撞不出同一个批次号"
    assert "-生产-" in a and "-研究-" in b
    # 旧断言口径（tests/test_batch.py 只看前缀）不许被打破
    assert a.startswith(f"{LOT}-26110-{TIER_GENERAL}-")
    assert b.startswith(f"{LOT}-26110-{TIER_GENERAL}-")


def test_batch_id_unique_for_same_base():
    p = {"目标期号": 26110, "记录时间": "2026-10-22T22:05:12.483000", "策略": "高频策略"}
    a = _make_batch_id(LOT, p)
    b = _make_batch_id(LOT, p)
    assert a != b and b.startswith(a)


# ---------------------------------------------------------------- ④ 聚合必须排除研究域

def _write_history(tmp_fb, rows):
    (tmp_fb / f"{LOT}_history.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")


def _fb_row(strategy, win=False, batch=None, compress=None, issue=26100):
    d = {"期号": issue, "策略": strategy, "valid_prediction": True, "档位": TIER_GENERAL,
         "预测红球": [1, 2, 3, 4, 5, 6], "预测蓝球": [7], "红球命中": 1, "蓝球命中": 1,
         "总命中": 2, "中奖等级": "六等" if win else "未中", "评估时间": "2026-10-22T00:00:00"}
    if batch:
        d["批次号"] = batch
        d["批次规模"] = 10
    if compress:
        d["压缩"] = compress
    return d


def test_tier_comparison_excludes_research(tmp_fb):
    """★ 最要紧的一处：研究批也是「一般」档，不排除会把三档统计稀释掉。"""
    rows = [_fb_row("高频策略") for _ in range(4)]
    rows += [_fb_row("低拥挤策略", win=True) for _ in range(5)]
    _write_history(tmp_fb, rows)

    out = tier_comparison(LOT)
    assert set(out["tiers"]) == {"一般", "稳健", "高鲁棒"}, "不许出现第 4 个档位"
    assert out["tiers"]["一般"]["预测注数"] == 4
    assert out["tiers"]["一般"]["中奖次数"] == 0


def test_weights_still_one_third_with_research_history(tmp_fb):
    """有 200 条研究历史时，三个生产策略的权重仍必须是 1/3。"""
    rows = []
    for s in ("高频策略", "遗漏值策略", "区间均衡策略"):
        rows += [_fb_row(s) for _ in range(12)]
    rows += [_fb_row("低拥挤策略", win=True) for _ in range(200)]
    _write_history(tmp_fb, rows)

    w = update_strategy_weights(LOT)
    assert "低拥挤策略" not in w
    assert len(w) == 3, w
    for v in w.values():
        assert abs(v - 1 / 3) < 1e-6, w


def test_recent_hit_stats_filters_before_truncating(tmp_fb):
    """★ 先过滤再截断：研究批在后，先截断会把生产样本挤空 ⇒ 安全阀按假信号改注数。"""
    rows = [_fb_row("高频策略", win=(i % 2 == 0)) for i in range(10)]      # 前 10 条生产
    rows += [_fb_row("低拥挤策略", win=True) for _ in range(200)]          # 后 200 条研究
    _write_history(tmp_fb, rows)

    r = _recent_hit_stats(LOT, lookback=5)
    assert r["hit_n"] == 5, f"应取最后 5 条**生产**记录，实得 {r}"
    # i 取 5..9，其中 i=6、8 命中 ⇒ 2/5。若改成「先截断再过滤」，取到的会是研究批，
    # hit_n 变成 0（或 hit_rate=1.0）—— 这正是这条断言要挡的。
    assert r["hit_rate"] == pytest.approx(0.4)


def test_last_compress_kpi_ignores_research(tmp_fb):
    """研究批的压缩 KPI 不许驱动动态注数滑轨。"""
    prod = [_fb_row("高频策略", batch="B1", compress={"质量淘汰": 2, "重复淘汰": 0,
                                                      "重叠淘汰": 0, "启用": True})
            for _ in range(3)]
    _write_history(tmp_fb, prod)
    base = _last_compress_kpi(LOT)
    assert base and base.get("启用") is True

    # 追加研究批（带压缩记录）后，结果必须不变
    _write_history(tmp_fb, prod + [_fb_row("低拥挤策略", batch="R1",
                                           compress={"质量淘汰": 9, "重复淘汰": 9,
                                                     "重叠淘汰": 9, "启用": True})
                                   for _ in range(5)])
    assert _last_compress_kpi(LOT) == base


def test_latest_production_pending_prefers_general():
    pend = [
        {"目标期号": 1, "档位": TIER_GENERAL, "策略": "高频策略", "记录时间": "2026-10-22T01:00:00"},
        {"目标期号": 1, "档位": "高鲁棒", "策略": "高频策略", "记录时间": "2026-10-22T02:00:00"},
        {"目标期号": 1, "策略": "低拥挤策略", "记录时间": "2026-10-22T03:00:00"},
    ]
    got = latest_production_pending(pend, target_issue=1)
    assert got is not None and got["档位"] == TIER_GENERAL
    # 只有研究批时必须返回 None（视作「无生产 pending」，触发补跑）
    assert latest_production_pending([pend[2]], target_issue=1) is None


# ---------------------------------------------------------------- ⑤ 生产侧消费方

def test_groups_invariant_ignores_research(tmp_fb, monkeypatch):
    """组数体检必须拿生产批比滑轨；研究批 200 注不许让它「假通过」。"""
    import config
    import web.utils as wu

    monkeypatch.setattr(config, "resolve_groups", lambda *a, **k: 37)
    monkeypatch.setattr(fb, "_next_issue", lambda name: 26110)

    save_pending(LOT, [
        {"目标期号": 26110, "档位": TIER_GENERAL, "策略": "高频策略",
         "预测号码": _tickets("高频策略", 37), "记录时间": "2026-10-22T01:00:00"},
        {"目标期号": 26110, "策略": "低拥挤策略",
         "预测号码": _tickets("低拥挤策略", 200), "记录时间": "2026-10-22T02:00:00"},
    ])
    out = wu.groups_invariant([LOT])
    row = out["彩种"][0]
    assert row["组数"] == 37 and row["状态"] == "一致", row


def test_digest_payload_uses_production_pending(tmp_fb, monkeypatch):
    """微信推送不许把 200 注研究票当成本期出号。"""
    import config
    import data.push_notify as pn

    monkeypatch.setattr(config, "resolve_groups", lambda *a, **k: 3)
    monkeypatch.setattr(pn, "_latest_draw", lambda lot: {})

    save_pending(LOT, [
        {"目标期号": 26110, "档位": TIER_GENERAL, "策略": "高频策略",
         "预测号码": _tickets("高频策略", 3), "记录时间": "2026-10-22T01:00:00"},
        {"目标期号": 26110, "策略": "低拥挤策略",
         "预测号码": _tickets("低拥挤策略", 200), "记录时间": "2026-10-22T02:00:00"},
    ])
    payload = pn.digest_payload(LOT)
    assert payload is not None
    assert len(payload["predictions"]) == 3

    # 只剩研究批 ⇒ 不许推 200 注
    save_pending(LOT, [{"目标期号": 26110, "策略": "低拥挤策略",
                        "预测号码": _tickets("低拥挤策略", 200),
                        "记录时间": "2026-10-22T02:00:00"}])
    assert pn.digest_payload(LOT) is None


# ---------------------------------------------------------------- ⑥ 战绩榜要看到研究行

def test_records_leaderboard_shows_lowcrowd_row(tmp_fb, monkeypatch):
    """需求本身：研究批要出现在战绩榜，且排在区间均衡之后。"""
    import data.records as rec

    monkeypatch.setattr(rec, "FEEDBACK_DIR", tmp_fb)
    rows = [_fb_row("高频策略") for _ in range(3)]
    rows += [_fb_row("低拥挤策略", win=True) for _ in range(4)]
    # data.records 读的文件名是 `<彩种>_feedback_history.json`，与 feedback 侧
    # `_history_path`（`<彩种>_history.json`）不同名，这里必须按前者的口径落盘。
    (tmp_fb / f"{LOT}_feedback_history.json").write_text(
        json.dumps(rows, ensure_ascii=False), encoding="utf-8")

    out = rec.get_feedback_records(LOT)
    names = list(out["strategies"])
    assert "低拥挤策略" in names
    assert names.index("低拥挤策略") > names.index("高频策略")
    assert out["strategies"]["低拥挤策略"]["样本数"] == 4
    # 两个域各自取样，行不互相污染
    assert out["strategies"]["高频策略"]["样本数"] == 3
