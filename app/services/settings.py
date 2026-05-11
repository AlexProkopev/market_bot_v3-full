import json
import os
import random
from datetime import datetime, timezone
from app.services.postgres_store import PostgresDocumentStore

SETTINGS_FILE = "storage/settings.json"
SETTINGS_DOC_KEY = "settings"
DEFAULT_SETTINGS = {
    "redirect_manual_to_auto": False,
    "ltc_rate_rub": None,
    "ltc_rate_updated_at": None,
    "manual_payment_template": None,
    "manual_commission": 600,
    "rf_card_commission": 300,
    "support_contact": "@egor_lavlav66",
    "contacts_text": None,
    "rf_cards": [],
    "exchange_bot_url": None,
}

class SettingsService:
    @staticmethod
    def _load_settings():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(SETTINGS_DOC_KEY, DEFAULT_SETTINGS)
            if not isinstance(data, dict):
                data = DEFAULT_SETTINGS.copy()
            for key, value in DEFAULT_SETTINGS.items():
                if key not in data:
                    data[key] = value
            return data
        if not os.path.exists("storage"):
            os.makedirs("storage", exist_ok=True)
        
        if not os.path.exists(SETTINGS_FILE):
            return DEFAULT_SETTINGS.copy()
        
        try:
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for key, value in DEFAULT_SETTINGS.items():
                    if key not in data:
                        data[key] = value
                return data
        except Exception:
            return DEFAULT_SETTINGS.copy()

    @staticmethod
    def _save_settings(settings: dict):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(SETTINGS_DOC_KEY, settings)
            return
        try:
            with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(settings, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Error saving settings: {e}")

    @staticmethod
    def is_redirect_active() -> bool:
        settings = SettingsService._load_settings()
        return settings.get("redirect_manual_to_auto", False)

    @staticmethod
    def set_redirect_active(state: bool):
        settings = SettingsService._load_settings()
        settings["redirect_manual_to_auto"] = state
        SettingsService._save_settings(settings)

    @staticmethod
    def get_ltc_rate() -> tuple[float | None, str | None]:
        settings = SettingsService._load_settings()
        return settings.get("ltc_rate_rub"), settings.get("ltc_rate_updated_at")

    @staticmethod
    def set_ltc_rate(rate: float | None):
        settings = SettingsService._load_settings()
        settings["ltc_rate_rub"] = rate
        if rate is None:
            settings["ltc_rate_updated_at"] = None
        else:
            settings["ltc_rate_updated_at"] = datetime.now(timezone.utc).isoformat()
        SettingsService._save_settings(settings)

    @staticmethod
    def get_manual_payment_template() -> str | None:
        settings = SettingsService._load_settings()
        template = settings.get("manual_payment_template")
        if isinstance(template, str) and template.strip():
            return template
        return None

    @staticmethod
    def set_manual_payment_template(template: str | None):
        settings = SettingsService._load_settings()
        cleaned = None
        if isinstance(template, str):
            cleaned = template.strip() or None
        settings["manual_payment_template"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def get_manual_commission() -> int:
        settings = SettingsService._load_settings()
        val = settings.get("manual_commission", 600)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 600

    @staticmethod
    def set_manual_commission(value: int):
        settings = SettingsService._load_settings()
        settings["manual_commission"] = int(value)
        SettingsService._save_settings(settings)

    @staticmethod
    def get_rf_card_commission() -> int:
        settings = SettingsService._load_settings()
        val = settings.get("rf_card_commission", 300)
        try:
            return int(val)
        except (TypeError, ValueError):
            return 300

    @staticmethod
    def set_rf_card_commission(value: int):
        settings = SettingsService._load_settings()
        settings["rf_card_commission"] = int(value)
        SettingsService._save_settings(settings)

    @staticmethod
    def get_support_contact() -> str:
        settings = SettingsService._load_settings()
        val = settings.get("support_contact", "@egor_lavlav66")
        return str(val).strip() if val else "@egor_lavlav66"

    @staticmethod
    def set_support_contact(contact: str):
        settings = SettingsService._load_settings()
        settings["support_contact"] = contact.strip()
        SettingsService._save_settings(settings)

    @staticmethod
    def get_contacts_text() -> str:
        settings = SettingsService._load_settings()
        value = settings.get("contacts_text")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return (
            "📞 <b>Контакты</b>\n\n"
            f"По всем вопросам: {SettingsService.get_support_contact()}"
        )

    @staticmethod
    def set_contacts_text(text: str | None):
        settings = SettingsService._load_settings()
        cleaned = None
        if isinstance(text, str):
            cleaned = text.strip() or None
        settings["contacts_text"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def get_rf_cards() -> list[str]:
        settings = SettingsService._load_settings()
        cards = settings.get("rf_cards", [])
        return [str(item).strip() for item in cards if str(item).strip()]

    @staticmethod
    def set_rf_cards(cards: list[str]):
        settings = SettingsService._load_settings()
        cleaned = [str(item).strip() for item in cards if str(item).strip()]
        settings["rf_cards"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def get_exchange_bot_url() -> str | None:
        settings = SettingsService._load_settings()
        value = settings.get("exchange_bot_url")
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    @staticmethod
    def set_exchange_bot_url(url: str | None):
        settings = SettingsService._load_settings()
        cleaned = None
        if isinstance(url, str):
            cleaned = url.strip() or None
        settings["exchange_bot_url"] = cleaned
        SettingsService._save_settings(settings)

    @staticmethod
    def get_random_rf_card() -> str | None:
        cards = SettingsService.get_rf_cards()
        if not cards:
            return None
        return cards[0] if len(cards) == 1 else cards[random.randint(0, len(cards) - 1)]
