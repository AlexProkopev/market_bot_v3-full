import json
import os
import random
import asyncio
import re
import time
import math
import requests
from concurrent.futures import ThreadPoolExecutor
from app.config import OPENAI_API_KEY, YANDEX_API_KEY, DGIS_API_KEY
from app.services.postgres_store import PostgresDocumentStore

CACHE_FILE = "storage/districts_cache.json"
CITY_POPULATION_FILE = "storage/city_population_cache.json"
GEO_DISTRICTS_CACHE_DOC_KEY = "geo_districts_cache"
GEO_CITY_POPULATION_DOC_KEY = "geo_city_population_cache"

class GeoService:
    _build_marker_logged = False

    REGION_STOPWORDS = {
        "область",
        "обл",
        "обл.",
        "край",
        "республика",
        "респ",
        "респ.",
        "автономный",
        "автономная",
        "округ",
        "ао",
        "федеральный",
        "фо",
        "г",
        "г.",
    }

    @staticmethod
    def _normalize_city_name(value: str | None) -> str:
        text = (value or "").strip().lower()
        if not text:
            return ""
        if "(" in text and text.endswith(")"):
            text = text.rsplit("(", 1)[0].strip()
        return " ".join(text.split())

    @staticmethod
    def _normalize_region_name(value: str | None) -> str:
        text = " ".join((value or "").strip().lower().split())
        if not text:
            return ""
        text = text.replace("-", " ")
        tokens = [token for token in text.split() if token and token not in GeoService.REGION_STOPWORDS]
        return " ".join(tokens)

    @staticmethod
    def _region_matches(cache_region: str | None, target_region: str | None) -> bool:
        left = GeoService._normalize_region_name(cache_region)
        right = GeoService._normalize_region_name(target_region)
        if not left or not right:
            return False
        if left == right:
            return True
        return left in right or right in left

    @staticmethod
    def _resolve_cache_key(city_name: str, region: str = "") -> str | None:
        """Find the most suitable cache key for city operations.

        Prefer exact `city (region)` if present, then exact city key, then any key for the same base city.
        """
        cache = GeoService._load_cache()
        if not isinstance(cache, dict) or not cache:
            return None

        city_norm = GeoService._normalize_city_name(city_name)
        region_norm = " ".join((region or "").strip().lower().split())
        if not city_norm:
            return None

        exact_with_region = f"{city_norm} ({region_norm})" if region_norm else None
        if exact_with_region and exact_with_region in cache:
            return exact_with_region

        regional_candidates = []
        for key in cache.keys():
            if not isinstance(key, str):
                continue
            if GeoService._normalize_city_name(key) != city_norm:
                continue
            if "(" in key and key.endswith(")"):
                regional_candidates.append(key)

        if region_norm and regional_candidates:
            matched = []
            for key in regional_candidates:
                _, cache_region = GeoService._parse_cache_city_key(key)
                if GeoService._region_matches(cache_region, region):
                    matched.append(key)
            if matched:
                return sorted(matched)[0]

        if city_norm in cache:
            # Use plain key only when there are no region-specific variants,
            # otherwise we risk mixing cities with the same name in different regions.
            if region_norm and regional_candidates:
                return None
            return city_norm

        if regional_candidates:
            if region_norm:
                return None
            return sorted(regional_candidates)[0]

        return None

    @staticmethod
    def _get_city_alias_keys(city_name: str) -> list[str]:
        cache = GeoService._load_cache()
        if not isinstance(cache, dict) or not cache:
            return []
        base = GeoService._normalize_city_name(city_name)
        if not base:
            return []
        keys = []
        for key in cache.keys():
            if isinstance(key, str) and GeoService._normalize_city_name(key) == base:
                keys.append(key)
        return keys

    @staticmethod
    def _geo_log(message: str):
        # Keep logs visible in managed deploys; can be enabled locally with GEO_DEBUG=1.
        managed_deploy = (
            os.getenv("RAILWAY_ENVIRONMENT_NAME")
            or os.getenv("RAILWAY_SERVICE_NAME")
            or os.getenv("RENDER")
        )
        if not GeoService._build_marker_logged and (managed_deploy or os.getenv("GEO_DEBUG") == "1"):
            print("[GeoDebug] runtime marker: geo-no-gpt-landmarks-v2")
            GeoService._build_marker_logged = True
        if managed_deploy or os.getenv("GEO_DEBUG") == "1":
            print(f"[GeoDebug] {message}")

    @staticmethod
    def _parse_cache_city_key(raw_key: str) -> tuple[str, str]:
        key = (raw_key or "").strip()
        if not key:
            return "", ""
        if "(" in key and key.endswith(")"):
            name_part, region_part = key.rsplit("(", 1)
            return name_part.strip().title(), region_part[:-1].strip().title()
        return key.strip().title(), ""

    @staticmethod
    def _fallback_cities_from_cache(city_name: str, limit: int = 10) -> list[dict]:
        query = (city_name or "").lower().strip().replace("-", " ")
        query = " ".join(query.split())
        if not query:
            return []

        cache = GeoService._load_cache()
        if not isinstance(cache, dict) or not cache:
            return []

        exact: list[tuple[str, str, str]] = []
        starts: list[tuple[str, str, str]] = []
        seen = set()

        for raw_key in cache.keys():
            if not isinstance(raw_key, str):
                continue
            city_label, region_label = GeoService._parse_cache_city_key(raw_key)
            city_lower = city_label.lower().replace("-", " ")
            city_lower = " ".join(city_lower.split())

            if not city_lower:
                continue
            if city_lower in seen:
                continue

            if city_lower == query:
                exact.append((city_label, region_label, city_lower))
                seen.add(city_lower)
            elif city_lower.startswith(query):
                starts.append((city_label, region_label, city_lower))
                seen.add(city_lower)
        ordered = exact + starts
        result = []
        for city_label, region_label, _ in ordered[: max(1, limit)]:
            result.append({
                "name": city_label,
                "region": region_label,
                "type": "city",
                "raw_data": {"source": "cache"},
            })
        return result

    @staticmethod
    def _nominatim_headers():
        # Keep a stable UA and include contact to reduce blocking by public instances.
        contact = os.getenv("GEO_CONTACT_EMAIL", "geo_check@internal.service")
        return {
            "User-Agent": f"MarketplaceSearchBot/1.1 ({contact})",
            "Accept": "application/json",
        }

    @staticmethod
    def _request_json_with_retry(url: str, params: dict | None = None, timeout: int = 12, retries: int = 3):
        last_error = None
        # Глобальная задержка между запросами к Nominatim (ограничение частоты)
        NOMINATIM_GLOBAL_DELAY = 1.5
        for attempt in range(retries):
            try:
                time.sleep(NOMINATIM_GLOBAL_DELAY)
                resp = requests.get(url, params=params, headers=GeoService._nominatim_headers(), timeout=timeout)
                # Public geocoders can throttle shared cloud IPs.
                if resp.status_code == 429:
                    wait = 1.0 + attempt * 1.5
                    print(f"Geo throttle 429 from {url}; retry in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                if resp.status_code != 200:
                    last_error = RuntimeError(f"HTTP {resp.status_code}")
                    continue
                return resp.json()
            except Exception as e:
                last_error = e
                # Small backoff for transient DNS/timeout issues in cloud environments.
                time.sleep(0.6 + attempt * 0.8)

        if last_error:
            print(f"Geo request failed for {url}: {last_error}")
        return None

    @staticmethod
    def _load_population_cache() -> dict:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(GEO_CITY_POPULATION_DOC_KEY, {})
            return data if isinstance(data, dict) else {}
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        if not os.path.exists(CITY_POPULATION_FILE):
            return {}
        try:
            with open(CITY_POPULATION_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _save_population_cache(cache: dict) -> None:
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(GEO_CITY_POPULATION_DOC_KEY, cache)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CITY_POPULATION_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    @staticmethod
    def _build_population_cache_key(city_name: str, region: str = "") -> str:
        city_norm = GeoService._normalize_city_name(city_name)
        region_norm = " ".join((region or "").strip().lower().split())
        if city_norm and region_norm:
            return f"{city_norm} ({region_norm})"
        return city_norm

    @staticmethod
    def _get_cached_population(city_name: str, region: str = "") -> int:
        cache = GeoService._load_population_cache()
        if not cache:
            return 0

        exact_key = GeoService._build_population_cache_key(city_name, region)
        if exact_key and isinstance(cache.get(exact_key), dict):
            return GeoService._parse_population_value(cache[exact_key].get("population"))

        city_norm = GeoService._normalize_city_name(city_name)
        if not city_norm:
            return 0

        for key, value in cache.items():
            if not isinstance(key, str) or not isinstance(value, dict):
                continue
            if GeoService._normalize_city_name(key) != city_norm:
                continue
            if region:
                _, cached_region = GeoService._parse_cache_city_key(key)
                if not GeoService._region_matches(cached_region, region):
                    continue
            return GeoService._parse_population_value(value.get("population"))

        return 0

    @staticmethod
    def _cache_population(city_name: str, region: str, population: int) -> None:
        if population <= 0:
            return
        cache = GeoService._load_population_cache()
        key = GeoService._build_population_cache_key(city_name, region)
        if not key:
            return
        cache[key] = {"population": int(population)}
        GeoService._save_population_cache(cache)

    @staticmethod
    def _search_population_by_name(city_name: str, region: str = "") -> int:
        city_norm = GeoService._normalize_city_name(city_name)
        if not city_norm:
            return 0

        queries = []
        if city_name and region:
            queries.append(f"{city_name}, {region}")
        queries.append(city_name)

        endpoints = [
            "https://nominatim.openstreetmap.org/search",
            "https://nominatim.openstreetmap.ru/search",
            "https://nominatim.openstreetmap.de/search",
            "https://nominatim.openstreetmap.fr/search",
        ]

        for query in queries:
            for endpoint in endpoints:
                payload = GeoService._find_city_osm_endpoint(query, 10, endpoint)
                if not payload or not isinstance(payload, list):
                    continue

                for result in payload:
                    if not isinstance(result, dict):
                        continue

                    addr = result.get("address") or {}
                    if not isinstance(addr, dict):
                        addr = {}

                    clean_name = addr.get("city") or addr.get("town") or addr.get("village") or result.get("name")
                    if GeoService._normalize_city_name(clean_name) != city_norm:
                        continue

                    result_region = addr.get("state") or addr.get("province") or addr.get("region") or ""
                    if region and result_region and not GeoService._region_matches(result_region, region):
                        continue

                    extratags = result.get("extratags") or {}
                    if not isinstance(extratags, dict):
                        extratags = {}

                    pop = GeoService._parse_population_value(extratags.get("population"))
                    if pop == 0:
                        pop = GeoService._lookup_population_from_osm(result)
                    if pop > 0:
                        return pop

        return 0

    @staticmethod
    def is_city_population_allowed(city_info: dict, min_population: int = 30000) -> bool:
        if not isinstance(city_info, dict):
            return False

        population = GeoService._parse_population_value(city_info.get("population"))
        has_real_population = bool(city_info.get("has_real_population"))
        return has_real_population and population >= min_population

    @staticmethod
    def _parse_population_value(value) -> int:
        if value is None:
            return 0
        try:
            digits_only = "".join(ch for ch in str(value) if ch.isdigit())
            return int(digits_only) if digits_only else 0
        except Exception:
            return 0

    @staticmethod
    def _lookup_population_from_osm(raw_data: dict) -> int:
        """Try to load population via Nominatim lookup endpoint using osm_id/osm_type."""
        if not isinstance(raw_data, dict):
            return 0

        osm_id = raw_data.get("osm_id")
        osm_type = (raw_data.get("osm_type") or "").strip().lower()
        if not osm_id or osm_type not in {"node", "way", "relation"}:
            return 0

        type_prefix = {"node": "N", "way": "W", "relation": "R"}[osm_type]
        lookup_id = f"{type_prefix}{osm_id}"

        lookup_endpoints = [
            "https://nominatim.openstreetmap.org/lookup",
            "https://nominatim.openstreetmap.ru/lookup",
            "https://nominatim.openstreetmap.de/lookup",
            "https://nominatim.openstreetmap.fr/lookup",
        ]

        params = {
            "osm_ids": lookup_id,
            "format": "json",
            "addressdetails": 1,
            "extratags": 1,
        }

        for endpoint in lookup_endpoints:
            payload = GeoService._request_json_with_retry(endpoint, params=params, timeout=12, retries=2)
            if not payload or not isinstance(payload, list):
                continue
            first = payload[0] if payload else {}
            if not isinstance(first, dict):
                continue
            extra = first.get("extratags") or {}
            if not isinstance(extra, dict):
                continue
            pop = GeoService._parse_population_value(extra.get("population"))
            if pop > 0:
                return pop

        return 0

    @staticmethod
    def _load_cache():
        """Загрузка кэша районов из файла"""
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(GEO_DISTRICTS_CACHE_DOC_KEY, {})
            return data if isinstance(data, dict) else {}
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
            
        if not os.path.exists(CACHE_FILE):
            return {}
        try:
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Error loading cache: {e}")
            return {}

    @staticmethod
    def _write_cache(cache: dict):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(GEO_DISTRICTS_CACHE_DOC_KEY, cache)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(CACHE_FILE, "w", encoding="utf-8") as f:
                json.dump(cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving cache: {e}")

    @staticmethod
    def _save_to_cache(city_name: str, districts: list):
        """Сохранение районов в кэш"""
        cache = GeoService._load_cache()
        cache[city_name.lower().strip()] = districts
        GeoService._write_cache(cache)

    @staticmethod
    def get_known_cities_from_cache():
        """Returns list of city key names from cache."""
        cache = GeoService._load_cache()
        return list(cache.keys())

    @staticmethod
    def clear_districts_cache():
        """Полная очистка кэша районов."""
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(GEO_DISTRICTS_CACHE_DOC_KEY, {})
            return True
        if os.path.exists(CACHE_FILE):
            try:
                os.remove(CACHE_FILE)
                return True
            except Exception:
                return False
        return False

    @staticmethod
    def clear_city_districts_cache(city_name: str) -> tuple[bool, int]:
        """Удаляет кэш районов только для указанного города (включая ключи-алиасы)."""
        cache = GeoService._load_cache()
        if not isinstance(cache, dict) or not cache:
            return False, 0

        city_norm = GeoService._normalize_city_name(city_name)
        if not city_norm:
            return False, 0

        keys_to_delete = []
        for key in list(cache.keys()):
            if isinstance(key, str) and GeoService._normalize_city_name(key) == city_norm:
                keys_to_delete.append(key)

        if not keys_to_delete:
            return False, 0

        for key in keys_to_delete:
            cache.pop(key, None)

        try:
            GeoService._write_cache(cache)
            return True, len(keys_to_delete)
        except Exception:
            return False, 0

    @staticmethod
    def _get_candidate_population(candidate: dict) -> tuple[int, bool]:
        if not isinstance(candidate, dict):
            return 0, False

        clean_name = (candidate.get('name') or '').strip()
        region = (candidate.get('region') or '').strip()
        if not clean_name:
            return 0, False

        raw_data = candidate.get('raw_data') or {}
        if not isinstance(raw_data, dict):
            raw_data = {}

        extratags = raw_data.get('extratags') or {}
        if not isinstance(extratags, dict):
            extratags = {}

        pop = GeoService._get_cached_population(clean_name, region)
        if pop == 0:
            pop = GeoService._parse_population_value(extratags.get('population'))
        if pop == 0 and raw_data:
            pop = GeoService._lookup_population_from_osm(raw_data)
        if pop == 0:
            pop = GeoService._search_population_by_name(clean_name, region)

        has_real_population = pop > 0
        if has_real_population:
            GeoService._cache_population(clean_name, region, pop)

        return pop, has_real_population

    @staticmethod
    def _filter_candidates_by_population(candidates: list[dict], min_population: int = 30000) -> list[dict]:
        allowed_candidates = []
        for candidate in candidates or []:
            population, has_real_population = GeoService._get_candidate_population(candidate)
            if not has_real_population or population < min_population:
                continue

            enriched_candidate = dict(candidate)
            enriched_candidate['population'] = population
            enriched_candidate['has_real_population'] = True
            allowed_candidates.append(enriched_candidate)

        return allowed_candidates

    @staticmethod
    async def search_cities(city_name: str):
        """
        Ищет список городов-кандидатов.
        Возвращает список словарей: [{'name': '...', 'region': '...', 'type': '...', 'full_data': ...}, ...]
        """
        query = (city_name or "").strip()
        GeoService._geo_log(f"search_cities start query='{query}'")
        loop = asyncio.get_event_loop()

        if re.search(r'[a-zA-Z]', city_name):
            GeoService._geo_log(f"search_cities skip latin query='{query}'")
            return []

        # Сначала проверяем кэш — если город есть, не тратим время на сеть
        cache_fallback = GeoService._fallback_cities_from_cache(city_name, 10)
        if cache_fallback:
            cache_fallback = await loop.run_in_executor(
                None,
                GeoService._filter_candidates_by_population,
                cache_fallback,
                30000,
            )
            GeoService._geo_log(f"search_cities cache-hit query='{query}' count={len(cache_fallback)}")
            return cache_fallback

        # Список endpoints для Nominatim
        nominatim_endpoints = [
            "https://nominatim.openstreetmap.org/search",
            "https://nominatim.openstreetmap.ru/search",
            "https://nominatim.openstreetmap.de/search",
            "https://nominatim.openstreetmap.fr/search",
        ]
        raw_results = None
        for endpoint in nominatim_endpoints:
            try:
                raw_results = await loop.run_in_executor(None, GeoService._find_city_osm_endpoint, city_name, 10, endpoint)
                if raw_results:
                    break
            except Exception as e:
                GeoService._geo_log(f"search_cities network error endpoint='{endpoint}' query='{query}' err='{e}'")
                continue

        if not raw_results:
            if cache_fallback:
                GeoService._geo_log(f"search_cities fallback cache query='{query}' count={len(cache_fallback)}")
            else:
                GeoService._geo_log(f"search_cities empty query='{query}' no online/no cache")
            return cache_fallback

        candidates = []
        seen_regions = set()
        stats = {
            "non_ru": 0,
            "bad_type": 0,
            "region_obj": 0,
            "admin_region_name": 0,
            "dup": 0,
            "accepted": 0,
        }
        for res in raw_results:
            addr = res.get('address', {})
            if addr.get('country_code', '').lower() != 'ru':
                stats["non_ru"] += 1
                continue
            place_type = res.get('type')
            if place_type not in ['city', 'town', 'administrative']:
                stats["bad_type"] += 1
                continue
            if res.get('addresstype') in ['state', 'province', 'region']:
                stats["region_obj"] += 1
                continue
            clean_name = addr.get('city') or addr.get('town') or addr.get('village') or res.get('name')
            if clean_name and any(x in clean_name.lower() for x in ['край', 'область', 'республика', 'округ']) and place_type == 'administrative':
                if not (addr.get('city') or addr.get('town') or addr.get('village')):
                    stats["admin_region_name"] += 1
                    continue
            if clean_name:
                clean_name = clean_name.replace("городской округ", "").replace("муниципальный округ", "").strip()
            region = addr.get('state') or addr.get('province') or addr.get('region') or ""
            unique_key = f"{clean_name}_{region}"
            if unique_key in seen_regions:
                stats["dup"] += 1
                continue
            seen_regions.add(unique_key)
            candidates.append({
                "name": clean_name,
                "region": region,
                "type": place_type,
                "raw_data": res
            })
            stats["accepted"] += 1
        GeoService._geo_log(
            "search_cities filter stats "
            f"query='{query}' accepted={stats['accepted']} non_ru={stats['non_ru']} "
            f"bad_type={stats['bad_type']} region_obj={stats['region_obj']} "
            f"admin_region_name={stats['admin_region_name']} dup={stats['dup']}"
        )
        candidates = await loop.run_in_executor(
            None,
            GeoService._filter_candidates_by_population,
            candidates,
            30000,
        )
        if candidates:
            GeoService._geo_log(f"search_cities done query='{query}' source=online count={len(candidates)}")
            return candidates
        if cache_fallback:
            GeoService._geo_log(f"search_cities fallback-after-filter query='{query}' count={len(cache_fallback)}")
        else:
            GeoService._geo_log(f"search_cities done query='{query}' no candidates")
        return cache_fallback

    @staticmethod
    def _find_city_osm_endpoint(city_name: str, limit: int, endpoint: str):
        """
        Поиск города через конкретный endpoint Nominatim
        """
        params = {
            "q": city_name,
            "countrycodes": "ru",
            "format": "json",
            "addressdetails": 1,
            "extratags": 1,
            "limit": limit,
        }
        try:
            time.sleep(1.5)  # задержка для Render
            resp = requests.get(endpoint, params=params, headers=GeoService._nominatim_headers(), timeout=8)
            if resp.status_code == 429:
                print(f"Geo throttle 429 from {endpoint}; retry in 2.5s")
                time.sleep(2.5)
                return []
            if resp.status_code != 200:
                return []
            return resp.json()
        except Exception as e:
            print(f"Geo request failed for {endpoint}: {e}")
            return []

    @staticmethod
    async def get_city_details(candidate: dict):
        """
        Получает полную инфу (районы) для УЖЕ выбранного из списка кандидата.
        """
        clean_name = candidate['name']
        region = candidate['region']
        place_type = candidate['type']
        
        # Ключ кэша теперь уникален (Город + Регион), чтобы не путать Городище (Пенза) и Городище (Волгоград)
        cache_key = f"{clean_name} ({region})".lower().strip()
        plain_city_key = GeoService._normalize_city_name(clean_name)
        
        cache = GeoService._load_cache()
        cached_districts = cache.get(cache_key)

        # Strictly match same-name cities by region first.
        regional_aliases = []
        for key in cache.keys():
            if not isinstance(key, str):
                continue
            if GeoService._normalize_city_name(key) != plain_city_key:
                continue
            if "(" in key and key.endswith(")"):
                regional_aliases.append(key)

        if not cached_districts and regional_aliases:
            for key in sorted(regional_aliases):
                _, key_region = GeoService._parse_cache_city_key(key)
                if GeoService._region_matches(key_region, region):
                    cached_districts = cache.get(key)
                    if cached_districts:
                        GeoService._save_to_cache_key(cache_key, cached_districts)
                        GeoService._geo_log(
                            f"get_city_details cache-region-match city='{clean_name}' key='{key}' exact_key='{cache_key}'"
                        )
                    break

        # Plain key is safe only if there are no region-specific same-name entries.
        if not cached_districts and not regional_aliases:
            plain_cached = cache.get(plain_city_key)
            if plain_cached:
                cached_districts = plain_cached
                GeoService._save_to_cache_key(cache_key, plain_cached)
                GeoService._geo_log(
                    f"get_city_details cache-plain city='{clean_name}' plain_key='{plain_city_key}' exact_key='{cache_key}'"
                )

        if not cached_districts:
            fallback_key = GeoService._resolve_cache_key(clean_name, region)
            if fallback_key and fallback_key in cache:
                cached_districts = cache.get(fallback_key)
                if cached_districts:
                    # Sync under exact key too, so future lookups are deterministic.
                    GeoService._save_to_cache_key(cache_key, cached_districts)
                    GeoService._geo_log(
                        f"get_city_details cache-alias city='{clean_name}' alias_key='{fallback_key}' exact_key='{cache_key}'"
                    )
        
        if cached_districts:
            GeoService._geo_log(
                f"get_city_details cache-hit city='{clean_name}' region='{region}' "
                f"key='{cache_key}' districts={len(cached_districts)} → генерирую прайс из кэша"
            )
            print(f"[GeoCache] '{clean_name}' найден в кэше ({len(cached_districts)} районов/улиц), прайс генерируется из них")
            districts = cached_districts
        else:
            GeoService._geo_log(
                f"get_city_details cache-miss city='{clean_name}' region='{region}' key='{cache_key}' → ищу онлайн"
            )
            print(f"[GeoCache] '{clean_name}' не найден в кэше, ищу районы онлайн...")
            # Ищем районы (по имени). Если имена одинаковые, районы могут смешаться при онлайн-поиске,
            # но OSM-search районов иногда учитывает контекст.
            # Передаем регион для уточнения поиска
            districts = await asyncio.get_event_loop().run_in_executor(None, GeoService._search_internet_for_districts, clean_name, region)
            
            if districts:
                districts = GeoService._filter_best_districts(districts)
                GeoService._geo_log(
                    f"get_city_details online-found city='{clean_name}' districts={len(districts)} → сохраняю в кэш"
                )
                print(f"[GeoCache] '{clean_name}' получено онлайн {len(districts)} районов/улиц, сохраняю в кэш")

            if not districts:
                # Another concurrent request may already have populated cache while this one was waiting on network.
                latest_cache = GeoService._load_cache()
                latest_cached = latest_cache.get(cache_key)
                if not latest_cached and not regional_aliases:
                    latest_cached = latest_cache.get(plain_city_key)
                if not latest_cached:
                    latest_key = GeoService._resolve_cache_key(clean_name, region)
                    if latest_key and latest_key in latest_cache:
                        latest_cached = latest_cache.get(latest_key)

                if latest_cached:
                    districts = latest_cached
                    GeoService._geo_log(
                        f"get_city_details late-cache-hit city='{clean_name}' districts={len(districts)} → не перезаписываю fallback'ом"
                    )

            if not districts:
                # Используем addresstype из raw_data, если есть (он точнее чем type)
                raw_data = candidate.get('raw_data', {})
                address_type = raw_data.get('addresstype', place_type)
                
                is_large_city = (place_type == 'city' or address_type == 'city')
                GeoService._geo_log(
                    f"get_city_details fallback city='{clean_name}' large={is_large_city} → генерирую fallback районы"
                )
                print(f"[GeoCache] '{clean_name}' онлайн не дал результат, генерирую fallback районы")
                districts = await GeoService._generate_fallback_districts(clean_name, is_large_city)

            GeoService._save_to_cache_key(cache_key, districts)
            GeoService._geo_log(f"get_city_details saved city='{clean_name}' key='{cache_key}' districts={len(districts)}")
             
        # Население - получаем из OSM данных
        raw_data = candidate.get('raw_data') or {}
        if not isinstance(raw_data, dict):
            raw_data = {}

        is_from_cache = (raw_data.get('source') == 'cache')

        extratags = raw_data.get('extratags') or {}
        if not isinstance(extratags, dict):
            extratags = {}
        
        # If cached population exists, prefer it to avoid external lookups.
        pop = GeoService._get_cached_population(clean_name, region)

        if pop == 0:
            # Пытаемся получить население из extratags.
            pop = GeoService._parse_population_value(extratags.get('population'))

        # Если в search-ответе населения нет, пробуем отдельный lookup по osm_id.
        if pop == 0 and not is_from_cache:
            pop = GeoService._lookup_population_from_osm(raw_data)

        if pop == 0 and not is_from_cache:
            pop = await asyncio.get_event_loop().run_in_executor(
                None,
                GeoService._search_population_by_name,
                clean_name,
                region,
            )

        has_real_population = (pop > 0)
        if has_real_population:
            GeoService._cache_population(clean_name, region, pop)

        # Города из кэша районов с неизвестным населением считаем допустимыми,
        # чтобы не блокировать пользователей при недоступности Nominatim.
        if is_from_cache and not has_real_population:
            has_real_population = True
            if pop == 0:
                pop = 100000  # fallback для прайса
             
        return {
            "name": clean_name,
            "region": region,
            "population": pop,
            "has_real_population": has_real_population,
            "districts": districts
        }
    
    @staticmethod
    def _save_to_cache_key(key: str, districts: list):
        cache = GeoService._load_cache()
        cache[key] = districts
        try:
            GeoService._write_cache(cache)
        except Exception:
            pass

    @staticmethod
    def get_districts_for_city(city_name: str):
        """Возвращает список районов и ключ кэша для города (поиск по частичному совпадению)"""
        cache = GeoService._load_cache()
        key = GeoService._resolve_cache_key(city_name)
        if key and key in cache:
            return cache[key], key

        return None, None

    @staticmethod
    def add_district(city_name: str, district: str):
        districts, key = GeoService.get_districts_for_city(city_name)
        if key:
            if district not in districts:
                districts.append(district)
                districts.sort()
                GeoService._save_to_cache_key(key, districts)
                # Mirror edits to all aliases for the same city key.
                for alias in GeoService._get_city_alias_keys(city_name):
                    if alias != key:
                        GeoService._save_to_cache_key(alias, list(districts))
                return True
            return False # Уже есть
        else:
            # Создаем новую запись если города не было
            # Используем введенное имя как ключ
            GeoService._save_to_cache_key(GeoService._normalize_city_name(city_name), [district])
            return True

    @staticmethod
    def remove_district(city_name: str, district: str):
        districts, key = GeoService.get_districts_for_city(city_name)
        if key and districts:
            if district in districts:
                districts.remove(district)
                GeoService._save_to_cache_key(key, districts)
                # Keep aliases synchronized with the edited list.
                for alias in GeoService._get_city_alias_keys(city_name):
                    if alias != key:
                        GeoService._save_to_cache_key(alias, list(districts))
                return True
        return False

    @staticmethod
    async def get_city_info(city_name: str):
        """
        LEGACY API (оставляем для обратной совместимости, если вдруг где вызовется, 
        но перенаправляем на новую логику с выбором первого попавшегося)
        """
        candidates = await GeoService.search_cities(city_name)
        if not candidates:
            return None
        return await GeoService.get_city_details(candidates[0])


    @staticmethod
    def _filter_best_districts(districts: list) -> list:
        """
        Оставляет только самые "важные" районы (максимум 15).
        Приоритет у Центральных, Ленинских и т.д.
        """
        max_districts = 15
        if len(districts) <= max_districts:
            return districts
            
        # Ключевые слова для приоритета (административные районы)
        priority_keywords = [
            "Центральный", "Ленинский", "Кировский", "Советский", "Октябрьский",
            "Заводской", "Железнодорожный", "Промышленный", "Свердловский", 
            "Московский", "Фрунзенский", "Калининский", "Приморский"
        ]
        
        selected = []
        others = []
        
        for d in districts:
            # Проверяем, содержит ли название одно из приоритетных слов
            score = 0
            for kw in priority_keywords:
                if kw.lower() in d.lower():
                    score = 1
                    break
            
            if score > 0:
                selected.append(d)
            else:
                others.append(d)
        
        # Сортируем выбранные по алфавиту для красоты
        selected.sort()
        
        # Если приоритетных не хватило до лимита, добираем из остальных
        # Но стараемся брать те, что покороче (часто это реальные названия, а не длинный мусор)
        if len(selected) < max_districts:
            needed = max_districts - len(selected)
            others.sort(key=len) # Самые короткие названия сначала
            selected.extend(others[:needed])
            
        return selected[:max_districts]

    @staticmethod
    def _search_internet_for_districts(city_name, region_name=""):
        """
        Комплексный поиск районов в интернете.
        """
        print(f"Searching internet for districts in '{city_name}' (region: {region_name})...")
        
        # Способ 0: Yandex Maps (Search API) - Приоритет пользователем
        yandex_districts = GeoService._fetch_districts_yandex(city_name, region_name)
        if yandex_districts and len(yandex_districts) >= 2:
            print(f"Yandex found districts for {city_name}: {yandex_districts}")
            return yandex_districts

        # 1. Сначала ищем административные районы (admin_level 8/9)
        districts = GeoService._fetch_districts_overpass(city_name, region_name)
        if districts and len(districts) >= 2:
            print(f"OSM districts found for {city_name}: {districts}")
            return districts

        # 2. Если районов нет — ищем улицы в радиусе 10 км от центра
        streets = GeoService._fetch_streets_overpass(city_name, region_name)
        if streets:
            print(f"OSM streets fallback found for {city_name}: {streets}")
            return streets

        # Способ 1.3: 2GIS (Catalog API)
        dgis_districts = GeoService._fetch_districts_2gis(city_name, region_name)
        if dgis_districts:
            print(f"2GIS found districts for {city_name}: {dgis_districts}")
            return dgis_districts

        # Способ 1.5: ChatGPT (AI) — опционально, отключен по умолчанию.
        # Включается только если явно задан GEO_ENABLE_GPT_DISTRICTS_FALLBACK=1.
        use_gpt_fallback = os.getenv("GEO_ENABLE_GPT_DISTRICTS_FALLBACK", "0") == "1"
        if use_gpt_fallback:
            gpt_districts = GeoService._fetch_districts_gpt(city_name, region_name)
            if gpt_districts:
                print(f"GPT found districts for {city_name}: {gpt_districts}")
                return gpt_districts
        else:
            print(f"GPT fallback disabled for {city_name}; using OSM/wiki only")
             
        # Способ 2: Парсинг страницы Википедии
        wiki_districts = GeoService._fetch_districts_wikipedia(city_name, region_name)
        if wiki_districts:
            return wiki_districts
            
        return []

    @staticmethod
    def _fetch_districts_gpt(city_name, region_name=""):
        """Запрос к ChatGPT для получения списка районов"""
        if not OPENAI_API_KEY:
            GeoService._geo_log(f"gpt disabled city='{city_name}' reason='OPENAI_API_KEY missing'")
            return []
            
        try:
            url = "https://api.openai.com/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "Content-Type": "application/json"
            }
            
            # ЭТАП 1: Пробуем найти официальные административные районы
            # Промпт: просим внутригородские, исключаем областные.
            prompt_official = (
                f"Назови только внутригородские административные районы города {city_name} ({region_name}). "
                "Исключительно те районы, которые находятся ВНУТРИ города (например: Ленинский, Заволжский). "
                "НЕ пиши районы области. "
                "Если у города нет внутригородского деления на районы, напиши 'Нет районов'. "
                "Ответ строго в одну строку через запятую."
            )
            
            data_official = {
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": prompt_official}],
                "temperature": 0.2
            }
            
            resp = requests.post(url, headers=headers, json=data_official, timeout=10)
            GeoService._geo_log(
                f"gpt official request city='{city_name}' status={resp.status_code}"
            )
            if resp.status_code == 200:
                content = resp.json()['choices'][0]['message']['content'].strip()
                
                # Если районы нашлись и это не отказ
                if "нет районов" not in content.lower() and len(content) > 5:
                    return GeoService._clean_gpt_list(content, is_street=False)
                
                # В GPT-ветке больше не дергаем OSM повторно.
                # OSM уже обрабатывается выше в _search_internet_for_districts.
                print(f"GPT: Official districts not found for {city_name}.")

                # Опциональный fallback: ориентиры GPT, если OSM недоступен на хостинге.
                allow_landmarks = os.getenv("GEO_ENABLE_GPT_LANDMARKS_FALLBACK", "1") == "1"
                GeoService._geo_log(
                    f"gpt landmarks fallback city='{city_name}' enabled={allow_landmarks}"
                )
                if not allow_landmarks:
                    print(f"GPT fallback landmarks disabled for {city_name}; using deterministic fallback districts.")
                    return []

                print(f"OSM failed. Looking for landmarks via GPT...")
                prompt_landmarks = (
                    f"Назови 8-12 реально существующих улиц, проспектов, переулков или шоссе в городе {city_name} ({region_name}). "
                    "Не указывай микрорайоны, районы, кварталы, поселки и общие ориентиры типа 'Центр'. "
                    "Пиши только названия улиц/проспектов/переулков/шоссе этого города. "
                    "Ответ строго списком через запятую, без пояснений."
                )
                data_landmarks = {
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt_landmarks}],
                    "temperature": 0.1
                }
                resp2 = requests.post(url, headers=headers, json=data_landmarks, timeout=12)
                GeoService._geo_log(
                    f"gpt landmarks request city='{city_name}' status={resp2.status_code}"
                )
                if resp2.status_code == 200:
                    content2 = resp2.json()['choices'][0]['message']['content'].strip()
                    return GeoService._clean_gpt_list(content2, is_street=True)
                # Keep short body snippet for Render diagnostics.
                try:
                    body_snippet = (resp2.text or "")[:200].replace("\n", " ")
                except Exception:
                    body_snippet = "<unavailable>"
                GeoService._geo_log(
                    f"gpt landmarks failed city='{city_name}' status={resp2.status_code} body='{body_snippet}'"
                )
                return []
            else:
                try:
                    body_snippet = (resp.text or "")[:200].replace("\n", " ")
                except Exception:
                    body_snippet = "<unavailable>"
                GeoService._geo_log(
                    f"gpt official failed city='{city_name}' status={resp.status_code} body='{body_snippet}'"
                )

        except Exception as e:
            print(f"GPT error: {e}")
            GeoService._geo_log(f"gpt exception city='{city_name}' err='{e}'")
            
        return []

    @staticmethod
    def _clean_gpt_list(raw_content, is_street=False):
        """Вспомогательный метод для очистки ответа GPT"""
        # Заменяем переносы строк на запятые
        content = raw_content.replace("\n", ",").replace(";", ",")
        raw_list = [x.strip() for x in content.split(",") if x.strip()]
        
        cleaned_list = []
        for item in raw_list:
            # Убираем нумерацию списков, но оставляем цифры в названии улиц (например "40 лет Победы")
            # Удаляем "1. ", "2) ", "- ", "• " в начале
            clean = re.sub(r'^(\d+[\.\)]|[-•])\s+', '', item).strip()
            
            # Убираем лишние слова из РАЙОНОВ НО сохраняем их для УЛИЦ
            clean_lower = clean.lower()

            if is_street:
                # В режиме улиц отбрасываем микрорайоны/районы и общие ориентиры.
                if any(token in clean_lower for token in ["микрорайон", "мкр", "район", "квартал", "поселок", "посёлок"]):
                    continue
                if clean_lower in ["центр", "центральный", "набережная", "вокзал", "парк", "площадь"]:
                    continue
            
            # 1. Обработка РАЙОНОВ (если это не режим "Улицы")
            if not is_street and (" район" in clean_lower or "округ" in clean_lower):
                clean = clean.replace("район", "").replace("муниципальный округ", "").replace("городской округ", "").strip()
            
            # Убираем "мкр", "микрорайон" для районного режима.
            if not is_street:
                clean = clean.replace("микрорайон", "мкр.").strip()
            
            # 2. Обработка УЛИЦ (проспект, улица, переулок)
            # Стандартизация: заменяем полные названия на сокращения
            
            # Улица
            if clean_lower.startswith("улица "):
                clean = "ул. " + clean[6:].strip()
            elif clean_lower.endswith(" улица"):
                clean = "ул. " + clean[:-6].strip()
            
            elif clean_lower.startswith("проспект "):
                clean = "пр-т " + clean[9:].strip()
            elif clean_lower.endswith(" проспект"):
                clean = "пр-т " + clean[:-9].strip()
                
            elif clean_lower.startswith("переулок "):
                clean = "пер. " + clean[9:].strip()
            elif clean_lower.startswith("проезд "):
                clean = "пр-д " + clean[7:].strip()
            elif clean_lower.startswith("бульвар "):
                clean = "бул. " + clean[8:].strip()
            elif clean_lower.startswith("шоссе "):
                clean = "ш. " + clean[6:].strip()

            # Если просто "ул." без пробела или с точкой
            clean = clean.replace("ул.", "ул. ").replace("  ", " ").strip()
            if clean.startswith("."): clean = clean[1:].strip()
            # Убираем хвостовые знаки, чтобы не оставались варианты вида "Советская.".
            clean = clean.rstrip(" .,;:-")
            
            # Финальная зачистка двойных пробелов и точек
            clean = re.sub(r'\s+', ' ', clean).strip()
            clean = clean.replace("ул. ул.", "ул.") # На случай если что-то пошло не так
            
            # Если это режим УЛИЦ и префикса нет, добавляем "ул."
            if is_street:
                 # Список маркеров, которые говорят что префикс уже есть
                 has_prefix = any(x in clean.lower() for x in ["ул.", "пр-т", "пер.", "мкр", "центр", "вокзал", "площадь", "парк", "набережная", "шоссе", "проезд", "бульвар", "ш.", "пр-д", "бул.", "проспект", "переулок", "аллея"])
                 if not has_prefix and len(clean) > 2:
                      # Исключаем явные ошибки
                      if clean.lower() not in ["нет", "нет районов", "центр"]:
                          clean = "ул. " + clean
            
            if len(clean) > 2:
                cleaned_list.append(clean)

        return sorted(list(set(cleaned_list)))

    @staticmethod
    def _fetch_districts_overpass(city_name, region_name=""):
        """Запрос к картам OpenStreetMap"""
        return GeoService._fetch_map_data_overpass(city_name, region_name, resource_type="districts")

    @staticmethod
    def _fetch_streets_overpass(city_name, region_name=""):
        """Запрос главных улиц через Overpass API"""
        return GeoService._fetch_map_data_overpass(city_name, region_name, resource_type="streets")

    @staticmethod
    def _fetch_map_data_overpass(city_name, region_name, resource_type="districts"):
        """Универсальный метод поиска через Overpass (районы или улицы)"""
        overpass_urls = [
            "https://overpass-api.de/api/interpreter",
            "https://overpass.kumi.systems/api/interpreter",
            "https://overpass.openstreetmap.ru/api/interpreter",
            "https://overpass.private.coffee/api/interpreter",
            "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
        ]
        try:
             # 1. Поиск города в Nominatim
             q_str = f"{city_name} {region_name}".strip()
             params = {"q": q_str, "format": "json", "limit": 1}
             GeoService._geo_log(
                 f"overpass preflight city='{city_name}' region='{region_name}' resource={resource_type} query='{q_str}'"
             )
             data_nom = GeoService._request_json_with_retry(
                 "https://nominatim.openstreetmap.org/search",
                 params=params,
                 timeout=12,
                 retries=2,
             )
             if not data_nom:
                 GeoService._geo_log(
                     f"overpass preflight nominatim-empty city='{city_name}' resource={resource_type}"
                 )
                 return []
             
             osm_data = data_nom[0]
             osm_id = osm_data.get('osm_id')
             osm_type = osm_data.get('osm_type')
             bbox = osm_data.get('boundingbox') # ['minLat', 'maxLat', 'minLon', 'maxLon']
             lat = osm_data.get('lat')
             lon = osm_data.get('lon')
             GeoService._geo_log(
                 f"overpass preflight nominatim-hit city='{city_name}' resource={resource_type} "
                 f"osm_type={osm_type} osm_id={osm_id} lat={lat} lon={lon} bbox={'yes' if bbox else 'no'}"
             )
             
             area_id = int(osm_id) + 3600000000 if osm_type == 'relation' else None
        except Exception as e:
             GeoService._geo_log(
                 f"overpass preflight exception city='{city_name}' resource={resource_type} err='{e}'"
             )
             return []

        headers = {
            "User-Agent": "TelegramBot/1.0",
            "Accept": "application/json",
        }

        # 2. Формируем запрос в зависимости от типа ресурса
        query = ""
        candidate_queries = []
        if resource_type == "districts":
            if not area_id: return []
            # Ищем границы (районы)
            query = f"""
            [out:json][timeout:14];
            area({area_id})->.searchArea;
            (
                            relation(area.searchArea)["admin_level"~"9|8"]["boundary"="administrative"]["name"~"район|округ",i];
            );
            out tags;
            """
            candidate_queries = [query]
        elif resource_type == "streets":
            # Основной режим: поиск улиц в пределах города и не дальше 10км от центра.
            # Далее сортируем по расстоянию до центра, чтобы в выдаче были более центральные улицы.
            if lat and lon:
                # Если есть area_id, ограничиваемся ТОЛЬКО улицами внутри границ города,
                # а вокруг центра применяем радиус 10км/6км как дополнительный фильтр.
                if area_id:
                    candidate_queries = [
                        f"""
                        [out:json][timeout:12];
                        area({area_id})->.searchArea;
                        way(area.searchArea)["highway"~"primary|secondary|tertiary|residential|unclassified|living_street"]["name"]->.inCity;
                        way(around:10000,{lat},{lon})["highway"~"primary|secondary|tertiary|residential|unclassified|living_street"]["name"]->.nearCenter;
                        way.inCity.nearCenter;
                        out tags center qt 50;
                        """,
                        f"""
                        [out:json][timeout:10];
                        area({area_id})->.searchArea;
                        way(area.searchArea)["highway"~"primary|secondary|tertiary|residential"]["name"]->.inCity;
                        way(around:6000,{lat},{lon})["highway"~"primary|secondary|tertiary|residential"]["name"]->.nearCenter;
                        way.inCity.nearCenter;
                        out tags center qt 30;
                        """,
                    ]
                else:
                    # Фолбэк для кейсов без relation-area: только круг вокруг центра.
                    candidate_queries = [
                        f"""
                        [out:json][timeout:12];
                        way(around:10000,{lat},{lon})["highway"~"primary|secondary|tertiary|residential|unclassified|living_street"]["name"];
                        out tags center qt 50;
                        """,
                        f"""
                        [out:json][timeout:10];
                        way(around:6000,{lat},{lon})["highway"~"primary|secondary|tertiary|residential"]["name"];
                        out tags center qt 30;
                        """,
                    ]
            elif area_id:
                # Фолбэк, если координаты центра не удалось получить.
                candidate_queries = [
                    f"""
                    [out:json][timeout:14];
                    area({area_id})->.searchArea;
                    way(area.searchArea)["highway"~"primary|secondary|tertiary|residential|unclassified|living_street"]["name"];
                    out tags center qt 40;
                    """,
                    f"""
                    [out:json][timeout:10];
                    area({area_id})->.searchArea;
                    way(area.searchArea)["highway"~"primary|secondary|tertiary|residential"]["name"];
                    out tags center qt 25;
                    """,
                ]
            elif bbox and len(bbox) == 4:
                s, n, w, e = bbox[0], bbox[1], bbox[2], bbox[3]
                bbox_str = f"{s},{w},{n},{e}"
                
                candidate_queries = [
                    f"""
                    [out:json][timeout:10];
                    way["highway"~"primary|secondary|tertiary|residential|unclassified|living_street"]["name"]({bbox_str});
                    out tags center qt 30;
                    """
                ]
            else:
                 return []
        if not candidate_queries and query:
            candidate_queries = [query]
        
        try:
            data = None
            used_endpoint = None
            attempts = 0
            max_attempts = 2 if resource_type == "streets" else 2
            for q_idx, q_text in enumerate(candidate_queries, start=1):
                for overpass_url in overpass_urls:
                    if attempts >= max_attempts:
                        break
                    attempts += 1
                    try:
                        # POST официально рекомендован в примерах Overpass для длинных/тяжелых QL-запросов.
                        resp = requests.post(overpass_url, data={'data': q_text}, headers=headers, timeout=25)
                        if resp.status_code == 200:
                            try:
                                payload = resp.json()
                            except Exception as json_err:
                                snippet = (resp.text or "")[:220].replace("\n", " ")
                                GeoService._geo_log(
                                    f"overpass bad-json resource={resource_type} city='{city_name}' url='{overpass_url}' "
                                    f"qidx={q_idx} err='{json_err}' body='{snippet}'"
                                )
                                continue

                            elements_count = len(payload.get('elements', []))
                            remark = payload.get('remark')
                            GeoService._geo_log(
                                f"overpass 200 resource={resource_type} city='{city_name}' url='{overpass_url}' qidx={q_idx} elements={elements_count}"
                            )
                            if remark:
                                GeoService._geo_log(
                                    f"overpass remark resource={resource_type} city='{city_name}' url='{overpass_url}' qidx={q_idx} remark='{remark}'"
                                )

                            if elements_count > 0 or resource_type == 'districts':
                                data = payload
                                used_endpoint = overpass_url
                                break
                            # streets: если пусто, пробуем следующий endpoint/облегченный запрос
                            continue

                        GeoService._geo_log(
                            f"overpass non-200 resource={resource_type} city='{city_name}' url='{overpass_url}' qidx={q_idx} status={resp.status_code}"
                        )
                        if resp.status_code in (429, 502, 503, 504):
                            snippet = (resp.text or "")[:220].replace("\n", " ")
                            GeoService._geo_log(
                                f"overpass response-body resource={resource_type} city='{city_name}' url='{overpass_url}' qidx={q_idx} body='{snippet}'"
                            )
                    except Exception as endpoint_err:
                        GeoService._geo_log(
                            f"overpass error resource={resource_type} city='{city_name}' url='{overpass_url}' qidx={q_idx} err='{endpoint_err}'"
                        )

                if data:
                    break
                if attempts >= max_attempts:
                    GeoService._geo_log(
                        f"overpass stop-early resource={resource_type} city='{city_name}' attempts={attempts}"
                    )
                    break

            if data:
                GeoService._geo_log(
                    f"overpass success resource={resource_type} city='{city_name}' url='{used_endpoint}' elements={len(data.get('elements', []))}"
                )
                found = set()
                streets_distance_km = {}

                city_lat = None
                city_lon = None
                try:
                    if lat is not None and lon is not None:
                        city_lat = float(lat)
                        city_lon = float(lon)
                except Exception:
                    city_lat = None
                    city_lon = None

                def _distance_km(lat1, lon1, lat2, lon2):
                    # Haversine distance in km.
                    r = 6371.0
                    d_lat = math.radians(lat2 - lat1)
                    d_lon = math.radians(lon2 - lon1)
                    a = (
                        math.sin(d_lat / 2) ** 2
                        + math.cos(math.radians(lat1))
                        * math.cos(math.radians(lat2))
                        * math.sin(d_lon / 2) ** 2
                    )
                    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))
                
                if resource_type == "districts":
                    for el in data.get('elements', []):
                        name = el.get('tags', {}).get('name')
                        if name and name.lower() != city_name.lower() and len(name) < 40:
                            if 'і' not in name.lower() and 'ї' not in name.lower() and 'є' not in name.lower():
                                lower_name = name.lower()
                                # Отсекаем не внутригородские сущности (поселения/сельсоветы и т.п.).
                                if any(x in lower_name for x in ["сельское поселение", "поселение", "сельсовет", "волость"]):
                                    continue
                                if not ("район" in lower_name or "округ" in lower_name):
                                    continue
                                clean_n = name.replace("административный округ", "").replace("муниципальный округ", "").strip()
                                found.add(clean_n)
                                
                elif resource_type == "streets":
                    for el in data.get('elements', []):
                        name = el.get('tags', {}).get('name')
                        if name:
                            # Пропускаем трассы и километры
                            if "—" in name or " км" in name:
                                continue
                            
                            # Пользователь хочет сохранять типы улиц (проспект, переулок).
                            # UPD: User requested to KEEP prefixes for better readability
                            # e.g. "ул. Ленина", "пер. Лесной"
                            
                            # However, OSM returns full name "улица Ленина". 
                            # Let's map long names to short prefixes
                            
                            clean = name
                            lower = clean.lower()
                            
                            if lower.startswith("улица "):
                                clean = "ул. " + clean[6:].strip()
                            elif lower.endswith(" улица"):
                                clean = "ул. " + clean[:-6].strip()
                            elif lower.startswith("проспект "):
                                clean = "пр-т " + clean[9:].strip()
                            elif lower.endswith(" проспект"):
                                clean = "пр-т " + clean[:-9].strip()
                            elif lower.startswith("переулок "):
                                clean = "пер. " + clean[9:].strip()
                            elif lower.endswith(" переулок"):
                                clean = "пер. " + clean[:-9].strip()
                            elif lower.startswith("проезд "):
                                clean = "пр-д " + clean[7:].strip()
                            elif lower.startswith("бульвар "):
                                clean = "бул. " + clean[8:].strip()
                            elif lower.startswith("шоссе "):
                                clean = "ш. " + clean[6:].strip()
                            # If no prefix found, check if it is just a clear noun
                            elif not any(x in lower for x in ["ул.", "пер.", "пр.", "мкр", "район", "парк", "сквер"]):
                                # It's like "Ленина" or "Лесная" raw from OSM? Usually OSM has "улица" tag.
                                # If raw name is just "Ленина", we force add "ул."
                                # Avoid adding to "Центр", "Вокзал"
                                if clean.lower() not in ["центр", "вокзал", "набережная", "парк"]:
                                     clean = "ул. " + clean
                                     
                            # Removed logic that stripped prefixes
                            # clean = clean.replace("ул. ", "").strip()
                                
                            if len(clean) > 2:
                                dist = 9999.0
                                if city_lat is not None and city_lon is not None:
                                    center = el.get('center')
                                    if isinstance(center, dict):
                                        c_lat = center.get('lat')
                                        c_lon = center.get('lon')
                                        if c_lat is not None and c_lon is not None:
                                            try:
                                                dist = _distance_km(city_lat, city_lon, float(c_lat), float(c_lon))
                                            except Exception:
                                                dist = 9999.0

                                prev_dist = streets_distance_km.get(clean)
                                if prev_dist is None or dist < prev_dist:
                                    streets_distance_km[clean] = dist

                if resource_type == "streets":
                    res_list = [k for k, _ in sorted(streets_distance_km.items(), key=lambda kv: (kv[1], kv[0]))]
                else:
                    res_list = sorted(list(found))
                GeoService._geo_log(
                    f"overpass parsed resource={resource_type} city='{city_name}' found={len(res_list)}"
                )
                if resource_type == "streets":
                    if res_list:
                         # Фильтр улиц на цифры/повторы
                         before_filter = len(res_list)
                         res_list = [x for x in res_list if not x[0].isdigit()]
                         GeoService._geo_log(
                             f"overpass streets filter resource={resource_type} city='{city_name}' before={before_filter} after={len(res_list)}"
                         )
                         
                         if len(res_list) > 8:
                            # После сортировки по расстоянию оставляем самые близкие к центру.
                            res_list = res_list[:8]
                            GeoService._geo_log(
                                f"overpass streets crop city='{city_name}' cropped_to={len(res_list)} mode='nearest_to_center'"
                            )

                         if "Центр" not in res_list:
                             res_list.insert(0, "Центр")
                             
                return res_list
            GeoService._geo_log(f"overpass all endpoints failed resource={resource_type} city='{city_name}'")
            return []
        except Exception as e:
            GeoService._geo_log(
                f"overpass exception resource={resource_type} city='{city_name}' err='{e}'"
            )
            
        return []

    @staticmethod
    def _fetch_districts_wikipedia(city_name, region_name=""):
        """
        Простой парсер Википедии.
        """
        try:
            # Пробуем более точный URL, если есть регион
            urls_to_try = []
            if region_name:
                urls_to_try.append(f"https://ru.wikipedia.org/wiki/{city_name}_({region_name.replace(' ', '_')})")
            urls_to_try.append(f"https://ru.wikipedia.org/wiki/{city_name}")

            for url in urls_to_try:
                try:
                    resp = requests.get(url, timeout=10)
                    if resp.status_code == 200:
                        text = resp.text
                        # Проверка, что это не дизамбиг (страница разрешения неоднозначностей)
                        if "неоднозначность" in text and len(text) < 5000:
                            continue
                            
                        # Проверка на украинский контекст (на всякий случай)
                        if "Украина" in text[:1000] and "Россия" not in text[:1000]:
                             continue

                        found = set()
                        
                        admin_districts = re.findall(r'title="([^"]+?)\sрайон\s\([^"]+\)"', text)
                        found.update([d.split('(')[0].strip() for d in admin_districts])
                        
                        simple_links = re.findall(r'>([А-Яа-я0-9\-\s]+?)\sрайон</a>', text)
                        filtered_links = [l for l in simple_links if len(l) > 3 and "муниципальный" not in l.lower()]
                        found.update(filtered_links)

                        result = sorted(list(found))
                        if len(result) >= 2:
                            print(f"Found districts on Wikipedia ({url}): {result}")
                            return result
                except:
                    continue
                
        except Exception as e:
            print(f"Wiki parse error: {e}")
            
        return []

    @staticmethod
    async def _generate_fallback_districts(city_name, is_large_city=False):
        """Fallback-генератор (если ничего не нашли)"""
        # Возвращаем универсальные районы без ложных улиц
        return ["Центр", "За городом"]

    @staticmethod
    def _find_city_osm(city_name, limit=1):
        # Добавляем addressdetails=1 для получения структуры адреса (страна, область)
        # extratags=1 для получения населения (population)
        params = {
            "q": city_name,
            "countrycodes": "ru",
            "format": "json",
            "addressdetails": 1,
            "extratags": 1,
            "limit": limit,
        }

        nominatim_urls = [
            "https://nominatim.openstreetmap.org/search",
            "https://nominatim.openstreetmap.fr/search",
        ]
        for url in nominatim_urls:
            payload = GeoService._request_json_with_retry(url, params=params, timeout=12, retries=3)
            if isinstance(payload, list) and payload:
                return payload
        return []

    @staticmethod
    def _fetch_districts_yandex(city_name, region_name=""):
        """Поиск районов через Яндекс Карты (Search API)"""
        if not YANDEX_API_KEY:
            print("Yandex API key is missing. Skipping Yandex search.")
            return []
            
        print(f"Querying Yandex Search API for {city_name}...")
        try:
            url = "https://search-maps.yandex.ru/v1/"
            # Ищем "районы города X"
            # bbox не задаем, надеемся на умный поиск Яндекса
            text = f"район {city_name}"
            if region_name:
                text += f" {region_name}"

            params = {
                "text": text,
                "type": "geo",
                "lang": "ru_RU",
                "apikey": YANDEX_API_KEY,
                "results": 50 # берем побольше
            }
            
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                features = data.get('features', [])
                
                districts = set()
                for feat in features:
                    props = feat.get('properties', {})
                    name = props.get('name', '')
                    meta = props.get('GeocoderMetaData', {})
                    kind = meta.get('kind', '')
                    
                    # Проверяем kind, если он есть (обычно в GeocoderMetaData)
                    # Но Search API возвращает свойства немного иначе
                    # У Search API kind может быть в 'properties.CompanyMetaData' или 'properties.GeocoderMetaData'
                    
                    # Фильтруем по имени
                    lower_name = name.lower()
                    if "район" in lower_name and "област" not in lower_name and "край" not in lower_name:
                         # Отсекаем сам город если он назван районом (редко)
                         if lower_name != city_name.lower():
                             clean = name.replace("район", "").replace("муниципальный", "").replace("округ", "").strip()
                             if len(clean) > 2:
                                 districts.add(clean)
                
                # Если районов мало, возможно нужно было искать "микрорайон"
                if len(districts) < 2:
                     return []

                return sorted(list(districts))
                    
        except Exception as e:
            print(f"Yandex search error: {e}")
            
        return []

    @staticmethod
    def _fetch_districts_2gis(city_name, region_name=""):
        """Поиск через 2GIS Catalog API (Stub)"""
        if not DGIS_API_KEY:
            return []
        # Реализация для 2GIS требует сложной логики (поиск project_id, потом items)
        # Оставим заглушку пока не будет ключа и точной документации
        return []
