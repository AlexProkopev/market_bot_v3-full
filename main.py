import asyncio
import logging
import os
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from app.config import BOT_TOKEN
from app.handlers import user_flow, admin
from app.services.postgres_store import PostgresDocumentStore
from app.services.users import UserService
from app.services.reviews import ReviewsService
from datetime import datetime, timedelta
import random

# Настройка логирования
logging.basicConfig(level=logging.INFO)

async def order_expiration_worker(bot: Bot):
    try:
        while True:
            try:
                users_ids = UserService.get_all_users()
                now = datetime.now()
                for uid in users_ids:
                    order = UserService.get_active_order(uid)
                    if order:
                        try:
                            order_time = datetime.strptime(order['timestamp'], "%Y-%m-%d %H:%M:%S")
                            if now - order_time > timedelta(minutes=20):
                                UserService.remove_active_order(uid)
                                try:
                                    await bot.send_message(uid, "⏳ <b>Время истекло</b>\nВаш заказ был автоматически отменен (истекли 20 минут).")
                                except Exception:
                                    pass
                        except ValueError:
                             pass
            except Exception as e:
                logging.error(f"Error in expiration worker: {e}")

            await asyncio.sleep(60)
    except asyncio.CancelledError:
        logging.info("Order expiration worker stopped")
        raise

async def main():
    if not BOT_TOKEN:
        print("Ошибка: Не задан токен бота в .env файле.")
        return

    PostgresDocumentStore.initialize()

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    worker_task = None

    try:
        worker_task = asyncio.create_task(order_expiration_worker(bot))

        # Регистрация роутеров
        # Сначала админка (чтобы команды типа /admin работали везде)
        dp.include_router(admin.router)
        dp.include_router(user_flow.router)

        deployment_commit = (
            os.getenv("RAILWAY_GIT_COMMIT_SHA")
            or os.getenv("RENDER_GIT_COMMIT")
            or "unknown"
        )
        deployment_service = (
            os.getenv("RAILWAY_SERVICE_NAME")
            or os.getenv("RENDER_SERVICE_NAME")
            or "local"
        )
        storage_backend = "postgres" if PostgresDocumentStore.is_enabled() else "json-fallback"
        print(
            f"✅ Бот запущен... service={deployment_service} "
            f"commit={deployment_commit} storage={storage_backend}"
        )
        print("[GeoDebug] build marker: geo-no-gpt-landmarks-v2")
        await bot.delete_webhook(drop_pending_updates=True)
        await dp.start_polling(bot)
    finally:
        if worker_task is not None:
            worker_task.cancel()
            try:
                await worker_task
            except asyncio.CancelledError:
                pass
        await bot.session.close()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Бот остановлен")
