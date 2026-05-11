import json
import os
from datetime import datetime
from app.services.postgres_store import PostgresDocumentStore

STATS_FILE = "storage/product_interest_stats.json"
STATS_DOC_KEY = "product_interest_stats"


class ProductInterestService:
    @staticmethod
    def _normalize_city(city_name: str | None) -> str:
        city = (city_name or "").strip().lower()
        if not city:
            return "unknown"
        if "(" in city and city.endswith(")"):
            city = city.rsplit("(", 1)[0].strip()
        return " ".join(city.split())

    @staticmethod
    def _load() -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(STATS_DOC_KEY, {"cities": {}, "updated_at": None})
            if isinstance(data, dict):
                if "cities" not in data or not isinstance(data.get("cities"), dict):
                    data["cities"] = {}
                return data
            return {"cities": {}, "updated_at": None}
        if not os.path.exists(STATS_FILE):
            return {"cities": {}, "updated_at": None}
        try:
            with open(STATS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                if "cities" not in data or not isinstance(data.get("cities"), dict):
                    data["cities"] = {}
                return data
        except Exception:
            pass
        return {"cities": {}, "updated_at": None}

    @staticmethod
    def _save(data: dict) -> None:
        if PostgresDocumentStore.is_enabled():
            data["updated_at"] = datetime.now().isoformat()
            PostgresDocumentStore.set_document(STATS_DOC_KEY, data)
            return
        os.makedirs("storage", exist_ok=True)
        data["updated_at"] = datetime.now().isoformat()
        with open(STATS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    @staticmethod
    def track_view(city_name: str | None, product_id: str | None, product_name: str | None) -> None:
        # Disabled to avoid blocking the event loop with synchronous file I/O.
        return

    @staticmethod
    def get_city_stats() -> list[dict]:
        data = ProductInterestService._load()
        cities = data.get("cities", {})
        result = []
        for city_key, city_entry in cities.items():
            if not isinstance(city_entry, dict):
                continue
            total = int(city_entry.get("total_views", 0))
            products = city_entry.get("products", {})
            rows = []
            if isinstance(products, dict):
                for p_id, p_entry in products.items():
                    if not isinstance(p_entry, dict):
                        continue
                    views = int(p_entry.get("views", 0))
                    pct = (views / total * 100.0) if total > 0 else 0.0
                    rows.append({
                        "id": p_id,
                        "name": p_entry.get("name", p_id),
                        "views": views,
                        "percent": pct,
                    })
            rows.sort(key=lambda x: x["views"], reverse=True)
            result.append({
                "city_key": city_key,
                "city": city_entry.get("display", city_key),
                "total_views": total,
                "products": rows,
            })

        result.sort(key=lambda x: x["total_views"], reverse=True)
        return result
