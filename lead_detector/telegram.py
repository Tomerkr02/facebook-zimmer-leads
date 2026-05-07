import json
import logging
from urllib import error, request

from ai_scorer import AIScoreResult
from matcher import MatchResult, score_label


logger = logging.getLogger(__name__)


def build_alert_message(
    lead_id: int,
    status: str,
    match_result: MatchResult,
    post_text: str,
    author_name: str | None,
    post_url: str | None,
    group_name: str | None,
    group_url: str,
    ai_reason_he: str | None,
    suggested_reply_he: str | None,
    ai_category: str | None,
    ai_score: int | None,
    heat_level: str | None,
    fit_score: int | None,
    guest_type: str | None,
    urgency: str | None,
    requested_area: str | None,
    pool_intent: str | None,
    ai_result: AIScoreResult | None = None,
) -> str:
    keywords_text = ", ".join(match_result.matched_keywords) or "-"
    content = post_text.strip() if post_text.strip() else "-"
    author = author_name or "לא זמין"
    url = post_url or "לא זמין"
    group = group_name or "לא זמין"
    ai_score_text = f"{ai_score}/10" if ai_score is not None else "לא זמין"
    category_text = ai_category or (ai_result.category if ai_result else "-")
    reason_text = ai_reason_he or (ai_result.reason_he if ai_result else "ליד שעבר סינון מילות מפתח.")
    reply_text = suggested_reply_he or (ai_result.suggested_reply_he if ai_result else "לא זמין")

    return (
        "🔥 ליד חדש לצימר\n\n"
        f"Lead ID: {lead_id}\n"
        f"סטטוס: {status}\n\n"
        f"רמת חום: {heat_level or '-'}\n"
        f"ציון התאמה: {fit_score if fit_score is not None else '-'}\n"
        f"סוג אורח: {guest_type or '-'}\n"
        f"דחיפות: {urgency or '-'}\n"
        f"אזור: {requested_area or '-'}\n"
        f"כוונת בריכה: {pool_intent or '-'}\n\n"
        f"רמת התאמה: {score_label(match_result.score)}\n"
        f"ניקוד מילים: {match_result.score}\n"
        f"ניקוד AI: {ai_score_text}\n"
        f"קטגוריה: {category_text}\n\n"
        "סיבת התאמה:\n"
        f"{reason_text}\n\n"
        "הצעת תגובה:\n"
        f"{reply_text}\n\n"
        "התאמות:\n"
        f"{keywords_text}\n\n"
        "תוכן:\n"
        f"{content}\n\n"
        "כותב:\n"
        f"{author}\n\n"
        "קבוצה:\n"
        f"{group}\n\n"
        "קישור קבוצה:\n"
        f"{group_url}\n\n"
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
