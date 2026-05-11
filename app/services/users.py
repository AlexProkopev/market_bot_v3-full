import json
import os
from datetime import datetime, timedelta
from app.utils import get_now_msk
from app.services.postgres_store import PostgresDocumentStore

USERS_FILE = "storage/users_db.json"
USERS_DOC_KEY = "users_db"

class UserService:
    @staticmethod
    def _load_db():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(USERS_DOC_KEY, {"users": {}})
            if not isinstance(data, dict):
                return {"users": {}}
            if not isinstance(data.get("users"), dict):
                data["users"] = {}
            return data
        if not os.path.exists(USERS_FILE):
            return {"users": {}}
        try:
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {"users": {}}

    @staticmethod
    def _save_db(data):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(USERS_DOC_KEY, data)
            return
        try:
            with open(USERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving users db: {e}")

    @staticmethod
    def add_user(user_id: int, username: str, referrer_id: str = None):
        data = UserService._load_db()
        str_id = str(user_id)
        
        # Если юзера нет или он был неактивен, обновляем
        if str_id not in data["users"]:
            new_user = {
                "active": True,
                "username": username,
                "joined": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
                "purchases_count": 0,
                "discount_percent": 0,
                "balance": 0.0,
                "referral_count": 0,
                "referrer_id": referrer_id if referrer_id and referrer_id != str_id else None
            }
            
            # Если есть реферер, начисляем ему приглашение (простая логика)
            if referrer_id and referrer_id in data["users"] and referrer_id != str_id:
                 ref_user = data["users"][referrer_id]
                 ref_user["referral_count"] = ref_user.get("referral_count", 0) + 1
            
            data["users"][str_id] = new_user
            UserService._save_db(data)
            return True # Новый юзер
        else:
            # Обновляем имя пользователя, если изменилось
            user = data["users"][str_id]
            changed = False
            if user.get("username") != username:
                user["username"] = username
                changed = True
            
            # Если юзер вернулся (был active: False)
            if not user.get("active", True):
                user["active"] = True
                changed = True
                
            # Миграция схемы (если старый юзер без новых полей)
            if "purchases_count" not in user:
                user["purchases_count"] = 0
                changed = True
            if "discount_percent" not in user:
                user["discount_percent"] = 0
                changed = True
            if "balance" not in user:
                user["balance"] = 0.0
                changed = True
            if "referral_count" not in user:
                user["referral_count"] = 0
                changed = True
            if "manual_payment_block_until" not in user:
                user["manual_payment_block_until"] = None
                changed = True
            if "rf_card_block_until" not in user:
                user["rf_card_block_until"] = None
                changed = True
            if "rf_card_failures" not in user:
                user["rf_card_failures"] = []
                changed = True
            if "rf_card_cancel_streak" not in user:
                user["rf_card_cancel_streak"] = 0
                changed = True
            if "last_rf_card" not in user:
                user["last_rf_card"] = None
                changed = True
                
            if changed:
                UserService._save_db(data)

    @staticmethod
    def get_balance(user_id: int) -> float:
        data = UserService._load_db()
        return data["users"].get(str(user_id), {}).get("balance", 0.0)

    @staticmethod
    def set_balance(user_id: int, amount: float):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["balance"] = float(amount)
            UserService._save_db(data)
            return True
        return False

    @staticmethod
    def add_balance(user_id: int, amount: float):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            current = data["users"][str_id].get("balance", 0.0)
            data["users"][str_id]["balance"] = current + float(amount)
            UserService._save_db(data)
            return True
        return False

    @staticmethod
    def deduct_balance(user_id: int, amount: float):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            current = data["users"][str_id].get("balance", 0.0)
            if current >= amount:
                data["users"][str_id]["balance"] = current - amount
                UserService._save_db(data)
                return True
        return False
            

    @staticmethod
    def get_user(user_id: int):
        data = UserService._load_db()
        return data["users"].get(str(user_id))

    @staticmethod
    def increment_purchase(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["purchases_count"] = data["users"][str_id].get("purchases_count", 0) + 1
            UserService._save_db(data)

    @staticmethod
    def set_silent_review_mode(user_id: int, mode: bool):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["silent_review_mode"] = mode
            UserService._save_db(data)
            
    @staticmethod
    def get_silent_review_mode(user_id: int) -> bool:
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
             return data["users"][str_id].get("silent_review_mode", False)
        return False

    @staticmethod
    def set_active_order(user_id: int, order_data: dict):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["active_order"] = order_data
            UserService._save_db(data)

    @staticmethod
    def get_active_order(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            return data["users"][str_id].get("active_order")
        return None

    @staticmethod
    def get_all_active_orders():
        data = UserService._load_db()
        orders = []
        for uid, udata in data.get("users", {}).items():
            if "active_order" in udata and udata["active_order"]:
                orders.append({
                    "user_id": uid,
                    "username": udata.get("username"),
                    "order": udata["active_order"]
                })
        return orders

    @staticmethod
    def remove_active_order(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"] and "active_order" in data["users"][str_id]:
            del data["users"][str_id]["active_order"]
            UserService._save_db(data)

    @staticmethod
    def increment_cancel_count(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            curr = data["users"][str_id].get("cancel_count", 0)
            data["users"][str_id]["cancel_count"] = curr + 1
            UserService._save_db(data)
            return curr + 1
        return 0

    @staticmethod
    def set_ban_until(user_id: int, until_dt: str):
        """until_dt format: YYYY-MM-DD HH:MM:SS"""
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["banned_until"] = until_dt
            # Сбросим счетчик канселов
            data["users"][str_id]["cancel_count"] = 0
            UserService._save_db(data)

    @staticmethod
    def is_temp_banned(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            ban_until = data["users"][str_id].get("banned_until")
            if ban_until:
                try:
                    dt = datetime.strptime(ban_until, "%Y-%m-%d %H:%M:%S")
                    if dt > get_now_msk():
                        return True, ban_until
                    else:
                        # Expired, clear it
                        del data["users"][str_id]["banned_until"]
                        UserService._save_db(data)
                except:
                    pass
        return False, None


    @staticmethod
    def set_active(user_id: int, is_active: bool):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["active"] = is_active
            UserService._save_db(data)

    @staticmethod
    def unban_user(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        changed = False
        if str_id in data["users"]:
            if "banned_until" in data["users"][str_id]:
                del data["users"][str_id]["banned_until"]
                changed = True
            if "cancel_count" in data["users"][str_id]:
                data["users"][str_id]["cancel_count"] = 0
                changed = True
        
        if changed:
            UserService._save_db(data)
            return True
        return False

    @staticmethod
    def set_manual_payment_block_until(user_id: int, until_dt: str):
        """until_dt format: YYYY-MM-DD HH:MM:SS"""
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["manual_payment_block_until"] = until_dt
            UserService._save_db(data)

    @staticmethod
    def is_manual_payment_blocked(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            block_until = data["users"][str_id].get("manual_payment_block_until")
            if block_until:
                try:
                    dt = datetime.strptime(block_until, "%Y-%m-%d %H:%M:%S")
                    if dt > get_now_msk():
                        return True, block_until
                    else:
                        data["users"][str_id]["manual_payment_block_until"] = None
                        UserService._save_db(data)
                except Exception:
                    pass
        return False, None

    @staticmethod
    def unblock_manual_payment(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            if data["users"][str_id].get("manual_payment_block_until"):
                data["users"][str_id]["manual_payment_block_until"] = None
                UserService._save_db(data)
                return True
        return False

    @staticmethod
    def set_rf_card_block_until(user_id: int, until_dt: str):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["rf_card_block_until"] = until_dt
            data["users"][str_id]["rf_card_cancel_streak"] = 0
            UserService._save_db(data)

    @staticmethod
    def is_rf_card_blocked(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            block_until = data["users"][str_id].get("rf_card_block_until")
            if block_until:
                try:
                    dt = datetime.strptime(block_until, "%Y-%m-%d %H:%M:%S")
                    if dt > get_now_msk():
                        return True, block_until
                    data["users"][str_id]["rf_card_block_until"] = None
                    UserService._save_db(data)
                except Exception:
                    pass
        return False, None

    @staticmethod
    def record_rf_card_failure(user_id: int) -> int:
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id not in data["users"]:
            return 0

        now = get_now_msk()
        cutoff = now - timedelta(hours=24)
        stored = data["users"][str_id].get("rf_card_failures", [])
        cleaned = []
        for item in stored or []:
            try:
                dt = datetime.strptime(item, "%Y-%m-%d %H:%M:%S")
            except Exception:
                continue
            if dt >= cutoff:
                cleaned.append(dt.strftime("%Y-%m-%d %H:%M:%S"))

        cleaned.append(now.strftime("%Y-%m-%d %H:%M:%S"))
        data["users"][str_id]["rf_card_failures"] = cleaned
        UserService._save_db(data)
        return len(cleaned)

    @staticmethod
    def increment_rf_card_cancel_streak(user_id: int) -> int:
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id not in data["users"]:
            return 0

        current = int(data["users"][str_id].get("rf_card_cancel_streak", 0) or 0)
        current += 1
        data["users"][str_id]["rf_card_cancel_streak"] = current
        UserService._save_db(data)
        return current

    @staticmethod
    def reset_rf_card_cancel_streak(user_id: int):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["rf_card_cancel_streak"] = 0
            UserService._save_db(data)

    @staticmethod
    def get_last_rf_card(user_id: int) -> str | None:
        data = UserService._load_db()
        return data.get("users", {}).get(str(user_id), {}).get("last_rf_card")

    @staticmethod
    def set_last_rf_card(user_id: int, card_number: str | None):
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            data["users"][str_id]["last_rf_card"] = card_number
            UserService._save_db(data)

    @staticmethod
    def unblock_rf_card_payment(user_id: int) -> bool:
        data = UserService._load_db()
        str_id = str(user_id)
        if str_id in data["users"]:
            if data["users"][str_id].get("rf_card_block_until"):
                data["users"][str_id]["rf_card_block_until"] = None
                data["users"][str_id]["rf_card_cancel_streak"] = 0
                UserService._save_db(data)
                return True
        return False

    @staticmethod
    def get_temp_banned_users():
        data = UserService._load_db()
        result = []
        now = get_now_msk()
        for uid, udata in data["users"].items():
            ban_until = udata.get("banned_until")
            if ban_until:
                try:
                    dt = datetime.strptime(ban_until, "%Y-%m-%d %H:%M:%S")
                    if dt > now:
                        result.append({
                            "id": uid, 
                            "username": udata.get("username"),
                            "until": ban_until
                        })
                except:
                    pass
        return result

    @staticmethod
    def get_stats():
        data = UserService._load_db()
        users = data.get("users", {})
        total = len(users)
        active = sum(1 for u in users.values() if u.get("active", True))
        blocked = total - active
        return {
            "total": total,
            "active": active,
            "blocked": blocked
        }

    @staticmethod
    def get_user_id_by_username(username: str):
        """Поиск ID пользователя по никнейму (без @)"""
        clean_username = username.lstrip("@").lower()
        data = UserService._load_db()
        
        for uid, info in data.get("users", {}).items():
            stored_username = info.get("username", "")
            if stored_username and stored_username.lower() == clean_username:
                return int(uid)
        return None

    @staticmethod
    def get_username(user_id: int):
        data = UserService._load_db()
        user = data.get("users", {}).get(str(user_id))
        if user:
            return user.get("username")
        return None

    @staticmethod
    def get_all_users():
        data = UserService._load_db()
        return [int(uid) for uid in data.get("users", {}).keys()]
