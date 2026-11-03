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


def test_netev_mode_requires_issue():
    """不给期号 = 允许前视：奖池/销量/分奖人数全按期取数，必须显式拒绝。"""
    with pytest.raises(ValueError, match="issue"):
        MS.select(MS.模式_净EV, lottery=LOT, K=2)


def test_netev_mode_degrades_and_says_so(monkeypatch):
    """模型未 Go / 倾斜来源为空 ⇒ 必须标退化，不许把"排了序"说成"选得更准"。"""
    from ev.profit import conservative_ev as CE
    from ev.profit import payouts as P
    from ev.profit import select as PS

    monkeypatch.setattr(P, "build_ctx", lambda *a, **k: object())
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


def test_netev_mode_not_degraded_when_tilt_present(monkeypatch):
    from ev.profit import conservative_ev as CE
    from ev.profit import payouts as P
    from ev.profit import select as PS

    monkeypatch.setattr(P, "build_ctx", lambda *a, **k: object())
    monkeypatch.setattr(PS, "select_profit_tickets", lambda *a, **k: {
        "票": [{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [1]}],
        "元数据": {"倾斜来源": "popularity（β as-of 2026-10-01）"},
    })
    monkeypatch.setattr(CE, "verdict", lambda *a, **k: {"判决": "仅情景内正期望", "理由": "x"})

    r = MS.select(MS.模式_净EV, lottery=LOT, K=1, issue="2026126", kappa=0.5)
    assert r["退化"] is False
    assert not any("退化中性基线" in s for s in r["说明"])


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
