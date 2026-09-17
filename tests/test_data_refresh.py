import unittest
from unittest.mock import Mock, patch


class DataRefreshTests(unittest.TestCase):
    def test_refresh_redblue_lottery_cleans_stats_cache_and_memory_cache(self):
        import web.utils as utils

        fake_record = Mock()
        fake_record.期号 = 26093
        fake_data = Mock()
        fake_data.total_records = 3490
        fake_data.records = [fake_record]

        fake_cache = Mock()

        with patch("data.cleaner.run_clean") as run_clean, \
             patch("data.cache.StatsCache", return_value=fake_cache), \
             patch("data.loader.invalidate_lottery_cache") as invalidate_memory, \
             patch.object(utils, "load_lottery", return_value=fake_data):
            result = utils.refresh_data("双色球")

        self.assertTrue(result["success"])
        run_clean.assert_called_once_with("双色球")
        fake_cache.invalidate.assert_called_once_with("双色球")
        invalidate_memory.assert_called_once_with("双色球")

    def test_refresh_generic_lottery_skips_redblue_clean_but_invalidates_caches(self):
        import web.utils as utils

        fake_record = Mock()
        fake_record.期号 = 26218
        fake_data = Mock()
        fake_data.total_records = 7690
        fake_data.records = [fake_record]

        fake_cache = Mock()

        with patch("data.cleaner.run_clean") as run_clean, \
             patch("data.cache.StatsCache", return_value=fake_cache), \
             patch("data.loader.invalidate_lottery_cache") as invalidate_memory, \
             patch.object(utils, "load_lottery", return_value=fake_data):
            result = utils.refresh_data("排列5")

        self.assertTrue(result["success"])
        run_clean.assert_not_called()
        fake_cache.invalidate.assert_called_once_with("排列5")
        invalidate_memory.assert_called_once_with("排列5")


if __name__ == "__main__":
    unittest.main()
