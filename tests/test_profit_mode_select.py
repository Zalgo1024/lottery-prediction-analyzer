# -*- coding: utf-8 -*-
"""两模式分离：**结构上**不许把「覆盖」和「净EV」合成一个总分。

这批测试的重点不是"跑得通"，而是"混用会被挡住"：
每一个混用入口都必须是**报错**，不是静默忽略 —— 静默忽略等于默认允许。
"""

from __future__ import annotations

import inspect

import pytest

from ev.profit import mode_select as MS

LOT = "双色球"


def _pool(n=8):
    """确定性小池（互相重叠通常 ≤3，cap=3 不会一上来就卡死）。"""
    out = []
    for i in range(n):
        reds = sorted({(i * 4 + k) % 33 + 1 for k in range(6)})
        if len(reds) < 6:
            continue
        out.append({"红球": reds, "蓝球": [i % 16 + 1]})
    return out


def test_no_weights_parameter_exists():
    """连参数都没有 —— 这是**结构性**禁止，不是"默认禁用"。"""
    sig = inspect.signature(MS.select)
    assert "weights" not in sig.parameters
    assert "score" not in sig.parameters
    assert "综合分" not in sig.parameters


def test_unknown_mode_is_rejected_and_says_why():
    with pytest.raises(ValueError) as ei:
        MS.select("综合分", lottery=LOT, K=3, pool=_pool())
    msg = str(ei.value)
    assert "权重无法事先约定" in msg
    assert "四等奖覆盖" in msg and "净EV" in msg


def test_coverage_mode_rejects_kappa():
    """κ 是「机制倾斜/拥挤度」侧的口径，覆盖模式用它就会滑向"买冷门更容易中"。"""
    with pytest.raises(ValueError, match="kappa"):
        MS.select(MS.模式_覆盖, lottery=LOT, K=3, pool=_pool(),
                  target={"kind": "grade", "grade": "四等"}, kappa=0.3)


def test_coverage_mode_rejects_ctx():
    with pytest.raises(ValueError, match="ctx"):
        MS.select(MS.模式_覆盖, lottery=LOT, K=3, pool=_pool(),
                  target={"kind": "grade", "grade": "四等"}, ctx=object())


def test_netev_mode_rejects_coverage_target():
    with pytest.raises(ValueError, match="target"):
        MS.select(MS.模式_净EV, lottery=LOT, K=3,
                  target={"kind": "grade", "grade": "四等"})


def test_netev_mode_rejects_external_pool():
    with pytest.raises(ValueError, match="pool"):
        MS.select(MS.模式_净EV, lottery=LOT, K=3, pool=_pool())


def test_coverage_mode_rejects_unknown_selector():
    with pytest.raises(ValueError, match="选择器"):
        MS.select(MS.模式_覆盖, lottery=LOT, K=2, pool=_pool(),
                  target={"kind": "grade", "grade": "四等"}, selector="magic")


def test_coverage_mode_returns_caveat_and_no_verdict():
    r = MS.select(MS.模式_覆盖, lottery=LOT, K=2, pool=_pool(),
                  target={"kind": "grade", "grade": "四等"})
    assert r["模式"] == MS.模式_覆盖
    assert r["注数"] == len(r["票"]) <= 2
    assert r["判决"] is None, "覆盖模式不该给「要不要参与」的判决（那是预算闸门的事）"
    assert "不改变任何一注自己的中奖概率" in r["诚实边界"]


def test_netev_mode_requires_issue():
    """不给期号 = 允许前视：奖池/销量/分奖人数全按期取数，必须显式拒绝。"""
    with pytest.raises(ValueError, match="issue"):
        MS.select(MS.模式_净EV, lottery=LOT, K=2)


def _fake_ctx(*, outer_choose=True, has_float=True):
    """带 `.rules` 的假上下文（满足模型适用性闸所需的最小字段）。"""
    import types

    kind = "floating" if has_float else "fixed"
    return types.SimpleNamespace(rules=types.SimpleNamespace(
        outer_choose=outer_choose,
        grades={"一等": types.SimpleNamespace(kind=kind)}))


def test_netev_mode_degrades_and_says_so(monkeypatch):
    """来源不可用 ⇒ 必须标退化，不许把"排了序"说成"选得更准"。"""
    from ev.profit import conservative_ev as CE
    from ev.profit import payouts as P
    from ev.profit import select as PS
    from ev.profit import tilt as T

    monkeypatch.setattr(T, "outer_tilt", lambda *a, **k: None)   # 无来源
    monkeypatch.setattr(P, "build_ctx", lambda *a, **k: _fake_ctx())
    monkeypatch.setattr(PS, "select_profit_tickets", lambda *a, **k: {
        "票": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}],
        "元数据": {"倾斜来源": "无（模型未Go/不支持）"},
    })
    monkeypatch.setattr(CE, "verdict", lambda *a, **k: {
        "判决": "不参与", "理由": "最保守情景净 EV ≤ 0"})

    r = MS.select(MS.模式_净EV, lottery=LOT, K=1, issue="2026126")
    assert r["退化"] is True
    assert any("退化中性基线" in s for s in r["说明"])
    assert r["判决"] == "不参与"
    assert any("没有" in s and "可证明" in s for s in r["说明"])
    assert "不能提高中奖概率" in r["诚实边界"]


def test_netev_mode_not_degraded_when_tilt_validated(monkeypatch):
    """来源经 Go 门槛/CV 检验（measured_GO）⇒ 不退化。"""
    import types

    from ev.profit import conservative_ev as CE
    from ev.profit import payouts as P
    from ev.profit import select as PS
    from ev.profit import tilt as T

    monkeypatch.setattr(T, "outer_tilt", lambda *a, **k: types.SimpleNamespace(
        source="measured_GO", beta_asof=None))
    monkeypatch.setattr(P, "build_ctx", lambda *a, **k: _fake_ctx())
    monkeypatch.setattr(PS, "select_profit_tickets", lambda *a, **k: {
        "票": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}],
        "元数据": {"倾斜来源": "measured_GO"},
    })
    monkeypatch.setattr(CE, "verdict", lambda *a, **k: {"判决": "仅情景内正期望", "理由": "x"})

    r = MS.select(MS.模式_净EV, lottery=LOT, K=1, issue="2026126", kappa=0.5)
    assert r["退化"] is False
    assert not any("退化中性基线" in s for s in r["说明"])


def test_netev_forced_neutral_when_source_unavailable(monkeypatch):
    """来源不可用但请求了 κ>0 ⇒ 强制降为 0（不许让无来源主导排序）。"""
    from ev.profit import conservative_ev as CE
    from ev.profit import payouts as P
    from ev.profit import select as PS
    from ev.profit import tilt as T

    captured = {}

    def _sel(lottery, K, ctx, kappa=0.0, seed=0, **kw):
        captured["kappa"] = kappa
        return {"票": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}],
                "元数据": {"倾斜来源": "无（模型未Go/不支持）"}}

    monkeypatch.setattr(T, "outer_tilt", lambda *a, **k: None)
    monkeypatch.setattr(P, "build_ctx", lambda *a, **k: _fake_ctx())
    monkeypatch.setattr(PS, "select_profit_tickets", _sel)
    monkeypatch.setattr(CE, "verdict", lambda *a, **k: {"判决": "不参与", "理由": "x"})

    r = MS.select(MS.模式_净EV, lottery=LOT, K=1, issue="2026126", kappa=0.5)
    assert captured["kappa"] == 0.0, "来源不可用时 κ 必须被强制降为 0"
    assert any("强制降为 0" in s for s in r["说明"])


def test_digital_lotteries_are_not_applicable(monkeypatch):
    """数字型（可重复数字 / 无浮动奖）⇒ 净EV 模式判「不适用」：不给票、不给正期望。

    ★ 这是本轮发现的既有 bug 的闸门：`grade_probabilities` 对数字型按无放回 C(n,k) 算，
      实测排列3 直选 1/84（真实 1/1000）、七星彩一等 1/1260（真实 1/1500万）。
      不闸住它，排列3 会给出「净EV +10.38 元/注、可进一步评估倍数分配」的假阳性。
    """
    from ev.profit import payouts as P

    monkeypatch.setattr(P, "build_ctx",
                        lambda *a, **k: _fake_ctx(outer_choose=False, has_float=False))
    for lot in ("排列3", "福彩3D", "七星彩"):
        r = MS.select(MS.模式_净EV, lottery=lot, K=2, issue="dummy")
        assert r["判决"] == "不适用", (lot, r["判决"])
        assert r["票"] == [] and r["注数"] == 0
        assert r["结论首行"].startswith("不适用")
        assert r["结构性上限能否补缺口"] is None
        assert r["明细"]["模型适用"] is False
        assert ("10^k" in r["结论首行"]) or ("可重复" in r["结论首行"])


def test_kl8_not_applicable_when_ctx_unbuildable(monkeypatch):
    """快乐8 的开奖摇出数 ≠ 票面数、不可枚举 ⇒ build_ctx 抛错时判「不适用」，不崩溃。"""
    from ev.profit import payouts as P

    def _boom(*a, **k):
        raise NotImplementedError("快乐8 是 hypergeom 模型（开奖摇出数 ≠ 票面数），不可枚举")

    monkeypatch.setattr(P, "build_ctx", _boom)
    r = MS.select(MS.模式_净EV, lottery="快乐8", K=2, issue="2026293")
    assert r["判决"] == "不适用"
    assert r["票"] == [] and r["注数"] == 0
    assert "hypergeom" in r["结论首行"]
    assert r["明细"]["模型适用"] is False


def test_lottery_not_applicable_when_selector_raises(monkeypatch):
    """选择器表示不了该彩种（七乐彩 '基本号' / 快乐8 hypergeom）⇒ 判「不适用」，不崩溃。"""
    from ev.profit import payouts as P
    from ev.profit import select as PS

    monkeypatch.setattr(P, "build_ctx", lambda *a, **k: _fake_ctx())

    def _boom(*a, **k):
        raise KeyError("基本号")

    monkeypatch.setattr(PS, "select_profit_tickets", _boom)
    r = MS.select(MS.模式_净EV, lottery="七乐彩", K=2, issue="2026127")
    assert r["判决"] == "不适用"
    assert r["票"] == []
    assert "基本号" in r["结论首行"]
    assert r["明细"]["模型适用"] is False


def test_tilt_reliability_tiers():
    """可靠性分级：measured_GO/walk_forward 可用；crowd_pi_v2 与空 不可用。"""
    assert MS._tilt_reliability("measured_GO", "双色球")[0] is True
    assert MS._tilt_reliability("walk_forward（β as-of 2026-10-01）", "大乐透")[0] is True
    ok, tier, why = MS._tilt_reliability("crowd_pi_v2", "排列3")
    assert ok is False and tier == "proxy_unvalidated" and "无 Go 门槛" in why
    assert MS._tilt_reliability("无（模型未Go/不支持）", "七乐彩")[0] is False
    assert MS._tilt_reliability("", "快乐8")[0] is False


# ---------------------------------------------------------------- 默认结论首行
def _verdict(**kw):
    base = {"判决": "不参与", "理由": "x",
            "保守净EV_元每注": -0.7, "结构性上限能否补缺口": False}
    base.update(kw)
    return base


def test_conclusion_line_defaults_to_no_provable_advantage():
    """默认结论必须是「无可证明优势」，且**首行**，依据含结构性上限。"""
    line = MS._conclusion_line(_verdict())
    assert line.startswith("无可证明优势"), line
    assert "保守情景净 EV" in line
    assert "结构性上限" in line
    # 不能说成「已证明永远不可能」那种越界话
    assert "不代表永远不可能" in line
    for w in ("证伪", "证否", "不可预测", "已证明公平", "公平随机", "写死"):
        assert w not in line


def test_conclusion_line_upgrades_only_with_positive_and_closable():
    a = MS._conclusion_line(_verdict(判决="仅情景内正期望", 保守净EV_元每注=0.1))
    assert a.startswith("仅情景内正期望")
    assert "不得据情景单点放大投入" in a

    b = MS._conclusion_line(_verdict(判决="可进一步评估倍数分配", 保守净EV_元每注=0.1,
                                     结构性上限能否补缺口=True))
    assert b.startswith("可进一步评估倍数分配")
    assert "须先登记新预注册块" in b          # 升级≠建议加倍


def test_conclusion_line_is_first_key_and_flags_no_advantage(monkeypatch):
    """`select()` 的返回里 `结论首行` 必须排第一，`无可证明优势` 与判决一致。"""
    from ev.profit import conservative_ev as CE
    from ev.profit import payouts as P
    from ev.profit import select as PS
    from ev.profit import tilt as T

    monkeypatch.setattr(T, "outer_tilt", lambda *a, **k: None)
    monkeypatch.setattr(P, "build_ctx", lambda *a, **k: _fake_ctx())
    monkeypatch.setattr(PS, "select_profit_tickets", lambda *a, **k: {
        "票": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}],
        "元数据": {"倾斜来源": "无（模型未Go/不支持）"},
    })
    monkeypatch.setattr(CE, "verdict", lambda *a, **k: _verdict())

    r = MS.select(MS.模式_净EV, lottery=LOT, K=1, issue="2026126")
    assert list(r.keys())[0] == "结论首行", "结论必须排在返回结构第一位"
    assert r["无可证明优势"] is True
    assert r["结构性上限能否补缺口"] is False
    assert r["判决"] == "不参与"
    assert r["结论首行"].startswith("无可证明优势")


# ---------------------------------------------------------------- 绝对值口径
def test_ev_proxy_absolute_level_matches_hand_calc():
    """代理 EV 的**绝对水平**必须等于手算的期望回款。

    ★ 2026-11-03 实踩：`ev_proxy` 把 `_uniform_prob`（已是联合 P(h,j)）又乘了一遍
      `p_j[j]` ⇒ 内层概率算两次。同一口径下（仅固定奖，奖池归零）：0.0934 vs 手算 0.4863
      —— 旧值只有真实的 **19.2%**（含浮动奖时 0.2457 vs 1.1754）。
      排序没受影响（同彩种内各票内层结构相同 ⇒ 各格同乘常数），所以任何"比大小"的
      测试都发现不了；**只有拿绝对值与手算对账才抓得到**。
    """
    import math

    import numpy as np

    from ev.profit import payouts as P
    from ev.profit import select as PS
    from ev.profit.rules import rules_for

    r = rules_for(LOT)
    ctx = P.build_ctx(LOT, "26126")
    row = np.array([[16, 20, 21, 24, 32, 33]], dtype=np.int16)

    # 只留固定奖（把浮动奖池归零）⇒ 与手算逐格相加可比
    class _NoPool:
        rules = r
        total_tickets = ctx.total_tickets

        @staticmethod
        def payout_basis(_g):
            return 0.0

    got = float(PS.ev_proxy(LOT, row, np.zeros(1), _NoPool(), 0.0)[0])

    n, wdt = r.outer_range[1], r.outer_width
    want = 0.0
    for (h, j), g in r.grade_table.items():
        gr = r.grades[g]
        if gr.kind != "fixed":
            continue
        p_h = math.comb(wdt, h) * math.comb(n - wdt, wdt - h) / math.comb(n, wdt)
        p_j = r.inner_multiplicity.get(j, 0) / float(r.inner_size)
        want += p_h * p_j * float(gr.fixed)

    assert got == pytest.approx(want, rel=1e-12), f"代理 EV {got} != 手算 {want}"
