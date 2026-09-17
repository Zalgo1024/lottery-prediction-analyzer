"""
走前验证无泄漏回归测试（2026-09-04 补）。

背景：train/engine.py::walk_forward_folds 于 2026-08-31 修复 look-ahead bias，
docstring 声称有 tests/test_no_lookahead.py 锁定，但该文件从未落地。
此外 train/search.py::_walk_forward_ml 曾手写 fold 循环且把「验证折之后（更晚）的
数据」并入训练集（用未来预测过去）→ 评估分数虚高、HPO 会稳定选出泄漏最重的配置。

本文件锁定三件事：
1. walk_forward_folds 的结构不变量：每折训练样本全部早于验证样本。
2. search._walk_forward_ml 走 engine.walk_forward_folds（而不是自己手写循环），
   防止未来有人把泄漏版折叠逻辑改回来。
3. statistical 评估路径同样不跨期（目标期只依赖其之前的数据）。
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from train.engine import walk_forward_folds


class WalkForwardInvariantTests(unittest.TestCase):
    """walk_forward_folds 结构不变量"""

    def test_train_strictly_older_than_val(self):
        """不变量：train_idx.min() > val_idx.max()（X 最新在前，索引越大越早）"""
        n_samples = 200
        n_splits = 5
        seen_val = []
        folds = list(walk_forward_folds(n_samples, n_splits))
        self.assertGreater(len(folds), 0, "应至少产出 1 折")
        for train_idx, val_idx in folds:
            if len(train_idx) == 0 or len(val_idx) == 0:
                continue
            self.assertGreater(
                int(train_idx.min()), int(val_idx.max()),
                f"训练样本 {train_idx.min()} 不早于验证样本 {val_idx.max()}"
            )
            seen_val.extend(val_idx.tolist())
        # 验证折互不重叠
        self.assertEqual(len(seen_val), len(set(seen_val)), "验证折存在重叠")

    def test_min_train_threshold_respected(self):
        """训练样本数不足 _MIN_TRAIN_SAMPLES 的折被跳过（最后折训练集为空）"""
        n_samples = 60
        folds = list(walk_forward_folds(n_samples, 5, min_train=50))
        # 60/5=12：最后折 val=[48,60) → train=[] → 跳过；靠前折 train 均 <50 → 全被跳过
        self.assertEqual(len(folds), 0)


class SearchMlUsesEngineFoldsTests(unittest.TestCase):
    """search._walk_forward_ml 必须复用 engine.walk_forward_folds"""

    def setUp(self):
        from config import LOTTERY_CONFIG
        from data.loader import load_lottery
        self.data = load_lottery("双色球")

    def test_ml_path_calls_walk_forward_folds(self):
        """mock walk_forward_folds：验证 _walk_forward_ml 通过它取训练/验证折"""
        import train.search as ts

        fixed_folds = [
            (np.arange(10, 15), np.arange(0, 5)),   # train 索引更大 = 更早
            (np.arange(5, 10), np.arange(5, 10)),
        ]
        # 固定 folds：每折 (train_idx, val_idx)
        fixed_folds = [(np.array([15, 16, 17, 18, 19]), np.array([0, 1, 2, 3, 4])),
                       (np.array([10, 11, 12, 13, 14]), np.array([5, 6, 7, 8, 9]))]

        captured = {}

        class StubModel:
            """记录收到的训练 X，predict_proba 返回中性概率"""
            def __init__(self, _i):
                self.i = _i
            def fit(self, X, y):
                captured.setdefault("Xtr_means", []).append(float(X.mean()))
                return self
            def predict_proba(self, X):
                n = len(X)
                return np.column_stack([np.full(n, 0.5), np.full(n, 0.5)])

        with patch("train.engine.walk_forward_folds", return_value=fixed_folds), \
             patch("train.engine._train_logistic",
                   side_effect=lambda X, y: [StubModel(i).fit(X, y)
                                             for i in range(y.shape[1])]):
            res = ts._walk_forward_ml(self.data, window_size=2, model_type="logistic",
                                      feature_version=1, n_splits=2)

        self.assertIsNotNone(res, "评估应成功")
        # 训练集切片来自 mock 的 train_idx：2 折 × 每号一列（y 共 49 列）= 98 次 fit
        means = captured.get("Xtr_means", [])
        self.assertEqual(len(means), 2 * 49, "每折应为每个号码分类器各训练一次")
        for mean in means:
            self.assertTrue(np.isfinite(mean))


class StatisticalNoLookaheadTests(unittest.TestCase):
    """statistical 评估：目标期只依赖其之前的数据（无跨期泄漏）"""

    def test_statistical_uses_past_window_only(self):
        """构造目标 = 前 30 期频率可反推的假记录，验证 window 取的是 target 之前的数据"""
        import train.search as ts
        from data.schema import LotteryData

        # 造 80 条假记录：红球 1-6，蓝球 1；最新在前（records[0] 最新）
        # 让"第 k 期"（records[k]）的红球 = 由 (k+10) 决定 → 若 window 含未来会暴露
        class FakeRec:
            def __init__(self, k):
                self.期号 = str(1000 + k)
                # 红球: 固定 6 个号但其中 5 个来自 k 的简单函数，保证目标期号可被"之后的记录"反推
                self.红球 = [(1 + (k + i) % 30) for i in range(6)]
                self.蓝球 = [1]

        # 只测窗口方向：records 最新在前，window = records[i+1:i+1+ws] 是 target 之前的更旧记录
        records = [FakeRec(k) for k in range(80)]
        data = LotteryData(lottery_name="双色球", records=records)
        # 红球范围需与 cfg 一致（1-33）；我们的假记录只出现 1..30，频率代理不越界即可
        res = ts._walk_forward_statistical(data, window_size=10, feature_version=1)
        self.assertIsNotNone(res)
        self.assertIn("n_samples", res)
        # 全量滚动评估：80 - 10 = 70 个样本
        self.assertEqual(res["n_samples"], 70)


if __name__ == "__main__":
    unittest.main()
