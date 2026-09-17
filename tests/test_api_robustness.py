"""L4 第2期：/robustness 页面与 /api/robustness 端点契约"""
import unittest

from web.app import app


class RobustnessApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config["TESTING"] = True
        cls.client = app.test_client()

    def test_page_renders(self):
        resp = self.client.get("/robustness")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("鲁棒性监控", html)
        self.assertIn("static/vendor/echarts.min.js", html)  # 本地 vendor，无 CDN

    def test_api_single_lottery_contract(self):
        resp = self.client.get("/api/robustness?lottery=双色球")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertEqual(d["lottery"], "双色球")
        self.assertIn("report", d)
        self.assertIn("trend", d)
        self.assertIsInstance(d["trend"], list)

    def test_api_unknown_lottery_rejected(self):
        resp = self.client.get("/api/robustness?lottery=不存在彩种")
        self.assertEqual(resp.status_code, 400)

    def test_api_summary_contract(self):
        resp = self.client.get("/api/robustness/summary")
        self.assertEqual(resp.status_code, 200)
        d = resp.get_json()
        self.assertIn("lotteries", d)
        self.assertIn("worker", d)
        self.assertEqual(len(d["lotteries"]), 6)
        for item in d["lotteries"]:
            for key in ("lottery", "verdict", "alarms", "generated_at", "mode"):
                self.assertIn(key, item)


if __name__ == "__main__":
    unittest.main()
