import json
import os
import tempfile
import unittest
from unittest.mock import patch

from app.services.catalog import CatalogService
from app.services.pricing import PricingService


class RegionPricingTests(unittest.TestCase):
    def test_region_multiplier_uses_city_cache_key_with_region_suffix(self) -> None:
        original_pricing_cache = PricingService._cache
        try:
            PricingService._cache = {
                "regions": {
                    "кировская область": {
                        "label": "Кировская область",
                        "value": 1.7,
                    }
                },
                "cities": {},
            }

            with patch("app.services.geo.GeoService._load_cache", return_value={"киров (кировская область)": ["Ленинский район"]}):
                multiplier = CatalogService._get_city_multiplier("Киров")

            self.assertEqual(multiplier, 1.7)
        finally:
            PricingService._cache = original_pricing_cache

    def test_region_multiplier_prefers_regional_cache_key_over_plain_city_key(self) -> None:
        original_pricing_cache = PricingService._cache
        try:
            PricingService._cache = {
                "regions": {
                    "ханты-мансийский автономный округ - югра": {
                        "label": "Ханты-Мансийский автономный округ — Югра",
                        "value": 2.0,
                    }
                },
                "cities": {},
            }

            with patch(
                "app.services.geo.GeoService._load_cache",
                return_value={
                    "сургут": ["Северный"],
                    "сургут ()": ["Пустой регион"],
                    "сургут (ханты-мансийский автономный округ — югра)": ["Северный район"],
                },
            ):
                multiplier = CatalogService._get_city_multiplier("Сургут")

            self.assertEqual(multiplier, 2.0)
        finally:
            PricingService._cache = original_pricing_cache


if __name__ == "__main__":
    unittest.main()