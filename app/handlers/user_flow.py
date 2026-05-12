from aiogram import Router, F, Bot
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, ChatMemberUpdated, BufferedInputFile
from aiogram.filters import Command, ChatMemberUpdatedFilter, KICKED, MEMBER
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
import asyncio # Добавляем импорт asyncio
import html
from io import BytesIO
import os
import random
import re
import uuid
from PIL import Image, ImageDraw, ImageFont

from app.states import OrderState
from app.services.geo import GeoService
from app.services.catalog import CatalogService
from app.services.blacklist import BlacklistService
from app.services.users import UserService # Подключаем сервис пользователей
from app.services.reviews import ReviewsService # Подключаем сервис отзывов
from app.services.crypto import CryptoService
from app.services.settings import SettingsService
from app.services.analytics import ProductInterestService
from app.states import OrderState, ReviewState, TopUpState # импортируем ReviewState, TopUpState
import app.keyboards as kb
from app.config import ADMIN_ID
from datetime import datetime, timedelta
from app.utils import get_now_msk

router = Router()


class _SafeTemplateDict(dict):
    def __missing__(self, key):
        return "{" + key + "}"


def _render_manual_payment_template(template: str | None, context: dict) -> str | None:
    if not template:
        return None
    try:
        return template.format_map(_SafeTemplateDict(context))
    except Exception:
        return template


def _format_rub_amount(value: float | int | str) -> str:
    try:
        amount = int(round(float(value)))
    except (TypeError, ValueError):
        return str(value)
    return f"{amount:,}".replace(",", " ")


def _build_card_rf_payment_text(
    amount,
    card_number: str,
    payment_request_id: str,
    expires_at: str,
    commission: float | int = 0,
) -> str:
    amount_display = _format_rub_amount(amount)
    commission_line = ""
    try:
        commission_value = float(commission)
    except (TypeError, ValueError):
        commission_value = 0
    if commission_value > 0:
        commission_line = f"💳 <b>КОМИССИЯ: {_format_rub_amount(commission_value)} ₽</b>\n\n"
    return (
        f"💰 <b>СУММА К ОПЛАТЕ: {amount_display} ₽</b>\n\n"
        f"{commission_line}"
        "⏳ <b>ТАЙМЕР: 12 минут</b>\n"
        "После истечения — заявка аннулируется\n\n"
        "📝 <b>РЕКВИЗИТЫ ДЛЯ ОПЛАТЫ:</b>\n\n"
        f"<code>{card_number}</code>\n\n"
        "🌟 <b>ID ЗАЯВКИ:</b>\n"
        f"<code>{payment_request_id}</code>\n\n"
        f"⏰ <b>АКТИВНА ДО: {expires_at} (МСК)</b>\n\n"
        "━━━━━━━━━━━━━━\n"
        "📩 После оплаты отправьте чек\n"
        "(PDF или скриншот)\n"
        "━━━━━━━━━━━━━━\n\n"
        "⚡️ Проверка проходит быстро — не задерживайте отправку"
    )

def _normalize_product_base_name(name: str) -> str:
    if not name:
        return ""
    cleaned = name
    cleaned = re.sub(r"\s*(\d+(?:[\.,]\d+)?)\s*г\.?", "", cleaned, flags=re.IGNORECASE)
    return " ".join(cleaned.split()).strip()

def _extract_base_weight_grams(name: str) -> float:
    if not name:
        return 1.0
    match = re.search(r"(\d+(?:[\.,]\d+)?)\s*г", name)
    if not match:
        return 1.0
    raw = match.group(1).replace(",", ".")
    try:
        value = float(raw)
        return value if value > 0 else 1.0
    except ValueError:
        return 1.0


def _extract_product_weight_label(name: str) -> str:
    match = re.search(r"(\d+(?:[\.,]\d+)?)\s*[🅶г]", name or "", flags=re.IGNORECASE)
    if not match:
        return "не указан"
    return f"{match.group(1).replace(',', '.')} г"


def _parse_index_command(text: str | None, *commands: str) -> int | None:
    raw = " ".join((text or "").strip().split())
    if not raw:
        return None
    for command in commands:
        match = re.fullmatch(rf"/?{re.escape(command)}\s*(\d+)", raw, flags=re.IGNORECASE)
        if match:
            return int(match.group(1))
    if raw.isdigit():
        return int(raw)
    return None


def _matches_command(text: str | None, *commands: str) -> bool:
    raw = " ".join((text or "").strip().split()).lower()
    if not raw:
        return False
    return any(raw == command.lower() or raw == f"/{command.lower()}" for command in commands)


def _parse_payment_command(text: str | None) -> str | None:
    raw = " ".join((text or "").strip().split()).lower()
    if not raw:
        return None
    match = re.fullmatch(r"/?(?:оплата|pay)\s+(.+)", raw)
    if not match:
        return None
    mapping = {
        "баланс": "balance",
        "balance": "balance",
        "крипто": "crypto",
        "крипта": "crypto",
        "crypto": "crypto",
        "карта": "card_rf",
        "card": "card_rf",
        "сбп": "sbp",
        "sbp": "sbp",
        "зарубеж": "foreign",
        "зарубежная": "foreign",
        "foreign": "foreign",
    }
    return mapping.get(match.group(1).strip())


async def _remember_menu_message(state: FSMContext, message: Message | None):
    if not message:
        return
    await state.update_data(active_menu_chat_id=message.chat.id, active_menu_message_id=message.message_id)


async def _delete_previous_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    prev_chat_id = data.get("active_menu_chat_id")
    prev_message_id = data.get("active_menu_message_id")
    if not prev_chat_id or not prev_message_id:
        return
    if prev_chat_id != message.chat.id:
        return
    try:
        await message.bot.delete_message(prev_chat_id, prev_message_id)
    except TelegramBadRequest:
        pass
    await state.update_data(active_menu_chat_id=None, active_menu_message_id=None)


async def _replace_menu_message(message: Message, state: FSMContext, text: str, reply_markup=None) -> Message:
    await _delete_previous_menu(message, state)
    sent = await message.answer(text, reply_markup=reply_markup)
    await _remember_menu_message(state, sent)
    return sent


async def _build_catalog_text(state: FSMContext, city_name: str, population: int = 0, header: str | None = None) -> str:
    products = CatalogService.get_products_for_city(city_name, population)
    product_ids = [item.get("id") for item in products if item.get("id")]
    await state.update_data(catalog_product_ids=product_ids)

    lines = [header or f"📦 <b>Каталог для города {city_name}</b>", "", "Нажмите по команде товара:", ""]
    for index, product in enumerate(products, start=1):
        symbol = product.get("emoji") or CatalogService.get_product_emoji(product["id"], product["name"])
        weight = _extract_product_weight_label(product.get("name", ""))
        lines.append(f"{index}. {symbol} <b>{product['name']}</b>")
        lines.append(f"Вес: {weight} | Цена: {product['price']} руб. | Наличие: есть")
        lines.append(f"/order{index} | /rew{index}")
        lines.append("")

    lines.extend([
        "Команды каталога:",
        "/order1 - купить товар под номером 1",
        "/rew1 - отзывы по товару под номером 1",
        "/allrew - все отзывы по текущему городу",
        "/cityreset - выбрать другой город",
        "/help - список всех команд",
        "/cancel - в начало",
    ])

    return "\n".join(lines)


def _build_user_help_text(city_name: str | None = None) -> str:
    lines = [
        "📘 <b>Подсказка по командам</b>",
        "",
        "<code>/start</code> - перезапустить бота и начать заново",
        "<code>/profile</code> - открыть профиль и баланс",
        "<code>/help</code> - показать эту подсказку",
        "<code>/cancel</code> - выйти в начало",
    ]

    if city_name:
        lines.extend([
            "",
            f"Для выбранного города <b>{html.escape(city_name)}</b>:",
            "<code>/order1</code> - купить товар под номером 1",
            "<code>/rew1</code> - открыть отзывы по товару под номером 1",
            "<code>/allrew</code> - показать все отзывы по этому городу",
            "<code>/cityreset</code> - выбрать другой город",
        ])

    lines.extend([
        "",
        "В режиме отзывов:",
        "<code>/след</code> - следующий отзыв",
        "<code>/пред</code> - предыдущий отзыв",
        "<code>/сброс</code> - убрать фильтр отзывов",
        "<code>/поискотзывов Томск</code> - найти отзывы по городу",
    ])
    return "\n".join(lines)


def _build_city_review_dump_chunks(city_name: str, reviews: list[dict], chunk_limit: int = 3800) -> list[str]:
    header = f"💬 <b>Все отзывы по городу {html.escape(city_name)}</b>\nВсего: <b>{len(reviews)}</b>\n"
    chunks: list[str] = []
    current = header

    for index, review in enumerate(reviews, start=1):
        stars = "⭐️" * int(review.get("stars", 5) or 5)
        user = html.escape(str(review.get("user") or ReviewsService.HIDDEN_USERNAME))
        date = html.escape(str(review.get("display_date") or ""))
        item_info = html.escape(str(review.get("item_info") or ""))
        text = html.escape(str(review.get("text") or ""))
        entry = (
            f"\n<b>{index}.</b> {stars}\n"
            f"👤 <b>{user}</b> ({date})\n"
            f"🛍 <i>{item_info}</i>\n"
            f"🗣 <i>{text}</i>\n"
        )

        if len(current) + len(entry) > chunk_limit:
            chunks.append(current)
            current = header + entry.lstrip("\n")
        else:
            current += entry

    if current:
        chunks.append(current)
    return chunks


async def _send_all_city_reviews(message: Message, state: FSMContext, city_name: str) -> None:
    data = await state.get_data()
    await state.update_data(
        rev_return_city=data.get("rev_search_city"),
        rev_return_product=data.get("rev_search_product"),
        rev_return_product_id=data.get("rev_search_product_id"),
        rev_return_index=int(data.get("rev_current_index", 0) or 0),
        rev_all_city_mode=True,
        rev_search_city=city_name,
        rev_search_product=None,
        rev_search_product_id=None,
    )
    await _show_review_page(
        lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup),
        0,
        state,
    )


async def _restore_after_all_city_reviews(send_method, state: FSMContext):
    data = await state.get_data()

    await state.update_data(
        rev_all_city_mode=False,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )

    city_name = data.get("city")
    population = data.get("population", 0)
    await state.update_data(rev_search_city=None, rev_search_product=None, rev_search_product_id=None)
    catalog_text = await _build_catalog_text(
        state,
        city_name,
        population,
        header=f"📦 <b>Каталог для города {city_name}</b>",
    )
    await send_method(catalog_text, reply_markup=ReplyKeyboardRemove())
    await state.set_state(OrderState.choosing_product)


async def _show_districts_text(message: Message, state: FSMContext, city_name: str, districts: list[str]):
    lines = [f"🏘 <b>Районы города {city_name}</b>", ""]
    for index, district in enumerate(districts, start=1):
        lines.append(f"/district{index} {district}")
    lines.extend([
        "",
        "/back | /cancel",
    ])
    await _replace_menu_message(message, state, "\n".join(lines), reply_markup=ReplyKeyboardRemove())
    await state.set_state(OrderState.choosing_district)


async def _show_treasure_types_text(message: Message, state: FSMContext, product_name: str, options: list[str]):
    lines = ["🔐 <b>Формат выдачи</b>", f"Товар: {product_name}", ""]
    for index, option in enumerate(options, start=1):
        lines.append(f"/format{index} {option}")
    lines.extend([
        "",
        "/back | /cancel",
    ])
    await _replace_menu_message(message, state, "\n".join(lines), reply_markup=ReplyKeyboardRemove())
    await state.set_state(OrderState.choosing_treasure_type)


async def _show_payment_text(message: Message, state: FSMContext, summary_text: str | None = None):
    lines = []
    if summary_text:
        lines.extend([summary_text, ""])
    lines.extend([
        "💳 <b>Способы оплаты</b>",
        "",
        "Можно нажать кнопку ниже сразу или использовать команду:",
        "/pay balance - Баланс (доступно при наличии средств)",
        "/pay crypto - LTC автооплата",
        "/pay card - Карта РФ",
        "/pay sbp - Оплата с помощью СБП",
        "/pay foreign - Оплата с помощью тадж карты",
        "",
        "/back - вернуться к районам",
        "/cancel - в начало",
    ])
    await _replace_menu_message(message, state, "\n".join(lines), reply_markup=kb.get_payment_keyboard())
    await state.set_state(OrderState.choosing_payment)


def _resolve_product_id_by_index(data: dict, index: int) -> str | None:
    if index < 1:
        return None
    product_ids = data.get("catalog_product_ids") or []
    if index > len(product_ids):
        return None
    return product_ids[index - 1]


def _resolve_captcha_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    font_candidates = [
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arialbd.ttf"),
        os.path.join(os.environ.get("WINDIR", "C:\\Windows"), "Fonts", "arial.ttf"),
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ]
    for candidate in font_candidates:
        if os.path.exists(candidate):
            try:
                return ImageFont.truetype(candidate, size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _render_captcha_image(code: str) -> bytes:
    width, height = 320, 140
    image = Image.new("RGB", (width, height), (245, 247, 250))
    draw = ImageDraw.Draw(image)

    for _ in range(14):
        x1 = random.randint(0, width)
        y1 = random.randint(0, height)
        x2 = random.randint(0, width)
        y2 = random.randint(0, height)
        color = (
            random.randint(150, 210),
            random.randint(150, 210),
            random.randint(150, 210),
        )
        draw.line((x1, y1, x2, y2), fill=color, width=random.randint(1, 3))

    for _ in range(70):
        x = random.randint(0, width - 1)
        y = random.randint(0, height - 1)
        radius = random.randint(1, 3)
        color = (
            random.randint(170, 230),
            random.randint(170, 230),
            random.randint(170, 230),
        )
        draw.ellipse((x, y, x + radius, y + radius), fill=color)

    font = _resolve_captcha_font(54)
    slot_width = width // (len(code) + 1)

    for index, digit in enumerate(code):
        char_layer = Image.new("RGBA", (90, 90), (255, 255, 255, 0))
        char_draw = ImageDraw.Draw(char_layer)
        text_color = (
            random.randint(20, 90),
            random.randint(50, 120),
            random.randint(90, 170),
            255,
        )
        bbox = char_draw.textbbox((0, 0), digit, font=font)
        text_width = bbox[2] - bbox[0]
        text_height = bbox[3] - bbox[1]
        text_x = (90 - text_width) // 2
        text_y = (90 - text_height) // 2 - 4
        char_draw.text((text_x, text_y), digit, font=font, fill=text_color)

        rotated = char_layer.rotate(random.randint(-22, 22), resample=Image.Resampling.BICUBIC, expand=True)
        paste_x = 18 + slot_width * index + random.randint(-4, 8)
        paste_y = 20 + random.randint(-6, 10)
        image.paste(rotated, (paste_x, paste_y), rotated)

    draw.rounded_rectangle((10, 10, width - 10, height - 10), radius=18, outline=(185, 191, 199), width=2)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


async def _send_numeric_captcha(message: Message, state: FSMContext) -> None:
    code = f"{random.randint(1000, 9999)}"
    await state.update_data(captcha_correct=code)
    await state.set_state(OrderState.waiting_for_captcha)

    photo = BufferedInputFile(_render_captcha_image(code), filename="captcha.png")
    await message.answer_photo(
        photo,
        caption=(
            "🛡 <b>Проверка входа</b>\n\n"
            "Введите <b>4 цифры</b> с картинки одним сообщением."
        ),
    )


async def _show_card_only_notice(callback: CallbackQuery) -> None:
    notice = (
        "⚠️ <b>Пока нет реквизитов для этого метода.</b>\n\n"
        "Пожалуйста, выберите <b>Оплату картой</b>."
    )
    try:
        await callback.answer("Пока нет реквизитов. Выберите Оплату картой.", show_alert=True)
    except TelegramBadRequest:
        pass
    try:
        await callback.message.answer(notice)
    except TelegramBadRequest:
        pass


async def _start_product_checkout(user_id: int, message: Message, state: FSMContext, product_id: str):
    active_order = UserService.get_active_order(user_id)
    if active_order:
        await message.answer("⚠️ У вас уже есть активный заказ. Оплатите его или отмените.")
        return

    data_state = await state.get_data()
    city_name = data_state.get('city')
    product = CatalogService.get_product_by_id(product_id, city_name=city_name)
    if not product:
        await message.answer("⚠️ Товар недоступен.")
        return

    await state.update_data(product=product, available_treasure_types=None, treasure_type=None)
    data = await state.get_data()
    if not data.get("city") or not data.get("districts"):
        await state.clear()
        await message.answer(
            "⚠️ Данные о городе устарели. Пожалуйста, введите город заново.",
            reply_markup=kb.get_base_keyboard()
        )
        await state.set_state(OrderState.waiting_for_city)
        return

    city_name = data['city']
    all_districts = data['districts']
    available_districts = CatalogService.get_available_districts_for_product(
        city_name,
        product['id'],
        all_districts,
    )

    if not available_districts:
        await message.answer("⚠️ Для этой позиции сейчас не удалось подобрать районы. Попробуйте другой товар или город.")
        await state.set_state(OrderState.choosing_product)
        return

    district_stash_map = CatalogService.get_district_stash_type_map(
        city_name,
        product['id'],
        available_districts,
    )
    await state.update_data(available_districts=available_districts, district_stash_map=district_stash_map)
    await _show_districts_text(message, state, city_name, available_districts)


async def _open_product_reviews_message(message: Message, state: FSMContext, product_id: str):
    data = await state.get_data()
    city_name = data.get('city')
    if not city_name:
        await message.answer("⚠️ Сессия города истекла. Введите город заново.")
        return

    product = CatalogService.get_product_by_id(product_id, city_name=city_name)
    if not product:
        await message.answer("⚠️ Товар недоступен.")
        return

    loading_msg = await _replace_menu_message(message, state, "⏳ <b>Загружаю отзывы по товару...</b>")
    await state.update_data(
        rev_search_city=city_name,
        rev_search_product=product.get('name'),
        rev_search_product_id=product_id,
    )
    await _show_review_page(lambda text, reply_markup=None: loading_msg.edit_text(text, reply_markup=reply_markup), 0, state)


async def _process_crypto_payment_message(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    data = await state.get_data()

    order_data = {
        "type": "crypto",
        "product": data['product']['name'],
        "price": data['product']['price'],
        "treasure_type": data.get('treasure_type', 'Тайник'),
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "message_id": message.message_id,
    }
    UserService.set_active_order(user_id, order_data)

    expires_at = (get_now_msk() + timedelta(minutes=20)).strftime("%H:%M")
    wallet = CryptoService.get_ltc_wallet()

    product_price = data['product']['price'] if data.get('product') else 0
    try:
        price_value = float(product_price)
    except (TypeError, ValueError):
        price_value = 0.0

    rate = await CryptoService.get_ltc_rub_rate()
    rate_line = "📈 Курс LTC: недоступен. Попробуйте позже."
    ltc_line = ""
    if rate:
        ltc_amount = CryptoService.format_ltc_amount(price_value, rate)
        rate_line_value = f"{rate:,.2f}".replace(",", " ")
        rate_line = f"📈 Курс LTC: 1 LTC ≈ {rate_line_value} руб."
        ltc_line = f"💎 К оплате: ≈ {ltc_amount:.6f} LTC"

    details_lines = "\n".join([line for line in (rate_line, ltc_line) if line])
    details_block = f"{details_lines}\n\n" if details_lines else ""

    await message.answer(
        (
            f"💎 <b>Оплата криптовалютой (LTC)</b> [AUTO]\n"
            f"ℹ️ <i>Автоматический режим: оплата проверяется ботом без участия оператора.</i>\n\n"
            f"Адрес кошелька LTC:\n<code>{wallet}</code>\n\n"
            f"{details_block}"
            f"⚠️ На оплату отводится <b>20 минут</b>.\n"
            f"⏳ Заказ автоматически закроется в <b>{expires_at}</b> (по МСК).\n"
            f"Статус: ⏳ <b>Ожидание транзакции...</b>"
        )
    )
    await notify_admin(bot, f"💰 Пользователь {user_id} запросил крипто-реквизиты.")


async def _process_balance_payment_message(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if BlacklistService.is_banned(user_id):
        await message.answer("🚫 Вы заблокированы.")
        return

    data = await state.get_data()
    product = data.get('product')
    price = product['price'] if product else 0

    if UserService.deduct_balance(user_id, price):
        district = data.get('district')
        treasure = data.get('treasure_type', 'Тайник')

        order_data = {
            "type": "balance",
            "product": product['name'],
            "price": price,
            "treasure_type": treasure,
            "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S")
        }
        UserService.set_active_order(user_id, order_data)

        await message.answer(
            f"✅ <b>Оплата прошла успешно!</b>\n"
            f"📦 Товар: {product['name']} ({district})\n"
            f"💰 Списано: {price} руб.\n"
            f"🕵️ Тип клада: {treasure}\n\n"
            f"🛠 <b>Заказ передан оператору.</b>\n"
            f"Пожалуйста, ожидайте, координаты и фото будут отправлены вам в личные сообщения в ближайшее время."
        )
        await notify_admin(bot, f"✅ ОПЛАТА С БАЛАНСА!\nUser: {user_id} (@{message.from_user.username})\nSum: {price}\nItem: {product['name']}")
        await state.clear()
    else:
        bal = UserService.get_balance(user_id)
        await message.answer(f"❌ Недостаточно средств. Ваш баланс: {bal} руб. К оплате: {price} руб.")


async def _process_card_rf_payment_message(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id

    if BlacklistService.is_banned(user_id):
        await message.answer("🚫 Вы заблокированы.")
        return

    blocked_rf, until_rf = UserService.is_rf_card_blocked(user_id)
    if blocked_rf:
        await message.answer(f"⛔️ Оплата картой РФ временно недоступна до {until_rf}. Выберите другой способ.")
        return

    if SettingsService.is_redirect_active():
        await message.answer("⚠️ Оплата картой временно недоступна. Попробуйте криптовалюту позже.")
        return

    blocked, until = UserService.is_manual_payment_blocked(user_id)
    if blocked:
        await message.answer(
            f"⛔️ Оплата картой временно недоступна до {until}. Попробуйте позже или используйте криптовалюту."
        )
        return

    cards = SettingsService.get_rf_cards()
    last_card = UserService.get_last_rf_card(user_id)
    if not cards:
        card_number = None
    elif len(cards) == 1:
        card_number = cards[0]
    else:
        candidates = [item for item in cards if item != last_card]
        if not candidates:
            candidates = list(cards)
        card_number = random.choice(candidates)

    if not card_number:
        await message.answer("⚠️ Способ оплаты картой РФ временно недоступен. Выберите другой способ.")
        return

    UserService.set_last_rf_card(user_id, card_number)

    data = await state.get_data()
    product = data.get('product')
    rf_commission = SettingsService.get_rf_card_commission()
    base_price = product['price'] if product else 0
    total_price = base_price + rf_commission
    try:
        amount_display = f"{float(total_price):.0f}"
    except (TypeError, ValueError):
        amount_display = str(total_price)

    payment_request_id = uuid.uuid4().hex
    expires_at = (get_now_msk() + timedelta(minutes=12)).strftime("%H:%M")

    order_data = {
        "type": "card_rf",
        "product": product['name'] if product else "Unknown",
        "price": total_price,
        "treasure_type": data.get('treasure_type', 'Тайник'),
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "message_id": message.message_id,
        "payment_request_id": payment_request_id,
        "card_number": card_number,
    }
    UserService.set_active_order(user_id, order_data)

    await state.update_data(
        payment_request_id=payment_request_id,
        payment_card=card_number,
        payment_amount=total_price,
    )

    await message.answer(
        _build_card_rf_payment_text(total_price, card_number, payment_request_id, expires_at, rf_commission)
    )

    admin_text = (
        "💳 <b>Заявка на оплату картой РФ</b>\n"
        f"👤 Пользователь: {user_id} (@{message.from_user.username or 'anon'})\n"
        f"🌇 Город: {data.get('city', 'Не выбран')}\n"
        f"🏙 Район: {data.get('district', 'Не выбран')}\n"
        f"📦 Товар: {product['name'] if product else 'Неизвестно'}\n"
        f"🏷 Тип: {data.get('treasure_type') or 'Не выбран'}\n"
        f"💵 Сумма: {amount_display} руб.\n"
        f"📝 Номер заявки: <code>{payment_request_id}</code>\n"
        f"📝 Карта: <code>{card_number}</code>\n"
        f"⏳ Действительно до: {expires_at} (МСК)"
    )

    await notify_admin(bot, admin_text)
    await bot.send_message(
        ADMIN_ID,
        "🔽 Нажмите для чата с пользователем",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")],
            [InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"admin_cancel_order_{user_id}")],
            [InlineKeyboardButton(text="✅ Подтвердить (+покупка)", callback_data=f"admin_confirm_order_{user_id}")]
        ])
    )

    await state.set_state(OrderState.waiting_for_receipt)
    asyncio.create_task(_schedule_rf_payment_reminders(bot, user_id, payment_request_id))

async def notify_admin(bot: Bot, text: str):
    """Отправляет уведомление админу о действиях пользователя"""
    if ADMIN_ID:
        try:
            await bot.send_message(ADMIN_ID, f"👀 Admin Watch:\n{text}")
        except:
            pass


def _apply_rf_card_cancel_policy(user_id: int) -> tuple[str | None, str | None]:
    count = UserService.increment_rf_card_cancel_streak(user_id)
    if count >= 3:
        until = (get_now_msk() + timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        UserService.set_rf_card_block_until(user_id, until)
        return (
            f"⛔️ Оплата картой РФ заблокирована до <b>{until}</b> (МСК).",
            until,
        )
    if count == 2:
        return (
            "⚠️ Это уже вторая отмена оплаты картой РФ подряд. "
            "Следующая отмена приведет к блокировке на сутки.",
            None,
        )
    return None, None


async def _schedule_rf_payment_reminders(bot: Bot, user_id: int, request_id: str):
    schedule = [
        (5 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталось 7 минут."),
        (4 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталось 3 минуты."),
        (2 * 60, "⏳ Напоминание: оплатите или отмените заказ. До авто-отмены осталась 1 минута."),
    ]

    for delay, text in schedule:
        await asyncio.sleep(delay)
        active_order = UserService.get_active_order(user_id)
        if not active_order or active_order.get("payment_request_id") != request_id:
            return
        try:
            await bot.send_message(user_id, text, reply_markup=kb.get_cancel_payment_keyboard())
        except Exception:
            return

    await asyncio.sleep(60)
    active_order = UserService.get_active_order(user_id)
    if not active_order or active_order.get("payment_request_id") != request_id:
        return

    UserService.remove_active_order(user_id)
    rf_note, rf_block_until = _apply_rf_card_cancel_policy(user_id)
    try:
        extra = f"\n\n{rf_note}" if rf_note else ""
        await bot.send_message(
            user_id,
            "⛔️ <b>Заказ отменен по истечению времени.</b>\n"
            "Вы можете оформить новый заказ, выбрав город." + extra,
            reply_markup=kb.get_base_keyboard(),
        )
    except Exception:
        pass

    await notify_admin(bot, f"⏱ Заказ по карте РФ отменен по таймауту. User: {user_id}")
    if rf_block_until:
        await notify_admin(bot, f"⛔️ RF карта блок 24ч. User: {user_id} до {rf_block_until}")


async def _forward_with_admin_controls(bot: Bot, source_message: Message, header: str = ""):
    """Forward a user message to admin with a delete-for-all control."""
    if not ADMIN_ID:
        return

    forwarded_id = 0
    try:
        forwarded = await source_message.forward(ADMIN_ID)
        forwarded_id = forwarded.message_id
    except Exception:
        forwarded_id = 0

    user_id = source_message.from_user.id
    username = source_message.from_user.username or "—"

    lines = []
    if header:
        lines.append(header)
    lines.append(f"👤 Пользователь: {user_id} (@{username})")
    lines.append(f"🆔 Сообщение: {source_message.message_id}")
    info_text = "\n".join(lines)

    delete_cb = f"admin_delmsg_{user_id}_{source_message.message_id}_{forwarded_id}"
    markup = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🗑 Удалить у всех", callback_data=delete_cb)],
        [InlineKeyboardButton(text="💬 Ответить", callback_data=f"admin_chat_{user_id}")]
    ])

    try:
        await bot.send_message(ADMIN_ID, info_text, reply_markup=markup)
    except TelegramBadRequest:
        pass

# --- Глобальная навигация (Обработчики кнопок Назад и Отмена) ---

@router.callback_query(F.data == "nav_cancel")
@router.message(F.text.in_({kb.BTN_CANCEL, "/cancel", "/отмена", "отмена"}))
async def navigation_cancel(event: Message | CallbackQuery, state: FSMContext, bot: Bot):
    user_id = event.from_user.id
    
    # Check TopUp cancellation
    current_state = await state.get_state()
    if current_state and "TopUpState" in current_state:
        username = event.from_user.username or str(user_id)
        # Получаем сумму если была
        data = await state.get_data()
        amount = data.get('topup_amount', 0)
        await notify_admin(bot, f"📉 Пользователь {username} отменил пополнение баланса.\nСумма: {amount} руб.")

    # Check active order cancellation
    active_order = UserService.get_active_order(user_id)
    warning_msg = None

    if active_order:
        UserService.remove_active_order(user_id)
        rf_note = None
        rf_block_until = None
        if active_order.get("type") == "card_rf" and str(user_id) != str(ADMIN_ID):
            rf_note, rf_block_until = _apply_rf_card_cancel_policy(user_id)
            if rf_block_until:
                await notify_admin(bot, f"⛔️ RF карта блок 24ч. User: {user_id} до {rf_block_until}")
        
        # Если это админ - не считаем отмены и не баним
        if str(user_id) != str(ADMIN_ID):
            current_cancels = UserService.increment_cancel_count(user_id)
            username = event.from_user.username or user_id
            await notify_admin(bot, f"🗑 Пользователь {username} отменил активный заказ. Отмен: {current_cancels}/15")
            
            if current_cancels >= 15:
                # Ban for 7 days
                ban_until = (get_now_msk() + timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
                UserService.set_ban_until(user_id, ban_until)
                
                text = f"🚫 <b>ВЫ ЗАБЛОКИРОВАНЫ</b>\n\nВы превысили лимит отмен заказов (15 подряд).\nДоступ ограничен на 7 дней.\nРазблокировка: {ban_until}"
                
                await state.clear()
                if isinstance(event, CallbackQuery):
                    await event.message.edit_text(text)
                else:
                    await event.answer(text, reply_markup=ReplyKeyboardRemove())
                return
            
            warning_msg = f"⚠️ <b>Осторожно!</b>\nВы отменили заказ. Отмен: {current_cancels}/15.\n15 отмен = БАН на 7 дней."
            if rf_note:
                warning_msg += f"\n\n{rf_note}"
        else:
            # Для админа просто уведомление (опционально) или ничего
            pass

    await state.clear()
    
    text = "🏠 <b>Главное меню</b>\n🚫 Операция отменена.\n👇 Пожалуйста, введите ваш город заново:"
    if warning_msg:
        text = warning_msg + "\n\n" + text

    if isinstance(event, CallbackQuery):
        # Удаляем сообщение с Inline кнопкой и отправляем НОВОЕ с Reply клавиатурой
        try:
            await event.message.delete()
        except Exception:
            pass
        await event.message.answer(text, reply_markup=kb.get_base_keyboard())
        await _safe_answer_callback(event)
    else:
        # Для текстового сообщения просто отвечаем с клавиатурой
        await event.answer(text, reply_markup=kb.get_base_keyboard())
    
    await state.set_state(OrderState.waiting_for_city)

@router.callback_query(F.data == "nav_back_city")
async def navigation_back_to_city(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    # Удаляем старое, отправляем новое с клавиатурой
    try:
        await callback.message.delete()
    except Exception:
        pass
    await callback.message.answer(
        "🏙 <b>Смена города</b>\n👇 Введите название нового города:", 
        reply_markup=kb.get_base_keyboard()
    )
    await state.set_state(OrderState.waiting_for_city)
    await callback.answer()

@router.message(F.text == kb.BTN_BACK, OrderState.choosing_district)
async def navigation_back_to_products(message: Message, state: FSMContext):
    data = await state.get_data()
    # Возвращаемся к показу товаров
    # Сначала удаляем Reply клавиатуру
    temp_msg = await message.answer("🔙", reply_markup=ReplyKeyboardRemove())
    await temp_msg.delete()
    
    catalog_text = await _build_catalog_text(
        state,
        data['city'],
        data.get('population', 0),
        header=f"🔙 <b>Возврат к каталогу</b>\n\nНажмите нужное действие ниже:",
    )
    await message.answer(catalog_text, reply_markup=ReplyKeyboardRemove())
    # Используем edit_text нельзя, так как предыдущее сообщение было текстовым (BTN_BACK)
    await state.set_state(OrderState.choosing_product)

@router.callback_query(F.data == "nav_back_district", OrderState.choosing_payment)
async def navigation_back_to_districts(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    available_districts = data.get('available_districts', [])
    await _safe_edit_or_send(
        callback,
        f"🏘 <b>Районы города {data['city']}</b>\n\nНажмите нужный район ниже:",
        reply_markup=None,
    )
    await state.set_state(OrderState.choosing_district)
    await _safe_answer_callback(callback)

# --- Профиль и Пополнение ---

@router.message(F.text == "👤 Профиль")
async def cmd_profile(message: Message):
    user_id = message.from_user.id
    user = UserService.get_user(user_id)
    
    if user:
        balance = user.get("balance", 0.0)
        purchases = user.get("purchases_count", 0)
        discount = user.get("discount_percent", 0)
        refs = user.get("referral_count", 0)
    else:
        balance = 0.0
        purchases = 0
        discount = 0
        refs = 0

    username = message.from_user.username or "Неизвестно"
    
    # Генерация реф ссылки
    bot_user = await message.bot.get_me()
    ref_link = f"https://t.me/{bot_user.username}?start={user_id}"
    
    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
        f"👤 <b>Username:</b> @{username}\n"
        f"➖➖➖➖➖➖➖➖➖➖➖\n"
        f"🛒 <b>Покупок:</b> {purchases}\n"
        f"📉 <b>Ваша скидка:</b> {discount}%\n"
        f"👥 <b>Рефералов:</b> {refs}\n"
        f"💰 <b>Баланс:</b> <code>{balance} руб.</code>\n"
        f"➖➖➖➖➖➖➖➖➖➖➖\n"
        f"🔗 <b>Ваша реферальная ссылка:</b>\n"
        f"<code>{ref_link}</code>\n\n"
        f"<i>Приглашайте друзей и получайте бонусы на баланс!</i>"
    )
    
    await message.answer(text, reply_markup=kb.get_profile_actions_keyboard())


@router.message(F.text == kb.BTN_CONTACTS)
async def show_contacts(message: Message):
    await message.answer(SettingsService.get_contacts_text(), reply_markup=kb.get_base_keyboard())

@router.callback_query(F.data == "profile_topup")
async def start_topup(callback: CallbackQuery, state: FSMContext):
    await callback.message.edit_text(
        "💰 <b>Пополнение баланса</b>\n"
        "👇 Введите сумму пополнения (в рублях):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=kb.BTN_CANCEL, callback_data="nav_cancel")]])
    )
    await state.set_state(TopUpState.waiting_for_amount)
    await callback.answer()

@router.message(TopUpState.waiting_for_amount)
async def process_topup_amount(message: Message, state: FSMContext):
    if BlacklistService.is_banned(message.from_user.id):
        return

    try:
        amount = float(message.text.strip())
        if amount < 1500:
             await message.answer("⚠️ Минимальная сумма пополнения: 1500 руб.")
             return
    except ValueError:
        await message.answer("⚠️ Пожалуйста, введите корректное число.")
        return

    await state.update_data(topup_amount=amount)
    await message.answer(
        f"💳 Сумма к пополнению: <b>{amount} руб.</b>\n👇 Выберите способ оплаты:",
        reply_markup=kb.get_topup_keyboard()
    )
    await state.set_state(TopUpState.choosing_method)

@router.callback_query(F.data == "topup_card", TopUpState.choosing_method)
async def process_topup_card(callback: CallbackQuery, state: FSMContext):
    if BlacklistService.is_banned(callback.from_user.id):
        await callback.answer("🚫 Вы заблокированы.", show_alert=True)
        return

    blocked, until = UserService.is_manual_payment_blocked(callback.from_user.id)
    if blocked:
        await callback.answer(
            f"⛔️ Ручная оплата временно недоступна до {until}. Используйте авто-оплату.",
            show_alert=True,
        )
        try:
            await callback.message.edit_text(
                "⛔️ Ручная оплата временно недоступна. Выберите другой способ оплаты:",
                reply_markup=kb.get_topup_keyboard(),
            )
        except TelegramBadRequest:
            pass
        return

    data = await state.get_data()
    amount = data.get('topup_amount')
    
    await callback.answer() # Убираем лоадер
    
    # Имитация поиска оператора с анимацией
    spinner_frames = ["⏳", "🔄", "🕒", "🔁"]
    frame_text = "{icon} <b>Поиск свободного оператора...</b>\nПожалуйста, не закрывайте меню."
    initial_state = await state.get_state()

    for step in range(15):  # ~30 секунд ожидания с обновлением каждые 2 секунды
        icon = spinner_frames[step % len(spinner_frames)]
        try:
            await callback.message.edit_text(
                frame_text.format(icon=icon),
                reply_markup=kb.get_cancel_payment_keyboard()
            )
        except TelegramBadRequest:
            return

        await asyncio.sleep(2)
        if await state.get_state() != initial_state:
            return

    expires_at = (get_now_msk() + timedelta(minutes=20)).strftime("%H:%M")

    try:
        await callback.message.edit_text(
            f"💳 <b>Пополнение картой / СБП</b> [MANUAL]\n"
            f"✅ <b>Оператор найден и подключен.</b>\n\n"
            f"💰 Сумма: <b>{amount} руб.</b>\n"
            f"📝 Реквизиты для оплаты: <b>Ожидайте ответа оператора (до 5 мин)</b>\n\n"
            f"ℹ️ Вы можете написать сообщение оператору прямо здесь или отправить скриншот оплаты.\n\n"
            f"⏳ Ваша заявка действительна до <b>{expires_at}</b> (по МСК).",
             reply_markup=kb.get_cancel_payment_keyboard()
        )
    except TelegramBadRequest:
        # Сообщение удалено или недоступно
        return

    template = SettingsService.get_manual_payment_template()
    rendered = _render_manual_payment_template(
        template,
        {
            "user_id": callback.from_user.id,
            "amount": amount,
            "expires_at": expires_at,
            "city": data.get("city", ""),
            "district": data.get("district", ""),
            "product": (data.get("product") or {}).get("name", ""),
        },
    )
    if rendered:
        await callback.message.answer(rendered)

    # В реальном сценарии здесь мы ждем скриншот или переводим на чат с админом
    await state.set_state(TopUpState.waiting_for_receipt)
    
    # Уведомляем админа
    # Подробности текущего заказа, если пользователь дошёл до оплаты
    data_state = await state.get_data()
    city = data_state.get('city', 'Не выбран')
    district = data_state.get('district', 'Не выбран')
    product = data_state.get('product') or {}
    product_name = product.get('name', 'Не выбран')
    stash_type = data_state.get('treasure_type') or ', '.join(product.get('stash_types', []) or []) or 'Не выбран'

    try:
        amount_display = f"{float(amount):.0f}"
    except (TypeError, ValueError):
        amount_display = str(amount)
    admin_text = (
        "🆘 Запрос на ручную оплату (СБП или Карта)\n"
        f"👤 Пользователь: {callback.from_user.id} (@{callback.from_user.username or 'anon'})\n"
        f"🌇 Город: {city}\n"
        f"🏙 Район: {district}\n"
        f"📦 Товар: {product_name}\n"
        f"🏷 Тип: {stash_type}\n"
        f"💵 Сумма: {amount_display} руб."
    )

    await notify_admin(callback.bot, admin_text)
    # Также отправим админу кнопку для начала чата
    await callback.bot.send_message(
        ADMIN_ID, 
        "🔽 Нажмите для чата с пользователем",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{callback.from_user.id}")]
        ])
    )

@router.message(TopUpState.waiting_for_receipt)
async def process_topup_receipt(message: Message, state: FSMContext, bot: Bot):
    # Принимаем любой контент как чек
    user_id = message.from_user.id
    data = await state.get_data()
    amount = data.get('topup_amount')
    
    if message.photo or message.document:
        await message.answer("✅ <b>Чек отправлен на проверку.</b>\nОжидайте зачисления средств после подтверждения оператором.", reply_markup=kb.get_base_keyboard())
        await notify_admin(bot, f"🧾 Пользователь {user_id} прислал чек для пополнения на {amount} руб.")
        await _forward_with_admin_controls(bot, message, f"🧾 Чек от клиента ({user_id})")
        await state.clear()
    else:
        # Если это текст, считаем это сообщением оператору
        if ADMIN_ID:
            await _forward_with_admin_controls(bot, message, f"📩 Сообщение от клиента ({user_id}) [TopUp]")
        await message.answer("✅ Сообщение отправлено оператору.")

@router.callback_query(F.data == "topup_crypto", TopUpState.choosing_method)
async def process_topup_crypto(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    amount = data.get('topup_amount')
    wallet = CryptoService.get_ltc_wallet()

    try:
        amount_value = float(amount)
    except (TypeError, ValueError):
        amount_value = 0.0

    rate = await CryptoService.get_ltc_rub_rate()
    rate_line = "📈 Курс LTC: недоступен. Попробуйте позже."
    ltc_line = ""
    if rate:
        ltc_amount = CryptoService.format_ltc_amount(amount_value, rate)
        rate_line_value = f"{rate:,.2f}".replace(",", " ")
        rate_line = f"📈 Курс LTC: 1 LTC ≈ {rate_line_value} руб."
        ltc_line = f"💎 К оплате: ≈ {ltc_amount:.6f} LTC"

    details_lines = "\n".join([line for line in (rate_line, ltc_line) if line])

    details_block = f"{details_lines}\n\n" if details_lines else ""

    await callback.message.edit_text(
        (
            f"💎 <b>Пополнение криптовалютой (LTC)</b>\n"
            f"💰 Сумма: <b>{amount} руб.</b>\n"
            f"{details_block}"
            f"Адрес кошелька LTC:\n<code>{wallet}</code>\n\n"
            f"⏳ Ожидание поступления средств..."
        ),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=kb.BTN_CANCEL, callback_data="nav_cancel")]])
    )
    await notify_admin(callback.bot, f"💎 Пользователь {callback.from_user.id} хочет пополнить на {amount} (Крипто).")
    await callback.answer()

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=KICKED))
async def user_blocked_bot(event: ChatMemberUpdated):
    """Пользователь заблокировал бота"""
    UserService.set_active(event.from_user.id, False)

@router.my_chat_member(ChatMemberUpdatedFilter(member_status_changed=MEMBER))
async def user_unblocked_bot(event: ChatMemberUpdated):
    """Пользователь разблокировал бота"""
    UserService.set_active(event.from_user.id, True)

# ------------------------------------------------------------------

@router.callback_query(F.data == "nav_cancel_delete")
async def navigation_cancel_delete(callback: CallbackQuery):
    await callback.message.delete()
    await callback.answer()

@router.message(F.text == "⭐️ Отзывы")
async def show_reviews(message: Message, state: FSMContext):
    # Reset search filter
    await state.update_data(rev_search_city=None, rev_search_product=None, rev_search_product_id=None)
    
    await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), 0, state)

async def _show_review_page(send_method, index, state):
    data = await state.get_data()
    city_filter = data.get('rev_search_city')
    product_filter = data.get('rev_search_product')
    product_id = data.get('rev_search_product_id')
    all_city_mode = bool(data.get('rev_all_city_mode'))
    await state.update_data(rev_current_index=index)
    
    review, total_real = ReviewsService.get_paginated_review(
        index,
        city_filter,
        product_filter,
    )
    
    if not review:
        if product_filter and city_filter:
            text = (
                "💬 <b>Отзывы по позиции</b>\n\n"
                f"Пока нет опубликованных отзывов для <b>{product_filter}</b> в городе <b>{city_filter}</b>."
            )
        else:
            text = "💬 <b>Отзывы</b>\n\nПока нет отзывов."
        if city_filter:
            text += f"\n(Фильтр: {city_filter})"
        if product_filter:
            text += f"\n(Товар: {product_filter})"
        buttons = []
        if product_id:
            buttons.append([
                InlineKeyboardButton(text="🛒 Оформить", callback_data=f"prod_{product_id}"),
                InlineKeyboardButton(text="↩️ К каталогу", callback_data="rev_back_products"),
            ])
        elif all_city_mode:
            buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="rev_back_from_all")])
        else:
            buttons.append([InlineKeyboardButton(text="🔍 Поиск по городу", callback_data="rev_search_start")])
        buttons.append([InlineKeyboardButton(text="❌ Отмена", callback_data="nav_cancel")])

        await send_method(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
        await state.set_state(ReviewState.browsing_reviews)
        return

    # Visual Counter based purely on real data
    total_visual = total_real
    current_visual = total_visual - index
    if current_visual < 1:
        current_visual = 1
        total_visual = max(total_visual, 1)

    stars = "⭐️" * review.get('stars', 5)
    user = review.get('user') or ReviewsService.HIDDEN_USERNAME
        
    date = review.get('display_date', '')
    text_content = review.get('text', '')
    item_info = review.get('item_info', '') 
    
    if product_filter and city_filter:
        header = f"💬 <b>Отзывы по товару</b> (Всего: {total_visual})\n🏙 Город: <b>{city_filter}</b>\n📦 Товар: <b>{product_filter}</b>\n"
    else:
        header = f"💬 <b>Отзывы магазина</b> (Всего: {total_visual})\n"
    if city_filter:
        header += f"🔍 Фильтр: <b>{city_filter}</b>\n"
    if product_filter and not (product_filter and city_filter):
        header += f"📦 Фильтр: <b>{product_filter}</b>\n"
        
    text = (
        f"{header}\n"
        f"👤 <b>{user}</b> ({date})\n"
        f"{stars}\n"
        f"🛍 <i>{item_info}</i>\n\n" 
        f"🗣 <i>{text_content}</i>\n"
    )

    text += f"\n📄 {current_visual}/{total_visual}\n"
    commands = ["<code>/пред</code> - предыдущий", "<code>/след</code> - следующий"]
    if product_id:
        commands.extend([
            "<code>/купить</code> - оформить товар",
            "<code>/каталог</code> - назад к каталогу",
            "<code>/allrew</code> - все отзывы по городу",
        ])
    elif all_city_mode:
        commands.extend([
            "<code>/назад</code> - назад к каталогу",
        ])
    elif city_filter:
        commands.append("<code>/allrew</code> - все отзывы по городу")
    else:
        commands.append("<code>/поискотзывов Томск</code> - искать по городу")
    text += "\nКоманды: " + " | ".join(commands)

    display_str = f"{current_visual}/{total_visual}"
    review_markup = kb.get_reviews_pagination_kb(
        index,
        total_real,
        city_filter,
        display_text=display_str,
        product_id=product_id,
        back_callback="rev_back_from_all" if all_city_mode and not product_id else None,
    )
    await send_method(text, reply_markup=review_markup)
    await state.set_state(ReviewState.browsing_reviews)

@router.callback_query(F.data == 'rev_back_products')
async def rev_back_products(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get('city')
    population = data.get('population', 0)

    await state.update_data(
        rev_search_city=None,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_city_mode=False,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )
    catalog_text = await _build_catalog_text(
        state,
        city_name,
        population,
        header=f"📦 <b>Каталог для города {city_name}</b>",
    )
    await _safe_answer_callback(callback)
    await _safe_edit_or_send(callback, catalog_text, reply_markup=None, state=state)
    await state.set_state(OrderState.choosing_product)

@router.callback_query(F.data == 'rev_back_from_all')
async def rev_back_from_all(callback: CallbackQuery, state: FSMContext):
    await _safe_answer_callback(callback)
    await _restore_after_all_city_reviews(
        lambda text, reply_markup=None: _safe_edit_or_send(callback, text, reply_markup, state),
        state,
    )

@router.callback_query(F.data.startswith('prod_review_'), OrderState.choosing_product)
async def show_product_reviews(callback: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    city_name = data.get('city')
    if not city_name:
        await callback.answer("Сессия города истекла. Введите город заново.", show_alert=True)
        return

    product_id = callback.data.split('_', 2)[2]
    product = CatalogService.get_product_by_id(product_id, city_name=city_name)
    if not product:
        await callback.answer("Товар недоступен.", show_alert=True)
        return

    await _safe_answer_callback(callback, "Загружаю отзывы...")
    if callback.message:
        try:
            await callback.message.edit_text("⏳ <b>Загружаю отзывы по товару...</b>")
            await _remember_menu_message(state, callback.message)
        except TelegramBadRequest:
            pass

    await state.update_data(
        rev_search_city=city_name,
        rev_search_product=product.get('name'),
        rev_search_product_id=product_id,
        rev_all_city_mode=False,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )
    await _show_review_page(lambda text, reply_markup=None: _safe_edit_or_send(callback, text, reply_markup, state), 0, state)

async def _safe_answer_callback(callback: CallbackQuery, *args, **kwargs):
    try:
        await callback.answer(*args, **kwargs)
    except TelegramBadRequest as err:
        error_text = str(err).lower()
        if "query is too old" in error_text or "query id is invalid" in error_text:
            return
        pass

async def _safe_edit_or_send(callback: CallbackQuery, text: str, reply_markup=None, state: FSMContext | None = None):
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
        if state:
            await _remember_menu_message(state, callback.message)
    except TelegramBadRequest:
        try:
            sent = await callback.message.answer(text, reply_markup=reply_markup)
            if state:
                await _remember_menu_message(state, sent)
        except TelegramBadRequest:
            pass
    
@router.callback_query(F.data.startswith('rev_nav_'))
async def process_rev_nav(callback: CallbackQuery, state: FSMContext):
    try:
        page_idx = int(callback.data.split('_')[2])
    except:
        page_idx = 0

    await _safe_answer_callback(callback)
    await _show_review_page(lambda text, reply_markup=None: _safe_edit_or_send(callback, text, reply_markup, state), page_idx, state)

@router.callback_query(F.data == 'rev_search_start')
async def rev_search_start(callback: CallbackQuery, state: FSMContext):
    await _safe_answer_callback(callback)
    await _safe_edit_or_send(
        callback,
        "🔍 <b>Поиск отзывов</b>\n\n"
        "Введите название города для поиска:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="nav_cancel")]]),
        state=state,
    )
    await state.set_state(ReviewState.waiting_for_search_city)

@router.message(ReviewState.waiting_for_search_city)
async def rev_search_process(message: Message, state: FSMContext):
    city_query = message.text.strip()
    await state.update_data(
        rev_search_city=city_query,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_city_mode=False,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )
    await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), 0, state)

@router.callback_query(F.data == 'rev_search_reset')
async def rev_search_reset(callback: CallbackQuery, state: FSMContext):
    await state.update_data(
        rev_search_city=None,
        rev_search_product=None,
        rev_search_product_id=None,
        rev_all_city_mode=False,
        rev_return_city=None,
        rev_return_product=None,
        rev_return_product_id=None,
        rev_return_index=None,
    )
    await _safe_answer_callback(callback)
    await _show_review_page(lambda text, reply_markup=None: _safe_edit_or_send(callback, text, reply_markup, state), 0, state)


@router.message(ReviewState.browsing_reviews)
async def process_review_commands(message: Message, state: FSMContext):
    data = await state.get_data()
    current_index = int(data.get('rev_current_index', 0) or 0)
    city_for_dump = data.get('rev_search_city') or data.get('city')
    all_city_mode = bool(data.get('rev_all_city_mode'))

    if _matches_command(message.text, "след", "next"):
        await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), current_index + 1, state)
        return
    if _matches_command(message.text, "пред", "prev"):
        await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), max(0, current_index - 1), state)
        return
    if _matches_command(message.text, "каталог", "catalog"):
        city_name = data.get('city')
        population = data.get('population', 0)
        await state.update_data(
            rev_search_city=None,
            rev_search_product=None,
            rev_search_product_id=None,
            rev_all_city_mode=False,
            rev_return_city=None,
            rev_return_product=None,
            rev_return_product_id=None,
            rev_return_index=None,
        )
        catalog_text = await _build_catalog_text(
            state,
            city_name,
            population,
            header=f"📦 <b>Каталог для города {city_name}</b>",
        )
        await _replace_menu_message(message, state, catalog_text, reply_markup=ReplyKeyboardRemove())
        await state.set_state(OrderState.choosing_product)
        return
    if _matches_command(message.text, "назад", "back") and data.get('rev_all_city_mode'):
        await _restore_after_all_city_reviews(
            lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup),
            state,
        )
        return
    if _matches_command(message.text, "купить", "buy"):
        product_id = data.get('rev_search_product_id')
        if not product_id:
            await message.answer("⚠️ Эта команда доступна только в отзывах по конкретному товару.")
            return
        await _start_product_checkout(message.from_user.id, message, state, product_id)
        return
    if _matches_command(message.text, "сброс", "reset"):
        await state.update_data(
            rev_search_city=None,
            rev_search_product=None,
            rev_search_product_id=None,
            rev_all_city_mode=False,
            rev_return_city=None,
            rev_return_product=None,
            rev_return_product_id=None,
            rev_return_index=None,
        )
        await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), 0, state)
        return
    if _matches_command(message.text, "allrew"):
        if not city_for_dump:
            await message.answer("⚠️ Команда <code>/allrew</code> доступна после выбора города.")
            return
        await _send_all_city_reviews(message, state, city_for_dump)
        return

    search_match = re.fullmatch(r"/?поискотзывов\s+(.+)", (message.text or "").strip(), flags=re.IGNORECASE)
    if search_match:
        city_query = search_match.group(1).strip()
        await state.update_data(rev_search_city=city_query, rev_search_product=None, rev_search_product_id=None)
        await _show_review_page(lambda text, reply_markup=None: _replace_menu_message(message, state, text, reply_markup), 0, state)
        return

    await message.answer(
        "⚠️ Используйте <code>/след</code>, <code>/пред</code>, <code>/каталог</code>, <code>/купить</code>, <code>/allrew</code>, <code>/help</code> или <code>/поискотзывов Томск</code>."
    )

@router.callback_query(F.data == "write_review")
async def start_review(callback: CallbackQuery, state: FSMContext):
    user_id = callback.from_user.id
    if BlacklistService.is_banned(user_id):
        await callback.answer("🚫 Вы заблокированы.", show_alert=True)
        return

    # User request: Only admin can write reviews manually via this button.
    # Others get the "need 1 purchase" message.
    if str(user_id) != str(ADMIN_ID):
        await callback.answer("🚫 Оставлять отзывы могут только клиенты с минимум 1 покупкой!", show_alert=True)
        return
        
    await callback.message.answer(
        "✍️ <b>Новый отзыв (Admin Mode)</b>\n"
        "Пожалуйста, напишите ваш честный отзыв одним сообщением.", 
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Отмена", callback_data="nav_cancel")]])
    )
    await state.set_state(ReviewState.waiting_for_text)

@router.message(ReviewState.waiting_for_text)
async def process_review_text(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if BlacklistService.is_banned(user_id):
        return

    # Проверяем "тихий" режим (если заказ подтвержден вручную)
    if UserService.get_silent_review_mode(user_id):
        # Если включен тихий режим - отзыв НЕ отправляется админу
        # Но пользователь видит успех.
        # Сбрасываем режим
        UserService.set_silent_review_mode(user_id, False)
        
        await message.answer("✅ <b>Успешно!</b>\nВаш отзыв принят системой! Спасибо за доверие.")
        await state.clear()
        return

    # Имитация сохранения (обычный режим)
    await message.answer("✅ <b>Успешно!</b>\nВаш отзыв отправлен на модерацию и скоро появится в списке.")
    
    # Уведомляем админа просто для информации (не обязательно, но полезно)
    user_info = f"@{message.from_user.username}" if message.from_user.username else f"ID {message.from_user.id}"
    await notify_admin(bot, f"📝 Новый отзыв от {user_info}:\n{message.text}")
    
    await state.clear()


@router.message(F.text == "❓ Поддержка и правила")
async def show_rules(message: Message):
    text = (
        "📜 <b>Правила магазина</b>\n\n"
        "<b>🔸 Если клад не найден:</b>\n"
        "1. Сначала внимательно сравните место с фотографией и описанием.\n"
        "2. Проверьте все ориентиры, указанные в выдаче.\n"
        "3. Осмотрите точку чуть шире: по сторонам, ниже, глубже, рядом с основным местом.\n"
        "4. Учитывайте, что клад может быть маленьким, незаметным и хорошо спрятанным.\n"
        "5. Не спешите делать вывод до полной проверки точки.\n\n"
        "📸 <b>Фото с места обязательно.</b>\n"
        "Без снимков с локации обращения по ненаходу не принимаются.\n\n"
        "<b>✍️ Как правильно открыть диспут:</b>\n"
        "- Напишите, когда вы прибыли на место.\n"
        "- Кратко опишите, что именно увидели и что уже проверили.\n"
        "- Приложите чёткие фото точки и ожидайте ответ оператора.\n\n"
        "⚖️ <b>Чем может завершиться проверка:</b>\n"
        "✅ <b>Скидка 50%</b> на замену. Если предложение не подходит, диспут закрывается.\n"
        "🔄 <b>Перезаклад</b> возможен только при наличии убедительных доказательств ошибки с нашей стороны.\n"
        "⛔️ <b>Отказ</b> выносится при нарушении правил или недостатке подтверждений.\n\n"
        "🚫 <b>Когда будет отказ:</b>\n"
        "- Если в обращении есть оскорбления, мат или угрозы.\n"
        "- Если обнаружена попытка обмана.\n"
        "- Если с момента покупки прошло более 24 часов.\n"
        "- Если координаты были переданы другим людям.\n"
        "- Если не приложены фото с места поиска.\n"
        "- Если заказ оформлен не на тот город.\n"
        "- Если в профиле нет хотя бы одной успешной покупки.\n"
        "- Если вопросы оператора игнорируются более суток.\n\n"
        f"👇 <b>Для связи используйте кнопку ниже или в лс {SettingsService.get_support_contact()}:</b>"
    )
    await message.answer(text, reply_markup=kb.get_rules_keyboard())

@router.callback_query(F.data == "rules_contact_support")
async def rules_contact_support(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    if BlacklistService.is_banned(user_id):
        await callback.answer("🚫 Вы заблокированы и не можете писать в поддержку.", show_alert=True)
        return

    username = callback.from_user.username
    
    await callback.message.answer(
        "📝 Опишите вашу проблему одним сообщением, приложите фото (если есть) и отправьте.\n"
        "Оператор ответит вам в ближайшее время."
    )
    await state.set_state(OrderState.chat_with_support)
    await callback.answer()
    
    # Уведомляем админа о том, что юзер хочет открыть диспут
    if ADMIN_ID:
        await bot.send_message(
            ADMIN_ID,
            f"🆘 <b>Диспут / Поддержка</b>\n"
            f"Пользователь {user_id} (@{username}) открыл диалог через 'Ненаход/Поддержка'.\n"
            f"Ожидайте сообщения от него.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")]
            ])
        )

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext, bot: Bot):
    # Check permanent ban
    if BlacklistService.is_banned(message.from_user.id):
        return 
        
    # Check temporary ban
    is_temp_banned, until = UserService.is_temp_banned(message.from_user.id)
    if is_temp_banned:
        await message.answer(f"🚫 <b>Вы временно заблокированы</b> до {until} за частые отмены заказов.")
        return

    # Разбираем реферальный аргумент (например /start 12345)
    args = message.text.split()
    referrer_id = None
    if len(args) > 1:
        potential_ref = args[1]
        if potential_ref.isdigit(): # простой чек на ID
            referrer_id = potential_ref

    # Регистрируем/обновляем пользователя
    is_new = UserService.add_user(message.from_user.id, message.from_user.username, referrer_id)
        
    await state.clear()
    await _send_numeric_captcha(message, state)
    
    if is_new:
        ref_info = f" (по приглашению {referrer_id})" if referrer_id else ""
        await notify_admin(bot, f"Пользователь {message.from_user.id} (@{message.from_user.username}) запустил бота{ref_info} (проходит капчу).")

@router.message(F.text.contains("Профиль"))
@router.message(Command("profile"))
async def show_profile(message: Message, bot: Bot):
    user = UserService.get_user(message.from_user.id)
    if not user:
        await message.answer("❌ Профиль не активен. Пожалуйста, введите /start")
        return

    # Получаем данные бота для формирования реф ссылки
    bot_info = await bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={message.from_user.id}"
    
    text = (
        f"👤 <b>Личный кабинет</b>\n\n"
        f"🆔 ID: <code>{message.from_user.id}</code>\n"
        f"👤 Имя: {user.get('username') or 'Не указано'}\n"
        f"📅 Регистрация: {user.get('joined', 'Неизвестно')}\n\n"
        f"🛍 Всего покупок: <b>{user.get('purchases_count', 0)}</b>\n"
        f"📉 Ваша скидка: <b>{user.get('discount_percent', 0)}%</b>\n"
        f"💵 Баланс: <b>{user.get('balance', 0.0)} руб.</b>\n\n"
        f"👥 Приглашено друзей: <b>{user.get('referral_count', 0)}</b>\n"
        f"🔗 <b>Реферальная ссылка:</b>\n<code>{ref_link}</code>"
    )
    
    await message.answer(text, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message, state: FSMContext):
    data = await state.get_data()
    city_name = data.get("city") or data.get("rev_search_city")
    await message.answer(_build_user_help_text(city_name))

@router.message(OrderState.waiting_for_captcha)
async def process_captcha(message: Message, state: FSMContext):
    entered = re.sub(r"\D", "", message.text or "")
    data = await state.get_data()
    correct = str(data.get("captcha_correct") or "")
    
    if entered == correct:
        await message.answer(
            "👋 <b>Добро пожаловать!</b>\n\n"
            "🏙 Начните поиск своего <b>Города</b> (одним словом).\n\n" 
            "📜 <i>Введите полное название города.</i>\n"
            "✅ Пример: <code>Москва</code>, <code>Ростов-на-Дону</code>.\n\n"
            "ℹ️ Команда <code>/help</code> покажет все доступные команды.\n\n"
            f"✅ Поддержка по боту: {SettingsService.get_support_contact()}",
            reply_markup=kb.get_base_keyboard(),
            disable_web_page_preview=True
        )
        await state.set_state(OrderState.waiting_for_city)
        return

    if len(entered) != 4:
        await message.answer("⚠️ Введите ровно 4 цифры с картинки.")
    else:
        await message.answer("❌ Код не совпал. Отправляю новую капчу.")

    await _send_numeric_captcha(message, state)


@router.callback_query(F.data.startswith("captcha_"), OrderState.waiting_for_captcha)
async def process_legacy_captcha_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer("Капча обновилась. Введите 4 цифры с новой картинки.", show_alert=True)
    await _send_numeric_captcha(callback.message, state)

@router.message(OrderState.waiting_for_city)
async def process_city(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    if BlacklistService.is_banned(user_id):
        return
        
    raw_city_name = message.text or ""
    city_name = " ".join(raw_city_name.strip().split())
    print(f"[CityFlow] main search start user={user_id} query='{city_name}'")
    if not city_name:
        await message.answer("⚠️ Название города не может быть пустым. Попробуйте еще раз.")
        return
    
    # === Обработка меню (Профиль, Поддержка) ===
    # Если пользователь нажал на кнопки меню, находясь в ожидании города
    if city_name == "👤 Профиль":
        await show_profile(message, bot)
        return
    elif city_name == kb.BTN_CONTACTS:
        await show_contacts(message)
        return
    elif city_name == "❓ Поддержка и правила":
        await show_rules(message)
        return
        
    # Проверка на навигацию
    if city_name in [kb.BTN_CANCEL]:
         return

    # 1. Пытаемся удалить сообщение пользователя для чистоты чата
    try:
        await message.delete()
    except:
        pass

    msg = await message.answer(f"🔎 <b>Проверяю город</b>: {city_name}")
    
    try:
        candidates = await GeoService.search_cities(city_name)
        print(f"[CityFlow] main search first attempt user={user_id} query='{city_name}' count={len(candidates or [])}")
        if not candidates:
            # Автоповтор запроса на случай кратковременного сбоя
            await asyncio.sleep(0.4)
            candidates = await GeoService.search_cities(city_name)
            print(f"[CityFlow] main search retry user={user_id} query='{city_name}' count={len(candidates or [])}")
    except Exception as e:
        print(f"[CityFlow] main search exception user={user_id} query='{city_name}' err='{e}'")
        await msg.edit_text(f"⚠️ <b>Сервис недоступен</b>\nНе удалось проверить город (проблемы с подключением).")
        return
    
    if not candidates:
        print(f"[CityFlow] main search no candidates user={user_id} query='{city_name}'")
        data = await state.get_data()
        fail_count = data.get("city_fail_count", 0) + 1
        await state.update_data(city_fail_count=fail_count)

        await msg.edit_text(
            "❌ <b>Не удалось найти город.</b>\n" \
            "Проверьте написание и попробуйте отправить название еще раз."
        )

        is_banned_now = False
        if fail_count >= 2:
            is_banned_now = BlacklistService.report_bad_input(user_id)
            if is_banned_now:
                await message.answer(
                    "⛔️ <b>Доступ ограничен</b>\n"
                    "Система безопасности заблокировала вас за подозрительную активность."
                )
                await notify_admin(bot, f"🔨 АВТО-БАН: Пользователь {user_id} заблокирован за спам невалидными городами.")
                await state.clear()
        return

    await state.update_data(city_fail_count=0)

    # Если вариантов несколько - предлагаем выбор
    if len(candidates) > 1:
        await state.update_data(city_candidates=candidates)
        lines = [f"🤔 Мы нашли несколько вариантов для <b>{city_name}</b>.", ""]
        for index, candidate in enumerate(candidates, start=1):
            label = candidate['name']
            if candidate['region']:
                label += f" ({candidate['region']})"
            else:
                label += f" ({candidate['type']})"
            lines.append(f"/city{index} {label}")
        lines.extend([
            "",
            "/cancel",
        ])
        await msg.edit_text(
            "\n".join(lines),
            reply_markup=None,
        )
        await _remember_menu_message(state, msg)
        await state.set_state(OrderState.choosing_city_option)
        return

    # Если вариант только один - используем его сразу
    # Передаем msg, чтобы не плодить новые сообщения, а редактировать это
    await _finalize_city_selection(message, state, bot, candidates[0], existing_msg=msg)

@router.callback_query(F.data.startswith("city_opt_"), OrderState.choosing_city_option)
async def process_city_option(callback: CallbackQuery, state: FSMContext, bot: Bot):
    idx = int(callback.data.split("_")[2])
    data = await state.get_data()
    candidates = data.get('city_candidates', [])
    
    if idx < 0 or idx >= len(candidates):
        await callback.answer("Ошибка выбора варианта.", show_alert=True)
        return
        
    selected_candidate = candidates[idx]
    
    # Редактируем сообщение с меню выбора
    await _finalize_city_selection(callback.message, state, bot, selected_candidate, existing_msg=callback.message)


@router.message(OrderState.choosing_city_option)
async def process_city_option_text(message: Message, state: FSMContext, bot: Bot):
    choice = _parse_index_command(message.text, "city", "город")
    data = await state.get_data()
    candidates = data.get('city_candidates', [])
    if not choice or choice < 1 or choice > len(candidates):
        await message.answer("⚠️ Use /city1 to choose a city variant.")
        return
    await _finalize_city_selection(message, state, bot, candidates[choice - 1])

async def _finalize_city_selection(message: Message, state: FSMContext, bot: Bot, candidate: dict, existing_msg: Message = None):
    # Теперь дополучаем полную инфу (районы и т.д.)
    if existing_msg:
        await existing_msg.edit_text(f"⏳ <b>Собираю каталог</b> для {candidate['name']}...\nПодождите пару секунд.")
        searching_msg = existing_msg
        await _remember_menu_message(state, existing_msg)
    else:
        searching_msg = await _replace_menu_message(
            message,
            state,
            f"⏳ <b>Собираю каталог</b> для {candidate['name']}...\nПодождите пару секунд.",
        )
    
    city_info = await GeoService.get_city_details(candidate)
    
    # Каталог доступен только для городов с подтвержденным населением от 30k.
    # Это исключает обходы через fallback-значения и кэш малых населенных пунктов.
    if not GeoService.is_city_population_allowed(city_info, min_population=30000):
        await searching_msg.edit_text(
            "❌ <b>Каталог для этого населенного пункта недоступен</b>\n\n"
            "Укажите ближайший крупный город и попробуйте снова.\n\n"
            "Введите другой город:"
        )
        await state.set_state(OrderState.waiting_for_city)
        return
    
    # --- TRACKING: Save city as active for AI reviews ---
    ReviewsService.register_active_city(city_info['name'])
    
    # Сохраняем в память вычищенные данные, включая население
    await state.update_data(
        city=city_info['name'], 
        districts=city_info['districts'],
        population=city_info.get('population', 0)
    )
    
    region_info = f" ({city_info['region']})" if city_info.get('region') else ""

    catalog_text = await _build_catalog_text(
        state,
        city_info['name'],
        city_info.get('population', 0),
        header=f"✅ <b>Город найден:</b> {city_info['name']}{region_info}\n\nНиже доступны позиции:",
    )
    await searching_msg.edit_text(catalog_text, reply_markup=None)
    await _remember_menu_message(state, searching_msg)
    await state.set_state(OrderState.choosing_product)
    
    # Получаем дату обновления прайса (если есть) или берем текущую
    updated_at = CatalogService.get_last_update_time(city_info['name'])
    if not updated_at:
        updated_at = get_now_msk().strftime("%d.%m.%Y %H:%M")
    
    await notify_admin(bot, f"Пользователь {message.chat.id} выбрал город: {city_info['name']}{region_info}")
    # await message.answer(f"🕒 Прайс обновлен: {updated_at}")


@router.callback_query(F.data.startswith("prod_"), OrderState.choosing_product)
@router.callback_query(F.data.startswith("prod_"), ReviewState.browsing_reviews)
async def process_product_selection(callback: CallbackQuery, state: FSMContext):
    product_id = callback.data.split("_")[1]
    await _safe_answer_callback(callback, "Открываю товар...")
    await _remember_menu_message(state, callback.message)
    await _start_product_checkout(callback.from_user.id, callback.message, state, product_id)

@router.callback_query(F.data.startswith("prod_"))
async def process_product_selection_ignored(callback: CallbackQuery):
    await callback.answer("⚠️ Выбор принят. Используйте кнопку '🔙 Назад' для смены.", show_alert=True)


@router.message(OrderState.choosing_product)
async def process_product_selection_text(message: Message, state: FSMContext):
    data = await state.get_data()

    if _matches_command(message.text, "другойгород", "cityreset"):
        await state.clear()
        await message.answer("🏙 <b>Смена города</b>\n👇 Введите название нового города:", reply_markup=kb.get_base_keyboard())
        await state.set_state(OrderState.waiting_for_city)
        return

    buy_index = _parse_index_command(message.text, "order", "buy", "купить")
    if buy_index:
        product_id = _resolve_product_id_by_index(data, buy_index)
        if not product_id:
            await message.answer("⚠️ Нет товара с таким номером. Используйте номер из каталога.")
            return
        await _start_product_checkout(message.from_user.id, message, state, product_id)
        return

    review_index = _parse_index_command(message.text, "rew", "reviews", "отзывы")
    if review_index:
        product_id = _resolve_product_id_by_index(data, review_index)
        if not product_id:
            await message.answer("⚠️ Нет товара с таким номером. Используйте номер из каталога.")
            return
        await _open_product_reviews_message(message, state, product_id)
        return

    if _matches_command(message.text, "allrew"):
        city_name = data.get("city")
        if not city_name:
            await message.answer("⚠️ Сначала выберите город, затем используйте <code>/allrew</code>.")
            return
        await _send_all_city_reviews(message, state, city_name)
        return

    if _matches_command(message.text, "help"):
        await message.answer(_build_user_help_text(data.get("city")))
        return

    await message.answer(
        "⚠️ Используйте <code>/order1</code> - купить, <code>/rew1</code> - отзывы по товару, <code>/allrew</code> - все отзывы по городу или <code>/help</code> - список команд."
    )

@router.message(OrderState.choosing_district)
async def process_district(message: Message, state: FSMContext):
    district = (message.text or "").strip()
    data = await state.get_data()

    if not data.get("city"):
        await state.clear()
        await message.answer(
            "⚠️ Сессия выбора города сброшена. Введите город заново.",
            reply_markup=kb.get_base_keyboard()
        )
        await state.set_state(OrderState.waiting_for_city)
        return

    # Проверяем, есть ли район в списке ДОСТУПНЫХ для этого товара
    allowed_districts = data.get('available_districts') or data.get('districts') or []

    if not allowed_districts:
        await state.clear()
        await message.answer(
            "⚠️ Не удалось загрузить список районов. Введите город заново.",
            reply_markup=kb.get_base_keyboard()
        )
        await state.set_state(OrderState.waiting_for_city)
        return
    
    if _matches_command(district, "назад", "back"):
        catalog_text = await _build_catalog_text(
            state,
            data['city'],
            data.get('population', 0),
            header=f"📦 <b>Каталог для города {data['city']}</b>",
        )
        await _replace_menu_message(message, state, catalog_text, reply_markup=ReplyKeyboardRemove())
        await state.set_state(OrderState.choosing_product)
        return

    district_index = _parse_index_command(district, "district", "район")
    if district_index:
        if 1 <= district_index <= len(allowed_districts):
            district = allowed_districts[district_index - 1]
        else:
            await message.answer("⚠️ No district with that number. Use /district1.")
            return
    elif district not in allowed_districts:
        await message.answer("⚠️ Use /district1 or send the exact district name.")
        return

    await state.update_data(district=district, treasure_type=None)

    product = data.get('product') or {}
    product_id = product.get('id')
    district_stash_map = data.get('district_stash_map') or {}
    raw_types = district_stash_map.get(district)
    if not raw_types and product_id:
        raw_types = CatalogService.get_district_stash_types(
            data.get('city', ''),
            product_id,
            district,
            allowed_districts,
        )
    if not raw_types:
        raw_types = CatalogService.STASH_TYPES[:]

    unique_types = []
    for item in raw_types:
        if isinstance(item, str) and item not in unique_types:
            unique_types.append(item)

    if len(unique_types) < 2 and product_id:
        fallback = CatalogService.get_district_stash_types(
            data.get('city', ''),
            product_id,
            district,
            allowed_districts,
        )
        for item in fallback:
            if item not in unique_types:
                unique_types.append(item)
            if len(unique_types) == 2:
                break

    if len(unique_types) >= 2:
        shuffled = random.sample(unique_types, k=len(unique_types))
        display_options = shuffled[:2]
    else:
        display_options = unique_types[:]

    if len(display_options) < 2:
        extras = [t for t in CatalogService.STASH_TYPES if t not in display_options]
        display_options.extend(extras[: max(0, 2 - len(display_options))])

    display_options = display_options[:2]
    await state.update_data(pending_treasure_types=display_options, available_treasure_types=display_options)

    await _show_treasure_types_text(message, state, product.get('name', '—'), display_options)


@router.message(F.text == kb.BTN_BACK, OrderState.choosing_treasure_type)
async def back_to_district_from_type(message: Message, state: FSMContext):
    data = await state.get_data()
    allowed = data.get('available_districts', data.get('districts', []))
    await state.update_data(treasure_type=None, pending_treasure_types=None)
    await _show_districts_text(message, state, data.get('city', ''), allowed)


@router.message(OrderState.choosing_treasure_type)
async def process_treasure_type(message: Message, state: FSMContext):
    choice = (message.text or "").strip()
    data = await state.get_data()
    options = data.get('pending_treasure_types') or data.get('available_treasure_types') or []

    if _matches_command(choice, "назад", "back"):
        allowed = data.get('available_districts', data.get('districts', []))
        await state.update_data(treasure_type=None, pending_treasure_types=None)
        await _show_districts_text(message, state, data.get('city', ''), allowed)
        return

    option_index = _parse_index_command(choice, "format", "формат")
    if option_index:
        if 1 <= option_index <= len(options):
            choice = options[option_index - 1]
        else:
            await message.answer("⚠️ No format with that number. Use /format1.")
            return
    elif choice not in options:
        await message.answer("⚠️ Use /format1 or send the exact format name.")
        return

    await state.update_data(treasure_type=choice, pending_treasure_types=None)

    product = data.get('product', {})
    district = data.get('district')
    city = data.get('city')

    summary = (
        f"🧾 <b>Проверьте заказ</b>\n\n"
        f"🌇 Город: {city}\n"
        f"🏙 Район: {district}\n"
        f"📦 Товар: {product.get('name', '—')}\n"
        f"🏷 Формат: <b>{choice}</b>\n"
        f"💵 Итого: <b>{product.get('price', 0)} руб.</b>\n\n"
        f"⚠️ После выбора способа оплаты у вас будет <b>20 минут</b> на завершение перевода.\n"
        f"Множественные отмены подряд могут привести к временной блокировке."
    )

    await _show_payment_text(message, state, summary_text=summary)


@router.message(OrderState.choosing_payment)
async def process_payment_text(message: Message, state: FSMContext, bot: Bot):
    command = _parse_payment_command(message.text)
    if not command:
        if _matches_command(message.text, "назад", "back"):
            data = await state.get_data()
            allowed = data.get('available_districts', data.get('districts', []))
            await _show_districts_text(message, state, data.get('city', ''), allowed)
            return
        await message.answer("⚠️ Use /pay card or /pay crypto.")
        return

    if command == "balance":
        await _process_balance_payment_message(message, state, bot)
        return
    if command == "crypto":
        await _process_crypto_payment_message(message, state, bot)
        return
    if command == "card_rf":
        await _process_card_rf_payment_message(message, state, bot)
        return
    if command in {"sbp", "foreign"}:
        await message.answer(
            "⚠️ <b>Пока нет реквизитов для этого метода.</b>\n\n"
            "Please choose <b>card payment</b> with /pay card."
        )
        return

@router.callback_query(F.data == "pay_crypto", OrderState.choosing_payment)
async def process_crypto(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    data = await state.get_data()
    
    # Register active order
    order_data = {
        "type": "crypto",
        "product": data['product']['name'],
        "price": data['product']['price'],
        "treasure_type": data.get('treasure_type', 'Тайник'),
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "message_id": callback.message.message_id
    }
    UserService.set_active_order(user_id, order_data)

    expires_at = (get_now_msk() + timedelta(minutes=20)).strftime("%H:%M")

    wallet = CryptoService.get_ltc_wallet()

    product_price = data['product']['price'] if data.get('product') else 0
    try:
        price_value = float(product_price)
    except (TypeError, ValueError):
        price_value = 0.0

    rate = await CryptoService.get_ltc_rub_rate()
    rate_line = "📈 Курс LTC: недоступен. Попробуйте позже."
    ltc_line = ""
    if rate:
        ltc_amount = CryptoService.format_ltc_amount(price_value, rate)
        rate_line_value = f"{rate:,.2f}".replace(",", " ")
        rate_line = f"📈 Курс LTC: 1 LTC ≈ {rate_line_value} руб."
        ltc_line = f"💎 К оплате: ≈ {ltc_amount:.6f} LTC"

    details_lines = "\n".join([line for line in (rate_line, ltc_line) if line])

    details_block = f"{details_lines}\n\n" if details_lines else ""

    await callback.message.edit_text(
        (
            f"💎 <b>Оплата криптовалютой (LTC)</b> [AUTO]\n"
            f"ℹ️ <i>Автоматический режим: оплата проверяется ботом без участия оператора.</i>\n\n"
            f"Адрес кошелька LTC:\n<code>{wallet}</code>\n\n"
            f"{details_block}"
            f"⚠️ На оплату отводится <b>20 минут</b>.\n"
            f"⏳ Заказ автоматически закроется в <b>{expires_at}</b> (по МСК).\n"
            f"Статус: ⏳ <b>Ожидание транзакции...</b>"
        ),
        reply_markup=kb.get_cancel_payment_keyboard()
    )
    # Тут должен быть запуск воркера проверяющего блокчейн
    await notify_admin(bot, f"💰 Пользователь {user_id} запросил крипто-реквизиты.")
    await callback.answer()

@router.callback_query(F.data == "pay_balance", OrderState.choosing_payment)
async def process_balance_payment(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    
    if BlacklistService.is_banned(user_id):
        await callback.answer("🚫 Вы заблокированы.", show_alert=True)
        return

    data = await state.get_data()
    product = data.get('product')
    price = product['price'] if product else 0
    
    # Пытаемся списать
    if UserService.deduct_balance(user_id, price):
        # Успешно списали
        
        # Регистрируем "активный заказ", чтобы пользователь не мог спамить, пока не получит клад
        # Или, если выдача ручная, то заказ висит пока админ не выдаст (админ должен снять active_order)
        # Но у нас нет команды "выдать клад" которая снимает активный заказ. 
        # Active order снимается пользователем (отмена) или по таймеру.
        # В данном случае считаем что заказ оформлен.
        
        district = data.get('district')
        treasure = data.get('treasure_type', 'Тайник')
        
        order_data = {
            "type": "balance",
            "product": product['name'],
            "price": price,
            "treasure_type": treasure,
            "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S")
        }
        UserService.set_active_order(user_id, order_data)
        
        await callback.message.edit_text(
            f"✅ <b>Оплата прошла успешно!</b>\n"
            f"📦 Товар: {product['name']} ({district})\n"
            f"💰 Списано: {price} руб.\n"
            f"🕵️ Тип клада: {treasure}\n\n"
            f"🛠 <b>Заказ передан оператору.</b>\n"
            f"Пожалуйста, ожидайте, координаты и фото будут отправлены вам в личные сообщения в ближайшее время."
            # Нет кнопки отмены, т.к. уже оплачено
        )
        await notify_admin(bot, f"✅ ОПЛАТА С БАЛАНСА!\nUser: {user_id} (@{callback.from_user.username})\nSum: {price}\nItem: {product['name']}")
        await state.clear() # Сбрасываем стейт выбора, но активный заказ остается в базе
    else:
        bal = UserService.get_balance(user_id)
        await callback.answer(f"❌ Недостаточно средств!\nВаш баланс: {bal} руб.\nК оплате: {price} руб.", show_alert=True)


@router.callback_query(F.data == "pay_card", OrderState.choosing_payment)
async def process_card(callback: CallbackQuery, state: FSMContext, bot: Bot):
    await _show_card_only_notice(callback)


@router.callback_query(F.data == "pay_card_rf", OrderState.choosing_payment)
async def process_card_rf(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id

    if BlacklistService.is_banned(user_id):
        await callback.answer("🚫 Вы заблокированы.", show_alert=True)
        return

    blocked_rf, until_rf = UserService.is_rf_card_blocked(user_id)
    if blocked_rf:
        await callback.answer(
            f"⛔️ Оплата картой РФ временно недоступна до {until_rf}. Выберите другой способ.",
            show_alert=True,
        )
        return

    if SettingsService.is_redirect_active():
        await callback.answer("⚠️ Оплата картой временно недоступна. Попробуйте криптовалюту позже.", show_alert=True)
        return

    blocked, until = UserService.is_manual_payment_blocked(user_id)
    if blocked:
        await callback.answer(
            f"⛔️ Оплата картой временно недоступна до {until}. Попробуйте позже или используйте криптовалюту.",
            show_alert=True,
        )
        return

    cards = SettingsService.get_rf_cards()
    last_card = UserService.get_last_rf_card(user_id)
    if not cards:
        card_number = None
    elif len(cards) == 1:
        card_number = cards[0]
    else:
        candidates = [item for item in cards if item != last_card]
        if not candidates:
            candidates = list(cards)
        card_number = random.choice(candidates)
    if not card_number:
        await callback.answer("⚠️ Карты для оплаты не настроены. Выберите другой способ.", show_alert=True)
        await callback.message.edit_text(
            "⚠️ Способ оплаты картой РФ временно недоступен. Выберите другой способ:",
            reply_markup=kb.get_payment_keyboard(),
        )
        return

    UserService.set_last_rf_card(user_id, card_number)

    data = await state.get_data()
    product = data.get('product')
    rf_commission = SettingsService.get_rf_card_commission()
    base_price = product['price'] if product else 0
    total_price = base_price + rf_commission
    try:
        amount_display = f"{float(total_price):.0f}"
    except (TypeError, ValueError):
        amount_display = str(total_price)

    payment_request_id = uuid.uuid4().hex
    expires_at = (get_now_msk() + timedelta(minutes=12)).strftime("%H:%M")

    order_data = {
        "type": "card_rf",
        "product": product['name'] if product else "Unknown",
        "price": total_price,
        "treasure_type": data.get('treasure_type', 'Тайник'),
        "timestamp": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
        "message_id": callback.message.message_id,
        "payment_request_id": payment_request_id,
        "card_number": card_number,
    }
    UserService.set_active_order(user_id, order_data)

    await state.update_data(
        payment_request_id=payment_request_id,
        payment_card=card_number,
        payment_amount=total_price,
    )

    await callback.message.edit_text(
        _build_card_rf_payment_text(total_price, card_number, payment_request_id, expires_at, rf_commission),
        reply_markup=kb.get_cancel_payment_keyboard()
    )

    admin_text = (
        "💳 <b>Заявка на оплату картой РФ</b>\n"
        f"👤 Пользователь: {user_id} (@{callback.from_user.username or 'anon'})\n"
        f"🌇 Город: {data.get('city', 'Не выбран')}\n"
        f"🏙 Район: {data.get('district', 'Не выбран')}\n"
        f"📦 Товар: {product['name'] if product else 'Неизвестно'}\n"
        f"🏷 Тип: {data.get('treasure_type') or 'Не выбран'}\n"
        f"💵 Сумма: {amount_display} руб.\n"
        f"📝 Номер заявки: <code>{payment_request_id}</code>\n"
        f"📝 Карта: <code>{card_number}</code>\n"
        f"⏳ Действительно до: {expires_at} (МСК)"
    )

    await notify_admin(bot, admin_text)
    await bot.send_message(
        ADMIN_ID,
        "🔽 Нажмите для чата с пользователем",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💬 Начать чат", callback_data=f"admin_chat_{user_id}")],
            [InlineKeyboardButton(text="❌ Отменить заказ", callback_data=f"admin_cancel_order_{user_id}")],
            [InlineKeyboardButton(text="✅ Подтвердить (+покупка)", callback_data=f"admin_confirm_order_{user_id}")]
        ])
    )

    await state.set_state(OrderState.waiting_for_receipt)
    await callback.answer()

    asyncio.create_task(_schedule_rf_payment_reminders(bot, user_id, payment_request_id))


@router.callback_query(F.data == "pay_sbp", OrderState.choosing_payment)
async def process_sbp_unavailable(callback: CallbackQuery, state: FSMContext):
    await _show_card_only_notice(callback)


@router.callback_query(F.data == "pay_foreign", OrderState.choosing_payment)
async def process_foreign_unavailable(callback: CallbackQuery, state: FSMContext):
    await _show_card_only_notice(callback)


@router.message(OrderState.waiting_for_receipt)
async def process_order_receipt(message: Message, state: FSMContext, bot: Bot):
    user_id = message.from_user.id
    data = await state.get_data()
    request_id = data.get("payment_request_id")
    amount = data.get("payment_amount")
    card = data.get("payment_card")

    active_order = UserService.get_active_order(user_id)
    if not active_order or active_order.get("payment_request_id") != request_id:
        await message.answer(
            "⚠️ Заявка не найдена или истекла. Оформите новый заказ.",
            reply_markup=kb.get_base_keyboard(),
        )
        await state.clear()
        return

    if message.photo or message.document:
        admin_note = (
            "🧾 <b>Чек по оплате картой РФ</b>\n"
            f"👤 Пользователь: {user_id} (@{message.from_user.username or 'anon'})\n"
            f"💵 Сумма: {amount} руб.\n"
            f"📝 Номер заявки: <code>{request_id}</code>\n"
            f"📝 Карта: <code>{card}</code>"
        )
        await notify_admin(bot, admin_note)
        await _forward_with_admin_controls(bot, message, f"🧾 Чек от клиента ({user_id})")

        checking_msg = await message.answer("⏳ <b>Проверка чека...</b> Пожалуйста, подождите.")
        await asyncio.sleep(20)

        support = SettingsService.get_support_contact()
        text = (
            "⚠️ <b>Оплата не найдена.</b>\n"
            "Попробуйте снова через 3-5 минут и нажмите кнопку обновления проверки.\n\n"
            f"Если возникли проблемы, напишите: {support}"
        )
        try:
            await checking_msg.edit_text(text, reply_markup=kb.get_rf_check_retry_keyboard())
        except Exception:
            await message.answer(text, reply_markup=kb.get_rf_check_retry_keyboard())
        return

    if ADMIN_ID:
        await _forward_with_admin_controls(bot, message, f"📩 Сообщение от клиента ({user_id}) [Карта РФ]")
    await message.answer("✅ Сообщение отправлено оператору.")


@router.callback_query(F.data == "rf_check_retry", OrderState.waiting_for_receipt)
async def rf_check_retry(callback: CallbackQuery, state: FSMContext, bot: Bot):
    user_id = callback.from_user.id
    data = await state.get_data()
    request_id = data.get("payment_request_id")
    amount = data.get("payment_amount")
    card = data.get("payment_card")

    active_order = UserService.get_active_order(user_id)
    if not active_order or active_order.get("payment_request_id") != request_id:
        try:
            await callback.answer("Заявка не найдена или истекла", show_alert=True)
        except TelegramBadRequest:
            pass
        await callback.message.edit_text(
            "⚠️ Заявка не найдена или истекла. Оформите новый заказ.",
            reply_markup=kb.get_base_keyboard(),
        )
        await state.clear()
        return

    try:
        await callback.answer()
    except TelegramBadRequest:
        pass

    checking_msg = await callback.message.edit_text("⏳ <b>Проверка оплаты...</b> Пожалуйста, подождите.")
    await asyncio.sleep(20)

    support = SettingsService.get_support_contact()
    text = (
        "⚠️ <b>Оплата не найдена.</b>\n"
        "Попробуйте снова через 3-5 минут и нажмите кнопку обновления проверки.\n\n"
        f"Если возникли проблемы, напишите: {support}"
    )
    await callback.message.edit_text(text, reply_markup=kb.get_rf_check_retry_keyboard())

@router.message(OrderState.chat_with_support)
async def process_support_chat(message: Message, state: FSMContext, bot: Bot):
    """Пересылает любые сообщения пользователя админу"""
    if BlacklistService.is_banned(message.from_user.id):
        await message.answer("🚫 Вы заблокированы.")
        return

    if ADMIN_ID:
        header = f"📩 Сообщение от клиента ({message.from_user.id})"
        await _forward_with_admin_controls(bot, message, header)
    
    await message.answer("✅ Сообщение доставлено. Ожидайте.")

# --- Авто-обмен (новый метод) ---

@router.callback_query(F.data == "pay_auto_exchange", OrderState.choosing_payment)
async def process_auto_exchange(callback: CallbackQuery, state: FSMContext):
    await _show_card_only_notice(callback)

@router.callback_query(F.data == "exchange_enter_id")
async def ask_exchange_order_id(callback: CallbackQuery, state: FSMContext):
    await state.set_state(OrderState.waiting_for_exchange_order_id)
    await callback.message.answer(
        "📝 Введите <b>ID заявки</b> из обменника:\n"
        "(Только число, например: <code>12345</code>)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="nav_cancel")]])
    )
    await callback.answer()

@router.message(OrderState.waiting_for_exchange_order_id)
async def process_exchange_order_id(message: Message, state: FSMContext, bot: Bot):
    order_id = message.text
    user = message.from_user
    
    data = await state.get_data()
    product = data.get('product')
    district = data.get('district')
    # Для авто-обмена комиссию НЕ добавляем
    total_price = product['price'] if product else 0
    
    # 1. Уведомляем админа
    await notify_admin(bot, 
        f"🔄 <b>Заявка на авто-обмен</b>\n"
        f"👤 Пользователь: {user.id} (@{user.username})\n"
        f"📦 Товар: {product['name'] if product else 'Неизвестно'} ({district})\n"
        f"💰 К оплате: {total_price} руб.\n"
        f"📝 Номер заявки: <code>{order_id}</code>\n"
        f"(Проверьте поступление средств/статус заявки в обменнике)"
    )
    
    # 2. Имитация проверки (думает...)
    checking_msg = await message.answer("⏳ <b>Проверка статуса заявки...</b>")
    await asyncio.sleep(3) # Имитация задержки поиска
    
    # 3. Ответ "Не найдено"
    await checking_msg.delete()
    await message.answer(
        f"⚠️ <b>Заявка #{order_id} не найдена.</b>\n\n"
        f"Проверьте правильность номера. Если вы оплатили только что, зачисление может занять до 10 минут.\n"
        f"Попробуйте проверить снова чуть позже.",
        reply_markup=kb.get_exchange_retry_keyboard()
    )
    # Стейт не сбрасываем? Или сбрасываем? 
    # Кнопка "Проверить оплату (Ввести номер)" снова триггернет 'exchange_enter_id' -> ввод номера.
    # Поэтому можно сбросить или оставить. Лучше сбросить, чтобы "Ввести номер" сработала чисто.
    await state.set_state(OrderState.choosing_payment) 


# --- Fallback (Catch-all) для непонятных сообщений ---
@router.message()
async def unknown_message(message: Message):
    """
    Обработчик-ловушка для всех сообщений, которые не были обработаны другими хендлерами.
    Срабатывает, если пользователь прислал текст, когда ожидалась кнопка или другое действие.
    """
    # Игнорируем команды (они должны обрабатываться отдельно или это реально неизвестная команда)
    if message.text and message.text.startswith("/"):
        return

    await message.answer(
        "🤷‍♂️ <b>Я вас не понимаю.</b>\n\n"
        "Ожидалось нажатие кнопки или ввод данных.\n"
        "👇 Если что-то пошло не так, нажмите <b>Отмена</b>.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text=kb.BTN_CANCEL, callback_data="nav_cancel")]
        ])
    )

