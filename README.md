# Marketplace Bot (AI Powered)

Это прототип бота-маркетплейса с гео-позиционированием и динамическим прайсом.

## Установка

1. Создайте виртуальное окружение:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/Mac
   venv\Scripts\activate     # Windows
   ```

2. Установите зависимости:
   ```bash
   pip install -r requirements.txt
   ```

3. Настройте файл `.env`:
   * Откройте `.env`
   * Вставьте ваш `BOT_TOKEN` от @BotFather.
   * Вставьте ваш `ADMIN_ID` (можно узнать у бота @userinfobot), чтобы получать уведомления.
   * Вставьте `DATABASE_URL` для Postgres, если хотите хранить runtime-данные не в JSON.

## Запуск

```bash
python main.py
```

## Railway

Проект готов к запуску на Railway как worker/service с long polling.

1. Создайте новый проект на Railway и подключите репозиторий.
2. Добавьте PostgreSQL service, если хотите хранить runtime-данные в Postgres.
3. В Variables задайте минимум:
   - `BOT_TOKEN`
   - `ADMIN_ID`
   - `DATABASE_URL` (обычно Railway подставляет автоматически после подключения Postgres)
4. При необходимости добавьте optional-переменные из `.env.example`.
5. Railway возьмёт команду запуска из `railway.json`: `python main.py`

Примечания:

* Бот работает через polling, поэтому отдельный HTTP-port не нужен.
* Если `DATABASE_URL` не задан, проект откатится на локальные JSON-файлы.
* Конфиг также поддерживает Railway-style PostgreSQL переменные `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`.

## Функционал (в коде)

* **GeoService** (`app/services/geo.py`): Имитирует ИИ-поиск. Сейчас настроен так:
    * Если ввести "село...", он скажет, что население маленькое.
    * Если ввести любой нормальный город, он сгенерирует список районов.
    * В будущем подключить сюда API Google Places или OpenAI API.
    
* **CatalogService** (`app/services/catalog.py`):
    * Меняет цены в зависимости от дня недели (в выходные дороже).
    * Использует захардкоженный список товаров для примера.

* **Платежи**:
    * Крипта: выдает тестовый кошелек.
    * Карта: просит чек и пересылает сообщение Админу.
