import json
import logging
import argparse
from urllib import error, request

from ai_scorer import AIScoreResult
from config import load_settings
from matcher import MatchResult, score_label


logger = logging.getLogger(__name__)


HEAT_LABELS = {
    "ultra_hot": "🔥 ULTRA HOT",
    "hot": "🔥 HOT",
    "warm": "🟡 WARM",
    "cold": "❄️ COLD",
    "reject": "⛔ REJECT",
}


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
    ai_category: str | None,
    ai_score: int | None,
    intent_score: int | None,
    heat_score: int | None,
    heat_label: str | None,
    heat_reasons: list[str] | None,
    conversion_score: int | None,
    vibe_score: int | None,
    heat_level: str | None,
    fit_score: int | None,
    fit_reason_he: str | None,
    guest_type: str | None,
    urgency: str | None,
    requested_area: str | None,
    pool_intent: str | None,
    relevance_score: int | None,
    decision_bucket: str | None,
    decision_explanation_he: str | None,
    weakness_reasons: list[str] | None,
    disqualification_risks: list[str] | None,
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
    heat_text = HEAT_LABELS.get((heat_level or "").strip().lower(), heat_level or "-")
    compact_heat = "HOT" if (heat_label or "").lower() == "hot" else "WARM" if (heat_label or "").lower() == "warm" else "COLD"
    short_reason = " + ".join((heat_reasons or [])[:3]) or (fit_reason_he or reason_text)
    hot_perfect = bool((heat_label or "").lower() == "hot" and heat_level == "ultra_hot")

    return (
        "🔥 ליד חדש לצימר\n\n"
        f"Lead ID: {lead_id}\n"
        f"סטטוס: {status}\n\n"
        f"{'👑 HOT PERFECT MATCH' if hot_perfect else heat_text}\n"
        f"Heat: {heat_score if heat_score is not None else '-'} ({compact_heat})\n"
        f"Reason: {short_reason}\n\n"
        f"למה זוהה כליד: {match_result.why_detected_he or '-'}\n"
        f"למה מתאים ל-Royal Water Villa: {fit_reason_he or '-'}\n"
        f"Intent Score: {intent_score if intent_score is not None else match_result.intent_score}\n"
        f"Fit Score: {fit_score if fit_score is not None else '-'}\n"
        f"Heat Score: {heat_score if heat_score is not None else '-'}\n"
        f"Conversion Score: {conversion_score if conversion_score is not None else '-'}\n"
        f"Vibe Score: {vibe_score if vibe_score is not None else '-'}\n"
        f"Relevance Score: {relevance_score if relevance_score is not None else '-'}\n"
        f"רמת חום: {heat_label or heat_level or '-'}\n"
        f"החלטה: {decision_bucket or '-'}\n"
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
        "הסבר החלטה:\n"
        f"{decision_explanation_he or '-'}\n\n"
        "נקודות חולשה:\n"
        f"{', '.join(weakness_reasons or []) or '-'}\n\n"
        "סיכוני פסילה:\n"
        f"{', '.join(disqualification_risks or []) or '-'}\n\n"
        "התאמות:\n"
        f"{keywords_text}\n\n"
        "סיבות כוונה:\n"
        f"{', '.join(match_result.intent_reasons) or '-'}\n\n"
        "סיבות דחיפות:\n"
        f"{', '.join(match_result.urgency_reasons) or '-'}\n\n"
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
        logger.warning("TELEGRAM_SEND_FAILED | reason=missing_credentials")
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
    logger.info("TELEGRAM_SEND_ATTEMPT")

    try:
        with request.urlopen(http_request, timeout=20) as response:
            response.read()
        logger.info("TELEGRAM_SEND_SUCCESS")
        return True
    except error.HTTPError as exc:
        logger.error("TELEGRAM_SEND_FAILED | http_status=%s | body=%s", exc.code, exc.read().decode("utf-8"))
    except error.URLError as exc:
        logger.error("TELEGRAM_SEND_FAILED | error=%s", exc)

    return False


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Telegram utility commands")
    parser.add_argument("--test", action="store_true", help="Send a Telegram test message.")
    args = parser.parse_args()
    if args.test:
        settings = load_settings()
        send_message(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            "✅ Royal Water Villa Lead Bot test successful",
        )
