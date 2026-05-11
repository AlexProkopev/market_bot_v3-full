import math
import os
import re
from io import BytesIO

from PIL import Image, ImageDraw, ImageFont

from app.services.catalog import CatalogService
from app.services.geo import GeoService
from app.services.pricing import PricingService
from app.utils import get_now_msk


class PdfExportService:
    PAGE_WIDTH = 1240
    PAGE_HEIGHT = 1754
    MARGIN_X = 80
    MARGIN_Y = 90
    LINE_GAP = 10

    @staticmethod
    def build_catalog_pdf() -> bytes:
        lines = PdfExportService._build_catalog_lines()
        return PdfExportService._render_pdf(lines)

    @staticmethod
    def _build_catalog_lines() -> list[tuple[str, str]]:
        geo_cache = GeoService._load_cache()
        catalog_cache = CatalogService._load_cache()
        inventory = CatalogService._load_inventory()
        all_products = CatalogService.get_all_products()
        global_stash_types = CatalogService.get_available_stash_types()
        city_pricing = PricingService.get_city_entries()
        region_pricing = PricingService.get_region_entries()

        product_by_id = {
            product.get("id"): product
            for product in all_products
            if isinstance(product, dict) and product.get("id")
        }

        pretty_names: dict[str, dict[str, str]] = {}
        city_keys: set[str] = set()

        for raw_key in (geo_cache or {}).keys():
            if not isinstance(raw_key, str):
                continue
            city_label, region_label = GeoService._parse_cache_city_key(raw_key)
            city_norm = GeoService._normalize_city_name(raw_key)
            if not city_norm:
                continue
            city_keys.add(city_norm)
            pretty_names[city_norm] = {
                "city": city_label or city_norm.title(),
                "region": region_label,
            }

        for raw_key in (catalog_cache or {}).keys():
            if not isinstance(raw_key, str):
                continue
            city_norm = GeoService._normalize_city_name(raw_key)
            if city_norm:
                city_keys.add(city_norm)

        for raw_key in (inventory or {}).keys():
            if not isinstance(raw_key, str):
                continue
            city_norm = GeoService._normalize_city_name(raw_key)
            if city_norm:
                city_keys.add(city_norm)

        for entry in city_pricing:
            label = entry.get("label")
            city_norm = GeoService._normalize_city_name(label)
            if city_norm:
                city_keys.add(city_norm)
                pretty_names.setdefault(city_norm, {"city": str(label).strip().title(), "region": ""})

        lines: list[tuple[str, str]] = []
        timestamp = get_now_msk().strftime("%d.%m.%Y %H:%M")
        lines.append(("title", "Выгрузка каталога Marketplace Bot"))
        lines.append(("meta", f"Сформировано: {timestamp}"))
        lines.append(("blank", ""))

        lines.append(("section", "1. Глобальные настройки"))
        lines.append(("text", f"Типы кладов: {', '.join(global_stash_types) if global_stash_types else 'не заданы'}"))
        if region_pricing:
            lines.append(("text", "Региональные наценки:"))
            for entry in region_pricing:
                lines.append(("bullet", f"{entry['label']}: x{entry['value']:.2f}"))
        else:
            lines.append(("text", "Региональные наценки: не заданы"))

        if city_pricing:
            lines.append(("text", "Городские наценки:"))
            for entry in city_pricing:
                lines.append(("bullet", f"{entry['label']}: x{entry['value']:.2f}"))
        else:
            lines.append(("text", "Городские наценки: не заданы"))

        sorted_cities = sorted(city_keys)
        if sorted_cities:
            lines.append(("blank", ""))
            lines.append(("section", "2. Города и каталог"))

        for index, city_norm in enumerate(sorted_cities, start=1):
            pretty = pretty_names.get(city_norm, {"city": city_norm.title(), "region": ""})
            city_label = pretty.get("city") or city_norm.title()
            region_label = pretty.get("region") or ""
            city_header = city_label if not region_label else f"{city_label} ({region_label})"

            lines.append(("city", f"{index}. {city_header}"))

            districts = PdfExportService._get_city_districts(city_norm, geo_cache)
            lines.append(("text", f"Районы/локации: {', '.join(districts) if districts else 'нет данных'}"))

            updated_at = CatalogService.get_last_update_time(city_label)
            lines.append(("text", f"Обновление каталога: {updated_at or 'нет данных'}"))

            city_products = PdfExportService._get_city_products(city_norm, city_label, catalog_cache, product_by_id)
            if not city_products:
                lines.append(("text", "Товары: пока не сгенерированы"))
                lines.append(("blank", ""))
                continue

            lines.append(("text", f"Товаров в каталоге: {len(city_products)}"))
            for product in city_products:
                product_name = product.get("name", "Без названия")
                weight = PdfExportService._extract_weight(product_name)
                lines.append(("product", f"• {product_name}"))
                lines.append(("detail", f"Вес: {weight}"))
                lines.append(("detail", f"Базовая цена: {product.get('base_price', 0)} руб."))

                try:
                    city_product = CatalogService.get_product_by_id(product.get("id"), city_label)
                    current_price = city_product.get("price") if isinstance(city_product, dict) else None
                except Exception:
                    current_price = None
                lines.append(("detail", f"Цена в городе: {current_price} руб." if current_price else "Цена в городе: нет данных"))

                stash_types = PdfExportService._get_product_stash_types(
                    city_norm,
                    product.get("id"),
                    catalog_cache,
                )
                lines.append(("detail", f"Типы кладов: {', '.join(stash_types) if stash_types else 'не заданы'}"))

                available_districts = CatalogService.get_product_districts(city_label, product.get("id"))
                if not available_districts:
                    available_districts = districts
                lines.append((
                    "detail",
                    f"Районы для товара: {', '.join(available_districts) if available_districts else 'все / не заданы'}"
                ))
            lines.append(("blank", ""))

        if not sorted_cities:
            lines.append(("blank", ""))
            lines.append(("text", "Нет данных по городам для выгрузки."))

        return lines

    @staticmethod
    def _get_city_districts(city_norm: str, geo_cache: dict) -> list[str]:
        districts = []
        for raw_key, values in (geo_cache or {}).items():
            if not isinstance(raw_key, str) or GeoService._normalize_city_name(raw_key) != city_norm:
                continue
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, str) and item not in districts:
                        districts.append(item)
        return districts

    @staticmethod
    def _get_city_products(city_norm: str, city_label: str, catalog_cache: dict, product_by_id: dict) -> list[dict]:
        entry = None
        for raw_key, value in (catalog_cache or {}).items():
            if isinstance(raw_key, str) and GeoService._normalize_city_name(raw_key) == city_norm:
                entry = value
                break

        product_ids = []
        if isinstance(entry, dict):
            product_ids = entry.get("ids", [])
        elif isinstance(entry, list):
            product_ids = entry

        products = []
        for product_id in product_ids:
            product = product_by_id.get(product_id)
            if product:
                products.append(product)
        return products

    @staticmethod
    def _get_product_stash_types(city_norm: str, product_id: str, catalog_cache: dict) -> list[str]:
        for raw_key, value in (catalog_cache or {}).items():
            if not isinstance(raw_key, str) or GeoService._normalize_city_name(raw_key) != city_norm:
                continue
            if isinstance(value, dict):
                stash_map = value.get("stash_types")
                if isinstance(stash_map, dict):
                    stash_types = stash_map.get(product_id)
                    if isinstance(stash_types, list) and stash_types:
                        return [str(item) for item in stash_types]
        try:
            return CatalogService.get_product_stash_types(product_id)
        except Exception:
            return []

    @staticmethod
    def _extract_weight(product_name: str) -> str:
        match = re.search(r"(\d+(?:[.,]\d+)?)\s*г", product_name or "", flags=re.IGNORECASE)
        if not match:
            return "не указан"
        return f"{match.group(1).replace(',', '.')} г"

    @staticmethod
    def _resolve_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        font_candidates = [
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf"),
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialbd.ttf"),
            os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "DejaVuSans.ttf"),
        ]
        for candidate in font_candidates:
            if os.path.exists(candidate):
                try:
                    return ImageFont.truetype(candidate, size=size)
                except OSError:
                    continue
        return ImageFont.load_default()

    @staticmethod
    def _measure_height(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> int:
        box = draw.textbbox((0, 0), text or " ", font=font)
        return max(1, box[3] - box[1])

    @staticmethod
    def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> list[str]:
        words = (text or "").split()
        if not words:
            return [""]
        lines = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = word
        lines.append(current)
        return lines

    @staticmethod
    def _render_pdf(lines: list[tuple[str, str]]) -> bytes:
        regular_font = PdfExportService._resolve_font(26)
        title_font = PdfExportService._resolve_font(38)
        section_font = PdfExportService._resolve_font(32)
        city_font = PdfExportService._resolve_font(30)
        product_font = PdfExportService._resolve_font(28)

        font_map = {
            "title": title_font,
            "section": section_font,
            "city": city_font,
            "product": product_font,
            "meta": regular_font,
            "text": regular_font,
            "bullet": regular_font,
            "detail": regular_font,
            "blank": regular_font,
        }
        indent_map = {
            "title": 0,
            "section": 0,
            "city": 0,
            "product": 20,
            "meta": 0,
            "text": 0,
            "bullet": 20,
            "detail": 50,
            "blank": 0,
        }
        extra_gap = {
            "title": 16,
            "section": 12,
            "city": 10,
            "product": 4,
            "meta": 4,
            "text": 4,
            "bullet": 2,
            "detail": 2,
            "blank": 10,
        }

        pages: list[Image.Image] = []

        def new_page() -> tuple[Image.Image, ImageDraw.ImageDraw, int]:
            page = Image.new("RGB", (PdfExportService.PAGE_WIDTH, PdfExportService.PAGE_HEIGHT), "white")
            return page, ImageDraw.Draw(page), PdfExportService.MARGIN_Y

        page, draw, y = new_page()
        max_base_width = PdfExportService.PAGE_WIDTH - (PdfExportService.MARGIN_X * 2)

        for style, text in lines:
            font = font_map.get(style, regular_font)
            indent = indent_map.get(style, 0)
            max_width = max_base_width - indent
            wrapped = [""] if style == "blank" else PdfExportService._wrap_text(draw, text, font, max_width)
            line_height = PdfExportService._measure_height(draw, "Ag", font)
            block_height = max(1, len(wrapped)) * (line_height + PdfExportService.LINE_GAP) + extra_gap.get(style, 0)

            if y + block_height > PdfExportService.PAGE_HEIGHT - PdfExportService.MARGIN_Y:
                pages.append(page)
                page, draw, y = new_page()

            if style == "blank":
                y += extra_gap.get(style, 0)
                continue

            for item in wrapped:
                draw.text((PdfExportService.MARGIN_X + indent, y), item, font=font, fill="black")
                y += line_height + PdfExportService.LINE_GAP
            y += extra_gap.get(style, 0)

        pages.append(page)

        rgb_pages = [page.convert("RGB") for page in pages]
        buffer = BytesIO()
        rgb_pages[0].save(buffer, format="PDF", save_all=True, append_images=rgb_pages[1:])
        return buffer.getvalue()