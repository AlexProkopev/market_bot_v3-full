import json
import os
from datetime import datetime, timedelta
from app.utils import get_now_msk
from app.services.postgres_store import PostgresDocumentStore

USERS_FILE = "storage/users_db.json"
USERS_DOC_KEY = "users_db"
ACTIVE_ORDERS_FILE = "storage/active_orders.json"
ACTIVE_ORDERS_DOC_KEY = "active_orders"

class UserService:
    @staticmethod
    def _build_default_user(username: str | None = None, referrer_id: str | None = None) -> dict:
        return {
            "active": True,
            "username": username,
            "joined": get_now_msk().strftime("%Y-%m-%d %H:%M:%S"),
            "purchases_count": 0,
            "discount_percent": 0,
            "balance": 0.0,
            "referral_count": 0,
            "referrer_id": referrer_id,
            "cancel_count": 0,
            "banned_until": None,
            "manual_payment_block_until": None,
            "rf_card_block_until": None,
            "rf_card_failures": [],
            "rf_card_cancel_streak": 0,
            "last_rf_card": None,
        }

    @staticmethod
    def _ensure_user_record(data: dict, user_id: int, username: str | None = None) -> dict:
        users = data.setdefault("users", {})
        str_id = str(user_id)
        user = users.get(str_id)
        if not isinstance(user, dict):
            user = UserService._build_default_user(username=username)
            users[str_id] = user
            return user

        defaults = UserService._build_default_user(username=user.get("username"))
        for key, value in defaults.items():
            if key not in user:
                user[key] = value

        if username is not None and user.get("username") != username:
            user["username"] = username
        return user

    @staticmethod
    def _is_legacy_card_rf_order(order_data: dict | None) -> bool:
        return bool(
            isinstance(order_data, dict)
            and order_data.get("type") == "card_rf"
            and not order_data.get("payment_request_id")
        )

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
    def _load_active_orders_db():
        if PostgresDocumentStore.is_enabled():
            data = PostgresDocumentStore.get_document(ACTIVE_ORDERS_DOC_KEY, {"orders": {}})
            if not isinstance(data, dict):
                return {"orders": {}}
            if not isinstance(data.get("orders"), dict):
                data["orders"] = {}
            return data
        if not os.path.exists(ACTIVE_ORDERS_FILE):
            return {"orders": {}}
        try:
            with open(ACTIVE_ORDERS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if not isinstance(data, dict):
                    return {"orders": {}}
                if not isinstance(data.get("orders"), dict):
                    data["orders"] = {}
                return data
        except:
            return {"orders": {}}

    @staticmethod
    def _save_active_orders_db(data):
        if PostgresDocumentStore.is_enabled():
            PostgresDocumentStore.set_document(ACTIVE_ORDERS_DOC_KEY, data)
            return
        try:
            with open(ACTIVE_ORDERS_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Error saving active orders db: {e}")

    @staticmethod
    def _migrate_legacy_active_orders():
        users_data = UserService._load_db()
        if not isinstance(users_data, dict):
            return

        users = users_data.get("users")
        if not isinstance(users, dict):
            return

        active_orders_data = UserService._load_active_orders_db()
        orders = active_orders_data.get("orders", {})
        changed = False

        for user_id, user_data in users.items():
            if not isinstance(user_data, dict):
                continue
            legacy_order = user_data.pop("active_order", None)
            if legacy_order and user_id not in orders:
                orders[user_id] = legacy_order
                changed = True
            elif legacy_order is not None:
                changed = True

        if changed:
            active_orders_data["orders"] = orders
            UserService._save_db(users_data)
            UserService._save_active_orders_db(active_orders_data)

    @staticmethod
    def add_user(user_id: int, username: str, referrer_id: str = None):
        data = UserService._load_db()
        str_id = str(user_id)
        
        # Если юзера нет или он был неактивен, обновляем
        if str_id not in data["users"]:
            new_user = UserService._build_default_user(
                username=username,
                referrer_id=referrer_id if referrer_id and referrer_id != str_id else None,
            )
            
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
        str_id = str(user_id)
        active_orders_data = UserService._load_active_orders_db()
        active_orders_data["orders"][str_id] = order_data
        UserService._save_active_orders_db(active_orders_data)

    @staticmethod
    def get_active_order(user_id: int):
        str_id = str(user_id)
        UserService._migrate_legacy_active_orders()
        active_orders_data = UserService._load_active_orders_db()
        order_data = active_orders_data.get("orders", {}).get(str_id)
        if UserService._is_legacy_card_rf_order(order_data):
            del active_orders_data["orders"][str_id]
            UserService._save_active_orders_db(active_orders_data)
            return None
        return order_data

    @staticmethod
    def get_all_active_orders():
        UserService._migrate_legacy_active_orders()
        active_orders_data = UserService._load_active_orders_db()
        orders = []
        for uid, order_data in list(active_orders_data.get("orders", {}).items()):
            if UserService._is_legacy_card_rf_order(order_data):
                del active_orders_data["orders"][uid]
                continue
            user = UserService.get_user(int(uid))
            orders.append({
                "user_id": uid,
                "username": user.get("username") if user else None,
                "order": order_data
            })
        if len(active_orders_data.get("orders", {})) != len(orders):
            UserService._save_active_orders_db(active_orders_data)
        return orders

    @staticmethod
    def remove_active_order(user_id: int):
        str_id = str(user_id)
        UserService._migrate_legacy_active_orders()
        active_orders_data = UserService._load_active_orders_db()
        if str_id in active_orders_data.get("orders", {}):
            del active_orders_data["orders"][str_id]
            UserService._save_active_orders_db(active_orders_data)

    @staticmethod
    def increment_cancel_count(user_id: int):
        data = UserService._load_db()
        user = UserService._ensure_user_record(data, user_id)
        curr = int(user.get("cancel_count", 0) or 0)
        user["cancel_count"] = curr + 1
        UserService._save_db(data)
        return curr + 1

    @staticmethod
    def set_ban_until(user_id: int, until_dt: str):
        """until_dt format: YYYY-MM-DD HH:MM:SS"""
        data = UserService._load_db()
        user = UserService._ensure_user_record(data, user_id)
        user["banned_until"] = until_dt
        # Сбросим счетчик канселов
        user["cancel_count"] = 0
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
        user = UserService._ensure_user_record(data, user_id)
        user["rf_card_block_until"] = until_dt
        user["rf_card_cancel_streak"] = 0
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
        UserService._ensure_user_record(data, user_id)

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
        UserService._ensure_user_record(data, user_id)

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
        user = UserService._ensure_user_record(data, user_id)
        user["last_rf_card"] = card_number
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

    @staticmethod
    def clear_users_db(preserve_user_ids: list[int] | None = None) -> dict:
        data = UserService._load_db()
        users = data.get("users", {})
        preserve_set = {str(uid) for uid in (preserve_user_ids or [])}

        kept: dict[str, dict] = {}
        for uid in preserve_set:
            user_data = users.get(uid)
            if isinstance(user_data, dict):
                kept[uid] = user_data

        removed_count = max(0, len(users) - len(kept))
        data["users"] = kept
        UserService._save_db(data)
        return {
            "removed": removed_count,
            "kept": len(kept),
        }
