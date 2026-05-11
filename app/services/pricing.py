import json
import os
from typing import Any, Dict, List, Optional
from app.services.postgres_store import PostgresDocumentStore

PRICING_FILE = os.path.join("storage", "region_pricing.json")
PRICING_DOC_KEY = "region_pricing"


class PricingService:
    _cache: Optional[Dict[str, Any]] = None

    @staticmethod
    def _normalize(value: str) -> str:
        cleaned = value.lower().strip()
        cleaned = cleaned.replace("ё", "е")
        cleaned = cleaned.replace("—", "-").replace("–", "-")
        cleaned = " ".join(cleaned.split())
        return cleaned

    @staticmethod
    def _ensure_defaults() -> Dict[str, Any]:
        default_regions: List[Dict[str, Any]] = [
            {"label": "Ханты-Мансийский автономный округ — Югра", "value": 1.5},
            {"label": "Забайкальский край", "value": 1.3},
            {"label": "Сахалинская область", "value": 1.8},
            {"label": "Алтайский край", "value": 1.3},
            {"label": "Республика Алтай", "value": 1.3},
            {"label": "Кабардино-Балкарская Республика", "value": 1.4},
            {"label": "Красноярский край", "value": 1.3},
            {"label": "Новосибирская область", "value": 1.3},
            {"label": "Иркутская область", "value": 1.3},
            {"label": "Республика Саха (Якутия)", "value": 1.3},
        ]

        regions: Dict[str, Any] = {}
        for item in default_regions:
            key = PricingService._normalize(item["label"])
            regions[key] = {"label": item["label"], "value": item["value"]}

        return {"regions": regions, "cities": {}}

    @staticmethod
    def _load() -> Dict[str, Any]:
        if PricingService._cache is not None:
            return PricingService._cache

        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(PRICING_DOC_KEY, PricingService._ensure_defaults())
            if not isinstance(data, dict):
                data = PricingService._ensure_defaults()
        else:
            if not os.path.exists(os.path.dirname(PRICING_FILE)):
                os.makedirs(os.path.dirname(PRICING_FILE), exist_ok=True)

            if not os.path.exists(PRICING_FILE):
                data = PricingService._ensure_defaults()
                PricingService._save(data)
                PricingService._cache = data
                return data

            try:
                with open(PRICING_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except (json.JSONDecodeError, OSError):
                data = PricingService._ensure_defaults()
                PricingService._save(data)

        if "regions" not in data or "cities" not in data:
            base = PricingService._ensure_defaults()
            base.update({k: data.get(k, base.get(k, {})) for k in ("regions", "cities")})
            data = base

        # Ensure structure of entries
        for bucket in ("regions", "cities"):
            raw = data.get(bucket, {})
            clean: Dict[str, Any] = {}
            for key, value in raw.items():
                norm_key = PricingService._normalize(key)
                if isinstance(value, dict):
                    label = value.get("label") or key
                    numeric = float(value.get("value", 1.0) or 1.0)
                else:
                    label = key
                    numeric = float(value or 1.0)
                clean[norm_key] = {"label": label, "value": numeric}
            data[bucket] = clean

        PricingService._cache = data
        return data

    @staticmethod
    def _save(data: Dict[str, Any]) -> None:
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(PRICING_DOC_KEY, data)
        else:
            if not os.path.exists(os.path.dirname(PRICING_FILE)):
                os.makedirs(os.path.dirname(PRICING_FILE), exist_ok=True)

            with open(PRICING_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        PricingService._cache = data

    @staticmethod
    def invalidate_cache() -> None:
        PricingService._cache = None

    @staticmethod
    def _extract_value(entry: Optional[Dict[str, Any]]) -> Optional[float]:
        if not entry:
            return None
        numeric = float(entry.get("value", 0) or 0)
        if numeric <= 0:
            return None
        return numeric

    @staticmethod
    def get_region_entries() -> List[Dict[str, Any]]:
        data = PricingService._load()
        entries = []
        for key, entry in data.get("regions", {}).items():
            entries.append({
                "key": key,
                "label": entry.get("label") or key,
                "value": float(entry.get("value", 1.0) or 1.0)
            })
        entries.sort(key=lambda item: item["label"].lower())
        return entries

    @staticmethod
    def set_region_multiplier(label: str, multiplier: float) -> None:
        if multiplier <= 0:
            raise ValueError("Multiplier must be positive")

        data = PricingService._load()
        key = PricingService._normalize(label)
        data.setdefault("regions", {})[key] = {"label": label, "value": float(multiplier)}
        PricingService._save(data)

    @staticmethod
    def clear_region_multiplier(label_or_key: str) -> None:
        data = PricingService._load()
        key = PricingService._normalize(label_or_key)
        if key in data.get("regions", {}):
            del data["regions"][key]
            PricingService._save(data)

    @staticmethod
    def get_multiplier(
        city_name: Optional[str] = None,
        region_name: Optional[str] = None,
        cache_key: Optional[str] = None
    ) -> Optional[float]:
        data = PricingService._load()

        def match_region(target: Optional[str]) -> Optional[float]:
            if not target:
                return None
            norm = PricingService._normalize(target)
            for key, entry in data.get("regions", {}).items():
                entry_label = PricingService._normalize(entry.get("label") or "")
                candidate_norms = [key, entry_label]
                for candidate in candidate_norms:
                    if not candidate:
                        continue
                    if candidate == norm or candidate in norm or norm in candidate:
                        value = PricingService._extract_value(entry)
                        if value:
                            return value
            return None

        def match_city(target: Optional[str]) -> Optional[float]:
            if not target:
                return None
            norm = PricingService._normalize(target)
            for key, entry in data.get("cities", {}).items():
                entry_label = PricingService._normalize(entry.get("label") or "")
                candidate_norms = [key, entry_label]
                for candidate in candidate_norms:
                    if not candidate:
                        continue
                    if candidate == norm or candidate in norm or norm in candidate:
                        value = PricingService._extract_value(entry)
                        if value:
                            return value
            return None

        city_value = match_city(city_name)
        if city_value:
            return city_value

        region_value = match_region(region_name)
        if region_value:
            return region_value

        if cache_key:
            key_value = match_region(cache_key)
            if key_value:
                return key_value

        return None

    @staticmethod
    def set_city_multiplier(label: str, multiplier: float) -> None:
        if multiplier <= 0:
            raise ValueError("Multiplier must be positive")

        data = PricingService._load()
        key = PricingService._normalize(label)
        data.setdefault("cities", {})[key] = {"label": label, "value": float(multiplier)}
        PricingService._save(data)

    @staticmethod
    def clear_city_multiplier(label_or_key: str) -> None:
        data = PricingService._load()
        key = PricingService._normalize(label_or_key)
        if key in data.get("cities", {}):
            del data["cities"][key]
            PricingService._save(data)

    @staticmethod
    def get_city_entries() -> List[Dict[str, Any]]:
        data = PricingService._load()
        entries = []
        for key, entry in data.get("cities", {}).items():
            entries.append({
                "key": key,
                "label": entry.get("label") or key,
                "value": float(entry.get("value", 1.0) or 1.0)
            })
        entries.sort(key=lambda item: item["label"].lower())
        return entries