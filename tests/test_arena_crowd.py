# -*- coding: utf-8 -*-
"""tests/test_arena_crowd.py —— 拥挤度闸门必须把「暂不评估」说清楚卡在哪一项。

★ 这条测试防的是一个很容易犯的错：把「没有输入」写成「0 期 / 0 达标率」。
  那样报表上看起来像是「跑了但很惨」，而真实情况是「根本没跑」。
"""
from __future__ import annotations

import pytest

from ev import popularity_proxy as PP
from ev.arena import crowd as CR


@pytest.mark.statsmodels
def test_supported_lottery_passes():
    g = CR.crowd_gate("双色球", n_periods=PP.MIN_TRAIN)
    assert g["可用"] is True
    assert g["状态"] == CR.STATE_OK
    assert g["卡在"] is None


@pytest.mark.statsmodels
def test_modeled_but_nogo_blocks_at_go_gate():
    """大乐透**建过模**但没过门槛 ⇒ 必须卡在「Go门槛」，不是「支持范围」。

    ★ 这两档的对外含义完全不同：
      · 卡在「支持范围」= 我们没做过，无证据可查；
      · 卡在「Go门槛」  = 做过（2,932 期）、有完整证据、结论是没通过。
    报成前者会把「做了没通过」伪装成「还没来得及做」。
    """
    g = CR.crowd_gate("大乐透", n_periods=10_000)
    assert g["可用"] is False
    assert g["状态"] == CR.STATE_SKIP
    assert g["卡在"] == "Go门槛"
    assert "建过模但未达 Go 门槛" in g["原因"]
    assert "这不是「没做过」，是「做了没通过」" in g["原因"]
    # 数字必须现取（模型报告里的实测值），不是写死的
    assert "期" in g["原因"] and "CV 仅" in g["原因"]
    assert g["探测"]["支持范围"]["通过"] is True


def test_never_modeled_lottery_blocks_at_support_scope():
    """从未建模的彩种（七星彩）才卡在「支持范围」—— 与大乐透必须分开。"""
    g = CR.crowd_gate("七星彩", n_periods=10_000)
    assert g["可用"] is False
    assert g["卡在"] == "支持范围"
    assert "从未建模" in g["原因"]


@pytest.mark.statsmodels
def test_insufficient_train_periods_blocks():
    """支持且有 Go 门槛，但期数不够 walk-forward 最小训练量 ⇒ 也不可用。"""
    g = CR.crowd_gate("双色球", n_periods=PP.MIN_TRAIN - 1)
    assert g["可用"] is False
    assert g["卡在"] == "训练样本量"


def test_skip_note_never_reads_as_invalid():
    assert "不是 0 期" in CR.SKIP_NOTE
    assert "无效" in CR.SKIP_NOTE          # 显式否掉「无效」这个读法


def test_require_usable_raises_with_reason():
    with pytest.raises(NotImplementedError, match="暂不评估"):
        CR.require_usable("大乐透", n_periods=3000)


@pytest.mark.statsmodels
def test_require_usable_returns_probe_when_ok():
    g = CR.require_usable("双色球", n_periods=5000)
    assert g["探测"]["支持范围"]["通过"] is True
