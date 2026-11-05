# -*- coding: utf-8 -*-
"""pending 写保护与字段同步回归（2026-09-20）。

锁住两个真实事故：
1. ★ `predict()` 泛型分支漏传 `record_pending` → record_pending=False 的验证/
   诊断出号**覆写生产 pending**（福彩3D 09-20 实际发生；rule 分支同类 bug 此前已修）。
2. `record_pending_prediction` 压缩后只更新批次规模、不同步 `号码组数`
   → 审计「号码组数不符」每晚误报（契约：号码组数 == len(预测号码)）。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def fb_dir(monkeypatch, tmp_path):
    """FEEDBACK_DIR 重定向到 tmp，测试绝不触碰真身 training/feedback。"""
    from data import feedback as fb
    monkeypatch.setattr(fb, "FEEDBACK_DIR", tmp_path)
    return tmp_path


def _digital_ticket(zone="第1位", n=1):
    return {"号码": {zone: [n]}, "策略": "高频策略", "置信度": 0.35, "类型": "single"}


def test_generic_predict_respects_record_pending_false(monkeypatch, fb_dir):
    """record_pending=False 的泛型出号（数字型路径）绝不能写 pending。"""
    from prediction import engine
    calls = []
    monkeypatch.setattr("data.feedback.record_pending_prediction",
                        lambda lot, pred: calls.append(lot))
    r = engine.predict("福彩3D", mode="fresh", record_pending=False, groups=5)
    assert r["号码组数"] == 5
    assert calls == [], "record_pending=False 仍写 pending（泛型分支漏传）"


def test_generic_predict_record_pending_true_writes_once(monkeypatch, fb_dir):
    from prediction import engine
    calls = []
    monkeypatch.setattr("data.feedback.record_pending_prediction",
                        lambda lot, pred: calls.append(lot))
    engine.predict("福彩3D", mode="fresh", record_pending=True, groups=5)
    assert calls == ["福彩3D"]


def test_record_pending_syncs_group_count_after_compress(monkeypatch, fb_dir):
    """压缩后 号码组数 必须等于 len(预测号码)（此前不同步 → 审计每晚误报）。"""
    from data.feedback import record_pending_prediction
    from pathlib import Path as _P

    def fake_compress(tickets, lottery, **kwargs):
        return [0, 1, 2], {"压缩前注数": len(tickets), "压缩后注数": 3,
                           "质量淘汰": 0, "重复淘汰": 0, "重叠淘汰": 0,
                           "启用": True, "说明": "test"}

    monkeypatch.setattr("ev.coverage.compress_tickets", fake_compress)
    pred = {
        "预测日期": "2099-01-01",
        "目标期号": 99999,
        "来源": "predict",
        "号码组数": 10,                 # 压缩前的陈旧值
        "预测号码": [_digital_ticket(n=i) for i in range(10)],
    }
    record_pending_prediction("排列3", pred)
    saved = json.loads((fb_dir / "排列3_pending.json").read_text(encoding="utf-8"))
    recs = saved if isinstance(saved, list) else saved.get("records", [])
    r = recs[-1]
    assert len(r["预测号码"]) == 3
    assert r["号码组数"] == 3, "压缩后号码组数未同步"
    assert r["批次规模"] == 3


def test_health_check_uses_declared_target(monkeypatch, fb_dir):
    """体检的期望注数优先取 pending 的目标注数（生成时刻快照），非实时配置。"""
    import scripts.health_check as hc
    # ★ hc.FB 是模块级常量（指向真身 training/feedback），patch data.feedback.FEEDBACK_DIR
    #   对它无效 → 必须同时重定向 hc.FB，否则本测试读到的是真实 pending，
    #   会随「福彩3D 是否恰好有未结算预测」而 flaky（2026-09-30 实测：真实 pending 清空 → 失败）。
    monkeypatch.setattr(hc, "FB", fb_dir)
    pend = [{"状态": "pending", "目标期号": 2026999,
             "目标注数": 35, "预测号码": [_digital_ticket(n=i) for i in range(35)]}]
    (fb_dir / "福彩3D_pending.json").write_text(
        json.dumps(pend, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(hc, "resolve_groups", lambda name: 37)  # 实时配置≠生成时
    # 同上：hc 顶部 `from data.loader import load_lottery` 已绑定到自己的模块名，
    # patch data.loader 不生效 → 必须 patch hc.load_lottery，期号才可控。
    fake_ld = lambda name: type("LD", (), {"records": [type("R", (), {"期号": 2026998})]})()
    monkeypatch.setattr(hc, "load_lottery", fake_ld)
    rows = hc.check_pending_freshness()
    row = next(r for r in rows if r["彩种"] == "福彩3D")
    assert row["问题"] == [], f"生成时刻快照应放行，实际: {row['问题']}"


def _stub_hc_freshness(monkeypatch, hc, fb_dir, pend, latest=2026998):
    """把体检的落盘/开奖/实时配置钉死，只留 pending 结构做变量。"""
    monkeypatch.setattr(hc, "FB", fb_dir)
    (fb_dir / "福彩3D_pending.json").write_text(
        json.dumps(pend, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(hc, "resolve_groups", lambda name: 37)
    fake_ld = lambda name: type("LD", (), {"records": [type("R", (), {"期号": latest})]})()
    monkeypatch.setattr(hc, "load_lottery", fake_ld)
    return hc.check_pending_freshness()


def _pending_batch(issue, target, size, stamp, **extra):
    r = {"状态": "pending", "目标期号": issue, "目标注数": target,
         "预测号码": [_digital_ticket(n=i) for i in range(size)],
         "记录时间": stamp, "台账域": "生产", "预测模式": "一般"}
    r.update(extra)
    return r


def test_health_check_counts_only_latest_production_batch(monkeypatch, fb_dir):
    """★ 重出号会往同一期追加多批：出号注数只认最新生产批，旧批显式披露。

    11-05 实踩：排列5 同期 4 批（100 + 1000×3）。旧口径把 3100 注全加起来、
    却拿数组里**最旧一批**的「目标注数 100」当期望 ⇒ 3 个数字型彩种假告警。
    """
    import scripts.health_check as hc
    pend = [
        _pending_batch(2026999, 100, 100, "2026-11-04T22:06:41"),
        _pending_batch(2026999, 1000, 1000, "2026-11-05T01:48:39", 重出批号=1),
        _pending_batch(2026999, 1000, 1000, "2026-11-05T14:49:36", 重出批号=2),
        _pending_batch(2026999, 1000, 1000, "2026-11-05T14:56:48", 重出批号=3),
    ]
    rows = _stub_hc_freshness(monkeypatch, hc, fb_dir, pend)
    row = next(r for r in rows if r["彩种"] == "福彩3D")
    assert row["问题"] == [], f"同期多批不该报出号注数不符，实际: {row['问题']}"
    assert row["出号注数"] == 1000, "出号注数必须与最新生产批一致"
    assert row["生产批数"] == 4
    # 旧批合计 = 100 + 1000 + 1000 = 2100（总 3100 减去当期有效批 1000）
    assert row["重出批数"] == 3 and row["重出批注数"] == 2100, "旧批不许静默丢弃"


def test_health_check_expectation_comes_from_same_batch(monkeypatch, fb_dir):
    """★ 期望值不许从别的批取：最新批无目标注数时回退实时配置，而非旧批的 100。"""
    import scripts.health_check as hc
    pend = [
        _pending_batch(2026999, 100, 100, "2026-11-04T22:06:41"),
        # 最新批刻意不带「目标注数」，实时配置 37 注 ⇒ 37 注才放行
        _pending_batch(2026999, None, 37, "2026-11-05T14:56:48", 重出批号=1),
    ]
    rows = _stub_hc_freshness(monkeypatch, hc, fb_dir, pend)
    row = next(r for r in rows if r["彩种"] == "福彩3D")
    assert row["问题"] == [], f"应回退实时配置 37 而非旧批 100，实际: {row['问题']}"
    assert row["出号注数"] == 37


def test_health_check_still_flags_real_mismatch(monkeypatch, fb_dir):
    """★ 修口径 ≠ 关检查：最新批自己的注数与目标注数不符，仍必须报出来。"""
    import scripts.health_check as hc
    pend = [
        _pending_batch(2026999, 100, 100, "2026-11-04T22:06:41"),
        _pending_batch(2026999, 1000, 999, "2026-11-05T14:56:48", 重出批号=1),
    ]
    rows = _stub_hc_freshness(monkeypatch, hc, fb_dir, pend)
    row = next(r for r in rows if r["彩种"] == "福彩3D")
    assert any("数字型出号 999 注（期望 1000）" in x for x in row["问题"]), \
        f"真实不符必须仍被抓到，实际: {row['问题']}"
