import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services import catalog as catalog_module
from app.services import geo as geo_module
from app.services.catalog import CatalogService
from app.services.postgres_store import PostgresDocumentStore


class CatalogDistrictLimitTests(unittest.TestCase):
    def setUp(self) -> None:
        self.postgres_enabled_patcher = patch.object(PostgresDocumentStore, "is_enabled", return_value=False)
        self.postgres_enabled_patcher.start()
        self.addCleanup(self.postgres_enabled_patcher.stop)

    def test_load_inventory_caps_full_city_districts_to_one_or_five(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            inventory_path = os.path.join(temp_dir, "products_inventory.json")
            geo_cache_path = os.path.join(temp_dir, "districts_cache.json")

            with open(geo_cache_path, "w", encoding="utf-8") as file_obj:
                json.dump(
                    {
                        "кемерово": [
                            "Заводский район",
                            "Кировский район",
                            "Ленинский район",
                            "Рудничный район",
                            "Центральный район",
                        ]
                    },
                    file_obj,
                    ensure_ascii=False,
                )
            with open(inventory_path, "w", encoding="utf-8") as file_obj:
                json.dump(
                    {
                        "кемерово": {
                            "p4": [
                                "Заводский район",
                                "Кировский район",
                                "Ленинский район",
                                "Рудничный район",
                                "Центральный район",
                            ]
                        }
                    },
                    file_obj,
                    ensure_ascii=False,
                )

            original_inventory = CatalogService.INVENTORY_FILE
            original_geo_cache = geo_module.CACHE_FILE

            try:
                CatalogService.INVENTORY_FILE = inventory_path
                geo_module.CACHE_FILE = geo_cache_path

                data = CatalogService._load_inventory()
                selected = data["кемерово"]["p4"]

                self.assertIn(len(selected), {1, 2, 3, 4, 5})
                self.assertLessEqual(len(selected), 5)

                with open(inventory_path, "r", encoding="utf-8") as file_obj:
                    saved = json.load(file_obj)
                self.assertEqual(saved["кемерово"]["p4"], selected)
            finally:
                CatalogService.INVENTORY_FILE = original_inventory
                geo_module.CACHE_FILE = original_geo_cache

    def test_get_available_districts_for_product_uses_one_to_five_for_large_city(self) -> None:
        districts = [
            "Заводский район",
            "Кировский район",
            "Ленинский район",
            "Рудничный район",
            "Центральный район",
            "ФПК",
        ]

        selected = CatalogService.get_available_districts_for_product("Кемерово", "p7", districts)

        self.assertIn(len(selected), {1, 2, 3, 4, 5})
        self.assertTrue(all(item in districts for item in selected))

    def test_get_available_districts_prefers_two_and_three(self) -> None:
        districts = [
            "Заводский район",
            "Кировский район",
            "Ленинский район",
            "Рудничный район",
            "Центральный район",
            "ФПК",
        ]

        counts = []
        for index in range(1, 101):
            selected = CatalogService.get_available_districts_for_product("Кемерово", f"p{index}", districts)
            counts.append(len(selected))

        middle_count = sum(1 for value in counts if value in {2, 3})
        edge_count = sum(1 for value in counts if value in {1, 4, 5})

        self.assertGreater(middle_count, edge_count)

    def test_get_available_districts_for_streets_uses_one_to_four(self) -> None:
        streets = [
            "ул. Ленина",
            "ул. Советская",
            "ул. Гагарина",
            "ул. Кирова",
            "ул. Победы",
            "пр-т Мира",
            "пер. Лесной",
            "бул. Молодежный",
        ]

        selected = CatalogService.get_available_districts_for_product("Томск", "p7", streets)

        self.assertGreaterEqual(len(selected), 1)
        self.assertLessEqual(len(selected), 4)
        self.assertLessEqual(len(selected), len(streets))
        self.assertTrue(all(item in streets for item in selected))

    def test_get_available_districts_keeps_microdistricts_and_quarters_as_districts(self) -> None:
        districts = [
            "Кировский",
            "Октябрьский",
            "42-й квартал",
            "мкр. Южный",
            "Центральный",
            "ФПК",
        ]

        selected = CatalogService.get_available_districts_for_product("Кемерово", "p10", districts)

        self.assertIn(len(selected), {1, 2, 3, 4, 5})
        self.assertTrue(all(item in districts for item in selected))

    def test_get_available_districts_combines_areas_and_streets_with_separate_limits(self) -> None:
        districts = [
            "Кировский",
            "42-й квартал",
            "мкр. Южный",
            "ул. Ленина",
            "ул. Советская",
            "ул. Гагарина",
            "ул. Кирова",
            "ул. Победы",
        ]

        selected = CatalogService.get_available_districts_for_product("Томск", "p11", districts)

        area_selected = [item for item in selected if not catalog_module._looks_like_street_name(item)]
        street_selected = [item for item in selected if catalog_module._looks_like_street_name(item)]

        self.assertGreaterEqual(len(area_selected), 1)
        self.assertLessEqual(len(area_selected), 5)
        self.assertGreaterEqual(len(street_selected), 1)
        self.assertLessEqual(len(street_selected), 4)

    def test_get_available_districts_ignores_center_in_pure_street_mode(self) -> None:
        districts = [
            "Центр",
            "ул. Ленина",
            "ул. Советская",
            "ул. Гагарина",
            "ул. Кирова",
        ]

        selected = CatalogService.get_available_districts_for_product("Томск", "p15", districts)

        self.assertNotIn("Центр", selected)
        self.assertTrue(all(catalog_module._looks_like_street_name(item) for item in selected))

    def test_clear_cache_removes_product_inventory_and_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            inventory_path = os.path.join(temp_dir, "products_inventory.json")
            geo_cache_path = os.path.join(temp_dir, "districts_cache.json")
            price_seed_path = os.path.join(temp_dir, "price_seed.json")
            products_cache_path = os.path.join(temp_dir, "products_cache.json")

            districts = [
                "Заводский район",
                "Кировский район",
                "Ленинский район",
                "Рудничный район",
                "Центральный район",
                "ФПК",
            ]

            with open(geo_cache_path, "w", encoding="utf-8") as file_obj:
                json.dump({"кемерово": districts}, file_obj, ensure_ascii=False)
            with open(inventory_path, "w", encoding="utf-8") as file_obj:
                json.dump({"кемерово": {"p7": ["Заводский район", "Кировский район", "Ленинский район"]}}, file_obj, ensure_ascii=False)
            with open(products_cache_path, "w", encoding="utf-8") as file_obj:
                json.dump({"кемерово": {"ids": ["p7"]}}, file_obj, ensure_ascii=False)
            with open(price_seed_path, "w", encoding="utf-8") as file_obj:
                json.dump({"seed": "111111"}, file_obj, ensure_ascii=False)

            original_inventory = CatalogService.INVENTORY_FILE
            original_geo_cache = geo_module.CACHE_FILE
            original_price_seed = catalog_module.PRICE_SEED_FILE
            original_products_cache = catalog_module.PRODUCTS_CACHE_FILE

            try:
                CatalogService.INVENTORY_FILE = inventory_path
                geo_module.CACHE_FILE = geo_cache_path
                catalog_module.PRICE_SEED_FILE = price_seed_path
                catalog_module.PRODUCTS_CACHE_FILE = products_cache_path

                before = CatalogService.get_product_districts("кемерово", "p7")
                CatalogService.clear_entire_cache()
                after = CatalogService.get_product_districts("кемерово", "p7")

                self.assertIsNotNone(before)
                self.assertIsNone(after)
                self.assertFalse(os.path.exists(products_cache_path))
                self.assertFalse(os.path.exists(inventory_path))
            finally:
                CatalogService.INVENTORY_FILE = original_inventory
                geo_module.CACHE_FILE = original_geo_cache
                catalog_module.PRICE_SEED_FILE = original_price_seed
                catalog_module.PRODUCTS_CACHE_FILE = original_products_cache


if __name__ == "__main__":
    unittest.main()