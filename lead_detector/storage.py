import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ALLOWED_LEAD_STATUSES = {
    "new",
    "contacted",
    "waiting_reply",
    "not_relevant",
    "closed",
    "archived",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS leads (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    created_at TEXT,
                    updated_at TEXT,
                    source TEXT DEFAULT 'facebook',
                    group_name TEXT,
                    group_url TEXT,
                    author TEXT,
                    post_url TEXT,
                    post_text TEXT NOT NULL,
                    cleaned_text TEXT,
                    matched_keywords TEXT,
                    keyword_score INTEGER DEFAULT 0,
                    ai_score INTEGER,
                    ai_category TEXT,
                    ai_reason_he TEXT,
                    suggested_reply_he TEXT,
                    status TEXT DEFAULT 'new',
                    sent_to_telegram INTEGER DEFAULT 0,
                    notes TEXT,
                    text_hash TEXT UNIQUE
                )
                """
            )
            self._ensure_leads_columns(connection)
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_leads_text_hash
                ON leads(text_hash)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_leads_post_url
                ON leads(post_url)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_leads_status_created_at
                ON leads(status, created_at DESC)
                """
            )

    def _ensure_leads_columns(self, connection: sqlite3.Connection) -> None:
        expected_columns = {
            "created_at": "TEXT",
            "updated_at": "TEXT",
            "source": "TEXT DEFAULT 'facebook'",
            "group_name": "TEXT",
            "group_url": "TEXT",
            "author": "TEXT",
            "post_url": "TEXT",
            "post_text": "TEXT",
            "cleaned_text": "TEXT",
            "matched_keywords": "TEXT",
            "keyword_score": "INTEGER DEFAULT 0",
            "ai_score": "INTEGER",
            "ai_category": "TEXT",
            "ai_reason_he": "TEXT",
            "suggested_reply_he": "TEXT",
            "status": "TEXT DEFAULT 'new'",
            "sent_to_telegram": "INTEGER DEFAULT 0",
            "notes": "TEXT",
            "text_hash": "TEXT",
            "guest_type": "TEXT",
            "urgency": "TEXT",
            "requested_area": "TEXT",
            "pool_intent": "TEXT",
            "privacy_intent": "TEXT",
            "bad_fit_reasons": "TEXT",
            "fit_score": "INTEGER",
            "heat_level": "TEXT",
            "short_reason_he": "TEXT",
            "recommended_action": "TEXT",
            "suggested_first_reply_he": "TEXT",
            "suggested_followup_he": "TEXT",
            "suggested_price_question_he": "TEXT",
        }
        existing_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(leads)").fetchall()
        }
        for column_name, definition in expected_columns.items():
            if column_name not in existing_columns:
                connection.execute(f"ALTER TABLE leads ADD COLUMN {column_name} {definition}")

    @staticmethod
    def build_text_hash(text: str) -> str:
        return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()

    @staticmethod
    def serialize_keywords(keywords: list[str] | None) -> str | None:
        if not keywords:
            return None
        return json.dumps(keywords, ensure_ascii=False)

    @staticmethod
    def deserialize_keywords(raw_value: str | None) -> list[str]:
        if not raw_value:
            return []
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return [item.strip() for item in raw_value.split(",") if item.strip()]
        if isinstance(parsed, list):
            return [str(item) for item in parsed]
        return []

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

    def count_leads(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM leads").fetchone()
        return int(row["count"]) if row else 0

    def latest_leads(self, limit: int = 5) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM leads
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def list_table_names(self) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table'
                ORDER BY name
                """
            ).fetchall()
        return [str(row["name"]) for row in rows]

    @staticmethod
    def serialize_string_list(items: list[str] | None) -> str | None:
        if not items:
            return None
        return json.dumps(items, ensure_ascii=False)

    def save_lead(
        self,
        *,
        source: str = "facebook",
        group_name: str | None,
        group_url: str | None,
        author: str | None,
        post_url: str | None,
        post_text: str,
        cleaned_text: str | None,
        matched_keywords: list[str] | None,
        keyword_score: int,
        ai_score: int | None,
        ai_category: str | None,
        ai_reason_he: str | None,
        suggested_reply_he: str | None,
        guest_type: str | None = None,
        urgency: str | None = None,
        requested_area: str | None = None,
        pool_intent: str | None = None,
        privacy_intent: str | None = None,
        bad_fit_reasons: list[str] | None = None,
        fit_score: int | None = None,
        heat_level: str | None = None,
        short_reason_he: str | None = None,
        recommended_action: str | None = None,
        suggested_first_reply_he: str | None = None,
        suggested_followup_he: str | None = None,
        suggested_price_question_he: str | None = None,
        status: str = "new",
        sent_to_telegram: int = 0,
        notes: str | None = None,
    ) -> tuple[int, str]:
        if status not in ALLOWED_LEAD_STATUSES:
            raise ValueError(f"Unsupported lead status: {status}")

        now = utc_now_iso()
        text_hash = self.build_text_hash(cleaned_text or post_text)
        keywords_serialized = self.serialize_keywords(matched_keywords)
        bad_fit_serialized = self.serialize_string_list(bad_fit_reasons)

        with self._connect() as connection:
            existing = connection.execute(
                """
                SELECT id
                FROM leads
                WHERE text_hash = ?
                   OR (post_url IS NOT NULL AND post_url = ?)
                ORDER BY id DESC
                LIMIT 1
                """,
                (text_hash, post_url),
            ).fetchone()

            if existing:
                lead_id = int(existing["id"])
                connection.execute(
                    """
                    UPDATE leads
                    SET updated_at = ?,
                        source = COALESCE(?, source),
                        group_name = COALESCE(?, group_name),
                        group_url = COALESCE(?, group_url),
                        author = COALESCE(?, author),
                        post_url = COALESCE(?, post_url),
                        post_text = ?,
                        cleaned_text = COALESCE(?, cleaned_text),
                        matched_keywords = COALESCE(?, matched_keywords),
                        keyword_score = ?,
                        ai_score = ?,
                        ai_category = ?,
                        ai_reason_he = ?,
                        suggested_reply_he = COALESCE(?, suggested_reply_he),
                        guest_type = COALESCE(?, guest_type),
                        urgency = COALESCE(?, urgency),
                        requested_area = COALESCE(?, requested_area),
                        pool_intent = COALESCE(?, pool_intent),
                        privacy_intent = COALESCE(?, privacy_intent),
                        bad_fit_reasons = COALESCE(?, bad_fit_reasons),
                        fit_score = COALESCE(?, fit_score),
                        heat_level = COALESCE(?, heat_level),
                        short_reason_he = COALESCE(?, short_reason_he),
                        recommended_action = COALESCE(?, recommended_action),
                        suggested_first_reply_he = COALESCE(?, suggested_first_reply_he),
                        suggested_followup_he = COALESCE(?, suggested_followup_he),
                        suggested_price_question_he = COALESCE(?, suggested_price_question_he),
                        sent_to_telegram = MAX(sent_to_telegram, ?),
                        notes = COALESCE(notes, ?),
                        text_hash = ?
                    WHERE id = ?
                    """,
                    (
                        now,
                        source,
                        group_name,
                        group_url,
                        author,
                        post_url,
                        post_text,
                        cleaned_text,
                        keywords_serialized,
                        keyword_score,
                        ai_score,
                        ai_category,
                        ai_reason_he,
                        suggested_reply_he,
                        guest_type,
                        urgency,
                        requested_area,
                        pool_intent,
                        privacy_intent,
                        bad_fit_serialized,
                        fit_score,
                        heat_level,
                        short_reason_he,
                        recommended_action,
                        suggested_first_reply_he,
                        suggested_followup_he,
                        suggested_price_question_he,
                        sent_to_telegram,
                        notes,
                        text_hash,
                        lead_id,
                    ),
                )
                return lead_id, "updated"

            cursor = connection.execute(
                """
                INSERT INTO leads (
                    created_at,
                    updated_at,
                    source,
                    group_name,
                    group_url,
                    author,
                    post_url,
                    post_text,
                    cleaned_text,
                    matched_keywords,
                    keyword_score,
                    ai_score,
                    ai_category,
                    ai_reason_he,
                    suggested_reply_he,
                    guest_type,
                    urgency,
                    requested_area,
                    pool_intent,
                    privacy_intent,
                    bad_fit_reasons,
                    fit_score,
                    heat_level,
                    short_reason_he,
                    recommended_action,
                    suggested_first_reply_he,
                    suggested_followup_he,
                    suggested_price_question_he,
                    status,
                    sent_to_telegram,
                    notes,
                    text_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    now,
                    now,
                    source,
                    group_name,
                    group_url,
                    author,
                    post_url,
                    post_text,
                    cleaned_text,
                    keywords_serialized,
                    keyword_score,
                    ai_score,
                    ai_category,
                    ai_reason_he,
                    suggested_reply_he,
                    guest_type,
                    urgency,
                    requested_area,
                    pool_intent,
                    privacy_intent,
                    bad_fit_serialized,
                    fit_score,
                    heat_level,
                    short_reason_he,
                    recommended_action,
                    suggested_first_reply_he,
                    suggested_followup_he,
                    suggested_price_question_he,
                    status,
                    sent_to_telegram,
                    notes,
                    text_hash,
                ),
            )
            return int(cursor.lastrowid), "created"

    def mark_lead_telegram_sent(self, lead_id: int) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE leads
                SET sent_to_telegram = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (utc_now_iso(), lead_id),
            )

    def get_lead(self, lead_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM leads WHERE id = ? LIMIT 1",
                (lead_id,),
            ).fetchone()
        return self._row_to_dict(row)

    def list_leads(
        self,
        status: str | None = None,
        limit: int = 100,
        heat_level: str | None = None,
        guest_type: str | None = None,
        urgency: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM leads"
        params: list[Any] = []
        filters: list[str] = []
        if status:
            filters.append("status = ?")
            params.append(status)
        if heat_level:
            filters.append("heat_level = ?")
            params.append(heat_level)
        if guest_type:
            filters.append("guest_type = ?")
            params.append(guest_type)
        if urgency:
            filters.append("urgency = ?")
            params.append(urgency)
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += " ORDER BY datetime(created_at) DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def summary_stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            total = int(connection.execute("SELECT COUNT(*) AS count FROM leads").fetchone()["count"])
            hot = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE heat_level = 'hot'").fetchone()["count"])
            warm = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE heat_level = 'warm'").fetchone()["count"])
            contacted = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE status IN ('contacted', 'waiting_reply')").fetchone()["count"])
            closed = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE status = 'closed'").fetchone()["count"])
        conversion_rate_placeholder = round((closed / total) * 100, 1) if total else 0.0
        return {
            "total_leads": total,
            "hot_leads": hot,
            "warm_leads": warm,
            "contacted": contacted,
            "closed": closed,
            "conversion_rate_placeholder": conversion_rate_placeholder,
        }

    def update_lead_status(self, lead_id: int, status: str) -> None:
        if status not in ALLOWED_LEAD_STATUSES:
            raise ValueError(f"Unsupported lead status: {status}")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE leads
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, utc_now_iso(), lead_id),
            )

    def update_lead_notes(self, lead_id: int, notes: str | None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE leads
                SET notes = ?, updated_at = ?
                WHERE id = ?
                """,
                ((notes or "").strip() or None, utc_now_iso(), lead_id),
            )

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["matched_keywords_list"] = self.deserialize_keywords(result.get("matched_keywords"))
        result["bad_fit_reasons_list"] = self.deserialize_keywords(result.get("bad_fit_reasons"))
        return result
