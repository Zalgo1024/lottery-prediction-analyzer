"""tests/test_gap_select.py —— P4 缝隙内选号（核心是 γ=0 红线）

★ 本文件最重要的一条是**逐位等价**：
  γ=0（含不可折算 / 未过显著门 / 取不到）时，倾斜链路必须与不倾斜**逐位一致**。
  这是 P4 唯一的伦理红线——管道可以先铺好，但在证据到位之前它必须什么都不做。

其余覆盖：倾斜方向（γ>0 ⇒ 冷门乘数大）、模型未 Go 显式降级、
数字型 sampler 的阈值在 γ=0 时不动、以及**文案黑名单**（不许暗示"更准/更可能"）。
"""
import numpy as np
import pytest

from ev import sampler, selection
from ev.mechanism import gap_select as GS


# ------------------------------------------------------------------ 夹具
def _mk_cands(m: int = 60, seed: int = 5):
    rng = np.random.default_rng(seed)
    out = []
    for _ in range(m):
        reds = sorted(int(x) for x in rng.choice(np.arange(1, 34), size=6,
                                                 replace=False))
        out.append({"红球": reds, "蓝球": int(rng.integers(1, 17))})
    return out


def _patch(monkeypatch):
    """把双色球向量化路径的外部依赖全部换成确定性的假实现。

    注意：`_select_redblue_vectorized` 是**函数内 import**，所以 patch 源模块
    （ev.payout / ev.popularity）的属性就是正确做法。
    """
    import ev.payout as pay
    import ev.popularity as pop

    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": True, "beta": np.full(9, 0.1),
                                     "m_": np.zeros(9), "s_": np.ones(9)})
    # μ 与红球和值相关 ⇒ eff1 有差异 ⇒ 排序真有信息（否则测不出倾斜）
    monkeypatch.setattr(pop, "batch_mu",
                        lambda lot, R, N, p, params: 1.0 + 0.05 * np.asarray(R).sum(axis=1))
    monkeypatch.setattr(pay, "_resolve_issue", lambda lot, issue: "2026001")
    monkeypatch.setattr(pay, "_grade_pool", lambda lot, issue, g: (1e8, 4e7))
    monkeypatch.setattr(pay, "grade_probabilities", lambda lot: {"一等": 5.6e-8})
    monkeypatch.setattr(pay, "pool_share_expected_batch",
                        lambda pool, mus: 1e7 / (1.0 + np.asarray(mus)))
    monkeypatch.setattr(pay, "payout_eff",
                        lambda lot, t, issue=None: {"净EV": -1.0, "奖级明细": [],
                                                    "分薄": {"乘数": 1.0}})


def _nums(r):
    return [x["号码"] for x in r["选中"]]


# ------------------------------------------------------------------ ① ★红线：γ=0 逐位等价
def test_zero_gamma_tilt_matches_no_tilt_bitwise(monkeypatch):
    """★ 硬红线：γ=0 时倾斜链路必须与不倾斜**逐位相同**（同 seed、同候选池）。"""
    cands = _mk_cands()
    _patch(monkeypatch)

    a = selection.select_tickets("双色球", n=5, candidates=cands, seed=1,
                                 mechanism_tilt=False)
    b = selection.select_tickets("双色球", n=5, candidates=cands, seed=1,
                                 mechanism_tilt=True, gamma=0.0)

    assert a["选中数"] == b["选中数"] == 5
    assert _nums(a) == _nums(b), "γ=0 却改变了出号 —— 红线破了"
    assert [x["一等奖实得"] for x in a["选中"]] == [x["一等奖实得"] for x in b["选中"]]
    assert b["机制倾斜"]["倾斜"] is False


def test_default_gamma_path_also_hits_the_red_line(monkeypatch):
    """★ 不显式传 γ（走 `ev.gap.mechanism_gamma` 红线）时同样逐位等价。"""
    monkeypatch.setattr("ev.gap.mechanism_gamma", lambda lot, **k: 0.0)
    cands = _mk_cands()
    _patch(monkeypatch)

    a = selection.select_tickets("双色球", n=5, candidates=cands, seed=2,
                                 mechanism_tilt=False)
    b = selection.select_tickets("双色球", n=5, candidates=cands, seed=2,
                                 mechanism_tilt=True)      # gamma=None → 走红线
    assert _nums(a) == _nums(b)


def test_resolve_gamma_failure_falls_back_to_zero(monkeypatch):
    """★ 取 γ 抛异常 ⇒ 0.0 ⇒ 不倾斜（不许把异常当成"可以倾斜"）。"""
    def boom(lot, **k):
        raise RuntimeError("模拟测量失败")
    monkeypatch.setattr("ev.gap.mechanism_gamma", boom)
    assert GS.resolve_gamma("双色球") == 0.0
    assert GS.mechanism_multiplier("双色球", np.zeros((3, 6), dtype=np.int16)) is None


def test_select_in_gap_returns_the_same_object_at_zero_gamma():
    """★ γ=0 时 `key` **就是传入的 base 对象本身**，不是乘 1.0 的新数组。

    为什么连乘 1.0 都不许：并列极多（110 万组合只有 21 万不同分数），
    argsort 在完全并列时靠稳定性，1 ULP 的差就可能换顺序。
    """
    base = np.array([3.0, 1.0, 2.0, 5.0])
    r = GS.select_in_gap("双色球", None, base, gamma=0.0)
    assert r["key"] is base
    assert r["倾斜"] is False


# ------------------------------------------------------------------ ② 倾斜方向与量纲
def test_multiplier_is_larger_for_cold_combinations(monkeypatch):
    """γ>0 = 开奖偏冷门 ⇒ 冷门（ss 小）乘数必须更大。"""
    import ev.popularity as pop
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": True, "beta": np.ones(9),
                                     "m_": np.zeros(9), "s_": np.ones(9)})
    # ss 直接给：[-1(冷), 0, 1, 2(热)]
    monkeypatch.setattr(pop, "_score_std",
                        lambda F, m_, s_, beta: np.array([-1.0, 0.0, 1.0, 2.0]))
    w = GS.mechanism_multiplier("双色球", np.zeros((4, 6), dtype=np.int16), gamma=1.0)
    assert w is not None
    assert w[0] > w[1] > w[2] > w[3], "方向反了：γ>0 应偏向冷门"


def test_multiplier_is_normalized_to_mean_one(monkeypatch):
    """乘数均值归一到 1 ⇒ 不污染「一等奖实得」等量纲相关的展示字段。"""
    import ev.popularity as pop
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": True, "beta": np.ones(9),
                                     "m_": np.zeros(9), "s_": np.ones(9)})
    rng = np.random.default_rng(0)
    monkeypatch.setattr(pop, "_score_std",
                        lambda F, m_, s_, beta: rng.normal(size=len(F)))
    w = GS.mechanism_multiplier("双色球", np.zeros((500, 6), dtype=np.int16), gamma=0.8)
    assert w.mean() == pytest.approx(1.0, abs=1e-9)


def test_negative_gamma_reverses_direction(monkeypatch):
    """γ<0（若将来测出开奖偏热门）自动反向，无需改代码。"""
    import ev.popularity as pop
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": True, "beta": np.ones(9),
                                     "m_": np.zeros(9), "s_": np.ones(9)})
    monkeypatch.setattr(pop, "_score_std",
                        lambda F, m_, s_, beta: np.array([-1.0, 0.0, 1.0, 2.0]))
    w = GS.mechanism_multiplier("双色球", np.zeros((4, 6), dtype=np.int16), gamma=-1.0)
    assert w[0] < w[1] < w[2] < w[3], "γ<0 时应偏向热门"


def test_nonzero_gamma_changes_the_selection(monkeypatch):
    """γ≠0 时链路必须**真的生效**（否则整条管道是死代码，测试却全绿）。"""
    cands = _mk_cands()
    _patch(monkeypatch)
    a = selection.select_tickets("双色球", n=5, candidates=cands, seed=3,
                                 mechanism_tilt=False)
    b = selection.select_tickets("双色球", n=5, candidates=cands, seed=3,
                                 mechanism_tilt=True, gamma=3.0)
    assert b["机制倾斜"]["倾斜"] is True
    assert _nums(a) != _nums(b), "γ=3 却没改变出号 ⇒ 倾斜链路是死代码"


def test_display_field_is_not_polluted_by_the_multiplier(monkeypatch):
    """★ 「一等奖实得」必须回到**未倾斜**的 eff1，不能被机制乘数污染。"""
    cands = _mk_cands()
    _patch(monkeypatch)
    a = selection.select_tickets("双色球", n=5, candidates=cands, seed=4,
                                 mechanism_tilt=False)
    b = selection.select_tickets("双色球", n=5, candidates=cands, seed=4,
                                 mechanism_tilt=True, gamma=2.5)
    # 逐注：只要号码相同（有可能），实得必须一致；号码不同则各自实得仍在 eff1 取值域内
    m = {tuple(sorted(x["号码"]["红球"])): x["一等奖实得"] for x in a["选中"]}
    for row in b["选中"]:
        k = tuple(sorted(row["号码"]["红球"]))
        if k in m:
            assert row["一等奖实得"] == pytest.approx(m[k], rel=1e-9)


# ------------------------------------------------------------------ ③ 显式降级
def test_model_not_go_returns_none(monkeypatch):
    """模型未 Go → 无 z 标尺 → 返回 None（不许拿 1 顶替，见「不许静默降级」纪律）。"""
    import ev.popularity as pop
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": False, "report": {}})
    assert GS.mechanism_multiplier("大乐透", np.zeros((4, 6), dtype=np.int16),
                                   gamma=1.0) is None
    r = GS.select_in_gap("大乐透", np.zeros((4, 6), dtype=np.int16),
                         np.array([1.0, 2.0, 3.0, 4.0]), gamma=1.0)
    assert r["倾斜"] is False
    assert "模型未 Go" in r["口径"]


def test_select_in_gap_reports_degradation_not_silence(monkeypatch):
    """γ≠0 但拿不到打分 ⇒ 显式降级 + 说明，绝不用别的口径顶替。"""
    import ev.popularity as pop
    monkeypatch.setattr(pop, "get_popularity_model",
                        lambda lot: {"go": False, "report": {}})
    base = np.array([1.0, 2.0, 3.0])
    r = GS.select_in_gap("双色球", None, base, gamma=1.5)
    assert r["倾斜"] is False
    assert r["gamma"] == pytest.approx(1.5), "降级时必须保留原始 γ，别假装它是 0"
    assert "不许静默降级" in r["说明"] or "显式回退" in r["说明"]


# ------------------------------------------------------------------ ④ 数字型 sampler
def test_crowd_gate_default_at_zero_gamma():
    """γ=0 / γ<0 / None / nan ⇒ 阈值恒为 0.5（sampler 行为逐位不变）。"""
    assert GS.crowd_gate(0.0) == GS.CROWD_GATE_DEFAULT == 0.5
    assert GS.crowd_gate(None) == 0.5
    assert GS.crowd_gate(-1.0) == 0.5          # γ<0 不许把阈值往上抬
    assert GS.crowd_gate(float("nan")) == 0.5


def test_crowd_gate_shifts_down_and_has_a_floor():
    """γ>0 → 阈值下移（更冷），且有下限 0.10（不许压到近乎全拒）。"""
    assert GS.crowd_gate(1.0) == pytest.approx(0.4)
    assert GS.crowd_gate(2.0) == pytest.approx(0.3)
    assert GS.crowd_gate(100.0) == pytest.approx(0.10)


class _FakeCrowd:
    def crowding_quantile(self, cand):
        return 0.30


def test_sampler_identical_at_zero_gamma(monkeypatch):
    """★ 数字型 sampler：γ=0 ⇒ 与不倾斜逐位一致。"""
    monkeypatch.setattr(sampler, "_crowd", lambda name: _FakeCrowd())
    a = sampler.sample_tickets("七星彩", n=3, seed=7, mechanism_tilt=False)
    b = sampler.sample_tickets("七星彩", n=3, seed=7, mechanism_tilt=True, gamma=0.0)
    assert [r["号码"] for r in a["号码组"]] == [r["号码"] for r in b["号码组"]]
    assert b["反大众阈值"] == pytest.approx(0.5)


def test_sampler_gate_actually_moves_for_positive_gamma(monkeypatch):
    """γ 足够大 ⇒ 阈值下移 ⇒ 0.30 的组合被拒 ⇒ 结果走补充分支（证明链路非死代码）。"""
    monkeypatch.setattr(sampler, "_crowd", lambda name: _FakeCrowd())
    r = sampler.sample_tickets("七星彩", n=3, seed=7, mechanism_tilt=True, gamma=6.0)
    assert r["反大众阈值"] == pytest.approx(0.10)
    assert r["机制倾斜"], "阈值变了却没在返回里说明"


def test_sampler_only_tilts_qxc(monkeypatch):
    """其它数字型（排列3/5、福彩3D）无判定轨可用 ⇒ 阈值恒 0.5。"""
    monkeypatch.setattr(sampler, "_crowd", lambda name: _FakeCrowd())
    r = sampler.sample_tickets("排列5", n=3, seed=7, mechanism_tilt=True, gamma=6.0)
    assert r["反大众阈值"] == pytest.approx(0.5)
    assert r["机制倾斜"] is None


# ------------------------------------------------------------------ ⑤ 文案黑名单
_BANNED_WORDS = ("更可能", "更准", "提高命中", "预测号码", "提高中奖")


@pytest.mark.parametrize("fn", [GS.tilt_state])
def test_zero_gamma_copy_is_worded_honestly(fn):
    """★ γ=0 时的说明不得暗示「更准 / 更可能 / 提高命中」。"""
    r = fn("双色球", gamma=0.0)
    blob = "".join(str(v) for v in r.values())
    for w in _BANNED_WORDS:
        assert w not in blob, f"γ=0 的文案里出现了「{w}」——那是钢印方向的话术"
    assert r["倾斜"] is False


def test_zero_gamma_copy_does_not_claim_fairness_either():
    """诚实是双向的：γ=0 也不许说成「已证明开奖公平」。"""
    blob = "".join(str(v) for v in GS.tilt_state("双色球", gamma=0.0).values())
    assert "不是" in blob and "公平" in blob
    assert "已证明" not in blob


def test_select_in_gap_zero_copy_is_honest():
    r = GS.select_in_gap("双色球", None, np.array([1.0]), gamma=0.0)
    blob = r["口径"] + r["说明"]
    for w in _BANNED_WORDS:
        assert w not in blob
    assert "不表示开奖已证明公平" in r["说明"]


# ------------------------------------------------------------------ ⑥ 状态与接线
def test_tilt_state_active_when_gamma_significant():
    r = GS.tilt_state("双色球", gamma=0.5)
    assert r["倾斜"] is True and r["可用"] is True
    assert r["方向"] == "偏向冷门"
    assert r["γ"] == pytest.approx(0.5)


def test_tilt_state_negative_gamma_labels_hot():
    r = GS.tilt_state("双色球", gamma=-0.5)
    assert r["倾斜"] is True
    assert r["方向"] == "偏向热门"


def test_tilt_state_respects_significance_flag():
    """★ γ 非零但**未过显著门** ⇒ 必须按 γ=0 处理（否则绕过 γ=0 红线）。

    端到端实测撞到过：快照里 γ=-0.0185 且显著=False，只判 `γ != 0` 就报了
    倾斜=True —— 出号权重被动了，而测量其实并不支持。
    """
    r = GS.tilt_state("双色球", gamma=-0.0185, significant=False)
    assert r["倾斜"] is False, "未过显著门却判激活 —— 红线被绕过"
    assert r["γ"] == 0.0
    assert "显著门" in r["原因"]


def test_tilt_state_activates_when_significant():
    r = GS.tilt_state("双色球", gamma=-0.0185, significant=True)
    assert r["倾斜"] is True
    assert r["方向"] == "偏向热门"


def test_snapshot_exposes_redline_filtered_gamma(monkeypatch):
    """快照必须给出「过红线后的 γ」，让消费方不必自己判显著位。"""
    from ev.mechanism import snapshot as SN
    monkeypatch.setattr("ev.mechanism.gap_cache.load", lambda *a, **k: {
        "applicable": True, "n": 100, "sig": "x",
        "裁决": {}, "track结果": {},
        "gamma": {"gamma_合并": {"gamma": -0.0185, "显著": False}},
        "B过程": {}, "_cached_at": "t"})
    s = SN.mechanism_snapshot("双色球")
    assert s["γ"] == pytest.approx(-0.0185)      # 原始值照实报（观众要看）
    assert s["γ显著"] is False
    assert s["γ_可倾斜"] == 0.0, "不显著却给出非零可倾斜 γ —— 会绕过红线"


def test_select_tickets_signature_has_tilt_params():
    import inspect
    p = inspect.signature(selection.select_tickets).parameters
    assert "mechanism_tilt" in p and "gamma" in p
    assert p["mechanism_tilt"].default is True     # 默认开启；γ=0 时自动退化


def test_selection_result_carries_tilt_state(monkeypatch):
    """返回里必须带「机制倾斜」，供 P5 的 pending 快照消费。"""
    _patch(monkeypatch)
    r = selection.select_tickets("双色球", n=3, candidates=_mk_cands(), seed=9,
                                 gamma=0.0)
    assert "机制倾斜" in r
    assert r["机制倾斜"]["倾斜"] is False


def test_mechanism_text_marks_tilt_when_active(monkeypatch):
    """倾斜真的生效时，「机制」文案必须写明（不许悄悄改权重不告诉人）。"""
    _patch(monkeypatch)
    r = selection.select_tickets("双色球", n=3, candidates=_mk_cands(), seed=9,
                                 gamma=3.0)
    assert "P4 机制倾斜已激活" in r["机制"]
