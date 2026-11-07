# -*- coding: utf-8 -*-
"""数据来源与新鲜度三态（`web/utils._freshness_state`）的契约锁。

为什么单独锁
------------
`/api/data-status` 读的是**本地文件**，不是实时接口。若界面只显示「滞后 0 天」，
读者会把它读成「刚从官网取过」。三态必须分开，而且**「读不到」不能是「实时」**：
文件缺失或解析失败时 `data_lag_days` 会缺省成 0，直接判实时就是把故障画成正常。
"""
from web.utils import _freshness_state, get_data_status


def test_three_states():
    assert _freshness_state(True, None, 0) == "实时"
    assert _freshness_state(True, None, 3) == "缓存"
    assert _freshness_state(False, None, 0) == "未知"


def test_error_is_never_realtime():
    """解析失败 + lag 缺省 0 ⇒ 必须「未知」，不许落成「实时」。"""
    assert _freshness_state(True, "boom", 0) == "未知"
    assert _freshness_state(True, RuntimeError("x"), 0) == "未知"


def test_bad_lag_value_degrades_to_unknown():
    assert _freshness_state(True, None, "N/A") == "未知"
    assert _freshness_state(True, None, None) == "实时"      # None = 缺省 0，视为已更新


def test_status_payload_has_source_and_freshness():
    d = get_data_status("双色球")
    assert d["来源"] == "本地文件（非实时接口）"
    assert d["新鲜度"] in ("实时", "缓存", "未知")
    assert "csv_modified" in d
