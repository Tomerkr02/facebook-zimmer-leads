import hashlib
import sqlite3
from pathlib import Path


class LeadStorage:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS seen_posts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    post_key TEXT NOT NULL UNIQUE,
                    post_url TEXT,
                    text_hash TEXT NOT NULL,
                    author_name TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_seen_posts_text_hash
                ON seen_posts(text_hash)
                """
            )

    @staticmethod
    def build_text_hash(text: str) -> str:
        return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()

    def has_seen(self, post_key: str | None, post_url: str | None, text: str) -> bool:
        text_hash = self.build_text_hash(text)
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1
                FROM seen_posts
                WHERE post_key = ?
                   OR (post_url IS NOT NULL AND post_url = ?)
                   OR text_hash = ?
                LIMIT 1
                """,
                (post_key or "", post_url, text_hash),
            ).fetchone()
        return row is not None

    def mark_seen(
        self,
        post_key: str,
        post_url: str | None,
        text: str,
        author_name: str | None,
    ) -> None:
        text_hash = self.build_text_hash(text)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO seen_posts (post_key, post_url, text_hash, author_name)
                VALUES (?, ?, ?, ?)
                """,
                (post_key, post_url, text_hash, author_name),
            )
