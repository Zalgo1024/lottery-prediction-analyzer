"""
P6 测试：机制缝隙类假设的注册与覆盖说明（discovery/hypothesis_registry.py）

与选号 edge 类假设是**两个问题**，别混：
- edge 类（credibility_gate / ev_edge）问「有没有策略能跑赢随机」；
- 缝隙类（mechanism_gap）问「开奖结果本身是否偏离等可能」。

本文件锁三件事：
① 模板按彩种可判定性给，不给齐（排列3/福彩3D 无 A′ 轨，硬注册 = 承诺一个测不了的检验）；
② `adjudicating` 标记与四轨纪律一致 —— **C/D 恒 False**，不许把诊断轨抬进判定；
③ 注册后能被 `_hypothesis_coverage` 正确覆盖，且明确写出「e-value 不覆盖」。

★ 全程在 tmp 目录里注册，**不碰 training/hypotheses/ 真身**：
真登记册里 25 条已判 rejected 是对外结论的一部分，测试不该顺手改它。
"""
import json

import pytest

from discovery.hypothesis_registry import (
    _GAP_ADJUDICATING, _gap_templates, auto_templates,
)

LOTTERIES = ["双色球", "大乐透", "排列5", "福彩3D", "排列3", "七星彩"]


def _register_in_tmp(tmp_path, monkeypatch, lottery):
    """把登记目录指到 tmp，跑 add_auto_hypotheses，返回新增假设。"""
    # monkeypatch 目标模块自己的属性（hr.HYP_DIR）——list_hypotheses 走 _path()
    # 读的正是这个全局，所以 always_valid 侧无需 reload。
    from discovery import hypothesis_registry as hr
    monkeypatch.setattr(hr, "HYP_DIR", tmp_path, raising=True)
    return hr.add_auto_hypotheses(lottery)


# ----------------------------------------------------------- ① 模板按可判定性给
def test_gap_templates_exist_for_all_lotteries():
    """六彩种都有缝隙模板——没有判定轨的（排列3/福彩3D）也至少留诊断轨。"""
    for lot in LOTTERIES:
        assert _gap_templates(lot), f"{lot} 没有任何缝隙模板"


def test_no_ap_track_for_3d_and_p3():
    """排列3/福彩3D **不给** A′ 轨：销售额覆盖全部玩法，量到的是投注份额不是开奖倾向。

    注册一个测不了的检验，比不注册更糟——它会一直挂着 proposed，
    读者却以为「还没测」而不是「测不了」。
    """
    for lot in ("排列3", "福彩3D"):
        tracks = {t["track"] for t in _gap_templates(lot)}
        assert "A′" not in tracks, f"{lot} 不该注册 A′ 轨（不可判定）"


def test_p5_and_qxc_have_ap_track():
    """排列5/七星彩有 A′ 轨（单玩法单奖级 / 切现行段后可判定）。"""
    for lot in ("排列5", "七星彩"):
        assert "A′" in {t["track"] for t in _gap_templates(lot)}


def test_lottery_type_has_a_and_b():
    """双色球/大乐透走 A（注数比）+ B（开出号分位）两条判定轨。"""
    for lot in ("双色球", "大乐透"):
        tracks = {t["track"] for t in _gap_templates(lot)}
        assert tracks == {"A", "B"}, f"{lot} 判定轨应为 A+B，实际 {tracks}"


# ----------------------------------------------------------- ② adjudicating 纪律
def test_cd_tracks_are_never_adjudicating():
    """★ 硬纪律：C/D 轨**一律不参与裁决**，只作诊断。"""
    assert _GAP_ADJUDICATING["C"] is False
    assert _GAP_ADJUDICATING["D"] is False
    for lot in LOTTERIES:
        for t in _gap_templates(lot):
            if t["track"] in ("C", "D"):
                assert t["adjudicating"] is False, (
                    f"{lot}/{t['name']} 是诊断轨却标了可裁决")


def test_ab_tracks_are_adjudicating():
    assert _GAP_ADJUDICATING["A"] is True
    assert _GAP_ADJUDICATING["A′"] is True
    assert _GAP_ADJUDICATING["B"] is True


def test_only_known_tracks():
    """不许凭空造轨——轨名必须与 ev/mechanism 四轨一致。"""
    for lot in LOTTERIES:
        for t in _gap_templates(lot):
            assert t["track"] in _GAP_ADJUDICATING, f"未知轨 {t['track']}"


def test_gap_names_are_unique_per_lottery():
    """name 是 add_auto_hypotheses 的去重键，重名会静默吞掉一条假设。"""
    for lot in LOTTERIES:
        names = [t["name"] for t in _gap_templates(lot)]
        assert len(names) == len(set(names)), f"{lot} 模板重名：{names}"


def test_gap_templates_in_auto_templates():
    """gap 模板必须并入 auto_templates —— 否则 --auto 永远生成不到它。"""
    for lot in LOTTERIES:
        gap_names = {t["name"] for t in _gap_templates(lot)}
        auto_names = {t["name"] for t in auto_templates(lot)}
        assert gap_names <= auto_names, f"{lot} gap 模板未并入 auto_templates"


def test_auto_templates_still_has_legacy():
    """并入 gap 时不能把原有模板挤掉。"""
    legacy = {"高频策略OOS显著优于随机", "遗漏值策略OOS显著优于随机",
              "区间均衡策略OOS显著优于随机"}
    for lot in LOTTERIES:
        assert legacy <= {t["name"] for t in auto_templates(lot)}


def test_probe_type_is_mechanism_gap():
    for lot in LOTTERIES:
        for t in _gap_templates(lot):
            assert t["probe_type"] == "mechanism_gap"
            assert t["target_metric"], "缺目标指标，无法判定检验什么"


# ----------------------------------------------------------- ③ 注册后覆盖说明
@pytest.mark.parametrize("lottery", LOTTERIES)
def test_registered_gap_hypothesis_has_coverage(tmp_path, monkeypatch, lottery):
    """注册 → _hypothesis_coverage 能给出非占位说明，且写明 e-value 不覆盖。"""
    from discovery import always_valid as av
    added = _register_in_tmp(tmp_path, monkeypatch, lottery)
    assert added, f"{lottery} 未新增任何假设"

    cov = av._hypothesis_coverage(lottery)
    gap = [c for c in cov if c["probe"] == "mechanism_gap"]
    assert gap, f"{lottery} 注册了 gap 假设但覆盖说明里没有"
    for c in gap:
        assert "python cli.py gap" in c["e-value覆盖"]
        assert "不覆盖" in c["e-value覆盖"], "必须明说 e-value 不覆盖，别让读者误以为有把关"
        assert c["adjudicating"] in (True, False)   # 不许是 None/缺失


def test_registered_gap_carries_track(tmp_path, monkeypatch):
    """track / adjudicating 必须真的落盘（probe 里），不是只在模板里。"""
    _register_in_tmp(tmp_path, monkeypatch, "双色球")
    data = json.loads((tmp_path / "双色球_hypotheses.json").read_text(encoding="utf-8"))
    gap = [h for h in data if h["probe"]["type"] == "mechanism_gap"]
    assert len(gap) == 2
    for h in gap:
        assert h["probe"]["track"] in ("A", "B")
        assert h["probe"]["adjudicating"] is True
        assert h["status"] == "proposed"        # 新登记未测，不许预设立场
        assert h["tests"] == []


def test_register_is_idempotent(tmp_path, monkeypatch):
    """重复 --auto 不该重复登记（按 name 去重）。"""
    from discovery import hypothesis_registry as hr
    monkeypatch.setattr(hr, "HYP_DIR", tmp_path, raising=True)
    a = hr.add_auto_hypotheses("七星彩")
    b = hr.add_auto_hypotheses("七星彩")
    assert len(a) > 0 and b == [], "第二次 --auto 又登记了一遍"
    data = json.loads((tmp_path / "七星彩_hypotheses.json").read_text(encoding="utf-8"))
    names = [h["name"] for h in data]
    assert len(names) == len(set(names))


@pytest.mark.realdata
def test_real_registry_untouched():
    """★ 测试不得污染真登记册：真身仍是 25 条已判假设。

    一旦有人在测试里忘了 monkeypatch，这条会先红——比让对外结论悄悄漂移强。
    """
    from discovery import hypothesis_registry as hr
    total = sum(len(hr.load_hypotheses(nm)) for nm in LOTTERIES)
    assert total == 25, (
        f"真登记册变成 {total} 条（应为 25）。若确属有意新增，"
        f"请先同步结题报告「25 条假设」的口径再改这条断言")
