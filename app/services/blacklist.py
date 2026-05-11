import json
import os
from app.services.postgres_store import PostgresDocumentStore

BLACKLIST_FILE = "storage/blacklist.json"
BLACKLIST_DOC_KEY = "blacklist"

class BlacklistService:
    _spam_counters = {}
    _payment_abuse_counters = {} # Отдельный счетчик для отмен оплат

    @staticmethod
    def load_blacklist():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(BLACKLIST_DOC_KEY, [])
            return data if isinstance(data, list) else []
        if not os.path.exists(BLACKLIST_FILE):
            return []
        try:
            with open(BLACKLIST_FILE, 'r') as f:
                return json.load(f)
        except:
            return []

    @staticmethod
    def save_blacklist(banned_ids):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(BLACKLIST_DOC_KEY, banned_ids)
            return
        with open(BLACKLIST_FILE, 'w') as f:
            json.dump(banned_ids, f)

    @staticmethod
    def is_banned(user_id):
        banned = BlacklistService.load_blacklist()
        return user_id in banned

    @staticmethod
    def ban_user(user_id):
        banned = BlacklistService.load_blacklist()
        if user_id not in banned:
            banned.append(user_id)
            BlacklistService.save_blacklist(banned)
            # Очищаем счетчики
            if user_id in BlacklistService._spam_counters:
                del BlacklistService._spam_counters[user_id]
            if user_id in BlacklistService._payment_abuse_counters:
                del BlacklistService._payment_abuse_counters[user_id]
            return True
        return False

    @staticmethod
    def unban_user(user_id):
        banned = BlacklistService.load_blacklist()
        if user_id in banned:
            banned.remove(user_id)
            BlacklistService.save_blacklist(banned)
            # Очищаем счетчики при разбане, чтобы дать шанс
            if user_id in BlacklistService._payment_abuse_counters:
                del BlacklistService._payment_abuse_counters[user_id]
            return True
        return False

    @staticmethod
    def report_bad_input(user_id):
        """
        Увеличивает счетчик ошибок города. Если ошибок >= 3, возвращает True (пора банить).
        """
        count = BlacklistService._spam_counters.get(user_id, 0) + 1
        BlacklistService._spam_counters[user_id] = count
        
        if count >= 5: # Увеличим порог для городов, т.к. люди могут просто ошибаться
            BlacklistService.ban_user(user_id)
            return True
        return False

    @staticmethod
    def report_payment_abuse(user_id):
        """
        Увеличивает счетчик злоупотребления отменой оплаты.
        Возвращает кортеж: (нужен_ли_бан, текст_предупреждения или None)
        """
        count = BlacklistService._payment_abuse_counters.get(user_id, 0) + 1
        BlacklistService._payment_abuse_counters[user_id] = count
        
        # Логика:
        # 1 раз - просто счетчик
        # 2 раза - последнее предупреждение
        # 3 раза - бан
        
        if count == 2:
            return False, "⚠️ <b>Предупреждение!</b>\nВы часто отменяете процесс оплаты.\nЕсли вы отмените оплату еще раз, <b>вы будете заблокированы</b> системой автоматически."
            
        if count >= 3:
            BlacklistService.ban_user(user_id)
            return True, "🚫 <b>Вы заблокированы.</b>\nСистема обнаружила подозрительную активность (злоупотребление запросом реквизитов)."
            
        return False, None
