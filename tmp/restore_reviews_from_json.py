import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCE = Path(__file__).resolve().with_name("reviews_recovery_seed.json")
HIDDEN_USERNAME = "Анонимный пират"
REVIEWS_DOC_KEY = "reviews"
LAST_BATCH_DOC_KEY = "reviews_last_batch"

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.postgres_store import PostgresDocumentStore


def main() -> int:
    source = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else DEFAULT_SOURCE
    if not source.exists():
        print(f"file not found: {source}")
        return 1

    payload = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        print("payload must be a JSON array")
        return 1

    cleaned = []
    for row in payload:
        if not isinstance(row, dict):
            continue
        row.setdefault("id", row.get("iso_date") or "")
        row.setdefault("user", HIDDEN_USERNAME)
        row.setdefault("base_name", HIDDEN_USERNAME)
        row.setdefault("hidden", False)
        row.setdefault("stars", 5)
        cleaned.append(row)

    iso_dates = [row.get("iso_date") for row in cleaned if row.get("iso_date")]
    last_batch_payload = {
        "iso_dates": iso_dates,
        "saved_at": __import__("datetime").datetime.now().isoformat(),
    }

    if PostgresDocumentStore.is_enabled():
        PostgresDocumentStore.set_document(REVIEWS_DOC_KEY, cleaned)
        PostgresDocumentStore.set_document(LAST_BATCH_DOC_KEY, last_batch_payload)
        print(f"restored_to_postgres={len(cleaned)}")
        return 0

    storage_dir = PROJECT_ROOT / "storage"
    storage_dir.mkdir(parents=True, exist_ok=True)
    (storage_dir / "reviews.json").write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    (storage_dir / "reviews_last_batch.json").write_text(
        json.dumps(last_batch_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"restored_to_file={len(cleaned)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())