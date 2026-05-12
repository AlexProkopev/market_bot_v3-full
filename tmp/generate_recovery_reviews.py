import json
import math
import random
import secrets
import sys
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.catalog import CatalogService
from app.services.geo import GeoService
from app.services.reviews import ReviewsService


TARGET_REVIEWS = 500
OUTPUT_PATH = Path("tmp/reviews_recovery_seed.json")


def _match_district_key(city_key: str, districts_cache: dict) -> str | None:
    for district_key in districts_cache.keys():
        if isinstance(district_key, str) and ReviewsService._is_city_key_match(city_key, district_key):
            return district_key
    return None


def _build_reviews() -> list[dict]:
    random.seed(42)

    city_options = ReviewsService.get_manual_city_options()
    products_cache = CatalogService._load_cache()
    districts_cache = GeoService._load_cache()
    all_products = {item["id"]: item for item in CatalogService.get_all_products() if item.get("id")}

    if not city_options:
        raise RuntimeError("No city options available for recovery generation")

    reviews: list[dict] = []
    start_dt = datetime.now() - timedelta(days=90)
    per_city = max(8, math.ceil(TARGET_REVIEWS / max(len(city_options), 1)))

    for city in city_options:
        city_key = city.get("city_key")
        city_display = city.get("display") or city_key
        if not city_key or not city_display:
            continue

        district_key = _match_district_key(city_key, districts_cache)
        if not district_key:
            continue

        district_options = districts_cache.get(district_key) or []
        product_entry = products_cache.get(city_key) or {}
        product_ids = product_entry.get("ids", []) if isinstance(product_entry, dict) else []
        product_options = [all_products[pid] for pid in product_ids if pid in all_products]

        if not district_options or not product_options:
            continue

        for _ in range(per_city):
            product = random.choice(product_options)
            district_raw = random.choice(district_options)
            district_display = ReviewsService._format_location_display(district_raw)
            current_dt = start_dt + timedelta(minutes=len(reviews) * random.randint(45, 240))
            text = ReviewsService._generate_local_review_text(
                product.get("name"),
                city_display,
                district_display,
                product.get("base_price"),
            )
            reviews.append(
                {
                    "id": secrets.token_hex(6),
                    "user": ReviewsService.HIDDEN_USERNAME,
                    "base_name": ReviewsService.HIDDEN_USERNAME,
                    "text": text,
                    "product": product.get("name"),
                    "city": city_display,
                    "item_info": ReviewsService._build_item_info(product.get("name"), city_display, district_display),
                    "stars": 5 if random.random() > 0.12 else 4,
                    "iso_date": current_dt.isoformat(),
                    "display_date": current_dt.strftime("%d.%m %H:%M"),
                    "hidden": False,
                }
            )
            if len(reviews) >= TARGET_REVIEWS:
                return reviews

    return reviews


def main() -> None:
    reviews = _build_reviews()
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"generated={len(reviews)}")
    print(f"output={OUTPUT_PATH}")


if __name__ == "__main__":
    main()