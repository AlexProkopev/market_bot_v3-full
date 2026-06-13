import asyncio
import logging
import math
import re
import secrets
import json
from io import BytesIO
from datetime import timedelta

from aiogram import Router, F, Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, BufferedInputFile
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from app.config import ADMIN_ID
from app.services.blacklist import BlacklistService
from app.services.users import UserService
from app.services.catalog import CatalogService
from app.services.reviews import ReviewsService
from app.services.geo import GeoService
from app.services.pricing import PricingService
from app.services.analytics import ProductInterestService
from app.services.pdf_export import PdfExportService
from app.utils import get_now_msk
import app.keyboards as kb

router = Router()

logger = logging.getLogger("app.handlers.admin")


async def _safe_callback_answer(callback: CallbackQuery, *args, **kwargs) -> bool:
    try:
        await callback.answer(*args, **kwargs)
        return True
    except TelegramBadRequest as err:
        error_text = str(err).lower()
        if "query is too old" in error_text or "query id is invalid" in error_text:
            return False
        raise


@router.callback_query(F.data.startswith("admin_delmsg_"))
async def admin_delete_forwarded(callback: CallbackQuery, bot: Bot):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Нет доступа", show_alert=True)
        return

    payload = callback.data[len("admin_delmsg_"):]
    parts = payload.split("_")
    if len(parts) != 3:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    user_id_str, original_id_str, forwarded_id_str = parts

    try:
        user_id = int(user_id_str)
        original_id = int(original_id_str)
        forwarded_id = int(forwarded_id_str)
    except ValueError:
        await callback.answer("Ошибка идентификаторов", show_alert=True)
        return

    user_deleted = False
    admin_deleted = False

    try:
        await bot.delete_message(user_id, original_id)
        user_deleted = True
    except TelegramBadRequest as err:
        logger.warning("Не удалось удалить сообщение у пользователя %s: %s", user_id, err)
    except Exception as err:  # noqa: BLE001
        logger.exception("Ошибка при удалении сообщения у пользователя %s", user_id)

    if forwarded_id:
        try:
            await bot.delete_message(ADMIN_ID, forwarded_id)
            admin_deleted = True
        except TelegramBadRequest as err:
            logger.warning("Не удалось удалить форвард у админа %s: %s", forwarded_id, err)
        except Exception as err:  # noqa: BLE001
            logger.exception("Ошибка при удалении форварда у админа %s", forwarded_id)

    status_lines: list[str] = ["🗑 Результат удаления"]
    status_lines.append("✅ Удалили у клиента" if user_deleted else "⚠️ Не удалось удалить у клиента")
    if forwarded_id:
        status_lines.append("✅ Удалили форвард" if admin_deleted else "⚠️ Форвард остался у админа")

    if callback.message:
        try:
            await callback.message.edit_text("\n".join(status_lines))
        except TelegramBadRequest:
            pass

    await callback.answer("Готово")

class AdminState(StatesGroup):
    chatting_with_user = State()
    waiting_for_ban_id = State()
    waiting_for_unban_id = State()
    
    waiting_for_broadcast = State()
    waiting_for_dm_id = State()
    waiting_for_dm_text = State()
    waiting_for_catalog_city = State() # Ожидание города для редактирования
    
    waiting_for_districts_city = State() # Ожидание города для районов
    waiting_for_district_name = State() # Ожидание имени района
    
    waiting_for_new_prod_name = State() # Товар Custom
    waiting_for_new_prod_price = State()
    
    # Review States
    # Since we have ReviewState for users, let's use AdminState specific ones to avoid conflicts or shared handlers
    waiting_for_rev_nick = State()
    waiting_for_rev_mode = State()
    waiting_for_rev_city = State()
    waiting_for_rev_prod = State() # ID
    waiting_for_rev_dist = State() 
    waiting_for_rev_text = State()
    waiting_for_search_city = State() # NEW for user search
    
    # Product District Edit
    # Usually we don't need a state if we just use callback toggles, but good to have context
    editing_product_districts = State() 
    editing_product_stash = State()
    
    waiting_for_review_phrase = State() # Добавление фразы
    waiting_for_review_del_idx = State() # Удаление фразы по номеру

    waiting_for_rev_mod_edit_text = State()
    waiting_for_rev_gen_import_json = State()
    
    # Настройки авто-отзывов
    waiting_for_rev_set_interval = State()
    waiting_for_rev_set_min = State()
    waiting_for_rev_set_max = State()

    waiting_for_stash_type_add = State()

    waiting_for_pricing_region_name = State()
    waiting_for_pricing_value = State()

    waiting_for_ltc_rate = State()

    waiting_for_manual_payment_template = State()

    waiting_for_manual_commission = State()

    waiting_for_rf_commission = State()

    waiting_for_support_contact = State()
    waiting_for_contacts_text = State()

    waiting_for_exchange_bot_url = State()

    waiting_for_rf_cards = State()

    waiting_for_rf_unblock_id = State()

def resolve_user_input(text: str):
    text = text.strip()
    # Если это число, возвращаем как int
    if text.isdigit():
        return int(text)
    # Иначе пробуем искать по username (убираем @ если есть)
    if text.startswith('@'):
        text = text[1:]
    return UserService.get_user_id_by_username(text)

# --- Управление Балансом ---

@router.message(Command("set_balance"))
async def cmd_set_balance(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer("⚠️ Используйте: /set_balance <id/username> <сумма>")
        return

    target = args[1]
    try:
        amount = float(args[2])
    except ValueError:
        await message.answer("⚠️ Сумма должна быть числом.")
        return

    user_id = resolve_user_input(target)
    if not user_id:
        await message.answer(f"⚠️ Пользователь '{target}' не найден.")
        return

    if UserService.set_balance(user_id, amount):
        await message.answer(f"✅ Баланс пользователя {user_id} установлен: {amount} руб.")
    else:
        await message.answer("❌ Ошибка при сохранении.")

@router.message(Command("add_balance"))
async def cmd_add_balance(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
    
    args = message.text.split()
    if len(args) < 3:
        await message.answer("⚠️ Используйте: /add_balance <id/username> <сумма>")
        return

    target = args[1]
    try:
        amount = float(args[2])
    except ValueError:
        await message.answer("⚠️ Сумма должна быть числом.")
        return

    user_id = resolve_user_input(target)
    if not user_id:
        await message.answer(f"⚠️ Пользователь '{target}' не найден.")
        return
    
    UserService.add_balance(user_id, amount)
    new_bal = UserService.get_balance(user_id)
    await message.answer(f"✅ Баланс пользователя {user_id} пополнен на {amount}. Текущий: {new_bal} руб.")

# --- Главная Панель ---

@router.message(Command('admin'))
async def cmd_admin_panel(message: Message, state: FSMContext):
    await state.clear()
    
    if str(message.from_user.id) != str(ADMIN_ID):
        print(f'Unauthorized admin attempt: {message.from_user.id} vs {ADMIN_ID}')
        return

    await message.answer(
        '🛠 <b>Панель Администратора</b>\n'
        'Выберите действие:',
        reply_markup=kb.get_admin_keyboard()
    )

@router.callback_query(F.data == 'admin_close')
async def admin_close(callback: CallbackQuery):
    await callback.message.delete()

@router.callback_query(F.data == 'admin_back_main')
async def admin_back_main(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text(
        '🛠 <b>Панель Администратора</b>\nВыберите действие:',
        reply_markup=kb.get_admin_keyboard()
    )


@router.message(Command('export_catalog_pdf'))
async def cmd_export_catalog_pdf(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    try:
        pdf_bytes = await asyncio.to_thread(PdfExportService.build_catalog_pdf)
    except Exception as err:  # noqa: BLE001
        logger.exception("Ошибка генерации PDF выгрузки каталога")
        await message.answer(f"⚠️ Не удалось собрать PDF: {err}")
        return

    file = BufferedInputFile(pdf_bytes, filename="catalog_export.pdf")
    await message.answer_document(
        file,
        caption="📄 PDF-выгрузка каталога готова. Внутри города, товары, веса, районы, типы кладов и наценки."
    )


@router.callback_query(F.data == 'admin_export_catalog_pdf')
async def admin_export_catalog_pdf(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Нет доступа", show_alert=True)
        return

    try:
        pdf_bytes = await asyncio.to_thread(PdfExportService.build_catalog_pdf)
    except Exception as err:  # noqa: BLE001
        logger.exception("Ошибка генерации PDF выгрузки каталога")
        await callback.answer("Не удалось собрать PDF", show_alert=True)
        await callback.message.answer(f"⚠️ Не удалось собрать PDF: {err}")
        return

    file = BufferedInputFile(pdf_bytes, filename="catalog_export.pdf")
    await callback.message.answer_document(
        file,
        caption="📄 PDF-выгрузка каталога готова. Внутри города, товары, веса, районы, типы кладов и наценки."
    )
    await callback.answer("PDF отправлен")

from app.services.settings import SettingsService


def _build_global_stash_text(types: list[str]) -> str:
    lines = ["🏷 <b>Глобальные типы кладов</b>", "Эти варианты доступны при настройке товаров:"]
    for idx, item in enumerate(types, start=1):
        lines.append(f"{idx}. {item}")
    lines.append("\nМинимум два типа должны оставаться в списке.")
    return "\n".join(lines)


async def _show_global_stash_menu(target, state: FSMContext | None = None):
    types = CatalogService.get_available_stash_types()
    text = _build_global_stash_text(types)
    markup = kb.get_admin_global_stash_menu(types)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


async def _show_pricing_menu(
    target: CallbackQuery | Message,
    state: FSMContext,
    *,
    bot: Bot | None = None,
    edit_chat_id: int | None = None,
    edit_message_id: int | None = None
):
    region_entries = PricingService.get_region_entries()
    await state.update_data(
        pricing_region_options=region_entries,
        pricing_prompt_chat_id=None,
        pricing_prompt_message_id=None
    )

    lines: list[str] = ["💰 <b>Управление наценками</b>"]
    if region_entries:
        lines.append("\n<b>Регионы:</b>")
        for entry in region_entries:
            lines.append(f"- {entry['label']}: x{entry['value']:.2f}")
    else:
        lines.append("\nРегионы: наценки не заданы.")

    lines.append("\nℹ️ Введите множитель, например 1.25. Значение 1 отключает наценку.")

    buttons: list[list[InlineKeyboardButton]] = []
    for idx, entry in enumerate(region_entries):
        label = f"{entry['label']} ({entry['value']:.2f}x)"
        buttons.append([InlineKeyboardButton(text=label, callback_data=f"adm_price_reg_{idx}")])

    buttons.append([InlineKeyboardButton(text="➕ Добавить регион", callback_data="adm_price_region_add")])
    buttons.append([InlineKeyboardButton(text="↩️ Назад", callback_data="admin_back_main")])

    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    text = "\n".join(lines)

    if bot and edit_chat_id and edit_message_id:
        try:
            await bot.edit_message_text(text, chat_id=edit_chat_id, message_id=edit_message_id, reply_markup=markup)
            await state.set_state(None)
            return
        except TelegramBadRequest:
            pass

    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    await state.set_state(None)


async def _show_ltc_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    rate, updated_at = SettingsService.get_ltc_rate()
    text, markup = kb.get_admin_ltc_menu(rate, updated_at)

    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    if state:
        await state.set_state(None)


def _build_manual_payment_template_text(template: str | None) -> str:
    base = [
        "✉️ <b>Шаблон ручной оплаты</b>",
        "Этот текст отправляется клиенту после подключения оператора в ручной оплате.",
        "",
        "Доступные плейсхолдеры:",
        "• {user_id}",
        "• {amount}",
        "• {expires_at}",
        "• {city}",
        "• {district}",
        "• {product}",
        "",
    ]
    if template:
        base.append("Текущий шаблон:")
        base.append(f"<pre>{template}</pre>")
    else:
        base.append("Текущий шаблон: <i>не задан</i>")
    return "\n".join(base)


def _normalize_rf_card(value: str) -> str | None:
    digits = re.sub(r"\D", "", value or "")
    if len(digits) < 12 or len(digits) > 19:
        return None
    groups = [digits[i:i + 4] for i in range(0, len(digits), 4)]
    return " ".join(groups)


def _build_rf_cards_text(cards: list[str]) -> str:
    lines = ["💳 <b>Карты РФ для оплаты</b>", "Выдаются случайно при оплате на карту РФ.", ""]
    if cards:
        lines.append("Текущие карты:")
        for idx, card in enumerate(cards, start=1):
            lines.append(f"{idx}. {card}")
    else:
        lines.append("Текущие карты: <i>не заданы</i>")
    lines.append("\nФормат ввода: по одной карте в строке (или через запятую).")
    return "\n".join(lines)


async def _show_rf_cards_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    cards = SettingsService.get_rf_cards()
    text = _build_rf_cards_text(cards)
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_rf_cards_set")],
        [InlineKeyboardButton(text="🗑 Очистить", callback_data="admin_rf_cards_clear")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    if state:
        await state.set_state(None)


async def _show_manual_payment_template_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    template = SettingsService.get_manual_payment_template()
    text = _build_manual_payment_template_text(template)
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_manual_pay_template_set")],
        [InlineKeyboardButton(text="🗑 Очистить", callback_data="admin_manual_pay_template_clear")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)

    if state:
        await state.set_state(None)

@router.callback_query(F.data == 'admin_toggle_redirect')
async def admin_toggle_redirect(callback: CallbackQuery):
    current = SettingsService.is_redirect_active()
    new_state = not current
    SettingsService.set_redirect_active(new_state)
    
    status = "ON" if new_state else "OFF"
    await callback.answer(f"Редирект на Авто-обмен: {status}")
    
    # Обновляем клавиатуру
    await callback.message.edit_reply_markup(reply_markup=kb.get_admin_keyboard())

# --- Управление наценками ---

@router.callback_query(F.data == 'admin_pricing_menu')
async def admin_pricing_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_pricing_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_ltc_menu')
async def admin_ltc_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_ltc_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_manual_pay_template')
async def admin_manual_pay_template(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_manual_payment_template_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_cards_menu')
async def admin_rf_cards_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_rf_cards_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_unblock_menu')
async def admin_rf_unblock_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_rf_unblock_id)
    await callback.message.edit_text(
        "Введите ID или @username для разблокировки оплаты картой РФ:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_back_main")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_manual_pay_template_set')
async def admin_manual_pay_template_set(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_manual_payment_template)
    await callback.message.edit_text(
        "Введите текст шаблона одним сообщением. Можно использовать плейсхолдеры {user_id}, {amount}, {expires_at}, {city}, {district}, {product}.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_manual_pay_template")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_cards_set')
async def admin_rf_cards_set(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_rf_cards)
    await callback.message.edit_text(
        "Введите список карт (по одной в строке или через запятую).",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_rf_cards_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_manual_pay_template_clear')
async def admin_manual_pay_template_clear(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.set_manual_payment_template(None)
    await _show_manual_payment_template_menu(callback, state)
    await callback.answer("Шаблон очищен")


@router.callback_query(F.data == 'admin_rf_cards_clear')
async def admin_rf_cards_clear(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.set_rf_cards([])
    await _show_rf_cards_menu(callback, state)
    await callback.answer("Карты очищены")


@router.message(AdminState.waiting_for_manual_payment_template)
async def admin_manual_pay_template_save(message: Message, state: FSMContext):
    template = (message.text or "").strip()
    if not template:
        await message.answer("⚠️ Шаблон не может быть пустым.")
        return

    SettingsService.set_manual_payment_template(template)
    await message.answer("✅ Шаблон сохранен.")
    await state.set_state(None)
    await _show_manual_payment_template_menu(message, state)


@router.message(AdminState.waiting_for_rf_cards)
async def admin_rf_cards_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("⚠️ Список карт не может быть пустым.")
        return

    chunks = []
    for line in raw.splitlines():
        parts = re.split(r"[,;]", line)
        for part in parts:
            item = part.strip()
            if item:
                chunks.append(item)

    cards: list[str] = []
    for item in chunks:
        normalized = _normalize_rf_card(item)
        if normalized and normalized not in cards:
            cards.append(normalized)

    if not cards:
        await message.answer("⚠️ Не удалось распознать номера карт. Пример: 2200 7020 2889 9388")
        return

    SettingsService.set_rf_cards(cards)
    await message.answer("✅ Карты сохранены.")
    await state.set_state(None)
    await _show_rf_cards_menu(message, state)


@router.message(AdminState.waiting_for_rf_unblock_id)
async def admin_rf_unblock_by_id(message: Message, state: FSMContext):
    user_id = resolve_user_input(message.text)

    if not user_id:
        await message.answer("❌ Пользователь не найден. Введите корректный ID или @username.")
        return

    if UserService.unblock_rf_card_payment(user_id):
        await message.answer(f"✅ Оплата картой РФ разблокирована для {user_id}.")
    else:
        await message.answer("⚠️ У пользователя нет активной блокировки по карте РФ.")

    await state.clear()
    await message.answer("🛠 Панель администратора:", reply_markup=kb.get_admin_keyboard())


@router.callback_query(F.data == 'admin_ltc_set')
async def admin_ltc_set(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_ltc_rate)
    await callback.message.edit_text(
        "Введите курс LTC в рублях за 1 LTC (например 7500.50):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_ltc_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_ltc_clear')
async def admin_ltc_clear(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.set_ltc_rate(None)
    await _show_ltc_menu(callback, state)
    await callback.answer("Курс сброшен")


@router.message(AdminState.waiting_for_ltc_rate)
async def admin_ltc_set_value(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        await message.answer("⚠️ Введите число, например 7500.50.")
        return

    if value <= 0:
        await message.answer("⚠️ Курс должен быть больше нуля.")
        return

    SettingsService.set_ltc_rate(value)
    await message.answer("✅ Курс LTC сохранен.")
    await state.set_state(None)
    await _show_ltc_menu(message, state)


# --- Управление комиссией ручной оплаты ---

async def _show_commission_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    from app.services.settings import SettingsService
    commission = SettingsService.get_manual_commission()
    text = (
        "💸 <b>Комиссия при ручном пополнении</b>\n\n"
        f"Текущая комиссия: <b>{commission} руб.</b>\n\n"
        "Эта сумма добавляется к цене товара при оплате через оператора (Карта / СБП).\n"
        "Установите 0, чтобы убрать комиссию."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_commission_set")],
        [InlineKeyboardButton(text="0️⃣ Обнулить", callback_data="admin_commission_zero")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


async def _show_rf_commission_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    commission = SettingsService.get_rf_card_commission()
    text = (
        "💸 <b>Комиссия для оплаты картой РФ</b>\n\n"
        f"Текущая комиссия: <b>{commission} руб.</b>\n\n"
        "Эта сумма добавляется к цене товара при оплате на карту РФ.\n"
        "Установите 0, чтобы убрать комиссию."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_rf_commission_set")],
        [InlineKeyboardButton(text="0️⃣ Обнулить", callback_data="admin_rf_commission_zero")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


@router.callback_query(F.data == 'admin_commission_menu')
async def admin_commission_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_commission_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_commission_menu')
async def admin_rf_commission_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_rf_commission_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_commission_set')
async def admin_commission_set(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_manual_commission)
    await callback.message.edit_text(
        "Введите размер комиссии в рублях (целое число, например 600 или 0):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_commission_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_rf_commission_set')
async def admin_rf_commission_set(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_rf_commission)
    await callback.message.edit_text(
        "Введите размер комиссии для карты РФ в рублях (целое число, например 300 или 0):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_rf_commission_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_commission_zero')
async def admin_commission_zero(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    from app.services.settings import SettingsService
    SettingsService.set_manual_commission(0)
    await _show_commission_menu(callback, state)
    await callback.answer("Комиссия обнулена")


@router.callback_query(F.data == 'admin_rf_commission_zero')
async def admin_rf_commission_zero(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    SettingsService.set_rf_card_commission(0)
    await _show_rf_commission_menu(callback, state)
    await callback.answer("Комиссия обнулена")


@router.message(AdminState.waiting_for_manual_commission)
async def admin_commission_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("⚠️ Введите целое число, например 600 или 0.")
        return
    if value < 0:
        await message.answer("⚠️ Комиссия не может быть отрицательной.")
        return
    from app.services.settings import SettingsService
    SettingsService.set_manual_commission(value)
    await message.answer(f"✅ Комиссия сохранена: {value} руб.")
    await state.set_state(None)
    await _show_commission_menu(message, state)


@router.message(AdminState.waiting_for_rf_commission)
async def admin_rf_commission_save(message: Message, state: FSMContext):
    raw = (message.text or "").strip()
    try:
        value = int(raw)
    except ValueError:
        await message.answer("⚠️ Введите целое число, например 300 или 0.")
        return
    if value < 0:
        await message.answer("⚠️ Комиссия не может быть отрицательной.")
        return
    SettingsService.set_rf_card_commission(value)
    await message.answer(f"✅ Комиссия карты РФ сохранена: {value} руб.")
    await state.set_state(None)
    await _show_rf_commission_menu(message, state)


# --- Управление контактом поддержки ---

async def _show_contacts_text_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    text_value = SettingsService.get_contacts_text()
    text = (
        "📇 <b>Кнопка Контакты</b>\n\n"
        "Текущий текст, который увидит пользователь при нажатии на кнопку <b>Контакты</b>:\n\n"
        f"{text_value}\n\n"
        "Можно использовать HTML-разметку и ссылки."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_contacts_text_set")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)

async def _show_support_contact_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    from app.services.settings import SettingsService
    contact = SettingsService.get_support_contact()
    text = (
        "📞 <b>Контакт поддержки</b>\n\n"
        f"Текущий контакт: <b>{contact}</b>\n\n"
        "Это значение подставляется во все сообщения бота, где упоминается поддержка.\n"
        "Например: правила магазина, инструкция, стартовое сообщение."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_support_contact_set")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


async def _show_exchange_bot_menu(target: CallbackQuery | Message, state: FSMContext | None = None):
    from app.services.settings import SettingsService
    exchange_bot_url = SettingsService.get_exchange_bot_url() or "—"
    text = (
        "🔗 <b>Ссылка на бота авто-оплаты</b>\n\n"
        f"Текущая ссылка: <b>{exchange_bot_url}</b>\n\n"
        "Эта ссылка используется в оплате \"Карта / СБП (самостоятельно)\"."
    )
    buttons = [
        [InlineKeyboardButton(text="✏️ Изменить", callback_data="admin_exchange_bot_set")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    markup = InlineKeyboardMarkup(inline_keyboard=buttons)
    if isinstance(target, CallbackQuery):
        try:
            await target.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest:
            await target.message.answer(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)
    if state:
        await state.set_state(None)


@router.callback_query(F.data == 'admin_support_contact_menu')
async def admin_support_contact_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_support_contact_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_contacts_text_menu')
async def admin_contacts_text_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_contacts_text_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_exchange_bot_menu')
async def admin_exchange_bot_menu(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await _show_exchange_bot_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'admin_support_contact_set')
async def admin_support_contact_set(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_support_contact)
    await callback.message.edit_text(
        "Введите новый контакт поддержки (например @username или ссылку):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_support_contact_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_contacts_text_set')
async def admin_contacts_text_set(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_contacts_text)
    await callback.message.edit_text(
        "Введите текст для кнопки Контакты. Можно использовать HTML и ссылки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_contacts_text_menu")]])
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_exchange_bot_set')
async def admin_exchange_bot_set(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return
    await state.set_state(AdminState.waiting_for_exchange_bot_url)
    await callback.message.edit_text(
        "Введите ссылку на бота авто-оплаты (например https://t.me/your_bot):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_exchange_bot_menu")]])
    )
    await callback.answer()


@router.message(AdminState.waiting_for_support_contact)
async def admin_support_contact_save(message: Message, state: FSMContext):
    contact = (message.text or "").strip()
    if not contact:
        await message.answer("⚠️ Контакт не может быть пустым.")
        return
    from app.services.settings import SettingsService
    SettingsService.set_support_contact(contact)
    await message.answer(f"✅ Контакт поддержки сохранён: {contact}")
    await state.set_state(None)
    await _show_support_contact_menu(message, state)


@router.message(AdminState.waiting_for_contacts_text)
async def admin_contacts_text_save(message: Message, state: FSMContext):
    text_value = (message.html_text or message.text or "").strip()
    if not text_value:
        await message.answer("⚠️ Текст контактов не может быть пустым.")
        return
    SettingsService.set_contacts_text(text_value)
    await message.answer("✅ Текст кнопки Контакты сохранён.")
    await state.set_state(None)
    await _show_contacts_text_menu(message, state)


@router.message(AdminState.waiting_for_exchange_bot_url)
async def admin_exchange_bot_save(message: Message, state: FSMContext):
    url = (message.text or "").strip()
    if not url:
        await message.answer("⚠️ Ссылка не может быть пустой.")
        return
    from app.services.settings import SettingsService
    SettingsService.set_exchange_bot_url(url)
    await message.answer(f"✅ Ссылка сохранена: {url}")
    await state.set_state(None)
    await _show_exchange_bot_menu(message, state)


@router.callback_query(F.data == 'adm_price_region_add')
async def adm_price_region_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_pricing_region_name)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_pricing_menu")]])
    await callback.message.edit_text(
        "Введите название региона, для которого требуется настроить наценку:",
        reply_markup=markup
    )
    await state.update_data(
        pricing_target_type="region",
        pricing_region_key=None,
        pricing_region_label=None,
        pricing_prompt_chat_id=callback.message.chat.id,
        pricing_prompt_message_id=callback.message.message_id
    )
    await callback.answer()


@router.callback_query(F.data.startswith('adm_price_reg_'))
async def adm_price_region_pick(callback: CallbackQuery, state: FSMContext):
    idx_raw = callback.data[len('adm_price_reg_'):]
    try:
        idx = int(idx_raw)
    except ValueError:
        await callback.answer("Некорректный выбор.", show_alert=True)
        return

    data = await state.get_data()
    options = data.get("pricing_region_options")
    if not options:
        options = PricingService.get_region_entries()
        await state.update_data(pricing_region_options=options)

    if idx < 0 or idx >= len(options):
        await callback.answer("Элемент недоступен.", show_alert=True)
        return

    entry = options[idx]
    await state.update_data(
        pricing_target_type="region",
        pricing_region_key=entry["key"],
        pricing_region_label=entry["label"]
    )
    await state.set_state(AdminState.waiting_for_pricing_value)

    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_pricing_menu")]])
    await callback.message.edit_text(
        f"{entry['label']}\nТекущая наценка: x{entry['value']:.2f}\n\nВведите новый множитель (например 1.25). Значение 1 отключит наценку.",
        reply_markup=markup
    )
    await state.update_data(
        pricing_prompt_chat_id=callback.message.chat.id,
        pricing_prompt_message_id=callback.message.message_id
    )
    await callback.answer()


@router.message(AdminState.waiting_for_pricing_region_name)
async def adm_price_region_name(message: Message, state: FSMContext):
    name = (message.text or "").strip()
    if not name:
        await message.answer("⚠️ Название не может быть пустым. Попробуйте снова.")
        return

    await state.update_data(
        pricing_target_type="region",
        pricing_region_label=name,
        pricing_region_key=None
    )
    await state.set_state(AdminState.waiting_for_pricing_value)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_pricing_menu")]])
    data = await state.get_data()
    chat_id = data.get("pricing_prompt_chat_id")
    msg_id = data.get("pricing_prompt_message_id")

    if chat_id and msg_id:
        try:
            await message.bot.edit_message_text(
                f"Регион: {name}\nВведите множитель (например 1.25). Значение 1 отключит наценку.",
                chat_id=chat_id,
                message_id=msg_id,
                reply_markup=markup
            )
        except TelegramBadRequest:
            sent = await message.answer(
                f"Регион: {name}\nВведите множитель (например 1.25). Значение 1 отключит наценку.",
                reply_markup=markup
            )
            await state.update_data(
                pricing_prompt_chat_id=sent.chat.id,
                pricing_prompt_message_id=sent.message_id
            )
    else:
        sent = await message.answer(
            f"Регион: {name}\nВведите множитель (например 1.25). Значение 1 отключит наценку.",
            reply_markup=markup
        )
        await state.update_data(
            pricing_prompt_chat_id=sent.chat.id,
            pricing_prompt_message_id=sent.message_id
        )


@router.message(AdminState.waiting_for_pricing_value)
async def adm_price_value(message: Message, state: FSMContext):
    raw = (message.text or "").strip().replace(",", ".")
    try:
        value = float(raw)
    except ValueError:
        await message.answer("⚠️ Введите число, например 1.25.")
        return

    if value <= 0:
        await message.answer("⚠️ Множитель должен быть больше нуля.")
        return

    data = await state.get_data()
    target_type = data.get("pricing_target_type")
    response_text = "Настройка обновлена."
    prompt_chat = data.get("pricing_prompt_chat_id")
    prompt_msg = data.get("pricing_prompt_message_id")

    if target_type == "region":
        label = data.get("pricing_region_label") or data.get("pricing_region_key")
        key = data.get("pricing_region_key") or label
        if not label:
            await message.answer("⚠️ Не удалось определить регион. Повторите настройку.")
            await state.set_state(None)
            return

        if value <= 1.0:
            PricingService.clear_region_multiplier(key or label)
            response_text = f"Наценка для региона '{label}' отключена."
        else:
            PricingService.set_region_multiplier(label, value)
            response_text = f"Наценка для региона '{label}' установлена: x{value:.2f}."

    else:
        await message.answer("⚠️ Неизвестный тип цели. Настройка не сохранена.")
        await state.set_state(None)
        return

    await message.answer(f"✅ {response_text}")
    await state.update_data(
        pricing_target_type=None,
        pricing_region_key=None,
        pricing_region_label=None,
        pricing_prompt_chat_id=None,
        pricing_prompt_message_id=None
    )
    await state.set_state(None)
    await _show_pricing_menu(
        message,
        state,
        bot=message.bot,
        edit_chat_id=prompt_chat,
        edit_message_id=prompt_msg
    )

# --- Управление Каталогами ---

async def _show_catalog_generation_settings(target: CallbackQuery | Message):
    all_products = CatalogService.get_all_products()
    selected_ids = CatalogService.get_allowed_generation_product_ids()
    mode = "Все товары (фильтр выключен)" if not selected_ids else f"Выбрано товаров: {len(selected_ids)}"

    text = (
        "🧬 <b>Настройки генерации прайса</b>\n\n"
        "Этот фильтр влияет на товары, которые попадают в НОВЫЙ сгенерированный прайс при выборе города.\n"
        "Уже созданные прайсы в кэше не меняются.\n\n"
        f"Режим: <b>{mode}</b>"
    )

    markup = kb.get_admin_catalog_generation_menu(all_products, selected_ids)
    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == 'admin_catalog_generation_settings')
async def admin_catalog_generation_settings(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _show_catalog_generation_settings(callback)


@router.callback_query(F.data == 'adm_cat_gen_select_all')
async def adm_cat_gen_select_all(callback: CallbackQuery):
    all_ids = [p.get("id") for p in CatalogService.get_all_products() if isinstance(p, dict) and p.get("id")]
    CatalogService.set_allowed_generation_product_ids(all_ids)
    await _show_catalog_generation_settings(callback)


@router.callback_query(F.data == 'adm_cat_gen_reset')
async def adm_cat_gen_reset(callback: CallbackQuery):
    CatalogService.reset_generation_product_filter()
    await _show_catalog_generation_settings(callback)


@router.callback_query(F.data.startswith('adm_cat_gen_tog_'))
async def adm_cat_gen_toggle(callback: CallbackQuery):
    product_id = callback.data[len('adm_cat_gen_tog_'):]
    CatalogService.toggle_allowed_generation_product_id(product_id)
    await _show_catalog_generation_settings(callback)

@router.callback_query(F.data == 'admin_catalog_search')
async def admin_catalog_search(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_catalog_city)
    await callback.message.edit_text(
        '🏙 <b>Редактор каталогов</b>\n'
        'Введите название города (например, "Москва" или "Томск"), чтобы посмотреть или изменить список товаров для него:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_back_main')]])
    )

@router.message(AdminState.waiting_for_catalog_city)
async def admin_process_catalog_city(message: Message, state: FSMContext):
    city_name = message.text.strip()
    
    # Пытаемся получить текущие товары. Если города нет в кэше, он сгенерируется.
    # Но админу лучше знать, есть он или нет.
    # CatalogService.get_products_for_city(city_name) САМ создаст если нет.
    # Это удобно: админ пишет город, если его не было - он создается.
    
    # Обновляем состояние, храним город
    await state.update_data(current_edit_city=city_name)
    await state.clear() # Сбрасываем ожидание ввода, но надо запомнить город как-то
    # Ой, clear() очистит data. Не будем клирить, или сохраним снова.
    # Лучше не использовать clear() все время, а просто сменить стейт на None
    
    await _show_city_catalog_menu(message, city_name)

async def _show_city_catalog_menu(message_or_callback, city_name):
    products = CatalogService.get_products_for_city(city_name)
    update_time = CatalogService.get_last_update_time(city_name) or "Только что"
    
    products_text = "\n".join([f"{i+1}. {p['name']} ({p['price']}р)" for i, p in enumerate(products)])
    
    text = (
        f"🏙 Город: <b>{city_name}</b>\n"
        f"🕒 Обновлено: {update_time}\n\n"
        f"📦 <b>Текущие товары ({len(products)} шт):</b>\n"
        f"{products_text}"
    )
    
    mk = kb.get_admin_catalog_menu(city_name)
    
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=mk)
    else:
        await message_or_callback.message.edit_text(text, reply_markup=mk)


async def _show_city_delete_menu(callback: CallbackQuery, city_name: str):
    products = CatalogService.get_products_for_city(city_name)
    await callback.message.edit_text(
        f"➖ Удаление товара из <b>{city_name}</b>:\nВыберите товар для удаления:",
        reply_markup=kb.get_admin_del_product_list(city_name, products)
    )


async def _show_city_add_menu(callback: CallbackQuery, city_name: str):
    current_products = CatalogService.get_products_for_city(city_name)
    current_ids = [p['id'] for p in current_products]
    all_products = CatalogService.ALL_PRODUCTS

    await callback.message.edit_text(
        f"➕ Добавление товара в <b>{city_name}</b>:",
        reply_markup=kb.get_admin_add_product_list(city_name, current_ids, all_products)
    )

@router.callback_query(F.data.startswith('adm_cat_back_'))
async def admin_cat_back(callback: CallbackQuery):
    city_name = callback.data.split('_', 3)[3]
    await _show_city_catalog_menu(callback, city_name)

@router.callback_query(F.data.startswith('adm_cat_reset_'))
async def admin_cat_reset(callback: CallbackQuery):
    city_name = callback.data.split('_', 3)[3]
    CatalogService.force_refresh_products(city_name)
    await callback.answer("🔄 Каталог пересоздан (случайно).")
    try:
        await _show_city_catalog_menu(callback, city_name)
    except Exception:
        # Игнорируем ошибку, если текст сообщения визуально не изменился
        # (такое бывает, если при регенерации выпал ТОТ ЖЕ список товаров, что редко, но возможно, или если меняется только разметка)
        pass

@router.callback_query(F.data.startswith('adm_cat_del_menu_'))
async def admin_cat_del_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_cat_del_menu_'):]
    await _show_city_delete_menu(callback, city_name)

@router.callback_query(F.data.startswith('adm_cat_del_'))
# Формат adm_cat_del_CITYname_ProdID. 
# Внимание: city_name может содержать подчеркивания? Надеемся нет или парсим аккуратно.
# Лучше парсить split с конца или использовать separator.
# Но у нас ID товара простой (p1, p2...).
async def admin_cat_del_do(callback: CallbackQuery):
    payload = callback.data[len('adm_cat_del_'):]
    city_name, p_id = payload.rsplit('_', 1)
    
    if CatalogService.remove_product_from_city(city_name, p_id):
        await callback.answer("✅ Удалено")
    else:
        await callback.answer("⚠️ Ошибка")
        
    await _show_city_delete_menu(callback, city_name)

@router.callback_query(F.data.startswith('adm_cat_add_menu_'))
async def admin_cat_add_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_cat_add_menu_'):]
    await _show_city_add_menu(callback, city_name)

@router.callback_query(F.data.startswith('adm_cat_add_'))
async def admin_cat_add_do(callback: CallbackQuery):
    payload = callback.data[len('adm_cat_add_'):]
    city_name, p_id = payload.rsplit('_', 1)
    
    if CatalogService.add_product_to_city(city_name, p_id):
        await callback.answer("✅ Добавлено")
    else:
        await callback.answer("⚠️ Ужe есть")
        
    await _show_city_add_menu(callback, city_name)

# --- Создание Custom товара ---

@router.callback_query(F.data.startswith('adm_cat_new_'))
async def admin_cat_new_product_start(callback: CallbackQuery, state: FSMContext):
    city_name = callback.data.split('_', 3)[3]
    await state.update_data(target_city_for_product=city_name)
    
    await state.set_state(AdminState.waiting_for_new_prod_name)
    await callback.message.edit_text(
        "✨ <b>Создание нового товара</b>\n\n"
        "Введите НАЗВАНИЕ товара (например: 'Супер Микс 0.5г'):"
    )

@router.message(AdminState.waiting_for_new_prod_name)
async def admin_cat_new_product_name(message: Message, state: FSMContext):
    name = message.text.strip()
    await state.update_data(new_prod_name=name)
    
    await state.set_state(AdminState.waiting_for_new_prod_price)
    await message.answer("💰 Введите БАЗОВУЮ ЦЕНУ (число, в рублях):")

@router.message(AdminState.waiting_for_new_prod_price)
async def admin_cat_new_product_price(message: Message, state: FSMContext):
    try:
        price = int(message.text.strip())
    except ValueError:
        await message.answer("⚠️ Нужно ввести целое число.")
        return
        
    data = await state.get_data()
    name = data['new_prod_name']
    city_name = data['target_city_for_product']
    
    # 1. Создаем глобально
    new_prod = CatalogService.create_new_product(name, price)
    
    # 2. Добавляем в город
    CatalogService.add_product_to_city(city_name, new_prod['id'])
    
    await message.answer(f"✅ Товар '{name}' создан и добавлен в город {city_name}!")
    
    # Возвращаемся в меню добавления (или меню города)
    await state.clear()
    
    # Трюк: чтобы показать меню, нам нужен callback object или сообщение с клавиатурой.
    # Но мы в message handler. Отправим новое сообщение.
    mk = kb.get_admin_catalog_menu(city_name)
    # Показываем главное меню каталога для этого города
    products = CatalogService.get_products_for_city(city_name)
    products_text = "\n".join([f"{i+1}. {p['name']} ({p['price']}р)" for i, p in enumerate(products)])
    text = (
        f"🏙 Город: <b>{city_name}</b>\n"
        f"🕒 Обновлено: только что\n\n"
        f"📦 <b>Текущие товары ({len(products)} шт):</b>\n"
        f"{products_text}"
    )
    await message.answer(text, reply_markup=mk)


# --- Управление Районами ---

@router.callback_query(F.data == 'admin_districts_menu')
async def admin_districts_menu(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_districts_city)
    await callback.message.edit_text(
        '🏘 <b>Редактор районов</b>\n'
        'Введите название города (например, "Москва"), чтобы редактировать районы:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_back_main')]])
    )

@router.message(AdminState.waiting_for_districts_city)
async def admin_process_districts_city(message: Message, state: FSMContext):
    city_name = message.text.strip()
    
    # Loading districts (searching partial match if possible)
    districts, cache_key = GeoService.get_districts_for_city(city_name)
    
    # If using cache_key, use clean city name from it?
    # Key format "City (Region)" or just "City".
    # Let's verify with user if it's correct? No, simpler to just start using it.
    
    if not cache_key:
        # City not found in cache. Create blank?
        await message.answer(
            f"⚠️ Город '{city_name}' не найден в кэше районов.\n"
            f"Будет создан новый список для '{city_name}'.",
        )
        districts = []
    
    real_city_name = city_name
    if cache_key:
        # cache_key format "Name (Region)" or "Name"
        # Extract name part? Only if needed.
        # But we need to pass a string identifier to callbacks.
        # If we pass "Moscow (Moscow)", it might be too long or contain symbols.
        # GeoService.get_districts_for_city handles partial match on input.
        # Can we stick to the user input `city_name`?
        # If user typed "moscow", and found "Moscow (Moscow)", we should prefer using "Moscow (Moscow)" for consistency?
        # Or just pass the input for `GeoService` to resolve again?
        # Better: pass the input `city_name` and let GeoService resolve it in add/del functions.
        # BUT: In callbacks we need consistent ID.
        # Let's assume we use the user's input as the "Session ID" for this editing, 
        # but GeoService will resolve it. That works if input is consistent. 
        # Problem: if user typed "moscow" now, and next time "Moscow", it might differ?
        # No, key resolution handles case.
        pass

    await state.update_data(dist_edit_city=city_name)
    # Don't clear state, we might need data if we use state based navigation.
    # But actually, we only need 'dist_edit_city' when adding items, which uses a new state.
    # So for now, we can stay in current state or clear only state (set_state(None)) but keep data.
    await state.set_state(None) # Reset state but keep data
    
    await _show_district_editor(message, city_name)

async def _show_district_editor(message_or_callback, city_name):
    # Reload from service to get latest
    districts, _ = GeoService.get_districts_for_city(city_name)
    if districts is None: districts = []
    
    d_list = "\n".join([f"- {d}" for d in districts])
    if not d_list: d_list = "(пусто)"
    
    text = (
        f"🏘 Районы города: <b>{city_name}</b>\n\n"
        f"{d_list}"
    )
    
    mk = kb.get_admin_districts_menu(city_name, districts)
    
    if isinstance(message_or_callback, Message):
        await message_or_callback.answer(text, reply_markup=mk)
    else:
        await message_or_callback.message.edit_text(text, reply_markup=mk)

@router.callback_query(F.data.startswith('adm_dist_add_'))
async def admin_dist_add(callback: CallbackQuery, state: FSMContext):
    # admin_dist_add_CITYNAME
    # City name is rest of string. But it might be at end.
    # Actually we used f"adm_dist_add_{city_name}"
    prefix = "adm_dist_add_"
    city_name = callback.data[len(prefix):]
    
    await state.update_data(dist_edit_city=city_name)
    await state.set_state(AdminState.waiting_for_district_name)
    
    await callback.message.edit_text(
        f"➕ Добавление района в <b>{city_name}</b>\n"
        f"Введите название района:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data=f"adm_dist_back_{city_name}")]])
    )

@router.message(AdminState.waiting_for_district_name)
async def admin_save_district(message: Message, state: FSMContext):
    data = await state.get_data()
    city_name = data.get('dist_edit_city')
    new_dist = message.text.strip()
    
    if GeoService.add_district(city_name, new_dist):
        await message.answer(f"✅ Район '{new_dist}' добавлен.")
    else:
        await message.answer(f"⚠️ Не удалось добавить (возможно дубль).")
        
    await state.clear()
    await _show_district_editor(message, city_name)

@router.callback_query(F.data.startswith('adm_dist_del_menu_'))
async def admin_dist_del_menu_show(callback: CallbackQuery, state: FSMContext):
    city_name = callback.data[len("adm_dist_del_menu_"):]
    districts, _ = GeoService.get_districts_for_city(city_name)
    if not districts:
        districts = []

    entries: list[tuple[str, str]] = []
    mapping: dict[str, str] = {}
    for dist in districts:
        token = secrets.token_hex(3)
        while token in mapping:
            token = secrets.token_hex(3)
        mapping[token] = dist
        entries.append((token, dist))

    await state.update_data(
        dist_del_city=city_name,
        dist_del_map=mapping,
        dist_del_order=[token for token, _ in entries]
    )

    buttons: list[list[InlineKeyboardButton]] = []
    for token, dist in entries:
        buttons.append([
            InlineKeyboardButton(text=f"❌ {dist}", callback_data=f"adm_dist_del_do_{token}")
        ])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_dist_back_{city_name}")])

    await callback.message.edit_text(
        f"➖ Удаление района из <b>{city_name}</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith('adm_dist_del_do_'))
async def admin_dist_del_do(callback: CallbackQuery, state: FSMContext):
    token = callback.data[len("adm_dist_del_do_"):]
    data = await state.get_data()
    city_name = data.get("dist_del_city")
    mapping: dict[str, str] = data.get("dist_del_map") or {}
    order: list[str] = data.get("dist_del_order") or []

    if not city_name or token not in mapping:
        await callback.answer("Данные устарели, откройте меню снова.", show_alert=True)
        return

    dist_name = mapping[token]
    if GeoService.remove_district(city_name, dist_name):
        await callback.answer(f"🗑 {dist_name} удален")
        mapping.pop(token, None)
        order = [t for t in order if t != token]
    else:
        await callback.answer("Ошибка удаления", show_alert=True)
        return

    await state.update_data(dist_del_map=mapping, dist_del_order=order)

    entries = [(t, mapping[t]) for t in order if t in mapping]
    buttons: list[list[InlineKeyboardButton]] = []
    for entry_token, dist in entries:
        buttons.append([
            InlineKeyboardButton(text=f"❌ {dist}", callback_data=f"adm_dist_del_do_{entry_token}")
        ])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_dist_back_{city_name}")])

    await callback.message.edit_text(
        f"➖ Удаление района из <b>{city_name}</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith('adm_dist_back_'))
async def admin_dist_back(callback: CallbackQuery):
    city_name = callback.data[len("adm_dist_back_"):]
    await _show_district_editor(callback, city_name)

# ----------------------------------------

from app.services.reviews import ReviewsService # Импорт

# --- Управление фразами для отзывов ---

@router.callback_query(F.data == 'admin_reviews_menu')
async def admin_reviews_main_menu_show(callback: CallbackQuery):
    await callback.message.edit_text(
        "⭐️ <b>Управление отзывами</b>\nЧто хотите сделать?",
        reply_markup=kb.get_admin_reviews_menu()
    )

@router.callback_query(F.data == 'admin_rev_phrases_menu')
async def admin_phrases_menu(callback: CallbackQuery):
    phrases = ReviewsService.get_custom_prompts()
    
    text = "🗣 <b>Настройка стиля отзывов</b>\n\nТекущие пользовательские фразы (AI будет стараться использовать их):\n"
    if not phrases:
        text += "<i>(Список пуст)</i>"
    else:
        for i, p in enumerate(phrases):
            text += f"{i+1}. {p}\n"
    
    # We need a sub-keyboard for phrases management here
    # Since get_admin_reviews_menu is now the MAIN menu, we need custom buttons here
    # Let's inline them here for simplicity or add to kb
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить фразу", callback_data="admin_rev_add")],
        [InlineKeyboardButton(text="🗑 Удалить фразу", callback_data="admin_rev_del")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")]
    ]
    
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))

# --- REVIEW MODERATION ---

def _trim_review_text(text: str, limit: int = 160) -> str:
    if not text:
        return ""
    compact = " ".join(text.split())
    if len(compact) <= limit:
        return compact
    return compact[: max(0, limit - 1)].rstrip() + "…"


def _build_admin_review_mod_markup(
    index: int,
    total: int,
    review_id: str,
    hidden: bool,
    nav_prefix: str = "adm_rev_mod_nav_",
) -> InlineKeyboardMarkup:
    buttons: list[list[InlineKeyboardButton]] = []

    nav_row: list[InlineKeyboardButton] = []
    if index > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"{nav_prefix}{index - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⬛️", callback_data="ignore"))
    nav_row.append(InlineKeyboardButton(text=f"{index + 1}/{total}", callback_data="ignore"))
    if index < total - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"{nav_prefix}{index + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⬛️", callback_data="ignore"))
    buttons.append(nav_row)

    hide_btn = InlineKeyboardButton(
        text="👁️ Показать" if hidden else "🙈 Скрыть",
        callback_data=f"adm_rev_mod_unhide_{review_id}" if hidden else f"adm_rev_mod_hide_{review_id}"
    )
    buttons.append([
        hide_btn,
        InlineKeyboardButton(text="✏️ Изменить текст", callback_data=f"adm_rev_mod_edit_{review_id}")
    ])

    buttons.append([
        InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm_rev_mod_del_{review_id}"),
        InlineKeyboardButton(text="🔎 Похожие", callback_data=f"adm_rev_mod_dups_{review_id}")
    ])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")])

    return InlineKeyboardMarkup(inline_keyboard=buttons)


async def _resume_review_moderation(target: CallbackQuery | Message, state: FSMContext):
    data = await state.get_data()
    source = data.get("adm_rev_mod_source", "all")
    index = data.get("adm_rev_mod_index", 0)
    if source == "generated":
        await _show_admin_review_moderation_generated(target, index, state)
    else:
        await _show_admin_review_moderation(target, index, state)


async def _show_admin_review_moderation(target: CallbackQuery | Message, index: int, state: FSMContext):
    review, total = ReviewsService.get_admin_review(index)

    if not review:
        text = "🛡 <b>Модерация отзывов</b>\n\nПока нет отзывов."
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")]])
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)
        return

    await state.update_data(
        adm_rev_mod_index=index,
        adm_rev_mod_total=total,
        adm_rev_mod_id=review.get("id"),
        adm_rev_mod_source="all",
    )

    status = "🙈 Скрыт" if review.get("hidden") else "👁️ Показан"
    stars = "⭐️" * review.get("stars", 5)
    user = review.get("user") or ReviewsService.HIDDEN_USERNAME
    date = review.get("display_date", "")
    item_info = review.get("item_info", "")
    text_body = review.get("text", "")

    text = (
        "🛡 <b>Модерация отзывов</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"ID: <b>{review.get('id', '—')}</b>\n\n"
        f"👤 <b>{user}</b> ({date})\n"
        f"{stars}\n"
        f"🛍 <i>{item_info}</i>\n\n"
        f"🗣 <i>{text_body}</i>\n"
    )

    markup = _build_admin_review_mod_markup(index, total, review.get("id", ""), review.get("hidden", False))

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


async def _show_admin_review_moderation_generated(target: CallbackQuery | Message, index: int, state: FSMContext):
    review, total = ReviewsService.get_last_batch_admin_review(index)

    if not review:
        text = "🧪 <b>Модерация сгенерированных</b>\n\nПока нет сгенерированных отзывов в последней волне."
        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")]])
        if isinstance(target, CallbackQuery):
            await target.message.edit_text(text, reply_markup=markup)
        else:
            await target.answer(text, reply_markup=markup)
        return

    await state.update_data(
        adm_rev_mod_index=index,
        adm_rev_mod_total=total,
        adm_rev_mod_id=review.get("id"),
        adm_rev_mod_source="generated",
    )

    status = "🙈 Скрыт" if review.get("hidden") else "👁️ Показан"
    stars = "⭐️" * review.get("stars", 5)
    user = review.get("user") or ReviewsService.HIDDEN_USERNAME
    date = review.get("display_date", "")
    item_info = review.get("item_info", "")
    text_body = review.get("text", "")

    text = (
        "🧪 <b>Модерация сгенерированных</b>\n"
        f"Статус: <b>{status}</b>\n"
        f"ID: <b>{review.get('id', '—')}</b>\n\n"
        f"👤 <b>{user}</b> ({date})\n"
        f"{stars}\n"
        f"🛍 <i>{item_info}</i>\n\n"
        f"🗣 <i>{text_body}</i>\n"
    )

    markup = _build_admin_review_mod_markup(
        index,
        total,
        review.get("id", ""),
        review.get("hidden", False),
        nav_prefix="adm_rev_gen_mod_nav_",
    )

    if isinstance(target, CallbackQuery):
        await target.message.edit_text(text, reply_markup=markup)
    else:
        await target.answer(text, reply_markup=markup)


@router.callback_query(F.data == "adm_rev_mod_start")
async def adm_rev_mod_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_admin_review_moderation(callback, 0, state)
    await callback.answer()


@router.callback_query(F.data == "adm_rev_gen_mod_start")
async def adm_rev_gen_mod_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await _show_admin_review_moderation_generated(callback, 0, state)
    await callback.answer()


@router.callback_query(F.data == "adm_rev_gen_export_json")
async def adm_rev_gen_export_json(callback: CallbackQuery):
    payload = ReviewsService.export_last_batch_texts()
    rows = payload.get("reviews", []) if isinstance(payload, dict) else []
    if not rows:
        await callback.answer("Последняя волна пуста", show_alert=True)
        return

    body = json.dumps(payload, ensure_ascii=False, indent=2)
    file = BufferedInputFile(body.encode("utf-8"), filename="generated_reviews_batch.json")
    await callback.message.answer_document(
        file,
        caption=(
            "📤 Выгрузка последней волны готова.\n"
            "Можно менять поля `text`, `district` и при необходимости `item_info`, затем загрузите файл обратно кнопкой '📥 Загрузить JSON'."
        )
    )
    await callback.answer("JSON отправлен")


@router.callback_query(F.data == "adm_rev_gen_import_json")
async def adm_rev_gen_import_json_start(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_rev_gen_import_json)
    await callback.message.edit_text(
        "📥 Отправьте JSON-файл с правками последней волны.\n\n"
        "Формат: список объектов с `id` и редактируемыми полями или объект с полем `reviews`.\n"
        "Поддерживаются поля `text`, `district` и `item_info`.",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_gen_import_cancel")]]
        )
    )
    await callback.answer()


@router.callback_query(F.data == "adm_rev_gen_import_cancel")
async def adm_rev_gen_import_cancel(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await callback.message.edit_text(
        "⭐️ <b>Управление отзывами</b>\nЧто хотите сделать?",
        reply_markup=kb.get_admin_reviews_menu()
    )
    await callback.answer("Отменено")


@router.message(AdminState.waiting_for_rev_gen_import_json, F.document)
async def adm_rev_gen_import_json_file(message: Message, state: FSMContext):
    doc = message.document
    if not doc:
        await message.answer("⚠️ Пришлите JSON-файл документом.")
        return

    try:
        telegram_file = await message.bot.get_file(doc.file_id)
        buffer = BytesIO()
        await message.bot.download_file(telegram_file.file_path, destination=buffer)
        raw = buffer.getvalue().decode("utf-8")
        payload = json.loads(raw)
    except Exception:
        await message.answer("⚠️ Не удалось прочитать JSON. Проверьте формат и кодировку UTF-8.")
        return

    result = ReviewsService.apply_last_batch_texts(payload)
    await state.set_state(None)
    await message.answer(
        "✅ Импорт завершен.\n"
        f"Обновлено: {result.get('updated', 0)}\n"
        f"Пропущено: {result.get('skipped', 0)}\n"
        f"Ошибок строк: {result.get('invalid', 0)}"
    )
    await message.answer("⭐️ Меню отзывов:", reply_markup=kb.get_admin_reviews_menu())


@router.message(AdminState.waiting_for_rev_gen_import_json)
async def adm_rev_gen_import_json_text_hint(message: Message):
    await message.answer("⚠️ Нужен именно JSON-файл документом, не текстовым сообщением.")


@router.callback_query(F.data.startswith("adm_rev_mod_nav_"))
async def adm_rev_mod_nav(callback: CallbackQuery, state: FSMContext):
    raw = callback.data[len("adm_rev_mod_nav_"):]
    try:
        index = int(raw)
    except ValueError:
        index = 0
    await _show_admin_review_moderation(callback, index, state)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rev_gen_mod_nav_"))
async def adm_rev_gen_mod_nav(callback: CallbackQuery, state: FSMContext):
    raw = callback.data[len("adm_rev_gen_mod_nav_"):]
    try:
        index = int(raw)
    except ValueError:
        index = 0
    await _show_admin_review_moderation_generated(callback, index, state)
    await callback.answer()


@router.callback_query(F.data.startswith("adm_rev_mod_hide_"))
async def adm_rev_mod_hide(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_hide_"):]
    ReviewsService.set_review_hidden(review_id, True)
    await _resume_review_moderation(callback, state)
    await callback.answer("Отзыв скрыт")


@router.callback_query(F.data.startswith("adm_rev_mod_unhide_"))
async def adm_rev_mod_unhide(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_unhide_"):]
    ReviewsService.set_review_hidden(review_id, False)
    await _resume_review_moderation(callback, state)
    await callback.answer("Отзыв показан")


@router.callback_query(F.data.startswith("adm_rev_mod_del_"))
async def adm_rev_mod_delete(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_del_"):]
    ReviewsService.delete_review(review_id)
    await _resume_review_moderation(callback, state)
    await callback.answer("Отзыв удален")


@router.callback_query(F.data.startswith("adm_rev_mod_edit_"))
async def adm_rev_mod_edit(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_edit_"):]
    await state.update_data(adm_rev_mod_edit_id=review_id)
    await state.set_state(AdminState.waiting_for_rev_mod_edit_text)
    await callback.message.edit_text(
        "✏️ Введите новый текст отзыва одним сообщением:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_mod_cancel_edit")]])
    )
    await callback.answer()


@router.callback_query(F.data == "adm_rev_mod_cancel_edit")
async def adm_rev_mod_cancel_edit(callback: CallbackQuery, state: FSMContext):
    await state.set_state(None)
    await _resume_review_moderation(callback, state)
    await callback.answer()


@router.message(AdminState.waiting_for_rev_mod_edit_text)
async def adm_rev_mod_edit_text(message: Message, state: FSMContext):
    new_text = (message.text or "").strip()
    if not new_text:
        await message.answer("⚠️ Текст не может быть пустым.")
        return

    data = await state.get_data()
    review_id = data.get("adm_rev_mod_edit_id")
    if not review_id:
        await message.answer("⚠️ Отзыв не найден.")
        await state.set_state(None)
        return

    ReviewsService.update_review_text(review_id, new_text)
    await message.answer("✅ Текст отзыва обновлен.")
    await state.set_state(None)
    await _resume_review_moderation(message, state)


@router.callback_query(F.data.startswith("adm_rev_mod_dups_"))
async def adm_rev_mod_dups(callback: CallbackQuery, state: FSMContext):
    review_id = callback.data[len("adm_rev_mod_dups_"):]
    matches = ReviewsService.find_similar_reviews(review_id)

    lines = ["🔎 <b>Похожие отзывы</b>"]
    if not matches:
        lines.append("\nПохожих отзывов не найдено.")
    else:
        for idx, item in enumerate(matches, start=1):
            ratio = item.get("ratio", 0.0)
            status = "🙈" if item.get("hidden") else "👁️"
            snippet = _trim_review_text(item.get("text", ""), 140)
            lines.append(
                f"\n{idx}. {status} <b>{ratio:.0%}</b> — {item.get('display_date', '')}"
                f"\n🛍 {item.get('item_info', '')}"
                f"\n🗣 {snippet}"
            )

    text = "\n".join(lines)
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Назад", callback_data="adm_rev_mod_back")]])

    await callback.message.edit_text(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data == "adm_rev_mod_back")
async def adm_rev_mod_back(callback: CallbackQuery, state: FSMContext):
    await _resume_review_moderation(callback, state)
    await callback.answer()

# --- MANUAL REVIEWS ---

MANUAL_CITY_PAGE_SIZE = 12


def _build_manual_city_markup(options: list[dict[str, str]], page: int, page_size: int) -> InlineKeyboardMarkup:
    total = len(options)
    if total <= 0:
        # Fallback keyboard with only cancel button
        return InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")]]
        )

    page_size = max(1, page_size)
    total_pages = max(1, math.ceil(total / page_size))
    page = max(0, min(page, total_pages - 1))
    start = page * page_size
    page_slice = options[start:start + page_size]

    rows: list[list[InlineKeyboardButton]] = []
    for offset, option in enumerate(page_slice):
        label = option.get("display") or option.get("city_key") or "—"
        idx = start + offset
        rows.append([InlineKeyboardButton(text=label, callback_data=f"adm_rev_city_{idx}")])

    if total_pages > 1:
        nav_row: list[InlineKeyboardButton] = []
        if page > 0:
            nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"adm_rev_citypage_{page - 1}"))
        nav_row.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="ignore"))
        if page < total_pages - 1:
            nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"adm_rev_citypage_{page + 1}"))
        rows.append(nav_row)

    rows.append([InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _build_manual_review_mode_markup() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🧾 Обычный отзыв", callback_data="adm_rev_mode_regular")],
        [InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")],
    ])


def _normalize_product_base_name(name: str) -> str:
    if not name:
        return ""
    cleaned = re.sub(r"\s*(\d+(?:[\.,]\d+)?)\s*г\.?", "", name, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()

@router.callback_query(F.data == 'adm_rev_manual_start')
async def adm_rev_manual_start(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await state.update_data(
        rev_nick=ReviewsService.HIDDEN_USERNAME,
        manual_city_options=None,
        manual_city_page=0,
        manual_city_page_size=MANUAL_CITY_PAGE_SIZE,
        rev_mode=None,
        rev_prod=None,
        rev_city=None,
        rev_dist=None,
    )
    await state.set_state(AdminState.waiting_for_rev_mode)
    await callback.message.edit_text(
        "✍️ <b>Добавление отзыва</b>\n\n1. Выберите тип отзыва:",
        reply_markup=_build_manual_review_mode_markup()
    )


@router.callback_query(F.data == 'adm_rev_mode_regular', AdminState.waiting_for_rev_mode)
async def adm_rev_mode_regular(callback: CallbackQuery, state: FSMContext):
    city_options = ReviewsService.get_manual_city_options()
    if not city_options:
        await callback.message.edit_text(
            "⚠️ Не нашёл городов с готовым каталогом и районами. Сначала выберите город через пользовательский поток или обновите кэши.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="↩️ Назад", callback_data="admin_reviews_menu")]])
        )
        await callback.answer()
        return

    await state.update_data(
        rev_mode="regular",
        manual_city_options=city_options,
        manual_city_page=0,
        manual_city_page_size=MANUAL_CITY_PAGE_SIZE,
    )
    await state.set_state(AdminState.waiting_for_rev_city)
    await callback.message.edit_text(
        "✍️ <b>Добавление обычного отзыва</b>\n\n2. Выберите <b>Город</b>:",
        reply_markup=_build_manual_city_markup(city_options, 0, MANUAL_CITY_PAGE_SIZE)
    )
    await callback.answer()


@router.callback_query(F.data.startswith('adm_rev_citypage_'), AdminState.waiting_for_rev_city)
async def adm_rev_city_page(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    options = data.get("manual_city_options") or []
    if not options:
        await callback.answer("Список городов недоступен.", show_alert=True)
        return

    raw_page = callback.data[len('adm_rev_citypage_'):]
    try:
        requested_page = int(raw_page)
    except ValueError:
        await callback.answer("Некорректная страница.", show_alert=True)
        return

    page_size = data.get("manual_city_page_size") or MANUAL_CITY_PAGE_SIZE
    markup = _build_manual_city_markup(options, requested_page, page_size)
    await state.update_data(manual_city_page=requested_page, manual_city_page_size=page_size)

    try:
        await callback.message.edit_reply_markup(reply_markup=markup)
    except TelegramBadRequest:
        # Сообщение могло быть изменено или удалено
        pass

    await callback.answer()


@router.callback_query(F.data.startswith('adm_rev_city_'), AdminState.waiting_for_rev_city)
async def adm_rev_city(callback: CallbackQuery, state: FSMContext):
    idx_raw = callback.data.split("_", 3)[3]
    try:
        idx = int(idx_raw)
    except ValueError:
        await callback.answer("Некорректный выбор города.", show_alert=True)
        return

    data = await state.get_data()
    options = data.get("manual_city_options") or []
    if idx < 0 or idx >= len(options):
        await callback.answer("Город недоступен.", show_alert=True)
        return

    option = options[idx]
    city_display = option.get("display") or option.get("city_key") or "—"
    city_key = option.get("city_key") or city_display.lower().strip()

    await state.update_data(
        rev_city=city_display,
        rev_city_key=city_key,
        manual_city_options=None,
        manual_city_page=None,
        manual_city_page_size=None,
        manual_rev_dist_map=None
    )
    
    # Show products for this city
    products = CatalogService.get_products_for_city(city_display)
    if not products:
        await callback.answer("В этом городе нет активных товаров. Выберите другой вариант.", show_alert=True)
        return
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=p['name'], callback_data=f"adm_rev_prod_{p['id']}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")])

    await state.set_state(AdminState.waiting_for_rev_prod)
    await callback.message.edit_text(
        f"3. Город: {city_display}\nВыберите <b>Товар</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )



@router.callback_query(F.data.startswith('adm_rev_prod_'), AdminState.waiting_for_rev_prod)
async def adm_rev_prod(callback: CallbackQuery, state: FSMContext):
    p_id = callback.data.split("_", 3)[3]
    data = await state.get_data()
    city = data['rev_city']
    
    # Store product name for display
    prod = CatalogService.get_product_by_id(p_id, city)
    p_name = prod['name'] if prod else "Unknown"
    
    await state.update_data(rev_prod=p_name)
    
    # Show districts
    districts, _ = GeoService.get_districts_for_city(city)
    if not districts:
        districts = ["Центр"]

    dist_map = {str(i): d for i, d in enumerate(districts)}
    await state.update_data(manual_rev_dist_map=dist_map)

    buttons = []
    for idx, d in dist_map.items():
        buttons.append([InlineKeyboardButton(text=d, callback_data=f"adm_rev_dist_{idx}")])
    buttons.append([InlineKeyboardButton(text="Отмена", callback_data="admin_reviews_menu")])

    await state.set_state(AdminState.waiting_for_rev_dist)
    await callback.message.edit_text(
        f"4. Товар: {p_name}\nВыберите <b>Район</b>:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith('adm_rev_dist_'), AdminState.waiting_for_rev_dist)
async def adm_rev_dist(callback: CallbackQuery, state: FSMContext):
    # District might have spaces
    prefix = "adm_rev_dist_"
    idx = callback.data[len(prefix):]
    data = await state.get_data()
    dist_map = data.get("manual_rev_dist_map") or {}
    dist = dist_map.get(idx)
    if not dist:
        await callback.answer("Район недоступен.", show_alert=True)
        return

    await state.update_data(rev_dist=dist)
    
    await state.set_state(AdminState.waiting_for_rev_text)
    await callback.message.edit_text(
        f"5. Район: {dist}\n\n👇 Введите <b>Текст отзыва</b>:"
    )

@router.message(AdminState.waiting_for_rev_text)
async def adm_rev_final(message: Message, state: FSMContext):
    text = (message.text or "").strip()
    if not text:
        await message.answer("⚠️ Текст отзыва не может быть пустым.")
        return
    data = await state.get_data()
    city = data.get("rev_city") or "Не указан"
    product = data.get("rev_prod") or "Не указан"
    district = data.get("rev_dist") or "Центр"
    
    ReviewsService.add_manual_review(
        data['rev_nick'],
        city,
        product,
        district,
        text
    )
    
    await message.answer(
        f"✅ Отзыв добавлен!\n\n"
        f"Тип: Обычный\n"
        f"Город: {city}\n"
        f"Позиция: {product}\n"
        f"Текст: {text}"
    )
    
    # Return to menu
    await state.clear()
    await message.answer("⭐️ Меню отзывов:", reply_markup=kb.get_admin_reviews_menu())

# --- MANAGE PRODUCT DISTRICTS ---

@router.callback_query(F.data.startswith('adm_pd_list_'))
async def adm_pd_list(callback: CallbackQuery):
    city_name = callback.data[len('adm_pd_list_'):]
    
    # Show list of products to manage
    products = CatalogService.get_products_for_city(city_name)
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=f"⚙️ {p['name']}", callback_data=f"adm_pd_edit_{city_name}&{p['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")]) # Reuse cat back
    
    await callback.message.edit_text(
        f"🏙 <b>{city_name}: Настройка районов</b>\nВыберите товар:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )

@router.callback_query(F.data.startswith('adm_pd_edit_'))
async def adm_pd_edit(callback: CallbackQuery, state: FSMContext):
    # data: adm_pd_edit_CITY&PID
    content = callback.data[len('adm_pd_edit_'):]
    city_name, p_id = content.split('&', 1)
    
    # Load available districts for city
    all_d, _ = GeoService.get_districts_for_city(city_name)
    if not all_d: all_d = []
    
    # Load currently assigned districts
    assigned = CatalogService.get_product_districts(city_name, p_id)
    # If None -> means "ALL" strictly or "DEFAULT"? 
    # Logic in user flow: if explicit_districts is None -> Fallback to random subset of ALL.
    # If we want to support "Select All", we can treat "All in list" as assigned.
    # If assigned is None, let's treat it as empty or 'All'?
    # To facilitate editing, if it's None, let's pre-select NONE (so user must enable districts).
    # OR pre-select ALL?
    if assigned is None:
        assigned = [] # Start blank
        
    await state.update_data(pd_city=city_name, pd_pid=p_id, pd_sel=assigned)
    
    # Store mapping for optimized callbacks
    dist_map = {str(i): d for i, d in enumerate(all_d)}
    await state.update_data(pd_dist_map=dist_map)
    
    await _show_pd_editor(callback, city_name, p_id, all_d, assigned)

async def _show_pd_editor(callback, city_name, p_id, all_d, assigned):
    # Use helper from kb
    mk = kb.get_admin_product_districts_selector(city_name, p_id, all_d, assigned)
    prod = CatalogService.get_product_by_id(p_id, city_name)
    prod_name = prod['name'] if prod else "???"
    
    try:
        await callback.message.edit_text(
            f"⚙️ <b>{prod_name}</b> ({city_name})\n\n"
            f"Выберите районы, где ЕСТЬ товар (отметьте ✅).\n"
            f"Если список пуст -> используется RANDOMLY сгенерированное наличие.",
            reply_markup=mk
        )
    except Exception as e:
        print(f"Edit error: {e}")
        # Fallback if message too long or other error?
        # Maybe restart menu
        await callback.answer("Ошибка обновления меню")

@router.callback_query(F.data.startswith('adm_pd_tog_'))
async def adm_pd_toggle(callback: CallbackQuery, state: FSMContext):
    # adm_pd_tog_{INDEX}
    idx = callback.data[len('adm_pd_tog_'):]
    
    data = await state.get_data()
    city_name = data.get('pd_city')
    p_id = data.get('pd_pid')
    dist_map = data.get('pd_dist_map', {})
    
    district = dist_map.get(idx)
    if not district:
        await callback.answer("Ошибка: район не найден")
        return
        
    current_sel = data.get('pd_sel', [])
    if district in current_sel:
        current_sel.remove(district)
    else:
        current_sel.append(district)
        
    await state.update_data(pd_sel=current_sel)
    
    # Refresh
    all_d, _ = GeoService.get_districts_for_city(city_name)
    await _show_pd_editor(callback, city_name, p_id, all_d, current_sel)

@router.callback_query(F.data.startswith('adm_pd_save_'))
async def adm_pd_save(callback: CallbackQuery, state: FSMContext):
    content = callback.data[len('adm_pd_save_'):]
    city_name, p_id = content.split('&', 1)
    
    data = await state.get_data()
    selected = data.get('pd_sel', [])
    
    # Save
    CatalogService.set_product_districts(city_name, p_id, selected)
    
    await callback.answer("✅ Сохранено!")
    await adm_pd_list(callback) # Return to list


@router.callback_query(F.data.startswith('adm_pd_back_'))
async def adm_pd_back(callback: CallbackQuery):
    city_name = callback.data[len('adm_pd_back_'):]
    # Creates a fake object simulating adm_pd_list_ call or just calls it directly?
    # adm_pd_list expects callback.data to start with adm_pd_list_
    # Let's just create a new callback data string and re-route? No.
    # Just reuse logic.
    products = CatalogService.get_products_for_city(city_name)
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=f"⚙️ {p['name']}", callback_data=f"adm_pd_edit_{city_name}&{p['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")]) 
    
    await callback.message.edit_text(
        f"🏙 <b>{city_name}: Настройка районов</b>\nВыберите товар:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.callback_query(F.data == 'admin_stash_types_menu')
async def admin_stash_types_menu(callback: CallbackQuery, state: FSMContext):
    await _show_global_stash_menu(callback, state)
    await callback.answer()


@router.callback_query(F.data == 'adm_global_stash_add')
async def adm_global_stash_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_stash_type_add)
    await callback.message.edit_text(
        "➕ Введите название нового типа клада и отправьте сообщением.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_stash_types_menu")]
        ])
    )
    await callback.answer()


@router.message(AdminState.waiting_for_stash_type_add)
async def admin_stash_type_add_message(message: Message, state: FSMContext):
    value = (message.text or "").strip()
    if CatalogService.add_stash_type(value):
        await message.answer("✅ Тип добавлен.")
        await _show_global_stash_menu(message, state)
    else:
        await message.answer(
            "⚠️ Не получилось добавить тип. Он пустой, уже существует или после нормализации совпадает с существующим типом."
        )


@router.callback_query(F.data.startswith('adm_global_stash_del_'))
async def adm_global_stash_delete(callback: CallbackQuery, state: FSMContext):
    index_str = callback.data[len('adm_global_stash_del_'):]
    try:
        idx = int(index_str)
    except ValueError:
        await callback.answer("Ошибка номера.", show_alert=True)
        return
    types = CatalogService.get_available_stash_types()
    if idx < 0 or idx >= len(types):
        await callback.answer("Тип не найден.", show_alert=True)
        return
    target = types[idx]
    if CatalogService.remove_stash_type(target):
        await callback.answer("✅ Удалено")
    else:
        await callback.answer("⚠️ Нельзя удалить: требуется минимум два типа.", show_alert=True)
    await _show_global_stash_menu(callback, state)


@router.callback_query(F.data.startswith('adm_stash_menu_'))
async def adm_stash_menu(callback: CallbackQuery):
    city_name = callback.data[len('adm_stash_menu_'):]
    products = CatalogService.get_products_for_city(city_name)
    await callback.message.edit_text(
        f"🏷 <b>{city_name}:</b> настройки типов кладов\nВыберите товар:",
        reply_markup=kb.get_admin_stash_product_list(city_name, products)
    )


@router.callback_query(F.data.startswith('adm_stash_edit_'))
async def adm_stash_edit(callback: CallbackQuery, state: FSMContext):
    payload = callback.data[len('adm_stash_edit_'):]
    city_name, product_id = payload.split('&', 1)
    all_types = list(CatalogService.STASH_TYPES)
    selected = CatalogService.get_product_stash_types(product_id)
    idx_map = {str(i): item for i, item in enumerate(all_types)}
    await state.update_data(
        stash_city=city_name,
        stash_pid=product_id,
        stash_selected=list(selected),
        stash_all_types=all_types,
        stash_type_map=idx_map
    )
    await state.set_state(AdminState.editing_product_stash)
    await _show_stash_editor(callback, city_name, product_id, all_types, selected)


async def _show_stash_editor(callback: CallbackQuery, city_name: str, product_id: str, all_types: list[str], selected: list[str]):
    product = CatalogService.get_product_by_id(product_id, city_name)
    product_name = product['name'] if product else product_id
    text = (
        f"🏷 <b>{product_name}</b> ({city_name})\n\n"
        "Отметьте ровно два типа кладов, которые будут доступны покупателю."
    )
    try:
        await callback.message.edit_text(
            text,
            reply_markup=kb.get_admin_stash_editor(city_name, product_id, all_types, selected)
        )
    except Exception as e:
        print(f"Stash editor render error: {e}")
        await callback.answer("Не удалось обновить меню", show_alert=True)


@router.callback_query(F.data.startswith('adm_stash_toggle_'), AdminState.editing_product_stash)
async def adm_stash_toggle(callback: CallbackQuery, state: FSMContext):
    idx = callback.data[len('adm_stash_toggle_'):]
    data = await state.get_data()
    all_types = data.get('stash_all_types', list(CatalogService.STASH_TYPES))
    type_map = data.get('stash_type_map', {str(i): item for i, item in enumerate(all_types)})
    target = type_map.get(idx)
    if not target:
        await callback.answer("Тип не найден", show_alert=True)
        return

    selected = list(data.get('stash_selected', []))
    if target in selected:
        selected.remove(target)
    else:
        if len(selected) >= 2:
            await callback.answer("Допустимо только два типа. Снимите лишний.", show_alert=True)
            return
        selected.append(target)

    await state.update_data(stash_selected=selected)
    await _show_stash_editor(callback, data.get('stash_city', ''), data.get('stash_pid', ''), all_types, selected)


@router.callback_query(F.data.startswith('adm_stash_save_'), AdminState.editing_product_stash)
async def adm_stash_save(callback: CallbackQuery, state: FSMContext):
    payload = callback.data[len('adm_stash_save_'):]
    city_name, product_id = payload.split('&', 1)
    data = await state.get_data()
    selected = list(data.get('stash_selected', []))
    if len(selected) != 2:
        await callback.answer("Нужно выбрать ровно два типа.", show_alert=True)
        return

    success = CatalogService.set_product_stash_types(product_id, selected)
    if success:
        await callback.answer("✅ Сохранено")
    else:
        await callback.answer("Не удалось сохранить", show_alert=True)

    await state.set_state(None)
    await state.update_data(stash_selected=None, stash_pid=None, stash_city=None, stash_all_types=None, stash_type_map=None)

    products = CatalogService.get_products_for_city(city_name)
    await callback.message.edit_text(
        f"🏷 <b>{city_name}:</b> настройки типов кладов\nВыберите товар:",
        reply_markup=kb.get_admin_stash_product_list(city_name, products)
    )


@router.callback_query(F.data.startswith('adm_stash_back_'))
async def adm_stash_back(callback: CallbackQuery, state: FSMContext):
    city_name = callback.data[len('adm_stash_back_'):]
    await state.set_state(None)
    await state.update_data(stash_selected=None, stash_pid=None, stash_city=None, stash_all_types=None, stash_type_map=None)
    products = CatalogService.get_products_for_city(city_name)
    await callback.message.edit_text(
        f"🏷 <b>{city_name}:</b> настройки типов кладов\nВыберите товар:",
        reply_markup=kb.get_admin_stash_product_list(city_name, products)
    )

@router.callback_query(F.data == 'admin_rev_add')
async def admin_rev_add(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_review_phrase)
    await callback.message.edit_text(
        "➕ Введите новую фразу или слово:\n"
        "(Например: 'В касание', 'четко', 'магазин огонь')\n\n"
        "Для отмены нажмите /admin и вернитесь.",
        reply_markup=None 
    )

@router.message(AdminState.waiting_for_review_phrase)
async def admin_rev_save(message: Message, state: FSMContext):
    phrase = message.text.strip()
    if phrase:
        if ReviewsService.add_custom_prompt(phrase):
            await message.answer(f"✅ Фраза '{phrase}' добавлена!")
        else:
            await message.answer("⚠️ Такая фраза уже есть или ошибка.")
            
    await state.clear()
    # Возвращаем меню. Придется отправить новым сообщением, так как edit не сработает на user message
    # Но лучше, если админ нажмет кнопку или команду.
    # Для удобства покажем снова панель
    await cmd_admin_panel(message, state) # Вернет в корень, придется кликать снова.
    # Или можно симулировать callback, но это сложно. 
    # Просто напишем что делать.

@router.callback_query(F.data == 'admin_rev_del')
async def admin_rev_del(callback: CallbackQuery, state: FSMContext):
    phrases = ReviewsService.get_custom_prompts()
    if not phrases:
        await callback.answer("Список пуст", show_alert=True)
        return
        
    text = "🗑 Выберите номер фразы для удаления:\n\n"
    for i, p in enumerate(phrases):
        text += f"{i+1}. {p}\n"
        
    await state.set_state(AdminState.waiting_for_review_del_idx)
    await callback.message.edit_text(text + "\nНапишите номер цифрой:")

@router.message(AdminState.waiting_for_review_del_idx)
async def admin_rev_del_do(message: Message, state: FSMContext):
    idx_str = message.text.strip()
    phrases = ReviewsService.get_custom_prompts()
    
    if idx_str.isdigit():
        idx = int(idx_str) - 1
        if 0 <= idx < len(phrases):
            todel = phrases[idx]
            if ReviewsService.remove_custom_prompt(todel):
                await message.answer(f"✅ Удалено: {todel}")
            else:
                await message.answer("Ошибка удаления.")
        else:
            await message.answer("Неверный номер.")
    else:
        await message.answer("Нужно число.")
        
    await state.clear()
    await cmd_admin_panel(message, state)


@router.message(Command('reset_reviews'))
async def cmd_reset_reviews(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    msg = await message.answer("⏳ Пересоздаю отзывы на основе активных городов...")
    new_reviews = ReviewsService.force_regenerate()
    count = len(new_reviews)
    
    await msg.edit_text(f"✅ <b>Отзывы обновлены!</b>\n"
                        f"Сгенерировано {count} шт. на основе истории посещений и доступных товаров.")

@router.message(Command('reset_catalog'))
async def cmd_reset_catalog(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Укажите название города.\nПример: /reset_catalog Томск")
        return
        
    city_name = args[1]
    if CatalogService.force_refresh_products(city_name):
        await message.answer(f"✅ Каталог для города <b>{city_name}</b> сброшен.\nПри следующем запросе сформируется новый список товаров (от 6 до 10 позиций).")
    else:
        await message.answer(f"⚠️ Город <b>{city_name}</b> не найден в кэше прайс-листов (или уже был пуст).")

@router.message(Command('clear_cache'))
async def cmd_clear_all_cache(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    # Очищаем только кэш товаров и прайса. Кэш районов/улиц НЕ трогаем —
    # он накапливается и переиспользуется при повторных запросах пользователей.
    c_cat, c_inventory, c_stash = CatalogService.clear_entire_cache()
    CatalogService.resync_product_stash_types()

    msg = "♻️ <b>Сброс кэша товаров выполнен!</b>\n\n"
    if c_cat in {"removed", "cleared"}:
        msg += "✅ Кэш товаров удален (будет сгенерирован заново).\n"
    elif c_cat == "missing":
        msg += "ℹ️ Кэш товаров уже был пуст.\n"
    else:
        msg += "⚠️ Кэш товаров не удалось удалить.\n"
    if c_inventory in {"removed", "cleared"}:
        msg += "✅ Инвентарь товаров удален (привязки к районам сброшены).\n"
    elif c_inventory == "missing":
        msg += "ℹ️ Инвентарь товаров уже был пуст.\n"
    else:
        msg += "⚠️ Инвентарь товаров не удалось удалить.\n"
    if c_stash in {"removed", "cleared"}:
        msg += "✅ Пары типов кладов сброшены (будут сгенерированы заново).\n"
    elif c_stash == "missing":
        msg += "ℹ️ Пары типов кладов уже были пусты.\n"
    else:
        msg += "⚠️ Пары типов кладов не удалось удалить.\n"
    msg += "✅ Пары типов кладов обновлены по текущему списку.\n"
    msg += "ℹ️ Кэш районов/улиц сохранён (используйте /clear_geo_cache чтобы сбросить его отдельно)."

    await message.answer(msg)


@router.message(Command('clear_geo_cache'))
async def cmd_clear_geo_cache(message: Message):
    """Сброс кэша районов/улиц: весь кэш или только выбранный город.

    Примеры:
    /clear_geo_cache
    /clear_geo_cache Пермь
    """
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    args = (message.text or "").split(maxsplit=1)
    if len(args) == 1:
        c_geo = GeoService.clear_districts_cache()
        if c_geo:
            await message.answer("✅ Кэш районов/улиц удалён полностью. При следующем запросе города данные будут получены заново.")
        else:
            await message.answer("⚠️ Кэш районов пуст или не удалился.")
        return

    city_name = args[1].strip()
    if not city_name:
        await message.answer("⚠️ Укажите город: /clear_geo_cache Пермь")
        return

    ok, removed = GeoService.clear_city_districts_cache(city_name)
    if ok:
        await message.answer(
            f"✅ Гео-кэш для города <b>{city_name}</b> очищен.\n"
            f"Удалено ключей: {removed}."
        )
    else:
        await message.answer(
            f"⚠️ Для города <b>{city_name}</b> записи в гео-кэше не найдены."
        )

@router.callback_query(F.data == 'admin_cancel_op')
async def admin_cancel_operation(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    await callback.message.edit_text('Операция отменена.', reply_markup=kb.get_admin_keyboard())


@router.callback_query(F.data == 'admin_clear_users_confirm')
async def admin_clear_users_confirm(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return

    await callback.message.edit_text(
        "🧹 <b>Очистка базы пользователей</b>\n\n"
        "Будут удалены все записи users_db, кроме вашего админ-профиля.\n"
        "Активные заказы в отдельной базе <b>не удаляются</b>.\n\n"
        "Продолжить?",
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Да, очистить", callback_data="admin_clear_users_execute")],
                [InlineKeyboardButton(text="↩️ Отмена", callback_data="admin_back_main")],
            ]
        ),
    )
    await callback.answer()


@router.callback_query(F.data == 'admin_clear_users_execute')
async def admin_clear_users_execute(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return

    result = UserService.clear_users_db(preserve_user_ids=[callback.from_user.id])
    await callback.message.edit_text(
        "✅ <b>База пользователей очищена.</b>\n\n"
        f"Удалено записей: <b>{result['removed']}</b>\n"
        f"Сохранено записей: <b>{result['kept']}</b>",
        reply_markup=kb.get_admin_keyboard(),
    )
    await callback.answer("Готово")

# --- Статистика ---

@router.callback_query(F.data == 'admin_stats')
async def admin_get_stats(callback: CallbackQuery):
    stats = UserService.get_stats()
    text = (
        f'📊 <b>Статистика бота</b>\n\n'
        f"👥 Всего пользователей: <b>{stats['total']}</b>\n"
        f"🟢 Активных (Бота не блокнули): <b>{stats['active']}</b>\n"
        f"🔴 Заблокировали бота: <b>{stats['blocked']}</b>\n"
    )
    await callback.message.edit_text(text, reply_markup=kb.get_admin_keyboard())


@router.callback_query(F.data == 'admin_product_interest_stats')
async def admin_product_interest_stats(callback: CallbackQuery):
    city_stats = ProductInterestService.get_city_stats()

    if not city_stats:
        await callback.message.edit_text(
            "📈 <b>Интерес к товарам (просмотры)</b>\n\nПока данных нет.",
            reply_markup=kb.get_admin_keyboard()
        )
        return

    await callback.message.edit_text(
        "📈 <b>Интерес к товарам (просмотры)</b>\n\n"
        "Детальная аналитика отправлена отдельным сообщением.",
        reply_markup=kb.get_admin_keyboard()
    )

    chunk_limit = 3800
    chunks: list[str] = ["📈 <b>Интерес к товарам (до оплаты)</b>"]

    for city_row in city_stats:
        city = city_row.get("city", "—")
        total = city_row.get("total_views", 0)
        city_lines = [f"🏙 <b>{city}</b> — {total} выборов"]

        for item in city_row.get("products", []):
            name = item.get("name", item.get("id", "—"))
            pct = item.get("percent", 0.0)
            views = item.get("views", 0)
            city_lines.append(f"• {name}: {pct:.1f}% ({views})")

        city_block = "\n" + "\n".join(city_lines)
        candidate = chunks[-1] + city_block

        if len(candidate) > chunk_limit:
            chunks.append("📈 <b>Интерес к товарам (до оплаты) — продолжение</b>" + city_block)
        else:
            chunks[-1] = candidate

    for chunk in chunks:
        await callback.message.answer(chunk)

# --- Рассылка ---

@router.callback_query(F.data == 'admin_broadcast')
async def admin_ask_broadcast(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_broadcast)
    await callback.message.edit_text(
        '📢 <b>Рассылка всем</b>\n'
        'Введите текст сообщения (или перешлите пост), которое получат все активные пользователи:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_op')]])
    )

@router.message(AdminState.waiting_for_broadcast)
async def admin_process_broadcast(message: Message, state: FSMContext, bot: Bot):
    await state.clear()
    msg = await message.answer('⏳ Начинаю рассылку...')
    
    users = UserService.get_all_users()
    count_ok = 0
    count_err = 0
    
    for uid in users:
        try:
            await message.send_copy(chat_id=uid)
            count_ok += 1
        except Exception:
            UserService.set_active(uid, False)
            count_err += 1
            
    await msg.edit_text(
        f'✅ Рассылка завершена.\n'
        f'Доставлено: {count_ok}\n'
        f'Не доставлено (Блок/Удален): {count_err}',
        reply_markup=kb.get_admin_keyboard()
    )

# --- ЛС Пользователю (инициация) ---

@router.callback_query(F.data == 'admin_toggle_redirect')
async def admin_toggle_redirect(callback: CallbackQuery):
    from app.services.settings import SettingsService
    # Toggle
    new_state = not SettingsService.is_redirect_active()
    SettingsService.set_redirect_active(new_state)
    
    # Update keyboard
    await callback.message.edit_reply_markup(reply_markup=kb.get_admin_keyboard())
    status = "ВКЛЮЧЕН" if new_state else "ВЫКЛЮЧЕН"
    await callback.answer(f"Редирект на Авто-обмен {status}")

@router.callback_query(F.data == 'admin_dm')
async def admin_ask_dm_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_dm_id)
    await callback.message.edit_text(
        '📩 <b>Личное сообщение</b>\n'
        'Введите ID пользователя или @username:',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_op')]])
    )

@router.message(AdminState.waiting_for_dm_id)
async def admin_process_dm_id(message: Message, state: FSMContext):
    user_id = resolve_user_input(message.text)
    
    if user_id:
        username = UserService.get_username(user_id)
        display = f'@{username}' if username else str(user_id)
        await state.update_data(dm_user_id=user_id)
        await state.set_state(AdminState.waiting_for_dm_text)
        await message.answer(
            f'✍ Введите текст сообщения для пользователя <b>{display}</b> ({user_id}):'
        )
    else:
        await message.answer('❌ Пользователь не найден. Введите корректный ID или @username:')

@router.message(AdminState.waiting_for_dm_text)
async def admin_process_dm_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    user_id = data['dm_user_id']
    text = message.text
    
    try:
        sent_msg = await bot.send_message(user_id, f'🔔 <b>Сообщение от администратора:</b>\n\n{text}')
        
        # Callback for deleting both
        del_callback = f"admin_del_dm_{user_id}_{sent_msg.message_id}"
        
        await message.answer(
            f'✅ Успешно отправлено пользователю {user_id}.', 
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑 Удалить у обоих", callback_data=del_callback)],
                [InlineKeyboardButton(text="🔙 В меню", callback_data="admin_back_main")]
            ])
        )
    except Exception as e:
        await message.answer(f'❌ Ошибка отправки: {e}', reply_markup=kb.get_admin_keyboard())
    
    await state.clear()

@router.callback_query(F.data.startswith('admin_del_dm_'))
async def admin_delete_dm_both(callback: CallbackQuery, bot: Bot):
    # data: admin_del_dm_USERID_MSGID
    try:
        parts = callback.data.split('_')
        # parts: ['admin', 'del', 'dm', 'USERID', 'MSGID']
        if len(parts) < 5:
             raise ValueError("Bad data")
             
        user_id = int(parts[3])
        msg_id = int(parts[4])
        
        # 1. Delete user message
        try:
            await bot.delete_message(chat_id=user_id, message_id=msg_id)
        except Exception:
            pass # Already deleted or block
            
        # 2. Delete admin message (or just the button)
        await callback.message.delete()
        await callback.answer("✅ Сообщение удалено у обоих!", show_alert=True)
        
    except Exception as e:
        await callback.answer(f"Ошибка: {e}", show_alert=True)


@router.callback_query(F.data.startswith('admin_del_sent_'))
async def admin_delete_sent(callback: CallbackQuery, bot: Bot):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Нет доступа", show_alert=True)
        return

    parts = callback.data.split('_')
    if len(parts) < 5:
        await callback.answer("Некорректные данные", show_alert=True)
        return

    try:
        user_id = int(parts[3])
        msg_id = int(parts[4])
    except ValueError:
        await callback.answer("Ошибка идентификаторов", show_alert=True)
        return

    removed = False
    try:
        await bot.delete_message(user_id, msg_id)
        removed = True
    except TelegramBadRequest as err:
        logger.warning("Не удалось удалить сообщение %s у пользователя %s: %s", msg_id, user_id, err)
    except Exception:
        logger.exception("Ошибка при удалении сообщения %s у пользователя %s", msg_id, user_id)

    try:
        feedback = "✅ Сообщение удалено у клиента" if removed else "⚠️ Не удалось удалить (уже удалено?)"
        await callback.message.edit_text(feedback)
    except TelegramBadRequest:
        pass

    await callback.answer("Готово")

# --- Баны и разбаны через Панель ---

@router.callback_query(F.data == 'admin_list_bans')
async def admin_show_bans(callback: CallbackQuery):
    try:
        # Постоянный бан (BlacklistService)
        banned_list_perm = BlacklistService.load_blacklist()
        
        # Временный бан (UserService)
        banned_list_temp = UserService.get_temp_banned_users()
        
        text = "📜 <b>Список заблокированных:</b>\n\n"
        
        if not banned_list_perm and not banned_list_temp:
            text += "✅ Список пуст."
        else:
            if banned_list_perm:
                text += "<b>🛑 Навсегда (Blacklist):</b>\n"
                for uid in banned_list_perm:
                    username = UserService.get_username(uid)
                    text += f"• <code>{uid}</code> (@{username if username else '?'})\n"
                text += "\n"
                
            if banned_list_temp:
                text += "<b>⏳ Временно (до ...):</b>\n"
                for item in banned_list_temp:
                    text += f"• <code>{item['id']}</code> (@{item['username']}) — до {item['until']}\n"
            
        await callback.message.edit_text(text, reply_markup=kb.get_admin_keyboard())
    except Exception as e:
        await callback.answer(f'Ошибка: {e}', show_alert=True)

@router.callback_query(F.data == 'admin_ban_user')
async def admin_ask_ban_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_ban_id)
    await callback.message.edit_text(
        '🔨 Введите ID или @username для блокировки (Навсегда):',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_op')]])
    )

@router.message(AdminState.waiting_for_ban_id)
async def admin_process_ban_id(message: Message, state: FSMContext):
    user_id = resolve_user_input(message.text)
    
    if user_id:
        if BlacklistService.ban_user(user_id):
            await message.answer(f'✅ Пользователь {user_id} заблокирован НАВСЕГДА.', reply_markup=kb.get_admin_keyboard())
        else:
            await message.answer(f'⚠️ Пользователь {user_id} уже заблокирован.', reply_markup=kb.get_admin_keyboard())
        await state.clear()
    else:
        await message.answer('❌ Пользователь не найден. Попробуйте еще раз или нажмите Отмена.')

@router.callback_query(F.data == 'admin_unban_user')
async def admin_ask_unban_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(AdminState.waiting_for_unban_id)
    await callback.message.edit_text(
        '😇 Введите ID или @username для разблокировки (Снять любой бан):',
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text='Отмена', callback_data='admin_cancel_op')]])
    )

@router.message(AdminState.waiting_for_unban_id)
async def admin_process_unban_id(message: Message, state: FSMContext):
    user_id = resolve_user_input(message.text)
    
    if user_id:
        # Пробуем снять оба вида бана
        res_perm = BlacklistService.unban_user(user_id)
        res_temp = UserService.unban_user(user_id)
        
        if res_perm or res_temp:
            await message.answer(f'✅ Пользователь {user_id} разблокирован.', reply_markup=kb.get_admin_keyboard())
        else:
            await message.answer(f'⚠️ Пользователь {user_id} не был найден в списках блокировки.', reply_markup=kb.get_admin_keyboard())
        await state.clear()
    else:
        await message.answer('❌ Пользователь не найден. Попробуйте еще раз.')

@router.callback_query(F.data.startswith('admin_cancel_order_'))
async def admin_cancel_order_callback(callback: CallbackQuery, bot: Bot):
    try:
        user_id_str = callback.data.split('_')[-1]
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка ID")
        return
        
    active_order = UserService.get_active_order(user_id)
    if active_order:
        # Delete old payment message if possible
        if "message_id" in active_order:
            try:
                await bot.delete_message(chat_id=user_id, message_id=active_order["message_id"])
            except:
                pass

        UserService.remove_active_order(user_id)
        await callback.answer("✅ Заказ успешно отменен", show_alert=True)
        
        # Обновляем сообщение админа
        try:
            await callback.message.edit_text(
                callback.message.text + "\n\n❌ <b>ЗАКАЗ ОТМЕНЕН АДМИНОМ</b>",
                reply_markup=None # Убираем кнопки
            )
        except Exception:
            pass # Если сообщение старое или удалено

        # Уведомляем пользователя
        try:
            await bot.send_message(
                user_id, 
                "ℹ️ <b>Ваш заказ был отменен администратором.</b>\n"
                "👇 Вы можете оформить новый заказ, выбрав город:", 
                reply_markup=kb.get_base_keyboard()
            )
        except Exception:
            pass
    else:
        await callback.answer("⚠️ Активный заказ не найден (возможно, уже отменен).", show_alert=True)
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except:
            pass

@router.callback_query(F.data.startswith('admin_confirm_order_'))
async def admin_confirm_order_callback(callback: CallbackQuery, bot: Bot):
    try:
        user_id_str = callback.data.split('_')[-1]
        user_id = int(user_id_str)
    except (ValueError, IndexError):
        await callback.answer("⚠️ Ошибка ID")
        return

    # Check authorized admin
    if str(callback.from_user.id) != str(ADMIN_ID):
        return
        
    active_order = UserService.get_active_order(user_id)
    if active_order:
        # Delete old payment message if possible
        if "message_id" in active_order:
            try:
                await bot.delete_message(chat_id=user_id, message_id=active_order["message_id"])
            except:
                pass

        # 1. Считаем покупку
        UserService.increment_purchase(user_id)
        
        # 2. Удаляем активный заказ
        UserService.remove_active_order(user_id)

        if active_order.get("type") == "card_rf":
            UserService.reset_rf_card_cancel_streak(user_id)
        
        # 3. Включаем тихий режим для следующего отзыва
        UserService.set_silent_review_mode(user_id, True)

        await callback.answer("✅ Заказ подтвержден!", show_alert=True)
        
        # 4. Обновляем сообщение админа
        try:
            await callback.message.edit_text(
                callback.message.text + "\n\n✅ <b>ЗАКАЗ ПОДТВЕРЖДЕН ВРУЧНУЮ</b>",
                reply_markup=None
            )
        except:
            pass
            
        # 5. Уведомляем пользователя (Просто подтверждение, без муляжа)
        product_name = active_order.get('product', 'Товар')
        
        try:
             await bot.send_message(
                 user_id, 
                 f"✅ <b>Оплата подтверждена!</b>\n\n"
                 f"Заказ на <b>{product_name}</b> успешно закрыт.\n"
                 f"Покупка зачислена в ваш профиль.\n\n"
                 f"🤝 Спасибо, что выбрали нас!",
                 reply_markup=kb.get_base_keyboard()
             )
        except Exception as e:
             await callback.message.answer(f"⚠️ Покупка засчитана, но сообщение юзеру не дошло: {e}")
    else:
        await callback.answer("⚠️ У пользователя нет активного заказа.", show_alert=True)


# --- Settings for Reviews ---

@router.callback_query(F.data == "adm_rev_settings")
async def adm_rev_settings(callback: CallbackQuery):
    settings = ReviewsService.get_gen_settings()
    text = (
        f"⚙️ <b>Настройки авто-отзывов</b>\n\n"
        f"Текущие параметры:\n"
        f"• Интервал запуска: <b>{settings.get('interval_hours')} ч.</b>\n"
        f"• Минимум на город: <b>{settings.get('min_reviews')} шт.</b>\n"
        f"• Максимум на город: <b>{settings.get('max_reviews')} шт.</b>\n\n"
        f"<i>После изменения интервала новые настройки применятся при следующем запуске процесса.</i>"
    )
    await callback.message.edit_text(text, reply_markup=kb.get_admin_reviews_settings_menu(settings))

@router.callback_query(F.data == "adm_rev_set_interval")
async def adm_rev_set_interval(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "⏳ Введите новый интервал в часах (например: 12 или 2.5):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_settings")]])
    )
    await state.set_state(AdminState.waiting_for_rev_set_interval)

@router.message(AdminState.waiting_for_rev_set_interval)
async def adm_rev_process_interval(message: Message, state: FSMContext):
    try:
        val = float(message.text.replace(",", "."))
        if val <= 0.1: raise ValueError
        ReviewsService.update_gen_settings(interval=val)
        await message.answer("✅ Интервал сохранен.", reply_markup=kb.get_admin_reviews_main_menu())
        await state.clear()
        # Возвращаем меню настроек
        # Можно вызвать adm_rev_settings(message, ...) но это callback
        # Просто отправим сообщение
        settings = ReviewsService.get_gen_settings()
        await message.answer(
             f"Новый интервал: {settings.get('interval_hours')} ч.", 
             reply_markup=kb.get_admin_reviews_settings_menu(settings)
        )
    except ValueError:
        await message.answer("⚠️ Введите корректное число (часы).")

@router.callback_query(F.data == "adm_rev_set_min")
async def adm_rev_set_min(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔢 Введите минимальное кол-во отзывов за раз (целое число):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_settings")]])
    )
    await state.set_state(AdminState.waiting_for_rev_set_min)

@router.message(AdminState.waiting_for_rev_set_min)
async def adm_rev_process_min(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if val < 0: raise ValueError
        ReviewsService.update_gen_settings(min_r=val)
        await message.answer("✅ Минимум сохранен.", reply_markup=kb.get_admin_reviews_main_menu())
        await state.clear()
        settings = ReviewsService.get_gen_settings()
        await message.answer(
             f"Новые настройки: {settings.get('min_reviews')} - {settings.get('max_reviews')}", 
             reply_markup=kb.get_admin_reviews_settings_menu(settings)
        )
    except ValueError:
        await message.answer("⚠️ Введите целое число.")

@router.callback_query(F.data == "adm_rev_set_max")
async def adm_rev_set_max(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "🔢 Введите максимальное кол-во отзывов за раз (целое число):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="adm_rev_settings")]])
    )
    await state.set_state(AdminState.waiting_for_rev_set_max)

@router.message(AdminState.waiting_for_rev_set_max)
async def adm_rev_process_max(message: Message, state: FSMContext):
    try:
        val = int(message.text)
        if val < 0: raise ValueError
        ReviewsService.update_gen_settings(max_r=val)
        await message.answer("✅ Максимум сохранен.", reply_markup=kb.get_admin_reviews_main_menu())
        await state.clear()
        settings = ReviewsService.get_gen_settings()
        await message.answer(
             f"Новые настройки: {settings.get('min_reviews')} - {settings.get('max_reviews')}", 
             reply_markup=kb.get_admin_reviews_settings_menu(settings)
        )
    except ValueError:
         await message.answer("⚠️ Введите целое число.")

# --- Старые команды (оставляем для удобства) ---

@router.callback_query(F.data == 'admin_active_orders')
async def admin_active_orders_callback(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        return

    orders = UserService.get_all_active_orders()
    text = "📋 <b>Активные заказы:</b>\n\n"
    buttons: list[list[InlineKeyboardButton]] = []

    if not orders:
        text += "📭 Активных заказов нет."
    else:
        for o in orders:
            uid = o['user_id']
            uname = o['username'] or "NoName"
            details = o['order']
            product = details.get('product', '???')
            price = details.get('price', 0)
            type_ = details.get('type', 'unknown')
            blocked, until = UserService.is_manual_payment_blocked(int(uid))
            block_status = f"⛔️ Ручная оплата до: {until}" if blocked else "✅ Ручная оплата: доступна"
            rf_blocked, rf_until = UserService.is_rf_card_blocked(int(uid))
            rf_status = f"⛔️ Карта РФ до: {rf_until}" if rf_blocked else "✅ Карта РФ: доступна"

            text += f"🔹 <b>User:</b> {uid} (@{uname})\n"
            text += f"   📦 {product} | {price} руб.\n"
            text += f"   💳 Тип: {type_}\n"
            text += f"   {block_status}\n"
            text += f"   {rf_status}\n"
            text += f"   👉 /confirm_order {uid}\n"
            text += f"   👉 /cancel_order {uid}\n\n"

            row = [
                InlineKeyboardButton(text="⛔️ Блок ручных 24ч", callback_data=f"adm_manual_block_24h_{uid}"),
                InlineKeyboardButton(text="✅ Разблокировать", callback_data=f"adm_manual_unblock_{uid}")
            ]
            if rf_blocked:
                row.append(InlineKeyboardButton(text="✅ Разбан Карта РФ", callback_data=f"adm_rf_unblock_{uid}"))
            buttons.append(row)

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    await callback.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))


@router.callback_query(F.data.startswith("adm_manual_block_24h_"))
async def admin_manual_block_24h(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return

    user_id_raw = callback.data[len("adm_manual_block_24h_"):]
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Некорректный ID", show_alert=True)
        return

    until = (get_now_msk() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    UserService.set_manual_payment_block_until(user_id, until)
    await callback.answer("Блок ручной оплаты установлен на 24 часа")
    await admin_active_orders_callback(callback)


@router.callback_query(F.data.startswith("adm_manual_unblock_"))
async def admin_manual_unblock(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return

    user_id_raw = callback.data[len("adm_manual_unblock_"):]
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Некорректный ID", show_alert=True)
        return

    if UserService.unblock_manual_payment(user_id):
        await callback.answer("Ручная оплата разблокирована")
    else:
        await callback.answer("Блокировка не найдена", show_alert=True)
    await admin_active_orders_callback(callback)


@router.callback_query(F.data.startswith("adm_rf_unblock_"))
async def admin_rf_unblock(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer("Недоступно", show_alert=True)
        return

    user_id_raw = callback.data[len("adm_rf_unblock_"):]
    try:
        user_id = int(user_id_raw)
    except ValueError:
        await callback.answer("Некорректный ID", show_alert=True)
        return

    if UserService.unblock_rf_card_payment(user_id):
        await callback.answer("Оплата картой РФ разблокирована")
    else:
        await callback.answer("Блокировка не найдена", show_alert=True)
    await admin_active_orders_callback(callback)

@router.message(Command("active_orders"))
async def cmd_active_orders(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
        
    orders = UserService.get_all_active_orders()
    if not orders:
        await message.answer("📭 Активных заказов нет.")
        return
        
    text = "📋 <b>Активные заказы:</b>\n\n"
    for o in orders:
        uid = o['user_id']
        uname = o['username'] or "NoName"
        details = o['order']
        product = details.get('product', '???')
        price = details.get('price', 0)
        type_ = details.get('type', 'unknown')
        
        text += f"🔹 <b>User:</b> {uid} (@{uname})\n"
        text += f"   📦 {product} | {price} руб.\n"
        text += f"   💳 Тип: {type_}\n"
        text += f"   👉 /confirm_order {uid}\n"
        text += f"   👉 /cancel_order {uid}\n\n"
        
    await message.answer(text)

@router.message(Command("confirm_order"))
async def cmd_confirm_order(message: Message, bot: Bot):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
        
    try:
        if len(message.text.split()) < 2:
            raise IndexError
            
        user_input = message.text.split()[1]
        user_id = resolve_user_input(user_input)
        
        if not user_id:
            await message.answer("❌ Пользователь не найден")
            return
            
        active_order = UserService.get_active_order(user_id)
        if active_order:
             # Reuse logic
             
             # Delete payment msg
             if "message_id" in active_order:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=active_order["message_id"])
                except:
                    pass

             # 1. Increment stats
             UserService.increment_purchase(user_id)
             # 2. Remove order
             UserService.remove_active_order(user_id)
             if active_order.get("type") == "card_rf":
                 UserService.reset_rf_card_cancel_streak(user_id)
             # 3. Silent review
             UserService.set_silent_review_mode(user_id, True)
             
             # Notify Admin
             await message.answer(f"✅ Заказ пользователя {user_input} подтвержден вручную!")
             
             # Notify User
             product_name = active_order.get('product', 'Товар')
             try:
                 await bot.send_message(
                     user_id, 
                     f"✅ <b>Оплата подтверждена!</b>\n\n"
                     f"Заказ на <b>{product_name}</b> успешно закрыт.\n"
                     f"Покупка зачислена в ваш профиль.\n\n"
                     f"🤝 Спасибо, что выбрали нас!",
                     reply_markup=kb.get_base_keyboard()
                 )
             except Exception as e:
                 await message.answer(f"⚠️ Юзеру не доставилось: {e}")
                 
        else:
             await message.answer(f"⚠️ У пользователя {user_input} нет активных заказов.")
             
    except IndexError:
        await message.answer("⚠️ Использование: /confirm_order ID или @username")

@router.message(Command("cancel_order"))
async def cmd_cancel_order(message: Message, bot: Bot):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
    
    try:
        if len(message.text.split()) < 2:
            raise IndexError
            
        user_input = message.text.split()[1]
        user_id = resolve_user_input(user_input)
        
        if not user_id:
            await message.answer("❌ Пользователь не найден")
            return
            
        active_order = UserService.get_active_order(user_id)
        if active_order:
            # Delete msg
            if "message_id" in active_order:
                try:
                    await bot.delete_message(chat_id=user_id, message_id=active_order["message_id"])
                except:
                    pass

            UserService.remove_active_order(user_id)
            await message.answer(f"✅ Заказ пользователя {user_input} отменен админом.")
            # Notify user
            try:
                await bot.send_message(
                    user_id, 
                    "ℹ️ <b>Ваш заказ был отменен администратором.</b>",
                    reply_markup=kb.get_base_keyboard()
                )
            except:
                pass
        else:
            await message.answer(f"⚠️ У пользователя {user_input} нет активных заказов.")
            
    except IndexError:
        await message.answer("⚠️ Использование: /cancel_order ID или @username")

@router.message(Command('ban'))
async def cmd_ban(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
    try:
        raw = message.text.split()[1]
        user_id = resolve_user_input(raw)
        if user_id:
            if BlacklistService.ban_user(user_id):
                await message.answer(f'🔨 Пользователь {user_id} заблокирован.')
            else:
                await message.answer(f'⚠️ Пользователь {user_id} уже был в бане.')
        else:
             await message.answer('⚠️ Пользователь не найден.')
    except:
        await message.answer('⚠️ Использование: /ban ID или @username')

@router.message(Command('unban'))
async def cmd_unban(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return
    try:
        raw = message.text.split()[1]
        user_id = resolve_user_input(raw)
        if user_id:
            if BlacklistService.unban_user(user_id):
                await message.answer(f'😇 Пользователь {user_id} разблокирован.')
            else:
                await message.answer(f'⚠️ Пользователь {user_id} не найден.')
        else:
             await message.answer('⚠️ Пользователь не найден.')
    except:
        await message.answer('⚠️ Использование: /unban ID или @username')


@router.message(Command("unblock_rf_card"))
async def cmd_unblock_rf_card(message: Message):
    if str(message.from_user.id) != str(ADMIN_ID):
        return

    try:
        if len(message.text.split()) < 2:
            raise IndexError

        user_input = message.text.split()[1]
        user_id = resolve_user_input(user_input)

        if not user_id:
            await message.answer("❌ Пользователь не найден")
            return

        if UserService.unblock_rf_card_payment(user_id):
            await message.answer(f"✅ Оплата картой РФ разблокирована для {user_input}.")
        else:
            await message.answer("⚠️ У пользователя нет активной блокировки по карте РФ.")
    except IndexError:
        await message.answer("⚠️ Использование: /unblock_rf_card ID или @username")

# --- Чат (ручной) ---

@router.callback_query(F.data.startswith('admin_chat_'))
async def start_admin_chat(callback: CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) != str(ADMIN_ID):
        await callback.answer('Вы не администратор.', show_alert=True)
        return
        
    user_id = callback.data.split('_')[2]
    # Попробуем найти имя
    try:
        uid_int = int(user_id)
        uname = UserService.get_username(uid_int)
        display = f'@{uname}' if uname else user_id
    except:
        display = user_id

    await state.update_data(target_user_id=user_id)
    await state.set_state(AdminState.chatting_with_user)
    
    await callback.message.answer(
        f'👨‍💻 <b>Режим чата активирован.</b>\n'
        f'Вы общаетесь с пользователем: {display}\n'
        f'Все ваши сообщения будут пересланы ему.\n'
        f'Для выхода нажмите: /stop_chat'
    )
    await callback.answer()

@router.message(Command('stop_chat'), AdminState.chatting_with_user)
async def stop_chat(message: Message, state: FSMContext):
    await state.clear()
    await message.answer('🛑 Чат завершен. Вы больше не пишете этому пользователю.')

@router.message(AdminState.chatting_with_user)
async def admin_message_proxy(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    target_user_id = data.get('target_user_id')
    
    if target_user_id:
        try:
            sent = await bot.send_message(
                chat_id=target_user_id,
                text=f'👨‍💻 <b>Администратор:</b>\n{message.text}'
            )
            await message.react([{'type': 'emoji', 'emoji': '👍'}])
            delete_cb = f"admin_del_sent_{target_user_id}_{sent.message_id}"
            await message.answer(
                '🗑 Управление сообщением',
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text='🗑 Удалить у клиента', callback_data=delete_cb)]
                ])
            )
        except Exception as e:
            await message.answer(f'❌ Не удалось отправить сообщение: {e}')
    else:
        await message.answer('Ошибка: не выбран пользователь для чата.')

@router.callback_query(F.data == "adm_rev_gen_mass")
async def adm_rev_gen_mass_handler(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        return
    logger.info("Admin %s requested manual review generation", callback.from_user.id)

    try:
        await callback.answer("⏳ Запустил генерацию...", show_alert=False)
    except TelegramBadRequest:
        logger.warning("Callback query too old for immediate answer", exc_info=False)

    try:
        count = await ReviewsService.generate_new_review_task(trigger="manual")
    except Exception:
        logger.exception("Manual review generation failed")
        await callback.message.answer("⚠️ Ошибка генерации. Подробности в логах.")
        return

    logger.info("Manual review generation finished with %s reviews", count)
    await callback.message.answer(f"✅ Сгенерировано {count} новых отзывов!")


@router.callback_query(F.data == "adm_rev_delete_last")
async def adm_rev_delete_last_handler(callback: CallbackQuery):
    if str(callback.from_user.id) != str(ADMIN_ID):
        return

    deleted = ReviewsService.remove_last_generated_batch()
    if deleted:
        try:
            await callback.answer("Удалено", show_alert=False)
        except TelegramBadRequest:
            logger.warning("Callback answer failed for delete-last reviews", exc_info=False)
        await callback.message.answer(f"🧹 Удалено {deleted} отзывов из последней волны.")
    else:
        await callback.answer("Новых отзывов для удаления нет", show_alert=True)

