import json
import logging
from urllib import error, request

from ai_scorer import AIScoreResult
from matcher import MatchResult, score_label


logger = logging.getLogger(__name__)


def build_alert_message(
    match_result: MatchResult,
    post_text: str,
    author_name: str | None,
    post_url: str | None,
    ai_result: AIScoreResult | None = None,
) -> str:
    keywords_text = ", ".join(match_result.matched_keywords) or "-"
    content = post_text.strip() if post_text.strip() else "-"
    author = author_name or "לא זמין"
    url = post_url or "לא זמין"
    ai_details = ""

    if ai_result:
        ai_details = (
            f"ניקוד AI: {ai_result.score}/10\n"
            f"קטגוריה: {ai_result.category}\n\n"
            "סיבת התאמה:\n"
            f"{ai_result.reason_he}\n\n"
            "הצעת תגובה:\n"
            f"{ai_result.suggested_reply_he}\n\n"
        )

    return (
        "🔥 ליד חדש לצימר\n\n"
        f"רמת התאמה: {score_label(match_result.score)}\n"
        f"ניקוד מילים: {match_result.score}\n"
        f"{ai_details}"
        "התאמות:\n"
        f"{keywords_text}\n\n"
        "תוכן:\n"
        f"{content}\n\n"
        "כותב:\n"
        f"{author}\n\n"
        "קישור:\n"
        f"{url}"
    )


def send_message(bot_token: str, chat_id: str, message: str) -> bool:
    if not bot_token or not chat_id:
        logger.warning("Telegram credentials are missing. Skipping alert.")
        return False

    endpoint = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": message,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")

    http_request = request.Request(
        endpoint,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=20) as response:
            response.read()
        logger.info("Telegram alert sent successfully.")
        return True
    except error.HTTPError as exc:
        logger.error("Telegram API returned %s: %s", exc.code, exc.read().decode("utf-8"))
    except error.URLError as exc:
        logger.error("Telegram delivery failed: %s", exc)

    return False
