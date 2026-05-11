import json
import os
import random
import re
import asyncio
import logging
import threading
import secrets
from difflib import SequenceMatcher
from datetime import datetime, timedelta
from time import perf_counter
from app.config import OPENAI_API_KEY
from app.services.catalog import CatalogService
from app.services.geo import GeoService
from app.services.postgres_store import PostgresDocumentStore
from app.services.users import UserService

# Попробуем импортировать openai
try:
    from openai import AsyncOpenAI
    has_openai = True
except ImportError:
    has_openai = False


logger = logging.getLogger("app.services.reviews")

class ReviewsService:
    FILE_PATH = "storage/reviews.json"
    SETTINGS_FILE = "storage/review_settings.json"
    ACTIVE_CITIES_FILE = "storage/active_cities.json"
    PROMPTS_FILE = "storage/review_prompts.json"
    LAST_BATCH_FILE = "storage/reviews_last_batch.json"
    REVIEWS_DOC_KEY = "reviews"
    SETTINGS_DOC_KEY = "review_settings"
    ACTIVE_CITIES_DOC_KEY = "active_cities"
    PROMPTS_DOC_KEY = "review_prompts"
    LAST_BATCH_DOC_KEY = "reviews_last_batch"
    HIDDEN_USERNAME = "Анонимный пират"
    DISTRICT_HINTS = {
        "октябрьский",
        "ленинский",
        "советский",
        "центральный",
        "кировский",
        "заводской",
        "индустриальный",
        "комсомольский",
        "московский",
        "свердловский",
        "фрунзенский",
        "первореченский",
        "железнодорожный",
        "приокский",
        "пролетарский",
        "калининский",
        "краснооктябрьский",
        "дворцовый",
        "новоильинский",
        "заречный",
    }
    
    DEFAULT_SETTINGS = {
        "interval_hours": 12,
        "min_reviews": 5,
        "max_reviews": 10
    }

    @staticmethod
    def _normalize_city_key(value: str | None) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = text.replace("ё", "е")
        text = re.sub(r"\(.*?\)", "", text)
        text = re.sub(r"\bг\.?\s+", "", text)
        text = re.sub(r"\bгород\s+", "", text)
        text = re.sub(r"[^\w\s-]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _normalize_product_key(value: str | None) -> str:
        text = str(value or "").strip().lower()
        if not text:
            return ""
        text = text.replace("ё", "е")
        text = re.sub(r"\([^)]*\)", " ", text)
        text = re.sub(r"[^\w\s-]", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
        return text

    @staticmethod
    def _is_city_key_match(product_key: str | None, district_key: str | None) -> bool:
        prod = ReviewsService._normalize_city_key(product_key)
        dist = ReviewsService._normalize_city_key(district_key)
        if not prod or not dist:
            return False
        if prod == dist:
            return True
        if dist.startswith(prod + " "):
            return True
        if prod.startswith(dist + " "):
            return True
        return False

    @staticmethod
    def _is_invalid_ai_text(text: str | None) -> bool:
        if not text:
            return True
        compact = text.strip()
        if not compact:
            return True
        lowered = compact.lower()
        if "заглушка" in lowered:
            return True
        return False

    @staticmethod
    def _filter_invalid_reviews(entries: list[dict], context: str) -> tuple[list[dict], int]:
        cleaned = []
        removed = 0
        for item in entries or []:
            if ReviewsService._is_invalid_ai_text(item.get("text")):
                removed += 1
                continue
            cleaned.append(item)
        if removed:
            logger.info(
                "[ReviewGen] Removed %s placeholder reviews during %s stage",
                removed,
                context,
            )
        return cleaned, removed

    @staticmethod
    def _normalize_review_text(text: str | None) -> str:
        if not text:
            return ""
        lowered = text.lower()
        cleaned = re.sub(r"[^\w\s]", " ", lowered)
        return " ".join(cleaned.split())

    @staticmethod
    def _collect_recent_texts(reviews: list[dict], limit: int = 400) -> set[str]:
        if not isinstance(reviews, list) or limit <= 0:
            return set()
        recent_slice = reviews[-limit:]
        norms: set[str] = set()
        for item in recent_slice:
            if not isinstance(item, dict):
                continue
            payload = item.get("text")
            normalized = ReviewsService._normalize_review_text(payload)
            if normalized:
                norms.add(normalized)
        return norms

    @staticmethod
    def _is_duplicate_text(candidate: str | None, existing_norms: set[str]) -> bool:
        if not candidate:
            return False
        normalized = ReviewsService._normalize_review_text(candidate)
        if not normalized:
            return False
        return normalized in existing_norms

    @staticmethod
    def get_gen_settings():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(
                ReviewsService.SETTINGS_DOC_KEY,
                ReviewsService.DEFAULT_SETTINGS.copy(),
            )
            if not isinstance(data, dict):
                data = ReviewsService.DEFAULT_SETTINGS.copy()
            for k, v in ReviewsService.DEFAULT_SETTINGS.items():
                if k not in data:
                    data[k] = v
            return data
        if not os.path.exists(ReviewsService.SETTINGS_FILE):
             return ReviewsService.DEFAULT_SETTINGS.copy()
        try:
             with open(ReviewsService.SETTINGS_FILE, "r", encoding="utf-8") as f:
                 data = json.load(f)
                 # Ensure all keys exist
                 for k, v in ReviewsService.DEFAULT_SETTINGS.items():
                     if k not in data:
                         data[k] = v
                 return data
        except Exception:
             return ReviewsService.DEFAULT_SETTINGS.copy()

    @staticmethod
    def update_gen_settings(interval=None, min_r=None, max_r=None):
        settings = ReviewsService.get_gen_settings()
        if interval is not None: settings["interval_hours"] = float(interval)
        if min_r is not None: settings["min_reviews"] = int(min_r)
        if max_r is not None: settings["max_reviews"] = int(max_r)
        
        # Validation
        if settings["min_reviews"] > settings["max_reviews"]:
             settings["max_reviews"] = settings["min_reviews"]

        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ReviewsService.SETTINGS_DOC_KEY, settings)
            return True
             
        try:
            with open(ReviewsService.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2)
            return True
        except Exception as e:
            print(f"Err saves settings: {e}")
            return False

    TEXTS = [
        "В касание, от души! 🤝",
        "Дома! Кура красава, место топ.",
        "Птичка в клетке. 10/10",
        "Пришлось покопаться, но все ровно.",
        "Кач пушка, по весу тоже норм.",
        "Съем в секунду, спасибо магазину.",
        "Все гуд, забегу еще.",
        "Место, конечно, дебри, но зато надежно. Снял.",
        "Ровно. Рекомендую.",
        "Всё по красоте, как всегда.",
        "Магазин ровный, беру не первый раз.",
        "Четко. Вес соответствует.",
        "Поднял без проблем.",
        "Корды точные, фото понятное. 5+",
        "Был ненаход в прошлый раз, дали пз, его забрал! Магазин честный.",
        "На изи, спс.",
        "Шкуроходы мимо, кладмен гений)",
        "Забрал, качество огонь 🔥",
        "Долго искал локацию, но по метке все четко.",
        "Магаз топчик, процветания!",
        "Всё супер, спасибо за работу.",
        "Квест легкий, стафф рабочий.",
        "Первый раз тут, все прошло гладко.",
        "Качество 10/10, вес 10/10, место 8/10.",
        "Все на месте, спасибо!",
        "Координаты четкие, забрал в касание.",
        "От души душевной, место тихое.",
        "По весу ровно, качество вышка.",
        "Накур плотный, доволен как слон 🐘",
        "Стафф 10 из 10, давно такого не курил.",
        "Стафф топ, спасибо команде!",
        "Товар зачет, качество порадовало.",
        "Качество огонь, всем советую! 🔥🔥🔥",
        "Мощно, одной хапки хватило.",
        "Убойная вещь, буду брать еще.",
        "Магазин держит марку, стафф отличный.",
        "Шишки ароматные, эффект бомба.",
        "Камень твердый, крошится хорошо, прет отлично.",
        "Криссы белоснежные, качество вышка.",
        
        # Более длинные и живые отзывы
        "Сложно было найти место. Поищу в следующий раз. Если лежит-найду.",
        "Молодцы Вы. Спасибо за проделанную работу. Всё как всегда на высоте. 10/10/10. Магазин рекомендую.",
        "Снят, через 30 минут после покупки. Качество не пробовал меня вряд-ли возьмет тк я курю каждый день…",
        "Магазин рекомендую, всегда на месте, качество неизменно, всегда вышка!",
        "В касание пришёл наклонился забрал Кура Красава качество не пробовал еще",
        "Место шкуроходное, но кура молодец, спрятал надежно. Подъем в касание.",
        "Пришел, увидел, победил. Вес ровный, качество бомба, буду брать ещё 🤝",
        "Не первый раз беру, всегда ровно. В этот раз пришлось поискать, но все на месте.",
        "От души! Клад в касание, стафф рабочий, настроение поднято.",
        "Всё чётко, как в аптеке. Снял за секунду, качество пушка, всем доволен.",
        "Спасибо команде за ровный движ. Место укромное, чайки мимо.",
        "Забрал без проблем. Упаковано надежно, по весу все ровно. Качество топчик 🔥",
        "Долго шел к месту, ноги промочил, но оно того стоило. Качество перекрывает все минусы.",
        "Координаты четкие, описание понятное. Подошел и забрал. Магазу респект.",
        "Всё дома! Спасибо за сервис, буду рекомендовать друзьям.",
    ]

    # Фразы для конструктора (для создания уникальных комбинаций)
    PHRASES_INTRO = [
        "В касание.", "Все дома.", "На месте.", "Поднял.", "Забрал.",
        "Съем быстрый.", "Квест пройден.", "Нашел сразу.", "Всё гуд.",
        "Дома.", "Изи квест.", "Снял.", "На базе."
    ]
    PHRASES_BODY = [
        "Вес ровный.", "Качество огонь.", "Кладмен красава.", "Место тихое.",
        "Упаковка надежная.", "Стафф рабочий.", "По весу четко.", "Корды точные.",
        "Камень мощный.", "Курнул и забыл.", "Проблем не возникло.", "Чайки мимо."
    ]
    PHRASES_OUTRO = [
        "Советую.", "10/10.", "Забегу еще.", "Магазу респект.", "Обнял.",
        "Рекомендую.", "Вернусь.", "Всем мир.", "Спасибо.", "От души.",
        "Магазин топ.", "Вы лучшие."
    ]

    PHRASES_EXTRA = [
        "Жаль только, что не живу рядом.",
        "Быстрее бы открыли ещё одно такое место.",
        "Мимо проходил, а тут такой кайф.",
        "После похода настроение как после праздника.",
        "Уровень сервиса завёл меня окончательно.",
        "Все в духе честного старого склада.",
        "Эх, если бы у меня был такой кладу — не вылазил бы из дома.",
        "Отдал бы тимуренку, кто сделал метку, пятёрку.",
        "Отходняк не возьмет, все по делу.",
        "Сказали просто: бери и забывай — так и получилось."
    ]

    PRODUCT_ALIAS_CONFIG = [
        (("kriss", "крисс", "криss", "крiss"), ["кр", "крисс", "крс", "ск", "крис", "криссы", "крисы"]),
        (("шиш", "бошк", "og kush", "white widow"), ["шиш", "шш", "хашка"]),
        (("меf", "mef", "меф", "мефедрон"), ["меф", "мефодий", "миф", "мф", "м", "кристалл"]),
        (("метадон", "мёд", "мед"), ["мед", "сладкий", "мёд"])
    ]

    NAMES = [
        # --- Английские Ники ---
        "Alex", "Dmitry", "Sergey", "Andrey", "Maxim", "Ivan", 
        "Kirill", "Anton", "Stas", "Vlad", "Egor", "Nikita", "Ilya",
        "Artem", "Roman", "Vadan", "Misha", "Sanek", "Dimon",
        "Hottabych", "Joker", "Speedy", "Stalker", "Ghost", "Viper",
        "Kladmen", "Pikachu", "Demon", "Neo", "Morpheus", "Riddick",
        "Shaman", "Ninja", "User", "Gamer", "Pro", "Anon", "Dark",
        # --- Русские имена и клички ---
        "Саня", "Димон", "Леха", "Колян", "Серга", "Влад", "Артем",
        "Мишаня", "Жека", "Пашок", "Кирюха", "Макс", "Андрюха",
        "Стас", "Виталик", "Юрок", "Толян", "Серый", "Тема",
        "Никитос", "Илюха", "Ромыч", "Денчик", "Вадя", "Гриша",
        "Вован", "Славик", "Тимур", "Руслан", "Мага", "Ахмед",
        "Бодя", "Сеня", "Федя", "Гоша", "Ярик", "Даня",
        "Малой", "Лысый", "Рыжий", "Хмурый", "Дикий", "Четкий",
        "Бродяга", "Кот", "Заяц", "Волк", "Медведь", "Тигр",
        "Зверь", "Псих", "Док", "Кэп", "Шеф", "Босс", "Мастер",
        "Профессор", "Студент", "Повар", "Химик", "Гровер",
        "Танкист", "Летчик", "Моряк", "Дед", "Батя", "Брат"
    ]

    # База знаний о городах и их реальных районах/местах
    # Эту структуру можно расширять автоматически в будущем
    CITY_KNOWLEDGE = {
        "Москва": ["ЦАО", "Южное Бутово", "Басманный", "Химки", "Москва-Сити", "ВДНХ", "Парк Горького", "Таганка", "Чертаново"],
        "Санкт-Петербург": ["Думская", "Василеостровский", "Мурино", "Купчино", "Невский", "Лиговка", "Девяткино"],
        "Екатеринбург": ["Уралмаш", "Химмаш", "Ельцин Центр", "Вайнера", "Академический", "Сортировка"],
        "Новосибирск": ["Академгородок", "Ленинский", "Заельцовский", "Березовая роща", "Речной вокзал"],
        "Казань": ["Баумана", "Кремль", "Московский р-н", "Азино", "Квартала"],
        "Сочи": ["Адлер", "Мамайка", "Центр", "Красная Поляна", "Хоста"],
        "Краснодар": ["ФМР", "ЮМР", "Парк Галицкого", "Музыкальный", "Энка"],
        "Ростов-на-Дону": ["Северный", "Западный", "Нахичевань", "Левбердон", "Центр"]
    }
    
    # Общие локации для городов, которых нет в базе или для разнообразия
    GENERIC_LOCATIONS = [
        "Центр", "Вокзал", "Парк", "Набережная", "Промзона", 
        "Гаражи", "Частный сектор", "Лесополоса", "Дачи", "Спальный р-н"
    ]

    FAKE_ITEMS = [
        "Шишки (OG Kush) 1г", "Шишки (Amnesia) 2г", "Шишки (White Widow) 3г",
        "Гашиш (Euro) 1г", "Гашиш (Ice-O-Lator) 2г", "Мефедрон (Кристалл) 0.5г",
        "Мефедрон (Кристалл) 1г", "Альфа-ПВП (Белая) 0.5г", "Амфетамин (HQ) 1г",
        "Кокаин (Колумбия) 0.5г", "Бошки (AK-47) 5г", "XTC (Punisher) 2шт"
    ]

    @staticmethod
    def _get_real_usernames():
        """Получает список реальных никнеймов из базы пользователей."""
        try:
            data = UserService._load_db()
        except Exception:
            return []

        users = data.get("users", {}) if isinstance(data, dict) else {}
        names = []
        for u in users.values():
            uname = u.get("username")
            if uname and len(uname) > 3:
                names.append(uname)
        return names

    @staticmethod
    def _generate_username():
        """Wrapper for _generate_fake_user used by AI task"""
        # We ignore the 'base_name' return value and just take the formatted one
        username, _ = ReviewsService._generate_fake_user()
        return username

    @staticmethod
    def _generate_fake_user(used_base_names=None):
        """
        Возвращает маскированное имя для отображения в отзывах.
        """
        return ReviewsService.HIDDEN_USERNAME, ReviewsService.HIDDEN_USERNAME

    @staticmethod
    def _load_reviews():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ReviewsService.REVIEWS_DOC_KEY, [])
            reviews = data if isinstance(data, list) else []
            cleaned, removed = ReviewsService._filter_invalid_reviews(reviews, "load")
            if removed or ReviewsService._ensure_review_fields(cleaned):
                ReviewsService._save_reviews(cleaned)
            return cleaned
        if os.path.exists(ReviewsService.FILE_PATH):
            try:
                with open(ReviewsService.FILE_PATH, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    reviews = data if isinstance(data, list) else []
            except Exception:
                return []
            cleaned, removed = ReviewsService._filter_invalid_reviews(reviews, "load")
            if removed:
                ReviewsService._save_reviews(cleaned)
            if ReviewsService._ensure_review_fields(cleaned):
                ReviewsService._save_reviews(cleaned)
            return cleaned
        return []

    @staticmethod
    def _save_reviews(reviews):
        # Removed limit to store ALL reviews as requested
        # if len(reviews) > 500:
        #    reviews = reviews[-500:]
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ReviewsService.REVIEWS_DOC_KEY, reviews)
            return
            
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
            
        try:
            with open(ReviewsService.FILE_PATH, 'w', encoding='utf-8') as f:
                json.dump(reviews, f, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Failed to save reviews")

    @staticmethod
    def _ensure_review_fields(reviews: list[dict]) -> bool:
        changed = False
        for item in reviews or []:
            if not isinstance(item, dict):
                continue
            if not item.get("id"):
                item["id"] = secrets.token_hex(6)
                changed = True
            if "hidden" not in item:
                item["hidden"] = False
                changed = True
        return changed

    @staticmethod
    def _split_item_info(item_info: str | None) -> tuple[str, str, str]:
        raw = str(item_info or "").strip()
        if not raw:
            return "", "", ""

        if "|" not in raw:
            return raw, "", ""

        product_part, location_part = raw.split("|", 1)
        product = product_part.strip()
        location = location_part.strip()

        if not location:
            return product, "", ""

        if "," not in location:
            return product, location, ""

        city_part, district_part = location.split(",", 1)
        return product, city_part.strip(), district_part.strip()

    @staticmethod
    def _build_item_info(product: str, city: str, district: str) -> str:
        product_value = str(product or "").strip()
        city_value = str(city or "").strip()

        location_value = city_value

        if product_value and location_value:
            return f"{product_value} | {location_value}"
        if product_value:
            return product_value
        return location_value

    @staticmethod
    def _save_last_batch(iso_dates: list[str]) -> None:
        payload = {
            "iso_dates": iso_dates or [],
            "saved_at": datetime.now().isoformat(),
        }
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ReviewsService.LAST_BATCH_DOC_KEY, payload)
            return
        try:
            os.makedirs(os.path.dirname(ReviewsService.LAST_BATCH_FILE), exist_ok=True)
            with open(ReviewsService.LAST_BATCH_FILE, "w", encoding="utf-8") as fp:
                json.dump(payload, fp, ensure_ascii=False, indent=2)
        except Exception:
            logger.exception("Failed to persist last review batch marker")

    @staticmethod
    def _load_last_batch() -> list[str]:
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ReviewsService.LAST_BATCH_DOC_KEY, {})
            iso_dates = data.get("iso_dates") if isinstance(data, dict) else None
            if isinstance(iso_dates, list):
                return [str(item) for item in iso_dates if isinstance(item, str)]
            return []
        if not os.path.exists(ReviewsService.LAST_BATCH_FILE):
            return []
        try:
            with open(ReviewsService.LAST_BATCH_FILE, "r", encoding="utf-8") as fp:
                data = json.load(fp)
            iso_dates = data.get("iso_dates")
            if isinstance(iso_dates, list):
                return [str(item) for item in iso_dates if isinstance(item, str)]
        except Exception:
            logger.exception("Failed to load last review batch marker")
        return []

    @staticmethod
    def remove_last_generated_batch() -> int:
        iso_dates = ReviewsService._load_last_batch()
        if not iso_dates:
            return 0

        reviews = ReviewsService._load_reviews()
        if not reviews:
            ReviewsService._save_last_batch([])
            return 0

        target_set = set(iso_dates)
        filtered = [rev for rev in reviews if rev.get("iso_date") not in target_set]
        removed = len(reviews) - len(filtered)

        if removed:
            ReviewsService._save_reviews(filtered)
        ReviewsService._save_last_batch([])
        return removed

    @staticmethod
    def generate_mass_reviews():
        """Generates random 4-10 reviews for EACH available city."""
        # Load caches
        products_cache = CatalogService._load_cache()
        districts_cache = GeoService._load_cache()
        
        # Find intersections (valid targets)
        targets = []
        for p_key in products_cache:
            for d_key in districts_cache:
                if ReviewsService._is_city_key_match(p_key, d_key):
                    targets.append((p_key, d_key))
                    break
        
        if not targets:
            return 0
            
        reviews = ReviewsService._load_reviews()
        now = datetime.now()
        count = 0
        
        for p_key, d_key in targets:
             # Random number of reviews per city/target (4 to 10)
             num_reviews = random.randint(4, 10)
             
             for _ in range(num_reviews):
                 # Spread time over last 48 hours to make it look organic
                 # But weighted towards recent? No, simple random across 2 days is fine.
                 minutes_back = random.randint(0, 48 * 60) 
                 dt = now - timedelta(minutes=minutes_back)
                 
                 new_rev = ReviewsService._create_review_dict(dt, reviews, forced_city_info=(p_key, d_key))
                 if new_rev:
                     reviews.append(new_rev)
                     count += 1
        
        # Sort by date so new ones are at the end (or correctly ordered)
        reviews.sort(key=lambda x: x['iso_date'])
        
        ReviewsService._save_reviews(reviews)
        return count

    @staticmethod
    def force_regenerate():
        """Удаляет текущие отзывы и генерирует новые на основе истории"""
        if os.path.exists(ReviewsService.FILE_PATH):
            try:
                os.remove(ReviewsService.FILE_PATH)
            except:
                pass
        
        # Запускаем первоначальный посев
        return ReviewsService._seed_initial_reviews()

    @staticmethod
    def _generate_local_review_text(product_name: str | None = None,
                                    city_name: str | None = None,
                                    district_name: str | None = None,
                                    price: int | float | None = None,
                                    forbidden_norms: set[str] | None = None) -> str:
        rng = random

        product = (product_name or rng.choice(ReviewsService.FAKE_ITEMS)).strip()
        product_clean = product.split('|')[0].strip()
        product_simple = product_clean.split('(')[0].strip() or "Стафф"
        product_lower = product_simple.lower()

        def style_alias(value: str) -> str:
            roll = rng.random()
            if roll < 0.3:
                return value.upper()
            if roll < 0.6:
                return value.capitalize()
            return value

        alias_value = None
        alias_options = []
        for keywords, options in ReviewsService.PRODUCT_ALIAS_CONFIG:
            if any(key in product_lower for key in keywords):
                alias_options = list(options)
                alias_value = style_alias(rng.choice(alias_options))
                break

        city = city_name or rng.choice(list(ReviewsService.CITY_KNOWLEDGE.keys()))
        city_display = city.strip()

        district = district_name.strip() if district_name else rng.choice(
            ReviewsService.CITY_KNOWLEDGE.get(city_display, ReviewsService.GENERIC_LOCATIONS)
        )

        product_label = alias_value or product_simple

        alias_token_raw = alias_value or product_simple
        alias_variants = []
        if alias_token_raw:
            base_alias = alias_token_raw.strip()
            if base_alias:
                base_alias = base_alias.split()[0]
                alias_variants = [base_alias, base_alias.lower(), base_alias.upper()]

        short_starts = [
            "Снято",
            "Забрал",
            "Дома",
            "Сходу",
            "На месте",
            "Чётко",
            "Сразу",
            "Без суеты",
            "Огонь",
            "Взял"
        ]
        short_payloads = [
            "кач",
            "кач пушка",
            "кач ровно",
            "качуха",
            "кач топ",
            "огонь",
            "кайф",
            "топ",
            "ровно",
            "ровно кач",
            "без шума",
            "в касание"
        ]
        if alias_variants:
            short_payloads.extend(alias_variants)

        short_endings = [
            "кайф",
            "топ",
            "ровно",
            "чётко",
            "без шума",
            "в касание",
            "по красоте",
            "без суеты",
            "норм"
        ]

        short_templates = [
            "{start}, {payload}",
            "{payload} {ending}",
            "{start} {payload}",
            "{start} {ending}",
            "{payload}",
            "{alias} {ending}"
        ]

        def finalize_phrase(phrase: str) -> str:
            phrase = re.sub(r"\s+", " ", phrase).strip()
            tokens = re.findall(r"[\wЁё]+", phrase)
            tokens = [tok for tok in tokens if tok]
            if not tokens:
                tokens = [rng.choice(short_payloads)]

            target_words = rng.choices([1, 2, 3, 4, 5], weights=[3, 4, 4, 3, 1], k=1)[0]
            if len(tokens) > target_words:
                tokens = tokens[:target_words]

            if len(tokens) > 5:
                tokens = tokens[:5]

            assembled = " ".join(tokens)
            roll = rng.random()
            if roll < 0.3:
                assembled = assembled.lower()
            elif roll < 0.45:
                assembled = assembled.upper()
            else:
                assembled = assembled[:1].upper() + assembled[1:]

            if rng.random() < 0.2 and not assembled.endswith("!"):
                assembled += "!"

            return assembled.strip().replace("—", "-").replace("–", "-")

        def build_short() -> str:
            template = rng.choice(short_templates)
            values = {
                "start": rng.choice(short_starts),
                "payload": rng.choice(short_payloads),
                "ending": rng.choice(short_endings),
                "alias": rng.choice(alias_variants) if alias_variants else rng.choice(short_payloads),
            }
            phrase = template.format(**values)
            return finalize_phrase(phrase)

        alias_for_long = alias_variants[0] if alias_variants else product_label

        long_openers = [
            "Снял без лишнего шума",
            "Только что забрал точку",
            "Доехал и сразу забрал",
            "Подтянулся и унёс",
            "Без суеты добрался и снял"
        ]
        long_core = [
            f"{alias_for_long} зашёл мягко",
            f"{alias_for_long} держит планку",
            f"{alias_for_long} — ровный",
            "Фасовка аккуратная",
            "Корды оказались точные",
            "Качество пушка, без сюрпризов"
        ]
        long_closers = [
            "Всё тихо, без палев",
            "Лаванда снова не подвела",
            "Буду брать ещё",
            "Поехал домой без нервов",
            "Настрой ровный, кач кайф"
        ]

        replacements = {
            "спасибо": "спс",
            "сейчас": "щас",
            "очень": "оч",
            "ребята": "пацаны",
            "точно": "точняк"
        }

        def build_long() -> str:
            segments = [rng.choice(long_openers), rng.choice(long_core)]
            if rng.random() < 0.6:
                segments.append(rng.choice(long_closers))

            sentences = []
            for segment in segments:
                seg = segment.strip()
                if not seg:
                    continue
                if rng.random() < 0.35:
                    seg = seg.lower()
                elif rng.random() < 0.15:
                    seg = seg.upper()
                else:
                    seg = seg[:1].upper() + seg[1:]
                if not seg.endswith(('.', '!', '?')):
                    seg += rng.choice(['.', '!'])
                sentences.append(seg)

            text = " ".join(sentences).strip()

            for src, dst in replacements.items():
                if rng.random() < 0.25:
                    text = text.replace(src, dst)

            if rng.random() < 0.2:
                emoji_pool = ["🔥", "🤝", "✅", "👌", "💨", "😎", "🙏", "💯", "✨", "⚡"]
                text = text.rstrip('.!') + " " + rng.choice(emoji_pool)

            return finalize_phrase(text)

        for _ in range(30):
            text = build_short() if rng.random() < 0.85 else build_long()
            normalized = ReviewsService._normalize_review_text(text)
            if forbidden_norms is None or (normalized and normalized not in forbidden_norms):
                if forbidden_norms is not None and normalized:
                    forbidden_norms.add(normalized)
                return text

        fallback_text = finalize_phrase(rng.choice(ReviewsService.TEXTS))
        normalized = ReviewsService._normalize_review_text(fallback_text)
        if forbidden_norms is not None and normalized:
            forbidden_norms.add(normalized)
        return fallback_text

    @staticmethod
    def _get_random_text(product_name: str | None = None,
                         city_name: str | None = None,
                         district_name: str | None = None,
                         price: int | float | None = None,
                         forbidden_norms: set[str] | None = None) -> str:
        ai_text = ReviewsService._generate_ai_text_sync(
            product_name=product_name,
            city_name=city_name,
            district_name=district_name,
            item_price=price
        )
        if ai_text and not ReviewsService._is_invalid_ai_text(ai_text):
            if forbidden_norms and ReviewsService._is_duplicate_text(ai_text, forbidden_norms):
                logger.info("[ReviewGen] AI sync duplicate detected, switching to template fallback")
            else:
                if forbidden_norms is not None:
                    norm = ReviewsService._normalize_review_text(ai_text)
                    if norm:
                        forbidden_norms.add(norm)
                return ai_text
        if ai_text and forbidden_norms:
            logger.info("[AI Review] Sync response rejected (duplicate or invalid) for city=%s", city_name or "?")
        if ai_text:
            logger.warning(
                "[AI Review] Dropped invalid AI response, switching to template (city=%s, district=%s, product=%s)",
                city_name or "?",
                district_name or "?",
                product_name or "?",
            )
        else:
            logger.info(
                "[AI Review] Fallback to template (city=%s, district=%s, product=%s)",
                city_name or "?",
                district_name or "?",
                product_name or "?",
            )
        return ReviewsService._generate_local_review_text(
            product_name,
            city_name,
            district_name,
            price,
            forbidden_norms=forbidden_norms
        )

    @staticmethod
    def _create_review_dict(dt_obj, recent_reviews=None, forced_city_info=None):
        product_name = None
        product_price = None
        city = None
        district = None
        item = None

        # Грузим кэши
        products_cache = CatalogService._load_cache() # {"city": {"ids": [...]}}
        districts_cache = GeoService._load_cache()    # {"city": ["d1", "d2"]}
        
        # Пересечение: города, которые есть И там И там (с учетом неточного совпадения ключей)
        available_cities = []
        city_mapping = {} # p_key -> d_key

        for p_key in products_cache:
            for d_key in districts_cache:
                # Проверяем совпадение по нормализованному имени города
                # Например p_key="томск", d_key="томск (томская область)"
                if ReviewsService._is_city_key_match(p_key, d_key):
                    available_cities.append(p_key)
                    city_mapping[p_key] = d_key
                    break

        used_catalog = False
        
        # Если есть города в кэше, выбираем из них
        if available_cities:
            # Выбираем город
            if forced_city_info:
               # forced_city_info is (p_key, d_key)
               chosen_p_key, chosen_d_key = forced_city_info
               # Verify it exists in our map or cache? Trust caller?
               # Let's trust caller but verify cache presence briefly
               if chosen_p_key not in products_cache or chosen_d_key not in districts_cache:
                   # Fallback to random if invalid
                   chosen_p_key = random.choice(available_cities)
                   chosen_d_key = city_mapping[chosen_p_key]
            else:
               chosen_p_key = random.choice(available_cities)
               chosen_d_key = city_mapping[chosen_p_key]
            
            city = chosen_p_key.title()

            # Теперь получаем список DISTRICTS
            districts_list = districts_cache[chosen_d_key]

            # Получаем список ТОВАРОВ
            prod_entry = products_cache[chosen_p_key]
            prod_ids = prod_entry.get("ids", []) if isinstance(prod_entry, dict) else prod_entry
            
            # Находим полные объекты товаров
            all_prods = CatalogService.get_all_products()
            real_products = [p for p in all_prods if p["id"] in prod_ids]
            
            if real_products and districts_list:
                # Ура, у нас есть всё реальное для этого города
                prod = random.choice(real_products)
                dist_raw = random.choice(districts_list)
                dist = ReviewsService._format_location_display(dist_raw)
                
                item = f"{prod['name']}"
                district = dist
                product_name = prod['name']
                product_price = prod.get('base_price')
                used_catalog = True
        
        if not used_catalog:
             # Кэша нет. Используем Generic данные (Fallback для первого запуска)
             f_city_name = random.choice(list(ReviewsService.CITY_KNOWLEDGE.keys()))
             f_districts = ReviewsService.CITY_KNOWLEDGE[f_city_name]
             f_district_raw = random.choice(f_districts)
             f_district = ReviewsService._format_location_display(f_district_raw)
             
             # Items fallback
             # Or use actual catalog if available
             prods = CatalogService.get_all_products()
             if prods:
                 p = random.choice(prods)
                 item = f"{p['name']} ({p['base_price']}р)"
                 product_name = p['name']
                 product_price = p.get('base_price')
             else:
                 item = random.choice(ReviewsService.FAKE_ITEMS)
                 product_name = item
                 product_price = None
             
             city = f_city_name
             district = f_district
             used_catalog = True # Marked as used to proceed

        if not product_name:
            product_name = item or "Стафф"

        text = ReviewsService._get_random_text(
            product_name=product_name,
            city_name=city,
            district_name=district,
            price=product_price
        )

        # Собираем список имен...
        used_names = []
        if recent_reviews:
            check_slice = recent_reviews[-15:]
            for r in check_slice:
                if 'base_name' in r:
                    used_names.append(r['base_name'])

        user_display, base_name = ReviewsService._generate_fake_user(used_base_names=used_names)
        
        stars_count = 5 if random.random() > 0.1 else 4
        
        return {
            "user": user_display,
            "base_name": base_name, 
            "text": text,
            "product": product_name,
            "city": city, # Added specific field
            "item_info": ReviewsService._build_item_info(item, city, district),
            "stars": stars_count,
            "iso_date": dt_obj.isoformat(),
            "display_date": dt_obj.strftime("%d.%m %H:%M")
        }



    @staticmethod
    def _seed_initial_reviews():
        """Генерирует 'историю' отзывов за последние 24-48 часов"""
        reviews = []
        now = datetime.now()
        # Начинаем с 24 часов назад
        current_ts = now - timedelta(hours=24)
        
        while current_ts < now:
            # Добавляем отзыв
            review = ReviewsService._create_review_dict(current_ts, recent_reviews=reviews)
            if review:
                reviews.append(review)
            
            # Следующий отзыв через 15-40 минут
            step_minutes = random.randint(15, 40)
            current_ts += timedelta(minutes=step_minutes)
            
        ReviewsService._save_reviews(reviews)
        return reviews

    @staticmethod
    def check_and_update():
        """
        Проверяет, нужно ли добавить новый отзыв.
        Вызывается при запросе списка отзывов.
        """
        reviews = ReviewsService._load_reviews()
        
        if not reviews:
            return ReviewsService._seed_initial_reviews()
        
        last_review = reviews[-1]
        try:
            last_ts = datetime.fromisoformat(last_review['iso_date'])
        except:
            return ReviewsService._seed_initial_reviews()
            
        now = datetime.now()
        
        if last_ts > now:
            last_ts = now - timedelta(minutes=60)

        diff_minutes = (now - last_ts).total_seconds() / 60
        required_gap = random.randint(15, 40)
        
        if diff_minutes > required_gap:
            return reviews

        return reviews


    @staticmethod
    def _format_location_display(name: str) -> str:
        """Return location name without additional prefixes."""
        return name.strip()


        # --- Persistent Cities Tracking ---
    @staticmethod
    def _load_active_cities():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ReviewsService.ACTIVE_CITIES_DOC_KEY, [])
            return data if isinstance(data, list) else []
        if not os.path.exists(ReviewsService.ACTIVE_CITIES_FILE):
            return []
        try:
            with open(ReviewsService.ACTIVE_CITIES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []

    @staticmethod
    def _save_active_cities(cities):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ReviewsService.ACTIVE_CITIES_DOC_KEY, cities)
            return
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        try:
            with open(ReviewsService.ACTIVE_CITIES_FILE, "w", encoding="utf-8") as f:
                json.dump(cities, f, ensure_ascii=False, indent=2)
        except:
            pass

    @staticmethod
    def register_active_city(city_name):
        cities = ReviewsService._load_active_cities()
        if city_name not in cities:
            cities.append(city_name)
            ReviewsService._save_active_cities(cities)

    @staticmethod
    def get_active_cities():
        return ReviewsService._load_active_cities()

    @staticmethod
    def get_manual_city_options(limit: int | None = None) -> list[dict[str, str]]:
        """Возвращает города с доступными товарами и районами для ручных отзывов."""
        products_cache = CatalogService._load_cache()
        districts_cache = GeoService._load_cache()

        if not products_cache or not districts_cache:
            return []

        active_cities = ReviewsService.get_active_cities()
        active_lookup: dict[str, str] = {}
        for name in active_cities:
            if isinstance(name, str):
                key = name.lower().strip()
                if key:
                    active_lookup[key] = name.strip()

        def _has_products(entry) -> bool:
            if isinstance(entry, dict):
                ids = entry.get("ids")
                return isinstance(ids, list) and bool(ids)
            if isinstance(entry, list):
                return bool(entry)
            return False

        def _has_district(city_key: str) -> bool:
            for d_key in districts_cache.keys():
                if isinstance(d_key, str) and ReviewsService._is_city_key_match(city_key, d_key):
                    return True
            return False

        seen: set[str] = set()
        prioritized: list[dict[str, str]] = []
        for display in active_cities:
            if not isinstance(display, str):
                continue
            city_key = display.lower().strip()
            if not city_key or city_key in seen:
                continue
            entry = products_cache.get(city_key)
            if not _has_products(entry):
                continue
            if not _has_district(city_key):
                continue
            prioritized.append({
                "display": display.strip(),
                "city_key": city_key
            })
            seen.add(city_key)

        extra: list[dict[str, str]] = []
        for raw_key, entry in products_cache.items():
            if not isinstance(raw_key, str):
                continue
            city_key = raw_key.lower().strip()
            if not city_key or city_key in seen:
                continue
            if not _has_products(entry):
                continue
            if not _has_district(city_key):
                continue
            display = active_lookup.get(city_key) or city_key.title()
            extra.append({
                "display": display.strip(),
                "city_key": city_key
            })
            seen.add(city_key)

        extra.sort(key=lambda item: item["display"])
        combined = prioritized + extra

        if limit is not None and limit > 0 and len(combined) > limit:
            return combined[:limit]

        return combined

    @staticmethod
    def add_manual_review(username: str, city: str, product: str, district: str, text: str):
        reviews = ReviewsService._load_reviews()
        if not reviews:
            reviews = []
            
        now = datetime.now()
        item_info = ReviewsService._build_item_info(product, city, district)
        masked_user = ReviewsService.HIDDEN_USERNAME
        
        new_review = {
            "id": secrets.token_hex(6),
            "user": masked_user,
            "base_name": masked_user,
            "text": text,
            "product": product,
            "city": city,
            "item_info": item_info,
            "stars": 5, 
            "iso_date": now.isoformat(),
            "display_date": now.strftime("%d.%m %H:%M"),
            "hidden": False
        }
        
        reviews.append(new_review)
        ReviewsService._save_reviews(reviews)
        return True

    # --- Custom Prompts Management ---
    @staticmethod
    def _load_custom_prompts():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ReviewsService.PROMPTS_DOC_KEY, [])
            return data if isinstance(data, list) else []
        if not os.path.exists(ReviewsService.PROMPTS_FILE):
            return []
        try:
            with open(ReviewsService.PROMPTS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return []

    @staticmethod
    def add_custom_prompt(phrase):
        phrases = ReviewsService._load_custom_prompts()
        if phrase not in phrases:
            phrases.append(phrase)
            if PostgresDocumentStore.is_enabled():
                PostgresDocumentStore.set_document(ReviewsService.PROMPTS_DOC_KEY, phrases)
                return True
            try:
                with open(ReviewsService.PROMPTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(phrases, f, ensure_ascii=False, indent=2)
                return True
            except:
                return False
        return False

    @staticmethod
    def get_custom_prompts():
        return ReviewsService._load_custom_prompts()

    @staticmethod
    def remove_custom_prompt(phrase):
        phrases = ReviewsService._load_custom_prompts()
        if phrase in phrases:
            phrases.remove(phrase)
            if PostgresDocumentStore.is_enabled():
                PostgresDocumentStore.set_document(ReviewsService.PROMPTS_DOC_KEY, phrases)
                return True
            try:
                with open(ReviewsService.PROMPTS_FILE, "w", encoding="utf-8") as f:
                    json.dump(phrases, f, ensure_ascii=False, indent=2)
                return True
            except:
                return False
        return False

    # --- AI Review Generation ---

    @staticmethod
    async def _generate_ai_text(product_name, city_name, district_name, item_price):
        """Generates feedback text using ChatGPT"""
        if not has_openai or not OPENAI_API_KEY:
            return None # Fallback to standard text

        # Determine style variants to avoid одинаковые отзывы
        styles = [
            "Опытный кладоискатель, уверенный тон, уличный сленг",
            "Новичок, который кайфует от квеста и делится эмоциями",
            "Спокойный постоянник, рассказывает про надёжный тайник",
            "Немного дерзкий, короткие фразы, подчёркивает качество"
        ]
        style = random.choice(styles)

        # Provide optional aliases for the товар
        normalized = product_name.lower()
        alias_options = []
        for keywords, options in ReviewsService.PRODUCT_ALIAS_CONFIG:
            if any(k in normalized for k in keywords):
                alias_options = list(dict.fromkeys([opt.strip() for opt in options if opt]))
                break

        product_aliases = [product_name] + alias_options
        unique_aliases = list(dict.fromkeys([alias.strip() for alias in product_aliases if alias]))
        alias_line = ", ".join(unique_aliases[:6]) if unique_aliases else product_name

        stash_types = ["тайник", "магнит", "прикоп"]
        stash_type = random.choice(stash_types)

        short_mode = random.random() < 0.85

        custom_phrases = ReviewsService._load_custom_prompts()
        custom_instructions = ""
        if custom_phrases:
            phrases_str = ", ".join(f'"{p}"' for p in custom_phrases)
            custom_instructions = (
                "\nЕсли уместно, вплети одну из фраз (можно слегка перефразировать): "
                f"{phrases_str}"
            )
        system_message = (
            """
Роль:
Ты — реальный покупатель закрытого магазина «Лаванда», который только что успешно забрал заказ.

Контекст:
Забор был через случайный тип клада (тайник / магнит / прикоп — выбирай каждый раз рандомно).
Отзыв пишется сразу после подъёма, по горячим эмоциям, как это делают обычные люди в закрытых чатах.

Стиль:
— Живой, разговорный, без официоза и рекламы
— Уличный сленг допустим, но умеренно
— Без литературщины и маркетинговых фраз
— Иногда упоминай магазин: Лаванда / лаванда / LAVANDA (не в каждом отзыве и в разном регистре)
— Чередуй регистр: часть предложений со строчной, часть со стандартной заглавной
— Опечатки и разговорные сокращения допускай примерно в 25% отзывов, остальное пиши нормально

Разнообразие (ОБЯЗАТЕЛЬНО):
Каждый отзыв — от нового человека:
— разные характеры (молчаливый, разговорчивый, опытный, новичок, спокойный, уверенный, скептик)
— разная манера речи
— разный эмоциональный уровень
— разный словарный запас

Длина отзывов:
— в 80% случаев делай ультра короткий отклик на 1–4 слова (можно использовать запятую или дефис, но без точек и кавычек)
— изредка (примерно 1 раз из 5) собирай компактный текст на 1–2 предложения
— избегай длинных «простыней» и однотипных клише
— Не делай все отзывы восторженными — часть должна быть спокойной и сухой.

Запреты:
— Не используй шаблонную структуру
— Не повторяй одинаковые слова в начале, середине или конце предложений
— Не пиши одинаковым стилем
— Не раскрывай внутреннюю кухню магазина
— Не упоминай, что текст сгенерирован
— Не используй много смайлов, но используй иногда (1-2 в 5 отзывах) для живости
— Не вставляй заученные благодарности вроде «Спасибо вам большое», «очень выручили»

Формат вывода:
Просто текст отзыва.
Без заголовков.
Без списков.
Без пояснений.
"""
        )

        alias_instruction = ""
        if short_mode and alias_options:
            alias_instruction = (
                " Всегда используй одно из прозвищ для товара: "
                + ", ".join(alias_options[:6]) + "."
            )

        if short_mode:
            user_message = (
                f"Это {stash_type}. "
                "Сделай реакцию максимально короткой: одно, два, три или четыре слова. Допускай запятую или '-', но без точек, кавычек и длинных предложений. "
                "Не используй числа. Не повторяй прошлые формулировки. Если уместно, вставь прозвище товара. "
                "Без благодарностей и клише вроде 'Спасибо вам большое', 'очень выручили'. "
                "Без обращений и без упоминания времени. Можешь иногда писать со строчной, но чередуй с заглавной. Легкие опечатки допустимы, но не чаще четверти отзывов. "
                "НЕ называй конкретные улицы, подъезды, координаты и НЕ упоминай цену или деньги. "
                "НЕ используй слова 'шишка' или 'шишка топ', если товар не из категории травы или гашиша. "
                "Если используешь тире, бери только короткий '-'. Пиши одной строкой без переносов и без обрывков вроде 'ск', 'в'."
                f"{alias_instruction}"
                f"{custom_instructions}"
            )
        else:
            user_message = (
                f"Это {stash_type}. "
                f"Опиши, как герой дошёл до {stash_type}, отметил, что корды точные и место тихое. "
                f"Сделай акцент, что это именно {stash_type}, а не магазин. Коротко похвали качество или фасовку. "
                "Можно добавить детали: настроение, эмоции, ощущения после пробы, как быстро забрал. "
                "Не упоминай конкретное время суток. "
                "Чередуй пунктуацию: в 70% фраз оставляй конец без знаков, остальные миксуй точками и '!'. "
                "Чередуй регистр — где-то со строчной, где-то с заглавной. Опечатки и разговорные сокращения оставляй примерно в четверти отзывов. "
                "Не вставляй банальные благодарности вроде 'Спасибо вам большое', 'очень выручили'. "
                "НЕ называй конкретные улицы, номера домов, подъезды, координаты и НЕ упоминай цену или деньги. "
                "Длина: 1-4 коротких предложения разной структуры, без кавычек, без списков, без обращений к оператору. "
                "НЕ используй слова 'шишка' или 'шишка топ', если товар не из категории травы или гашиша. "
                "Если используешь тире, бери только короткий '-'. Пиши одной строкой без переносов." 
                f"{custom_instructions}"
            )

        start_time = perf_counter()

        try:
            async with AsyncOpenAI(api_key=OPENAI_API_KEY) as client:
                response = await client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": system_message},
                        {"role": "user", "content": user_message}
                    ],
                    max_tokens=120,
                    temperature=0.85,
                    top_p=0.9
                )
                raw_text = response.choices[0].message.content or ""
                text = raw_text.strip().replace("\r", "\n")
                segments = [seg.strip() for seg in text.split("\n") if seg.strip()]
                text = " ".join(segments)
                text = re.sub(r"\s+", " ", text).strip()
                if short_mode:
                    text = text.replace("—", "-").replace("–", "-")
                    text = re.sub(r"[.,!?;:]+", "", text)
                    words = [w for w in text.split() if w]
                    clean_words = []
                    for word in words:
                        token = word.strip("-")
                        if not token:
                            continue
                        if len(token) == 1 and token.lower() not in {"в", "и", "к"}:
                            continue
                        clean_words.append(token)
                        if len(clean_words) >= 4:
                            break
                    if not clean_words:
                        clean_words = [word.strip("-") for word in words if word.strip("-")][:3]
                    text = " ".join(clean_words).strip()
                else:
                    base_segments = re.split(r"(?<=[.!?])\s+", text) if text else []
                    processed_segments = []
                    for segment in base_segments or [text]:
                        segment = segment.strip()
                        if not segment:
                            continue
                        if len(segment) > 0:
                            if random.random() < 0.5:
                                segment = segment[0].lower() + segment[1:]
                            else:
                                segment = segment[0].upper() + segment[1:]
                        if random.random() < 0.7:
                            segment = re.sub(r"[.,!?;:]+$", "", segment)
                        else:
                            if random.random() < 0.3:
                                segment = re.sub(r"[.!?]+$", "!", segment)
                            elif not re.search(r"[.!?]$", segment):
                                segment = segment + random.choice([".", "!"])
                        processed_segments.append(segment)
                    text = " ".join(processed_segments).strip()
                    if text:
                        typo_pairs = [
                            (r"\bчто\b", "чо"),
                            (r"\bсейчас\b", "щас"),
                            (r"\bочень\b", "оч"),
                            (r"\bнормально\b", "норм"),
                            (r"\bхорошо\b", "харошо"),
                            (r"\bпросто\b", "прсто"),
                            (r"\bещё\b", "еще"),
                            (r"\bтолько\b", "ток"),
                            (r"\bвообще\b", "ваще"),
                            (r"\bреально\b", "реал"),
                        ]
                        typo_trigger = random.random()
                        if typo_trigger < 0.25:
                            for pattern, replacement in typo_pairs:
                                if random.random() < 0.35:
                                    text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
                            if random.random() < 0.2:
                                text = re.sub(r"е", "э", text, count=1)
                            if random.random() < 0.15:
                                text = re.sub(r"и", "ы", text, count=1)
                            if random.random() < 0.15:
                                text = re.sub(r"[тд]ся\b", "ца", text, count=1)
                            if random.random() < 0.2:
                                text = re.sub(r"\bчтобы\b", "чтоб", text, flags=re.IGNORECASE)
                            if random.random() < 0.2:
                                text = re.sub(r"\bкак будто\b", "как будт", text, flags=re.IGNORECASE)
                    text = text.strip()
                text = re.sub(r"(?i)\b(спасибо вам большое|очень выручили)\b", "", text)
                text = re.sub(r"\s{2,}", " ", text).strip()
                text = text.replace("—", "-").replace("–", "-")
                if text.casefold() == "нет":
                    text = "Центр"
                if ReviewsService._is_invalid_ai_text(text):
                    elapsed = perf_counter() - start_time
                    logger.warning(
                        "[AI Review] Invalid AI payload received in %.2fs (short_mode=%s)",
                        elapsed,
                        short_mode,
                    )
                    return None
                elapsed = perf_counter() - start_time
                logger.info(
                    "[AI Review] Generated text in %.2fs (short_mode=%s)",
                    elapsed,
                    short_mode,
                )
                return text
        except Exception:
            elapsed = perf_counter() - start_time
            logger.exception("OpenAI request failed after %.2fs", elapsed)
            return None

    @staticmethod
    def _generate_ai_text_sync(product_name, city_name, district_name, item_price):
        if not has_openai or not OPENAI_API_KEY:
            return None

        try:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None

            if running_loop and running_loop.is_running():
                result_holder: dict[str, str | None] = {}
                error_holder: dict[str, Exception] = {}

                def run_in_thread() -> None:
                    new_loop = asyncio.new_event_loop()
                    try:
                        asyncio.set_event_loop(new_loop)
                        result_holder["value"] = new_loop.run_until_complete(
                            ReviewsService._generate_ai_text(
                                product_name=product_name,
                                city_name=city_name,
                                district_name=district_name,
                                item_price=item_price
                            )
                        )
                    except Exception as thread_exc:  # noqa: BLE001
                        error_holder["error"] = thread_exc
                    finally:
                        asyncio.set_event_loop(None)
                        new_loop.close()

                worker = threading.Thread(target=run_in_thread, name="reviews-ai-call")
                worker.start()
                worker.join()

                if error_holder:
                    raise error_holder["error"]

                return result_holder.get("value")
            else:
                return asyncio.run(
                    ReviewsService._generate_ai_text(
                        product_name=product_name,
                        city_name=city_name,
                        district_name=district_name,
                        item_price=item_price
                    )
                )
        except Exception:
            logger.exception("AI sync wrapper failed")
            return None

    @staticmethod
    async def generate_new_review_task(trigger: str = "auto"):
        """
        Attempts to generate a new AI-based review.
        Strictly follows data from products_cache and districts_cache.
        """
        reviews = ReviewsService._load_reviews()

        trigger_label = trigger or "auto"
        logger.info("[ReviewGen] Generation triggered (%s)", trigger_label)

        reviews, removed_invalid = ReviewsService._filter_invalid_reviews(reviews, f"pre-{trigger_label}")
        if removed_invalid:
            ReviewsService._save_reviews(reviews)
        
        # 1. Load Caches
        products_cache = CatalogService._load_cache() # {"city": {"ids": [...]}}
        districts_cache = GeoService._load_cache()    # {"city": ["d1", "d2"]}
        
        # 2. Find Available Cities (Intersection) with fuzzy matching
        available_cities = []
        city_mapping = {} # p_key -> d_key

        for p_key in products_cache:
            for d_key in districts_cache:
                if ReviewsService._is_city_key_match(p_key, d_key):
                    available_cities.append(p_key)
                    city_mapping[p_key] = d_key
                    break

        # Fallback if caches are empty (e.g. fresh start)
        if not available_cities:
            logger.warning("[ReviewGen] No cache intersection (trigger=%s)", trigger_label)
            return 0

        # 3. Generate one review per available city
        now_base = datetime.now()
        shuffled_cities = list(available_cities)
        random.shuffle(shuffled_cities)
        count_gen = 0
        time_cursor = now_base
        new_batch_iso: list[str] = []

        existing_norms = ReviewsService._collect_recent_texts(reviews)

        for target_p_key in shuffled_cities:
            target_d_key = city_mapping[target_p_key]

            districts_list = districts_cache.get(target_d_key, [])
            if not districts_list:
                logger.warning("[ReviewGen] No districts for %s", target_p_key)
                continue

            target_district_raw = random.choice(districts_list)
            target_district = ReviewsService._format_location_display(target_district_raw)

            prod_entry = products_cache.get(target_p_key, {})
            prod_ids = prod_entry.get("ids", []) if isinstance(prod_entry, dict) else prod_entry
            all_prods = CatalogService.get_all_products()
            real_products = [p for p in all_prods if p["id"] in prod_ids]
            if not real_products:
                logger.warning("[ReviewGen] No products for %s", target_p_key)
                continue

            target_product = random.choice(real_products)
            city_display = target_p_key.title()
            product_name = target_product.get('name', 'Unknown product')

            ai_text: str | None = None
            for attempt in range(3):
                try:
                    candidate = await ReviewsService._generate_ai_text(
                        product_name=product_name,
                        city_name=city_display,
                        district_name=target_district,
                        item_price=target_product['base_price']
                    )
                except Exception:
                    logger.exception(
                        "[ReviewGen] AI call failed (trigger=%s, city=%s, product=%s, attempt=%s)",
                        trigger_label,
                        target_p_key,
                        product_name,
                        attempt + 1,
                    )
                    candidate = None

                if ReviewsService._is_invalid_ai_text(candidate):
                    candidate = None

                if candidate and ReviewsService._is_duplicate_text(candidate, existing_norms):
                    logger.info(
                        "[ReviewGen] Duplicate AI payload skipped (city=%s, product=%s)",
                        city_display,
                        product_name,
                    )
                    candidate = None

                if candidate:
                    ai_text = candidate
                    normalized_candidate = ReviewsService._normalize_review_text(candidate)
                    if normalized_candidate:
                        existing_norms.add(normalized_candidate)
                    break

            if not ai_text:
                ai_text = ReviewsService._generate_local_review_text(
                    product_name,
                    city_display,
                    target_district,
                    target_product.get('base_price'),
                    forbidden_norms=existing_norms
                )

            if count_gen == 0:
                initial_gap_minutes = random.randint(0, 3)
                initial_gap_seconds = random.randint(0, 55)
                time_cursor = (now_base - timedelta(minutes=initial_gap_minutes, seconds=initial_gap_seconds)).replace(microsecond=0)
            else:
                gap_minutes = random.randint(5, 18)
                gap_seconds = random.randint(5, 55)
                time_cursor = (time_cursor - timedelta(minutes=gap_minutes, seconds=gap_seconds)).replace(microsecond=0)

            review_time = time_cursor
            user_display = ReviewsService._generate_username()
            item_info = ReviewsService._build_item_info(product_name, city_display, target_district)

            new_review = {
                "user": user_display,
                "base_name": user_display,
                "text": ai_text,
                "product": product_name,
                "city": city_display,
                "item_info": item_info,
                "stars": 5,
                "iso_date": review_time.isoformat(),
                "display_date": review_time.strftime("%d.%m %H:%M")
            }

            reviews.append(new_review)
            normalized_payload = ReviewsService._normalize_review_text(ai_text)
            if normalized_payload:
                existing_norms.add(normalized_payload)
            count_gen += 1
            new_batch_iso.append(new_review["iso_date"])

        ReviewsService._save_last_batch(new_batch_iso)

        reviews.sort(key=lambda x: x['iso_date'])
        ReviewsService._save_reviews(reviews)

        logger.info(
            "[ReviewGen] Generation finished (%s), total=%s",
            trigger_label,
            count_gen,
        )
        return count_gen


    @staticmethod
    async def get_reviews_text(limit=10):
        # Если бот только включился, проверим, не устарели ли отзывы
        ReviewsService.check_and_update()
        reviews = ReviewsService._load_reviews() # Reload after update

        if not reviews:
            # Seed initial if completely empty (and data exists)
            reviews = ReviewsService._seed_initial_reviews()
        
        selection = reviews[-limit:]
        selection.reverse() 
        
        result = [f"💬 <b>Последние отзывы:</b>\n"]
        
        for r in selection:
            stars = "⭐️" * r.get('stars', 5)
            user = r.get('user', 'User')
            date = r.get('display_date', '')
            text = r.get('text', '')
            item_info = r.get('item_info', '') 
            
            review_item = (
                f"{stars}\n"
                f"👤 <b>{user}</b> ({date})\n"
                f"🛍 <i>{item_info}</i>\n" 
                f"🗣 <i>{text}</i>\n"
            )
            result.append(review_item)
            
        return "\n".join(result)

    @staticmethod
    def get_filtered_reviews(
        city_filter: str = None,
        product_filter: str = None,
        *,
        include_hidden: bool = False,
        newest_first: bool = True,
    ) -> list[dict]:
        """Return reviews filtered by city/product using the same rules as pagination."""
        ReviewsService.check_and_update()
        reviews = ReviewsService._load_reviews()
        review_items = list(reversed(reviews)) if newest_first else list(reviews)

        filtered = []

        cf_norm = ReviewsService._normalize_city_key(city_filter) if city_filter else ""
        pf_norm = ReviewsService._normalize_product_key(product_filter) if product_filter else ""
        if city_filter and not cf_norm:
            return []
        if product_filter and not pf_norm:
            return []

        for r in review_items:
            if not include_hidden and r.get("hidden"):
                continue

            review_city = str(r.get("city") or "")
            review_product = str(r.get("product") or "")
            item_product, item_city, _ = ReviewsService._split_item_info(r.get("item_info"))

            if cf_norm:
                city_candidates = [review_city, item_city]
                if not any(ReviewsService._normalize_city_key(value) == cf_norm for value in city_candidates if value):
                    continue

            if pf_norm:
                product_candidates = [review_product, item_product]
                if not any(ReviewsService._normalize_product_key(value) == pf_norm for value in product_candidates if value):
                    continue

            filtered.append(r)

        return filtered

    @staticmethod
    def get_paginated_review(index: int, city_filter: str = None, product_filter: str = None):
        """Returns (review_obj, total_count) for specific index (0-based, 0 = newest)."""
        filtered = ReviewsService.get_filtered_reviews(city_filter, product_filter)
            
        total = len(filtered)
        
        if total == 0:
            return None, 0
            
        # Wrap index
        if index < 0: index = 0
        if index >= total: index = total - 1
        
        return filtered[index], total

    @staticmethod
    def get_admin_review(index: int, include_hidden: bool = True):
        """Returns (review_obj, total_count) for admin moderation, newest first."""
        reviews = ReviewsService._load_reviews()
        reviews_rev = list(reversed(reviews))
        if not include_hidden:
            reviews_rev = [r for r in reviews_rev if not r.get("hidden")]

        total = len(reviews_rev)
        if total == 0:
            return None, 0

        if index < 0:
            index = 0
        if index >= total:
            index = total - 1

        return reviews_rev[index], total

    @staticmethod
    def get_last_batch_admin_review(index: int, include_hidden: bool = True):
        """Returns (review_obj, total_count) for last generated batch, newest first."""
        iso_dates = ReviewsService._load_last_batch()
        if not iso_dates:
            return None, 0

        reviews = ReviewsService._load_reviews()
        reviews_rev = list(reversed(reviews))
        target_set = set(iso_dates)
        filtered = [r for r in reviews_rev if r.get("iso_date") in target_set]
        if not include_hidden:
            filtered = [r for r in filtered if not r.get("hidden")]

        total = len(filtered)
        if total == 0:
            return None, 0

        if index < 0:
            index = 0
        if index >= total:
            index = total - 1

        return filtered[index], total

    @staticmethod
    def export_last_batch_texts() -> dict:
        """Export editable payload for last generated batch.

        Payload schema:
        {
          "meta": {...},
                    "reviews": [{"id": "...", "text": "...", "district": "..."}, ...]
        }
        """
        iso_dates = set(ReviewsService._load_last_batch())
        reviews = ReviewsService._load_reviews()
        if not iso_dates or not reviews:
            return {
                "meta": {
                    "source": "last_generated_batch",
                    "count": 0,
                },
                "reviews": [],
            }

        rows = []
        for item in reviews:
            if item.get("iso_date") in iso_dates:
                _, _, district = ReviewsService._split_item_info(item.get("item_info", ""))
                rows.append({
                    "id": item.get("id", ""),
                    "text": item.get("text", ""),
                    "district": district,
                    "city": item.get("city", ""),
                    "item_info": item.get("item_info", ""),
                    "hidden": bool(item.get("hidden", False)),
                })

        return {
            "meta": {
                "source": "last_generated_batch",
                "count": len(rows),
            },
            "reviews": rows,
        }

    @staticmethod
    def apply_last_batch_texts(payload) -> dict:
        """Apply text and district updates for last generated batch only.

        Accepts either list[{id,...}] or dict with key "reviews".
        Returns counters: updated, skipped, invalid.
        """
        if isinstance(payload, dict):
            raw_rows = payload.get("reviews", [])
        else:
            raw_rows = payload

        if not isinstance(raw_rows, list):
            return {"updated": 0, "skipped": 0, "invalid": 1}

        updates: dict[str, dict[str, str]] = {}
        invalid = 0
        for row in raw_rows:
            if not isinstance(row, dict):
                invalid += 1
                continue
            review_id = str(row.get("id", "")).strip()
            if not review_id:
                invalid += 1
                continue

            patch: dict[str, str] = {}

            if "text" in row:
                text = row.get("text")
                if not isinstance(text, str) or not text.strip():
                    invalid += 1
                    continue
                patch["text"] = text.strip()

            if "district" in row:
                district = row.get("district")
                if not isinstance(district, str) or not district.strip():
                    invalid += 1
                    continue
                patch["district"] = district.strip()

            if "item_info" in row:
                item_info = row.get("item_info")
                if not isinstance(item_info, str) or not item_info.strip():
                    invalid += 1
                    continue
                patch["item_info"] = item_info.strip()

            if not patch:
                invalid += 1
                continue
            updates[review_id] = patch

        if not updates:
            return {"updated": 0, "skipped": 0, "invalid": max(1, invalid)}

        batch_iso = set(ReviewsService._load_last_batch())
        reviews = ReviewsService._load_reviews()
        if not batch_iso or not reviews:
            return {"updated": 0, "skipped": len(updates), "invalid": invalid}

        updated = 0
        now = datetime.now().isoformat()
        for item in reviews:
            if item.get("iso_date") not in batch_iso:
                continue
            review_id = item.get("id")
            if review_id in updates:
                patch = updates[review_id]
                changed = False

                new_text = patch.get("text")
                if new_text is not None and item.get("text") != new_text:
                    item["text"] = new_text
                    changed = True

                new_item_info = None
                if "district" in patch:
                    current_product, current_city, _ = ReviewsService._split_item_info(item.get("item_info", ""))
                    product_name = current_product
                    city_name = str(item.get("city") or current_city).strip() or current_city

                    if "item_info" in patch:
                        patch_product, patch_city, _ = ReviewsService._split_item_info(patch["item_info"])
                        if patch_product:
                            product_name = patch_product
                        if patch_city:
                            city_name = patch_city

                    rebuilt = ReviewsService._build_item_info(product_name, city_name, patch["district"])
                    if rebuilt:
                        new_item_info = rebuilt
                elif "item_info" in patch:
                    new_item_info = patch["item_info"]

                if new_item_info is not None and item.get("item_info") != new_item_info:
                    item["item_info"] = new_item_info
                    changed = True

                if changed:
                    item["edited_at"] = now
                    updated += 1

        if updated:
            ReviewsService._save_reviews(reviews)

        skipped = max(0, len(updates) - updated)
        return {"updated": updated, "skipped": skipped, "invalid": invalid}

    @staticmethod
    def set_review_hidden(review_id: str, hidden: bool = True) -> bool:
        reviews = ReviewsService._load_reviews()
        changed = False
        for item in reviews:
            if item.get("id") == review_id:
                item["hidden"] = bool(hidden)
                changed = True
                break
        if changed:
            ReviewsService._save_reviews(reviews)
        return changed

    @staticmethod
    def update_review_text(review_id: str, new_text: str) -> bool:
        reviews = ReviewsService._load_reviews()
        changed = False
        now = datetime.now().isoformat()
        for item in reviews:
            if item.get("id") == review_id:
                item["text"] = new_text
                item["edited_at"] = now
                changed = True
                break
        if changed:
            ReviewsService._save_reviews(reviews)
        return changed

    @staticmethod
    def delete_review(review_id: str) -> bool:
        reviews = ReviewsService._load_reviews()
        before = len(reviews)
        reviews = [r for r in reviews if r.get("id") != review_id]
        if len(reviews) == before:
            return False
        ReviewsService._save_reviews(reviews)
        return True

    @staticmethod
    def find_similar_reviews(review_id: str, threshold: float = 0.82, limit: int = 5) -> list[dict]:
        reviews = ReviewsService._load_reviews()
        target = None
        for item in reviews:
            if item.get("id") == review_id:
                target = item
                break
        if not target:
            return []

        target_norm = ReviewsService._normalize_review_text(target.get("text"))
        if not target_norm:
            return []

        matches: list[dict] = []
        for item in reviews:
            if item.get("id") == review_id:
                continue
            other_norm = ReviewsService._normalize_review_text(item.get("text"))
            if not other_norm:
                continue
            ratio = SequenceMatcher(None, target_norm, other_norm).ratio()
            if ratio >= threshold:
                matches.append({
                    "id": item.get("id"),
                    "ratio": ratio,
                    "text": item.get("text", ""),
                    "user": item.get("user", ReviewsService.HIDDEN_USERNAME),
                    "city": item.get("city", ""),
                    "item_info": item.get("item_info", ""),
                    "display_date": item.get("display_date", ""),
                    "hidden": item.get("hidden", False)
                })

        matches.sort(key=lambda x: x["ratio"], reverse=True)
        if limit and len(matches) > limit:
            matches = matches[:limit]
        return matches

