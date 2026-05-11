import argparse
import json
import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.reviews import ReviewsService


def main():
    parser = argparse.ArgumentParser(description="Generate preview reviews")
    parser.add_argument("count", nargs="?", type=int, default=40)
    parser.add_argument("--print", dest="dump", action="store_true")
    args = parser.parse_args()

    target = max(1, min(args.count, 200))

    now = datetime.now()
    offsets = sorted(random.randint(0, 24 * 60) for _ in range(target))

    reviews = []
    for offset in offsets:
        ts = now - timedelta(minutes=offset)
        review = ReviewsService._create_review_dict(ts, recent_reviews=reviews)
        if not review:
            break
        reviews.append({
            "user": review["user"],
            "city": review["city"],
            "item_info": review["item_info"],
            "text": review["text"],
            "display_date": review["display_date"]
        })

    output_path = Path("tmp/preview_reviews.json")
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(reviews, fh, ensure_ascii=False, indent=2)

    if args.dump:
        print(json.dumps(reviews, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
