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

    def fake_compress(tickets, lottery):
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
    pend = [{"状态": "pending", "目标期号": 2026999,
             "目标注数": 35, "预测号码": [_digital_ticket(n=i) for i in range(35)]}]
    (fb_dir / "福彩3D_pending.json").write_text(
        json.dumps(pend, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(hc, "resolve_groups", lambda name: 37)  # 实时配置≠生成时
    monkeypatch.setattr("data.loader.load_lottery", lambda name: type(
        "LD", (), {"records": [type("R", (), {"期号": 2026998})]})())
    rows = hc.check_pending_freshness()
    row = next(r for r in rows if r["彩种"] == "福彩3D")
    assert row["问题"] == [], f"生成时刻快照应放行，实际: {row['问题']}"
