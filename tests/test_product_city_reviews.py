import os
import tempfile
import unittest
from unittest.mock import patch

from app.services.reviews import ReviewsService


class ProductCityReviewsTests(unittest.TestCase):
    def test_manual_review_stores_product_and_city_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            original_reviews_file = ReviewsService.FILE_PATH
            original_active_cities_file = ReviewsService.ACTIVE_CITIES_FILE
            original_prompts_file = ReviewsService.PROMPTS_FILE
            original_last_batch_file = ReviewsService.LAST_BATCH_FILE
            original_settings_file = ReviewsService.SETTINGS_FILE
            try:
                ReviewsService.FILE_PATH = os.path.join(temp_dir, "reviews.json")
                ReviewsService.ACTIVE_CITIES_FILE = os.path.join(temp_dir, "active_cities.json")
                ReviewsService.PROMPTS_FILE = os.path.join(temp_dir, "review_prompts.json")
                ReviewsService.LAST_BATCH_FILE = os.path.join(temp_dir, "reviews_last_batch.json")
                ReviewsService.SETTINGS_FILE = os.path.join(temp_dir, "review_settings.json")

                ReviewsService.add_manual_review(
                    username="admin",
                    city="Томск",
                    product="Альфа 1г",
                    district="Советский",
                    text="Все ровно",
                )

                reviews = ReviewsService._load_reviews()
                self.assertEqual(len(reviews), 1)
                self.assertEqual(reviews[0]["item_info"], "Альфа 1г | Томск")
                self.assertEqual(reviews[0]["product"], "Альфа 1г")
            finally:
                ReviewsService.FILE_PATH = original_reviews_file
                ReviewsService.ACTIVE_CITIES_FILE = original_active_cities_file
                ReviewsService.PROMPTS_FILE = original_prompts_file
                ReviewsService.LAST_BATCH_FILE = original_last_batch_file
                ReviewsService.SETTINGS_FILE = original_settings_file

    def test_paginated_reviews_filter_by_city_and_product(self) -> None:
        reviews = [
            {
                "id": "r1",
                "user": "ИМЯ СКРЫТО",
                "text": "Первый отзыв",
                "product": "Альфа 1г",
                "city": "Томск",
                "item_info": "Альфа 1г | Томск",
                "stars": 5,
                "iso_date": "2026-05-06T10:00:00",
                "display_date": "06.05 10:00",
                "hidden": False,
            },
            {
                "id": "r2",
                "user": "ИМЯ СКРЫТО",
                "text": "Второй отзыв",
                "product": "Меф 0.5г",
                "city": "Томск",
                "item_info": "Меф 0.5г | Томск",
                "stars": 5,
                "iso_date": "2026-05-06T11:00:00",
                "display_date": "06.05 11:00",
                "hidden": False,
            },
            {
                "id": "r3",
                "user": "ИМЯ СКРЫТО",
                "text": "Третий отзыв",
                "product": "Альфа 1г",
                "city": "Омск",
                "item_info": "Альфа 1г | Омск",
                "stars": 5,
                "iso_date": "2026-05-06T12:00:00",
                "display_date": "06.05 12:00",
                "hidden": False,
            },
        ]

        with patch.object(ReviewsService, "check_and_update", return_value=reviews), patch.object(
            ReviewsService,
            "_load_reviews",
            return_value=reviews,
        ):
            review, total = ReviewsService.get_paginated_review(
                0,
                city_filter="Томск",
                product_filter="Альфа 1г",
            )

        self.assertEqual(total, 1)
        self.assertIsNotNone(review)
        self.assertEqual(review["id"], "r1")


if __name__ == "__main__":
    unittest.main()