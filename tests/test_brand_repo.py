"""brand_repo.py 单元测试（提案 §D 验收）。"""

from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from autocut import brand_repo


class _RepoTestBase(unittest.TestCase):
    """每个测试用独立 tmp 仓库文件。"""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.repo_path = Path(self._tmp.name) / "brand_repo.json"
        self._env = patch.dict(
            "os.environ",
            {brand_repo.ENV_REPO_PATH: str(self.repo_path)},
        )
        self._env.start()

    def tearDown(self):
        self._env.stop()
        self._tmp.cleanup()


class BrandCRUDTests(_RepoTestBase):
    def test_list_empty_when_no_file(self):
        self.assertEqual(brand_repo.list_brands(), [])

    def test_create_and_get(self):
        brand = brand_repo.create_brand("Pinkypinky", ["哈利波特"])
        self.assertTrue(brand["id"].startswith("brand_"))
        self.assertEqual(brand["name"], "Pinkypinky")
        self.assertEqual(brand["associations"], ["哈利波特"])
        self.assertEqual(brand["products"], [])

        fetched = brand_repo.get_brand(brand["id"])
        self.assertEqual(fetched["name"], "Pinkypinky")

    def test_create_dedupe_associations(self):
        brand = brand_repo.create_brand("X", ["a", "b", "a", "", "  ", "b"])
        self.assertEqual(brand["associations"], ["a", "b"])

    def test_create_duplicate_name_conflict(self):
        brand_repo.create_brand("Same")
        with self.assertRaises(brand_repo.Conflict):
            brand_repo.create_brand("Same")

    def test_create_empty_name_rejected(self):
        with self.assertRaises(brand_repo.BrandRepoError):
            brand_repo.create_brand("   ")

    def test_get_unknown_404(self):
        with self.assertRaises(brand_repo.NotFound):
            brand_repo.get_brand("brand_nope")

    def test_update_name_and_rename(self):
        brand = brand_repo.create_brand("A")
        updated = brand_repo.update_brand(brand["id"], {"name": "B"})
        self.assertEqual(updated["name"], "B")
        self.assertEqual(updated["id"], brand["id"])  # id 永远不变

    def test_update_rename_conflict(self):
        a = brand_repo.create_brand("A")
        brand_repo.create_brand("B")
        with self.assertRaises(brand_repo.Conflict):
            brand_repo.update_brand(a["id"], {"name": "B"})

    def test_update_associations_replaces(self):
        brand = brand_repo.create_brand("A", ["x"])
        updated = brand_repo.update_brand(brand["id"], {"associations": ["y", "z"]})
        self.assertEqual(updated["associations"], ["y", "z"])

    def test_delete_empty_brand(self):
        brand = brand_repo.create_brand("A")
        brand_repo.delete_brand(brand["id"])
        with self.assertRaises(brand_repo.NotFound):
            brand_repo.get_brand(brand["id"])

    def test_delete_brand_with_products_requires_cascade(self):
        brand = brand_repo.create_brand("A")
        brand_repo.create_product(brand["id"], "P1")
        with self.assertRaises(brand_repo.Conflict):
            brand_repo.delete_brand(brand["id"])
        brand_repo.delete_brand(brand["id"], cascade=True)
        with self.assertRaises(brand_repo.NotFound):
            brand_repo.get_brand(brand["id"])


class ProductCRUDTests(_RepoTestBase):
    def setUp(self):
        super().setUp()
        self.brand = brand_repo.create_brand("Pinky", ["assoc-brand"])

    def test_create_product(self):
        product = brand_repo.create_product(
            self.brand["id"],
            "P1",
            selling_points=["sp1", "sp2"],
            associations=["a1"],
        )
        self.assertTrue(product["id"].startswith("prod_"))
        self.assertEqual(product["selling_points"], ["sp1", "sp2"])

    def test_create_product_unknown_brand_404(self):
        with self.assertRaises(brand_repo.NotFound):
            brand_repo.create_product("brand_nope", "P1")

    def test_duplicate_product_name_in_brand_conflict(self):
        brand_repo.create_product(self.brand["id"], "P1")
        with self.assertRaises(brand_repo.Conflict):
            brand_repo.create_product(self.brand["id"], "P1")

    def test_same_product_name_across_brands_ok(self):
        other = brand_repo.create_brand("Other")
        brand_repo.create_product(self.brand["id"], "P1")
        brand_repo.create_product(other["id"], "P1")  # 不应冲突

    def test_update_product(self):
        product = brand_repo.create_product(self.brand["id"], "P1")
        updated = brand_repo.update_product(
            self.brand["id"], product["id"], {"selling_points": ["new"]}
        )
        self.assertEqual(updated["selling_points"], ["new"])

    def test_delete_product(self):
        product = brand_repo.create_product(self.brand["id"], "P1")
        brand_repo.delete_product(self.brand["id"], product["id"])
        self.assertEqual(brand_repo.list_products(self.brand["id"]), [])


class MergeTermsTests(_RepoTestBase):
    """D4: merge_terms 语义表。"""

    def setUp(self):
        super().setUp()
        self.brand = brand_repo.create_brand("B", ["b1", "b2"])
        self.product = brand_repo.create_product(
            self.brand["id"], "P", associations=["p1", "b1"]  # b1 与 brand 重复
        )

    def test_none_none_returns_extra(self):
        result = brand_repo.merge_terms(None, None, ["x", "y", "x"])
        self.assertEqual(result, ["x", "y"])

    def test_brand_only(self):
        result = brand_repo.merge_terms(self.brand["id"], None, ["x"])
        self.assertEqual(result, ["b1", "b2", "x"])

    def test_brand_and_product_dedupe(self):
        result = brand_repo.merge_terms(self.brand["id"], self.product["id"], ["x", "b1"])
        # brand b1, b2 → product p1 (b1 跳重) → extra x (b1 跳重)
        self.assertEqual(result, ["b1", "b2", "p1", "x"])

    def test_product_without_brand_raises(self):
        with self.assertRaises(ValueError):
            brand_repo.merge_terms(None, "prod_xxx", [])

    def test_unknown_brand_404(self):
        with self.assertRaises(brand_repo.NotFound):
            brand_repo.merge_terms("brand_nope", None, [])


class SchemaVersionTests(_RepoTestBase):
    def _write_repo(self, payload: dict):
        self.repo_path.parent.mkdir(parents=True, exist_ok=True)
        self.repo_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_missing_schema_treated_as_empty(self):
        self._write_repo({"brands": [{"id": "x", "name": "x", "associations": [], "products": []}]})
        # 缺 schema_version 视为空仓库重新初始化
        self.assertEqual(brand_repo.list_brands(), [])

    def test_lower_schema_treated_as_empty(self):
        self._write_repo({"schema_version": 0, "brands": []})
        self.assertEqual(brand_repo.list_brands(), [])

    def test_higher_schema_refuses(self):
        self._write_repo({"schema_version": 999, "brands": []})
        with self.assertRaises(brand_repo.SchemaVersionError):
            brand_repo.list_brands()

    def test_malformed_json_raises(self):
        self.repo_path.parent.mkdir(parents=True, exist_ok=True)
        self.repo_path.write_text("not json", encoding="utf-8")
        with self.assertRaises(brand_repo.BrandRepoError):
            brand_repo.list_brands()


class ConcurrencyTests(_RepoTestBase):
    """D5: 20 线程并发创建品牌，最终应有 20 条不重名、无丢失。"""

    def test_concurrent_create_brands(self):
        errors: list[BaseException] = []

        def worker(idx: int):
            try:
                brand_repo.create_brand(f"brand-{idx}")
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [], f"unexpected errors: {errors}")
        names = sorted(b["name"] for b in brand_repo.list_brands())
        self.assertEqual(names, sorted(f"brand-{i}" for i in range(20)))

    def test_concurrent_conflict_only_one_wins(self):
        successes: list[dict] = []
        conflicts: list[BaseException] = []

        def worker():
            try:
                successes.append(brand_repo.create_brand("same"))
            except brand_repo.Conflict as exc:
                conflicts.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(successes), 1)
        self.assertEqual(len(conflicts), 9)


class SuggestAssociationsTests(_RepoTestBase):
    def test_input_validation(self):
        with self.assertRaises(brand_repo.BrandRepoError):
            brand_repo.suggest_associations("", "product", [])
        with self.assertRaises(brand_repo.BrandRepoError):
            brand_repo.suggest_associations("brand", "  ", [])

    def test_success_returns_dedup_keep_order(self):
        from autocut import llm as llm_mod

        fake_content = '{"suggestions": ["哈利波特", "魔杖", "哈利波特", "格兰芬多"]}'
        with patch.object(llm_mod, "chat_completion", return_value=fake_content):
            out = brand_repo.suggest_associations(
                "Pinkypinky", "口红", ["持久", "不沾杯"]
            )
        self.assertEqual(out, ["哈利波特", "魔杖", "格兰芬多"])

    def test_llm_error_translated_to_unavailable(self):
        from autocut import llm as llm_mod

        def boom(_prompt):
            raise llm_mod.LLMError("api key missing")

        with patch.object(llm_mod, "chat_completion", side_effect=boom):
            with self.assertRaises(brand_repo.LLMUnavailable):
                brand_repo.suggest_associations("brand", "product", [])

    def test_missing_suggestions_field(self):
        from autocut import llm as llm_mod

        with patch.object(llm_mod, "chat_completion", return_value='{"foo": []}'):
            with self.assertRaises(brand_repo.LLMUnavailable):
                brand_repo.suggest_associations("brand", "product", [])

    def test_invalid_json(self):
        from autocut import llm as llm_mod

        with patch.object(llm_mod, "chat_completion", return_value="not json at all"):
            with self.assertRaises(brand_repo.LLMUnavailable):
                brand_repo.suggest_associations("brand", "product", [])


if __name__ == "__main__":
    unittest.main()
