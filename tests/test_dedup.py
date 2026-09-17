"""
大批量出号（人海战术）去重回归测试（2026-09-01 新增）。

背景：数字型（有序位）彩种的 _g_max_overlap 原为 max(1, int(total_choose*0.6))，
对 3 位彩种 = 1，即「任一位同号即相似」，导致 --groups 300 只产出 ~37 组。

修复（A 方案）：保留相似去重，但数字型阈值下限提到 2（只拦「两两 >= 2 位相同」），
并修好两处缺陷：
  1. 重试只改 zones[:len//2]（3 位彩种只改第 1 位）→ 改为遍历所有分区
  2. 完全重复分支不检查相似就放行 → 两个分支统一检查 _g_similar

本文件锁住的契约：
  1. 数字型：1 位相同可共存；>= 2 位相同被拦（相似去重仍生效）
  2. 数字型大批量出号：产出号码两两最多 1 位相同，且能出满（3D 上限 ~100）
  3. 乐透型：_g_dedup 备用路径不被破坏；_g_similar 语义固化
"""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import LOTTERY_CONFIG
from data.schema import schema_from_cfg
from prediction.engine import _g_dedup, _g_key, _g_max_overlap


def _zones(schema, values):
    """按 schema 分区顺序打包号码：values 为每区号码列表"""
    return {z.name: list(v) for z, v in zip(schema.zones, values)}


class DigitalDedupTests(unittest.TestCase):
    """数字型：相似去重仍生效，但阈值下限为 2"""

    def setUp(self):
        self.schema = schema_from_cfg(LOTTERY_CONFIG["福彩3D"])

    def _key(self, values):
        return _g_key(_zones(self.schema, values), self.schema)

    def test_threshold_is_two_for_3digit(self):
        """3 位彩种阈值 = 2（不是 1），否则人海战术会被压到 ~37 组"""
        self.assertEqual(_g_max_overlap(self.schema), 2)

    def test_one_position_same_is_allowed(self):
        """仅 1 位相同（< 阈值 2）→ 原样通过，不被替换"""
        existing = {self._key([[1], [2], [3]])}
        zones = _zones(self.schema, [[1], [5], [9]])  # 只有第 1 位相同
        ok = _g_dedup(zones, existing, self.schema, _g_max_overlap(self.schema))
        self.assertTrue(ok)
        self.assertEqual([zones[z.name] for z in self.schema.zones],
                         [[1], [5], [9]])

    def test_two_positions_same_is_blocked(self):
        """2 位相同（>= 阈值 2）→ 判相似，必须被替换"""
        existing = {self._key([[1], [2], [3]])}
        zones = _zones(self.schema, [[1], [2], [4]])  # 前两位相同
        original = [zones[z.name] for z in self.schema.zones]
        _g_dedup(zones, existing, self.schema, _g_max_overlap(self.schema))
        new = [zones[z.name] for z in self.schema.zones]
        # 要么放弃（返回 False，号码不变），要么被替换成不相似的号
        if new != original:
            same = sum(1 for a, b in zip(new, [[1], [2], [3]]) if a == b)
            self.assertLess(same, 2, f"相似号未被拦下：{new} 与 (1,2,3) 有 {same} 位相同")

    def test_exact_duplicate_is_replaced(self):
        """完全重复必须被替换成新号"""
        existing = {self._key([[1], [2], [3]])}
        zones = _zones(self.schema, [[1], [2], [3]])
        ok = _g_dedup(zones, existing, self.schema, _g_max_overlap(self.schema))
        if ok:
            self.assertNotEqual([zones[z.name] for z in self.schema.zones],
                                [[1], [2], [3]])


class MassGroupTests(unittest.TestCase):
    """集成：数字型大批量出号（3D 上限 ~100，见 Singleton 界注释）"""

    def _run(self, name, groups, max_same_positions=None):
        from prediction.engine import predict
        with patch("data.feedback.record_pending_prediction"):
            res = predict(name, groups=groups, mode="fresh")
        numbers = res["预测号码"]
        keys = [tuple(tuple(v) for v in t["号码"].values()) for t in numbers]
        self.assertEqual(len(keys), groups, f"{name} 未出满：{len(keys)}/{groups}")
        self.assertEqual(len(set(keys)), len(keys), f"{name} 存在完全重复号码")
        if max_same_positions is not None:
            for i in range(len(keys)):
                for j in range(i + 1, len(keys)):
                    same = sum(1 for a, b in zip(keys[i], keys[j])
                               if a == b)
                    self.assertLessEqual(
                        same, max_same_positions,
                        f"{name} 出现相似号：{keys[i]} vs {keys[j]} 有 {same} 位相同")
        return len(keys)

    def test_3d_mass_groups(self):
        """3D 80 组（< Singleton 界 100）应出满，且两两最多 1 位相同"""
        self._run("福彩3D", 80, max_same_positions=1)

    def test_pl5_mass_groups(self):
        """排列5 100 组：阈值 = max(2, int(5*0.6)) = 3 → 两两最多 2 位相同"""
        self._run("排列5", 100, max_same_positions=2)


class LotteryDedupTests(unittest.TestCase):
    """乐透型：_g_dedup 备用路径不被本次改动破坏（零回归）"""

    def setUp(self):
        self.schema = schema_from_cfg(LOTTERY_CONFIG["双色球"])

    def test_lottery_threshold_unchanged(self):
        """乐透型阈值仍是 4（数字型下限 2 的改动不得波及乐透型）"""
        self.assertEqual(_g_max_overlap(self.schema), 4)

    def test_dedup_and_record_records_real_output(self):
        """乐透型去重：existing 必须记录「真实输出」，而非微调前的幽灵号。

        旧 bug：相似分支微调成功后 reds 已变，但 existing.add 用的还是微调前的旧 key，
        → 后续号码只跟幽灵号比较，真实输出之间可红球重叠 >= 4
        （实测双色球 300 组 102 对，其中 3 对重叠 5）。修复后为 0。
        """
        from prediction.engine import _dedup_and_record
        cfg = LOTTERY_CONFIG["双色球"]
        existing = {((1, 2, 3, 4, 5, 6), (1,))}
        # 与 A 红球重叠 4 → 相似，会触发微调
        reds, blues, valid = _dedup_and_record([1, 2, 3, 4, 7, 8], [1], existing, cfg)
        self.assertTrue(valid)
        overlap = len(set(reds) & {1, 2, 3, 4, 5, 6})
        self.assertLessEqual(overlap, 3, f"输出 {reds} 与 A 红球重叠 {overlap} >= 4")
        # existing 记录的是真实输出，不是微调前的幽灵号
        out_key = (tuple(sorted(reds)), tuple(sorted(blues)))
        self.assertIn(out_key, existing, "existing 未记录真实输出")
        self.assertNotIn(((1, 2, 3, 4, 7, 8), (1,)), existing,
                         "微调前的幽灵号被错误记入 existing")

    def test_exact_duplicate_is_replaced(self):
        """乐透型完全重复仍会被替换成与原有号不同的号码（本路径为备用，双/大实际走 _dedup_and_record）

        注：_g_dedup 对乐透型的相似阈值是 4，重采样若只改蓝球（红球区相同只算 1 个
        分区 < 4）即合法通过，所以断言是「新 key != 原 key」，而非强制红球变化。
        ⚠️ 比较对象必须是**调用前的原 key**，不能用 next(iter(existing))：_g_dedup 成功时
        会把新 key 加入 existing，而 set 迭代顺序随哈希/插入历史变化，指向"任意一个元素"
        会偶发误报（2026-09-04 在 Optuna 测试确定性播种 RNG 后稳定复现）。
        """
        existing = {_g_key(_zones(self.schema, [[1, 2, 3, 4, 5, 6], [1]]), self.schema)}
        original_key = next(iter(existing))
        zones = _zones(self.schema, [[1, 2, 3, 4, 5, 6], [1]])
        ok = _g_dedup(zones, existing, self.schema, _g_max_overlap(self.schema))
        if ok:
            new_key = _g_key(zones, self.schema)
            self.assertNotEqual(new_key, original_key,
                                "乐透型完全重复未被替换")

    def test_similar_semantics_is_zone_identity(self):
        """固化 _g_similar 的真实语义：统计「完全相同的分区个数」，不是「同位同号数」。

        乐透型只有 2 个分区（红球/蓝球），same 上限 = 2，
        而 max_overlap = 4 → 相似分支对乐透型永不触发（已知限制，已在源码注释说明）。
        若将来有人修正 _g_similar 为逐位比较，本测试会失败并提醒重新评估。
        """
        from prediction.engine import _g_similar
        existing = {_g_key(_zones(self.schema, [[1, 2, 3, 4, 5, 6], [1]]), self.schema)}
        # 红球重叠 4 个号码，但「红球区」作为一个整体并不相同 → 不判相似
        key = _g_key(_zones(self.schema, [[1, 2, 3, 4, 7, 8], [1]]), self.schema)
        self.assertFalse(_g_similar(key, existing, _g_max_overlap(self.schema)))
        # 完全相同（2 个分区都相同）→ same = 2，仍 < max_overlap(4)
        key_same = _g_key(_zones(self.schema, [[1, 2, 3, 4, 5, 6], [1]]), self.schema)
        self.assertFalse(_g_similar(key_same, existing, _g_max_overlap(self.schema)))


if __name__ == "__main__":
    unittest.main()
