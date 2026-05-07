import argparse
import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from config import load_settings


logger = logging.getLogger(__name__)


ALLOWED_LEAD_STATUSES = {
    "new",
    "contacted",
    "waiting_reply",
    "not_relevant",
    "closed",
    "archived",
}

ALLOWED_EVENT_TYPES = {
    "lead_created",
    "lead_updated",
    "telegram_sent",
    "status_changed",
    "notes_updated",
}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class LeadStorage:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path).resolve()
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS lead_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    event_text TEXT,
                    FOREIGN KEY (lead_id) REFERENCES leads(id)
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    total_groups INTEGER DEFAULT 0,
                    groups_done INTEGER DEFAULT 0,
                    posts_extracted INTEGER DEFAULT 0,
                    posts_matched INTEGER DEFAULT 0,
                    leads_saved INTEGER DEFAULT 0,
                    telegram_sent INTEGER DEFAULT 0,
                    error_text TEXT,
                    log_text TEXT
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_group_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_run_id INTEGER NOT NULL,
                    group_url TEXT NOT NULL,
                    group_name TEXT,
                    status TEXT NOT NULL,
                    cards_found INTEGER DEFAULT 0,
                    extracted INTEGER DEFAULT 0,
                    cleaned INTEGER DEFAULT 0,
                    matched INTEGER DEFAULT 0,
                    saved INTEGER DEFAULT 0,
                    telegram_sent INTEGER DEFAULT 0,
                    failure_reason TEXT,
                    UNIQUE(scan_run_id, group_url),
                    FOREIGN KEY (scan_run_id) REFERENCES scan_runs(id)
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
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_lead_events_lead_created
                ON lead_events(lead_id, created_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_runs_started_at
                ON scan_runs(started_at DESC)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_group_results_scan_run
                ON scan_group_results(scan_run_id, group_url)
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

    @staticmethod
    def serialize_string_list(items: list[str] | None) -> str | None:
        if not items:
            return None
        return json.dumps(items, ensure_ascii=False)

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

    def count_lead_events(self) -> int:
        with self._connect() as connection:
            row = connection.execute("SELECT COUNT(*) AS count FROM lead_events").fetchone()
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

    def debug_snapshot(self, limit: int = 10) -> dict[str, Any]:
        return {
            "resolved_db_path": str(self.database_path),
            "file_exists": self.database_path.exists(),
            "table_names": self.list_table_names(),
            "total_leads": self.count_leads(),
            f"latest_{limit}_leads": self.latest_leads(limit=limit),
            "total_lead_events": self.count_lead_events(),
        }

    def create_scan_run(self, mode: str, total_groups: int) -> int:
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_runs (
                    started_at,
                    status,
                    mode,
                    total_groups,
                    groups_done,
                    posts_extracted,
                    posts_matched,
                    leads_saved,
                    telegram_sent,
                    log_text
                )
                VALUES (?, 'running', ?, ?, 0, 0, 0, 0, 0, '')
                """,
                (now, mode, total_groups),
            )
        return int(cursor.lastrowid)

    def append_scan_log(self, scan_run_id: int, line: str) -> None:
        if not line:
            return
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT COALESCE(log_text, '') AS log_text FROM scan_runs WHERE id = ?",
                (scan_run_id,),
            ).fetchone()
            current = str(existing["log_text"]) if existing else ""
            updated = (current + ("\n" if current else "") + line)[-12000:]
            connection.execute(
                "UPDATE scan_runs SET log_text = ? WHERE id = ?",
                (updated, scan_run_id),
            )

    def update_scan_run(self, scan_run_id: int, **fields: Any) -> None:
        if not fields:
            return
        assignments = ", ".join(f"{column} = ?" for column in fields)
        values = list(fields.values())
        values.append(scan_run_id)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE scan_runs SET {assignments} WHERE id = ?",
                values,
            )

    def sync_scan_run_totals(self, scan_run_id: int) -> None:
        with self._connect() as connection:
            totals = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN status != 'running' THEN 1 ELSE 0 END) AS groups_done,
                    COALESCE(SUM(extracted), 0) AS posts_extracted,
                    COALESCE(SUM(matched), 0) AS posts_matched,
                    COALESCE(SUM(saved), 0) AS leads_saved,
                    COALESCE(SUM(telegram_sent), 0) AS telegram_sent
                FROM scan_group_results
                WHERE scan_run_id = ?
                """,
                (scan_run_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE scan_runs
                SET groups_done = ?,
                    posts_extracted = ?,
                    posts_matched = ?,
                    leads_saved = ?,
                    telegram_sent = ?
                WHERE id = ?
                """,
                (
                    int(totals["groups_done"]) if totals else 0,
                    int(totals["posts_extracted"]) if totals else 0,
                    int(totals["posts_matched"]) if totals else 0,
                    int(totals["leads_saved"]) if totals else 0,
                    int(totals["telegram_sent"]) if totals else 0,
                    scan_run_id,
                ),
            )

    def save_scan_group_result(
        self,
        scan_run_id: int,
        *,
        group_url: str,
        group_name: str | None,
        status: str,
        cards_found: int,
        extracted: int,
        cleaned: int,
        matched: int,
        saved: int,
        telegram_sent: int,
        failure_reason: str | None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO scan_group_results (
                    scan_run_id,
                    group_url,
                    group_name,
                    status,
                    cards_found,
                    extracted,
                    cleaned,
                    matched,
                    saved,
                    telegram_sent,
                    failure_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scan_run_id, group_url) DO UPDATE SET
                    group_name = excluded.group_name,
                    status = excluded.status,
                    cards_found = excluded.cards_found,
                    extracted = excluded.extracted,
                    cleaned = excluded.cleaned,
                    matched = excluded.matched,
                    saved = excluded.saved,
                    telegram_sent = excluded.telegram_sent,
                    failure_reason = excluded.failure_reason
                """,
                (
                    scan_run_id,
                    group_url,
                    group_name,
                    status,
                    cards_found,
                    extracted,
                    cleaned,
                    matched,
                    saved,
                    telegram_sent,
                    failure_reason,
                ),
            )
        self.sync_scan_run_totals(scan_run_id)

    def list_scan_group_results(self, scan_run_id: int) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT *
                FROM scan_group_results
                WHERE scan_run_id = ?
                ORDER BY id ASC
                """,
                (scan_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_scan_run(self, scan_run_id: int) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM scan_runs WHERE id = ? LIMIT 1",
                (scan_run_id,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["group_results"] = self.list_scan_group_results(scan_run_id)
        return result

    def latest_scan_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM scan_runs ORDER BY id DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        return self.get_scan_run(int(row["id"]))

    def get_running_scan_run(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT id
                FROM scan_runs
                WHERE status = 'running'
                ORDER BY id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return self.get_scan_run(int(row["id"]))

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

    def filter_options(self) -> dict[str, list[str]]:
        columns = [
            "heat_level",
            "status",
            "guest_type",
            "urgency",
            "requested_area",
            "ai_category",
        ]
        options: dict[str, list[str]] = {}
        with self._connect() as connection:
            for column in columns:
                rows = connection.execute(
                    f"""
                    SELECT DISTINCT {column} AS value
                    FROM leads
                    WHERE {column} IS NOT NULL AND TRIM({column}) != ''
                    ORDER BY value
                    """
                ).fetchall()
                options[column] = [str(row["value"]) for row in rows]
        return options

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
        logger.info(
            "LEAD_SAVE_ATTEMPT | db_path=%s | post_url=%s | text_hash=%s",
            self.database_path,
            post_url or "-",
            text_hash,
        )
        try:
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
                    connection.execute(
                        """
                        INSERT INTO lead_events (lead_id, created_at, event_type, event_text)
                        VALUES (?, ?, 'lead_updated', ?)
                        """,
                        (lead_id, now, "Lead updated from scraper or rescan."),
                    )
                    logger.info("LEAD_UPDATED | db_path=%s | lead_id=%s", self.database_path, lead_id)
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
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                lead_id = int(cursor.lastrowid)
                connection.execute(
                    """
                    INSERT INTO lead_events (lead_id, created_at, event_type, event_text)
                    VALUES (?, ?, 'lead_created', ?)
                    """,
                    (lead_id, now, "Lead created from scraper."),
                )
                logger.info("LEAD_SAVED | db_path=%s | lead_id=%s", self.database_path, lead_id)
                return lead_id, "created"
        except Exception as exc:  # noqa: BLE001
            logger.exception(
                "LEAD_SAVE_FAILED | db_path=%s | post_url=%s | error=%s",
                self.database_path,
                post_url or "-",
                exc,
            )
            raise

    def mark_lead_telegram_sent(self, lead_id: int) -> None:
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE leads
                SET sent_to_telegram = 1,
                    updated_at = ?
                WHERE id = ?
                """,
                (now, lead_id),
            )
            connection.execute(
                """
                INSERT INTO lead_events (lead_id, created_at, event_type, event_text)
                VALUES (?, ?, 'telegram_sent', ?)
                """,
                (lead_id, now, "Telegram alert sent."),
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
        requested_area: str | None = None,
        ai_category: str | None = None,
        search: str | None = None,
        sort_by: str = "newest",
    ) -> list[dict[str, Any]]:
        sort_mapping = {
            "newest": "datetime(created_at) DESC, id DESC",
            "fit_score": "fit_score DESC, datetime(created_at) DESC",
            "hottest": """
                CASE heat_level
                    WHEN 'hot' THEN 4
                    WHEN 'warm' THEN 3
                    WHEN 'cold' THEN 2
                    WHEN 'reject' THEN 1
                    ELSE 0
                END DESC,
                fit_score DESC,
                datetime(created_at) DESC
            """,
            "urgency": """
                CASE urgency
                    WHEN 'today' THEN 6
                    WHEN 'tomorrow' THEN 5
                    WHEN 'weekend' THEN 4
                    WHEN 'shabbat' THEN 3
                    WHEN 'date_specific' THEN 2
                    WHEN 'flexible' THEN 1
                    ELSE 0
                END DESC,
                datetime(created_at) DESC
            """,
        }
        order_clause = sort_mapping.get(sort_by, sort_mapping["newest"])

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
        if requested_area:
            filters.append("requested_area = ?")
            params.append(requested_area)
        if ai_category:
            filters.append("ai_category = ?")
            params.append(ai_category)
        if search:
            filters.append(
                "(COALESCE(cleaned_text, '') LIKE ? OR COALESCE(post_text, '') LIKE ? OR COALESCE(group_name, '') LIKE ? OR COALESCE(author, '') LIKE ?)"
            )
            like_value = f"%{search.strip()}%"
            params.extend([like_value, like_value, like_value, like_value])
        if filters:
            query += " WHERE " + " AND ".join(filters)
        query += f" ORDER BY {order_clause} LIMIT ?"
        params.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_dict(row) for row in rows]

    def summary_stats(self) -> dict[str, Any]:
        with self._connect() as connection:
            today_prefix = datetime.now(timezone.utc).date().isoformat()
            total = int(connection.execute("SELECT COUNT(*) AS count FROM leads").fetchone()["count"])
            hot = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE heat_level = 'hot'").fetchone()["count"])
            warm = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE heat_level = 'warm'").fetchone()["count"])
            new_leads = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE status = 'new'").fetchone()["count"])
            contacted = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE status IN ('contacted', 'waiting_reply')").fetchone()["count"])
            closed = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE status = 'closed'").fetchone()["count"])
            rejected = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE status = 'not_relevant' OR heat_level = 'reject'").fetchone()["count"])
            today = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM leads WHERE substr(created_at, 1, 10) = ?",
                    (today_prefix,),
                ).fetchone()["count"]
            )
        conversion_rate_placeholder = round((closed / total) * 100, 1) if total else 0.0
        return {
            "total_leads": total,
            "hot_leads": hot,
            "warm_leads": warm,
            "new_leads": new_leads,
            "contacted": contacted,
            "closed": closed,
            "rejected": rejected,
            "today_leads": today,
            "conversion_rate_placeholder": conversion_rate_placeholder,
        }

    def status_counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT status, COUNT(*) AS count
                FROM leads
                GROUP BY status
                """
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows if row["status"]}

    def insights(self) -> dict[str, Any]:
        with self._connect() as connection:
            today_prefix = datetime.now(timezone.utc).date().isoformat()
            best_group = connection.execute(
                """
                SELECT COALESCE(group_name, group_url, 'לא ידוע') AS label, COUNT(*) AS hot_count
                FROM leads
                WHERE heat_level = 'hot'
                GROUP BY COALESCE(group_name, group_url, 'לא ידוע')
                ORDER BY hot_count DESC, label ASC
                LIMIT 1
                """
            ).fetchone()
            area = connection.execute(
                """
                SELECT COALESCE(requested_area, 'unknown') AS label, COUNT(*) AS count
                FROM leads
                GROUP BY COALESCE(requested_area, 'unknown')
                ORDER BY count DESC, label ASC
                LIMIT 1
                """
            ).fetchone()
            guest_type = connection.execute(
                """
                SELECT COALESCE(guest_type, 'unknown') AS label, COUNT(*) AS count
                FROM leads
                GROUP BY COALESCE(guest_type, 'unknown')
                ORDER BY count DESC, label ASC
                LIMIT 1
                """
            ).fetchone()
            urgency = connection.execute(
                """
                SELECT COALESCE(urgency, 'unknown') AS label, COUNT(*) AS count
                FROM leads
                GROUP BY COALESCE(urgency, 'unknown')
                ORDER BY count DESC, label ASC
                LIMIT 1
                """
            ).fetchone()
            today_count = int(
                connection.execute(
                    "SELECT COUNT(*) AS count FROM leads WHERE substr(created_at, 1, 10) = ?",
                    (today_prefix,),
                ).fetchone()["count"]
            )
            hot_today = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM leads
                    WHERE substr(created_at, 1, 10) = ?
                      AND heat_level = 'hot'
                    """,
                    (today_prefix,),
                ).fetchone()["count"]
            )
            rows = connection.execute(
                """
                SELECT matched_keywords
                FROM leads
                WHERE matched_keywords IS NOT NULL
                """
            ).fetchall()

        keyword_counter: dict[str, int] = {}
        for row in rows:
            for keyword in self.deserialize_keywords(row["matched_keywords"]):
                keyword_counter[keyword] = keyword_counter.get(keyword, 0) + 1
        top_keywords = sorted(keyword_counter.items(), key=lambda item: (-item[1], item[0]))[:8]

        return {
            "best_group_by_hot_leads": dict(best_group) if best_group else None,
            "most_common_requested_area": dict(area) if area else None,
            "most_common_guest_type": dict(guest_type) if guest_type else None,
            "most_common_urgency": dict(urgency) if urgency else None,
            "top_matched_keywords": top_keywords,
            "leads_found_today": today_count,
            "hot_leads_today": hot_today,
        }

    def group_performance(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    COALESCE(group_name, group_url, 'לא מזוהה') AS group_label,
                    group_url,
                    COUNT(*) AS total_leads,
                    SUM(CASE WHEN heat_level = 'hot' THEN 1 ELSE 0 END) AS hot_leads,
                    SUM(CASE WHEN heat_level = 'warm' THEN 1 ELSE 0 END) AS warm_leads,
                    SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_leads,
                    SUM(CASE WHEN status IN ('contacted', 'waiting_reply') THEN 1 ELSE 0 END) AS contacted_leads,
                    MAX(created_at) AS last_lead_time
                FROM leads
                GROUP BY COALESCE(group_name, group_url, 'לא מזוהה'), group_url
                ORDER BY total_leads DESC, hot_leads DESC, last_lead_time DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def log_event(self, lead_id: int, event_type: str, event_text: str | None = None) -> None:
        if event_type not in ALLOWED_EVENT_TYPES:
            raise ValueError(f"Unsupported event type: {event_type}")
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO lead_events (lead_id, created_at, event_type, event_text)
                VALUES (?, ?, ?, ?)
                """,
                (lead_id, now, event_type, event_text),
            )

    def list_lead_events(self, lead_id: int, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT id, lead_id, created_at, event_type, event_text
                FROM lead_events
                WHERE lead_id = ?
                ORDER BY datetime(created_at) DESC, id DESC
                LIMIT ?
                """,
                (lead_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def update_lead_status(self, lead_id: int, status: str) -> None:
        if status not in ALLOWED_LEAD_STATUSES:
            raise ValueError(f"Unsupported lead status: {status}")
        now = utc_now_iso()
        with self._connect() as connection:
            current = connection.execute(
                "SELECT status FROM leads WHERE id = ? LIMIT 1",
                (lead_id,),
            ).fetchone()
            connection.execute(
                """
                UPDATE leads
                SET status = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, now, lead_id),
            )
            previous = current["status"] if current else None
            event_text = f"Status changed from {previous or '-'} to {status}."
            connection.execute(
                """
                INSERT INTO lead_events (lead_id, created_at, event_type, event_text)
                VALUES (?, ?, 'status_changed', ?)
                """,
                (lead_id, now, event_text),
            )

    def update_lead_notes(self, lead_id: int, notes: str | None) -> None:
        cleaned_notes = (notes or "").strip() or None
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE leads
                SET notes = ?, updated_at = ?
                WHERE id = ?
                """,
                (cleaned_notes, now, lead_id),
            )
            connection.execute(
                """
                INSERT INTO lead_events (lead_id, created_at, event_type, event_text)
                VALUES (?, ?, 'notes_updated', ?)
                """,
                (lead_id, now, "Lead notes updated."),
            )

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["matched_keywords_list"] = self.deserialize_keywords(result.get("matched_keywords"))
        result["bad_fit_reasons_list"] = self.deserialize_keywords(result.get("bad_fit_reasons"))
        return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Lead storage debug utilities")
    parser.add_argument("--debug", action="store_true", help="Print DB path, tables, and recent leads.")
    args = parser.parse_args()
    if args.debug:
        settings = load_settings()
        storage = LeadStorage(settings.resolved_database_path)
        snapshot = storage.debug_snapshot(limit=5)
        print(f"DB path: {snapshot['resolved_db_path']}")
        print(f"Table names: {', '.join(snapshot['table_names']) or '-'}")
        print(f"Lead count: {snapshot['total_leads']}")
        print(f"Lead events count: {snapshot['total_lead_events']}")
        print("Latest 5 leads:")
        for lead in snapshot["latest_5_leads"]:
            print(
                f"- #{lead['id']} | {lead.get('created_at') or '-'} | "
                f"{lead.get('group_name') or lead.get('group_url') or '-'}"
            )
