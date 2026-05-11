import json
import os
import tempfile
import unittest

from app.services import catalog as catalog_module
from app.services.catalog import CatalogService


class CatalogStashTypeMigrationTests(unittest.TestCase):
    def test_load_stash_types_migrates_legacy_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = os.path.join(temp_dir, "stash_types.json")
            mapping_path = os.path.join(temp_dir, "product_stash_types.json")

            with open(registry_path, "w", encoding="utf-8") as file_obj:
                json.dump(["Тайник-Камень", "Прикоп снежный", "Магнит"], file_obj, ensure_ascii=False)
            with open(mapping_path, "w", encoding="utf-8") as file_obj:
                json.dump({"p1": ["Тайник-Камень", "Магнит"]}, file_obj, ensure_ascii=False)

            original_registry = CatalogService.STASH_TYPES_REGISTRY_FILE
            original_mapping = CatalogService.STASH_TYPES_FILE
            original_stash_types = CatalogService.STASH_TYPES
            original_product_stash_types = CatalogService.PRODUCT_STASH_TYPES

            try:
                CatalogService.STASH_TYPES_REGISTRY_FILE = registry_path
                CatalogService.STASH_TYPES_FILE = mapping_path
                CatalogService.STASH_TYPES = []
                CatalogService.PRODUCT_STASH_TYPES = {}

                loaded = CatalogService._load_stash_types()

                self.assertEqual(CatalogService.get_available_stash_types(), ["Тайник", "Прикоп", "Магнит"])
                self.assertEqual(loaded["p1"], ["Тайник", "Магнит"])

                with open(registry_path, "r", encoding="utf-8") as file_obj:
                    self.assertEqual(json.load(file_obj), ["Тайник", "Прикоп", "Магнит"])
                with open(mapping_path, "r", encoding="utf-8") as file_obj:
                    self.assertEqual(json.load(file_obj), {"p1": ["Тайник", "Магнит"]})
            finally:
                CatalogService.STASH_TYPES_REGISTRY_FILE = original_registry
                CatalogService.STASH_TYPES_FILE = original_mapping
                CatalogService.STASH_TYPES = original_stash_types
                CatalogService.PRODUCT_STASH_TYPES = original_product_stash_types

    def test_set_available_stash_types_rebalances_products_with_canonical_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = os.path.join(temp_dir, "stash_types.json")
            mapping_path = os.path.join(temp_dir, "product_stash_types.json")
            cache_path = os.path.join(temp_dir, "products_cache.json")

            with open(registry_path, "w", encoding="utf-8") as file_obj:
                json.dump(["Тайник-Камень", "Магнит", "Прикоп снежный"], file_obj, ensure_ascii=False)
            with open(mapping_path, "w", encoding="utf-8") as file_obj:
                json.dump({"p1": ["Тайник-Камень", "Магнит"]}, file_obj, ensure_ascii=False)

            original_registry = CatalogService.STASH_TYPES_REGISTRY_FILE
            original_mapping = CatalogService.STASH_TYPES_FILE
            original_all_products = CatalogService.ALL_PRODUCTS
            original_stash_types = CatalogService.STASH_TYPES
            original_product_stash_types = CatalogService.PRODUCT_STASH_TYPES
            original_cache_file = catalog_module.PRODUCTS_CACHE_FILE

            try:
                CatalogService.STASH_TYPES_REGISTRY_FILE = registry_path
                CatalogService.STASH_TYPES_FILE = mapping_path
                CatalogService.ALL_PRODUCTS = [{"id": f"p{index}"} for index in range(1, 7)]
                CatalogService.STASH_TYPES = []
                CatalogService.PRODUCT_STASH_TYPES = {}
                catalog_module.PRODUCTS_CACHE_FILE = cache_path

                result = CatalogService.set_available_stash_types(["тайник", "прикоп", "магнит"])

                self.assertTrue(result)
                self.assertEqual(CatalogService.get_available_stash_types(), ["Тайник", "Прикоп", "Магнит"])

                used_types = set()
                for product in CatalogService.ALL_PRODUCTS:
                    pair = CatalogService.get_product_stash_types(product["id"])
                    self.assertEqual(len(pair), 2)
                    self.assertTrue(all(item in {"Тайник", "Прикоп", "Магнит"} for item in pair))
                    used_types.update(pair)

                self.assertEqual(used_types, {"Тайник", "Прикоп", "Магнит"})
            finally:
                CatalogService.STASH_TYPES_REGISTRY_FILE = original_registry
                CatalogService.STASH_TYPES_FILE = original_mapping
                CatalogService.ALL_PRODUCTS = original_all_products
                CatalogService.STASH_TYPES = original_stash_types
                CatalogService.PRODUCT_STASH_TYPES = original_product_stash_types
                catalog_module.PRODUCTS_CACHE_FILE = original_cache_file

    def test_district_stash_types_vary_between_districts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = os.path.join(temp_dir, "stash_types.json")
            mapping_path = os.path.join(temp_dir, "product_stash_types.json")
            price_seed_path = os.path.join(temp_dir, "price_seed.json")

            with open(registry_path, "w", encoding="utf-8") as file_obj:
                json.dump(["Тайник", "Прикоп", "Магнит", "Снеговик"], file_obj, ensure_ascii=False)
            with open(mapping_path, "w", encoding="utf-8") as file_obj:
                json.dump({}, file_obj, ensure_ascii=False)
            with open(price_seed_path, "w", encoding="utf-8") as file_obj:
                json.dump({"seed": "123456"}, file_obj, ensure_ascii=False)

            original_registry = CatalogService.STASH_TYPES_REGISTRY_FILE
            original_mapping = CatalogService.STASH_TYPES_FILE
            original_stash_types = CatalogService.STASH_TYPES
            original_product_stash_types = CatalogService.PRODUCT_STASH_TYPES
            original_price_seed = catalog_module.PRICE_SEED_FILE

            try:
                CatalogService.STASH_TYPES_REGISTRY_FILE = registry_path
                CatalogService.STASH_TYPES_FILE = mapping_path
                CatalogService.STASH_TYPES = []
                CatalogService.PRODUCT_STASH_TYPES = {}
                catalog_module.PRICE_SEED_FILE = price_seed_path

                district_map = CatalogService.get_district_stash_type_map(
                    "Москва",
                    "p7",
                    ["Арбат", "Тверской район", "Хамовники", "Ясенево"],
                )

                self.assertEqual(len(district_map), 4)
                self.assertTrue(all(len(value) == 2 for value in district_map.values()))
                self.assertGreater(len({tuple(value) for value in district_map.values()}), 1)
            finally:
                CatalogService.STASH_TYPES_REGISTRY_FILE = original_registry
                CatalogService.STASH_TYPES_FILE = original_mapping
                CatalogService.STASH_TYPES = original_stash_types
                CatalogService.PRODUCT_STASH_TYPES = original_product_stash_types
                catalog_module.PRICE_SEED_FILE = original_price_seed

    def test_add_stash_type_rejects_alias_of_existing_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            registry_path = os.path.join(temp_dir, "stash_types.json")

            with open(registry_path, "w", encoding="utf-8") as file_obj:
                json.dump(["Тайник", "Прикоп", "Магнит"], file_obj, ensure_ascii=False)

            original_registry = CatalogService.STASH_TYPES_REGISTRY_FILE
            original_stash_types = CatalogService.STASH_TYPES

            try:
                CatalogService.STASH_TYPES_REGISTRY_FILE = registry_path
                CatalogService.STASH_TYPES = []

                result = CatalogService.add_stash_type("Тайник-камень")

                self.assertFalse(result)
                self.assertEqual(CatalogService.get_available_stash_types(), ["Тайник", "Прикоп", "Магнит"])
            finally:
                CatalogService.STASH_TYPES_REGISTRY_FILE = original_registry
                CatalogService.STASH_TYPES = original_stash_types


if __name__ == "__main__":
    unittest.main()