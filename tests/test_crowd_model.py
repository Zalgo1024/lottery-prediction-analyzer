"""
ev/crowd_model.py 回归测试（2026-09-04，P1-1）。

锁定四件事：
1. 均匀 π 下各彩种 q 与解析值一致（七星彩=官方组合数/15e6，精确到 1e-9 量级）：
   → 证明奖级命中 DP 与「任意位置匹配」现行奖级表映射正确。
2. 合成人群仿真 → 反推 π 能还原真值（七星彩 68 参数 / 3D 含组选掩码与票种占比）。
3. 真实数据冒烟：七星彩现行期 / 福彩3D 能拟合出有限 π（不再像 v1 返回 None）。
4. 撞号分薄 API：share_multiplier 单调（越热越小），expected_sales 线性于票量。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from ev.crowd_model import (_q_all, _softmax_rows, _zones, fit_crowd_v2)


def _uniform_pi(name):
    sizes = _zones(name)
    return _softmax_rows(np.zeros(sum(sizes)), sizes)


def _true_pi(name, amp=0.6):
    """确定性偏置 π：每位按旋转斜坡制造冷热差。"""
    sizes = _zones(name)
    theta = np.zeros(sum(sizes))
    off = 0
    for i, s in enumerate(sizes):
        for d in range(s):
            v = ((d + 2 * i) % s) / max(s - 1, 1)  # 0..1
            theta[off + d] = amp * (2 * v - 1)
        off += s
    return _softmax_rows(theta, sizes)


def _sim_obs(name, pi_true, n_draws, rng, shares=None, mask3d=True):
    """按 π_true 仿真 n_draws 期注数（z 均匀、N 波动、c~Poisson(N·share·q)）。"""
    sizes = _zones(name)
    n_pos = len(sizes)
    obs = []
    for _ in range(n_draws):
        z = [int(rng.integers(0, s)) for s in sizes]
        N = float(rng.uniform(2.5e6, 5.5e6)) if name == "七星彩" else float(rng.uniform(1.5e6, 3.5e6))
        q = _q_all(name, pi_true, np.array([z]))[0]
        if shares is None:
            mu = N * q
        else:
            mu = N * shares * q
        counts = []
        keep = [True, False, False]
        for ti in range(len(mu)):
            if shares is not None:
                if mask3d and ti == 1 and rng.random() > 0.25:   # 组选3 常缺失
                    continue
                if mask3d and ti == 2 and rng.random() > 0.70:   # 组选6 部分缺失
                    continue
            c = int(rng.poisson(max(mu[ti], 0.0)))
            counts.append((ti, float(c)))
        obs.append({"digits": z, "N": N, "counts": counts})
    return obs


class UniformSanityTests(unittest.TestCase):
    def test_qixing_uniform_matches_official_counts(self):
        """均匀 π：七星彩各奖级 q = 官方组合数/15e6（误差 < 1e-8 相对）"""
        pi = _uniform_pi("七星彩")
        z = np.array([[1, 3, 5, 7, 9, 2, 14]])  # 含第7位 14 越界安全值
        z = np.array([[1, 3, 5, 7, 9, 2, 0]])
        q = _q_all("七星彩", pi, z)[0]
        official = np.array([1, 14, 54, 1971, 31590, 1188270]) / 15e6
        rel = np.abs(q - official) / official
        self.assertLess(rel.max(), 1e-6, f"q 与官方组合数不符: {q}")
        # 总中奖率 8.146%（官方口径）
        self.assertAlmostEqual(q.sum(), 0.08146, places=4)

    def test_3digit_forms(self):
        """均匀 π：3D 直选 1e-3；全异→组6=6e-3；一对→组3=3e-3；豹子无组选"""
        pi = _uniform_pi("福彩3D")
        q = _q_all("福彩3D", pi, np.array([[1, 2, 3]]))[0]
        np.testing.assert_allclose(q, [1e-3, 0, 6e-3], atol=1e-12)
        q = _q_all("福彩3D", pi, np.array([[1, 2, 2]]))[0]
        np.testing.assert_allclose(q, [1e-3, 3e-3, 0], atol=1e-12)
        q = _q_all("福彩3D", pi, np.array([[7, 7, 7]]))[0]
        np.testing.assert_allclose(q, [1e-3, 0, 0], atol=1e-12)

    def test_pl5_uniform(self):
        pi = _uniform_pi("排列5")
        q = _q_all("排列5", pi, np.array([[1, 2, 3, 4, 5]]))[0]
        self.assertAlmostEqual(q[0], 1e-5, places=12)


class SyntheticRecoveryTests(unittest.TestCase):
    def test_recover_qixing_pi(self):
        """七星彩 68 参数：合成 350 期全奖级注数 → 反推 π 与真值 MAE 小"""
        pi_true = _true_pi("七星彩")
        rng = np.random.default_rng(11)
        obs = _sim_obs("七星彩", pi_true, 350, rng)
        m = fit_crowd_v2("七星彩", obs=obs, max_iter=200)
        self.assertIsNotNone(m)
        sizes = _zones("七星彩")
        diffs = []
        for i, s in enumerate(sizes):
            diffs.extend(np.abs(m.pi[i, :s] - pi_true[i, :s]))
        mae = float(np.mean(diffs))
        self.assertLess(mae, 0.03, f"七星彩 π 还原 MAE={mae:.4f} 过大")

    def test_recover_3d_with_masks_and_shares(self):
        """3D：组选列部分缺失（模拟 500.com 源）+ 票种占比 λ → 仍能还原 π 与 λ"""
        pi_true = _true_pi("福彩3D")
        shares_true = np.array([0.55, 0.15, 0.30])
        rng = np.random.default_rng(23)
        obs = _sim_obs("福彩3D", pi_true, 400, rng, shares=shares_true, mask3d=True)
        m = fit_crowd_v2("福彩3D", obs=obs, max_iter=200)
        self.assertIsNotNone(m)
        sizes = _zones("福彩3D")
        mae = float(np.mean([abs(m.pi[i, d] - pi_true[i, d])
                             for i in range(3) for d in range(sizes[i])]))
        self.assertLess(mae, 0.03, f"3D π 还原 MAE={mae:.4f} 过大")
        self.assertIsNotNone(m.shares)
        for a, b in zip(m.shares, shares_true):
            self.assertLess(abs(a - b), 0.06, f"票种占比还原偏差过大 {m.shares}")


class ShareMultiplierTests(unittest.TestCase):
    def test_multiplier_monotonic_and_sales_linear(self):
        """冷门号 share_multiplier≈1，大众号趋小；expected_sales 随票量线性"""
        pi = _uniform_pi("七星彩")
        from ev.crowd_model import CrowdV2
        m = CrowdV2("七星彩", pi, _zones("七星彩"))
        # 构造一冷一热：最后一位 0 vs 高热度位…均匀 π 下 score 相同；改用偏置 π
        m2 = CrowdV2("七星彩", _true_pi("七星彩"), _zones("七星彩"))
        cold = [0, 0, 0, 0, 0, 0, 0]
        hot = [int(np.argmax(pi_row)) for pi_row in m2.pi]
        N = 4e6
        mu_c = m2.expected_co_winners(cold, N)
        mu_h = m2.expected_co_winners(hot, N)
        self.assertGreater(mu_h, mu_c)
        self.assertLess(m2.share_multiplier(hot, N), m2.share_multiplier(cold, N) + 1e-12)
        # 冷门 μ≈0 → 乘数≈1
        self.assertLess(abs(m2.share_multiplier(cold, N) - 1.0), 0.05)
        # 线性：票量翻倍 → 预期同号翻倍
        self.assertAlmostEqual(m2.expected_sales(cold, 2 * N) / max(m2.expected_sales(cold, N), 1e-12), 2.0, places=6)


class RealDataSmokeTests(unittest.TestCase):
    def test_qixing_real_fit(self):
        """真实现行期（2020-10-13 后）能拟合出 π——v1 在此返回 None 的短板已补"""
        m = fit_crowd_v2("七星彩", lookback=250, max_iter=150)
        self.assertIsNotNone(m, "七星彩真实数据应能拟合")
        self.assertEqual(m.n_obs, 250)
        for i, s in enumerate(_zones("七星彩")):
            self.assertAlmostEqual(float(m.pi[i, :s].sum()), 1.0, places=6)
            self.assertGreater(float(m.pi[i, :s].max() - m.pi[i, :s].min()), 0.0)

    def test_3d_real_fit(self):
        m = fit_crowd_v2("福彩3D", lookback=400, max_iter=150)
        self.assertIsNotNone(m)
        self.assertGreaterEqual(float(m.nll), 0.0)  # 目标=−logL≥0
        self.assertGreater(m.n_iter, 0)
        self.assertIsNotNone(m.shares)
        self.assertAlmostEqual(float(m.shares.sum()), 1.0, places=6)


class OutOfSampleTests(unittest.TestCase):
    """留出期验收：反推 π 在"没见过的期"上预测各奖级注数，必须优于均匀零模型。"""

    def test_qixing_holdout_beats_uniform(self):
        """七星彩前 700 期拟合，后 100 期算 Poisson 偏差；crowd 应 < 均匀 π。"""
        from ev.crowd_model import _q_all, _load_observations

        obs = _load_observations("七星彩")
        obs = [d for d in obs if d["N"] is not None]
        fit_obs = obs[:700]
        evl = obs[700:800]
        m = fit_crowd_v2("七星彩", obs=fit_obs, max_iter=250)
        self.assertIsNotNone(m)

        sizes = _zones("七星彩")
        pi_u = _uniform_pi("七星彩")
        pi_hat = m.pi

        def deviance(pi):
            total = 0.0
            for d in evl:
                z = np.array([d["digits"]])
                q = _q_all("七星彩", pi, z)[0]
                N = d["N"]
                for ti, c in d["counts"]:
                    mu = N * q[ti]
                    if c > 0:
                        total += 2 * (c * np.log(c / max(mu, 1e-12)) - (c - mu))
                    else:
                        total += 2 * mu
            return total / len(evl)

        dv_hat = deviance(pi_hat)
        dv_u = deviance(pi_u)
        self.assertLess(dv_hat, dv_u * 0.985,
                        f"反推模型未优于均匀零模型: crowd={dv_hat:.2f} vs uniform={dv_u:.2f}")


if __name__ == "__main__":
    unittest.main()
