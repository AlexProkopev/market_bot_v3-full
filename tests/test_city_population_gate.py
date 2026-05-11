import asyncio
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from app.services import geo as geo_module
from app.services.geo import GeoService


class CityPopulationGateTests(unittest.TestCase):
    def test_rejects_unknown_population(self) -> None:
        self.assertFalse(
            GeoService.is_city_population_allowed(
                {"population": 0, "has_real_population": False},
                min_population=30000,
            )
        )

    def test_rejects_population_below_limit(self) -> None:
        self.assertFalse(
            GeoService.is_city_population_allowed(
                {"population": 29999, "has_real_population": True},
                min_population=30000,
            )
        )

    def test_accepts_population_from_limit(self) -> None:
        self.assertTrue(
            GeoService.is_city_population_allowed(
                {"population": 30000, "has_real_population": True},
                min_population=30000,
            )
        )

    def test_get_city_details_does_not_fabricate_population(self) -> None:
        original_cache_file = geo_module.CACHE_FILE
        original_population_file = geo_module.CITY_POPULATION_FILE

        with tempfile.TemporaryDirectory() as temp_dir:
            geo_module.CACHE_FILE = os.path.join(temp_dir, "districts_cache.json")
            geo_module.CITY_POPULATION_FILE = os.path.join(temp_dir, "city_population_cache.json")

            candidate = {
                "name": "Давлеканово",
                "region": "Республика Башкортостан",
                "type": "town",
                "raw_data": {
                    "addresstype": "city",
                    "extratags": {},
                },
            }

            try:
                with patch.object(GeoService, "_search_internet_for_districts", return_value=[]), patch.object(
                    GeoService,
                    "_generate_fallback_districts",
                    AsyncMock(return_value=["Центр"]),
                ), patch.object(GeoService, "_lookup_population_from_osm", return_value=0), patch.object(
                    GeoService,
                    "_search_population_by_name",
                    return_value=0,
                ):
                    info = asyncio.run(GeoService.get_city_details(candidate))

                self.assertEqual(info["population"], 0)
                self.assertFalse(info["has_real_population"])
            finally:
                geo_module.CACHE_FILE = original_cache_file
                geo_module.CITY_POPULATION_FILE = original_population_file

    def test_search_cities_filters_cache_candidates_below_population_limit(self) -> None:
        cache_candidates = [
            {
                "name": "Давлеканово",
                "region": "Республика Башкортостан",
                "type": "town",
                "raw_data": {"source": "cache"},
            },
            {
                "name": "Благовещенск",
                "region": "Республика Башкортостан",
                "type": "city",
                "raw_data": {"source": "cache"},
            },
        ]

        with patch.object(GeoService, "_fallback_cities_from_cache", return_value=cache_candidates), patch.object(
            GeoService,
            "_get_candidate_population",
            side_effect=[(23820, True), (35037, True)],
        ):
            result = asyncio.run(GeoService.search_cities("благ"))

        self.assertEqual([city["name"] for city in result], ["Благовещенск"])

    def test_search_cities_filters_online_candidates_below_population_limit(self) -> None:
        online_results = [
            {
                "address": {
                    "country_code": "ru",
                    "town": "Давлеканово",
                    "state": "Республика Башкортостан",
                },
                "type": "town",
                "addresstype": "city",
                "name": "Давлеканово",
                "extratags": {"population": "23820"},
            },
            {
                "address": {
                    "country_code": "ru",
                    "city": "Благовещенск",
                    "state": "Республика Башкортостан",
                },
                "type": "city",
                "addresstype": "city",
                "name": "Благовещенск",
                "extratags": {"population": "35037"},
            },
        ]

        with patch.object(GeoService, "_fallback_cities_from_cache", return_value=[]), patch.object(
            GeoService,
            "_find_city_osm_endpoint",
            return_value=online_results,
        ):
            result = asyncio.run(GeoService.search_cities("благ"))

        self.assertEqual([city["name"] for city in result], ["Благовещенск"])

    def test_search_cities_keeps_unknown_population_candidates_for_selected_city_check(self) -> None:
        cache_candidates = [
            {
                "name": "Тестоград",
                "region": "Тестовая область",
                "type": "city",
                "raw_data": {"source": "cache"},
            }
        ]

        with patch.object(GeoService, "_fallback_cities_from_cache", return_value=cache_candidates), patch.object(
            GeoService,
            "_get_candidate_population",
            return_value=(0, False),
        ):
            result = asyncio.run(GeoService.search_cities("тест"))

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Тестоград")
        self.assertEqual(result[0]["population"], 0)
        self.assertFalse(result[0]["has_real_population"])


if __name__ == "__main__":
    unittest.main()
