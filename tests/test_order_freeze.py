# -*- coding: utf-8 -*-
"""tests/test_order_freeze.py —— 「排序冻结」的牙齿测试。

核心性质只有一条：**结算时拿到的是开奖前冻结的那份票序**。
故意破坏每一道锁（票序哈希 / 策略哈希 / 批次时间 / 版本 / 覆盖语义），
确认每一条都真的会抛，而不是「看起来有校验」。
"""
from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest

from ev import order_freeze as OF

ROOT = Path(__file__).resolve().parents[1]


def _tks(k: int = 8):
    return [{"序号": i, "号": [i, i + 1]} for i in range(k)]


def _snap(tmp_path=None, k: int = 8, **kw):
    params = dict(
        lottery="双色球", issue="26127", tickets=_tks(k),
        strategy_version="tier_coverage/2026-11-05-A", seed=0, K=k,
        rule_version="2026", code_version="2026-11-05-A",
        batch_time="离线回放（未记录生产批次时间）",
        target={"kind": "grade", "grade": "四等"},
    )
    params.update(kw)
    return OF.build_snapshot(**params)


def test_snapshot_roundtrip_and_prefix_is_pure_slice(tmp_path):
    """前 3 注必须逐位等于冻结票序的前 3 注，且是深拷贝。"""
    snap = _snap()
    p = OF.save_snapshot(snap, directory=tmp_path)
    loaded = OF.load_snapshot(p)
    assert loaded["票序"] == snap["票序"]
    pref = OF.prefix_of(loaded, 3)
    assert pref == snap["票序"][:3]
    assert len(pref) == 3
    pref[0]["序号"] = 999
    assert loaded["票序"][0]["序号"] == 0, "改前缀不该反过来改冻结快照"


def test_tampering_ticket_order_is_rejected(tmp_path):
    snap = _snap()
    snap["票序"] = list(reversed(snap["票序"]))
    with pytest.raises(OF.OrderFreezeError, match="票序哈希"):
        OF.verify_snapshot(snap)
    with pytest.raises(OF.OrderFreezeError):
        OF.save_snapshot(snap, directory=tmp_path)


def test_tampering_strategy_inputs_is_rejected():
    snap = _snap()
    snap["随机种子"] = 7
    with pytest.raises(OF.OrderFreezeError, match="策略哈希"):
        OF.verify_snapshot(snap)


def test_missing_batch_time_is_rejected():
    with pytest.raises(OF.OrderFreezeError, match="批次时间"):
        _snap(batch_time="")


def test_count_mismatch_is_rejected():
    with pytest.raises(OF.OrderFreezeError, match="K"):
        _snap(k=8, K=9)


def test_same_key_different_tickets_refuses_overwrite(tmp_path):
    """★ 排名不许事后改：同键不同票序必须拒绝覆盖；同内容重跑幂等。"""
    a = _snap()
    p1 = OF.save_snapshot(a, directory=tmp_path)
    assert OF.save_snapshot(a, directory=tmp_path) == p1
    b = _snap()
    b["票序"] = list(reversed(b["票序"]))
    b["票序哈希"] = OF.digest(b["票序"])
    with pytest.raises(OF.OrderFreezeError, match="拒绝覆盖"):
        OF.save_snapshot(b, directory=tmp_path)
    assert json.loads(p1.read_text(encoding="utf-8"))["票序"] == a["票序"]


def test_version_mismatch_is_rejected(tmp_path):
    snap = _snap()
    p = OF.save_snapshot(snap, directory=tmp_path)
    with pytest.raises(OF.OrderFreezeError, match="版本"):
        OF.load_snapshot(p, expected_version="another-version")


def test_prefix_out_of_range():
    snap = _snap()
    for n in (0, -1, snap["K"] + 1):
        with pytest.raises(OF.OrderFreezeError, match="越界"):
            OF.prefix_of(snap, n)


def test_prefix_of_never_calls_a_selector():
    """纯切片：函数体不许出现选号 / 排序调用。"""
    src = inspect.getsource(OF.prefix_of)
    for bad in ("select_", "sort(", "sorted(", "shuffle", "permutation"):
        assert bad not in src, f"prefix_of 里出现了 {bad} ⇒ 排序会被重跑"


def test_module_does_not_read_system_clock():
    """离线回放时间必须由调用方给，模块不许自己取系统时间。"""
    src = Path(OF.__file__).read_text(encoding="utf-8")
    for bad in ("datetime.now", "time.time", "date.today"):
        assert bad not in src, f"冻结模块读了系统时间：{bad}"


def test_snapshot_name_carries_strategy_hash():
    snap = _snap()
    name = OF.snapshot_name(snap)
    assert snap["策略哈希"][:12] in name
    assert OF.ORDER_FREEZE_VERSION in name


def test_conftest_isolates_offline_stores():
    """新增落盘模块必须进 autouse 隔离清单，否则单测会污染真实 logs/。"""
    text = (ROOT / "conftest.py").read_text(encoding="utf-8")
    assert '("ev.order_freeze", "SNAPSHOT_DIR"' in text
    assert '("ev.prefix_curve", "SHADOW_PATH"' in text
