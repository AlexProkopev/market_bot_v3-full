from __future__ import annotations

import os
from copy import deepcopy

from psycopg import connect
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.config import DATABASE_URL


class PostgresDocumentStore:
    TABLE_NAME = "app_documents"
    _initialized = False

    @staticmethod
    def is_enabled() -> bool:
        if os.getenv("PYTEST_CURRENT_TEST") and os.getenv("ALLOW_TEST_DATABASE") != "1":
            return False
        return bool(DATABASE_URL)

    @classmethod
    def _connect(cls):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is not configured")
        return connect(DATABASE_URL, autocommit=True)

    @classmethod
    def initialize(cls) -> None:
        if cls._initialized or not cls.is_enabled():
            return

        with cls._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {cls.TABLE_NAME} (
                        key TEXT PRIMARY KEY,
                        payload JSONB NOT NULL,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
        cls._initialized = True

    @classmethod
    def get_document(cls, key: str, default):
        if not cls.is_enabled():
            return deepcopy(default)

        cls.initialize()
        with cls._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"SELECT payload FROM {cls.TABLE_NAME} WHERE key = %s",
                    (key,),
                )
                row = cur.fetchone()

        if not row:
            return deepcopy(default)
        return row.get("payload", deepcopy(default))

    @classmethod
    def set_document(cls, key: str, payload) -> None:
        if not cls.is_enabled():
            return

        cls.initialize()
        with cls._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""
                    INSERT INTO {cls.TABLE_NAME} (key, payload, updated_at)
                    VALUES (%s, %s, NOW())
                    ON CONFLICT (key)
                    DO UPDATE SET payload = EXCLUDED.payload, updated_at = NOW()
                    """,
                    (key, Jsonb(payload)),
                )
