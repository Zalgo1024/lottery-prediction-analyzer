"""
数据写入保护 + 分区名归一化 回归测试（2026-09-11 新增）。

背景（两起真实数据事故）：
1. 分区名迁移静默清零命中：数字型早期用「万位/千位/百位…」作分区名，schema 改为
   「第N位」后，`_hit_detail` 用 `zones.get("第N位")` 取到空列表 → 命中被算成 0。
   实锤：排列5 期26215 #27/#29/#30、排列3 期26215 #28 共 4 条应中 1 位却记 0。
2. 测试写生产数据：`tests/test_robust_tiers.py` 直接 `predict("排列5", groups=3, ...)`，
   `record_pending_prediction` 用 (目标期号, 档位) 去重 → 把生产 26244 期 pending
   从 ~100 组覆盖成 3 组。

本文件锁住两个契约：
  A. `normalize_zone_names` 按「从右往左数位」正确映射旧位名。
  B. `_hit_detail` 对旧位名预测仍能算出真实命中（不再静默归零）。
  C. pytest 进程内禁止写生产 feedback 目录；隔离到 tmp 的写入必须放行。
"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import data.feedback as fb
from data.feedback import _hit_detail
from data.schema import get_schema, normalize_zone_names, _legacy_digit_aliases


# ------------------------------------------------------------
# A. 分区名归一化映射
# ------------------------------------------------------------

def test_alias_5digit_pl5():
    """排列5（5 位）：万位→第1位 … 个位→第5位。"""
    m = _legacy_digit_aliases(5)
    assert m["万位"] == "第1位"
    assert m["千位"] == "第2位"
    assert m["百位"] == "第3位"
    assert m["十位"] == "第4位"
    assert m["个位"] == "第5位"


def test_alias_3digit_3d():
    """福彩3D/排列3（3 位）：百位→第1位、十位→第2位、个位→第3位。"""
    m = _legacy_digit_aliases(3)
    assert m["百位"] == "第1位"
    assert m["十位"] == "第2位"
    assert m["个位"] == "第3位"
    # 越界的「万位/千位」不应出现
    assert "万位" not in m and "千位" not in m


def test_alias_7digit_qxc_chinese_ordinal():
    """七星彩早期「第一位..第七位」写法。"""
    m = _legacy_digit_aliases(7)
    for k, zh in enumerate("一二三四五六七", start=1):
        assert m[f"{zh}位"] == f"第{k}位"


def test_normalize_keeps_current_names():
    """已是现行名（红球/蓝球/第N位）的原样保留。"""
    s = get_schema("双色球")
    assert normalize_zone_names({"红球": [1], "蓝球": [2]}, s) == {"红球": [1], "蓝球": [2]}
    s5 = get_schema("排列5")
    assert normalize_zone_names({"第1位": [1], "第5位": [9]}, s5) == {"第1位": [1], "第5位": [9]}


# ------------------------------------------------------------
# B. _hit_detail 不再静默归零
# ------------------------------------------------------------

class _Actual:
    def __init__(self, zones):
        self.zone_numbers = zones


def test_hit_detail_legacy_zone_names_not_zeroed():
    """旧位名预测（万位/千位/…）必须能算出真实命中，而不是全 0。"""
    schema = get_schema("排列5")
    # 实际开奖 9 2 7 3 8；旧格式预测 万位9 千位3 百位0 十位2 个位4 → 第1位命中
    pred_ps = {"号码": {"万位": [9], "千位": [3], "百位": [0], "十位": [2], "个位": [4]}}
    actual = _Actual({"第1位": [9], "第2位": [2], "第3位": [7], "第4位": [3], "第5位": [8]})
    zone_hits, total_hits, total_choose, is_rb = _hit_detail("排列5", pred_ps, actual, schema)
    assert is_rb is False
    assert total_choose == 5
    assert total_hits == 1
    assert zone_hits["第1位"] == 1


def test_hit_detail_legacy_3digit():
    """3 位彩种旧名（百位/十位/个位 → 第1/2/3位）。"""
    schema = get_schema("排列3")
    pred_ps = {"号码": {"百位": [9], "十位": [2], "个位": [7]}}
    actual = _Actual({"第1位": [9], "第2位": [2], "第3位": [7]})
    _zh, total_hits, _tc, _rb = _hit_detail("排列3", pred_ps, actual, schema)
    assert total_hits == 3


# ------------------------------------------------------------
# C. 生产写入保护
# ------------------------------------------------------------

def test_prod_write_blocked_under_pytest():
    """pytest 下写生产 feedback 路径 → 被拦下（文件不应被创建）。"""
    prod_file = fb.FEEDBACK_DIR / "_guard_probe_should_never_exist.json"
    assert not prod_file.exists(), "探针文件本不该存在，请先清理"
    fb._safe_write_json(prod_file, {"probe": 1})
    assert not prod_file.exists(), "pytest 进程不应写入生产 feedback 目录"


def test_tmp_write_allowed_under_pytest(tmp_path):
    """隔离到 tmp 目录的写入必须放行（测试夹具依赖）。"""
    tmp_file = tmp_path / "ok.json"
    fb._safe_write_json(tmp_file, {"ok": 1})
    assert json.loads(tmp_file.read_text(encoding="utf-8")) == {"ok": 1}


def test_prod_guard_detection():
    """路径判定：生产目录内为 True，tmp 为 False。"""
    assert fb._is_prod_feedback_path(fb.FEEDBACK_DIR / "排列5_pending.json")
    assert not fb._is_prod_feedback_path(Path("/tmp/whatever.json"))


# ------------------------------------------------------------
# D. 非法票不得判奖级
# ------------------------------------------------------------

def test_illegal_ticket_gets_no_prize():
    """红/蓝球个数不符 schema 的预测 = 不是合法彩票 → 修复时奖级必须归「未中」。

    实证：双色球 26090 预测蓝球 [1,2]（应为 1 个），红球恰全中 → 曾判「二等」，
    假高奖级且会出现在不过滤 invalid 的战绩查询里。
    """
    from config import LOTTERY_CONFIG
    from data.schema import schema_from_cfg
    from scripts.repair_records import diff_record

    class _Draw:
        zone_numbers = {"红球": [2, 4, 15, 23, 25, 27], "蓝球": [3]}

    rec = {
        "期号": 26090,
        "预测红球": [2, 4, 15, 23, 25, 27],
        "预测蓝球": [1, 2],          # 非法：双色球蓝球只能 1 个
        "红球命中": 6, "蓝球命中": 0, "总命中": 6,
        "中奖等级": "二等",
        "valid_prediction": False,
    }
    schema = schema_from_cfg(LOTTERY_CONFIG["双色球"], "双色球")
    patch = diff_record("双色球", rec, _Draw(), schema)
    assert patch.get("中奖等级") == "未中"
    assert "备注" in patch
