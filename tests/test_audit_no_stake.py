# -*- coding: utf-8 -*-
"""审计模块的物理隔离（2026-10-31 任务 3 的牙齿）。

两条硬约束，用**源码扫描**而不是「靠自觉」来保证：

1. **异常不得触发加码**。`data/audit/` 里不允许出现 `apply_dynamic_result` /
   `record_spend` / `clamp` / `resolve_groups`，`checks.py` 不允许 import
   `prediction` 或 `ev.budget_gate`。
   理由：一个能改注数的异常监测，就是「看见不利结果就加码」的反馈回路 ——
   所有赌徒破产路径的共同形状。

2. **不得在开奖后改阈值**。阈值锁定后 `save()` 必须抛错；阈值哈希要可复算，
   这样「阈值被改过」本身成为可查的事实。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from data.audit import checks as CK
from data.audit import thresholds as TH

AUDIT_DIR = Path(CK.__file__).resolve().parent
# ★ 只查**真实调用**，不查字符串出现：这些名字在 docstring 里被提及是**合法的**
#   （注释正是在解释「为什么禁止」）。用文本匹配会把解释当违规、逼人删注释，
#   而删掉解释之后下一个人就真的会加进代码里 —— 那是把守卫做成了帮倒忙。
FORBIDDEN_CALLS = ("apply_dynamic_result", "record_spend", "record_predict",
                   "clamp", "resolve_groups", "predict")


def _sources():
    return sorted(AUDIT_DIR.glob("*.py"))


def _called_names(tree) -> set:
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def test_audit_package_has_sources():
    assert len(_sources()) >= 3, "审计包文件缺失，扫描会变成空跑（假绿）"


@pytest.mark.parametrize("path", _sources(), ids=lambda p: p.name)
def test_no_stake_side_effects_in_source(path):
    """★★ 源码里不得**调用**任何能改变注数/记账的函数。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hit = set(_called_names(tree)) & set(FORBIDDEN_CALLS)
    assert not hit, (
        f"{path.name} 调用了 {sorted(hit)} ⇒ 审计模块可能影响出号。"
        "「发现异常就能加码」是必须从结构上排除的路径。"
    )


def test_checks_does_not_import_prediction_or_budget_gate():
    """★★ 用 AST 查真实 import，而不是字符串匹配（注释里提到不算违规）。"""
    tree = ast.parse(Path(CK.__file__).read_text(encoding="utf-8"))
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module)
    for banned in ("prediction", "ev.budget_gate", "prediction.engine"):
        assert banned not in mods, f"checks.py 不应 import {banned}"


def test_thresholds_are_locked_and_refuse_writes(tmp_path):
    """★★ 锁定后写阈值必须抛错 —— 开奖后改阈值等于给自己开解释权。"""
    p = tmp_path / "audit_thresholds.json"
    TH.save({"版本": "t1", "锁定": True, "阈值": {"缺失期段上限": 1}}, path=p, unlock=True)
    with pytest.raises(TH.ThresholdLocked):
        TH.save({"版本": "t2", "阈值": {"缺失期段上限": 99}}, path=p)


def test_threshold_hash_is_stable_and_sensitive(tmp_path):
    a = TH.content_hash({"阈值": {"x": 1}})
    b = TH.content_hash({"阈值": {"x": 1}})
    c = TH.content_hash({"阈值": {"x": 2}})
    assert a == b and a != c
    assert len(a) == 16


def test_missing_threshold_file_falls_back_without_crash(tmp_path):
    doc = TH.load(path=tmp_path / "nope.json")
    assert doc["版本"] and doc["阈值"]["金额未知占比上限"] > 0


def test_run_reports_discipline_and_no_stake_fields():
    """结论必须带纪律句，且返回体里没有任何「注数/额度」字段。"""
    res = CK.run(["排列5"])
    assert res["结论"].startswith(("未检出", "检出"))
    assert "无法识别地区偏向" in res["纪律"]
    for k in res:
        assert not re.search(r"注数|额度|加注|倍数", k), f"审计结果不该含出号相关字段：{k}"


def test_lookahead_uses_batch_time_not_prediction_date():
    """★ 前视判据只认 `批次时间`。`预测日期` 是开奖日的派生字段，用它判永远判不出。"""
    recs = [{"期号": "1", "批次时间": "2026-10-29T14:00:00", "开奖日期": "2026-10-29",
             "预测日期": "2026-10-29"}]
    assert CK.check_lookahead("排列5", records=recs) == []

    recs[0]["批次时间"] = "2026-10-30T14:00:00"     # 出号晚于开奖日
    out = CK.check_lookahead("排列5", records=recs)
    assert out and out[0]["项"] == "时间戳错位"

def test_data_files_missing_is_reported(tmp_path, monkeypatch):
    """★ 12 个 CSV 被删空那次，没有任何检查项会响。

    这条把它补上：文件不在 / 是空文件 / 只有表头，都要变成一条「待查」。
    判据落在**文件状态**上，不猜原因（那次确实没查出是谁删的）。
    """
    import data.loader as LD

    monkeypatch.setattr(LD, "_file_path", lambda lot: tmp_path / f"{lot}历史数据.csv")
    out = CK.check_data_files()
    assert len(out) == len(CK.LOTS), "六个彩种的文件缺失都要报"
    assert all(f["级别"] == "待查" and "不存在" in f["描述"] for f in out)

    # 只有表头 = 空数据，同样要报
    for lot in CK.LOTS:
        (tmp_path / f"{lot}历史数据.csv").write_text("期号,日期\n", encoding="utf-8")
    out = CK.check_data_files()
    assert out and "只有表头" in out[0]["描述"]

    # 有数据行就不再报
    for lot in CK.LOTS:
        (tmp_path / f"{lot}历史数据.csv").write_text("期号,日期\n1,2026-01-01\n",
                                                     encoding="utf-8")
    assert CK.check_data_files() == []
