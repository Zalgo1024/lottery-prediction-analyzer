# -*- coding: utf-8 -*-
"""四轨裁决层测试。

要点：
  ① 判定轨只有 A/A′/B；**C/D 绝不参与判定**（把 C 灌成"实质偏离"也不改 status）
  ② 乐透型 = A+B；数字型 = A′（只有一轨 ⇒ 方向一致性无定义，不得因此判"不一致"）
  ③ 排列3/福彩3D 无判定轨 ⇒ 必须判 `no-track`，**不许**说"无缝隙"（那是不同的断言）
  ④ BH 只在"有判定轨"的彩种之间做（否则虚增 m，校正过松）
  ⑤ 缓存协议版本已 bump（v1 缓存不得被复用）
"""
from __future__ import annotations

import pytest

from ev.mechanism import gap_verdict as GV
from ev.mechanism import gap_cache


# ------------------------------------------------------------------ 合成轨道
def _A(gap=False, R=1.0, se=0.02, note="x"):
    return {"lottery": "双色球", "applicable": True, "n": 1000,
            "主口径": {"R": R, "se_块bootstrap": se, "se_长块bootstrap": se,
                       "ci95": [R - 2 * se, R + 2 * se], "usable": True},
            "判定": {"gap": gap, "R": R, "z": (R - 1) / se, "ci95": [R - 2 * se, R + 2 * se],
                     "最小效应量": 0.03, "警告": None, "note": note}}


def _AP(gap=False, R=1.0, se=0.02, applicable=True):
    return {"lottery": "七星彩", "applicable": applicable, "n": 900,
            "主口径": {"R": R, "se_块bootstrap": se, "se_长块bootstrap": se,
                       "ci95": [R - 2 * se, R + 2 * se], "usable": applicable},
            "各奖级": {"六等": {"usable": applicable, "R": R, "n": 900}},
            "各奖级R极差": 0.01, "全奖级低于1": R < 1,
            "判定": {"gap": gap, "R": R, "z": (R - 1) / se,
                     "ci95": [R - 2 * se, R + 2 * se], "最小效应量": 0.03,
                     "note": "x"}}


def _B(gap=False, mean_u=0.5):
    return {"applicable": True, "n": 2000, "mean_u": mean_u, "gap": gap,
            "ci95": [mean_u - 0.02, mean_u + 0.02], "模型Go": True, "se": 0.005}


def _C(applicable=True, practical=None, inhomog=None):
    return {"lottery": "双色球", "applicable": applicable,
            "n检验": 8, "族": {"双色球|位级": {"m": 1, "统计显著": 0,
                                              "实质显著": 0, "分段不一致": 0}},
            "检验": [], "实质偏离": practical or [], "分段不一致": inhomog or [],
            "统计显著但效应微小": [], "gap": False, "gap说明": "C 轨不单独判缝隙",
            "结论": "x", "排查": {}, "口径": {}}


def _D(applicable=True, gap=False, n_aw=21):
    return {"lottery": "七星彩", "applicable": applicable, "n_派奖期": n_aw,
            "n_平常期": 880, "各奖级": [], "结论": "x", "gap": gap,
            "gap说明": "D 轨的 gap 只表示两组有差异，不是「存在机制缝隙」的判据",
            "功效": {"检出下限": {"奖级": "六等", "可检出|ΔR|下限": 0.36}},
            "未覆盖的特别时期形态": []}


def _patch(monkeypatch, a=None, ap=None, b=None, c=None, d=None):
    monkeypatch.setattr(GV, "ticket_ratio_test", lambda lot, **k: a or _A())
    monkeypatch.setattr(GV, "digital_ticket_ratio_test", lambda lot, **k: ap or _AP())
    monkeypatch.setattr(GV, "walk_forward_quantiles",
                        lambda lot, **k: {"applicable": True, "walk_forward": True,
                                          "n_leak": 0, "n_total": 3000, "模型Go": True,
                                          "cuts": [], "u": None})
    monkeypatch.setattr(GV, "quantile_test", lambda wf, **k: b or _B())
    monkeypatch.setattr(GV, "fit_gamma",
                        lambda lot, **k: {"gamma_合并": {"gamma": 0.01, "se": 0.02,
                                                         "显著": False}})
    monkeypatch.setattr(GV, "structural_bias_report", lambda lot, **k: c or _C())
    monkeypatch.setattr(GV, "special_period_report", lambda lot, **k: d or _D())


# ------------------------------------------------------------------ ①②③
def test_lotto_verdict_uses_a_and_b(monkeypatch):
    _patch(monkeypatch, a=_A(gap=True, R=0.95), b=_B(gap=True, mean_u=0.48))
    r = GV.measure_gap("双色球", use_cache=False)
    assert r["轨类型"] == "乐透型"
    assert r["裁决"]["status"] == "strong"
    assert r["裁决"]["判定轨"] == ["A", "B"]
    assert r["裁决"]["方向一致"] is True


def test_lotto_weak_when_only_one_track(monkeypatch):
    _patch(monkeypatch, a=_A(gap=True, R=0.95), b=_B(gap=False, mean_u=0.50))
    r = GV.measure_gap("双色球", use_cache=False)
    assert r["裁决"]["status"] == "weak"
    assert "弱证据" in r["裁决"]["note"]


def test_digital_verdict_has_single_judging_track(monkeypatch):
    _patch(monkeypatch, ap=_AP(gap=True, R=0.95))
    r = GV.measure_gap("七星彩", use_cache=False)
    assert r["轨类型"] == "数字型"
    assert r["裁决"]["判定轨"] == ["A′"]
    # 只有一轨 ⇒ 方向一致性无定义，不得因此判"不一致"
    assert r["裁决"]["方向一致"] is None
    assert r["裁决"]["status"] == "weak", "单判定轨即使检出也只能是 weak"
    assert r["裁决"]["R"] == 0.95


def test_digital_neutral_verdict(monkeypatch):
    _patch(monkeypatch, ap=_AP(gap=False, R=1.03))
    r = GV.measure_gap("排列5", use_cache=False)
    assert r["裁决"]["status"] == "none"
    assert "未检出机制缝隙" in r["裁决"]["note"]
    assert "不表示已证明公平" in r["裁决"]["note"]


def test_pl3_3d_have_no_judging_track(monkeypatch):
    """无判定轨 ≠ 无缝隙：必须显式区分，否则会给出错误的"已检验"印象。"""
    _patch(monkeypatch)
    monkeypatch.setattr(GV, "digital_ticket_ratio_test", lambda lot, **k: (
        {"lottery": lot, "applicable": False, "note": "销售额覆盖全部玩法…"}))
    for lot in ("排列3", "福彩3D"):
        r = GV.measure_gap(lot, use_cache=False)
        assert r["裁决"]["status"] == "no-track"
        note = r["裁决"]["note"]
        assert "没有可用的判定轨" in note
        # 关键：不能说「未检出缝隙」——那是"检验过了没检出"，与"根本没检验"完全不同
        assert "未检出机制缝隙" not in note
        assert "不表示已证明公平" not in note


def test_unknown_lottery_degrades():
    r = GV.measure_gap("不存在的彩种", use_cache=False)
    assert r["applicable"] is False
    assert r["status"] == "not-applicable"


# ------------------------------------------------------------------ ① C/D 不判
def test_c_track_practical_deviation_does_not_change_status(monkeypatch):
    _patch(monkeypatch, a=_A(gap=False, R=1.0), b=_B(gap=False, mean_u=0.5),
           c=_C(practical=[{"族": "双色球|红球形态", "特征": "和值",
                            "统计量": 5.0, "效应量": 0.05, "q": 0.001}]))
    r = GV.measure_gap("双色球", use_cache=False)
    assert r["裁决"]["status"] == "none"
    assert r["裁决"]["C轨"] is False
    assert "C 轨有实质结构偏离" in r["裁决"]["note"]


def test_d_track_difference_does_not_change_status(monkeypatch):
    _patch(monkeypatch, ap=_AP(gap=False, R=1.0), d=_D(gap=True))
    r = GV.measure_gap("七星彩", use_cache=False)
    assert r["裁决"]["status"] == "none"
    assert r["裁决"]["D轨"] is True
    assert "参考" in r["裁决"]["note"]


def test_c_track_failure_is_contained(monkeypatch):
    _patch(monkeypatch, a=_A())

    def boom(lot, **k):
        raise RuntimeError("C 崩了")
    monkeypatch.setattr(GV, "structural_bias_report", boom)
    r = GV.measure_gap("双色球", use_cache=False)
    assert r["applicable"] is True
    assert r["track结果"]["C"]["applicable"] is False
    assert "执行失败" in r["track结果"]["C"]["note"]


# ------------------------------------------------------------------ ④ BH
def test_bh_excludes_lotteries_without_judging_track(monkeypatch):
    _patch(monkeypatch, a=_A(gap=False, R=1.0), ap=_AP(gap=False, R=1.0))
    real = GV.measure_gap

    def meas(lot, **k):
        k.pop("use_cache", None)
        if lot in ("排列3", "福彩3D"):
            out = {"lottery": lot, "applicable": True, "轨类型": "数字型",
                   "track结果": {"A′": {"applicable": False, "note": "x"}},
                   "gamma": {"gamma_合并": None}}
            out["裁决"] = GV._verdict(out)
            return out
        return real(lot, use_cache=False, **k)
    monkeypatch.setattr(GV, "measure_gap", meas)
    g = GV.measure_gap_global(["双色球", "大乐透", "排列5", "七星彩",
                               "排列3", "福彩3D"], use_cache=False)
    assert g["彩种数"] == 6
    assert g["入BH彩种数"] == 4
    assert g["逐彩种"][4]["BH_q"] is None and g["逐彩种"][5]["BH_q"] is None
    assert all(per["BH_q"] is not None for per in g["逐彩种"][:4])


def test_judging_pvalue_sign_is_one_sided_cold():
    cold = {"轨类型": "乐透型", "track结果": {"A": _A(R=0.95, se=0.02)}}
    hot = {"轨类型": "乐透型", "track结果": {"A": _A(R=1.05, se=0.02)}}
    assert GV._judging_pvalue(cold) < 0.05
    assert GV._judging_pvalue(hot) > 0.95
    assert GV._judging_pvalue({"轨类型": "乐透型", "track结果": {}}) is None


# ------------------------------------------------------------------ ⑤ 缓存
def test_cache_protocol_bumped_to_v3():
    """v2→v3：B 轨判定/z/γ 的标准误由「解析/短块」改为长块 bootstrap。

    这条断言的对象**就是**协议版本本身，所以 bump 时必须同步改它 ——
    它不是「迁就实现的改断言」，而是防止「改了口径却让旧缓存继续命中」的那道锁。
    """
    assert gap_cache._PROTOCOL == "v3"


def _sig(lottery="双色球", n=1, last="1", **over):
    kw = dict(min_train=1200, step=50, min_effect=0.03, c_B=2000, c_mc=100_000)
    kw.update(over)
    return gap_cache.make_sig(lottery, n, last, **kw)


def test_old_protocol_cache_is_not_reused(tmp_path, monkeypatch):
    """v2 的指纹（含旧协议前缀、少 5 个阈值段）必须读不出来。"""
    monkeypatch.setattr(gap_cache, "CACHE_DIR", tmp_path / "mechanism")
    gap_cache.save("双色球", {"x": 1}, "v2|双色球|n=1|last=1")
    assert gap_cache.load("双色球", _sig()) is None
    # 同参数的新 sig 也不能命中别人的落盘 ⇒ 说明读的是 _sig 字段比对
    gap_cache.save("双色球", {"x": 1}, _sig())
    assert gap_cache.load("双色球", _sig(min_effect=0.1)) is None


def test_sig_changes_with_each_threshold_parameter():
    """缺陷 ③ 的正面锁：五个阈值参数任一变化，指纹都必须变。"""
    base = _sig()
    for over in ({"min_train": 1201}, {"step": 51}, {"min_effect": 0.031},
                 {"c_B": 2001}, {"c_mc": 100_001}):
        assert _sig(**over) != base, over


def test_sig_identical_for_same_thresholds():
    """反向锁：同参数必须同指纹，否则修复会把缓存彻底打死（每次都重算）。"""
    assert _sig() == _sig()
    # 浮点归一：0.03 与 0.030000000000000002 必须同指纹
    assert _sig(min_effect=0.03) == _sig(min_effect=0.030000000000000002)


def test_make_sig_requires_all_threshold_params():
    """刻意不给默认值：漏传阈值 ⇒ TypeError（响亮失败），不静默用旧阈值的结果。"""
    with pytest.raises(TypeError):
        gap_cache.make_sig("双色球", 100, "1")


def test_measure_gap_recomputes_when_min_effect_changes_under_cache(monkeypatch, tmp_path):
    """缺陷 ③ 的直接回归测试：改 --min-effect 必须重算，不能静默吃旧缓存。"""
    monkeypatch.setattr(gap_cache, "CACHE_DIR", tmp_path / "mechanism")
    _patch(monkeypatch, a=_A(), b=_B())
    r1 = GV.measure_gap("双色球", min_effect=0.03)
    r2 = GV.measure_gap("双色球", min_effect=0.03)
    r3 = GV.measure_gap("双色球", min_effect=0.10)
    assert r1["_from_cache"] is False
    assert r2["_from_cache"] is True, "同参数必须命中缓存"
    assert r3["_from_cache"] is False, "改了 min_effect 却吃了旧缓存"
    assert r1["sig"] != r3["sig"]


def test_measure_gap_caches_hit_for_identical_params(monkeypatch, tmp_path):
    """反向锁：同参数第二次必须命中（否则缓存失去意义，全量重算到无法使用）。"""
    monkeypatch.setattr(gap_cache, "CACHE_DIR", tmp_path / "mechanism")
    _patch(monkeypatch, a=_A(), b=_B())
    GV.measure_gap("双色球")
    assert GV.measure_gap("双色球")["_from_cache"] is True


def test_cached_result_is_flagged(monkeypatch, tmp_path):
    monkeypatch.setattr(gap_cache, "CACHE_DIR", tmp_path / "mechanism")
    _patch(monkeypatch, a=_A(), b=_B())
    r1 = GV.measure_gap("双色球")
    assert r1["_from_cache"] is False
    r2 = GV.measure_gap("双色球")
    assert r2["_from_cache"] is True


# ------------------------------------------------------------------ 报告文本
def test_verdict_exposes_available_and_expected_tracks_separately():
    """「本次真判过的轨」与「本应存在的轨」必须分开报。

    根因：旧实现 `"判定轨": labels or [s[0] for s in spots]` 在 labels 为空时
    回落到 spots，于是排列3 明明没有可用判定轨，却报出 `判定轨 = ["A′"]`。
    """
    out = {"轨类型": "数字型", "track结果": {"A′": _AP(applicable=False)}}
    v = GV._verdict(out)
    assert v["status"] == "no-track"
    assert v["判定轨"] == [], "无判定轨时 判定轨 必须是空列表"
    assert v["应有判定轨"] == ["A′"]


def test_report_text_judging_track_column_matches_verdict_status(monkeypatch, tmp_path):
    """表格契约：同一行的「判定轨」列与「裁决」列不得自相矛盾。

    旧实现按彩种类型硬打印 `A′`/`A`，于是排列3 那行会同时出现
    「判定轨 = A′」与「裁决 = 无判定轨」——读者会误以为它有 A′ 轨但没检出。
    """
    from ev import gap
    monkeypatch.setattr(gap_cache, "CACHE_DIR", tmp_path / "mechanism")
    _patch(monkeypatch, a=_A(), b=_B())

    def meas(lot, **k):
        for key in ("use_cache", "force", "min_train", "step", "min_effect",
                    "c_mc", "tracks"):
            k.pop(key, None)
        dig = lot in ("排列3", "排列5", "福彩3D", "七星彩")
        out = {"lottery": lot, "applicable": True, "n": 1000,
               "轨类型": "数字型" if dig else "乐透型",
               "track结果": {"C": _C(), "D": _D()},
               "gamma": {"gamma_合并": None}}
        if dig:
            # 排列3 / 福彩3D 的 A′ 轨不适用（销售额覆盖全部玩法）
            ap = _AP(applicable=lot in ("排列5", "七星彩"))
            out["track结果"]["A′"] = ap
        else:
            out["track结果"]["A"] = _A()
            out["track结果"]["B"] = _B()
        out["裁决"] = GV._verdict(out)
        return out
    monkeypatch.setattr(GV, "measure_gap", meas)
    txt = gap.report_text(["双色球", "排列5", "排列3"], use_cache=False)

    rows = {}
    for ln in txt.splitlines():
        tok = ln.split()
        # 主表行的第 2 个 token 是型别（数字/乐透）；C/D 明细表与 note 行都不匹配
        if len(tok) > 1 and tok[0] in ("双色球", "排列5", "排列3") \
                and tok[1] in ("数字", "乐透"):
            rows[tok[0]] = ln
    assert set(rows) == {"双色球", "排列5", "排列3"}, rows
    # 有判定轨的：显示真实判定轨
    assert "A/B" in rows["双色球"] and "无判定轨" not in rows["双色球"]
    assert "A′" in rows["排列5"]
    # 无判定轨的：判定轨列显示「无」，**不得出现 A′**
    assert "无判定轨" in rows["排列3"]
    assert "A′" not in rows["排列3"], rows["排列3"]


def test_report_text_covers_four_tracks(monkeypatch, tmp_path):
    """CLI 与看板共用同一份措辞：四轨说明、C/D 不判缝隙、A′ 降级理由都必须出现。"""
    from ev import gap
    monkeypatch.setattr(gap_cache, "CACHE_DIR", tmp_path / "mechanism")
    _patch(monkeypatch, a=_A(), b=_B())

    def meas(lot, **k):
        for key in ("use_cache", "force", "min_train", "step", "min_effect",
                    "c_mc", "tracks"):
            k.pop(key, None)
        assert not k, f"未知参数：{k}"
        dig = lot in ("排列3", "排列5", "福彩3D", "七星彩")
        out = {"lottery": lot, "applicable": True, "n": 1000,
               "轨类型": "数字型" if dig else "乐透型",
               "track结果": {"C": _C(), "D": _D()},
               "gamma": {"gamma_合并": None}}
        if dig:
            out["track结果"]["A′"] = _AP()
        else:
            out["track结果"]["A"] = _A()
            out["track结果"]["B"] = _B()
        out["裁决"] = GV._verdict(out)
        return out
    monkeypatch.setattr(GV, "measure_gap", meas)
    txt = gap.report_text(["双色球", "七星彩"], use_cache=False)
    assert "机制缝隙测量 · 四轨" in txt
    assert "A′ 轨" in txt and "C  轨" in txt and "D  轨" in txt
    assert "判定轨只有 A/A′/B；C/D 一律不参与判定" in txt
    assert "排列3 / 福彩3D" in txt, "A′ 轨的降级理由必须写进报告"
