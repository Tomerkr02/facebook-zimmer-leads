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

ALLOWED_FEEDBACK_TYPES = {
    "good_lead",
    "bad_lead",
    "closed_successfully",
    "irrelevant",
    "too_expensive",
    "too_large",
    "pets",
    "bad_location",
    "spam",
    "owner_ad",
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
                CREATE TABLE IF NOT EXISTS lead_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    lead_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    feedback_type TEXT NOT NULL,
                    feedback_reason TEXT,
                    original_scores_json TEXT,
                    lead_snapshot_json TEXT,
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
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS scan_matches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scan_run_id INTEGER NOT NULL,
                    group_url TEXT,
                    group_name TEXT,
                    created_at TEXT NOT NULL,
                    raw_text TEXT,
                    cleaned_text TEXT,
                    post_url TEXT,
                    author TEXT,
                    matched_keywords TEXT,
                    intent_score INTEGER DEFAULT 0,
                    fit_score INTEGER DEFAULT 0,
                    heat_score INTEGER DEFAULT 0,
                    conversion_score INTEGER DEFAULT 0,
                    classification TEXT,
                    saved_as_lead_id INTEGER,
                    reject_reason TEXT,
                    FOREIGN KEY (scan_run_id) REFERENCES scan_runs(id),
                    FOREIGN KEY (saved_as_lead_id) REFERENCES leads(id)
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
                CREATE INDEX IF NOT EXISTS idx_lead_feedback_lead_created
                ON lead_feedback(lead_id, created_at DESC)
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
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_scan_matches_scan_run
                ON scan_matches(scan_run_id, created_at DESC)
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
            "intent_score": "INTEGER DEFAULT 0",
            "intent_reasons": "TEXT",
            "urgency_reasons": "TEXT",
            "pet_friendly_requested": "INTEGER DEFAULT 0",
            "lead_type": "TEXT",
            "group_size_estimate": "INTEGER DEFAULT 0",
            "religious_signal": "INTEGER DEFAULT 0",
            "romantic_signal": "INTEGER DEFAULT 0",
            "family_signal": "INTEGER DEFAULT 0",
            "privacy_signal": "INTEGER DEFAULT 0",
            "urgency_signal": "INTEGER DEFAULT 0",
            "budget_signal": "TEXT",
            "pet_request": "INTEGER DEFAULT 0",
            "preferred_area": "TEXT",
            "required_area": "TEXT",
            "flexibility_level": "TEXT",
            "pool_requirement_strength": "TEXT",
            "emotional_vibe": "TEXT",
            "fit_reason_he": "TEXT",
            "reject_reason_he": "TEXT",
            "conversion_reason_he": "TEXT",
            "heat_score": "INTEGER DEFAULT 0",
            "conversion_score": "INTEGER DEFAULT 0",
            "vibe_score": "INTEGER DEFAULT 0",
            "vip_match": "INTEGER DEFAULT 0",
            "owner_advertisement": "INTEGER DEFAULT 0",
            "budget_sensitive": "INTEGER DEFAULT 0",
            "ai_explanation_he": "TEXT",
            "feedback_label": "TEXT",
            "feedback_at": "TEXT",
            "last_contacted_at": "TEXT",
            "recommended_media_type": "TEXT",
            "recommended_media_reason": "TEXT",
            "scan_run_id": "INTEGER",
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

    def count_active_leads(
        self,
        *,
        include_archived: bool = False,
        include_rejected: bool = False,
        include_owner_ads: bool = False,
        include_closed: bool = False,
    ) -> int:
        filters: list[str] = []
        if not include_closed:
            filters.append("COALESCE(status, 'new') IN ('new', 'contacted', 'waiting_reply')")
        if not include_archived:
            filters.append("COALESCE(status, 'new') != 'archived'")
        if not include_rejected:
            filters.append("COALESCE(status, 'new') != 'not_relevant'")
            filters.append("COALESCE(heat_level, 'cold') != 'reject'")
        if not include_owner_ads:
            filters.append("COALESCE(owner_advertisement, 0) = 0")
        query = "SELECT COUNT(*) AS count FROM leads"
        if filters:
            query += " WHERE " + " AND ".join(filters)
        with self._connect() as connection:
            row = connection.execute(query).fetchone()
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

    def save_scan_match(
        self,
        scan_run_id: int,
        *,
        group_url: str | None,
        group_name: str | None,
        raw_text: str | None,
        cleaned_text: str | None,
        post_url: str | None,
        author: str | None,
        matched_keywords: list[str] | None,
        intent_score: int = 0,
        fit_score: int = 0,
        heat_score: int = 0,
        conversion_score: int = 0,
        classification: str | None = None,
        saved_as_lead_id: int | None = None,
        reject_reason: str | None = None,
    ) -> int:
        now = utc_now_iso()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO scan_matches (
                    scan_run_id,
                    group_url,
                    group_name,
                    created_at,
                    raw_text,
                    cleaned_text,
                    post_url,
                    author,
                    matched_keywords,
                    intent_score,
                    fit_score,
                    heat_score,
                    conversion_score,
                    classification,
                    saved_as_lead_id,
                    reject_reason
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    scan_run_id,
                    group_url,
                    group_name,
                    now,
                    raw_text,
                    cleaned_text,
                    post_url,
                    author,
                    self.serialize_keywords(matched_keywords),
                    intent_score,
                    fit_score,
                    heat_score,
                    conversion_score,
                    classification,
                    saved_as_lead_id,
                    reject_reason,
                ),
            )
        return int(cursor.lastrowid)

    def update_scan_match_saved_lead(self, scan_match_id: int, lead_id: int | None, reject_reason: str | None = None) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE scan_matches
                SET saved_as_lead_id = COALESCE(?, saved_as_lead_id),
                    reject_reason = COALESCE(?, reject_reason)
                WHERE id = ?
                """,
                (lead_id, reject_reason, scan_match_id),
            )

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

    def list_scan_matches(
        self,
        scan_run_id: int,
        *,
        classification: str | None = None,
        group_url: str | None = None,
        telegram_only: bool = False,
    ) -> list[dict[str, Any]]:
        query = """
            SELECT sm.*, l.sent_to_telegram
            FROM scan_matches sm
            LEFT JOIN leads l ON l.id = sm.saved_as_lead_id
            WHERE sm.scan_run_id = ?
        """
        params: list[Any] = [scan_run_id]
        if classification:
            query += " AND sm.classification = ?"
            params.append(classification)
        if group_url:
            query += " AND sm.group_url = ?"
            params.append(group_url)
        if telegram_only:
            query += " AND COALESCE(l.sent_to_telegram, 0) = 1"
        query += " ORDER BY datetime(sm.created_at) DESC, sm.id DESC"
        with self._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["matched_keywords_list"] = self.deserialize_keywords(item.get("matched_keywords"))
            results.append(item)
        return results

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
            "lead_type",
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
        intent_score: int = 0,
        intent_reasons: list[str] | None = None,
        urgency_reasons: list[str] | None = None,
        pet_friendly_requested: bool = False,
        lead_type: str | None = None,
        group_size_estimate: int = 0,
        religious_signal: bool = False,
        romantic_signal: bool = False,
        family_signal: bool = False,
        privacy_signal: bool = False,
        urgency_signal: bool = False,
        budget_signal: str | None = None,
        pet_request: bool = False,
        preferred_area: str | None = None,
        required_area: str | None = None,
        flexibility_level: str | None = None,
        pool_requirement_strength: str | None = None,
        emotional_vibe: str | None = None,
        fit_reason_he: str | None = None,
        reject_reason_he: str | None = None,
        conversion_reason_he: str | None = None,
        heat_score: int = 0,
        conversion_score: int = 0,
        vibe_score: int = 0,
        vip_match: bool = False,
        owner_advertisement: bool = False,
        budget_sensitive: bool = False,
        ai_explanation_he: str | None = None,
        last_contacted_at: str | None = None,
        recommended_media_type: str | None = None,
        recommended_media_reason: str | None = None,
        scan_run_id: int | None = None,
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
        intent_reasons_serialized = self.serialize_string_list(intent_reasons)
        urgency_reasons_serialized = self.serialize_string_list(urgency_reasons)
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
                            intent_score = ?,
                            intent_reasons = COALESCE(?, intent_reasons),
                            urgency_reasons = COALESCE(?, urgency_reasons),
                            pet_friendly_requested = ?,
                            lead_type = COALESCE(?, lead_type),
                            group_size_estimate = ?,
                            religious_signal = ?,
                            romantic_signal = ?,
                            family_signal = ?,
                            privacy_signal = ?,
                            urgency_signal = ?,
                            budget_signal = COALESCE(?, budget_signal),
                            pet_request = ?,
                            preferred_area = COALESCE(?, preferred_area),
                            required_area = COALESCE(?, required_area),
                            flexibility_level = COALESCE(?, flexibility_level),
                            pool_requirement_strength = COALESCE(?, pool_requirement_strength),
                            emotional_vibe = COALESCE(?, emotional_vibe),
                            fit_reason_he = COALESCE(?, fit_reason_he),
                            reject_reason_he = COALESCE(?, reject_reason_he),
                            conversion_reason_he = COALESCE(?, conversion_reason_he),
                            heat_score = ?,
                            conversion_score = ?,
                            vibe_score = ?,
                            vip_match = ?,
                            owner_advertisement = ?,
                            budget_sensitive = ?,
                            ai_explanation_he = COALESCE(?, ai_explanation_he),
                            last_contacted_at = COALESCE(?, last_contacted_at),
                            recommended_media_type = COALESCE(?, recommended_media_type),
                            recommended_media_reason = COALESCE(?, recommended_media_reason),
                            scan_run_id = COALESCE(?, scan_run_id),
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
                            intent_score,
                            intent_reasons_serialized,
                            urgency_reasons_serialized,
                            1 if pet_friendly_requested else 0,
                            lead_type,
                            group_size_estimate,
                            1 if religious_signal else 0,
                            1 if romantic_signal else 0,
                            1 if family_signal else 0,
                            1 if privacy_signal else 0,
                            1 if urgency_signal else 0,
                            budget_signal,
                            1 if pet_request else 0,
                            preferred_area,
                            required_area,
                            flexibility_level,
                            pool_requirement_strength,
                            emotional_vibe,
                            fit_reason_he,
                            reject_reason_he,
                            conversion_reason_he,
                            heat_score,
                            conversion_score,
                            vibe_score,
                            1 if vip_match else 0,
                            1 if owner_advertisement else 0,
                            1 if budget_sensitive else 0,
                            ai_explanation_he,
                            last_contacted_at,
                            recommended_media_type,
                            recommended_media_reason,
                            scan_run_id,
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

                insert_columns = [
                    "created_at",
                    "updated_at",
                    "source",
                    "group_name",
                    "group_url",
                    "author",
                    "post_url",
                    "post_text",
                    "cleaned_text",
                    "matched_keywords",
                    "keyword_score",
                    "ai_score",
                    "ai_category",
                    "ai_reason_he",
                    "suggested_reply_he",
                    "guest_type",
                    "urgency",
                    "requested_area",
                    "pool_intent",
                    "privacy_intent",
                    "bad_fit_reasons",
                    "fit_score",
                    "heat_level",
                    "short_reason_he",
                    "recommended_action",
                    "suggested_first_reply_he",
                    "suggested_followup_he",
                    "suggested_price_question_he",
                    "intent_score",
                    "intent_reasons",
                    "urgency_reasons",
                    "pet_friendly_requested",
                    "lead_type",
                    "group_size_estimate",
                    "religious_signal",
                    "romantic_signal",
                    "family_signal",
                    "privacy_signal",
                    "urgency_signal",
                    "budget_signal",
                    "pet_request",
                    "preferred_area",
                    "required_area",
                    "flexibility_level",
                    "pool_requirement_strength",
                    "emotional_vibe",
                    "fit_reason_he",
                    "reject_reason_he",
                    "conversion_reason_he",
                    "heat_score",
                    "conversion_score",
                    "vibe_score",
                    "vip_match",
                    "owner_advertisement",
                    "budget_sensitive",
                    "ai_explanation_he",
                    "last_contacted_at",
                    "recommended_media_type",
                    "recommended_media_reason",
                    "scan_run_id",
                    "status",
                    "sent_to_telegram",
                    "notes",
                    "text_hash",
                ]
                insert_values = (
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
                    intent_score,
                    intent_reasons_serialized,
                    urgency_reasons_serialized,
                    1 if pet_friendly_requested else 0,
                    lead_type,
                    group_size_estimate,
                    1 if religious_signal else 0,
                    1 if romantic_signal else 0,
                    1 if family_signal else 0,
                    1 if privacy_signal else 0,
                    1 if urgency_signal else 0,
                    budget_signal,
                    1 if pet_request else 0,
                    preferred_area,
                    required_area,
                    flexibility_level,
                    pool_requirement_strength,
                    emotional_vibe,
                    fit_reason_he,
                    reject_reason_he,
                    conversion_reason_he,
                    heat_score,
                    conversion_score,
                    vibe_score,
                    1 if vip_match else 0,
                    1 if owner_advertisement else 0,
                    1 if budget_sensitive else 0,
                    ai_explanation_he,
                    last_contacted_at,
                    recommended_media_type,
                    recommended_media_reason,
                    scan_run_id,
                    status,
                    sent_to_telegram,
                    notes,
                    text_hash,
                )
                cursor = connection.execute(
                    f"""
                    INSERT INTO leads ({", ".join(insert_columns)})
                    VALUES ({", ".join(["?"] * len(insert_columns))})
                    """,
                    insert_values,
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
        lead_type: str | None = None,
        religious_only: bool = False,
        romantic_only: bool = False,
        family_only: bool = False,
        owner_ads_only: bool = False,
        rejected_only: bool = False,
        budget_sensitive_only: bool = False,
        include_archived: bool = False,
        include_rejected: bool = False,
        include_owner_ads: bool = False,
        scan_run_id: int | None = None,
        created_date: str | None = None,
        search: str | None = None,
        sort_by: str = "newest",
    ) -> list[dict[str, Any]]:
        sort_mapping = {
            "priority": """
                CASE heat_level
                    WHEN 'ultra_hot' THEN 6
                    WHEN 'hot' THEN 5
                    WHEN 'warm' THEN 4
                    WHEN 'cold' THEN 2
                    WHEN 'reject' THEN 1
                    ELSE 0
                END DESC,
                COALESCE(vip_match, 0) DESC,
                COALESCE(fit_score, 0) DESC,
                datetime(created_at) DESC,
                id DESC
            """,
            "newest": "datetime(created_at) DESC, id DESC",
            "fit_score": "fit_score DESC, datetime(created_at) DESC",
            "conversion_score": "conversion_score DESC, fit_score DESC, datetime(created_at) DESC",
            "hottest": """
                CASE heat_level
                    WHEN 'ultra_hot' THEN 5
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
        if lead_type:
            filters.append("lead_type = ?")
            params.append(lead_type)
        if scan_run_id is not None:
            filters.append("scan_run_id = ?")
            params.append(scan_run_id)
        if created_date == "today":
            filters.append("substr(created_at, 1, 10) = ?")
            params.append(datetime.now(timezone.utc).date().isoformat())
        if not status:
            filters.append("COALESCE(status, 'new') IN ('new', 'contacted', 'waiting_reply')")
        if not include_archived and not status:
            filters.append("COALESCE(status, 'new') != 'archived'")
        if not include_rejected and not rejected_only and not status:
            filters.append("COALESCE(status, 'new') != 'not_relevant'")
            filters.append("COALESCE(heat_level, 'cold') != 'reject'")
        if not include_owner_ads and not owner_ads_only:
            filters.append("COALESCE(owner_advertisement, 0) = 0")
        if religious_only:
            filters.append("religious_signal = 1")
        if romantic_only:
            filters.append("romantic_signal = 1")
        if family_only:
            filters.append("family_signal = 1")
        if owner_ads_only:
            filters.append("owner_advertisement = 1")
        if rejected_only:
            filters.append("heat_level = 'reject'")
        if budget_sensitive_only:
            filters.append("budget_sensitive = 1")
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

    def count_filtered_leads(
        self,
        **filters: Any,
    ) -> int:
        status = filters.get("status")
        heat_level = filters.get("heat_level")
        guest_type = filters.get("guest_type")
        urgency = filters.get("urgency")
        requested_area = filters.get("requested_area")
        ai_category = filters.get("ai_category")
        lead_type = filters.get("lead_type")
        religious_only = bool(filters.get("religious_only"))
        romantic_only = bool(filters.get("romantic_only"))
        family_only = bool(filters.get("family_only"))
        owner_ads_only = bool(filters.get("owner_ads_only"))
        rejected_only = bool(filters.get("rejected_only"))
        budget_sensitive_only = bool(filters.get("budget_sensitive_only"))
        include_archived = bool(filters.get("include_archived"))
        include_rejected = bool(filters.get("include_rejected"))
        include_owner_ads = bool(filters.get("include_owner_ads"))
        scan_run_id = filters.get("scan_run_id")
        created_date = filters.get("created_date")
        search = filters.get("search")

        query = "SELECT COUNT(*) AS count FROM leads"
        params: list[Any] = []
        clauses: list[str] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if heat_level:
            clauses.append("heat_level = ?")
            params.append(heat_level)
        if guest_type:
            clauses.append("guest_type = ?")
            params.append(guest_type)
        if urgency:
            clauses.append("urgency = ?")
            params.append(urgency)
        if requested_area:
            clauses.append("requested_area = ?")
            params.append(requested_area)
        if ai_category:
            clauses.append("ai_category = ?")
            params.append(ai_category)
        if lead_type:
            clauses.append("lead_type = ?")
            params.append(lead_type)
        if scan_run_id is not None:
            clauses.append("scan_run_id = ?")
            params.append(scan_run_id)
        if created_date == "today":
            clauses.append("substr(created_at, 1, 10) = ?")
            params.append(datetime.now(timezone.utc).date().isoformat())
        if not status:
            clauses.append("COALESCE(status, 'new') IN ('new', 'contacted', 'waiting_reply')")
        if not include_archived and not status:
            clauses.append("COALESCE(status, 'new') != 'archived'")
        if not include_rejected and not rejected_only and not status:
            clauses.append("COALESCE(status, 'new') != 'not_relevant'")
            clauses.append("COALESCE(heat_level, 'cold') != 'reject'")
        if not include_owner_ads and not owner_ads_only:
            clauses.append("COALESCE(owner_advertisement, 0) = 0")
        if religious_only:
            clauses.append("religious_signal = 1")
        if romantic_only:
            clauses.append("romantic_signal = 1")
        if family_only:
            clauses.append("family_signal = 1")
        if owner_ads_only:
            clauses.append("owner_advertisement = 1")
        if rejected_only:
            clauses.append("heat_level = 'reject'")
        if budget_sensitive_only:
            clauses.append("budget_sensitive = 1")
        if search:
            clauses.append(
                "(COALESCE(cleaned_text, '') LIKE ? OR COALESCE(post_text, '') LIKE ? OR COALESCE(group_name, '') LIKE ? OR COALESCE(author, '') LIKE ?)"
            )
            like_value = f"%{str(search).strip()}%"
            params.extend([like_value, like_value, like_value, like_value])
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return int(row["count"]) if row else 0

    def summary_stats(
        self,
        *,
        include_archived: bool = False,
        include_rejected: bool = False,
    ) -> dict[str, Any]:
        with self._connect() as connection:
            today_prefix = datetime.now(timezone.utc).date().isoformat()
            visibility_filters: list[str] = ["COALESCE(owner_advertisement, 0) = 0"]
            if not include_archived:
                visibility_filters.append("COALESCE(status, 'new') != 'archived'")
            if not include_rejected:
                visibility_filters.append("COALESCE(status, 'new') != 'not_relevant'")
                visibility_filters.append("COALESCE(heat_level, 'cold') != 'reject'")
            visible_where = " WHERE " + " AND ".join(visibility_filters)

            total = int(connection.execute(f"SELECT COUNT(*) AS count FROM leads{visible_where}").fetchone()["count"])
            active_total = int(connection.execute(f"SELECT COUNT(*) AS count FROM leads{visible_where} AND status IN ('new', 'contacted', 'waiting_reply')").fetchone()["count"])
            hot = int(connection.execute(f"SELECT COUNT(*) AS count FROM leads{visible_where} AND heat_level IN ('ultra_hot', 'hot')").fetchone()["count"])
            warm = int(connection.execute(f"SELECT COUNT(*) AS count FROM leads{visible_where} AND heat_level = 'warm'").fetchone()["count"])
            new_leads = int(connection.execute(f"SELECT COUNT(*) AS count FROM leads{visible_where} AND status = 'new'").fetchone()["count"])
            contacted = int(connection.execute(f"SELECT COUNT(*) AS count FROM leads{visible_where} AND status IN ('contacted', 'waiting_reply')").fetchone()["count"])
            closed = int(connection.execute(f"SELECT COUNT(*) AS count FROM leads{visible_where} AND status = 'closed'").fetchone()["count"])
            rejected = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE status = 'not_relevant' OR heat_level = 'reject' OR owner_advertisement = 1").fetchone()["count"])
            archived = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE status = 'archived'").fetchone()["count"])
            owner_ads = int(connection.execute("SELECT COUNT(*) AS count FROM leads WHERE owner_advertisement = 1").fetchone()["count"])
            today = int(
                connection.execute(
                    f"SELECT COUNT(*) AS count FROM leads{visible_where} AND substr(created_at, 1, 10) = ?",
                    (today_prefix,),
                ).fetchone()["count"]
            )
        conversion_rate_placeholder = round((closed / total) * 100, 1) if total else 0.0
        return {
            "total_leads": total,
            "active_leads": active_total,
            "hot_leads": hot,
            "warm_leads": warm,
            "new_leads": new_leads,
            "contacted": contacted,
            "closed": closed,
            "rejected": rejected,
            "archived": archived,
            "owner_ads": owner_ads,
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
                WHERE heat_level IN ('ultra_hot', 'hot')
                  AND COALESCE(status, 'new') NOT IN ('archived', 'not_relevant')
                  AND COALESCE(owner_advertisement, 0) = 0
                GROUP BY COALESCE(group_name, group_url, 'לא ידוע')
                ORDER BY hot_count DESC, label ASC
                LIMIT 1
                """
            ).fetchone()
            area = connection.execute(
                """
                SELECT COALESCE(requested_area, 'unknown') AS label, COUNT(*) AS count
                FROM leads
                WHERE COALESCE(status, 'new') NOT IN ('archived', 'not_relevant')
                  AND COALESCE(owner_advertisement, 0) = 0
                GROUP BY COALESCE(requested_area, 'unknown')
                ORDER BY count DESC, label ASC
                LIMIT 1
                """
            ).fetchone()
            guest_type = connection.execute(
                """
                SELECT COALESCE(guest_type, 'unknown') AS label, COUNT(*) AS count
                FROM leads
                WHERE COALESCE(status, 'new') NOT IN ('archived', 'not_relevant')
                  AND COALESCE(owner_advertisement, 0) = 0
                GROUP BY COALESCE(guest_type, 'unknown')
                ORDER BY count DESC, label ASC
                LIMIT 1
                """
            ).fetchone()
            urgency = connection.execute(
                """
                SELECT COALESCE(urgency, 'unknown') AS label, COUNT(*) AS count
                FROM leads
                WHERE COALESCE(status, 'new') NOT IN ('archived', 'not_relevant')
                  AND COALESCE(owner_advertisement, 0) = 0
                GROUP BY COALESCE(urgency, 'unknown')
                ORDER BY count DESC, label ASC
                LIMIT 1
                """
            ).fetchone()
            today_count = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM leads
                    WHERE substr(created_at, 1, 10) = ?
                      AND COALESCE(status, 'new') NOT IN ('archived', 'not_relevant')
                      AND COALESCE(owner_advertisement, 0) = 0
                    """,
                    (today_prefix,),
                ).fetchone()["count"]
            )
            hot_today = int(
                connection.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM leads
                    WHERE substr(created_at, 1, 10) = ?
                      AND heat_level IN ('ultra_hot', 'hot')
                      AND COALESCE(status, 'new') NOT IN ('archived', 'not_relevant')
                      AND COALESCE(owner_advertisement, 0) = 0
                    """,
                    (today_prefix,),
                ).fetchone()["count"]
            )
            rows = connection.execute(
                """
                SELECT matched_keywords, intent_reasons, feedback_label, owner_advertisement, lead_type
                FROM leads
                WHERE COALESCE(status, 'new') NOT IN ('archived', 'not_relevant')
                """
            ).fetchall()
            feedback_counts = connection.execute(
                """
                SELECT
                    SUM(CASE WHEN feedback_type IN ('good_lead', 'closed_successfully') THEN 1 ELSE 0 END) AS positive_count,
                    SUM(CASE WHEN feedback_type NOT IN ('good_lead', 'closed_successfully') THEN 1 ELSE 0 END) AS negative_count
                FROM lead_feedback
                """
            ).fetchone()
            rejection_rows = connection.execute(
                """
                SELECT feedback_type, COUNT(*) AS count
                FROM lead_feedback
                WHERE feedback_type NOT IN ('good_lead', 'closed_successfully')
                GROUP BY feedback_type
                ORDER BY count DESC, feedback_type ASC
                LIMIT 8
                """
            ).fetchall()
            converting_rows = connection.execute(
                """
                SELECT COALESCE(lead_type, 'guest_seeker') AS label, COUNT(*) AS count
                FROM leads
                WHERE status = 'closed'
                GROUP BY COALESCE(lead_type, 'guest_seeker')
                ORDER BY count DESC, label ASC
                LIMIT 5
                """
            ).fetchall()

        keyword_counter: dict[str, int] = {}
        vip_counter: dict[str, int] = {}
        owner_counter: dict[str, int] = {}
        for row in rows:
            for keyword in self.deserialize_keywords(row["matched_keywords"]):
                keyword_counter[keyword] = keyword_counter.get(keyword, 0) + 1
            if row["owner_advertisement"]:
                for keyword in self.deserialize_keywords(row["intent_reasons"]):
                    owner_counter[keyword] = owner_counter.get(keyword, 0) + 1
            if row["lead_type"] in {"religious_couple", "romantic_couple", "family_small", "guest_seeker"}:
                for keyword in self.deserialize_keywords(row["intent_reasons"]):
                    vip_counter[keyword] = vip_counter.get(keyword, 0) + 1
        top_keywords = sorted(keyword_counter.items(), key=lambda item: (-item[1], item[0]))[:8]
        top_vip_patterns = sorted(vip_counter.items(), key=lambda item: (-item[1], item[0]))[:6]
        top_owner_patterns = sorted(owner_counter.items(), key=lambda item: (-item[1], item[0]))[:6]

        return {
            "best_group_by_hot_leads": dict(best_group) if best_group else None,
            "most_common_requested_area": dict(area) if area else None,
            "most_common_guest_type": dict(guest_type) if guest_type else None,
            "most_common_urgency": dict(urgency) if urgency else None,
            "top_matched_keywords": top_keywords,
            "leads_found_today": today_count,
            "hot_leads_today": hot_today,
            "total_positive_feedback": int(feedback_counts["positive_count"] or 0) if feedback_counts else 0,
            "total_negative_feedback": int(feedback_counts["negative_count"] or 0) if feedback_counts else 0,
            "common_rejection_reasons": [dict(row) for row in rejection_rows],
            "top_vip_patterns": top_vip_patterns,
            "most_common_owner_ad_patterns": top_owner_patterns,
            "best_converting_lead_types": [dict(row) for row in converting_rows],
        }

    def group_performance(self) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT
                    COALESCE(group_name, group_url, 'לא מזוהה') AS group_label,
                    group_url,
                    COUNT(*) AS total_leads,
                    SUM(CASE WHEN heat_level IN ('ultra_hot', 'hot') THEN 1 ELSE 0 END) AS hot_leads,
                    SUM(CASE WHEN heat_level = 'warm' THEN 1 ELSE 0 END) AS warm_leads,
                    SUM(CASE WHEN status = 'closed' THEN 1 ELSE 0 END) AS closed_leads,
                    SUM(CASE WHEN status IN ('contacted', 'waiting_reply') THEN 1 ELSE 0 END) AS contacted_leads,
                    MAX(created_at) AS last_lead_time
                FROM leads
                WHERE COALESCE(status, 'new') != 'archived'
                GROUP BY COALESCE(group_name, group_url, 'לא מזוהה'), group_url
                ORDER BY total_leads DESC, hot_leads DESC, last_lead_time DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def bulk_update_lead_status(self, lead_ids: list[int], status: str) -> None:
        if status not in ALLOWED_LEAD_STATUSES or not lead_ids:
            return
        placeholders = ", ".join(["?"] * len(lead_ids))
        now = utc_now_iso()
        with self._connect() as connection:
            connection.execute(
                f"""
                UPDATE leads
                SET status = ?, updated_at = ?
                WHERE id IN ({placeholders})
                """,
                [status, now, *lead_ids],
            )

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
                "SELECT status, last_contacted_at FROM leads WHERE id = ? LIMIT 1",
                (lead_id,),
            ).fetchone()
            last_contacted_at = now if status == "contacted" else (current["last_contacted_at"] if current else None)
            connection.execute(
                """
                UPDATE leads
                SET status = ?, updated_at = ?, last_contacted_at = ?
                WHERE id = ?
                """,
                (status, now, last_contacted_at, lead_id),
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

    def add_lead_feedback(
        self,
        lead_id: int,
        feedback_type: str,
        feedback_reason: str | None = None,
    ) -> None:
        if feedback_type not in ALLOWED_FEEDBACK_TYPES:
            raise ValueError(f"Unsupported feedback: {feedback_type}")
        now = utc_now_iso()
        with self._connect() as connection:
            lead_row = connection.execute(
                "SELECT * FROM leads WHERE id = ? LIMIT 1",
                (lead_id,),
            ).fetchone()
            if not lead_row:
                return
            lead_snapshot = self._row_to_dict(lead_row) or {}
            scores_snapshot = {
                "keyword_score": lead_snapshot.get("keyword_score"),
                "intent_score": lead_snapshot.get("intent_score"),
                "fit_score": lead_snapshot.get("fit_score"),
                "heat_score": lead_snapshot.get("heat_score"),
                "conversion_score": lead_snapshot.get("conversion_score"),
                "vibe_score": lead_snapshot.get("vibe_score"),
                "ai_score": lead_snapshot.get("ai_score"),
            }
            feedback_label = "good" if feedback_type in {"good_lead", "closed_successfully"} else "bad"
            connection.execute(
                """
                UPDATE leads
                SET feedback_label = ?, feedback_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (feedback_label, now, now, lead_id),
            )
            connection.execute(
                """
                INSERT INTO lead_feedback (
                    lead_id, created_at, feedback_type, feedback_reason, original_scores_json, lead_snapshot_json
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    lead_id,
                    now,
                    feedback_type,
                    feedback_reason,
                    json.dumps(scores_snapshot, ensure_ascii=False),
                    json.dumps(lead_snapshot, ensure_ascii=False),
                ),
            )
            connection.execute(
                """
                INSERT INTO lead_events (lead_id, created_at, event_type, event_text)
                VALUES (?, ?, 'lead_updated', ?)
                """,
                (lead_id, now, f"Feedback recorded: {feedback_type}."),
            )

    def update_lead_feedback(self, lead_id: int, feedback_label: str) -> None:
        mapping = {"good": "good_lead", "bad": "bad_lead"}
        if feedback_label not in mapping:
            raise ValueError(f"Unsupported feedback: {feedback_label}")
        self.add_lead_feedback(lead_id, mapping[feedback_label])

    def _row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["matched_keywords_list"] = self.deserialize_keywords(result.get("matched_keywords"))
        result["bad_fit_reasons_list"] = self.deserialize_keywords(result.get("bad_fit_reasons"))
        result["intent_reasons_list"] = self.deserialize_keywords(result.get("intent_reasons"))
        result["urgency_reasons_list"] = self.deserialize_keywords(result.get("urgency_reasons"))
        result["pet_friendly_requested"] = bool(result.get("pet_friendly_requested"))
        result["religious_signal"] = bool(result.get("religious_signal"))
        result["romantic_signal"] = bool(result.get("romantic_signal"))
        result["family_signal"] = bool(result.get("family_signal"))
        result["privacy_signal"] = bool(result.get("privacy_signal"))
        result["urgency_signal"] = bool(result.get("urgency_signal"))
        result["pet_request"] = bool(result.get("pet_request"))
        result["vip_match"] = bool(result.get("vip_match"))
        result["owner_advertisement"] = bool(result.get("owner_advertisement"))
        result["budget_sensitive"] = bool(result.get("budget_sensitive"))
        return result

    def get_scan_run_log_lines(self, scan_run_id: int) -> list[str]:
        scan = self.get_scan_run(scan_run_id)
        if not scan:
            return []
        return [line for line in str(scan.get("log_text") or "").splitlines() if line.strip()]

    def get_scan_telegram_failures(self, scan_run_id: int) -> list[str]:
        return [
            line for line in self.get_scan_run_log_lines(scan_run_id)
            if "TELEGRAM_SEND_FAILED" in line or "missing_lead_id" in line
        ]


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
