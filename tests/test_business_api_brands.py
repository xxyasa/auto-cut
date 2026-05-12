"""business_api.py 集成测试：品牌 CRUD + 鉴权端到端（提案 §C / §D 子集）。"""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from autocut import auth, brand_repo
from autocut.api import create_app


TOKEN = "test-secret-token"


class BusinessApiTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.repo_path = Path(self._tmp.name) / "brand_repo.json"
        self._env = patch.dict(
            "os.environ",
            {
                brand_repo.ENV_REPO_PATH: str(self.repo_path),
                auth.ENV_VAR: TOKEN,
            },
        )
        self._env.start()
        self.app = create_app()
        self.client = TestClient(self.app)

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()

    def _h(self, token: str | None = TOKEN) -> dict:
        return {"Authorization": f"Bearer {token}"} if token else {}


class AuthEndToEnd(BusinessApiTestBase):
    def test_unauth_401(self):
        resp = self.client.get("/api/business/brands")
        self.assertEqual(resp.status_code, 401)

    def test_wrong_token_401(self):
        resp = self.client.get("/api/business/brands", headers=self._h("wrong"))
        self.assertEqual(resp.status_code, 401)

    def test_bearer_ok(self):
        resp = self.client.get("/api/business/brands", headers=self._h())
        self.assertEqual(resp.status_code, 200)

    def test_x_header_ok(self):
        resp = self.client.get(
            "/api/business/brands", headers={"X-Autocut-Token": TOKEN}
        )
        self.assertEqual(resp.status_code, 200)

    def test_cookie_ok(self):
        self.client.cookies.set("autocut_token", TOKEN)
        resp = self.client.get("/api/business/brands")
        self.assertEqual(resp.status_code, 200)

    def test_legacy_runs_unaffected(self):
        # /api/runs 仍然不需要 token（保持本地审核台体验）
        resp = self.client.get("/api/runs")
        self.assertEqual(resp.status_code, 200)


class BrandRoutes(BusinessApiTestBase):
    def test_create_get_list(self):
        resp = self.client.post(
            "/api/business/brands",
            json={"name": "Pinky", "associations": ["哈利波特"]},
            headers=self._h(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        brand = resp.json()
        self.assertTrue(brand["id"].startswith("brand_"))

        resp = self.client.get(f"/api/business/brands/{brand['id']}", headers=self._h())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "Pinky")

        resp = self.client.get("/api/business/brands", headers=self._h())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["brands"]), 1)

    def test_duplicate_brand_409(self):
        self.client.post(
            "/api/business/brands", json={"name": "X"}, headers=self._h()
        )
        resp = self.client.post(
            "/api/business/brands", json={"name": "X"}, headers=self._h()
        )
        self.assertEqual(resp.status_code, 409)

    def test_get_unknown_404(self):
        resp = self.client.get("/api/business/brands/brand_nope", headers=self._h())
        self.assertEqual(resp.status_code, 404)

    def test_patch_rename(self):
        brand = self.client.post(
            "/api/business/brands", json={"name": "A"}, headers=self._h()
        ).json()
        resp = self.client.patch(
            f"/api/business/brands/{brand['id']}",
            json={"name": "B"},
            headers=self._h(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "B")
        self.assertEqual(resp.json()["id"], brand["id"])

    def test_delete_empty(self):
        brand = self.client.post(
            "/api/business/brands", json={"name": "A"}, headers=self._h()
        ).json()
        resp = self.client.delete(
            f"/api/business/brands/{brand['id']}", headers=self._h()
        )
        self.assertEqual(resp.status_code, 204)

    def test_delete_with_products_requires_cascade(self):
        brand = self.client.post(
            "/api/business/brands", json={"name": "A"}, headers=self._h()
        ).json()
        self.client.post(
            f"/api/business/brands/{brand['id']}/products",
            json={"name": "P1"},
            headers=self._h(),
        )
        resp = self.client.delete(
            f"/api/business/brands/{brand['id']}", headers=self._h()
        )
        self.assertEqual(resp.status_code, 409)

        resp = self.client.delete(
            f"/api/business/brands/{brand['id']}?cascade=true", headers=self._h()
        )
        self.assertEqual(resp.status_code, 204)

    def test_invalid_payload_422(self):
        # name 缺失 → Pydantic 422
        resp = self.client.post(
            "/api/business/brands", json={}, headers=self._h()
        )
        self.assertEqual(resp.status_code, 422)


class ProductRoutes(BusinessApiTestBase):
    def _make_brand(self) -> dict:
        return self.client.post(
            "/api/business/brands", json={"name": "B"}, headers=self._h()
        ).json()

    def test_create_list_delete(self):
        brand = self._make_brand()
        resp = self.client.post(
            f"/api/business/brands/{brand['id']}/products",
            json={"name": "P1", "selling_points": ["sp"]},
            headers=self._h(),
        )
        self.assertEqual(resp.status_code, 201, resp.text)
        product = resp.json()
        self.assertTrue(product["id"].startswith("prod_"))

        resp = self.client.get(
            f"/api/business/brands/{brand['id']}/products", headers=self._h()
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(resp.json()["products"]), 1)

        resp = self.client.delete(
            f"/api/business/brands/{brand['id']}/products/{product['id']}",
            headers=self._h(),
        )
        self.assertEqual(resp.status_code, 204)

    def test_duplicate_product_in_brand_409(self):
        brand = self._make_brand()
        self.client.post(
            f"/api/business/brands/{brand['id']}/products",
            json={"name": "P1"},
            headers=self._h(),
        )
        resp = self.client.post(
            f"/api/business/brands/{brand['id']}/products",
            json={"name": "P1"},
            headers=self._h(),
        )
        self.assertEqual(resp.status_code, 409)

    def test_product_in_unknown_brand_404(self):
        resp = self.client.post(
            "/api/business/brands/brand_nope/products",
            json={"name": "P1"},
            headers=self._h(),
        )
        self.assertEqual(resp.status_code, 404)


class SuggestAssociationsRoute(BusinessApiTestBase):
    def test_pr1_returns_502(self):
        brand = self.client.post(
            "/api/business/brands", json={"name": "B"}, headers=self._h()
        ).json()
        resp = self.client.post(
            f"/api/business/brands/{brand['id']}/suggest-associations",
            json={"product_name": "P", "selling_points": []},
            headers=self._h(),
        )
        # PR-1 骨架阶段：LLMUnavailable → 502
        self.assertEqual(resp.status_code, 502)


if __name__ == "__main__":
    unittest.main()
