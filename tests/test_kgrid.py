# -*- coding: utf-8 -*-
"""tests/test_kgrid.py —— 五档 K 网格的编排层（任务 1「单位预算验证」）。

锁的是**编排**，不是数值结论：
① 格的形状：预算 = K×单价、评估窗是后 40%、常量只有 `run_cell` 认的那三个键；
② 基线是**副本军团**：`random` 自己的 Δ 必须落在副本间极差内（否则说明基线换了人）；
③ 层 1 不越界：达标率 ≤ `min(1,K·p) + 3·SE`，且 `≥三等` 的空间近似为 0；
④ 规模效应可核对：随机臂达标率 vs 理论 `1−(1−p)^K`，偏离 ≤ 2·SE；
⑤ `prod_mix` 必须标 cap 污染且不进主检验；
⑥ BH 的族规模必须与预注册的 20 一致，不一致要显式标出而不是静默。

★ 测试用极小的 MC 采样与 K=10：本文件验的是「编排对不对」，
  不是「五档 K 的结论」，跑真数由 `scripts/oneoff/_kgrid_run.py` 负责。
"""
from __future__ import annotations

import json
import math
import re

import numpy as np
import pytest

from ev import preregistration as PR
from ev.arena import kgrid as KG


def _const(**kw):
    c = dict(PR.constants())
    c.update({"候选池规模": 2000, "MC拟合采样数": 2000, "MC评估采样数": 2000})
    c.update(kw)
    return c


@pytest.fixture(scope="module")
def cell():
    return KG.run_kgrid_cell("双色球", 10, const=_const(), replicas=2,
                             arms=("random", "union_greedy", "prod_mix"),
                             verbose=False)


# ---------------------------------------------------------------- ① 形状
def test_cell_shape(cell):
    assert cell["彩种"] == "双色球" and cell["K"] == 10
    assert cell["预算元每期"] == 20                     # K × 2 元（单价真源）
    n_all = cell["期数"]
    n_eval = cell["评估窗"]["期数"]
    assert n_eval == n_all - int(math.floor(n_all * KG.DEV_FRACTION))
    assert cell["开发窗"]["期数"] + n_eval == n_all
    assert cell["评估窗"]["止"] == n_all - 1
    # 三个门都在（主终点 + 两个描述门）
    for name in ("random", "union_greedy"):
        assert set(cell["臂"][name]) >= set(KG.DEFAULT_LABELS)


def test_cell_of_constants_are_only_the_three_run_keys():
    """`run_cell` 逐项对齐实跑常量 ⇒ 格常量多一个键就会在开跑前抛。"""
    c = KG.cell_of("大乐透", 100)
    assert set(c["常量"]) == {"期窗", "规则口径", "剔除规则版本"}
    assert c["预算元每期"] == 200
    assert c["常量"]["规则口径"] == "periodic"


def test_cell_of_names_the_gate():
    assert KG.cell_of("双色球", 50)["名"] == "双色球-K50-≥10元"
    c = KG.cell_of("大乐透", 38, target={"kind": "grade", "grade": "四等"})
    assert c["名"] == "大乐透-K38-≥四等"


# ---------------------------------------------------------------- ② 基线
def test_random_arm_delta_is_within_replica_spread(cell):
    """`random` 就是副本 0 ⇒ 它与副本均值的差不可能超出副本间极差太多。"""
    base = cell["随机基线"]
    assert base["副本数"] == 2
    a = cell["臂"]["random"]["≥10元"]["全窗"]
    assert abs(a["覆盖效应Δ"]) <= base["门"]["≥10元"]["副本间极差"] + 1e-9
    assert a["基线副本SD"] == pytest.approx(base["门"]["≥10元"]["副本SD"])


def test_replicas_have_distinct_seeds(cell):
    seeds = cell["随机基线"]["副本seed"]
    assert len(seeds) == 2 and len(set(seeds)) == 2


# ---------------------------------------------------------------- ③④ 数值自洽
def test_level1_never_violated(cell):
    assert cell["层1否决"] is False
    for name, a in cell["臂"].items():
        for lb in KG.DEFAULT_LABELS:
            row = a[lb]["评估窗"]
            cap = row["理论上限Kp"]
            assert row["达标率"] <= cap + 3 * row["抽样SE"] + 1e-12, (name, lb)
            assert row["可争取空间"] >= -1e-12, (name, lb)


def test_third_prize_gate_has_no_space(cell):
    """≥三等 单注 p≈1e-5 ⇒ 空间是 O((K·p)²) 的 1e-7 量级，读不出任何东西。"""
    assert cell["臂"]["random"]["≥三等"]["全窗"]["可争取空间"] < 1e-5


def test_size_effect_matches_theory(cell):
    """随机臂的全窗达标率必须与 `1-(1-p)^K` 相容 —— 否则池/cap 口径有问题。"""
    row = cell["臂"]["random"]["≥10元"]["全窗"]
    K, p = cell["K"], cell["单注达标概率"]
    theory = 1.0 - (1.0 - p) ** K
    se = row["抽样SE"]
    assert abs(row["基线达标率"] - theory) <= 2 * se + 1e-12
    # 随机基线（副本均值）也要与理论相容
    assert abs(cell["随机基线"]["门"]["≥10元"]["副本均值"] - theory) <= 2 * se + 1e-12


def test_decomposition_fields_exist(cell):
    row = cell["臂"]["union_greedy"]["≥10元"]["评估窗"]
    for k in ("覆盖效应Δ", "相对提升", "可争取空间", "归一化η", "规模效应_绝对",
              "规模效应_相对纯随机", "抽样SE", "★单期样本"):
        assert k in row, k
    n_eval = cell["评估窗"]["期数"]
    assert row["★单期样本"] == n_eval


# ---------------------------------------------------------------- ⑤ cap 污染
def test_prod_mix_is_flagged_and_out_of_main(cell):
    a = cell["臂"].get("prod_mix")
    if a is None:
        pytest.skip("prod_mix 在本机拒绝出票（NotImplementedError），本条不适用")
    assert a["进主检验"] is False
    assert a["★cap污染"] is True, "生产臂内部 cap=4 ≠ 实验 cap=3，必须标污染"


def test_main_arms_are_declared():
    assert set(KG.MAIN_ARMS) == {"tier_packing", "union_greedy"}
    assert "random" not in KG.MAIN_ARMS and "prod_mix" not in KG.MAIN_ARMS


# ---------------------------------------------------------------- ⑥ BH
def test_bh_family_size_is_declared_and_checked(cell):
    out = KG.bh_across_cells([cell], label="≥10元", m_expected=20)
    # 只有 union_greedy 进主检验 ⇒ 族规模 1，与预注册 20 不符必须**显式标出**
    assert out["族规模"] == 1
    assert out["族规模_预注册"] == 20
    assert out["★族规模不符"] is True
    assert out["明细"][0]["臂"] == "union_greedy"


def test_bh_single_arm_equals_raw_p(cell):
    out = KG.bh_across_cells([cell], label="≥10元")
    row = out["明细"][0]
    assert row["校正后p"] == pytest.approx(row["原始p"])    # m=1 ⇒ BH 不改变 p


def test_bh_handles_empty_family():
    out = KG.bh_across_cells([{"降级": "无可回放期"}], label="≥10元")
    assert out["族规模"] == 0 and "说明" in out


# ---------------------------------------------------------------- 报告表
def test_report_block_renders_three_tables(cell):
    """三张表必须**分开**：规模效应是口径体检、覆盖效应才是结论、附加门是描述。"""
    from ev.arena import report as RP
    md = "\n".join(RP.kgrid_block([cell], label="≥10元"))
    assert "表 1 · 规模效应" in md
    assert "表 2 · 覆盖效应" in md
    assert "表 3 · 附加门" in md
    # 每张表都有表头分隔行，且不含会破坏朴素切分的转义竖线
    assert "|---|---|" in md
    assert "\\|" not in md
    # 表格里必须同时出现 K 与 Δ ——「Δ 与 K 同读」的纪律靠这一列存在
    rows = [ln for ln in md.splitlines() if ln.startswith("| 双色球 | 10 |")]
    assert rows, "表 2 缺少本格的行"
    assert any("union_greedy" in ln for ln in rows)


def test_report_table4_carries_per_100_yuan_yield(cell):
    """表 4 必须能直接回答「这笔预算换到几次命中、多少钱」。

    用户原话要的是「每 100 元投入的命中期数和奖金」——缺这两列，表 4 只回答了
    「净亏多少」，没法做单位预算的比较。同时锁住两个「≥10元」的分离说明，
    防止有人把金额侧的 `≥10元期占比`（随 K 机械上升）当覆盖证据贴回去。
    """
    from ev.arena import report as RP
    md = "\n".join(RP.kgrid_block([cell], label="≥10元"))
    assert "表 4 · 成本与实得" in md
    assert "每100元命中期数" in md and "每100元奖金元" in md
    assert "中奖期数" in md
    # 口径分离的声明必须**写在报告里**，不能只活在注释里
    assert "两个「≥10元」不是同一个量" in md
    assert "只能用表 2" in md


# ------------------------------------------------- 任务 3：按门单独优化的矩阵
def test_tier_opt_matrix_reads_only_diagonal(cell):
    """矩阵必须同时给出「为该门优化」与「为别的门优化在它上面的顺带结果」。

    只报主终点 Δ 的写法会把「误配的代价」整个藏起来 —— 这条测试锁住：
    ① 每个门都有列；② 每个优化目标都有行；③ 判决只按对角线；
    ④ 低于实益门槛时**明写**不接生产选号，不许含糊成「趋势向好」。
    """
    from ev.arena import report as RP

    c = json.loads(json.dumps(cell))          # 深拷贝，避免污染 fixture
    c["优化目标门"] = "≥四等"
    md = "\n".join(RP.tier_opt_matrix_block([c], labels=KG.DEFAULT_LABELS,
                                            lotteries=("双色球",),
                                            arms=("union_greedy",)))
    for lb in KG.DEFAULT_LABELS:
        assert f"Δ {lb}" in md, f"矩阵缺 {lb} 列"
    assert "| ≥四等 | union_greedy |" in md, "优化目标行缺失"
    # 结论必须按对角线判，且门槛写明
    assert f"{RP.TIER_OPT_MIN_GAIN * 100:.1f}pp" in md
    has_gain = re.search(r"至少一条 Δ ≥ [\d.]+pp", md)
    no_gain = "保留为研究结论，不接生产选号" in md
    assert has_gain or no_gain, "结论既没判有收益也没判不接生产 —— 那是含糊话"
    # 诚实边界必须在表里，不是只在注释里
    assert "不改变任何一注的中奖概率" in md


def test_tier_opt_matrix_marks_missing_gate_and_no_break():
    """某格缺门时必须打「—」，不能 KeyError；朴素切分也不许出现转义竖线。"""
    from ev.arena import report as RP

    empty_cell = {"格": "x", "彩种": "双色球", "K": 10, "优化目标门": "≥三等",
                  "臂": {"union_greedy": {"≥三等": {"评估窗": {
                      "达标率": 0.0, "覆盖效应Δ": -0.001, "可争取空间": 1e-5,
                      "归一化η": None, "基线达标率": 0.0}}}}}
    md = "\n".join(RP.tier_opt_matrix_block([empty_cell], lotteries=("双色球",),
                                            arms=("union_greedy",)))
    assert "| — |" in md or md.count("—") >= 2
    assert "\\|" not in md


def test_tier_opt_concludes_per_gate_not_per_lottery():
    """★★ 结论必须**逐门**判：`≥10元` 过了门槛，不能替 `≥四等` 作证。

    这是一个真实踩过的 bug：早期实现把「对角线上有没有任何一条 Δ 过关」按**彩种**
    汇总，于是「为四等奖优化」的实验因为 ≥10元 的旧结果而被判「该门有实测收益」。
    门是用户问题的单位，不是彩种。
    """
    from ev.arena import report as RP

    def mk(gate: str, delta: float, space: float, rate: float, base: float):
        return {"格": f"x-{gate}", "彩种": "双色球", "K": 50, "优化目标门": gate,
                "臂": {"union_greedy": {gate: {
                    "评估窗": {"达标率": rate, "基线达标率": base,
                               "覆盖效应Δ": delta, "可争取空间": space,
                               "归一化η": delta / space if space > 1e-12 else None},
                    "配对": {"判决": "可判"}}}}}

    cells = [mk("≥10元", 0.0500, 0.0726, 0.39, 0.34),      # 过关
             mk("≥四等", 0.0009, 0.0002, 0.019, 0.018),    # 不过关
             mk("≥三等", 0.0001, 0.0000, 0.001, 0.001)]    # 不过关
    md = "\n".join(RP.tier_opt_matrix_block(
        cells, lotteries=("双色球",), arms=("union_greedy",)))

    assert "达实益门槛" in md
    row10 = next(ln for ln in md.splitlines() if ln.startswith("| ≥10元 | +"))
    row4 = next(ln for ln in md.splitlines() if ln.startswith("| ≥四等 | +"))
    assert row10.rstrip().endswith("| 是 |"), row10
    assert row4.rstrip().endswith("| 否 |"), row4
    # 结论句必须点名「过了门槛的是哪个门」，而不是笼统说「该彩种有收益」
    assert "对角线达实益门槛的门 = ≥10元" in md
    assert "对角线达实益门槛的门 = ≥四等" not in md
