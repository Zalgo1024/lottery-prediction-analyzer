# -*- coding: utf-8 -*-
"""P1/P2 的**防退化**测试。

这些用例不测任何统计结论，只测「包的结构不会悄悄烂掉」——
它们的存在理由是本轮加固时发现的真实退化：P2 建的三个模块没进 `__all__`、
硬纪律① 指向一个当时并不存在的模块、`measure_all()` 腐化后没人发现。

⚠️ 断言方式刻意用**行为**（`importlib` 真导入、docstring 里的模块名真能
`find_spec`）而不是「某行文本等于某串」—— 后者只会锁住改法，锁不住事实。
"""
import importlib
import importlib.util
import re

import pytest

import ev.mechanism as M


# ------------------------------------------------------ ① 包出口不许漏模块

def test_mechanism_package_all_exports_p2_modules():
    """`__all__` 必须覆盖 P2 建的三个模块，且每个真能导入。

    历史问题：P1 的 5 个模块进了 `__all__`，P2 新建的
    `gap_c_structural` / `gap_d_special_period` / `sales_grade` 全漏了。
    """
    for name in ("gap_c_structural", "gap_d_special_period", "sales_grade"):
        assert name in M.__all__, f"{name} 不在 __all__ 里"
    for name in M.__all__:
        mod = importlib.import_module(f"ev.mechanism.{name}")
        assert mod is not None


# 明确标注为「计划中、尚未建立」的模块：允许被 docstring 提及。
# 列在这里必须同时满足「docstring 里写了它尚未建立」，否则本测试失去意义。
#
# ★ `gap_select` 已从本表移除：P4 已建立并接线 ev.selection / ev.sampler。
#   留在这里会反过来逼 docstring 对一个**已存在**的模块写「尚未建立」——
#   守卫过期后自己就变成了误导源（2026-10-04 实测踩到：改对注释反而被它判红）。
#   ⇒ 模块一旦落地，务必把它从 KNOWN_FUTURE 挪走（现在它是普通模块，
#     走上面那条 `find_spec` 必须为真的检查）。
KNOWN_FUTURE = set()


def test_package_docstring_references_only_real_modules():
    """docstring 里凡是被说成**已存在**的模块，都必须真的能 `find_spec`。

    历史问题：硬纪律① 写「见 gap_select」，而 `gap_select` 属 P4、当时并不存在
    ⇒ 纪律的锚点是空指针，读者按它去找会扑空。

    修法不是删掉这个名字（读者需要知道将来会有），而是把它明确标成
    「计划中、尚未建立」，并把纪律的**实现点**指向真实存在的
    `ev.gap.mechanism_gamma` —— 后者由下面那条用例锁住。
    """
    doc = M.__doc__ or ""
    refs = set(re.findall(r"见\s*`?([a-z_][a-z0-9_]*)`?", doc))
    refs |= set(re.findall(r"`(gap_[a-z_]+|sales_grade)`", doc))
    assert refs, "docstring 里一个模块引用都没抽到，正则坏了"
    for name in sorted(refs - KNOWN_FUTURE):
        assert importlib.util.find_spec(f"ev.mechanism.{name}") is not None, (
            f"docstring 引用了不存在的模块 {name}")
    # 提到将来的模块时必须同时说清「尚未建立」，不许让人以为它已经在那儿
    for name in sorted(refs & KNOWN_FUTURE):
        assert "尚未建立" in doc, (
            f"docstring 提到 {name} 却没写「尚未建立」——读者会以为它已存在")


def test_hard_discipline_anchors_on_a_real_implementation():
    """纪律①「γ=0 时选号与今天逐位一致」必须锚在**真实存在**的函数上。

    纪律写在 docstring 里很容易，锚点空指针（指向尚不存在的模块）等于没写。
    """
    from ev import gap
    doc = M.__doc__ or ""
    assert "mechanism_gamma" in doc, "纪律① 必须指向 ev.gap.mechanism_gamma"
    assert callable(getattr(gap, "mechanism_gamma", None)), \
        "纪律① 的实现点 ev.gap.mechanism_gamma 不存在"


# ------------------------------------------------------ ② 门面不许腐化

def test_measure_all_is_wired_to_global():
    """`measure_all` 必须在 `__all__` 里（对外 API）**且真的转发**到
    `measure_gap_global`——它当前没有内部调用者，纯靠 docstring 承诺，
    腐化不会被任何别的测试发现。
    """
    from ev import gap
    assert "measure_all" in gap.__all__

    called = {}

    def fake_global(lots, **kw):
        called["lots"] = list(lots)
        called["kw"] = sorted(kw)
        return {"逐彩种": [], "检出彩种": None, "入BH彩种数": 0}

    real = gap.measure_gap_global
    try:
        gap.measure_gap_global = fake_global
        gap.measure_all(["双色球", "大乐透"])
    finally:
        gap.measure_gap_global = real
    assert called["lots"] == ["双色球", "大乐透"]


# ------------------------------------------------------ ③ 判定路径不许碰解析口径

def test_no_analytic_se_reaches_any_judgement_path():
    """**行为**断言：判定用的 z 必须由长块 se 算出，而不是解析 se。

    解析 se=1/√(12n) 假定 u 序列 i.i.d.，只能作参照。本轮把 B 轨的 z 门从
    解析 se 改成 bootstrap se，这条用「手算 z 对上哪一个 se」来锁 ——
    比断言某段说明文字里有没有「解析」两个字可靠得多。
    """
    from ev.mechanism.gap_b_drawn_quantile import quantile_test
    from ev.mechanism.gap_a_ticket_ratio import ticket_ratio_test
    from tests.test_gap_b import _mk_wf
    from tests.test_gap_a import _frame

    # ⚠️ 必须用**偏离 0.5 较大**的数据：mean_u≈0.5 时分子趋零，解析 se 与长块 se
    # 算出的 z 几乎一样（真实数据上就是 0.249 vs 0.225），根本区分不出来。
    qt = quantile_test(_mk_wf(0.45), B=200)
    mean_u, se_long, se_ana = qt["mean_u"], qt["se_长块bootstrap"], qt["se_解析"]
    z_from_long = (mean_u - 0.5) / se_long
    z_from_ana = (mean_u - 0.5) / se_ana
    assert qt["z"] == pytest.approx(z_from_long, abs=2e-3), "z 不是长块 se 算的"
    assert abs(qt["z"] - z_from_ana) > 0.5, "z 与解析 se 算的几乎一样，测试无力"
    # 参照口径仍在（读者要能看到它），但带「不参与判定」的标注
    assert "解析" not in (qt["z_零分布"] or "")
    assert "不参与判定" in (qt["解析se_说明"] or "")
    assert "长块" in (qt["判定口径"] or "")

    at = ticket_ratio_test("双色球", frame=_frame(n=300, m1=0.85), B=200)
    assert at["判定"]["z_用"] == "长块 bootstrap se"
    assert at["判定"]["z"] == pytest.approx(
        (at["判定"]["R"] - 1.0) / at["主口径"]["se_长块bootstrap"], abs=2e-3)
    for k in at["判定"]:
        assert "解析" not in k, k


def test_real_gap_pipeline_does_not_fall_back_to_short_se():
    """真实管线跑一遍，γ 的 se 口径必须是长块（回退分支在生产路径不可达）。"""
    from ev.mechanism.gap_b_drawn_quantile import (
        fit_gamma, quantile_test, walk_forward_quantiles,
    )
    from ev.mechanism.gap_a_ticket_ratio import ticket_ratio_test
    from ev.mechanism import gap_verdict as GV

    wf = walk_forward_quantiles("双色球", min_train=200, step=50)
    if not wf.get("applicable"):
        pytest.skip("样本不足")
    qt = quantile_test(wf, B=200)
    a = ticket_ratio_test("双色球")
    if not a.get("applicable"):
        pytest.skip("A 轨不可用")
    g = fit_gamma("双色球", wf=wf, qtest=qt, a_result=a)
    if g.get("scale") is None:
        pytest.skip("模型未 Go，γ 不可折算")
    assert g["se_口径"] == {"gamma_B": "长块bootstrap",
                            "gamma_A": "长块bootstrap"}, g["se_口径"]
