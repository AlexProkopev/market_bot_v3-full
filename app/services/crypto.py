import logging
from typing import Optional

from app.config import LTC_WALLET_ADDRESS
from app.services.settings import SettingsService

logger = logging.getLogger("app.services.crypto")


class CryptoService:
    """Utility helpers for LTC payments."""

    @classmethod
    def get_ltc_wallet(cls) -> str:
        """Return configured LTC wallet address."""
        return LTC_WALLET_ADDRESS

    @classmethod
    async def get_ltc_rub_rate(cls) -> Optional[float]:
        """Return LTC to RUB rate configured by admin."""
        rate, _ = SettingsService.get_ltc_rate()
        if not rate or rate <= 0:
            logger.warning("LTC/RUB rate is not configured by admin")
            return None
        return float(rate)

    @classmethod
    def format_ltc_amount(cls, rub_amount: float, rate: float) -> float:
        """Convert RUB to LTC using provided rate."""
        if rate <= 0:
            raise ValueError("Rate must be positive")
        return rub_amount / rate
