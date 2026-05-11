from datetime import datetime, timedelta, timezone

def get_now_msk() -> datetime:
    """
    Возвращает текущее время МСК (UTC+3) как naive datetime (без информации о таймзоне),
    чтобы сохранить совместимость с текущим кодом (сравнение с strptime).
    """
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3)
