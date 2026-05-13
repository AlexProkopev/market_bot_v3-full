import json
import os
import random
import itertools
from datetime import datetime
from app.utils import get_now_msk
from app.services.postgres_store import PostgresDocumentStore
from app.services.pricing import PricingService

PRODUCTS_CACHE_FILE = "storage/products_cache.json"
PRICE_SEED_FILE = "storage/price_seed.json"
CATALOG_GENERATION_SETTINGS_FILE = "storage/catalog_generation_settings.json"
CATALOG_PRODUCTS_CACHE_DOC_KEY = "catalog_products_cache"
CATALOG_PRICE_SEED_DOC_KEY = "catalog_price_seed"
CATALOG_GENERATION_SETTINGS_DOC_KEY = "catalog_generation_settings"
CATALOG_INVENTORY_DOC_KEY = "catalog_inventory"
CATALOG_STASH_TYPES_DOC_KEY = "catalog_product_stash_types"
CATALOG_STASH_REGISTRY_DOC_KEY = "catalog_stash_registry"
CATALOG_ALL_PRODUCTS_DOC_KEY = "catalog_all_products"
DEFAULT_STASH_TYPES = ["Тайник", "Прикоп", "Магнит"]
AUTO_PRODUCT_DISTRICT_MIN = 1
AUTO_PRODUCT_DISTRICT_MAX = 4
AUTO_PRODUCT_STREET_MIN = 5
AUTO_PRODUCT_STREET_MAX = 10
STREET_LOCATION_PREFIXES = (
    "ул. ",
    "пр-т ",
    "пер. ",
    "пр-д ",
    "бул. ",
    "ш. ",
    "наб. ",
    "аллея ",
)
STREET_LOCATION_KEYWORDS = (
    "улица",
    "проспект",
    "переулок",
    "проезд",
    "бульвар",
    "шоссе",
    "набережная",
    "аллея",
)
DISTRICT_LOCATION_KEYWORDS = (
    "район",
    "мкр",
    "микрорайон",
    "квартал",
    "округ",
)
GENERIC_LOCATION_NAMES = {"центр"}
LEGACY_STASH_TYPE_ALIASES = {
    "тайник": "Тайник",
    "тайник-камень": "Тайник",
    "тайник камень": "Тайник",
    "прикоп": "Прикоп",
    "прикоп снежный": "Прикоп",
    "магнит": "Магнит",
}
DEFAULT_ALL_PRODUCTS = [
    {"id": "p1", "name": "GASH ИЗОЛЯТОР 0.5г", "base_price": 2000},
    {"id": "p4", "name": "GASH ИЗОЛЯТОР 1г", "base_price": 2900},
    {"id": "p6", "name": "MIF кр 0.5г", "base_price": 2400},
    {"id": "p7", "name": "MIF кр 1г", "base_price": 4400},
    {"id": "p10", "name": "MIF кр 0.8г", "base_price": 3700},
    {"id": "p8", "name": "Солями белый (КРБ) 0.5г", "base_price": 2400},
    {"id": "p9", "name": "Солями белый (КРБ) 1.0г", "base_price": 4400},
    {"id": "p11", "name": "Солями белый (КРБ) 0.8г", "base_price": 3400},
    {"id": "p15", "name": "Солями синий (КРС) 0.5г", "base_price": 2400},
    {"id": "p17", "name": "Солями синий (КРС) 1.0г", "base_price": 4400},
    {"id": "p27", "name": "Медок 0.25г", "base_price": 2500},
    {"id": "p24", "name": "Медок 0.5г", "base_price": 3300},
    {"id": "p25", "name": "Медок 1.0г", "base_price": 5700},
    {"id": "p26", "name": "Ромаха 1к30 0.5г", "base_price": 2600},
    {"id": "p28", "name": "Ромаха 1к30 1.0г", "base_price": 3300},
]


def _copy_default_products() -> list[dict]:
    return [dict(item) for item in DEFAULT_ALL_PRODUCTS]


def _normalize_products_payload(data) -> list[dict]:
    if not isinstance(data, list):
        return []

    normalized = []
    seen_ids = set()
    for item in data:
        if not isinstance(item, dict):
            continue
        product_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or "").strip()
        if not product_id or not name or product_id in seen_ids:
            continue
        try:
            base_price = int(item.get("base_price"))
        except (TypeError, ValueError):
            continue
        normalized.append({"id": product_id, "name": name, "base_price": base_price})
        seen_ids.add(product_id)
    return normalized


def _normalize_city_key(city_name: str | None) -> str:
    """Canonical city key used across catalog/inventory caches.

    Admin can type cities with region suffix (e.g. "Томск (Томская область)").
    User flow stores city as plain name. We keep a single key format to avoid split caches.
    """
    key = (city_name or "").strip()
    if not key:
        return ""
    if "(" in key and key.endswith(")"):
        key = key.rsplit("(", 1)[0].strip()
    return " ".join(key.lower().split())


def _resolve_legacy_city_key(data: dict, city_name: str | None) -> str:
    """Resolve existing city key in cache/inventory, falling back to canonical key."""
    normalized = _normalize_city_key(city_name)
    if normalized in data:
        return normalized

    raw = " ".join((city_name or "").strip().lower().split())
    if raw in data:
        return raw

    if normalized:
        pref = normalized + " ("
        candidates = [k for k in data.keys() if isinstance(k, str) and k.startswith(pref)]
        if candidates:
            return sorted(candidates)[0]

    return normalized


def _purge_invalid_city_cache_entries(cache: dict) -> tuple[dict, bool]:
    """Remove bogus city keys that are actually product IDs from old admin callback bugs."""
    if not isinstance(cache, dict) or not cache:
        return cache, False

    try:
        product_ids = {
            item.get("id")
            for item in CatalogService.get_all_products()
            if isinstance(item, dict) and item.get("id")
        }
    except Exception:
        return cache, False

    invalid_keys = [key for key in list(cache.keys()) if isinstance(key, str) and key in product_ids]
    if not invalid_keys:
        return cache, False

    for key in invalid_keys:
        cache.pop(key, None)

    return cache, True


def _load_price_seed() -> str:
    """Загружает текущий сид прайса. Если нет — создаёт новый."""
    if PostgresDocumentStore.is_enabled():
        data = PostgresDocumentStore.get_document(CATALOG_PRICE_SEED_DOC_KEY, {})
        if isinstance(data, dict):
            seed = str(data.get("seed") or "").strip()
            if seed:
                return seed
    if os.path.exists(PRICE_SEED_FILE):
        try:
            with open(PRICE_SEED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return str(data.get("seed", "initial"))
        except Exception:
            pass
    seed = str(random.randint(100000, 999999))
    _save_price_seed(seed)
    return seed


def _save_price_seed(seed: str) -> None:
    """Сохраняет новый сид прайса."""
    payload = {"seed": seed, "updated_at": datetime.now().isoformat()}
    if PostgresDocumentStore.is_enabled():
        PostgresDocumentStore.set_document(CATALOG_PRICE_SEED_DOC_KEY, payload)
        return
    try:
        os.makedirs("storage", exist_ok=True)
        with open(PRICE_SEED_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def _normalize_stash_type(value: str | None) -> str:
    cleaned = " ".join((value or "").strip().split())
    if not cleaned:
        return ""
    return LEGACY_STASH_TYPE_ALIASES.get(cleaned.casefold(), cleaned)


def _clean_stash_types(values: list[str] | tuple[str, ...] | None, limit: int | None = None) -> list[str]:
    cleaned = []
    for item in values or []:
        normalized = _normalize_stash_type(item)
        if not normalized or normalized in cleaned:
            continue
        cleaned.append(normalized)
        if limit is not None and len(cleaned) >= limit:
            break
    return cleaned


def _clean_district_values(values: list[str] | tuple[str, ...] | None) -> list[str]:
    cleaned = []
    for item in values or []:
        if not isinstance(item, str):
            continue
        district = " ".join(item.strip().split())
        if district and district not in cleaned:
            cleaned.append(district)
    return cleaned


def _normalize_location_name(value: str | None) -> str:
    return " ".join((value or "").strip().lower().split())


def _is_street_location(value: str | None) -> bool:
    normalized = _normalize_location_name(value)
    if not normalized or normalized in GENERIC_LOCATION_NAMES:
        return False
    if normalized.startswith(STREET_LOCATION_PREFIXES):
        return True
    return any(
        normalized.startswith(keyword + " ") or normalized.endswith(" " + keyword)
        for keyword in STREET_LOCATION_KEYWORDS
    )


def _is_district_location(value: str | None) -> bool:
    normalized = _normalize_location_name(value)
    if not normalized:
        return False
    return any(keyword in normalized for keyword in DISTRICT_LOCATION_KEYWORDS)


def _uses_street_distribution(values: list[str] | tuple[str, ...] | None) -> bool:
    locations = _clean_district_values(values)
    if not locations:
        return False

    street_count = sum(1 for item in locations if _is_street_location(item))
    if street_count == 0:
        return False

    district_count = sum(1 for item in locations if _is_district_location(item))
    meaningful_count = sum(1 for item in locations if _normalize_location_name(item) not in GENERIC_LOCATION_NAMES)
    required_street_count = max(1, (meaningful_count + 1) // 2)
    return street_count >= required_street_count and street_count > district_count


def _get_auto_product_location_limits(values: list[str] | tuple[str, ...] | None) -> tuple[int, int, bool]:
    if _uses_street_distribution(values):
        return AUTO_PRODUCT_STREET_MIN, AUTO_PRODUCT_STREET_MAX, True
    return AUTO_PRODUCT_DISTRICT_MIN, AUTO_PRODUCT_DISTRICT_MAX, False


def _get_stash_pool() -> list[str]:
    try:
        pool = _clean_stash_types(CatalogService.STASH_TYPES)
    except NameError:
        pool = []
    if len(pool) < 2:
        pool = list(DEFAULT_STASH_TYPES)
    # Preserve order while removing duplicates
    unique = []
    for value in pool:
        if value not in unique:
            unique.append(value)
    if len(unique) < 2:
        unique = list(DEFAULT_STASH_TYPES)
    return unique


def _pick_default_stash_types(rng: random.Random | None = None) -> list[str]:
    pool = _get_stash_pool()
    rng = rng or random
    if len(pool) <= 2:
        return pool[:2]
    return rng.sample(pool, k=2)


def _build_stash_combinations(pool: list[str]) -> list[list[str]]:
    unique_pool = []
    for item in pool:
        if item not in unique_pool:
            unique_pool.append(item)
    if len(unique_pool) <= 2:
        return [unique_pool[:2]]
    return [list(pair) for pair in itertools.combinations(unique_pool, 2)]


def _pick_weight_group_variants(
    rng: random.Random,
    product_ids: list[str] | tuple[str, ...],
) -> list[str]:
    variants = []
    for product_id in product_ids:
        if product_id and product_id not in variants:
            variants.append(product_id)

    if not variants:
        return []
    if len(variants) == 1:
        return list(variants)
    if len(variants) == 2:
        if rng.choice([True, False]):
            return list(variants)
        return [rng.choice(variants)]

    combinations = []
    for size in range(1, len(variants) + 1):
        for combo in itertools.combinations(variants, size):
            combinations.append(list(combo))

    return list(rng.choice(combinations))


def _remove_optional_file(path: str) -> str:
    if not os.path.exists(path):
        return "missing"
    try:
        os.remove(path)
        return "removed"
    except Exception:
        return "error"

class CatalogService:
    PRODUCTS_DB_FILE = "storage/all_products.json"
    PRODUCTS_CACHE_FILE = "storage/products_cache.json"
    INVENTORY_FILE = "storage/products_inventory.json"
    STASH_TYPES_FILE = "storage/product_stash_types.json"
    STASH_TYPES_REGISTRY_FILE = "storage/stash_types.json"
    STASH_TYPES = list(DEFAULT_STASH_TYPES)
    PRODUCT_EMOJIS = {
        "p1": "🧱",
        "p2": "🍁",
        "p4": "🧱",
        "p5": "🧱",
        "p6": "❄️",
        "p7": "❄️",
        "p8": "💎",
        "p9": "💎",
        "p10": "❄️",
        "p11": "💎",
        "p15": "💎",
        "p17": "💎",
        "p19": "💎",
        "p22": "🍁",
        "p27": "🍯",
        "p24": "🍯",
        "p25": "🍯",
        "p26": "⚡",
        "p28": "⚡",
        "p20": "💥",
        "p21": "🥥",
        "p23": "✨",
    }

    ALL_PRODUCTS = _copy_default_products()
    PRODUCT_STASH_TYPES: dict[str, list[str]] = {}
    DEFAULT_GENERATION_SETTINGS = {
        "allowed_product_ids": []
    }

    @staticmethod
    def _load_stash_registry():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(CATALOG_STASH_REGISTRY_DOC_KEY, list(DEFAULT_STASH_TYPES))
            if isinstance(data, list):
                cleaned = _clean_stash_types(data)
                if len(cleaned) >= 2:
                    CatalogService.STASH_TYPES = cleaned
                    if cleaned != data:
                        CatalogService._save_stash_registry()
                    return CatalogService.STASH_TYPES
        if os.path.exists(CatalogService.STASH_TYPES_REGISTRY_FILE):
            try:
                with open(CatalogService.STASH_TYPES_REGISTRY_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    cleaned = _clean_stash_types(data)
                    if len(cleaned) >= 2:
                        CatalogService.STASH_TYPES = cleaned
                        if cleaned != data:
                            CatalogService._save_stash_registry()
                        return CatalogService.STASH_TYPES
            except Exception:
                pass
        CatalogService.STASH_TYPES = list(DEFAULT_STASH_TYPES)
        return CatalogService.STASH_TYPES

    @staticmethod
    def _save_stash_registry():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_STASH_REGISTRY_DOC_KEY, list(CatalogService.STASH_TYPES))
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.STASH_TYPES_REGISTRY_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.STASH_TYPES, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving stash registry: {e}")

    @staticmethod
    def _load_stash_types():
        CatalogService._load_stash_registry()
        if CatalogService.PRODUCT_STASH_TYPES and not PostgresDocumentStore.is_enabled():
            return CatalogService.PRODUCT_STASH_TYPES
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(CATALOG_STASH_TYPES_DOC_KEY, {})
        elif not os.path.exists(CatalogService.STASH_TYPES_FILE):
            CatalogService.PRODUCT_STASH_TYPES = {}
            CatalogService._save_stash_types()
            return CatalogService.PRODUCT_STASH_TYPES
        else:
            try:
                with open(CatalogService.STASH_TYPES_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                data = {}

        try:
            if isinstance(data, dict):
                cleaned = {}
                changed = False
                for key, value in data.items():
                    if not isinstance(value, list):
                        changed = True
                        continue
                    cleaned_list = [t for t in _clean_stash_types(value) if t in _get_stash_pool()]
                    if len(cleaned_list) >= 2:
                        normalized_pair = cleaned_list[:2]
                        cleaned[key] = normalized_pair
                        if value != normalized_pair:
                            changed = True
                    else:
                        changed = True
                CatalogService.PRODUCT_STASH_TYPES = cleaned
                if changed:
                    CatalogService._save_stash_types()
            else:
                CatalogService.PRODUCT_STASH_TYPES = {}
        except Exception:
            CatalogService.PRODUCT_STASH_TYPES = {}
        return CatalogService.PRODUCT_STASH_TYPES

    @staticmethod
    def _save_stash_types():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_STASH_TYPES_DOC_KEY, CatalogService.PRODUCT_STASH_TYPES)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.STASH_TYPES_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.PRODUCT_STASH_TYPES, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving stash types: {e}")

    @staticmethod
    def get_available_stash_types() -> list[str]:
        CatalogService._load_stash_registry()
        return list(CatalogService.STASH_TYPES)

    @staticmethod
    def set_available_stash_types(values: list[str]) -> bool:
        CatalogService._load_stash_registry()
        cleaned = _clean_stash_types(values)
        if len(cleaned) < 2:
            return False
        CatalogService.STASH_TYPES = cleaned
        CatalogService._save_stash_registry()
        CatalogService.resync_product_stash_types(force_rebalance=True)
        return True

    @staticmethod
    def resync_product_stash_types(force_rebalance: bool = False):
        """Align product stash pairs with the current global pool and refresh caches."""
        CatalogService.get_all_products()
        CatalogService._ensure_stash_for_all_products(force_rebalance=force_rebalance)
        CatalogService._refresh_cache_stash_types()

    @staticmethod
    def add_stash_type(value: str) -> bool:
        if not isinstance(value, str):
            return False
        CatalogService._load_stash_registry()
        value = _normalize_stash_type(value)
        if not value:
            return False
        current = CatalogService.get_available_stash_types()
        if value in current:
            return False
        current.append(value)
        return CatalogService.set_available_stash_types(current)

    @staticmethod
    def remove_stash_type(value: str) -> bool:
        CatalogService._load_stash_registry()
        value = _normalize_stash_type(value)
        current = CatalogService.get_available_stash_types()
        if value not in current:
            return False
        if len(current) <= 2:
            return False
        current = [item for item in current if item != value]
        return CatalogService.set_available_stash_types(current)

    @staticmethod
    def _ensure_stash_pair(product_id: str, force_rebalance: bool = False):
        CatalogService._load_stash_types()
        current = CatalogService.PRODUCT_STASH_TYPES.get(product_id)
        normalized_current = [item for item in _clean_stash_types(current, limit=2) if item in _get_stash_pool()]
        if not force_rebalance and len(normalized_current) >= 2:
            if current != normalized_current:
                CatalogService.PRODUCT_STASH_TYPES[product_id] = normalized_current
                CatalogService._save_stash_types()
            return
        price_seed = _load_price_seed()
        rng = random.Random(f"stash::{product_id}::{price_seed}")
        CatalogService.PRODUCT_STASH_TYPES[product_id] = _pick_default_stash_types(rng)
        CatalogService._save_stash_types()

    @staticmethod
    def _ensure_stash_for_all_products(force_rebalance: bool = False):
        CatalogService._load_stash_types()
        missing = False
        CatalogService._load_stash_registry()
        price_seed = _load_price_seed()
        for product in CatalogService.ALL_PRODUCTS:
            p_id = product.get("id") if isinstance(product, dict) else None
            if not p_id:
                continue
            current = CatalogService.PRODUCT_STASH_TYPES.get(p_id)
            normalized_current = [item for item in _clean_stash_types(current, limit=2) if item in _get_stash_pool()]
            if force_rebalance or len(normalized_current) < 2:
                rng = random.Random(f"stash::{p_id}::{price_seed}")
                CatalogService.PRODUCT_STASH_TYPES[p_id] = _pick_default_stash_types(rng)
                missing = True
            elif current != normalized_current:
                CatalogService.PRODUCT_STASH_TYPES[p_id] = normalized_current
                missing = True
        if missing:
            CatalogService._save_stash_types()
    
    @staticmethod
    def _load_all_products():
        if PostgresDocumentStore.is_enabled():
            payload = PostgresDocumentStore.get_document(CATALOG_ALL_PRODUCTS_DOC_KEY, _copy_default_products())
            normalized = _normalize_products_payload(payload)
            CatalogService.ALL_PRODUCTS = normalized or _copy_default_products()
        elif os.path.exists(CatalogService.PRODUCTS_DB_FILE):
            try:
                with open(CatalogService.PRODUCTS_DB_FILE, "r", encoding="utf-8") as f:
                    payload = json.load(f)
                normalized = _normalize_products_payload(payload)
                CatalogService.ALL_PRODUCTS = normalized or _copy_default_products()
            except Exception:
                CatalogService.ALL_PRODUCTS = _copy_default_products()
        else:
            CatalogService.ALL_PRODUCTS = _copy_default_products()
        CatalogService._ensure_core_products()
        CatalogService._ensure_stash_for_all_products()

    @staticmethod
    def _save_all_products():
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_ALL_PRODUCTS_DOC_KEY, CatalogService.ALL_PRODUCTS)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.PRODUCTS_DB_FILE, "w", encoding="utf-8") as f:
                json.dump(CatalogService.ALL_PRODUCTS, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving all products: {e}")

    @staticmethod
    def _ensure_core_products():
        """Make sure essential items are always present in the catalog file."""
        core_defaults = [
                {"id": "p1", "name": "GASH ИЗОЛЯТОР 0.5г", "base_price": 2000},
                {"id": "p4", "name": "GASH ИЗОЛЯТОР 1г", "base_price": 2900}, 
                {"id": "p6", "name": "MIF кр 0.5г", "base_price": 2400},
                {"id": "p7", "name": "MIF кр 1г", "base_price": 4400},
                {"id": "p10", "name": "MIF кр 0.8г", "base_price": 3700},
                {"id": "p8", "name": "Солями белый (КРБ) 0.5г", "base_price": 2400},
                {"id": "p9", "name": "Солями белый (КРБ) 1.0г", "base_price": 4400},
                {"id": "p11", "name": "Солями белый (КРБ) 0.8г", "base_price": 3400},
                {"id": "p15", "name": "Солями синий (КРС) 0.5г", "base_price": 2400},
                {"id": "p17", "name": "Солями синий (КРС) 1.0г", "base_price": 4400},
                {"id": "p27", "name": "Медок 0.25г", "base_price": 2500},
                {"id": "p24", "name": "Медок 0.5г", "base_price": 3300},
                {"id": "p25", "name": "Медок 1.0г", "base_price": 5700},
                {"id": "p26", "name": "Ромаха 1к30 0.5г", "base_price": 2600},
                {"id": "p28", "name": "Ромаха 1к30 1.0г", "base_price": 3300},
            ]

        if not isinstance(CatalogService.ALL_PRODUCTS, list):
            CatalogService.ALL_PRODUCTS = []

        existing_ids = {item.get("id") for item in CatalogService.ALL_PRODUCTS if isinstance(item, dict)}
        order_map = {item["id"]: idx for idx, item in enumerate(core_defaults)}
        changed = False
        for item in core_defaults:
            if item["id"] not in existing_ids:
                CatalogService.ALL_PRODUCTS.append(item)
                changed = True

        if changed:
            fallback_positions = {
                item.get("id"): idx
                for idx, item in enumerate(CatalogService.ALL_PRODUCTS)
                if isinstance(item, dict) and item.get("id")
            }

            def _sort_key(prod):
                pid = prod.get("id") if isinstance(prod, dict) else None
                if pid in order_map:
                    return order_map[pid]
                return len(order_map) + fallback_positions.get(pid, 9999)

            CatalogService.ALL_PRODUCTS.sort(key=_sort_key)
            CatalogService._save_all_products()

    @staticmethod
    def get_product_emoji(product_id: str, name: str | None = None) -> str:
        emoji = CatalogService.PRODUCT_EMOJIS.get(product_id)
        if emoji:
            return emoji

        lookup = (name or "").lower()
        keyword_map = (
            ("шиш", "🍁"),
            ("гаш", "🧱"),
            ("меф", "❄️"),
            ("крис", "💎"),
            ("кокс", "🥥"),
            ("экстази", "✨"),
            ("амф", "💥"),
            ("мёд", "🍯"),
            ("шок", "⚡"),
        )
        for marker, symbol in keyword_map:
            if marker in lookup:
                return symbol
        return "🛒"

    @staticmethod
    def get_product_stash_types(product_id: str) -> list[str]:
        CatalogService.get_all_products()
        CatalogService._ensure_stash_pair(product_id)
        stash = CatalogService.PRODUCT_STASH_TYPES.get(product_id, [])
        if isinstance(stash, list) and len(stash) >= 2:
            return stash[:2]
        return _pick_default_stash_types()

    @staticmethod
    def get_district_stash_type_map(city_name: str, product_id: str, districts: list[str] | None) -> dict[str, list[str]]:
        cleaned_districts = _clean_district_values(districts)
        if not cleaned_districts:
            return {}

        pool = CatalogService.get_available_stash_types()
        combinations = _build_stash_combinations(pool)
        if not combinations:
            fallback = _pick_default_stash_types()
            return {district: fallback for district in cleaned_districts}

        district_seed = _load_price_seed()
        rng = random.Random(f"district-stash::{_normalize_city_key(city_name)}::{product_id}::{district_seed}")
        shuffled = list(combinations)
        rng.shuffle(shuffled)

        mapping = {}
        for index, district in enumerate(cleaned_districts):
            mapping[district] = list(shuffled[index % len(shuffled)])
        return mapping

    @staticmethod
    def get_district_stash_types(city_name: str, product_id: str, district_name: str, districts: list[str] | None = None) -> list[str]:
        district_map = CatalogService.get_district_stash_type_map(city_name, product_id, districts or [district_name])
        selected = district_map.get((district_name or "").strip())
        if isinstance(selected, list) and len(selected) >= 2:
            return selected[:2]
        return _pick_default_stash_types()

    @staticmethod
    def set_product_stash_types(product_id: str, types: list[str]) -> bool:
        CatalogService.get_all_products()
        CatalogService._load_stash_registry()
        CatalogService._load_stash_types()
        cleaned = []
        for t in types:
            if isinstance(t, str) and t in CatalogService.STASH_TYPES and t not in cleaned:
                cleaned.append(t)
            if len(cleaned) == 2:
                break
        if len(cleaned) < 2:
            return False
        cleaned = cleaned[:2]
        CatalogService.PRODUCT_STASH_TYPES[product_id] = cleaned
        CatalogService._save_stash_types()
        CatalogService._update_cache_stash_types(product_id, cleaned)
        return True

    @staticmethod
    def _refresh_cache_stash_types():
        cache = CatalogService._load_cache()
        changed = False
        for city_key, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            ids = entry.get("ids")
            if not isinstance(ids, list):
                continue
            stash_map = {}
            for pid in ids:
                stash_map[pid] = CatalogService.get_product_stash_types(pid)
            if entry.get("stash_types") != stash_map:
                entry["stash_types"] = stash_map
                changed = True
        if changed:
            CatalogService._write_cache(cache)

    @staticmethod
    def create_new_product(name: str, price: int):
        # Генерируем ID
        CatalogService._load_all_products()
        new_id = f"p{random.randint(1000, 99999)}"
        # Проверяем уникальность
        while any(p['id'] == new_id for p in CatalogService.ALL_PRODUCTS):
            new_id = f"p{random.randint(1000, 99999)}"
            
        new_product = {
            "id": new_id,
            "name": name,
            "base_price": price,
        }
        CatalogService.ALL_PRODUCTS.append(new_product)
        CatalogService._save_all_products()
        CatalogService._ensure_stash_pair(new_id)
        return new_product

    @staticmethod
    def get_all_products():
        if PostgresDocumentStore.is_enabled() or not CatalogService.ALL_PRODUCTS:
            CatalogService._load_all_products()
        return CatalogService.ALL_PRODUCTS

    @staticmethod
    def _load_generation_settings() -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(
                CATALOG_GENERATION_SETTINGS_DOC_KEY,
                CatalogService.DEFAULT_GENERATION_SETTINGS.copy(),
            )
            if not isinstance(data, dict):
                return CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
            merged = CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
            merged.update(data)
            raw_ids = merged.get("allowed_product_ids")
            if not isinstance(raw_ids, list):
                merged["allowed_product_ids"] = []
            else:
                merged["allowed_product_ids"] = [
                    str(pid).strip() for pid in raw_ids if isinstance(pid, str) and str(pid).strip()
                ]
            return merged
        if not os.path.exists(CATALOG_GENERATION_SETTINGS_FILE):
            return CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
        try:
            with open(CATALOG_GENERATION_SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
            merged = CatalogService.DEFAULT_GENERATION_SETTINGS.copy()
            merged.update(data)
            raw_ids = merged.get("allowed_product_ids")
            if not isinstance(raw_ids, list):
                merged["allowed_product_ids"] = []
            else:
                merged["allowed_product_ids"] = [
                    str(pid).strip() for pid in raw_ids if isinstance(pid, str) and str(pid).strip()
                ]
            return merged
        except Exception:
            return CatalogService.DEFAULT_GENERATION_SETTINGS.copy()

    @staticmethod
    def _save_generation_settings(settings: dict) -> bool:
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_GENERATION_SETTINGS_DOC_KEY, settings)
            return True
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CATALOG_GENERATION_SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, ensure_ascii=False, indent=2)
            return True
        except Exception:
            return False

    @staticmethod
    def get_allowed_generation_product_ids() -> set[str]:
        settings = CatalogService._load_generation_settings()
        raw_ids = settings.get("allowed_product_ids")
        if not isinstance(raw_ids, list):
            return set()
        return {str(pid).strip() for pid in raw_ids if isinstance(pid, str) and str(pid).strip()}

    @staticmethod
    def set_allowed_generation_product_ids(product_ids: list[str]) -> bool:
        settings = CatalogService._load_generation_settings()
        normalized = []
        seen = set()
        for pid in product_ids or []:
            value = str(pid).strip()
            if not value or value in seen:
                continue
            seen.add(value)
            normalized.append(value)
        settings["allowed_product_ids"] = normalized
        return CatalogService._save_generation_settings(settings)

    @staticmethod
    def toggle_allowed_generation_product_id(product_id: str) -> bool:
        pid = (product_id or "").strip()
        if not pid:
            return False
        current = CatalogService.get_allowed_generation_product_ids()
        if pid in current:
            current.remove(pid)
        else:
            current.add(pid)
        return CatalogService.set_allowed_generation_product_ids(sorted(current))

    @staticmethod
    def reset_generation_product_filter() -> bool:
        # Empty list means "all products allowed"
        return CatalogService.set_allowed_generation_product_ids([])

    @staticmethod
    def _apply_generation_product_filter(
        generated_products: list[dict],
        rng: random.Random,
        population: int,
    ) -> list[dict]:
        allowed_ids = CatalogService.get_allowed_generation_product_ids()
        if not allowed_ids:
            return generated_products

        filtered = [p for p in generated_products if p.get("id") in allowed_ids]
        if filtered:
            return filtered

        # If branch-specific pool doesn't intersect with allowed set, fallback to allowed pool.
        allowed_pool = [p for p in CatalogService.ALL_PRODUCTS if p.get("id") in allowed_ids]
        if not allowed_pool:
            return generated_products

        if 0 < population < 100000:
            min_take, max_take = 2, 5
        elif 100000 <= population < 500000:
            min_take, max_take = 3, 7
        else:
            min_take, max_take = 4, 9

        max_take = min(max_take, len(allowed_pool))
        min_take = min(min_take, max_take)
        if max_take <= 0:
            return generated_products

        take = rng.randint(min_take, max_take) if min_take < max_take else max_take
        picked = rng.sample(allowed_pool, k=take)
        return sorted(picked, key=lambda x: CatalogService.ALL_PRODUCTS.index(x))

    @staticmethod
    def _load_cache():
        if PostgresDocumentStore.is_enabled():
            cache = PostgresDocumentStore.get_document(CATALOG_PRODUCTS_CACHE_DOC_KEY, {})
            if not isinstance(cache, dict):
                return {}
            cache, changed = _purge_invalid_city_cache_entries(cache)
            if changed:
                CatalogService._write_cache(cache)
            return cache
        if not os.path.exists(PRODUCTS_CACHE_FILE):
            return {}
        try:
            with open(PRODUCTS_CACHE_FILE, "r", encoding="utf-8") as f:
                cache = json.load(f)
            cache, changed = _purge_invalid_city_cache_entries(cache)
            if changed:
                CatalogService._write_cache(cache)
            return cache
        except Exception:
            return {}

    @staticmethod
    def _write_cache(cache: dict):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_PRODUCTS_CACHE_DOC_KEY, cache)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.PRODUCTS_CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving products cache: {e}")

    @staticmethod
    def _save_to_cache(city_name: str, product_ids: list):
        cache = CatalogService._load_cache()
        city_key = _normalize_city_key(city_name)
        stash_map = {pid: CatalogService.get_product_stash_types(pid) for pid in product_ids}
        cache[city_key] = {
            "ids": product_ids,
            "stash_types": stash_map,
            "updated_at": get_now_msk().strftime("%d.%m.%Y %H:%M")
        }
        CatalogService._write_cache(cache)

    @staticmethod
    def _update_cache_stash_types(product_id: str, stash_types: list[str]):
        cache = CatalogService._load_cache()
        changed = False
        for city_key, entry in cache.items():
            if not isinstance(entry, dict):
                continue
            ids = entry.get("ids")
            if isinstance(ids, list) and product_id in ids:
                stash = entry.get("stash_types")
                if not isinstance(stash, dict):
                    stash = {}
                if stash.get(product_id) != stash_types:
                    stash[product_id] = stash_types
                    entry["stash_types"] = stash
                    changed = True
        if changed:
            CatalogService._write_cache(cache)

    @staticmethod
    def add_product_to_city(city_name: str, product_id: str):
        CatalogService.get_all_products() # Ensure loaded
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_cache()
        source_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(source_key)

        if source_key and source_key != city_key and data is not None:
            cache[city_key] = data
            cache.pop(source_key, None)
            CatalogService._write_cache(cache)
        
        ids = []
        if isinstance(data, dict):
            ids = data.get("ids", [])
        elif isinstance(data, list):
            ids = data
            
        if product_id not in ids:
            ids.append(product_id)
            # Сортируем по порядку в общей базе
            ids.sort(key=lambda x: next((i for i, p in enumerate(CatalogService.ALL_PRODUCTS) if p["id"] == x), 999))
            CatalogService._save_to_cache(city_key, ids)
            return True
        return False

    @staticmethod
    def _load_inventory():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(CATALOG_INVENTORY_DOC_KEY, {})
            cleaned = CatalogService._normalize_inventory_payload(data)
            if cleaned != data:
                CatalogService._save_inventory(cleaned)
            return cleaned
        if not os.path.exists(CatalogService.INVENTORY_FILE):
            return {}
        try:
            with open(CatalogService.INVENTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            cleaned = CatalogService._normalize_inventory_payload(data)
            if cleaned != data:
                CatalogService._save_inventory(cleaned)
            return cleaned
        except:
            return {}

    @staticmethod
    def _save_inventory(data):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_INVENTORY_DOC_KEY, data)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CatalogService.INVENTORY_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving inventory: {e}")

    @staticmethod
    def _get_city_district_pool(city_name: str) -> list[str]:
        try:
            from app.services.geo import GeoService

            cache = GeoService._load_cache()
            target = _normalize_city_key(city_name)
            if not isinstance(cache, dict) or not target:
                return []

            matched = []
            for raw_key, districts in cache.items():
                if not isinstance(raw_key, str):
                    continue
                raw_norm = " ".join(raw_key.strip().lower().split())
                if raw_norm == target or raw_norm.startswith(target + " ("):
                    for district in _clean_district_values(districts):
                        if district not in matched:
                            matched.append(district)
            return matched
        except Exception:
            return []

    @staticmethod
    def _pick_auto_product_districts(
        city_name: str,
        product_id: str,
        all_districts: list[str] | None = None,
        previous_selection: list[str] | None = None,
        force_change: bool = False,
    ) -> list[str]:
        districts = _clean_district_values(all_districts or CatalogService._get_city_district_pool(city_name))
        if not districts:
            return []
        if len(districts) == 1:
            return districts

        district_seed = _load_price_seed()
        rng = random.Random(f"districts::{_normalize_city_key(city_name)}::{product_id}::{district_seed}")
        min_count, max_count, is_street_pool = _get_auto_product_location_limits(districts)
        counts = list(range(min(min_count, len(districts)), min(max_count, len(districts)) + 1))
        count_weights = {1: 1, 2: 4, 3: 5, 4: 2, 5: 1}
        weights = [1] * len(counts) if is_street_pool else [count_weights.get(value, 1) for value in counts]
        count = rng.choices(counts, weights=weights, k=1)[0]
        sampled = set(rng.sample(districts, count))
        selected = [district for district in districts if district in sampled]

        previous = [district for district in _clean_district_values(previous_selection) if district in districts]
        if force_change and previous and selected == previous and len(districts) > count:
            for attempt in range(1, len(districts) + 1):
                alt_rng = random.Random(
                    f"districts::{_normalize_city_key(city_name)}::{product_id}::{district_seed}::{attempt}"
                )
                alt_sampled = set(alt_rng.sample(districts, count))
                alternative = [district for district in districts if district in alt_sampled]
                if alternative != previous:
                    return alternative

        return selected

    @staticmethod
    def _normalize_product_districts(
        city_name: str,
        product_id: str,
        districts: list[str] | None,
        all_districts: list[str] | None = None,
    ) -> list[str]:
        city_districts = _clean_district_values(all_districts or CatalogService._get_city_district_pool(city_name))
        selected = _clean_district_values(districts)
        if city_districts:
            selected = [district for district in selected if district in city_districts]
        if not selected:
            return []
        _, max_count, _ = _get_auto_product_location_limits(city_districts)
        if city_districts and len(city_districts) > max_count:
            if len(selected) > max_count or len(selected) == len(city_districts):
                return CatalogService._pick_auto_product_districts(city_name, product_id, city_districts)
        return selected

    @staticmethod
    def _normalize_inventory_payload(data: dict) -> dict:
        if not isinstance(data, dict):
            return {}

        cleaned_inventory = {}
        for city_name, product_map in data.items():
            city_key = _normalize_city_key(city_name)
            if not city_key or not isinstance(product_map, dict):
                continue

            city_districts = CatalogService._get_city_district_pool(city_name)
            cleaned_products = {}
            for product_id, districts in product_map.items():
                normalized = CatalogService._normalize_product_districts(city_name, str(product_id), districts, city_districts)
                if normalized:
                    cleaned_products[str(product_id)] = normalized

            if cleaned_products:
                cleaned_inventory[city_key] = cleaned_products

        return cleaned_inventory

    @staticmethod
    def get_available_districts_for_product(city_name: str, product_id: str, all_districts: list[str] | None = None) -> list[str]:
        explicit_districts = CatalogService.get_product_districts(city_name, product_id)
        if explicit_districts:
            return explicit_districts
        return CatalogService._pick_auto_product_districts(city_name, product_id, all_districts)

    @staticmethod
    def resync_product_districts(force_rebalance: bool = False) -> bool:
        data = CatalogService._load_inventory()
        if not data:
            return False

        changed = False
        for city_name, product_map in list(data.items()):
            if not isinstance(product_map, dict):
                continue
            city_districts = CatalogService._get_city_district_pool(city_name)
            for product_id, districts in list(product_map.items()):
                if force_rebalance:
                    normalized = CatalogService._pick_auto_product_districts(
                        city_name,
                        str(product_id),
                        city_districts,
                        previous_selection=districts,
                        force_change=True,
                    )
                else:
                    normalized = CatalogService._normalize_product_districts(city_name, str(product_id), districts, city_districts)

                if normalized:
                    if normalized != districts:
                        product_map[str(product_id)] = normalized
                        changed = True
                else:
                    if str(product_id) in product_map:
                        product_map.pop(str(product_id), None)
                        changed = True

            if not product_map:
                data.pop(city_name, None)
                changed = True

        if changed:
            CatalogService._save_inventory(data)
        return changed

    @staticmethod
    def set_product_districts(city_name: str, product_id: str, districts: list):
        """Sets explicit availability of a product in specific districts."""
        data = CatalogService._load_inventory()
        city_key = _normalize_city_key(city_name)

        source_key = _resolve_legacy_city_key(data, city_name)
        if source_key and source_key != city_key and source_key in data:
            data[city_key] = data.get(source_key, {})
            data.pop(source_key, None)
        
        if city_key not in data:
            data[city_key] = {}

        normalized = CatalogService._normalize_product_districts(city_name, product_id, districts)
        data[city_key][product_id] = normalized
        CatalogService._save_inventory(data)

    @staticmethod
    def get_product_districts(city_name: str, product_id: str):
        """Returns list of districts if explicitly set, else None."""
        data = CatalogService._load_inventory()
        city_key = _resolve_legacy_city_key(data, city_name)
        
        if city_key in data and product_id in data[city_key]:
            normalized = CatalogService._normalize_product_districts(city_name, product_id, data[city_key][product_id])
            if normalized != data[city_key][product_id]:
                data[city_key][product_id] = normalized
                CatalogService._save_inventory(data)
            return normalized or None
        return None
        
    @staticmethod
    def remove_product_from_city(city_name: str, product_id: str):
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_cache()
        source_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(source_key)

        if source_key and source_key != city_key and data is not None:
            cache[city_key] = data
            cache.pop(source_key, None)
            CatalogService._write_cache(cache)
        
        ids = []
        if isinstance(data, dict):
            ids = data.get("ids", [])
        elif isinstance(data, list):
            ids = data
            
        if product_id in ids:
            ids.remove(product_id)
            CatalogService._save_to_cache(city_key, ids)
            return True
        return False

    @staticmethod
    def get_week_day():
        return get_now_msk().weekday()

    @staticmethod
    def force_refresh_products(city_name: str):
        """Принудительно удаляет кэш для города, заставляя перегенерировать список"""
        cache = CatalogService._load_cache()
        city_key = _resolve_legacy_city_key(cache, city_name)
        if city_key in cache:
            del cache[city_key]
            try:
                CatalogService._write_cache(cache)
                return True
            except:
                pass
        return False

    @staticmethod
    def get_last_update_time(city_name: str):
        cache = CatalogService._load_cache()
        city_key = _resolve_legacy_city_key(cache, city_name)
        data = cache.get(city_key)
        if isinstance(data, dict):
            return data.get("updated_at")
        return None

    @staticmethod
    def clear_entire_cache():
        """Полная очистка кэша товаров для всех городов."""
        # Генерируем новый сид — при следующей генерации прайс будет другим
        new_seed = str(random.randint(100000, 999999))
        _save_price_seed(new_seed)
        print(f"[CatalogSeed] Новый сид прайса: {new_seed}")
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(CATALOG_INVENTORY_DOC_KEY, {})
            PostgresDocumentStore.set_document(CATALOG_STASH_TYPES_DOC_KEY, {})
            PostgresDocumentStore.set_document(CATALOG_PRODUCTS_CACHE_DOC_KEY, {})
            CatalogService.PRODUCT_STASH_TYPES = {}
            inventory_status = "cleared"
            stash_status = "cleared"
            cache_status = "cleared"
        else:
            inventory_status = _remove_optional_file(CatalogService.INVENTORY_FILE)
            stash_status = _remove_optional_file(CatalogService.STASH_TYPES_FILE)
            cache_status = _remove_optional_file(PRODUCTS_CACHE_FILE)

        return cache_status, inventory_status, stash_status

    @staticmethod
    def _get_city_multiplier(city_name: str) -> float:
        cache_key = None
        region_label = None
        try:
            from app.services.geo import GeoService
            districts_cache = GeoService._load_cache()
            city_key_lower = _normalize_city_key(city_name)
            regional_cache_key = None
            empty_region_cache_key = None
            plain_cache_key = None

            for k in districts_cache.keys():
                if not isinstance(k, str):
                    continue
                normalized_key = " ".join(k.strip().lower().split())
                if normalized_key == city_key_lower:
                    plain_cache_key = k
                    continue
                if normalized_key.startswith(city_key_lower + " ("):
                    start = k.find("(") + 1
                    end = k.rfind(")")
                    region_part = ""
                    if start > 0 and end > start:
                        region_part = k[start:end].strip()
                    if region_part:
                        regional_cache_key = k
                        break
                    if empty_region_cache_key is None:
                        empty_region_cache_key = k

            cache_key = regional_cache_key or plain_cache_key or empty_region_cache_key

            if cache_key and "(" in cache_key and ")" in cache_key:
                start = cache_key.find("(") + 1
                end = cache_key.rfind(")")
                if start > 0 and end > start:
                    region_label = cache_key[start:end].strip()

            resolved = PricingService.get_multiplier(city_name, region_label, cache_key)
            if resolved:
                return float(resolved)

        except Exception as e:
            print(f"Multiplier error: {e}")

        key_for_match = (cache_key or city_name).lower()

        if any(x in key_for_match for x in ["ханты", "хмао", "югра"]):
            return 1.5
        if "забайкал" in key_for_match:
            return 1.3
        if "сахалин" in key_for_match:
            return 1.8
        if "алтай" in key_for_match:
            return 1.3
        if any(x in key_for_match for x in ["кабардино", "кбр"]):
            return 1.4
        if any(x in key_for_match for x in ["красноярск", "новосибир", "новосиб"]):
            return 1.3
        if "иркутск" in key_for_match:
            return 1.3
        if "якутск" in key_for_match or "якутия" in key_for_match:
            return 1.3

        return 1.0

    @staticmethod
    def get_products_for_city(city_name: str, population: int = 0):
        """
        Возвращает закрепленный за городом список товаров.
        Если список не создан - генерирует случайный набор и закрепляет его.
        population uses to filter luxury items for small cities (< 1 mln).
        """
        CatalogService.get_all_products() # Ensure loaded
        city_key = _normalize_city_key(city_name)
        cache = CatalogService._load_cache()

        source_key = _resolve_legacy_city_key(cache, city_name)
        cache_data = cache.get(source_key)

        if source_key and source_key != city_key and cache_data is not None:
            cache[city_key] = cache_data
            cache.pop(source_key, None)
            CatalogService._write_cache(cache)
        
        # Поддержка старого формата кэша (просто список ID) и нового (dict)
        if isinstance(cache_data, list):
            saved_ids = cache_data
            cached_stash = {}
        elif isinstance(cache_data, dict):
            saved_ids = cache_data.get("ids", [])
            cached_stash = cache_data.get("stash_types", {}) if isinstance(cache_data.get("stash_types"), dict) else {}
        else:
            saved_ids = None
            cached_stash = {}
        
        if saved_ids:
            # Если есть в кэше - собираем объекты по ID
            current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in saved_ids]
        else:
            # === ГЕНЕРАЦИЯ НОВОГО НАБОРА ===
            price_seed = _load_price_seed()
            rng = random.Random(f"{city_key}::{price_seed}")
            print(f"[CatalogSeed] Генерирую прайс для '{city_name}' с сидом '{price_seed}'")

            if 0 < population < 100000:
                # 1. МАЛЕНЬКИЙ ГОРОД (< 100к)
                # Базовые позиции дополняем более лёгкими фасовками, а весовые линейки
                # гаша, медка и ромахи выбираем случайно по поднаборам весов.
                small_city_ids = list(dict.fromkeys(
                    _pick_weight_group_variants(rng, ["p1", "p4"])
                    + ["p7", "p9", "p17"]
                    + _pick_weight_group_variants(rng, ["p27", "p24", "p25"])
                    + _pick_weight_group_variants(rng, ["p26", "p28"])
                ))
                lightweight_pool = ["p6", "p8", "p10", "p11", "p15", "p24"]
                if lightweight_pool:
                    max_take = min(4, len(lightweight_pool))
                    min_take = 2 if len(lightweight_pool) >= 2 else 1
                    extra_count = rng.randint(min_take, max_take)
                    for candidate in rng.sample(lightweight_pool, k=extra_count):
                        if candidate not in small_city_ids:
                            small_city_ids.append(candidate)
                current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in small_city_ids]

            elif 100000 <= population < 500000:
                # 2. СРЕДНИЙ ГОРОД (100к - 500к)
                # Допускаем веса 0.5г и 1.0г. 
                # Без Luxury (Кокаин, Экстази). Амфетамин возможен.
                medium_ids = list(dict.fromkeys(
                    ["p6", "p7"]
                    + _pick_weight_group_variants(rng, ["p1", "p4"])
                    + _pick_weight_group_variants(rng, ["p27", "p24", "p25"])
                    + _pick_weight_group_variants(rng, ["p26", "p28"])
                ))
                
                # СК: 0.5 и 1.0
                sk_options = [
                   ["p8", "p15"], # 0.5
                   ["p9", "p17"]  # 1.0
                ]
                selected_sk = [rng.choice(opts) for opts in sk_options]
                medium_ids.extend(selected_sk)
                
                # Амфетамин 1г (p20) - 50% шанс
                if rng.random() > 0.5:
                    medium_ids.append("p20")

                current_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in medium_ids]
                current_products = sorted(current_products, key=lambda x: CatalogService.ALL_PRODUCTS.index(x))

            else:
                # 3. БОЛЬШОЙ ГОРОД (>= 500к)
                # Генерируем расширенный прайс (не полный, но больше выбора)
                
                # 1. Основные категории (Меф, плюс случайные сочетания весов по ключевым линейкам)
                base_mandatory = list(dict.fromkeys(
                    ["p6", "p7"]
                    + _pick_weight_group_variants(rng, ["p1", "p4"])
                    + _pick_weight_group_variants(rng, ["p27", "p24", "p25"])
                    + _pick_weight_group_variants(rng, ["p26", "p28"])
                ))

                # 3. Обязательная СК (Кристаллы) разных весов
                # 0.5г, 0.8г, 1.0г, 2.0г
                sk_options = [
                    ["p8", "p15"],         # 0.5г
                    ["p10", "p11"],        # 0.8г
                    ["p9", "p17"],         # 1.0г
                ]
                
                selected_sk = [rng.choice(opts) for opts in sk_options]
                
                mandatory_ids = base_mandatory + selected_sk
                mandatory_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in mandatory_ids]
                
                # Товары для случайной выборки (Экстази, Кокаин, Амфетамин)
                other_ids = ["p20", "p21", "p23"] # Амф 1г, Кокс 0.5, XTC
                
                other_products = [p for p in CatalogService.ALL_PRODUCTS if p["id"] in other_ids]
                
                # Собираем РАСШИРЕННЫЙ список (Больше выбора, но не весь каталог)
                # Обязательные позиции + несколько случайных из премиум-сегмента
                
                final_products = list(mandatory_products)
                
                # И добавляем 2-3 случайных "эксклюзива" (Кокаин, Экстази, Амфетамин)
                if other_products:
                    # Случайное количество доп. позиций (от 2 до максимума доступных)
                    # Если доступно мало, берем сколько есть. Если много - рандом 2..3
                    max_take = min(len(other_products), 3)
                    min_take = min(len(other_products), 2)
                    
                    count_to_take = rng.randint(min_take, max_take)
                    extras = rng.sample(other_products, count_to_take)
                    final_products.extend(extras)

                current_products = sorted(final_products, key=lambda x: CatalogService.ALL_PRODUCTS.index(x))
            
            current_products = CatalogService._apply_generation_product_filter(
                current_products,
                rng,
                population,
            )

            
            # Сохраняем ID-шники в кэш
            saved_ids = [p["id"] for p in current_products]
            CatalogService._save_to_cache(city_key, saved_ids)
            cached_stash = {pid: CatalogService.get_product_stash_types(pid) for pid in saved_ids}

        display_list = []
        multiplier = CatalogService._get_city_multiplier(city_name)
        
        for p in current_products:
            price = int(p["base_price"] * multiplier)
            display_list.append({
                "id": p["id"],
                "name": p["name"],
                "price": price,
                "desc": f"Цена для г. {city_name} (актуальна сегодня)",
                "emoji": CatalogService.get_product_emoji(p["id"], p["name"]),
                "stash_types": cached_stash.get(p["id"]) or CatalogService.get_product_stash_types(p["id"])
            })
            
        return display_list

    @staticmethod
    def get_product_by_id(p_id, city_name=None):
        CatalogService.get_all_products() # Load if needed
        # Ищем в общей базе
        for p in CatalogService.ALL_PRODUCTS:
            if p["id"] == p_id:
                price = p["base_price"]
                if city_name:
                    multiplier = CatalogService._get_city_multiplier(city_name)
                    price = int(price * multiplier)
                    # Round to 100
                    price = (price // 100) * 100
                    
                return {
                    "id": p["id"],
                    "name": p["name"],
                    "price": price,
                    "desc": "Актуальный прайс",
                    "emoji": CatalogService.get_product_emoji(p["id"], p["name"]),
                    "stash_types": CatalogService.get_product_stash_types(p["id"]),
                }
        return None

