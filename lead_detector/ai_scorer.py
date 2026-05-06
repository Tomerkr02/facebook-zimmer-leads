import json
import logging
from dataclasses import dataclass
from typing import Any
from urllib import error, request


logger = logging.getLogger(__name__)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL = "gpt-5-mini"
MIN_TEXT_LENGTH = 25
MAX_TEXT_LENGTH = 4000

AI_SCORE_CATEGORIES = [
    "couple",
    "couple_with_kids",
    "private_pool",
    "weekend",
    "urgent_today",
    "location_match",
    "not_relevant",
]


@dataclass(frozen=True)
class AIScoreResult:
    is_relevant: bool
    score: int
    category: str
    reason_he: str
    suggested_reply_he: str


def is_text_reasonable_for_ai(text: str) -> bool:
    length = len((text or "").strip())
    return MIN_TEXT_LENGTH <= length <= MAX_TEXT_LENGTH


def _build_prompt(post_text: str) -> list[dict[str, Any]]:
    system_prompt = (
        "אתה מסווג פוסטים מפייסבוק כליד אפשרי עבור Royal Water Villa. "
        "העסק הוא וילה/צימר פרטי בקריית עקרון, ליד רחובות, במרכז ישראל. "
        "ההתאמה הגבוהה ביותר היא לזוגות, זוג + ילד אחד או שניים, חופשה שקטה, פרטיות, בריכה פרטית, שבת, סופ\"ש ונופש קצר. "
        "העסק לא מתאים למסיבות, ימי הולדת, אירועים גדולים, קבוצות גדולות, אירועי מנגל, או מי שמחפש רק את האופציה הכי זולה. "
        "דחה בעלי צימרים שמפרסמים, מציעים נכסים, מודעות דרושים, ספאם, פוסטים לא ברורים, ופוסטים שלא נראים כמו חיפוש אמיתי של אורח. "
        "החזר JSON בלבד לפי הסכמה. reason_he ו-suggested_reply_he חייבים להיות קצרים ובעברית. "
        "אם הפוסט לא רלוונטי, suggested_reply_he צריך להיות 'לא לשלוח הודעה אוטומטית'."
    )
    user_prompt = (
        "נתח את הפוסט הבא כליד הזמנה פוטנציאלי.\n"
        "בדוק אם מדובר באדם שמחפש מקום אירוח שמתאים ל-Royal Water Villa.\n"
        "תעדף חיפוש לזוג, זוג עם 1-2 ילדים, פרטיות, בריכה פרטית, מרכז/רחובות/קריית עקרון/תל אביב/ירושלים, "
        "או דחיפות להיום/מחר/סופ\"ש/שבת.\n\n"
        f"פוסט:\n{post_text}"
    )
    return [
        {
            "role": "system",
            "content": [{"type": "input_text", "text": system_prompt}],
        },
        {
            "role": "user",
            "content": [{"type": "input_text", "text": user_prompt}],
        },
    ]


def _extract_output_text(response_json: dict[str, Any]) -> str:
    output_chunks: list[str] = []
    for item in response_json.get("output", []):
        if item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if content.get("type") == "output_text" and content.get("text"):
                output_chunks.append(content["text"])
    return "\n".join(output_chunks).strip()


def _parse_ai_result(raw_json_text: str) -> AIScoreResult:
    payload = json.loads(raw_json_text)
    result = AIScoreResult(
        is_relevant=bool(payload["is_relevant"]),
        score=int(payload["score"]),
        category=str(payload["category"]),
        reason_he=str(payload["reason_he"]).strip(),
        suggested_reply_he=str(payload["suggested_reply_he"]).strip(),
    )
    if not 1 <= result.score <= 10:
        raise RuntimeError(f"Invalid AI score returned: {result.score}")
    if not result.reason_he:
        raise RuntimeError("AI reason_he was empty.")
    if not result.suggested_reply_he:
        raise RuntimeError("AI suggested_reply_he was empty.")
    return result


def score_post_with_ai(api_key: str, post_text: str) -> AIScoreResult:
    request_body = {
        "model": OPENAI_MODEL,
        "input": _build_prompt(post_text),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "guest_lead_score",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "is_relevant": {"type": "boolean"},
                        "score": {"type": "integer", "minimum": 1, "maximum": 10},
                        "category": {
                            "type": "string",
                            "enum": AI_SCORE_CATEGORIES,
                        },
                        "reason_he": {"type": "string"},
                        "suggested_reply_he": {"type": "string"},
                    },
                    "required": [
                        "is_relevant",
                        "score",
                        "category",
                        "reason_he",
                        "suggested_reply_he",
                    ],
                },
            }
        },
        "max_output_tokens": 220,
    }

    http_request = request.Request(
        OPENAI_RESPONSES_URL,
        data=json.dumps(request_body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with request.urlopen(http_request, timeout=45) as response:
            response_json = json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenAI API error {exc.code}: {detail}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"OpenAI API request failed: {exc}") from exc

    raw_output = _extract_output_text(response_json)
    if not raw_output:
        raise RuntimeError("OpenAI API returned no output_text for AI scoring.")

    result = _parse_ai_result(raw_output)
    if result.category not in AI_SCORE_CATEGORIES:
        raise RuntimeError(f"Invalid AI category returned: {result.category}")

    return result
