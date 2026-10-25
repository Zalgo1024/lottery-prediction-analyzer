# -*- coding: utf-8 -*-
"""tests/test_popularity_proxy.py —— 拥挤度**代理**层的契约测试。

锁住四件事（都是这块最容易被人"好心改坏"的地方）：
  1. 支持范围：首版只有双色球，大乐透必须**显式降级**，不许静默回退到启发式；
  2. 不可用时返回全 NaN，而不是 0 分 / 中位数 —— 那会把"没数据"伪装成"中等拥挤"；
  3. 打分只有一条路径（薄封装 popularity.drawn_quantiles），不许另开一套；
  4. walk-forward 的 β 只能用切点**之前**的数据拟合；拟合失败的块分位必须是 None
     （用相邻块的 β 顶替 = 前向泄漏）。
"""
import math
import numpy as np
import pytest

from ev import popularity_proxy as pp


def _tickets(n=8):
    """8 张合法双色球票（红 6 + 蓝 1）。"""
    out = []
    for i in range(n):
        r = sorted(np.random.default_rng(i).choice(33, 6, replace=False) + 1)
        out.append({"红球": [int(x) for x in r], "蓝球": [int(i % 16) + 1]})
    return out


# ------------------------------------------------------------ ① 支持范围

def test_supported_means_modeled_not_usable():
    """「建过模」与「可用」是**两档**，不许合并成一个 `SUPPORTED`。

    大乐透是反例：建过模（2,932 期完整拟合）但没过 Go 门槛。
    若把它留在「不在支持范围 ⇒ 从未建模」那一档，原因文案会变成
    「没做过」—— 而真相是「做了没通过」，两者对外可信度完全不同。
    """
    assert pp.is_supported("双色球") is True
    assert pp.is_supported("大乐透") is True        # 建过模
    assert pp.is_supported("七星彩") is False       # 从未建模
    assert pp.SUPPORTED == ("双色球", "大乐透")
    # ★ 关键反向断言：建过模 ⇒ 不等于可用
    assert pp.go_or_none("大乐透") is False


def test_unsupported_lottery_degrades_explicitly():
    """大乐透 popularity 未达 Go 门槛 ⇒ 必须显式降级（False），不许静默放行。"""
    assert pp.go_or_none("大乐透") is False


@pytest.mark.statsmodels
def test_go_evidence_exposes_measured_numbers():
    """原因文案引用的数字必须**现取**，不许硬编码。

    硬编码的「CV 仅 2/5 折」会随数据增长静默过期，而读者看不出来 ——
    这条测试要求 `go_evidence` 真的把实测字段交出去。
    """
    ev = pp.go_evidence("大乐透")
    assert ev["applicable"] is True
    assert int(ev["n"]) >= pp.MIN_TRAIN            # 样本远超门槛 ⇒ 阻塞不在数据量
    assert 0 <= int(ev["cv_better_folds"]) < 3     # 未达 ≥3/5 门槛
    assert ev["cv_full"] and ev["cv_null"]         # 两种偏差都得在，供对账
    assert isinstance(ev["lrt_p"], float)
    # 从未建模的彩种必须走另一档文案
    ev2 = pp.go_evidence("七星彩")
    assert ev2["applicable"] is False
    assert "从未建模" in ev2["verdict"]


@pytest.mark.statsmodels
def test_go_verdict_driven_by_significance_not_sign_count():
    """闸门 Go/No-Go 必须看**显著性折数**(cv_sig_folds)，不是纯符号计数(cv_better_folds)。

    纯符号计数（只要 full 平均偏差低 1e-9 就算「更优」）会被少数离群期支配而虚高。
    这条哨兵锁住：go 由 cv_sig_folds（逐折单侧 Wilcoxon p<0.05 的折数）决定，
    且双色球应达门槛(≥3)、大乐透应未达(<3)；另附全局池化随机化 p 同向佐证。
    """
    for lot, expect_go in (("双色球", True), ("大乐透", False)):
        m = pp.go_or_none(lot)
        ev = pp.go_evidence(lot)
        assert "cv_sig_folds" in ev, "cv_sig_folds 字段被移除 = 显著性口径丢失"
        n_folds = len(ev["cv_fold_wilcoxon_p"])
        need = math.ceil(n_folds * 0.6)
        # go 决策必须等于「LRT 显著 且 显著折数 ≥ 门槛」—— 锁死用显著性而非符号
        assert m == (ev["lrt_p"] < 0.01 and ev["cv_sig_folds"] >= need), \
            f"{lot}: go 未由显著性折数驱动"
        if expect_go:
            assert ev["cv_sig_folds"] >= need, f"{lot} 应达 Go 门槛"
            assert ev["cv_global_perm_p"] is not None and ev["cv_global_perm_p"] < 0.05, \
                f"{lot} 全局随机化应显著"
        else:
            assert ev["cv_sig_folds"] < need, f"{lot} 应未达 Go 门槛"
            assert ev["cv_global_perm_p"] is not None and ev["cv_global_perm_p"] > 0.05, \
                f"{lot} 全局随机化不应显著"


def test_go_or_none_false_when_model_read_fails(monkeypatch):
    """读模型抛异常时按不可用处理，不能当作 '可用但 β=0' 继续打分。"""
    import ev.popularity as pop

    def _boom(lottery):
        raise RuntimeError("model cache missing")

    monkeypatch.setattr(pop, "get_popularity_model", _boom)
    assert pp.go_or_none("双色球") is False


# ------------------------------------------------------------ ② 不可用 = 全 NaN

def test_crowd_quantiles_nan_for_unsupported_lottery():
    u = pp.crowd_quantiles("大乐透", _tickets())
    assert u.shape == (8,)
    assert np.isnan(u).all(), "不可用必须返回 NaN，不能返回 0 分（那等于说'最冷'）"


def test_crowd_quantiles_nan_when_model_not_go(monkeypatch):
    monkeypatch.setattr(pp, "go_or_none", lambda lottery: False)
    u = pp.crowd_quantiles("双色球", _tickets())
    assert np.isnan(u).all()


def test_crowd_quantiles_empty_pool_is_empty():
    assert len(pp.crowd_quantiles("双色球", [])) == 0


# ------------------------------------------------------------ ③ 单一打分路径

def test_crowd_quantiles_delegates_to_popularity(monkeypatch):
    """必须薄封装 drawn_quantiles：另开一套打分 = 两条口径各算一遍（历史坑：差 1 ULP）。"""
    import ev.popularity as pop

    seen = {}

    def _fake(lottery, R, beta=None, mu_=None, sd_=None, persist=True):
        seen["lottery"] = lottery
        seen["R"] = np.asarray(R)
        seen["kwargs"] = (beta, mu_, sd_, persist)
        return np.linspace(0.0, 1.0, len(R))

    monkeypatch.setattr(pop, "drawn_quantiles", _fake)
    monkeypatch.setattr(pp, "go_or_none", lambda lottery: True)

    tks = _tickets(4)
    u = pp.crowd_quantiles("双色球", tks, persist=False)
    assert seen["lottery"] == "双色球"
    assert seen["R"].shape == (4, 6)
    assert seen["kwargs"] == (None, None, None, False)
    assert np.allclose(u, [0.0, 1 / 3, 2 / 3, 1.0])


def test_reds_matrix_reads_main_zone_only():
    """拥挤度只按主区（红球）打分；数字型票用「号码」键也要能读出来。"""
    R = pp._reds_matrix([{"红球": [1, 2, 3, 4, 5, 6], "蓝球": [7]},
                         {"号码": [7, 8, 9, 10, 11, 12]}])
    assert R.shape == (2, 6)
    assert R[0].tolist() == [1, 2, 3, 4, 5, 6]
    assert R[1].tolist() == [7, 8, 9, 10, 11, 12]


# ------------------------------------------------------------ ④ walk-forward 不得泄漏未来

def _fake_rows(n=1300):
    rng = np.random.default_rng(0)
    return {
        "reds": rng.integers(1, 34, size=(n, 6)).astype(np.int16),
        "prev": rng.integers(1, 34, size=(n, 6)).astype(np.int16),
        "y": rng.integers(0, 20, size=n).astype(np.float64),
        "N": np.full(n, 4e8),
        "issues": [str(26000 + i) for i in range(n)],
    }


def test_walk_forward_beta_only_uses_past_rows(monkeypatch):
    """切点 c 处只能用 [0, c) 拟合 —— 多给一行就是前向泄漏。"""
    import ev.popularity as pop

    calls = []

    def _fake_fit(reds, prev, y, N, fit_null=False):
        calls.append(len(y))
        return {"beta": np.zeros(9), "m_": np.zeros(9), "s_": np.ones(9)}

    monkeypatch.setattr(pop, "fit_beta", _fake_fit)
    blocks = pp.walk_forward_beta("双色球", rows=_fake_rows(1300), min_train=1200, step=50)

    assert [b["切点"] for b in blocks] == [1200, 1250]
    assert calls == [1200, 1250], "拟合用的行数必须严格等于切点"
    assert blocks[0]["止"] == 1250 and blocks[-1]["止"] == 1300
    assert blocks[0]["期号"] == [str(26000 + i) for i in range(1200, 1250)]


def test_walk_forward_beta_empty_when_history_too_short():
    assert pp.walk_forward_beta("双色球", rows=_fake_rows(500), min_train=1200, step=50) == []


def test_walk_forward_quantiles_none_on_fit_failure(monkeypatch):
    """拟合失败的块分位必须是 None —— 拿相邻块的 β 顶替等于前向泄漏。"""
    import ev.popularity as pop

    def _boom(*a, **k):
        raise RuntimeError("singular matrix")

    monkeypatch.setattr(pop, "fit_beta", _boom)
    out = pp.walk_forward_quantiles("双色球", _tickets(3),
                                    rows=_fake_rows(1300), min_train=1200, step=50)
    assert out and all(b["分位"] is None for b in out)


def test_walk_forward_quantiles_uses_block_params(monkeypatch):
    import ev.popularity as pop

    def _fake_fit(reds, prev, y, N, fit_null=False):
        return {"beta": np.zeros(9), "m_": np.zeros(9), "s_": np.ones(9)}

    monkeypatch.setattr(pop, "fit_beta", _fake_fit)
    seen = {}

    def _fake_q(lottery, tickets, beta=None, mu_=None, sd_=None, persist=True):
        seen.setdefault("persist", persist)
        seen.setdefault("beta_seen", beta is not None)
        return np.full(len(tickets), 0.25)

    monkeypatch.setattr(pp, "crowd_quantiles", _fake_q)
    out = pp.walk_forward_quantiles("双色球", _tickets(3),
                                    rows=_fake_rows(1300), min_train=1200, step=50)
    assert all(b["分位"].shape == (3,) for b in out)
    assert seen["persist"] is False          # walk-forward 绝不落盘覆盖生产模型
    assert seen["beta_seen"] is True


# ------------------------------------------------------------ ⑤ 声明单一真源

def test_caveat_is_single_source_and_honest():
    assert pp.CROWD_PROXY_CAVEAT
    note = pp.proxy_note()
    assert note["循环性声明"] == pp.CROWD_PROXY_CAVEAT
    assert note["支持范围"] == ["双色球", "大乐透"]
    assert note["walk_forward"]["最小训练期数"] == pp.MIN_TRAIN
    # 反向黑名单：不许说成「已证明开奖公平」那一侧
    assert "已证明" not in pp.CROWD_PROXY_CAVEAT
    assert "公平" not in pp.CROWD_PROXY_CAVEAT
    assert "不可预测" not in pp.CROWD_PROXY_CAVEAT


def test_caveat_appears_in_selector_meta():
    """选号器的元数据必须带上循环性声明 —— 否则页面上只剩一个『拥挤度』裸数字。"""
    from ev import lowcrowd_select as ls

    pool = _tickets(20)
    u = np.linspace(0.0, 1.0, 20)
    _, meta = ls.select_lowcrowd("双色球", pool, 5, u)
    assert meta["循环性声明"] == pp.CROWD_PROXY_CAVEAT
    assert meta["分位口径"] == pp.QUANTILE_CALIBER
