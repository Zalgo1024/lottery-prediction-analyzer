"""
P3-1 覆盖设计测试（combo/covering.py）

诚实口径（本项目特色）：
- 覆盖设计的价值 = **确定性地板 / t-子集覆盖率**，不是期望增益——
  同票数下每票命中概率只由票数决定（均匀开奖），随机与覆盖的期望相同。
  因此验收指标：满覆盖时任意开奖 ≥1 票命中 ≥t 红（穷举/大样本验证）；
  部分预算时覆盖率高于随机同注数。
- 小规模穷举锚点 v=9,k=4,t=3（C(9,3)=84 三元组，每票盖 C(4,3)=4，LB=21）：
  全覆盖后对全部 C(9,4)=126 种开奖逐一验证（随机 9 票必存在失败开奖）。
"""
import itertools
import random

from combo.covering import covering_tickets, plan


def _hits_ge(tickets, draw, t):
    return sum(1 for tk in tickets if len(set(draw) & set(tk)) >= t)


class TestSmallExhaustive:
    V, K, T = 9, 4, 3

    def test_full_cover_exhaustive_all_draws(self):
        """v9k4t3 满覆盖：全部 C(9,4)=126 种开奖都 ≥1 票命中 ≥3"""
        r = covering_tickets(9, 4, 3)
        assert r["满覆盖"] and r["覆盖率"] == 1.0
        tickets = r["tickets"]
        assert len(tickets) <= 3 * 21          # 质量不劣于 LB 的 3 倍
        for draw in itertools.combinations(range(9), 4):
            assert _hits_ge(tickets, draw, 3) >= 1, f"开奖 {draw} 未覆盖"

    def test_random_same_budget_has_failing_draws(self):
        """对照组：随机 9 票存在未命中 ≥3 的开奖（衬托确定性保证）
        单票命中概率=0.167，9 票对某开奖全失败≈19%，随机必输。"""
        rng = random.Random(0)
        for _ in range(5):
            tickets = [tuple(sorted(rng.sample(range(9), 4))) for _ in range(9)]
            fails = sum(1 for draw in itertools.combinations(range(9), 4)
                        if _hits_ge(tickets, draw, 3) == 0)
            assert fails > 0, f"随机 9 票竟 126 种开奖全中（fails={fails}）"


class TestLotteryReal:
    def test_ssq_red_t2_full_cover_mc(self):
        """双色球红 (33,6,2) 满覆盖：随机 2000 期开奖每期 ≥1 票 ≥2 红"""
        r = plan("双色球", t=2, mc_draws=2000, seed=1)
        assert r["满覆盖"], r
        assert r["覆盖率"] == 1.0
        assert r["MC验证"]["每期≥t红票数-最差"] >= 1

    def test_coverage_beats_random_at_same_budget(self):
        """部分预算 150 注（33,6,3）：覆盖三元组占比高于随机同注数"""
        r = covering_tickets(33, 6, 3, budget=150, seed=3)
        assert not r["满覆盖"]
        f_cov = r["覆盖率"]
        rng = random.Random(4)
        rand_covered = set()
        for _ in range(150):
            tk = tuple(sorted(rng.sample(range(33), 6)))
            rand_covered |= set(itertools.combinations(tk, 3))
        f_rand = len(rand_covered) / 5456
        assert f_cov > f_rand + 0.05, (f_cov, f_rand)

    def test_dlt_red_supported(self):
        """大乐透红 (35,5,2) 快覆盖可跑（t=3 全覆盖 ~1100 注较慢，测试用 t=2）"""
        r = plan("大乐透", t=2, mc_draws=300, seed=5)
        assert r["满覆盖"]

    def test_digital_not_applicable(self):
        """数字型无组合覆盖语义 → 诚实不适用"""
        r = plan("排列3", t=3)
        assert not r["applicable"] and "不适用" in r["reason"]

    def test_deterministic_same_seed(self):
        a = covering_tickets(33, 6, 2, budget=40, seed=9)
        b = covering_tickets(33, 6, 2, budget=40, seed=9)
        assert a["tickets"] == b["tickets"]
        assert a["覆盖率"] == b["覆盖率"]
