# -*- coding: utf-8 -*-
"""tests/test_shadow_settle.py —— 影子结算编排。

只测**边界与拒绝路径**（快照缺失 / 换票 / 未开奖 / 幂等），不伪造判级读数：
真实判级要真实开奖数据，那属于生产运行期，不该用合成数据假装跑通。
"""
from __future__ import annotations

import ast

import pytest

from ev import order_freeze as OF
from ev import prefix_shadow as PS
from ev import shadow_settle as SS

BATCH = "2026-11-08T12:00:00"
SETTLE = "2026-11-08T22:00:00"


def _tickets(k=3):
    return [{"号码": {"红球": [i + 1, 2, 3, 4, 5, 6], "蓝球": [7]},
             "策略": f"s{i}", "置信度": round(1.0 - i * 0.01, 4)} for i in range(k)]


def _snap_ok():
    return OF.build_snapshot(
        lottery="双色球", issue="26128", tickets=_tickets(), K=3,
        strategy_version="test", seed=1, rule_version="ssq-2003",
        code_version="test", batch_time=BATCH,
        time_nature=PS.PRODUCTION_NATURE, experiment={"重出批号": 0})


def _reg(snap, **over):
    r = {"阶段": PS.KIND_REGISTER, "彩种": "双色球", "期号": snap["期号"],
         "冻结版本": snap["版本"], "策略哈希": snap["策略哈希"],
         "票序哈希": snap["票序哈希"], "n": 3,
         "开奖时间": "2026-11-08T21:15:00"}
    r.update(over)
    return r


def test_snapshot_path_matches_order_freeze_naming(tmp_path):
    """★ 单一真源：本模块的文件名规则必须与 `order_freeze.snapshot_name` 完全一致。"""
    snap = _snap_ok()
    reg = {"期号": snap["期号"], "冻结版本": snap["版本"], "策略哈希": snap["策略哈希"]}
    assert SS.snapshot_path(reg, snapshot_dir=tmp_path).name == OF.snapshot_name(snap)


def test_rejects_non_register_row():
    got = SS.settle_from_register({"阶段": "结算"}, settle_time=SETTLE)
    assert got["状态"] == SS.STATUS_FAIL and "登记行" in got["原因"]


def test_rejects_missing_fields():
    got = SS.settle_from_register({"阶段": PS.KIND_REGISTER}, settle_time=SETTLE)
    assert got["状态"] == SS.STATUS_FAIL


def test_missing_snapshot_is_failure_not_skip(tmp_path):
    snap = _snap_ok()
    got = SS.settle_from_register(_reg(snap), settle_time=SETTLE,
                                  snapshot_dir=tmp_path / "nope",
                                  shadow_path=tmp_path / "s.jsonl")
    assert got["状态"] == SS.STATUS_FAIL
    assert "快照不存在" in got["原因"]


def test_ticket_hash_mismatch_is_rejected(tmp_path):
    """换票必须被拒：快照与登记行的票序哈希不一致。"""
    snap = _snap_ok()
    OF.save_snapshot(snap, directory=tmp_path)
    got = SS.settle_from_register(_reg(snap, 票序哈希="f" * 40), settle_time=SETTLE,
                                  snapshot_dir=tmp_path,
                                  shadow_path=tmp_path / "s.jsonl")
    assert got["状态"] == SS.STATUS_FAIL
    assert "不许换票" in got["原因"]


def test_no_draw_record_is_skip_not_pass(tmp_path):
    """未开奖 ⇒ 跳过，且不得写成「通过」或 0 。"""
    snap = _snap_ok()
    OF.save_snapshot(snap, directory=tmp_path)
    got = SS.settle_from_register(_reg(snap), settle_time=SETTLE,
                                  snapshot_dir=tmp_path,
                                  shadow_path=tmp_path / "s.jsonl",
                                  records=[])
    assert got["状态"] == SS.STATUS_SKIP
    assert "不写成通过" in got["原因"]
    assert not (tmp_path / "s.jsonl").exists()


def test_never_raises_on_broken_snapshot(tmp_path):
    """坏快照文件 ⇒ 返回失败，不抛。"""
    snap = _snap_ok()
    p = tmp_path / OF.snapshot_name(snap)
    p.write_text("{broken", encoding="utf-8")
    got = SS.settle_from_register(_reg(snap), settle_time=SETTLE,
                                  snapshot_dir=tmp_path,
                                  shadow_path=tmp_path / "s.jsonl")
    assert got["状态"] == SS.STATUS_FAIL


def test_module_does_not_read_system_time():
    src = (SS.SETTLE_VERSION and
           __import__("pathlib").Path(SS.__file__).read_text(encoding="utf-8"))
    tree = ast.parse(src)
    hits = [n.func.attr for n in ast.walk(tree)
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
            and n.func.attr in ("now", "today", "utcnow")]
    assert hits == [], f"结算编排读了系统时间：{hits}（settle_time 必须由调用方注入）"
