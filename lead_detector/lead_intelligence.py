import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any
from urllib import error, request

from reply_suggestions import generate_reply_suggestion


logger = logging.getLogger(__name__)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL = "gpt-5-mini"

HEAT_LEVELS = {"ultra_hot", "hot", "warm", "cold", "reject"}
GUEST_TYPES = {"couple", "couple_with_kids", "small_family", "large_group", "religious_couple", "romantic_couple", "unknown"}
LEAD_TYPES = {
    "guest_seeker",
    "owner_advertiser",
    "spam",
    "event_seeker",
    "romantic_couple",
    "religious_couple",
    "family_small",
    "budget_sensitive",
}
AREA_TYPES = {
    "center",
    "rehovot_area",
    "tel_aviv_area",
    "jerusalem_area",
    "north",
    "south",
    "eilat",
    "mixed_center_jerusalem",
    "unknown",
}
FLEXIBILITY_TYPES = {"low", "medium", "high", "unknown"}
POOL_REQUIREMENT_TYPES = {"hard", "soft", "none", "unknown"}


@dataclass(frozen=True)
class LeadIntelligenceResult:
    lead_type: str
    guest_type: str
    group_size_estimate: int
    religious_signal: bool
    romantic_signal: bool
    family_signal: bool
    privacy_signal: bool
    urgency_signal: bool
    budget_signal: str
    pet_request: bool
    preferred_area: str
    required_area: str
    flexibility_level: str
    pool_requirement_strength: str
    emotional_vibe: str
    fit_reason_he: str
    reject_reason_he: str
    conversion_reason_he: str
    intent_score: int
    fit_score: int
    heat_score: int
    heat_label: str
    heat_reasons_json: list[str]
    conversion_score: int
    vibe_score: int
    heat_level: str
    vip_match: bool
    owner_advertisement: bool
    budget_sensitive: bool
    ai_explanation_he: str
    recommended_media_type: str
    recommended_media_reason: str
    requested_area: str
    pool_intent: str
    privacy_intent: str
    urgency: str
    bad_fit_reasons: list[str]
    recommended_action: str
    suggested_first_reply_he: str
    suggested_followup_he: str
    suggested_price_question_he: str
    short_reason_he: str


def _normalize(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text or "")
    return (
        collapsed.replace("״", '"')
        .replace("“", '"')
        .replace("”", '"')
        .replace("׳", "'")
        .replace("’", "'")
        .strip()
        .lower()
    )


def _contains_any(text: str, keywords: list[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def _find_matches(text: str, keywords: list[str]) -> list[str]:
    return [keyword for keyword in keywords if keyword in text]


def _extract_budget(text: str) -> int | None:
    matches = re.findall(r"(\d{3,4})", text)
    for raw_value in matches:
        try:
            value = int(raw_value)
        except ValueError:
            continue
        if 200 <= value <= 10000:
            return value
    return None


def _fallback_followup(lead_type: str, urgency: str) -> str:
    if urgency in {"today", "tomorrow"}:
        return "אם זה עדיין להיום או למחר, אפשר לבדוק לך זמינות ממש עכשיו ולשלוח פרטים מהר."
    if lead_type == "family_small":
        return "אם תרצו, אפשר לשלוח גם פרטים שמתאימים לזוג עם ילדים במתחם שקט ופרטי."
    return "אם זה עדיין רלוונטי, אשמח לשלוח לך תמונות, פרטים וזמינות."


def _fallback_price_question() -> str:
    return "כדי לכוון נכון, מה התאריכים המדויקים וכמה מבוגרים וילדים אתם?"


def _required_or_preferred_area(text: str) -> tuple[str, str, str]:
    if _contains_any(text, ["רק ירושלים", "בלבד ירושלים"]):
        return "jerusalem_area", "jerusalem_area", "low"
    if _contains_any(text, ["עדיפות ירושלים", "הרי ירושלים"]):
        return "jerusalem_area", "center", "medium"
    if _contains_any(text, ["מרכז/ירושלים", "מרכז או ירושלים", "שעה מירושלים"]):
        return "jerusalem_area", "mixed_center_jerusalem", "high"
    if _contains_any(text, ["אילת בלבד", "רק אילת"]):
        return "eilat", "eilat", "low"
    if _contains_any(text, ["צפון בלבד", "כנרת בלבד", "רק צפון"]):
        return "north", "north", "low"
    if _contains_any(text, ["רחובות", "קריית עקרון", "גדרה", "נס ציונה"]):
        return "rehovot_area", "rehovot_area", "high"
    if _contains_any(text, ["תל אביב", "ראשון לציון"]):
        return "tel_aviv_area", "tel_aviv_area", "high"
    if _contains_any(text, ["ירושלים"]):
        return "jerusalem_area", "jerusalem_area", "medium"
    if _contains_any(text, ["מרכז", "אזור המרכז"]):
        return "center", "center", "high"
    return "unknown", "unknown", "unknown"


def _post_freshness_score(post_timestamp: str | None) -> tuple[int, str | None]:
    raw = (post_timestamp or "").strip()
    if not raw:
        return 0, None
    freshness_matches = ["דקה", "דקות", "minute", "minutes", "שעה", "שעות", "hour", "hours", "today", "היום", "אתמול", "yesterday"]
    if any(token in raw.lower() for token in freshness_matches):
        return 10, "Posted recently"
    return 0, None


def build_rule_based_intelligence(
    cleaned_text: str,
    matched_keywords: list[str] | None = None,
    *,
    post_timestamp: str | None = None,
    repeated_author_count: int = 0,
) -> LeadIntelligenceResult:
    text = _normalize(f"{cleaned_text} {' '.join(matched_keywords or [])}")
    bad_fit_reasons: list[str] = []

    owner_matches = _find_matches(
        text,
        ["לפרטים", "מחיר מיוחד", "נותרו תאריכים", "מבצע", "פנוי", "וילה מפנקת", "מוזמנים", "אירוח", "נותר", "התקשרו", "וואטסאפ"],
    )
    phone_number_like = bool(re.search(r"\b05\d{8}\b", text))
    promotional_formatting = text.count("!") >= 2 or ">>" in text or "<<<" in text
    owner_advertisement = bool(owner_matches or phone_number_like or promotional_formatting)

    pet_request = _contains_any(text, ["עם כלב", "עם כלבים", "כלב", "כלבים", "חתול", "חיות", "pet friendly"])
    if pet_request:
        bad_fit_reasons.append("pets_not_allowed")

    if _contains_any(text, ["מסיבה", "וילה למסיבה", "יום הולדת", "אירוע", "על האש", "מנגל"]):
        bad_fit_reasons.append("event_or_party")
    if _contains_any(text, ["2 משפחות", "שתי משפחות", "12 אנשים", "10 אנשים", "15 אנשים", "20 אנשים"]):
        bad_fit_reasons.append("too_large")
    if _contains_any(text, ["הכי זול", "זול בלבד", "מחיר הכי זול"]):
        bad_fit_reasons.append("cheap_only")
    if _contains_any(text, ["אילת בלבד", "רק אילת"]):
        bad_fit_reasons.append("eilat_only")
    if _contains_any(text, ["צפון בלבד", "כנרת בלבד", "רק צפון"]):
        bad_fit_reasons.append("north_only")

    religious_signal = _contains_any(text, ["מוצ\"ש", "מוצש", "דתיים", "שומרי שבת", "צניעות", "פרטיות מלאה"])
    romantic_signal = _contains_any(text, ["רומנטי", "חופשה זוגית", "אני ובעלי", "מאורסים", "יום נישואין", "זוגי"])
    family_signal = _contains_any(text, ["ילד", "ילדים", "משפחה", "זוג + 1", "זוג + 2", "זוג + 3"])
    privacy_signal = _contains_any(text, ["פרטיות", "פרטיות מלאה", "בריכה פרטית", "שקט", "רגוע", "לנקות את הראש"])
    urgency_signal = _contains_any(text, ["להיום", "למחר", "ממחר", "לסופש", "לסופ\"ש", "שישי שבת", "לילה אחד", "מוצ\"ש"])

    guest_type = "unknown"
    if family_signal and _contains_any(text, ["זוג + 3", "זוג עם שלושה", "זוג עם 3"]):
        guest_type = "small_family"
        group_size_estimate = 5
    elif family_signal and _contains_any(text, ["זוג + 2", "זוג עם שני ילדים", "זוג עם 2"]):
        guest_type = "couple_with_kids"
        group_size_estimate = 4
    elif family_signal and _contains_any(text, ["זוג + 1", "זוג עם ילד", "זוג עם 1"]):
        guest_type = "couple_with_kids"
        group_size_estimate = 3
    elif _contains_any(text, ["2 משפחות", "12 אנשים", "10 אנשים", "15 אנשים", "20 אנשים"]):
        guest_type = "large_group"
        group_size_estimate = 10
    elif religious_signal and _contains_any(text, ["זוג", "זוגי"]):
        guest_type = "religious_couple"
        group_size_estimate = 2
    elif romantic_signal and _contains_any(text, ["זוג", "זוגי"]):
        guest_type = "romantic_couple"
        group_size_estimate = 2
    elif _contains_any(text, ["זוג", "זוגי", "מקום לזוג"]):
        guest_type = "couple"
        group_size_estimate = 2
    elif family_signal:
        guest_type = "small_family"
        group_size_estimate = 4
    else:
        group_size_estimate = 0

    preferred_area, required_area, flexibility_level = _required_or_preferred_area(text)
    requested_area = preferred_area if preferred_area != "unknown" else required_area

    pool_requirement_strength = "unknown"
    if _contains_any(text, ["חובה בריכה פרטית", "בריכה פרטית חובה", "חייב בריכה", "רק עם בריכה", "בלבד עם בריכה"]):
        pool_requirement_strength = "hard"
    elif _contains_any(text, ["עדיפות בריכה", "כדאי בריכה", "רצוי בריכה"]):
        pool_requirement_strength = "soft"
    elif _contains_any(text, ["בריכה", "בריכה פרטית"]):
        pool_requirement_strength = "soft"
    else:
        pool_requirement_strength = "none"

    pool_intent = "private_pool" if _contains_any(text, ["בריכה פרטית", "בריכה פרטית לחלוטין"]) else "pool_general" if "בריכה" in text else "unknown"
    privacy_intent = "high" if privacy_signal else "medium" if _contains_any(text, ["פסטורלי", "אווירה", "רגוע"]) else "unknown"

    emotional_flags = []
    if romantic_signal:
        emotional_flags.append("romantic")
    if privacy_signal:
        emotional_flags.append("private")
    if _contains_any(text, ["פסטורלי", "אווירה", "לברוח קצת", "לנקות את הראש"]):
        emotional_flags.append("pastoral")
    if _contains_any(text, ["שקט", "רגוע"]):
        emotional_flags.append("quiet")
    emotional_vibe = ", ".join(emotional_flags) if emotional_flags else "neutral"

    if _contains_any(text, ["להיום", "היום"]):
        urgency = "today"
    elif _contains_any(text, ["ממחר", "למחר", "מחר"]):
        urgency = "tomorrow"
    elif _contains_any(text, ["שישי שבת", "לסופש", 'לסופ"ש', "סופ\"ש", "סופש"]):
        urgency = "weekend"
    elif _contains_any(text, ["מוצ\"ש", "מוצש", "שבת"]):
        urgency = "shabbat"
    elif _contains_any(text, ["14/5", "15/5", "לילה אחד"]):
        urgency = "date_specific"
    else:
        urgency = "flexible"

    budget_value = _extract_budget(text)
    if budget_value is not None and budget_value < 900:
        budget_signal = "budget_sensitive"
    elif budget_value is not None:
        budget_signal = "acceptable"
    else:
        budget_signal = "neutral"
    budget_sensitive = budget_signal == "budget_sensitive"

    intent_score = 1
    if _contains_any(text, ["מחפש מקום", "מחפשת מקום", "מחפשים מקום", "מחפש צימר", "מחפשת צימר", "דירת נופש", "חופשה זוגית", "רק לשים את הראש"]):
        intent_score += 4
    if guest_type != "unknown":
        intent_score += 3
    if urgency_signal:
        intent_score += 3
    if privacy_signal:
        intent_score += 2
    if _contains_any(text, ["חופשה", "לברוח קצת", "לנקות את הראש", "התארחות"]):
        intent_score += 2
    if pool_intent == "private_pool":
        intent_score += 3
    elif pool_intent == "pool_general":
        intent_score += 1
    intent_score = max(1, min(10, intent_score))

    fit_score = 3
    if guest_type in {"couple", "religious_couple", "romantic_couple"}:
        fit_score += 3
    elif guest_type == "couple_with_kids":
        fit_score += 3
    elif guest_type == "small_family":
        fit_score += 2
    if requested_area in {"center", "rehovot_area", "tel_aviv_area", "jerusalem_area", "mixed_center_jerusalem"}:
        fit_score += 2
    if pool_intent == "private_pool":
        fit_score += 2 if pool_requirement_strength == "hard" else 1
    if privacy_signal:
        fit_score += 2
    if religious_signal:
        fit_score += 2
    if romantic_signal:
        fit_score += 2
    if guest_type == "large_group":
        fit_score -= 5
    if budget_sensitive:
        fit_score -= 2
    if owner_advertisement:
        fit_score -= 6
    if any(reason in {"north_only", "eilat_only", "pets_not_allowed", "event_or_party", "too_large"} for reason in bad_fit_reasons):
        fit_score -= 4
    fit_score = max(1, min(10, fit_score))

    heat_reasons: list[str] = []
    heat_score = 35
    freshness_bonus, freshness_reason = _post_freshness_score(post_timestamp)
    heat_score += freshness_bonus
    if freshness_reason:
        heat_reasons.append(freshness_reason)
    if urgency in {"today", "tomorrow"}:
        heat_score += 22
        heat_reasons.append("Urgent today/tomorrow request")
    elif urgency in {"weekend", "shabbat"}:
        heat_score += 16
        heat_reasons.append("Weekend urgency detected")
    elif urgency == "date_specific":
        heat_score += 10
        heat_reasons.append("Date-specific request")
    if _contains_any(text, ["דחוף", "עכשיו", "מהרגע להרגע", "urgent", "last minute", "tonight"]):
        heat_score += 16
        heat_reasons.append("Strong urgency wording")
    if _contains_any(text, ["לילה אחד", "לילה", "tonight"]):
        heat_score += 8
        heat_reasons.append("Short-stay / one-night request")
    if pool_requirement_strength == "hard":
        heat_score += 10
        heat_reasons.append("Private pool required")
    elif pool_intent == "private_pool":
        heat_score += 6
        heat_reasons.append("Private pool requested")
    if guest_type in {"couple", "religious_couple", "romantic_couple", "couple_with_kids", "small_family"}:
        heat_score += 8
        heat_reasons.append("Good guest-size fit")
    if religious_signal or privacy_signal:
        heat_score += 8
        heat_reasons.append("Religious/privacy signal found")
    if repeated_author_count > 1:
        heat_score += 6
        heat_reasons.append("Repeated posting by same author")
    if budget_sensitive:
        heat_score -= 16
        heat_reasons.append("Budget-sensitive request")
    if _contains_any(text, ["עד 500", "עד 700", "הכי זול"]):
        heat_score -= 18
        heat_reasons.append("Very low budget signal")
    if any(reason in {"event_or_party", "too_large", "north_only", "eilat_only", "pets_not_allowed"} for reason in bad_fit_reasons):
        heat_score -= 24
        heat_reasons.append("Bad fit signal lowered urgency value")
    heat_score = max(0, min(100, heat_score))

    vibe_score = 2
    if romantic_signal:
        vibe_score += 3
    if privacy_signal:
        vibe_score += 3
    if _contains_any(text, ["פסטורלי", "אווירה", "לנקות את הראש", "לברוח קצת"]):
        vibe_score += 2
    vibe_score = max(1, min(10, vibe_score))

    conversion_score = 4
    if intent_score >= 7:
        conversion_score += 2
    if fit_score >= 7:
        conversion_score += 2
    if heat_score >= 80:
        conversion_score += 1
    if budget_sensitive:
        conversion_score -= 2
    if flexibility_level == "high":
        conversion_score += 1
    if owner_advertisement or guest_type == "large_group":
        conversion_score -= 4
    if pet_request:
        conversion_score -= 5
    conversion_score = max(1, min(10, conversion_score))

    vip_match = (
        (religious_signal and pool_intent == "private_pool")
        or (romantic_signal and pool_intent == "private_pool")
        or (guest_type in {"couple", "religious_couple", "romantic_couple"} and requested_area in {"center", "rehovot_area", "mixed_center_jerusalem", "jerusalem_area"})
        or (privacy_signal and pool_intent in {"private_pool", "pool_general"})
    )

    if owner_advertisement:
        lead_type = "owner_advertiser"
    elif _contains_any(text, ["מסיבה", "אירוע", "יום הולדת"]):
        lead_type = "event_seeker"
    elif budget_sensitive:
        lead_type = "budget_sensitive"
    elif guest_type == "religious_couple":
        lead_type = "religious_couple"
    elif guest_type == "romantic_couple":
        lead_type = "romantic_couple"
    elif guest_type in {"small_family", "couple_with_kids"}:
        lead_type = "family_small"
    else:
        lead_type = "guest_seeker"

    if owner_advertisement or pet_request or guest_type == "large_group" or any(reason in {"event_or_party", "too_large", "north_only", "eilat_only"} for reason in bad_fit_reasons):
        heat_level = "reject"
    elif vip_match and heat_score >= 80:
        heat_level = "ultra_hot"
    elif heat_score >= 80:
        heat_level = "hot"
    elif heat_score >= 50:
        heat_level = "warm"
    else:
        heat_level = "cold"
    heat_label = "hot" if heat_score >= 80 else "warm" if heat_score >= 50 else "cold"

    fit_reason_bits = []
    if guest_type in {"couple", "religious_couple", "romantic_couple"}:
        fit_reason_bits.append("זוג שמחפש אירוח פרטי")
    if guest_type in {"couple_with_kids", "small_family"}:
        fit_reason_bits.append("משפחה קטנה שמתאימה למתחם")
    if pool_intent == "private_pool":
        fit_reason_bits.append("חיפוש בריכה פרטית")
    if privacy_signal:
        fit_reason_bits.append("דגש על פרטיות ושקט")
    if religious_signal:
        fit_reason_bits.append("איתות דתי/מסורתי חזק")
    if requested_area in {"center", "rehovot_area", "mixed_center_jerusalem", "jerusalem_area"}:
        fit_reason_bits.append("אזור חיפוש שמתאים לקריית עקרון והמרכז")
    fit_reason_he = " | ".join(fit_reason_bits) or "יש התאמה כללית לאירוח פרטי ושקט."

    reject_reason_he = ""
    if owner_advertisement:
        reject_reason_he = "נראה כמו פרסום של בעל מקום ולא חיפוש אירוח."
    elif pet_request:
        reject_reason_he = "הפוסט מבקש הגעה עם חיות מחמד, וזה לא אפשרי ב-Royal Water Villa."
    elif guest_type == "large_group" or "too_large" in bad_fit_reasons:
        reject_reason_he = "ההרכב גדול מדי ביחס למתחם."
    elif "event_or_party" in bad_fit_reasons:
        reject_reason_he = "נראה שמחפשים מסיבה/אירוע ולא אירוח שקט."
    elif "north_only" in bad_fit_reasons or "eilat_only" in bad_fit_reasons:
        reject_reason_he = "הפוסט מכוון לאזור שלא מתאים למיקום שלנו."

    conversion_reason_he = "יש פוטנציאל סגירה טוב כי יש כוונה, התאמה ואווירה שמתאימה למתחם."
    if budget_sensitive:
        conversion_reason_he = "יש כוונה, אבל רגישות למחיר עשויה להקשות על סגירה."
    if heat_level == "reject":
        conversion_reason_he = "פוטנציאל סגירה נמוך בגלל חוסר התאמה מהותי."

    ai_explanation_he = (
        f"Intent {intent_score}/10, Fit {fit_score}/10, Heat {heat_score}/100, "
        f"Conversion {conversion_score}/10, Vibe {vibe_score}/10. "
        f"הפוסט סווג כ-{lead_type} בגלל: {fit_reason_he or reject_reason_he}."
    )
    short_reason_he = fit_reason_he if heat_level != "reject" else reject_reason_he or "נראה לא מתאים."
    recommended_action = "contact_now" if heat_level in {"ultra_hot", "hot", "warm"} else "save_for_later"
    if heat_level == "reject":
        recommended_action = "reject"

    suggested_first_reply_he = generate_reply_suggestion(
        cleaned_text=cleaned_text,
        ai_category="couple_with_kids" if guest_type == "couple_with_kids" else guest_type,
        matched_keywords=matched_keywords,
    )
    suggested_followup_he = _fallback_followup(lead_type, urgency)
    suggested_price_question_he = _fallback_price_question()
    if religious_signal:
        recommended_media_type = "religious"
        recommended_media_reason = "יש איתותים לדתיים/צניעות/מוצ\"ש ולכן כדאי להציג פרטיות מלאה ואווירה מתאימה."
    elif romantic_signal:
        recommended_media_type = "romantic"
        recommended_media_reason = "הפוסט משדר חופשה זוגית/רומנטית ולכן כדאי להדגיש אווירה שקטה ובריכה פרטית."
    elif family_signal:
        recommended_media_type = "family"
        recommended_media_reason = "יש הרכב זוג עם ילדים או משפחה קטנה ולכן מתאים להראות שימוש רגוע למשפחה קטנה."
    elif pool_intent == "private_pool":
        recommended_media_type = "pool"
        recommended_media_reason = "בריכה פרטית היא דרישה או רצון מרכזי ולכן כדאי לפתוח עם ויזואליה של הבריכה."
    elif privacy_signal:
        recommended_media_type = "privacy"
        recommended_media_reason = "הפוסט מדגיש פרטיות, שקט או התנתקות ולכן כדאי להראות את המתחם הפרטי."
    else:
        recommended_media_type = "pastoral"
        recommended_media_reason = "כדאי להציג אווירה פסטורלית ושקטה כדי לחזק את תחושת הבריחה והניקוי ראש."

    return LeadIntelligenceResult(
        lead_type=lead_type,
        guest_type=guest_type,
        group_size_estimate=group_size_estimate,
        religious_signal=religious_signal,
        romantic_signal=romantic_signal,
        family_signal=family_signal,
        privacy_signal=privacy_signal,
        urgency_signal=urgency_signal,
        budget_signal=budget_signal,
        pet_request=pet_request,
        preferred_area=preferred_area,
        required_area=required_area,
        flexibility_level=flexibility_level,
        pool_requirement_strength=pool_requirement_strength,
        emotional_vibe=emotional_vibe,
        fit_reason_he=fit_reason_he,
        reject_reason_he=reject_reason_he,
        conversion_reason_he=conversion_reason_he,
        intent_score=intent_score,
        fit_score=fit_score,
        heat_score=heat_score,
        heat_label=heat_label,
        heat_reasons_json=heat_reasons[:6],
        conversion_score=conversion_score,
        vibe_score=vibe_score,
        heat_level=heat_level,
        vip_match=vip_match,
        owner_advertisement=owner_advertisement,
        budget_sensitive=budget_sensitive,
        ai_explanation_he=ai_explanation_he,
        recommended_media_type=recommended_media_type,
        recommended_media_reason=recommended_media_reason,
        requested_area=requested_area,
        pool_intent=pool_intent,
        privacy_intent="high" if privacy_signal else privacy_intent,
        urgency=urgency,
        bad_fit_reasons=sorted(set(bad_fit_reasons)),
        recommended_action=recommended_action,
        suggested_first_reply_he=suggested_first_reply_he,
        suggested_followup_he=suggested_followup_he,
        suggested_price_question_he=suggested_price_question_he,
        short_reason_he=short_reason_he,
    )


def _build_ai_prompt(cleaned_text: str) -> list[dict[str, Any]]:
    system_prompt = (
        "אתה מערכת דירוג לידים עבור Royal Water Villa בקריית עקרון. "
        "תנתח את הפוסט כמו בעל המקום: כוונת אירוח אמיתית, התאמה למתחם פרטי ושקט עם בריכה פרטית, התאמה לדתיים/רומנטיים/משפחה קטנה, "
        "ודחייה לפוסטים של חיות מחמד, מסיבות, אירועים, צפון בלבד, אילת בלבד, או פרסום של בעלי מקומות. "
        "החזר JSON בלבד לפי הסכמה."
    )
    user_prompt = (
        "נתח את הפוסט כליד פוטנציאלי והחזר דירוג רב-מימדי.\n\n"
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
    return LeadIntelligenceResult(**payload)


def score_lead_intelligence_with_ai(api_key: str, cleaned_text: str) -> LeadIntelligenceResult:
    request_body = {
        "model": OPENAI_MODEL,
        "input": _build_ai_prompt(cleaned_text),
        "text": {"format": {"type": "json_object"}},
        "max_output_tokens": 700,
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
    post_timestamp: str | None = None,
    repeated_author_count: int = 0,
) -> LeadIntelligenceResult:
    if enable_ai_scoring and openai_api_key:
        try:
            return score_lead_intelligence_with_ai(openai_api_key, cleaned_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LEAD_INTELLIGENCE_AI_FAILED | error=%s", exc)
    return build_rule_based_intelligence(
        cleaned_text,
        matched_keywords,
        post_timestamp=post_timestamp,
        repeated_author_count=repeated_author_count,
    )


def to_dict(result: LeadIntelligenceResult) -> dict[str, Any]:
    return asdict(result)
