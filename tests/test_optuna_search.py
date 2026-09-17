"""
P0-2 Optuna 诚实化搜索回归测试（2026-09-04 补）。

锁定三件事：
1. _bh_fdr：BH 阶梯正确（q_(i) = min_{k>=i} (m/k·p_(k))，cap 1，倒序保序）。
2. search_best_config_optuna 在「合成评估器」下的 verdict：
   - 真有强配置时 → BH-FDR 后仍显著 → 判"显著优于随机"；
   - 全部试次评估失败时 → 优雅返回 best=None，不崩（不含"宣称显著"）。
3. 所有试次都带 selection_p / selection_q / params 落到结果里（供人工查阅）。
"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from train.search import _bh_fdr


class BhFdrTests(unittest.TestCase):
    def test_bh_ladder_example(self):
        """教科书例子：p=[0.01,0.02,0.20,0.30], m=4 → q=[0.04,0.04,0.2667,0.30]"""
        qs = _bh_fdr([0.01, 0.02, 0.20, 0.30])
        expected = [0.04, 0.04, 0.2666666667, 0.30]
        for got, exp in zip(qs, expected):
            self.assertAlmostEqual(got, exp, places=6)

    def test_bh_unsorted_input_preserves_order(self):
        """输入乱序时 q 与输入同序"""
        p = [0.30, 0.01, 0.20, 0.02]
        qs = _bh_fdr(p)
        # 按 p 排序对应的 q 必须单调不减（BH 阶梯性质）
        order = np.argsort(p)
        sorted_q = [qs[i] for i in order]
        for a, b in zip(sorted_q, sorted_q[1:]):
            self.assertLessEqual(a, b + 1e-12)
        # 最小的 p 校正后 q <= m * p / 1
        self.assertAlmostEqual(qs[1], 0.04, places=6)
        for q in qs:
            self.assertLessEqual(q, 1.0 + 1e-12)

    def test_bh_caps_at_one_and_empty(self):
        self.assertEqual(_bh_fdr([]), [])
        qs = _bh_fdr([0.9, 0.95])
        # BH 阶梯（倒序保序）：q2=p2=0.95；q1=min(0.9*2/1, q2)=0.95 → 单调且不超 1
        self.assertAlmostEqual(qs[0], 0.95, places=12)
        self.assertAlmostEqual(qs[1], 0.95, places=12)
        # 单元素：q = p
        self.assertAlmostEqual(_bh_fdr([0.03])[0], 0.03, places=12)


class OptunaVerdictTests(unittest.TestCase):
    """用 mock 掉 _evaluate_config 的合成评估器验证 verdict 逻辑"""

    def setUp(self):
        # search_best_config_optuna 内部会 random.seed/np.random.seed 全局播种，
        # 必须保存/恢复进程级 RNG 状态，避免污染其他测试（顺序敏感，2026-09-04 踩过：
        # 未恢复时令 test_dedup 的 set 迭代序用例稳定误报）。
        import random
        self._rand_state = random.getstate()
        self._np_state = np.random.get_state()

    def tearDown(self):
        import random
        random.setstate(self._rand_state)
        np.random.set_state(self._np_state)

    def _patch_path(self):
        """把结果文件写到临时目录，避免污染项目 training/"""
        tmp = tempfile.mkdtemp(prefix="optuna_test_")
        return patch(
            "train.search._optuna_path",
            return_value=Path(tmp) / "search_best_config_test_optuna.json",
        )

    def test_verdict_significant_when_strong_config_exists(self):
        """合成评估器：logistic 稳定最强且 p 极小 → BH-FDR 后仍显著 → 可宣称"""
        import train.search as ts

        def fake_eval(data, w, mt, fv, hp=None):
            # logistic 给最强分数 + 极小 p（真信号）；其余模型平庸
            if mt == "logistic":
                score = 0.9 - w * 1e-4
                p, adv = 0.001, 0.35
            else:
                score = 0.5 - w * 1e-4
                p, adv = 0.5, 0.0
            return {
                "combined_score": score,
                "hit_rate": 0.2,
                "calibration_error": 0.05,
                "n_samples": 200,
                "selection_advantage": adv,
                "selection_p": p,
            }

        with self._patch_path(), \
             patch("train.search._evaluate_config", side_effect=fake_eval):
            out = ts.search_best_config_optuna("双色球", n_trials=12, seed=1)

        self.assertIsNotNone(out.get("best"))
        self.assertEqual(out["best"]["params"]["model_type"], "logistic")
        q = out["best"].get("selection_q")
        self.assertIsNotNone(q)
        self.assertLess(q, 0.05, "真信号经 BH-FDR 后应仍显著")
        self.assertIn("显著优于随机", out.get("verdict", ""))
        # 每个试次都应带 p/q 可查
        self.assertGreaterEqual(len(out["trials"]), 1)
        self.assertIn("selection_q", out["trials"][0])

    def test_all_failed_trials_return_gracefully(self):
        """评估器全部失败（TrialPruned）→ 不崩、不宣称显著"""
        import train.search as ts

        with self._patch_path(), \
             patch("train.search._evaluate_config", return_value=None):
            out = ts.search_best_config_optuna("双色球", n_trials=6, seed=2)

        self.assertIsNone(out.get("best"))
        self.assertEqual(out.get("n_trials_completed"), 0)
        self.assertIn("无完成试次", out.get("verdict", ""))

    def test_no_significance_verdict_when_p_values_high(self):
        """所有配置都不显著（p 大）→ 即便 score 有高低，verdict 必须"无显著优势\""""
        import train.search as ts

        def fake_eval(data, w, mt, fv, hp=None):
            # 故意让某配置 score 最高但 p 仍平庸（赢家诅咒场景）
            score = 0.7 if mt == "random_forest" else 0.5
            return {
                "combined_score": score,
                "hit_rate": 0.2,
                "calibration_error": 0.05,
                "n_samples": 200,
                "selection_advantage": 0.02,
                "selection_p": 0.45,
            }

        with self._patch_path(), \
             patch("train.search._evaluate_config", side_effect=fake_eval):
            out = ts.search_best_config_optuna("双色球", n_trials=12, seed=3)

        best = out.get("best")
        self.assertIsNotNone(best)
        # 最优试次 q >= 0.05 → 不得宣称显著
        self.assertIn("无显著优势", out.get("verdict", ""))


if __name__ == "__main__":
    unittest.main()
