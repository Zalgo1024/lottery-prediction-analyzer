# -*- coding: utf-8 -*-
"""tests/test_lowcrowd_batch.py —— 调度器「平行批次」（研究台账）的契约。

锁五件事：
  1. 默认关闭时**一次都不许调 `predict`**（不能因为加了这个功能就让生产多跑一遍）；
  2. 打开后恰好写 1 条研究域 pending，注数固定 200（**不跟看板滑轨**）；
  3. 第二次调用幂等跳过（调度器 / worker / 启动恢复三条路径都会来调）；
  4. 内部任何异常都被吞掉，且**生产 pending 原封不动**；
  5. 拥挤度代理不可用的彩种显式跳过，不静默出号。
"""
import pytest

import data.feedback as fb
from data.feedback import DOMAIN_PROD, DOMAIN_RESEARCH, _domain_of, save_pending
from prediction.engine import maybe_run_lowcrowd_parallel_batch

LOT = "双色球"
N = 200


@pytest.fixture
def tmp_fb(tmp_path, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(fb, "_pending_path", lambda name: tmp_path / f"{name}_pending.json")
    monkeypatch.setattr(fb, "_history_path", lambda name: tmp_path / f"{name}_history.json")
    monkeypatch.setattr(fb, "_weights_path", lambda name: tmp_path / f"{name}_weights.json")
    # 隔离「马后炮」防御（它有独立测试）：否则这里会随真实开奖日期漂移而随机失败。
    monkeypatch.setattr(fb, "load_lottery", lambda name: SimpleNamespace(records=[]))
    monkeypatch.setattr(fb, "_target_draw_date", lambda lot, issue: None)
    return tmp_path


def _tickets(strategy, n):
    """互不相同的票（重复票会被 compress_tickets 的「重复淘汰」压掉）。"""
    from itertools import combinations, islice

    return [{"红球": list(c), "蓝球": [1], "策略": strategy, "置信度": 0.3}
            for c in islice(combinations(range(1, 34), 6), n)]


def _set_params(monkeypatch, enabled=True, n=N):
    import ev.lowcrowd_select as ls

    monkeypatch.setattr(ls, "load_params",
                        lambda scenario=None: {"调度器平行批次": {"启用": enabled, "注数": n}})


def _set_supported(monkeypatch, ok=True):
    import ev.popularity_proxy as pp

    monkeypatch.setattr(pp, "go_or_none", lambda lot: ok)


def _stub_predict(monkeypatch, calls, *, n=N, reject=False, boom=False, record=True):
    """替身：真落 pending（模拟引擎的真实副作用），并记录入参。"""
    def _fake(lottery_name, groups=None, mode=None, record_pending=True, **kw):
        calls.append({"lottery": lottery_name, "groups": groups,
                      "mode": mode, "record_pending": record_pending})
        if boom:
            raise RuntimeError("模拟引擎崩溃")
        if reject:
            return {"拒绝": True, "原因": "代理不可用", "预测号码": []}
        res = {"目标期号": 26110, "预测日期": "2026-10-22", "预测模式": "lowcrowd",
               "策略": "低拥挤策略", "号码组数": n, "目标注数": n,
               "预测号码": _tickets("低拥挤策略", n)}
        if record and record_pending:
            fb.record_pending_prediction(lottery_name, res)
        return res

    monkeypatch.setattr("prediction.engine.predict", _fake)
    return calls


def test_disabled_by_default_calls_nothing(tmp_fb, monkeypatch):
    """★ 默认关闭时不许调 predict —— 加功能不能改变生产节奏。"""
    calls = _stub_predict(monkeypatch, [])
    _set_supported(monkeypatch, True)

    r = maybe_run_lowcrowd_parallel_batch(LOT, target_issue=26110)
    assert r["状态"] == "未启用"
    assert calls == []
    assert fb.load_pending(LOT) == []


def test_enabled_writes_one_research_pending(tmp_fb, monkeypatch):
    calls = _stub_predict(monkeypatch, [])
    _set_params(monkeypatch, enabled=True, n=N)
    _set_supported(monkeypatch, True)

    r = maybe_run_lowcrowd_parallel_batch(LOT, target_issue=26110)
    assert r["状态"] == "已生成" and r["注数"] == N
    assert len(calls) == 1 and calls[0]["mode"] == "lowcrowd"

    pend = fb.load_pending(LOT)
    assert len(pend) == 1
    assert _domain_of(pend[0]) == DOMAIN_RESEARCH
    assert len(pend[0]["预测号码"]) == N
    assert pend[0]["预测号码"][0]["策略"] == "低拥挤策略"


def test_second_call_is_idempotent(tmp_fb, monkeypatch):
    """三条触发路径都会来调，第二次必须跳过而不是再写一遍。"""
    calls = _stub_predict(monkeypatch, [])
    _set_params(monkeypatch, enabled=True, n=N)
    _set_supported(monkeypatch, True)

    assert maybe_run_lowcrowd_parallel_batch(LOT, target_issue=26110)["状态"] == "已生成"
    r2 = maybe_run_lowcrowd_parallel_batch(LOT, target_issue=26110)
    assert r2["状态"] == "已存在"
    assert len(calls) == 1, "第二次不该再调 predict"
    assert len(fb.load_pending(LOT)) == 1


def test_groups_is_fixed_200_not_the_slide(tmp_fb, monkeypatch):
    """★ 固定 200、不跟滑轨：跟滑轨会让 K 逐期漂移，台账序列失去可比性。"""
    import config

    monkeypatch.setattr(config, "resolve_groups", lambda *a, **k: 37)
    calls = _stub_predict(monkeypatch, [])
    _set_params(monkeypatch, enabled=True, n=N)
    _set_supported(monkeypatch, True)

    maybe_run_lowcrowd_parallel_batch(LOT, target_issue=26110)
    assert calls[0]["groups"] == N, f"应固定 200，实得 {calls[0]['groups']}"


def test_failure_is_swallowed_and_production_intact(tmp_fb, monkeypatch):
    """★ 研究侧崩溃不许外溢，也不许动生产 pending。"""
    save_pending(LOT, [{"目标期号": 26110, "档位": "一般", "策略": "高频策略",
                        "预测号码": _tickets("高频策略", 37),
                        "记录时间": "2026-10-22T01:00:00"}])
    before = [dict(p) for p in fb.load_pending(LOT)]

    _stub_predict(monkeypatch, [], boom=True)
    _set_params(monkeypatch, enabled=True, n=N)
    _set_supported(monkeypatch, True)

    r = maybe_run_lowcrowd_parallel_batch(LOT, target_issue=26110)   # 不许抛
    assert r["状态"] == "失败" and "模拟引擎崩溃" in r["原因"]

    after = fb.load_pending(LOT)
    assert len(after) == 1 and after[0]["策略"] == "高频策略"
    assert len(after[0]["预测号码"]) == 37
    assert after[0] == before[0]


def test_rejection_does_not_write(tmp_fb, monkeypatch):
    _stub_predict(monkeypatch, [], reject=True)
    _set_params(monkeypatch, enabled=True, n=N)
    _set_supported(monkeypatch, True)

    r = maybe_run_lowcrowd_parallel_batch(LOT, target_issue=26110)
    assert r["状态"] == "拒绝"
    assert fb.load_pending(LOT) == []


def test_unsupported_lottery_is_skipped(tmp_fb, monkeypatch):
    """大乐透等彩种显式跳过（拥挤度代理不可用），不许静默出号。"""
    calls = _stub_predict(monkeypatch, [])
    _set_params(monkeypatch, enabled=True, n=N)
    _set_supported(monkeypatch, False)

    r = maybe_run_lowcrowd_parallel_batch("大乐透", target_issue=26110)
    assert r["状态"] == "不支持彩种"
    assert calls == []
    assert fb.load_pending("大乐透") == []


def test_research_batch_does_not_touch_production_slot(tmp_fb, monkeypatch):
    """★ 端到端：同期已有生产批时，研究批只能新增，不能替换。"""
    save_pending(LOT, [{"目标期号": 26110, "档位": "一般", "策略": "高频策略",
                        "预测号码": _tickets("高频策略", 37),
                        "记录时间": "2026-10-22T01:00:00"}])
    _stub_predict(monkeypatch, [])
    _set_params(monkeypatch, enabled=True, n=N)
    _set_supported(monkeypatch, True)

    maybe_run_lowcrowd_parallel_batch(LOT, target_issue=26110)
    pend = fb.load_pending(LOT)
    assert len(pend) == 2
    dom = {_domain_of(p) for p in pend}
    assert dom == {DOMAIN_PROD, DOMAIN_RESEARCH}
