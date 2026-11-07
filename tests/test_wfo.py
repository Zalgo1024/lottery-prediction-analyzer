# -*- coding: utf-8 -*-
"""WFO 窗口披露（`ev/wfo.py`）的契约锁。

外部参考程序在 35–50 期里从 N 套权重中挑一套，然后在同一段数据上报效果，
且从不报候选数与窗口。本文件锁住「必须把这几个数说出来」：
  · 没有候选选择 ⇒ 明说没有，别让人以为在挑参数；
  · 有候选且窗口短 ⇒ 直接判「选择噪声可能大于效应」；
  · 选择窗与评估窗重叠 ⇒ 读数是样本内的，不能当样本外表现。
"""
from ev.wfo import SELECTION_NOTE, disclose


def test_no_candidates_is_stated_explicitly():
    d = disclose(train_n=1200, eval_n=50, candidates=None)
    assert d["候选数"] is None
    assert "无候选选择" in d["判读"]


def test_many_candidates_short_window_is_flagged():
    d = disclose(train_n=35, candidates=120)
    assert "噪声" in d["判读"]


def test_overlap_dominates_everything():
    d = disclose(train_n=500, eval_n=100, candidates=3, overlap=True)
    assert "重叠" in d["判读"] and "样本内" in d["判读"]


def test_few_candidates_long_window_is_acceptable():
    d = disclose(train_n=600, eval_n=100, candidates=3, overlap=False)
    assert "可接受范围" in d["判读"]


def test_note_wording():
    assert "不参与判决" in SELECTION_NOTE


def test_proxy_note_declares_no_candidate_selection():
    """β 滚动是拟合不是挑选：披露里必须写清，否则会被读成「在挑参数」。"""
    from ev.popularity_proxy import proxy_note

    w = proxy_note()["walk_forward口径"]
    assert w["候选数"] is None
    assert w["窗口重叠"] is False
    assert "无候选选择" in w["判读"]
