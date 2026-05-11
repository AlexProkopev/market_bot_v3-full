import unittest
import os
import tempfile

from app.services.reviews import ReviewsService


def _collect_samples(count: int = 200) -> list[str]:
    return [
        ReviewsService._generate_local_review_text(
            "Мёд 0.5г",
            "Кемерово",
            "Октябрьский",
            None,
        )
        for _ in range(count)
    ]


class LocalReviewVariationTests(unittest.TestCase):
    def test_unique_ratio_above_threshold(self) -> None:
        samples = _collect_samples()
        unique_ratio = len(set(samples)) / len(samples)
        self.assertGreater(unique_ratio, 0.6)

    def test_majority_are_ultra_short(self) -> None:
        samples = _collect_samples()
        short_ratio = sum(len(text.split()) <= 4 for text in samples) / len(samples)
        self.assertGreater(short_ratio, 0.8)

    def test_average_word_count_stays_low(self) -> None:
        samples = _collect_samples()
        avg_words = sum(len(text.split()) for text in samples) / len(samples)
        self.assertLessEqual(avg_words, 3.6)

    def test_no_placeholder_fragments(self) -> None:
        samples = _collect_samples()
        self.assertTrue(all("заглушка" not in text.lower() for text in samples))

    def test_filter_invalid_reviews_cleans(self) -> None:
        items = [
            {"text": "Ai-заглушка от GPT"},
            {"text": " "},
            {"text": None},
            {"text": "Живой отзыв"},
        ]
        cleaned, removed = ReviewsService._filter_invalid_reviews(items, "test")
        self.assertEqual(removed, 3)
        self.assertEqual(cleaned, [{"text": "Живой отзыв"}])


class LastBatchImportTests(unittest.TestCase):
    def test_apply_last_batch_texts_updates_text_and_district(self) -> None:
        original_file_path = ReviewsService.FILE_PATH
        original_last_batch_path = ReviewsService.LAST_BATCH_FILE

        with tempfile.TemporaryDirectory() as temp_dir:
            try:
                ReviewsService.FILE_PATH = os.path.join(temp_dir, "reviews.json")
                ReviewsService.LAST_BATCH_FILE = os.path.join(temp_dir, "reviews_last_batch.json")

                review = {
                    "id": "rev-1",
                    "iso_date": "2026-04-05T12:00:00",
                    "text": "Старый текст",
                    "city": "Солнечногорск",
                    "item_info": "Мёд 1г | Солнечногорск, Старый район",
                    "hidden": False,
                }

                ReviewsService._save_reviews([review])
                ReviewsService._save_last_batch([review["iso_date"]])

                result = ReviewsService.apply_last_batch_texts({
                    "reviews": [
                        {
                            "id": "rev-1",
                            "text": "Новый текст",
                            "district": "Новый район",
                            "item_info": "Мёд 1г | Солнечногорск, Старый район",
                        }
                    ]
                })

                self.assertEqual(result["updated"], 1)
                self.assertEqual(result["invalid"], 0)

                saved = ReviewsService._load_reviews()
                self.assertEqual(saved[0]["text"], "Новый текст")
                self.assertEqual(saved[0]["item_info"], "Мёд 1г | Солнечногорск, Новый район")
            finally:
                ReviewsService.FILE_PATH = original_file_path
                ReviewsService.LAST_BATCH_FILE = original_last_batch_path


if __name__ == "__main__":
    unittest.main()
