from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

import requests

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import DATABASE_URL
from app.services.catalog import CatalogService
from app.services.geo import (
    GEO_CITY_POPULATION_DOC_KEY,
    GEO_DISTRICTS_CACHE_DOC_KEY,
    GeoService,
)
from app.services.postgres_store import PostgresDocumentStore

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.ru/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Import Russian cities >= min population into Postgres geo caches from OSM/Overpass.",
    )
    parser.add_argument("--min-population", type=int, default=30000)
    parser.add_argument("--limit", type=int, default=0, help="0 = no limit")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--delay-seconds", type=float, default=0.4)
    parser.add_argument("--flush-every", type=int, default=10)
    parser.add_argument("--city", action="append", default=[], help="Import specific city names in addition to OSM batch list")
    parser.add_argument("--skip-existing", action="store_true", default=False)
    parser.add_argument("--warm-catalog", action="store_true", default=False)
    parser.add_argument("--allow-fallback", action="store_true", default=False)
    return parser.parse_args()


def _parse_population(value) -> int:
    return GeoService._parse_population_value(value)


def _clean_region(value: str | None) -> str:
    return " ".join((value or "").strip().split())


def _build_cache_key(city_name: str, region_name: str = "") -> str:
    city_key = GeoService._normalize_city_name(city_name)
    region_key = " ".join(_clean_region(region_name).lower().split())
    if city_key and region_key:
        return f"{city_key} ({region_key})"
    return city_key


def _request_overpass(query: str, timeout_seconds: int = 180) -> dict:
    headers = {
        "User-Agent": "bot-city-import/1.0",
        "Accept": "application/json",
    }
    last_error = None
    for url in OVERPASS_URLS:
        try:
            response = requests.post(url, data={"data": query}, headers=headers, timeout=timeout_seconds)
            if response.status_code != 200:
                last_error = RuntimeError(f"{url} returned HTTP {response.status_code}")
                continue
            payload = response.json()
            if not isinstance(payload, dict):
                last_error = RuntimeError(f"{url} returned non-dict JSON")
                continue
            return payload
        except Exception as exc:
            last_error = exc
    if last_error:
        raise RuntimeError(f"Overpass request failed: {last_error}")
    raise RuntimeError("Overpass request failed without details")


def _extract_region_from_tags(tags: dict) -> str:
    candidates = [
        tags.get("addr:region"),
        tags.get("is_in:state"),
        tags.get("addr:state"),
        tags.get("official_name:region"),
        tags.get("addr:province"),
        tags.get("is_in:province"),
    ]
    for candidate in candidates:
        cleaned = _clean_region(candidate)
        if cleaned:
            return cleaned
    return ""


def _extract_city_center(element: dict) -> tuple[float | None, float | None]:
    if isinstance(element.get("center"), dict):
        center = element["center"]
        return center.get("lat"), center.get("lon")
    return element.get("lat"), element.get("lon")


def _fetch_city_elements(min_population: int) -> list[dict]:
    query = f"""
    [out:json][timeout:600];
    area["ISO3166-1"="RU"][admin_level=2]->.ru;
    (
      node["place"~"city|town"]["population"](area.ru);
      way["place"~"city|town"]["population"](area.ru);
      relation["place"~"city|town"]["population"](area.ru);
    );
    out tags center;
    """
    payload = _request_overpass(query)
    elements = payload.get("elements") or []
    result: dict[str, dict] = {}

    for element in elements:
        if not isinstance(element, dict):
            continue
        tags = element.get("tags") or {}
        if not isinstance(tags, dict):
            continue
        city_name = " ".join(str(tags.get("name") or "").split())
        if not city_name:
            continue
        population = _parse_population(tags.get("population"))
        if population < min_population:
            continue
        region_name = _extract_region_from_tags(tags)
        cache_key = _build_cache_key(city_name, region_name)
        if not cache_key:
            continue
        lat, lon = _extract_city_center(element)
        current = result.get(cache_key)
        candidate = {
            "name": city_name,
            "region": region_name,
            "population": population,
            "type": str(tags.get("place") or "city"),
            "osm_id": element.get("id"),
            "osm_type": element.get("type"),
            "lat": lat,
            "lon": lon,
        }
        if current is None or population > current.get("population", 0):
            result[cache_key] = candidate

    regionful_by_name = {
        GeoService._normalize_city_name(item["name"])
        for item in result.values()
        if item.get("region")
    }
    cities = [
        item
        for item in result.values()
        if item.get("region") or GeoService._normalize_city_name(item["name"]) not in regionful_by_name
    ]
    cities = sorted(cities, key=lambda item: (-item["population"], item["name"], item["region"]))
    return cities


def _fetch_specific_city(city_name: str, min_population: int) -> list[dict]:
    candidates = []
    try:
        search_result = asyncio.run(GeoService.search_cities(city_name))
    except Exception as exc:
        print(f"[import] specific city search failed city='{city_name}' err='{exc}'")
        return []
    for candidate in search_result or []:
        if not isinstance(candidate, dict):
            continue
        population = _parse_population(candidate.get("population"))
        if population < min_population:
            continue
        candidates.append({
            "name": str(candidate.get("name") or "").strip(),
            "region": _clean_region(candidate.get("region") or ""),
            "population": population,
            "type": str(candidate.get("type") or "city"),
            "osm_id": (candidate.get("raw_data") or {}).get("osm_id"),
            "osm_type": (candidate.get("raw_data") or {}).get("osm_type"),
            "lat": (candidate.get("raw_data") or {}).get("lat"),
            "lon": (candidate.get("raw_data") or {}).get("lon"),
        })
    return candidates


def _fetch_districts_or_streets(city: dict, allow_fallback: bool) -> list[str]:
    city_name = city["name"]
    region_name = city.get("region", "")

    districts = GeoService._fetch_districts_overpass(city_name, region_name)
    if districts:
        return GeoService._filter_best_districts(districts)

    streets = GeoService._fetch_streets_overpass(city_name, region_name)
    if streets:
        return streets

    if allow_fallback:
        return ["Центр", "За городом"]
    return []


def _flush_docs(population_cache: dict, districts_cache: dict) -> None:
    PostgresDocumentStore.set_document(GEO_CITY_POPULATION_DOC_KEY, population_cache)
    PostgresDocumentStore.set_document(GEO_DISTRICTS_CACHE_DOC_KEY, districts_cache)


def _load_existing_doc(key: str) -> dict:
    data = PostgresDocumentStore.get_document(key, {})
    return data if isinstance(data, dict) else {}


def main() -> int:
    args = _parse_args()

    if not DATABASE_URL:
        print("DATABASE_URL is required for this import script")
        return 1

    PostgresDocumentStore.initialize()

    population_cache = _load_existing_doc(GEO_CITY_POPULATION_DOC_KEY)
    districts_cache = _load_existing_doc(GEO_DISTRICTS_CACHE_DOC_KEY)

    print("[import] fetching city list from OSM/Overpass...")
    cities = _fetch_city_elements(args.min_population)

    if args.city:
        extra_candidates = []
        for city_name in args.city:
            extra_candidates.extend(_fetch_specific_city(city_name, args.min_population))
        existing_keys = {_build_cache_key(item["name"], item.get("region", "")) for item in cities}
        for item in extra_candidates:
            key = _build_cache_key(item["name"], item.get("region", ""))
            if key and key not in existing_keys:
                cities.append(item)
                existing_keys.add(key)
        cities.sort(key=lambda item: (-item["population"], item["name"], item["region"]))

    if args.offset:
        cities = cities[args.offset :]
    if args.limit > 0:
        cities = cities[: args.limit]

    print(f"[import] cities queued: {len(cities)}")

    processed = 0
    imported = 0
    skipped = 0
    failed = 0
    pending_flush = 0

    for index, city in enumerate(cities, start=1):
        city_name = city["name"]
        region_name = city.get("region", "")
        population = city["population"]
        cache_key = _build_cache_key(city_name, region_name)

        if not cache_key:
            skipped += 1
            continue

        has_population = isinstance(population_cache.get(cache_key), dict)
        has_districts = isinstance(districts_cache.get(cache_key), list) and bool(districts_cache.get(cache_key))
        if args.skip_existing and has_population and has_districts:
            skipped += 1
            print(f"[{index}/{len(cities)}] skip existing: {city_name} ({region_name or 'no-region'})")
            continue

        try:
            locations = _fetch_districts_or_streets(city, allow_fallback=args.allow_fallback)
            if not locations:
                failed += 1
                print(f"[{index}/{len(cities)}] no districts/streets: {city_name} ({region_name or 'no-region'})")
                continue

            population_cache[cache_key] = {"population": int(population)}
            districts_cache[cache_key] = locations

            if args.warm_catalog:
                CatalogService.get_products_for_city(city_name, population, region_name or None)

            imported += 1
            processed += 1
            pending_flush += 1
            print(
                f"[{index}/{len(cities)}] imported: {city_name} ({region_name or 'no-region'}) "
                f"pop={population} places={len(locations)}"
            )

            if pending_flush >= max(1, args.flush_every):
                _flush_docs(population_cache, districts_cache)
                pending_flush = 0

            if args.delay_seconds > 0:
                time.sleep(args.delay_seconds)
        except KeyboardInterrupt:
            print("[import] interrupted, flushing current progress...")
            if pending_flush:
                _flush_docs(population_cache, districts_cache)
            raise
        except Exception as exc:
            failed += 1
            print(f"[{index}/{len(cities)}] failed: {city_name} ({region_name or 'no-region'}) err='{exc}'")

    if pending_flush:
        _flush_docs(population_cache, districts_cache)

    print(
        "[import] done: "
        f"queued={len(cities)} imported={imported} skipped={skipped} failed={failed} processed={processed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())