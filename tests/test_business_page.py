import unittest
from fastapi.testclient import TestClient
from autocut.api import create_app

class TestBusinessPage(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = TestClient(self.app)

    def test_business_html_route(self):
        response = self.client.get("/business.html")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/html", response.headers["content-type"])
        self.assertIn("<title>Auto Cut · 自助成片</title>", response.text)
        self.assertIn("混剪方案数", response.text)
        self.assertIn('id="remix-duration" value="30"', response.text)
        self.assertIn('id="remix-plan-count" value="2"', response.text)
        self.assertIn("处理过程", response.text)
        self.assertIn("初剪轨道", response.text)
        self.assertIn("成片预览", response.text)
        self.assertNotIn("Remix 轨道", response.text)

    def test_business_css_route(self):
        response = self.client.get("/business.css")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/css", response.headers["content-type"])
        self.assertIn("--primary", response.text)

    def test_business_js_route(self):
        response = self.client.get("/business.js")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/javascript", response.headers.get("content-type", "").lower() or response.headers.get("content-type", "").lower())
        # Actually fastapi static files might return application/javascript or application/x-javascript depending on OS. Let's just check for javascript
        self.assertTrue("javascript" in response.headers["content-type"].lower())
        self.assertIn("API_BASE", response.text)
        self.assertIn("plan_count", response.text)
        self.assertIn("remix_plans", response.text)
        self.assertIn("setValue('glm-asr')", response.text)
        self.assertIn("glm-asr (默认，高精度)", response.text)

if __name__ == '__main__':
    unittest.main()
