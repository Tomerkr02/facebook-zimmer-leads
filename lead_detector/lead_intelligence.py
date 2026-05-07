import json
import logging
from dataclasses import asdict, dataclass
from typing import Any
from urllib import error, request

from reply_suggestions import generate_reply_suggestion


logger = logging.getLogger(__name__)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL = "gpt-5-mini"

HEAT_LEVELS = {"hot", "warm", "cold", "reject"}
GUEST_TYPES = {"couple", "couple_with_kids", "small_family", "large_group", "unknown"}
URGENCY_TYPES = {"today", "tomorrow", "weekend", "shabbat", "date_specific", "flexible", "unknown"}
AREA_TYPES = {
    "center",
    "rehovot_area",
    "tel_aviv_area",
    "jerusalem_area",
    "north",
    "south",
    "eilat",
    "unknown",
}
POOL_TYPES = {"private_pool", "pool_general", "no_pool", "unknown"}
PRIVACY_TYPES = {"high", "medium", "low", "unknown"}
ACTION_TYPES = {"contact_now", "save_for_later", "reject"}


@dataclass(frozen=True)
class LeadIntelligenceResult:
    guest_type: str
    urgency: str
    requested_area: str
    pool_intent: str
    privacy_intent: str
    bad_fit_reasons: list[str]
    fit_score: int
    heat_level: str
    short_reason_he: str
    recommended_action: str
    suggested_first_reply_he: str
    suggested_followup_he: str
    suggested_price_question_he: str


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _find_matches(text: str, keywords: list[str]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def _normalize(text: str) -> str:
    return " ".join((text or "").lower().split())


def _fallback_followup(guest_type: str, urgency: str) -> str:
    if urgency in {"today", "tomorrow"}:
        return "אם עדיין רלוונטי להיום או למחר, אשמח לבדוק לך זמינות ולעזור לך מהר."
    if guest_type == "couple_with_kids":
        return "אם תרצו, אני יכול לשלוח עוד פרטים שמתאימים לזוג עם ילד או שניים."
    return "אם עדיין רלוונטי, אשמח לשלוח לך פרטים, תמונות וזמינות."


def _fallback_price_question(guest_type: str) -> str:
    if guest_type == "large_group":
        return "לפני שממשיכים, כמה אנשים אתם בדיוק מחפשים לארח?"
    return "כדי לכוון נכון, לאילו תאריכים בערך אתם מחפשים ומה ההרכב שלכם?"


def build_rule_based_intelligence(cleaned_text: str, matched_keywords: list[str] | None = None) -> LeadIntelligenceResult:
    text = _normalize(f"{cleaned_text} {' '.join(matched_keywords or [])}")

    guest_type = "unknown"
    if _contains_any(text, ["10 אנשים", "15 אנשים", "20 אנשים", "קבוצת חברים", "קבוצת בנות", "קבוצה"]):
        guest_type = "large_group"
    elif _contains_any(text, ["זוג עם שני ילדים", "זוג + 2", "זוג פלוס שתיים"]):
        guest_type = "couple_with_kids"
    elif _contains_any(text, ["זוג עם ילד", "זוג + 1", "זוג + ילד", "זוג פלוס ילד"]):
        guest_type = "couple_with_kids"
    elif _contains_any(text, ["משפחה קטנה", "משפחה"]):
        guest_type = "small_family"
    elif _contains_any(text, ["צימר לזוג", "זוג", "לזוג", "זוגי"]):
        guest_type = "couple"

    urgency = "unknown"
    if _contains_any(text, ["להיום", "פנוי היום", "היום"]):
        urgency = "today"
    elif _contains_any(text, ["למחר", "פנוי מחר", "מחר"]):
        urgency = "tomorrow"
    elif _contains_any(text, ["לסופש הקרוב", "פנוי לסופש", "לסופש", "סופ\"ש", "סופש"]):
        urgency = "weekend"
    elif _contains_any(text, ["לשבת", "שבת"]):
        urgency = "shabbat"
    elif _contains_any(text, ["בתאריך", "בתאריכים", "לילה", "ללילה"]):
        urgency = "date_specific"
    elif _contains_any(text, ["מתי שיש", "גמיש", "זורם"]):
        urgency = "flexible"

    requested_area = "unknown"
    if _contains_any(text, ["צפון בלבד", "כנרת בלבד", "צפון", "כנרת", "גליל"]):
        requested_area = "north"
    elif _contains_any(text, ["אילת בלבד", "אילת"]):
        requested_area = "eilat"
    elif _contains_any(text, ["דרום בלבד", "דרום", "ים המלח"]):
        requested_area = "south"
    elif _contains_any(text, ["ירושלים", "ליד ירושלים"]):
        requested_area = "jerusalem_area"
    elif _contains_any(text, ["תל אביב", "ראשון לציון", "רמת גן", "גבעתיים"]):
        requested_area = "tel_aviv_area"
    elif _contains_any(text, ["רחובות", "קריית עקרון", "גדרה", "נס ציונה"]):
        requested_area = "rehovot_area"
    elif _contains_any(text, ["מרכז", "במרכז", "אזור המרכז"]):
        requested_area = "center"

    pool_intent = "unknown"
    if _contains_any(text, ["בריכה פרטית", "עם בריכה פרטית"]):
        pool_intent = "private_pool"
    elif _contains_any(text, ["בריכה", "עם בריכה"]):
        pool_intent = "pool_general"
    elif _contains_any(text, ["בלי בריכה", "לא חייב בריכה"]):
        pool_intent = "no_pool"

    privacy_intent = "unknown"
    if _contains_any(text, ["פרטיות", "פרטי", "שקט", "שקטה"]):
        privacy_intent = "high"
    elif _contains_any(text, ["נעים", "רגוע"]):
        privacy_intent = "medium"
    elif _contains_any(text, ["מסיבה", "אירוע", "רועש"]):
        privacy_intent = "low"

    bad_fit_reasons = []
    bad_fit_reasons.extend(
        _find_matches(
            text,
            [
                "מסיבה",
                "יום הולדת",
                "אירוע",
                "וילה למסיבה",
                "על האש",
                "מנגל",
                "10 אנשים",
                "15 אנשים",
                "20 אנשים",
                "קבוצת חברים",
                "קבוצת בנות",
                "צפון בלבד",
                "כנרת בלבד",
                "אילת בלבד",
                "זול בלבד",
                "הכי זול",
            ],
        )
    )

    fit_score = 4
    if guest_type == "couple":
        fit_score += 2
    if guest_type == "couple_with_kids":
        fit_score += 3
    if guest_type == "small_family":
        fit_score += 1
    if pool_intent == "private_pool":
        fit_score += 2
    elif pool_intent == "pool_general":
        fit_score += 1
    if privacy_intent == "high":
        fit_score += 2
    elif privacy_intent == "medium":
        fit_score += 1
    if urgency in {"today", "tomorrow", "weekend", "shabbat"}:
        fit_score += 1
    if requested_area in {"center", "rehovot_area", "tel_aviv_area", "jerusalem_area"}:
        fit_score += 1

    if guest_type == "large_group":
        fit_score -= 4
    if requested_area in {"north", "south", "eilat"}:
        fit_score -= 4
    if bad_fit_reasons:
        fit_score -= min(5, len(bad_fit_reasons) + 1)

    fit_score = max(1, min(10, fit_score))

    if guest_type == "large_group" or requested_area in {"north", "south", "eilat"} or bad_fit_reasons:
        heat_level = "reject" if fit_score <= 3 else "cold"
    elif fit_score >= 8:
        heat_level = "hot"
    elif fit_score >= 5:
        heat_level = "warm"
    else:
        heat_level = "cold"

    recommended_action = "contact_now" if heat_level in {"hot", "warm"} else "save_for_later"
    if heat_level == "reject":
        recommended_action = "reject"

    short_reason_he = "נראה מתאים למתחם פרטי במרכז עבור זוג או משפחה קטנה."
    if heat_level == "reject":
        short_reason_he = "נראה לא מתאים בגלל אירוע, קבוצה גדולה או אזור יעד שלא מתאים."
    elif heat_level == "cold":
        short_reason_he = "יש עניין חלקי, אבל ההתאמה לא מלאה או לא ברורה."
    elif requested_area == "rehovot_area":
        short_reason_he = "חיפוש באזור רחובות והמרכז עם התאמה טובה למתחם פרטי ושקט."
    elif pool_intent == "private_pool":
        short_reason_he = "מחפש בריכה פרטית ופרטיות, התאמה טובה ל-Royal Water Villa."

    suggested_first_reply_he = generate_reply_suggestion(
        cleaned_text=cleaned_text,
        ai_category="couple_with_kids" if guest_type == "couple_with_kids" else guest_type,
        matched_keywords=matched_keywords,
    )
    suggested_followup_he = _fallback_followup(guest_type, urgency)
    suggested_price_question_he = _fallback_price_question(guest_type)

    return LeadIntelligenceResult(
        guest_type=guest_type,
        urgency=urgency,
        requested_area=requested_area,
        pool_intent=pool_intent,
        privacy_intent=privacy_intent,
        bad_fit_reasons=sorted(set(bad_fit_reasons)),
        fit_score=fit_score,
        heat_level=heat_level,
        short_reason_he=short_reason_he,
        recommended_action=recommended_action,
        suggested_first_reply_he=suggested_first_reply_he,
        suggested_followup_he=suggested_followup_he,
        suggested_price_question_he=suggested_price_question_he,
    )


def _build_ai_prompt(cleaned_text: str) -> list[dict[str, Any]]:
    system_prompt = (
        "אתה שכבת אינטליגנציה ללידים עבור Royal Water Villa בקריית עקרון, ליד רחובות, במרכז ישראל. "
        "המקום מתאים בעיקר לזוג, זוג עם ילד אחד או שניים, פרטיות, שקט ובריכה פרטית. "
        "המקום לא מתאים למסיבות, ימי הולדת, אירועים, קבוצות גדולות, מנגל, צפון בלבד, כנרת בלבד, אילת בלבד או חיפוש זול בלבד. "
        "החזר JSON בלבד לפי הסכמה. כל השדות הטקסטואליים בעברית קצרה וטבעית."
    )
    user_prompt = (
        "נתח את הפוסט כליד פוטנציאלי עבור Royal Water Villa.\n"
        "החזר אפיון מלא: סוג אורח, דחיפות, אזור מבוקש, כוונת בריכה, כוונת פרטיות, סיבות חוסר התאמה, ציון התאמה, רמת חום, "
        "סיבה קצרה בעברית, פעולה מומלצת, הצעת תגובה ראשונה, פולואפ ושאלת מחיר/תאריכים.\n\n"
        f"פוסט:\n{cleaned_text}"
    )
    return [
        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
        {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
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


def _parse_ai_result(raw_json_text: str) -> LeadIntelligenceResult:
    payload = json.loads(raw_json_text)
    result = LeadIntelligenceResult(
        guest_type=str(payload["guest_type"]),
        urgency=str(payload["urgency"]),
        requested_area=str(payload["requested_area"]),
        pool_intent=str(payload["pool_intent"]),
        privacy_intent=str(payload["privacy_intent"]),
        bad_fit_reasons=[str(item) for item in payload["bad_fit_reasons"]],
        fit_score=int(payload["fit_score"]),
        heat_level=str(payload["heat_level"]),
        short_reason_he=str(payload["short_reason_he"]).strip(),
        recommended_action=str(payload["recommended_action"]),
        suggested_first_reply_he=str(payload["suggested_first_reply_he"]).strip(),
        suggested_followup_he=str(payload["suggested_followup_he"]).strip(),
        suggested_price_question_he=str(payload["suggested_price_question_he"]).strip(),
    )
    if result.guest_type not in GUEST_TYPES:
        raise RuntimeError(f"Invalid guest_type: {result.guest_type}")
    if result.urgency not in URGENCY_TYPES:
        raise RuntimeError(f"Invalid urgency: {result.urgency}")
    if result.requested_area not in AREA_TYPES:
        raise RuntimeError(f"Invalid requested_area: {result.requested_area}")
    if result.pool_intent not in POOL_TYPES:
        raise RuntimeError(f"Invalid pool_intent: {result.pool_intent}")
    if result.privacy_intent not in PRIVACY_TYPES:
        raise RuntimeError(f"Invalid privacy_intent: {result.privacy_intent}")
    if result.heat_level not in HEAT_LEVELS:
        raise RuntimeError(f"Invalid heat_level: {result.heat_level}")
    if result.recommended_action not in ACTION_TYPES:
        raise RuntimeError(f"Invalid recommended_action: {result.recommended_action}")
    if not 1 <= result.fit_score <= 10:
        raise RuntimeError(f"Invalid fit_score: {result.fit_score}")
    return result


def score_lead_intelligence_with_ai(api_key: str, cleaned_text: str) -> LeadIntelligenceResult:
    request_body = {
        "model": OPENAI_MODEL,
        "input": _build_ai_prompt(cleaned_text),
        "text": {
            "format": {
                "type": "json_schema",
                "name": "lead_intelligence",
                "strict": True,
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "guest_type": {"type": "string", "enum": sorted(GUEST_TYPES)},
                        "urgency": {"type": "string", "enum": sorted(URGENCY_TYPES)},
                        "requested_area": {"type": "string", "enum": sorted(AREA_TYPES)},
                        "pool_intent": {"type": "string", "enum": sorted(POOL_TYPES)},
                        "privacy_intent": {"type": "string", "enum": sorted(PRIVACY_TYPES)},
                        "bad_fit_reasons": {"type": "array", "items": {"type": "string"}},
                        "fit_score": {"type": "integer", "minimum": 1, "maximum": 10},
                        "heat_level": {"type": "string", "enum": sorted(HEAT_LEVELS)},
                        "short_reason_he": {"type": "string"},
                        "recommended_action": {"type": "string", "enum": sorted(ACTION_TYPES)},
                        "suggested_first_reply_he": {"type": "string"},
                        "suggested_followup_he": {"type": "string"},
                        "suggested_price_question_he": {"type": "string"},
                    },
                    "required": [
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
                    ],
                },
            }
        },
        "max_output_tokens": 350,
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
        raise RuntimeError("OpenAI API returned no output_text for lead intelligence.")
    return _parse_ai_result(raw_output)


def analyze_lead_intelligence(
    *,
    cleaned_text: str,
    matched_keywords: list[str] | None,
    enable_ai_scoring: bool,
    openai_api_key: str,
) -> LeadIntelligenceResult:
    if enable_ai_scoring and openai_api_key:
        try:
            return score_lead_intelligence_with_ai(openai_api_key, cleaned_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LEAD_INTELLIGENCE_AI_FAILED | error=%s", exc)
    return build_rule_based_intelligence(cleaned_text, matched_keywords)


def to_dict(result: LeadIntelligenceResult) -> dict[str, Any]:
    return asdict(result)
