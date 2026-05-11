from aiogram.types import ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton
from app.services.catalog import CatalogService

# Общие кнопки
BTN_BACK = "🔙 Назад"
BTN_CANCEL = "❌ Отмена / В начало"
BTN_CONTACTS = "📞 Контакты"

def get_base_keyboard():
    # Клавиатура для состояний, где нужен только сброс и возможно профиль
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="👤 Профиль")],
            [KeyboardButton(text=BTN_CONTACTS)],
            [KeyboardButton(text="❓ Поддержка и правила")],
            [KeyboardButton(text=BTN_CANCEL)]
        ],
        resize_keyboard=True
    )

def get_profile_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=BTN_CANCEL)]],
        resize_keyboard=True
    )

def get_rules_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✍️ Нᴀᴨиᴄᴀᴛь нᴀʍ", callback_data="rules_contact_support")],
            [InlineKeyboardButton(text="🔙 Зᴀᴋᴩыᴛь", callback_data="nav_cancel_delete")]
        ]
    )


def get_districts_keyboard(districts: list):
    builder = []
    row = []
    for district in districts:
        row.append(KeyboardButton(text=district))
        if len(row) == 2:
            builder.append(row)
            row = []
    if row:
        builder.append(row)
    
    # Добавляем навигацию
    builder.append([KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)])
    
    return ReplyKeyboardMarkup(keyboard=builder, resize_keyboard=True)

def get_treasure_types_keyboard(types: list[str]):
    rows = [[KeyboardButton(text=option)] for option in types if isinstance(option, str)]
    rows.append([KeyboardButton(text=BTN_BACK), KeyboardButton(text=BTN_CANCEL)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)

def get_products_keyboard(city_name: str, population: int = 0):
    products = CatalogService.get_products_for_city(city_name, population)
    buttons = []
    for p in products:
        symbol = p.get("emoji") or CatalogService.get_product_emoji(p["id"], p["name"])
        btn_text = f"{symbol} {p['name']} - {p['price']} руб."
        buttons.append([InlineKeyboardButton(text=btn_text, callback_data=f"prod_{p['id']}")])
        buttons.append([InlineKeyboardButton(text="💬 Отзывы по товару", callback_data=f"prod_review_{p['id']}")])
    
    # Кнопка отмены для инлайн режима (т.к. клавиатура в сообщении)
    buttons.append([InlineKeyboardButton(text=BTN_CANCEL, callback_data="nav_cancel")])
    buttons.append([InlineKeyboardButton(text="🔄 Выбрать другой город", callback_data="nav_back_city")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_payment_keyboard():
    buttons = [
        [InlineKeyboardButton(text="💰 С бᴀᴧᴀнᴄᴀ", callback_data="pay_balance")],
        [InlineKeyboardButton(text="💎 Кᴩиᴨᴛᴏʙᴀᴧюᴛᴀ (Аʙᴛᴏ)", callback_data="pay_crypto")],
        [InlineKeyboardButton(text="💳 Оᴨᴧᴀᴛᴀ ᴋᴀᴩᴛᴏй", callback_data="pay_card_rf")],
        [InlineKeyboardButton(text="📲 СБП", callback_data="pay_sbp")],
        [InlineKeyboardButton(text="🌍 Зарубежная карта", callback_data="pay_foreign")],
        [InlineKeyboardButton(text=BTN_BACK, callback_data="nav_back_district")], 
        [InlineKeyboardButton(text=BTN_CANCEL, callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_topup_keyboard():
    buttons = [
        [InlineKeyboardButton(text="💎 Кᴩиᴨᴛᴏʙᴀᴧюᴛᴀ (Аʙᴛᴏ)", callback_data="topup_crypto")],
        [InlineKeyboardButton(text="💳 Кᴀᴩᴛᴀ / СБП (ᴄ ᴏᴨᴇᴩᴀᴛᴏᴩᴏʍ)", callback_data="topup_card")],
        [InlineKeyboardButton(text=BTN_CANCEL, callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_profile_actions_keyboard():
    buttons = [
        [InlineKeyboardButton(text="➕ Пᴏᴨᴏᴧниᴛь бᴀᴧᴀнᴄ", callback_data="profile_topup")],
        [InlineKeyboardButton(text=BTN_CANCEL, callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_exchange_init_keyboard():
    buttons = [
        [InlineKeyboardButton(text="✅ Ввести номер заявки", callback_data="exchange_enter_id")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_exchange_retry_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🔄 Проверить оплату (Ввести номер)", callback_data="exchange_enter_id")],
        [InlineKeyboardButton(text="❌ Отмена", callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def check_payment_keyboard():
    buttons = [
        [InlineKeyboardButton(text="✅ Я оплатил", callback_data="check_payment")],
        [InlineKeyboardButton(text="❌ Отменить заказ", callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_cancel_payment_keyboard():
    """Клавиатура для отмены внутри процесса оплаты"""
    buttons = [
        [InlineKeyboardButton(text="❌ Отменить оплату", callback_data="nav_cancel")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_rf_check_retry_keyboard():
    buttons = [
        [InlineKeyboardButton(text="🔄 Обновить проверку", callback_data="rf_check_retry")],
        [InlineKeyboardButton(text="❌ Отменить оплату", callback_data="nav_cancel")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_reviews_pagination_kb(
    current_index: int,
    total_count: int,
    city_filter: str = None,
    display_text: str = None,
    product_id: str | None = None,
    back_callback: str | None = None,
):
    # current_index is 0-based.
    # Display index is usually current_index + 1, OR handled by display_text
    
    buttons = []
    
    nav_row = []
    
    # Prev button (Left)
    if current_index > 0:
        nav_row.append(InlineKeyboardButton(text="⬅️", callback_data=f"rev_nav_{current_index-1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⬛️", callback_data="ignore"))
        
    # Counter in middle
    if display_text:
        count_display = display_text
    else:
        count_display = f"{current_index + 1}/{total_count}"
        
    nav_row.append(InlineKeyboardButton(text=count_display, callback_data="ignore"))
    
    # Next button (Right)
    if current_index < total_count - 1:
        nav_row.append(InlineKeyboardButton(text="➡️", callback_data=f"rev_nav_{current_index+1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="⬛️", callback_data="ignore"))
        
    buttons.append(nav_row)
    
    if product_id:
        buttons.append([
            InlineKeyboardButton(text="🛒 Оформить", callback_data=f"prod_{product_id}"),
            InlineKeyboardButton(text="↩️ К каталогу", callback_data="rev_back_products"),
        ])
    else:
        if back_callback:
            buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=back_callback)])
        if city_filter:
            buttons.append([InlineKeyboardButton(text="❌ Сброс фильтра", callback_data="rev_search_reset")])
        else:
            buttons.append([InlineKeyboardButton(text="🔍 Поиск по городу", callback_data="rev_search_start")])
    
    buttons.append([InlineKeyboardButton(text=BTN_CANCEL, callback_data="nav_cancel")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_keyboard():

    # Импорт внутри функции чтобы избежать циклических импортов
    from app.services.settings import SettingsService
    is_redirect = SettingsService.is_redirect_active()
    toggle_text = "🔴 Вкл. редирект на Авто" if not is_redirect else "🟢 Выкл. редирект (Активен)"

    buttons = [
        [InlineKeyboardButton(text="🛒 Управление товарами (Каталоги)", callback_data="admin_catalog_search")],
        [InlineKeyboardButton(text="📦 База товаров (all_products)", callback_data="admin_products_global_menu")],
        [InlineKeyboardButton(text=" Генерация прайса: фильтр товаров", callback_data="admin_catalog_generation_settings")],
        [InlineKeyboardButton(text="💰 Управление наценками на районы", callback_data="admin_pricing_menu")],
        [InlineKeyboardButton(text="💎 Курс LTC", callback_data="admin_ltc_menu")],
        [InlineKeyboardButton(text="💸 Комиссия ручной оплаты", callback_data="admin_commission_menu")],
        [InlineKeyboardButton(text="💸 Комиссия Карта РФ", callback_data="admin_rf_commission_menu")],
        [InlineKeyboardButton(text="💳 Карты РФ (оплата)", callback_data="admin_rf_cards_menu")],
        [InlineKeyboardButton(text="✅ Разбан Карта РФ", callback_data="admin_rf_unblock_menu")],
        [InlineKeyboardButton(text="🔗 Ссылка авто-оплаты", callback_data="admin_exchange_bot_menu")],
        [InlineKeyboardButton(text="📞 Контакт поддержки", callback_data="admin_support_contact_menu")],
        [InlineKeyboardButton(text="📇 Кнопка Контакты", callback_data="admin_contacts_text_menu")],
        [InlineKeyboardButton(text="🏘 Управление районами города", callback_data="admin_districts_menu")],
        [InlineKeyboardButton(text=" Настройка отзывов", callback_data="admin_reviews_menu")],
        [InlineKeyboardButton(text="✉️ Шаблон ручной оплаты", callback_data="admin_manual_pay_template")],
        [InlineKeyboardButton(text="🏷 Типы кладов (глобально)", callback_data="admin_stash_types_menu")],
        [InlineKeyboardButton(text="📄 Выгрузка каталога в PDF", callback_data="admin_export_catalog_pdf")],
        [InlineKeyboardButton(text="📋 Активные заказы", callback_data="admin_active_orders")],
        [InlineKeyboardButton(text="📈 Интерес к товарам (просмотры)", callback_data="admin_product_interest_stats")],
        [InlineKeyboardButton(text=toggle_text, callback_data="admin_toggle_redirect")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="📢 Рассылка", callback_data="admin_broadcast")],
        [InlineKeyboardButton(text="📩 ЛС пользователю", callback_data="admin_dm")],
        [InlineKeyboardButton(text="📜 Список забаненных", callback_data="admin_list_bans")],
        [
            InlineKeyboardButton(text="🔨 Забанить ID", callback_data="admin_ban_user"), 
            InlineKeyboardButton(text="😇 Разбанить ID", callback_data="admin_unban_user")
        ],
        [InlineKeyboardButton(text="❌ Закрыть панель", callback_data="admin_close")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def get_admin_catalog_generation_menu(all_products: list, selected_ids: set[str], optional_chance: int | None = None):
    chance_label = f"🎲 Шанс рандомных: {optional_chance}%" if isinstance(optional_chance, int) else "🎲 Шанс рандомных"
    buttons = [
        [InlineKeyboardButton(text=chance_label, callback_data="adm_cat_gen_set_optional_chance")],
        [InlineKeyboardButton(text="✅ Выбрать все", callback_data="adm_cat_gen_select_all")],
        [InlineKeyboardButton(text="↺ Сбросить группу (p1/p4)", callback_data="adm_cat_gen_reset")],
    ]

    for p in all_products:
        pid = p.get("id")
        if not pid:
            continue
        mark = "✅" if pid in selected_ids else "⬜️"
        name = p.get("name", pid)
        buttons.append([
            InlineKeyboardButton(text=f"{mark} {name}", callback_data=f"adm_cat_gen_tog_{pid}")
        ])

    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_ltc_menu(rate: float | None, updated_at: str | None):
    rate_text = "не задан"
    if isinstance(rate, (int, float)) and rate > 0:
        rate_text = f"{rate:,.2f}".replace(",", " ")
    updated_line = f"🕒 Обновлено: {updated_at}" if updated_at else "🕒 Обновлено: —"

    buttons = [
        [InlineKeyboardButton(text="✏️ Установить курс", callback_data="admin_ltc_set")],
        [InlineKeyboardButton(text="🗑 Сбросить курс", callback_data="admin_ltc_clear")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")]
    ]

    text = (
        "💎 <b>Курс Litecoin (LTC)</b>\n"
        f"Текущий курс: <b>{rate_text}</b> руб за 1 LTC\n"
        f"{updated_line}\n\n"
        "ℹ️ Введите ваш курс вручную. API не используется."
    )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_reviews_menu():
    buttons = [
        [InlineKeyboardButton(text="✍️ Добавить MANUALLY", callback_data="adm_rev_manual_start")],
        [InlineKeyboardButton(text="🛡 Модерация отзывов", callback_data="adm_rev_mod_start")],
        [InlineKeyboardButton(text="🧪 Модерация сгенерированных", callback_data="adm_rev_gen_mod_start")],
        [InlineKeyboardButton(text="📤 Выгрузить JSON (последняя волна)", callback_data="adm_rev_gen_export_json")],
        [InlineKeyboardButton(text="📥 Загрузить JSON (текст/район)", callback_data="adm_rev_gen_import_json")],
        [InlineKeyboardButton(text="🎲 Генерировать (Все Города)", callback_data="adm_rev_gen_mass")],
        [InlineKeyboardButton(text="🗑 Удалить последнюю волну", callback_data="adm_rev_delete_last")],
        [InlineKeyboardButton(text="🔄 Кастомные фразы (AI)", callback_data="admin_rev_phrases_menu")],
        [InlineKeyboardButton(text="⚙️ Настройки авторассылки ✨", callback_data="adm_rev_settings")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_catalog_menu(city_name: str):
    buttons = [
        [InlineKeyboardButton(text="🔄 Пересоздать случайно", callback_data=f"adm_cat_reset_{city_name}")],
        [InlineKeyboardButton(text="➕ Добавить товар", callback_data=f"adm_cat_add_menu_{city_name}")],
        [InlineKeyboardButton(text="✏️ Районы для товаров", callback_data=f"adm_pd_list_{city_name}")],
        [InlineKeyboardButton(text="🏷 Типы кладов", callback_data=f"adm_stash_menu_{city_name}")],
        [InlineKeyboardButton(text="➖ Удалить товар", callback_data=f"adm_cat_del_menu_{city_name}")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_products_global_menu(all_products: list):
    count = len(all_products)
    buttons = [
        [InlineKeyboardButton(text=f"➕ Добавить товар (всего {count})", callback_data="admin_products_global_add")],
        [InlineKeyboardButton(text="➖ Удалить товар", callback_data="admin_products_global_del_menu")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_products_global_delete_list(all_products: list):
    buttons = []
    for product in all_products:
        pid = product.get("id")
        name = product.get("name") or pid
        if not pid:
            continue
        buttons.append([InlineKeyboardButton(text=f"❌ {name}", callback_data=f"adm_glob_prod_del_{pid}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_products_global_menu")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_add_product_list(city_name: str, existing_ids: list, all_products: list):
    buttons = []
    # Button to create new custom product
    buttons.append([InlineKeyboardButton(text="✨ Создать новый товар (Custom)", callback_data=f"adm_cat_new_{city_name}")])
    
    for p in all_products:
        if p['id'] not in existing_ids:
            buttons.append([InlineKeyboardButton(text=f"➕ {p['name']}", callback_data=f"adm_cat_add_{city_name}_{p['id']}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_del_product_list(city_name: str, current_products: list):
    buttons = []
    for p in current_products:
        buttons.append([InlineKeyboardButton(text=f"❌ {p['name']}", callback_data=f"adm_cat_del_{city_name}_{p['id']}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_stash_product_list(city_name: str, products: list):
    buttons = []
    for p in products:
        buttons.append([InlineKeyboardButton(text=f"🏷 {p['name']}", callback_data=f"adm_stash_edit_{city_name}&{p['id']}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_cat_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_stash_editor(city_name: str, product_id: str, all_types: list[str], selected: list[str]):
    buttons = [[InlineKeyboardButton(text="💾 Сохранить", callback_data=f"adm_stash_save_{city_name}&{product_id}")]]
    for idx, item in enumerate(all_types):
        mark = "✅" if item in selected else "⬜️"
        buttons.append([InlineKeyboardButton(text=f"{mark} {item}", callback_data=f"adm_stash_toggle_{idx}")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_stash_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_global_stash_menu(stash_types: list[str]):
    buttons = []
    for idx, item in enumerate(stash_types):
        buttons.append([InlineKeyboardButton(text=f"❌ {item}", callback_data=f"adm_global_stash_del_{idx}")])
    buttons.append([InlineKeyboardButton(text="➕ Добавить тип", callback_data="adm_global_stash_add")])
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_districts_menu(city_name: str, districts: list):
    buttons = [
        [InlineKeyboardButton(text="➕ Добавить район", callback_data=f"adm_dist_add_{city_name}")],
        [InlineKeyboardButton(text="➖ Удалить район", callback_data=f"adm_dist_del_menu_{city_name}")],
        [InlineKeyboardButton(text="◀️ Назад в админку", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_del_district_list(city_name: str, districts: list):
    buttons = []
    for d in districts:
        # Use callback prefix adm_dist_del_do_CITY_DISTRICT
        # If district has spaces, it might break simple splitting. 
        # But we can reconstruct city name if we know parts logic.
        # Or even better, use an index if we stored index, but we don't.
        # Let's hope city_name doesn't contain crazy chars.
        # We will handle splitting carefully in handler.
        buttons.append([InlineKeyboardButton(text=f"❌ {d}", callback_data=f"adm_dist_del_do_{city_name}&{d}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_dist_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_product_districts_selector(city_name: str, product_id: str, all_districts: list, selected_districts: list):
    buttons = []
    # Button to Save
    buttons.append([InlineKeyboardButton(text="💾 СОХРАНИТЬ", callback_data=f"adm_pd_save_{city_name}&{product_id}")])
    
    # Toggle "Select All" / "Clear All" logic is hard in pure inline, usually just per item
    
    for idx, d in enumerate(all_districts):
        is_sel = d in selected_districts
        icon = "✅" if is_sel else "⬜️"
        # Toggle callback optimized: adm_pd_tog_{INDEX}
        # We rely on FSM to map index back to district
        cb = f"adm_pd_tog_{idx}"
        buttons.append([InlineKeyboardButton(text=f"{icon} {d}", callback_data=cb)])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"adm_pd_back_{city_name}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_reviews_main_menu():
    buttons = [
        [InlineKeyboardButton(text="✍️ Добавить вручную", callback_data="adm_rev_manual_start")],
        [InlineKeyboardButton(text="💣 Генерация (Тест)", callback_data="adm_rev_gen_start")],
        [InlineKeyboardButton(text="⚙️ Настройки авторассылки ✨", callback_data="adm_rev_settings")],
        [InlineKeyboardButton(text="◀️ Назад", callback_data="admin_back_main")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def get_admin_reviews_settings_menu(settings):
    # settings = {interval_hours, min_reviews, max_reviews}
    interval = settings.get('interval_hours', 12)
    min_r = settings.get('min_reviews', 5)
    max_r = settings.get('max_reviews', 10)
    
    buttons = [
        [InlineKeyboardButton(text=f"⏳ Интервал: {interval} ч.", callback_data="adm_rev_set_interval")],
        [InlineKeyboardButton(text=f"🔢 Мин. кол-во: {min_r}", callback_data="adm_rev_set_min")],
        [InlineKeyboardButton(text=f"🔢 Макс. кол-во: {max_r}", callback_data="adm_rev_set_max")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_reviews_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

