import logging
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "lead_detector.db"
DEFAULT_STORAGE_STATE = BASE_DIR / "facebook_state.json"


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    telegram_chat_id: str
    facebook_storage_state_path: Path
    facebook_group_url: str
    database_path: Path
    headless: bool
    max_posts: int
    max_scrolls: int
    min_delay_seconds: float
    max_delay_seconds: float
    min_score: int
    enable_ai_scoring: bool
    openai_api_key: str
    ai_min_score: int
    log_level: str


def load_settings() -> Settings:
    load_dotenv()

    settings = Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        facebook_storage_state_path=Path(
            os.getenv("FACEBOOK_STORAGE_STATE_PATH", str(DEFAULT_STORAGE_STATE))
        ).expanduser(),
        facebook_group_url=os.getenv("FACEBOOK_GROUP_URL", "").strip(),
        database_path=Path(
            os.getenv("LEAD_DETECTOR_DB_PATH", str(DEFAULT_DB_PATH))
        ).expanduser(),
        headless=os.getenv("HEADLESS", "true").strip().lower() in {"1", "true", "yes"},
        max_posts=int(os.getenv("MAX_POSTS", "25")),
        max_scrolls=int(os.getenv("MAX_SCROLLS", "5")),
        min_delay_seconds=float(os.getenv("MIN_DELAY_SECONDS", "2.0")),
        max_delay_seconds=float(os.getenv("MAX_DELAY_SECONDS", "5.0")),
        min_score=int(os.getenv("MIN_SCORE", "5")),
        enable_ai_scoring=os.getenv("ENABLE_AI_SCORING", "false").strip().lower()
        in {"1", "true", "yes"},
        openai_api_key=os.getenv("OPENAI_API_KEY", "").strip(),
        ai_min_score=int(os.getenv("AI_MIN_SCORE", "7")),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper(),
    )

    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    return settings
