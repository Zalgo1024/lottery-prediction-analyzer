# -*- coding: utf-8 -*-
"""A 轨（注数比检验）测试。

锁定的契约：
  ① 公平开奖（γ=0）**不误报** —— 这是"尺子必须能量出缝隙很窄"的最低要求；
  ② 真偏置（R=0.85）**能检出**；
  ③ 「票量 = 总投注额/2」不成立时，销售额口径**必须标 usable=False**，
     不能悄悄参与判定（大乐透追加投注就是这么骗过原方案的）；
  ④ 免票量口径对票量整体乘一个常数**免疫**；
  ⑤ 数字型/七星彩 → applicable=False（不硬凑代理）；
  ⑥ 最小效应量门控：R=0.98 虽 CI 排除 1，但不达 3% 门槛 → 不判缝隙；
  ⑦ 时间趋势要给出可反驳的数字，而不是只报极差。
"""
from datetime import date, timedelta

import numpy as np
import pytest

from ev.mechanism.common import DrawFrame, prize_probs, ticket_calibration
from ev.mechanism.gap_a_ticket_ratio import ticket_ratio_test


def _frame(lottery="双色球", n=400, m1=1.0, sales_mult=1.0, trend=None,
           seed=20261003, spacing=5):
    """合成 DrawFrame：y2 ~ Poisson(N·p2)，y1 ~ Poisson(N·p1·m(t))。

    m1 或 trend 控制一等奖注数相对"公平"的倍数：
      m=1 → R=1；m=0.85 → R≈0.85；trend=0.2 → m 从 1.1 线性降到 0.9。
    """
    p = prize_probs(lottery)
    rng = np.random.default_rng(seed)
    N = np.full(n, 1.4e8)
    sales = 2.0 * N * sales_mult
    base = date(2003, 2, 1)
    dates = [base + timedelta(days=spacing * i) for i in range(n)]
    y2 = rng.poisson(N * p["二等"]).astype(float)
    m = np.full(n, float(m1))
    if trend is not None:
        m = np.linspace(1.0 + trend / 2.0, 1.0 - trend / 2.0, n)
    y1 = rng.poisson(N * p["一等"] * m).astype(float)
    reds = np.array([sorted(rng.choice(np.arange(1, 34), 6, replace=False))
                     for _ in range(n)], dtype=np.int16)
    return DrawFrame(lottery=lottery, issues=[str(2003000 + i) for i in range(n)],
                     dates=dates, reds=reds, prev=reds.copy(),
                     y1=y1, y2=y2, sales=sales, dropped=0)


def _det_frame(lottery="双色球", n=600, m=1.0, sales_mult=1.0, spacing=3,
               y2_val=150000.0):
    """**无噪声** frame：y1/y2 取到精确比值 → R 恰等于 m。

    构造（而不是 round(N·p1)）是刻意的：round 会引入 0.8% 的偏差，
    把"门控逻辑测试"变成"四舍五入测试"（实测踩到）。
    取 y2_val=150000：双色球 p1/p2=1/15 → y1=10000m，大乐透 p1/p2=1/20 → y1=7500m，
    m 取三位小数时 y1 恒为整数。
    """
    p = prize_probs(lottery)
    N = y2_val / p["二等"]                      # 使二等反推票量恰等于 N（校准精确通过）
    base = date(2003, 2, 1)
    dates = [base + timedelta(days=spacing * i) for i in range(n)]
    y2 = np.full(n, y2_val, dtype=float)
    y1 = np.full(n, y2_val * (p["一等"] / p["二等"]) * m, dtype=float)
    reds = np.tile(np.arange(1, 7, dtype=np.int16), (n, 1))
    return DrawFrame(lottery=lottery, issues=[str(2003000 + i) for i in range(n)],
                     dates=dates, reds=reds, prev=reds.copy(),
                     y1=y1, y2=y2, sales=np.full(n, 2.0 * N * sales_mult),
                     dropped=0)


# ------------------------------------------------------------ ①② 核心判据

def test_fair_draw_does_not_fire():
    """① 公平开奖（m=1）不得报出缝隙 —— 尺子必须能量出'没有'。"""
    r = ticket_ratio_test("双色球", frame=_frame(m1=1.0), B=600)
    assert r["applicable"] is True
    assert abs(r["主口径"]["R"] - 1.0) < 0.05, r["主口径"]
    assert r["判定"]["gap"] is False, r["判定"]
    assert r["票量校准"]["ok"] is True


def test_biased_draw_is_detected():
    """② 真偏置（m=0.85）必须检出。"""
    r = ticket_ratio_test("双色球", frame=_frame(m1=0.85), B=600)
    assert r["主口径"]["R"] == pytest.approx(0.85, abs=0.05)
    assert r["判定"]["gap"] is True, r["判定"]
    assert r["判定"]["ci95"][1] < 1.0


def test_direction_is_cold_side():
    """② 方向：偏置必须表现为 R<1（开出的组合被买得更少），而不是 R>1。"""
    cold = ticket_ratio_test("双色球", frame=_frame(m1=0.85, seed=7), B=400)
    hot = ticket_ratio_test("双色球", frame=_frame(m1=1.15, seed=7), B=400)
    assert cold["主口径"]["R"] < 1.0 < hot["主口径"]["R"]
    assert cold["判定"]["gap"] is True
    assert hot["判定"]["gap"] is False


# ------------------------------------------------------------ ③ 票量前提

def test_sales_basis_marked_unusable_when_calibration_fails():
    """③ 追加投注式记账（销售额/2 高估票量 13.24%）：销售额口径必须 usable=False。"""
    r = ticket_ratio_test("大乐透", frame=_det_frame("大乐透", sales_mult=1.1324,
                                                    m=0.98), B=200)
    cal = r["票量校准"]
    assert cal["factor"] == pytest.approx(1.1324, abs=1e-6)
    assert cal["ok"] is False
    assert r["对照口径"]["usable"] is False, "票量前提不成立却标 usable=True"
    # 销售额口径的 R 恰好多乘了 1/factor → 假偏置
    assert r["对照口径"]["R"] == pytest.approx(r["主口径"]["R"] / 1.1324, rel=1e-6)
    # 而主口径（免票量）不受影响
    assert r["主口径"]["R"] == pytest.approx(0.98, abs=1e-6)
    assert r["判定"]["gap"] is False, "0.98 未达最小效应量，不应判缝隙"


def test_calibration_ok_for_flat_price_lottery():
    """③ 反例：平价彩种（双色球，2 元/注）校准必须通过。"""
    cal = ticket_calibration(_frame(m1=1.0), B=400)
    assert cal["ok"] is True
    assert cal["factor"] == pytest.approx(1.0, abs=0.01)
    assert cal["推定追加率"] == 0.0


def test_addon_rate_recovered_from_factor():
    """③ 追加率反解：factor = 1 + f/2 ⇒ f = 2(factor-1)。"""
    cal = ticket_calibration(_det_frame("大乐透", sales_mult=1.15), B=200)
    assert cal["推定追加率"] == pytest.approx(0.30, abs=1e-6)
    assert cal["ok"] is False


# ------------------------------------------------------------ ④ 免票量免疫

def test_calib_basis_immune_to_ticket_scale():
    """④ 把整体票量（销售额）乘一个常数，免票量口径 R 必须不动。"""
    a = ticket_ratio_test("双色球", frame=_frame(m1=0.9, sales_mult=1.0, seed=11), B=300)
    b = ticket_ratio_test("双色球", frame=_frame(m1=0.9, sales_mult=1.37, seed=11), B=300)
    assert a["主口径"]["R"] == pytest.approx(b["主口径"]["R"], abs=1e-9)
    # 而销售额口径会被整体拉偏
    assert b["对照口径"]["R"] == pytest.approx(a["对照口径"]["R"] / 1.37, rel=0.01)


# ------------------------------------------------------------ ⑤ 适用范围

@pytest.mark.parametrize("lot", ["排列3", "排列5", "福彩3D", "七星彩"])
def test_non_lotto_not_applicable(lot):
    """⑤ 数字型/七星彩没有一等/二等注数列 → 必须显式不适用，不硬凑。"""
    r = ticket_ratio_test(lot)
    assert r["applicable"] is False
    assert "note" in r and r["note"]


# ------------------------------------------------------------ ⑥ 效应量门控

def test_min_effect_gate_blocks_tiny_effect():
    """⑥ R=0.985 严格小于 1（无噪声 ⇒ CI 排除 1），但未达 3% 门槛 → 不判缝隙。"""
    r = ticket_ratio_test("双色球", frame=_det_frame(m=0.985), B=200)
    assert r["主口径"]["R"] == pytest.approx(0.985, abs=1e-3)
    assert r["主口径"]["ci95"][1] < 1.0, "无噪声下 CI 应排除 1（否则门控测的不是效应量）"
    assert r["判定"]["gap"] is False
    assert "最小效应量" in r["判定"]["note"], r["判定"]["note"]


def test_min_effect_gate_can_be_lowered():
    """⑥ 门槛是显式参数：调低后同一份数据即被判为检出 —— 证明门控在起作用。"""
    f = _det_frame(m=0.985)
    strict = ticket_ratio_test("双色球", frame=f, B=200, min_effect=0.03)
    loose = ticket_ratio_test("双色球", frame=f, B=200, min_effect=0.001)
    assert strict["判定"]["gap"] is False
    assert loose["判定"]["gap"] is True, loose["判定"]


def test_threshold_is_inclusive_of_ci_upper_bound():
    """⑥ R 恰为 1：CI 上界 = 1，不满足严格小于 1 → 不判缝隙（边界不许含混）。"""
    r = ticket_ratio_test("双色球", frame=_det_frame(m=1.0), B=200)
    assert r["主口径"]["R"] == pytest.approx(1.0, abs=1e-3)
    assert r["判定"]["gap"] is False


# ------------------------------------------------------------ ⑦ 趋势诊断

def test_drift_becomes_a_number_not_an_eyeball():
    """⑦ 强趋势：Spearman ρ 与早/晚端点对比都应给出并指向同一方向。"""
    r = ticket_ratio_test("双色球", frame=_frame(trend=0.20, n=900, spacing=3), B=600)
    tr = r["逐年趋势"]
    assert tr["n年"] >= 6
    assert tr["趋势_Spearman_rho"] == pytest.approx(-1.0, abs=0.25)
    assert tr["趋势_p"] < 0.05
    assert tr["差_近期减早期"] < 0
    assert tr["差_z"] < -2.0
    assert tr["早期"]["R"] > tr["近期"]["R"]
    assert r["判定"]["警告"], "有显著趋势却没有任何警告"


def test_flat_data_reports_no_trend():
    """⑦ 反例：无趋势数据不得报出**幅度上**的漂移（避免把噪声读成机制变化）。"""
    r = ticket_ratio_test("双色球", frame=_frame(m1=1.0, n=900, spacing=3), B=600)
    tr = r["逐年趋势"]
    assert tr["趋势_p"] is not None
    assert abs(tr["差_z"]) < 3.0, "无趋势数据却给出 >3σ 的端点差"
    # Spearman 在无趋势下 p 均匀（5% 概率 p<0.05），故只要求：真出现 p<0.05 时
    # 端点差必须仍然不显著（这正是"线索级"的判据），不能升级成结论。
    if tr["趋势_p"] < 0.05:
        assert abs(tr["差_z"]) < 2.0


# ------------------------------------------------------------ 结构自检

def test_zero_winner_period_does_not_blow_up():
    """y2=0 的期（必然 y1=0）不应触发除零，也不该把 autocorr 弄成 NaN。"""
    fr = _frame(m1=0.9, n=300)
    y1, y2 = fr.y1.copy(), fr.y2.copy()
    y1[::50] = 0.0
    y2[::50] = 0.0
    sparse = DrawFrame(lottery=fr.lottery, issues=fr.issues, dates=fr.dates,
                       reds=fr.reds, prev=fr.prev, y1=y1, y2=y2, sales=fr.sales,
                       dropped=0)
    r = ticket_ratio_test("双色球", frame=sparse, B=200)
    assert r["applicable"] is True
    assert r["主口径"]["R"] == pytest.approx(0.9, abs=0.06)
    assert all(v is None or v == v for v in r["r_t自相关"].values())


def test_result_is_json_serializable():
    """整份结果必须可 JSON 落盘（看板/缓存都靠它）。

    用 `allow_nan=False`：**默认的 allow_nan=True 等于没检查** —— 它会让 NaN
    悄悄写成 `NaN`，而那不是合法 JSON 字面量，浏览器 `JSON.parse` / `jq` /
    严格解析器全部会失败。这条从「能 dumps」升级为「dumps 出来的东西是合法 JSON」。
    """
    import json
    r = ticket_ratio_test("双色球", frame=_frame(m1=0.9), B=200)
    json.dumps(r, ensure_ascii=False, default=str, allow_nan=False)


# ------------------------------------------------- 分段在 y2 全零时必须降级

def _sparse(fr, zero_half=True, keep_y1=False):
    """把前半个样本期的 y2 全部置 0（模拟"该段无人中二等奖"）。"""
    h = len(fr.y2) // 2
    y1, y2 = fr.y1.copy(), fr.y2.copy()
    y2[:h] = 0.0
    if not keep_y1:
        y1[:h] = 0.0
    return DrawFrame(lottery=fr.lottery, issues=fr.issues, dates=fr.dates,
                     reds=fr.reds, prev=fr.prev, y1=y1, y2=y2, sales=fr.sales,
                     dropped=0)


def test_seg_all_zero_half_is_none_and_blocks_verdict():
    """契约：某半段 y2 全为 0 ⇒ 该段分母为 0 ⇒ 分段**不可用**，且绝不判缝隙。

    ⚠️ 这测的是「整段全零」，不是「零星为零」——后者由
    `test_zero_winner_period_does_not_blow_up` 覆盖（`y2[::50]=0`）。
    """
    r = ticket_ratio_test("双色球", frame=_sparse(_frame(n=300)), B=200)
    assert r["分段"]["前半"] is None
    assert r["分段不可用"] == ["前半"]
    assert r["判定"]["gap"] is False
    # 「算不出来」不得被说成「不一致」——两者是不同断言，混淆会误导读者
    assert "不一致（" not in r["判定"]["note"], r["判定"]["note"]


def test_seg_unavailable_note_is_distinguished_from_inconsistent():
    """note 文案必须区分「某段算不出来（数据问题）」与「两段方向不一致（机制不稳）」。

    用 m1=0.85 让前面的 CI / 效应量门都通过，确保能走到分段这一分支。
    """
    r = ticket_ratio_test("双色球", frame=_sparse(_frame(n=300, m1=0.85)), B=200)
    assert r["分段不可用"] == ["前半"]
    assert "分段不可用" in r["判定"]["note"], r["判定"]["note"]


def test_seg_all_zero_half_with_nonzero_y1_is_also_unavailable():
    """分子非零时 numpy 给 inf（不是 nan）—— 同样必须挡住。"""
    r = ticket_ratio_test("双色球", frame=_sparse(_frame(n=300), keep_y1=True), B=200)
    assert r["分段"]["前半"] is None, "inf 没被挡住"
    assert r["分段不可用"] == ["前半"]
    assert r["判定"]["gap"] is False


def test_seg_output_is_strict_json_serializable():
    """最强的一条：稀疏帧的完整结果必须是**合法 JSON**（allow_nan=False 通过）。"""
    import json
    for frame in (_sparse(_frame(n=300)),
                  _sparse(_frame(n=300), keep_y1=True),
                  _frame(m1=0.9, n=300)):
        r = ticket_ratio_test("双色球", frame=frame, B=200)
        json.dumps(r, ensure_ascii=False, default=str, allow_nan=False)


def test_seg_available_when_both_halves_have_denominator():
    """反向锁：防过度降级 —— 正常数据两段都该是 float，且不标不可用。"""
    r = ticket_ratio_test("双色球", frame=_frame(m1=0.9, n=300), B=200)
    assert r["分段不可用"] is None
    for v in r["分段"].values():
        assert isinstance(v, float) and v == v


def test_real_pipeline_a_track_exposes_long_block_se():
    """⑥ 与 B 轨对称：A 轨主口径必须自带长块字段（γ 的 se 回退在生产路径不可达）。"""
    r = ticket_ratio_test("双色球", frame=_frame(m1=0.9, n=300), B=200)
    assert r["主口径"]["se_长块bootstrap"] > 0
    assert r["判定"]["z_用"] == "长块 bootstrap se"
