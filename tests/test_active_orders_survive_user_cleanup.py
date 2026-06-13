import json

import app.services.users as users_module
from app.services.users import UserService


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def test_active_order_survives_user_cleanup(tmp_path, monkeypatch):
    users_file = tmp_path / "users_db.json"
    active_orders_file = tmp_path / "active_orders.json"

    monkeypatch.setattr(users_module, "USERS_FILE", str(users_file))
    monkeypatch.setattr(users_module, "ACTIVE_ORDERS_FILE", str(active_orders_file))

    order_data = {
        "type": "card_rf",
        "payment_request_id": "req-123",
        "message_id": 42,
        "price": 1500,
    }

    UserService.add_user(1001, "tester")
    UserService.set_active_order(1001, order_data)

    users_data = _read_json(users_file)
    del users_data["users"]["1001"]
    _write_json(users_file, users_data)

    assert UserService.get_active_order(1001) == order_data

    active_orders = UserService.get_all_active_orders()
    assert len(active_orders) == 1
    assert active_orders[0]["user_id"] == "1001"
    assert active_orders[0]["order"] == order_data

    UserService.remove_active_order(1001)
    assert UserService.get_active_order(1001) is None
    assert _read_json(active_orders_file)["orders"] == {}


def test_legacy_card_rf_order_without_request_id_is_cleared(tmp_path, monkeypatch):
    users_file = tmp_path / "users_db.json"
    active_orders_file = tmp_path / "active_orders.json"

    monkeypatch.setattr(users_module, "USERS_FILE", str(users_file))
    monkeypatch.setattr(users_module, "ACTIVE_ORDERS_FILE", str(active_orders_file))

    _write_json(users_file, {"users": {"1002": {"active": True, "username": "old", "balance": 0}}})
    _write_json(
        active_orders_file,
        {
            "orders": {
                "1002": {
                    "type": "card_rf",
                    "product": "Item",
                    "price": 1500,
                    "message_id": 99,
                    "timestamp": "2026-06-12 10:00:00",
                }
            }
        },
    )

    assert UserService.get_active_order(1002) is None
    assert _read_json(active_orders_file)["orders"] == {}


def test_increment_cancel_count_recreates_missing_user(tmp_path, monkeypatch):
    users_file = tmp_path / "users_db.json"
    active_orders_file = tmp_path / "active_orders.json"

    monkeypatch.setattr(users_module, "USERS_FILE", str(users_file))
    monkeypatch.setattr(users_module, "ACTIVE_ORDERS_FILE", str(active_orders_file))

    UserService.add_user(2001, "cancel_tester")
    users_data = _read_json(users_file)
    del users_data["users"]["2001"]
    _write_json(users_file, users_data)

    current = UserService.increment_cancel_count(2001)

    assert current == 1
    restored = UserService.get_user(2001)
    assert restored is not None
    assert restored.get("cancel_count") == 1


def test_clear_users_db_keeps_admin_and_active_orders(tmp_path, monkeypatch):
    users_file = tmp_path / "users_db.json"
    active_orders_file = tmp_path / "active_orders.json"

    monkeypatch.setattr(users_module, "USERS_FILE", str(users_file))
    monkeypatch.setattr(users_module, "ACTIVE_ORDERS_FILE", str(active_orders_file))

    UserService.add_user(1, "admin")
    UserService.add_user(2, "user2")
    UserService.add_user(3, "user3")
    UserService.set_active_order(2, {"type": "card_rf", "payment_request_id": "req-2"})

    result = UserService.clear_users_db(preserve_user_ids=[1])

    assert result == {"removed": 2, "kept": 1}
    users_data = _read_json(users_file)
    assert set(users_data["users"].keys()) == {"1"}

    active_orders_data = _read_json(active_orders_file)
    assert active_orders_data["orders"].get("2", {}).get("payment_request_id") == "req-2"