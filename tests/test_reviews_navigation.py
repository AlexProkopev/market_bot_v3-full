import asyncio
from unittest.mock import AsyncMock, patch

from app.handlers import user_flow
from app.keyboards import get_reviews_pagination_kb
from app.states import OrderState


class DummyState:
    def __init__(self, initial_data):
        self.data = dict(initial_data)
        self.state = None

    async def get_data(self):
        return dict(self.data)

    async def update_data(self, **kwargs):
        self.data.update(kwargs)

    async def set_state(self, value):
        self.state = value


def test_reviews_keyboard_shows_back_button_for_all_city_mode():
    markup = get_reviews_pagination_kb(
        current_index=0,
        total_count=5,
        city_filter="Томск",
        back_callback="rev_back_from_all",
    )

    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = {button.callback_data for button in buttons}
    labels = {button.text for button in buttons}

    assert "rev_back_from_all" in callbacks
    assert "🔙 Назад" in labels
    assert "rev_search_reset" not in callbacks


def test_reviews_keyboard_keeps_product_actions():
    markup = get_reviews_pagination_kb(
        current_index=1,
        total_count=5,
        city_filter="Томск",
        product_id="p1",
    )

    buttons = [button for row in markup.inline_keyboard for button in row]
    callbacks = {button.callback_data for button in buttons}

    assert "prod_p1" in callbacks
    assert "rev_back_products" in callbacks
    assert "rev_back_from_all" not in callbacks


def test_restore_after_all_city_reviews_returns_to_product_reviews():
    state = DummyState(
        {
            "city": "Томск",
            "population": 120000,
            "rev_all_city_mode": True,
            "rev_return_city": "Томск",
            "rev_return_product": "Альфа 1г",
            "rev_return_product_id": "p1",
            "rev_return_index": 2,
        }
    )
    send_method = AsyncMock()

    with patch.object(user_flow, "_show_review_page", AsyncMock()) as show_review_page:
        asyncio.run(user_flow._restore_after_all_city_reviews(send_method, state))

    show_review_page.assert_awaited_once_with(send_method, 2, state)
    assert state.data["rev_search_city"] == "Томск"
    assert state.data["rev_search_product"] == "Альфа 1г"
    assert state.data["rev_search_product_id"] == "p1"
    assert state.data["rev_all_city_mode"] is False
    assert state.state is None


def test_restore_after_all_city_reviews_returns_to_city_reviews_when_no_product_context():
    state = DummyState(
        {
            "city": "Томск",
            "population": 120000,
            "rev_all_city_mode": True,
            "rev_return_city": "Томск",
            "rev_return_product": None,
            "rev_return_product_id": None,
            "rev_return_index": 0,
        }
    )
    send_method = AsyncMock()

    with patch.object(user_flow, "_show_review_page", AsyncMock()) as show_review_page, patch.object(
        user_flow,
        "_build_catalog_text",
        AsyncMock(return_value="catalog"),
    ) as build_catalog_text:
        asyncio.run(user_flow._restore_after_all_city_reviews(send_method, state))

    show_review_page.assert_awaited_once_with(send_method, 0, state)
    build_catalog_text.assert_not_called()
    assert state.data["rev_search_city"] == "Томск"
    assert state.data["rev_search_product"] is None
    assert state.data["rev_search_product_id"] is None
    assert state.data["rev_all_city_mode"] is False
    assert state.state is None


def test_restore_after_all_city_reviews_returns_to_city_reviews_without_product_context():
    state = DummyState(
        {
            "city": "Томск",
            "population": 120000,
            "rev_all_city_mode": True,
            "rev_return_city": "Томск",
            "rev_return_product": None,
            "rev_return_product_id": None,
            "rev_return_index": 3,
        }
    )
    send_method = AsyncMock()

    with patch.object(user_flow, "_show_review_page", AsyncMock()) as show_review_page, patch.object(
        user_flow,
        "_build_catalog_text",
        AsyncMock(return_value="catalog"),
    ) as build_catalog_text:
        asyncio.run(user_flow._restore_after_all_city_reviews(send_method, state))

    show_review_page.assert_awaited_once_with(send_method, 3, state)
    build_catalog_text.assert_not_called()
    assert state.data["rev_search_city"] == "Томск"
    assert state.data["rev_search_product"] is None
    assert state.data["rev_search_product_id"] is None
    assert state.data["rev_all_city_mode"] is False