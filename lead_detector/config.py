import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "lead_detector.db"
DEFAULT_STORAGE_STATE = BASE_DIR / "facebook_state.json"


def resolve_env_path(raw_value: str, default_path: Path) -> Path:
    candidate = Path(raw_value).expanduser() if raw_value else default_path
    if not candidate.is_absolute():
        candidate = BASE_DIR / candidate
    return candidate.resolve()


def parse_group_urls(raw_value: str) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for item in (raw_value or "").split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            urls.append(cleaned)
    return urls


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    facebook_storage_state_path: Path
    facebook_group_urls: list[str]
    database_path: Path
    headless: bool
    max_scrolls: int
    posts_per_group_limit: int
    group_scan_limit: int
    min_delay_seconds: float
    max_delay_seconds: float
    min_keyword_score: int
    enable_ai_scoring: bool
    openai_api_key: str
    ai_min_score: int
    debug_matching: bool
    log_level: str

    @property
    def resolved_database_path(self) -> Path:
        return self.database_path.resolve()


def load_settings() -> Settings:
    load_dotenv()

    settings = Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        facebook_storage_state_path=resolve_env_path(
            os.getenv("FACEBOOK_STORAGE_STATE_PATH", str(DEFAULT_STORAGE_STATE)),
            DEFAULT_STORAGE_STATE,
        ),
        facebook_group_urls=parse_group_urls(os.getenv("FACEBOOK_GROUP_URLS", "")),
        database_path=resolve_env_path(
            os.getenv("LEAD_DETECTOR_DB_PATH", str(DEFAULT_DB_PATH)),
            DEFAULT_DB_PATH,
        ),
        headless=os.getenv("HEADLESS", "true").strip().lower() in {"1", "true", "yes"},
        max_scrolls=int(os.getenv("MAX_SCROLLS", "8")),
        posts_per_group_limit=int(os.getenv("POSTS_PER_GROUP_LIMIT", "80")),
        group_scan_limit=int(os.getenv("GROUP_SCAN_LIMIT", "0")),
        min_delay_seconds=float(os.getenv("MIN_DELAY_SECONDS", "2.0")),
        max_delay_seconds=float(os.getenv("MAX_DELAY_SECONDS", "5.0")),
        min_keyword_score=int(os.getenv("MIN_KEYWORD_SCORE", "4")),
        enable_ai_scoring=os.getenv("ENABLE_AI_SCORING", "false").strip().lower()
        in {"1", "true", "yes"},
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        ai_min_score=int(os.getenv("AI_MIN_SCORE", "7")),
        debug_matching=os.getenv("DEBUG_MATCHING", "false").strip().lower()
        in {"1", "true", "yes"},
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    logging.getLogger(__name__).info(
        "Configured Facebook groups count: %s",
        len(settings.facebook_group_urls),
    )
    logging.getLogger(__name__).info("DB_PATH | %s", settings.resolved_database_path)

    return settings
