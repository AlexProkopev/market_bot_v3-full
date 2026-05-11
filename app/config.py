import os
from dotenv import load_dotenv

load_dotenv()


def _build_database_url() -> str | None:
    direct_url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if direct_url:
        return direct_url

    host = os.getenv("PGHOST") or os.getenv("POSTGRES_HOST")
    port = os.getenv("PGPORT") or os.getenv("POSTGRES_PORT")
    database = (
        os.getenv("PGDATABASE")
        or os.getenv("POSTGRES_DB")
        or os.getenv("POSTGRES_DATABASE")
    )
    user = os.getenv("PGUSER") or os.getenv("POSTGRES_USER")
    password = os.getenv("PGPASSWORD") or os.getenv("POSTGRES_PASSWORD")

    if all([host, port, database, user, password]):
        return f"postgresql://{user}:{password}@{host}:{port}/{database}"

    return None

BOT_TOKEN = os.getenv("BOT_TOKEN")
ADMIN_ID = os.getenv("ADMIN_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY") # Ключ для ChatGPT
DATABASE_URL = _build_database_url()
YANDEX_API_KEY = os.getenv("YANDEX_API_KEY") # Ключ для Яндекс Карт (Search API)
DGIS_API_KEY = os.getenv("DGIS_API_KEY") # Ключ для 2GIS
LTC_WALLET_ADDRESS = os.getenv("LTC_WALLET_ADDRESS", "LYYvBiJa3nXZpnzeezvCPmTtkSaGtq3NxX")

if ADMIN_ID:
    ADMIN_ID = str(ADMIN_ID).strip() # Убираем пробелы, если есть

if not BOT_TOKEN:
    print("Ошибка: Не указан BOT_TOKEN в переменных окружения")
