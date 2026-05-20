import json
import logging
import re
from dataclasses import asdict, dataclass
from typing import Any
from urllib import error, request


logger = logging.getLogger(__name__)

OPENAI_RESPONSES_URL = "https://api.openai.com/v1/responses"
OPENAI_MODEL = "gpt-5-mini"


@dataclass
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
    location_score: int
    privacy_score: int
    timing_score: int
    budget_score: int
    fit_score: int
    heat_score: int
    heat_label: str
    heat_reasons_json: list[str]
    conversion_score: int
    vibe_score: int
    relevance_score: int
    decision_bucket: str
    decision_explanation_he: str
    matched_rules_json: list[str]
    weakness_reasons_json: list[str]
    disqualification_risks_json: list[str]
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
        .replace("–", "-")
        .replace("—", "-")
        .strip()
        .lower()
    )


def _contains_any(text: str, phrases: list[str]) -> bool:
    return any(phrase in text for phrase in phrases)


def _find_matches(text: str, phrases: list[str]) -> list[str]:
    return [phrase for phrase in phrases if phrase in text]


def _extract_budget(text: str) -> int | None:
    for raw in re.findall(r"(\d{3,5})", text):
        try:
            value = int(raw)
        except ValueError:
            continue
        if 200 <= value <= 10000:
            return value
    return None


def _post_freshness_score(post_timestamp: str | None) -> tuple[int, str | None]:
    raw = (post_timestamp or "").strip().lower()
    if not raw:
        return 0, None
    if any(token in raw for token in ["דקה", "דקות", "minute", "minutes", "שעה", "שעות", "hour", "hours"]):
        return 14, "הפוסט פורסם ממש לאחרונה"
    if any(token in raw for token in ["היום", "today", "אתמול", "yesterday"]):
        return 8, "פוסט טרי יחסית"
    return 0, None


def _required_or_preferred_area(text: str) -> tuple[str, str, str]:
    if _contains_any(text, ["רק ירושלים", "בלבד ירושלים", "ירושלים בלבד"]):
        return "jerusalem_area", "jerusalem_area", "low"
    if _contains_any(text, ["צפון בלבד", "כנרת בלבד", "רק צפון"]):
        return "north", "north", "low"
    if _contains_any(text, ["אילת בלבד", "רק אילת"]):
        return "eilat", "eilat", "low"
    if _contains_any(text, ["עדיפות ירושלים", "הרי ירושלים"]):
        return "jerusalem_area", "center", "medium"
    if _contains_any(text, ["מרכז/ירושלים", "מרכז או ירושלים", "שעה מירושלים"]):
        return "mixed_center_jerusalem", "center", "high"
    if _contains_any(text, ["רחובות", "קריית עקרון", "מזכרת בתיה", "ביל\"ו", "בילו", "השפלה", "גדרה", "נס ציונה"]):
        return "rehovot_area", "rehovot_area", "high"
    if _contains_any(text, ["תל אביב", "ראשון לציון"]):
        return "tel_aviv_area", "tel_aviv_area", "high"
    if "ירושלים" in text:
        return "jerusalem_area", "jerusalem_area", "medium"
    if _contains_any(text, ["מרכז", "אזור המרכז"]):
        return "center", "center", "high"
    return "unknown", "unknown", "unknown"


def _estimate_guest_type(text: str) -> tuple[str, int, bool]:
    if _contains_any(text, ["10 אנשים", "12 אנשים", "15 אנשים", "20 אנשים", "כמה משפחות", "2 משפחות", "שתי משפחות", "קבוצת חברים", "קבוצת בנות"]):
        return "large_group", 10, False
    if _contains_any(text, ["זוג + 3", "זוג פלוס 3", "זוג עם 3", "זוג עם שלושה ילדים"]):
        return "small_family", 5, True
    if _contains_any(text, ["זוג + 2", "זוג פלוס 2", "זוג עם 2", "זוג עם שני ילדים", "זוג ושני ילדים"]):
        return "couple_with_kids", 4, True
    if _contains_any(text, ["זוג + ילד", "זוג + 1", "זוג פלוס ילד", "זוג עם ילד", "זוג עם 1", "משפחה קטנה"]):
        return "couple_with_kids", 3, True
    if _contains_any(text, ["משפחה", "למשפחה", "יום משפחתי"]):
        return "small_family", 4, True
    if _contains_any(text, ["זוג", "זוגי", "לזוג", "זוג בלבד"]):
        return "couple", 2, False
    return "unknown", 0, False


def _classify_lead_type(text: str, hard_risks: list[str], strong_recommendation: bool, booking_intent: bool) -> str:
    if "owner_advertising" in hard_risks:
        return "owner_advertiser"
    if "irrelevant_category" in hard_risks or "spam_like" in hard_risks:
        return "irrelevant"
    if strong_recommendation:
        return "recommendation_request"
    if booking_intent:
        return "guest_seeker"
    return "unclear"


def build_rule_based_intelligence(
    cleaned_text: str,
    matched_keywords: list[str] | None = None,
    *,
    post_timestamp: str | None = None,
    repeated_author_count: int = 0,
    learning_adjustment: dict[str, Any] | None = None,
) -> LeadIntelligenceResult:
    text = _normalize(f"{cleaned_text} {' '.join(matched_keywords or [])}")
    learning_adjustment = learning_adjustment or {}

    owner_ad_phrases = [
        "הצימר שלנו",
        "הוילה שלנו",
        "מתחם אירוח",
        "מוזמנים",
        "נותרו תאריכים",
        "לפרטים והזמנות",
        "פרטים בפרטי",
        "סופ\"ש פנוי אצלנו",
        "סופש פנוי אצלנו",
        "בריכה מחוממת במתחם שלנו",
        "מבצע",
        "דקה מהים",
        "אירוח מושלם אצלנו",
        "וילה מפנקת",
        "מחיר מיוחד",
        "פנוי אצלנו",
        "אירוח אצלנו",
    ]
    business_irrelevant_phrases = [
        "מלון בחו\"ל",
        "מלון בחול",
        "מלונות בחו\"ל",
        "השכרת דירה",
        "דירה להשכרה",
        "שכירות לטווח ארוך",
        "נדל\"ן",
        "נדלן",
        "מכירת",
        "קייטרינג",
        "אטרקציה",
        "מוצר",
        "אוכל מוכן",
    ]
    spam_phrases = ["חייגו", "וואטסאפ", "מבצע מטורף", "קוד קופון", "לינק בתגובה"]
    hard_risks: list[str] = []
    owner_matches = _find_matches(text, owner_ad_phrases)
    phone_number_like = bool(re.search(r"\b05\d{8}\b", text))
    first_person_ad = _contains_any(text, ["אצלנו", "שלנו", "נשמח לארח", "מארחים", "מזמינים אתכם"])
    if owner_matches or phone_number_like or first_person_ad:
        hard_risks.append("owner_advertising")
    if _contains_any(text, business_irrelevant_phrases):
        hard_risks.append("irrelevant_category")
    if _contains_any(text, spam_phrases):
        hard_risks.append("spam_like")

    recommendation_patterns = ["המלצות על", "יש למישהו", "איפה יש", "ממליצים על"]
    seeker_patterns = [
        "מחפש",
        "מחפשת",
        "מחפשים",
        "צריכה מקום",
        "צריך מקום",
        "מחפשת וילה",
        "מחפש בריכה פרטית",
        "מחפשת צימר",
        "מחפש צימר",
        "מחפשים מקום",
        "בריכה פרטית להשכרה",
        "לילה אחד",
        "רק לשים את הראש",
    ]
    couple_family_patterns = [
        "לזוג",
        "זוג",
        "זוגי",
        "למשפחה",
        "משפחה קטנה",
        "זוג עם ילד",
        "זוג + ילד",
        "זוג + 2",
        "זוג + 3",
    ]
    location_patterns = [
        "באזור המרכז",
        "קרוב לרחובות",
        "רחובות",
        "מזכרת בתיה",
        "קריית עקרון",
        "ביל\"ו",
        "בילו",
        "השפלה",
        "גדרה",
        "נס ציונה",
        "ראשון לציון",
        "תל אביב",
        "ירושלים",
    ]
    privacy_patterns = [
        "בריכה פרטית",
        "מחפש בריכה פרטית",
        "בריכה פרטית להשכרה",
        "פרטי ושקט",
        "פרטיות",
        "שקט",
        "מקום שקט",
        "רומנטי",
        "וילה פרטית",
        "מקום פרטי",
        "לנקות את הראש",
        "חופשה זוגית",
    ]
    timing_patterns = [
        "היום",
        "מחר",
        "להיום",
        "למחר",
        "לשבת",
        "שבת",
        "סופ\"ש",
        "סופש",
        "סופ\"ש הקרוב",
        "שישי שבת",
        "לילה אחד",
        "עכשיו",
        "דחוף",
    ]
    religious_patterns = ["כשר", "דתיים", "שומרי שבת", "צניעות", "מוצ\"ש", "מוצש"]
    event_patterns = ["מסיבה", "אירוע", "מנגל", "על האש", "הרבה חברים", "וילה למסיבה"]
    pet_patterns = ["כלב", "כלבים", "חתול", "חתולים", "חיות", "pet friendly"]
    budget_patterns = ["הכי זול", "עד 500", "עד 700", "זול בלבד", "מחיר נמוך"]

    matched_intent = _find_matches(text, seeker_patterns)
    matched_recommendation = _find_matches(text, recommendation_patterns)
    matched_couple_family = _find_matches(text, couple_family_patterns)
    matched_locations = _find_matches(text, location_patterns)
    matched_privacy = _find_matches(text, privacy_patterns)
    matched_timing = _find_matches(text, timing_patterns)
    matched_religious = _find_matches(text, religious_patterns)
    matched_events = _find_matches(text, event_patterns)
    matched_pets = _find_matches(text, pet_patterns)
    matched_budget = _find_matches(text, budget_patterns)

    booking_intent = bool(matched_intent)
    strong_recommendation = bool(matched_recommendation) and bool(
        matched_couple_family or matched_locations or matched_privacy or matched_timing
    )
    guest_type, group_size_estimate, family_signal = _estimate_guest_type(text)
    preferred_area, required_area, flexibility_level = _required_or_preferred_area(text)
    requested_area = preferred_area if preferred_area != "unknown" else required_area
    religious_signal = bool(matched_religious)
    romantic_signal = _contains_any(text, ["רומנטי", "ליום נישואין", "הצעת נישואין", "יום הולדת זוגי", "חופשה זוגית"])
    privacy_signal = bool(matched_privacy)
    urgency_signal = bool(matched_timing)
    pet_request = bool(matched_pets)

    if pet_request:
        hard_risks.append("pets_not_allowed")
    if matched_events:
        hard_risks.append("event_or_party")
    if guest_type == "large_group":
        hard_risks.append("too_large")
    if _contains_any(text, ["אילת בלבד", "רק אילת", "צפון בלבד", "כנרת בלבד", "רק צפון"]):
        hard_risks.append("bad_area_required")

    lead_type = _classify_lead_type(text, hard_risks, strong_recommendation, booking_intent)

    pool_requirement_strength = "hard" if _contains_any(text, ["חובה בריכה", "חובה בריכה פרטית", "בריכה פרטית חובה", "חייב בריכה", "רק עם בריכה", "בלבד עם בריכה"]) else "soft" if _contains_any(text, ["בריכה פרטית", "בריכה"]) else "none"
    pool_intent = "private_pool" if _contains_any(text, ["בריכה פרטית", "בריכה פרטית להשכרה", "חובה בריכה"]) else "pool_general" if "בריכה" in text else "unknown"
    privacy_intent = "high" if privacy_signal else "low"

    if _contains_any(text, ["היום", "להיום"]):
        urgency = "today"
    elif _contains_any(text, ["מחר", "למחר"]):
        urgency = "tomorrow"
    elif _contains_any(text, ["סופ\"ש", "סופש", "שישי שבת"]):
        urgency = "weekend"
    elif _contains_any(text, ["לשבת", "מוצ\"ש", "מוצש"]):
        urgency = "shabbat"
    elif _contains_any(text, ["לילה אחד"]):
        urgency = "date_specific"
    else:
        urgency = "unknown" if not matched_timing else "flexible"

    budget_value = _extract_budget(text)
    budget_signal = "neutral"
    budget_score = 50
    if matched_budget or (budget_value is not None and budget_value < 900):
        budget_signal = "budget_sensitive"
        budget_score = 20
    elif budget_value is not None and budget_value >= 900:
        budget_signal = "acceptable"
        budget_score = 60
    budget_sensitive = budget_signal == "budget_sensitive"

    intent_score = 0
    if booking_intent:
        intent_score += 45
    if strong_recommendation:
        intent_score += 35
    if matched_couple_family:
        intent_score += 15
    if matched_privacy or matched_timing or matched_locations:
        intent_score += 10
    intent_score = max(0, min(100, intent_score))

    location_score = 20
    if requested_area in {"rehovot_area", "center"}:
        location_score = 95
    elif requested_area in {"tel_aviv_area", "jerusalem_area", "mixed_center_jerusalem"}:
        location_score = 78
    elif requested_area in {"north", "eilat"}:
        location_score = 5

    privacy_score = 20
    if pool_intent == "private_pool":
        privacy_score += 40
    if pool_requirement_strength == "hard":
        privacy_score += 15
    if privacy_signal:
        privacy_score += 25
    if romantic_signal:
        privacy_score += 10
    privacy_score = max(0, min(100, privacy_score))

    timing_score = 20
    if urgency in {"today", "tomorrow"}:
        timing_score = 95
    elif urgency in {"weekend", "shabbat"}:
        timing_score = 82
    elif urgency == "date_specific":
        timing_score = 70
    elif urgency_signal:
        timing_score = 60

    fit_score = 25
    if guest_type == "couple":
        fit_score += 35
    elif guest_type == "couple_with_kids":
        fit_score += 32
    elif guest_type == "small_family":
        fit_score += 28
    if religious_signal:
        fit_score += 15
    if romantic_signal:
        fit_score += 15
    if privacy_signal:
        fit_score += 10
    if guest_type == "large_group":
        fit_score -= 50
    fit_score = max(0, min(100, fit_score))

    vibe_score = 15
    emotional_flags: list[str] = []
    if romantic_signal:
        emotional_flags.append("romantic")
        vibe_score += 25
    if privacy_signal:
        emotional_flags.append("private")
        vibe_score += 25
    if _contains_any(text, ["שקט", "רגוע", "פסטורלי", "לנקות את הראש", "חופשה"]):
        emotional_flags.append("relaxing")
        vibe_score += 20
    emotional_vibe = ", ".join(emotional_flags) if emotional_flags else "neutral"
    vibe_score = max(0, min(100, vibe_score))

    freshness_bonus, freshness_reason = _post_freshness_score(post_timestamp)
    heat_reasons: list[str] = []
    if freshness_reason:
        heat_reasons.append(freshness_reason)
    heat_score = 15 + freshness_bonus
    if timing_score >= 80:
        heat_score += 35
        heat_reasons.append("דחיפות גבוהה בזמנים")
    elif timing_score >= 60:
        heat_score += 22
        heat_reasons.append("יש איתות זמן רלוונטי")
    if pool_requirement_strength == "hard":
        heat_score += 16
        heat_reasons.append("בריכה פרטית היא דרישה חזקה")
    elif pool_intent == "private_pool":
        heat_score += 10
        heat_reasons.append("מחפשים בריכה פרטית")
    if guest_type in {"couple", "couple_with_kids", "small_family"}:
        heat_score += 12
        heat_reasons.append("הרכב אורחים מתאים")
    if religious_signal or privacy_signal:
        heat_score += 10
        heat_reasons.append("יש דגש על פרטיות / התאמה לדתיים")
    if repeated_author_count > 1:
        heat_score += 6
        heat_reasons.append("אותו כותב פרסם שוב")
    if budget_sensitive:
        heat_score -= 18
    if hard_risks:
        heat_score -= 30
    heat_score = max(0, min(100, heat_score))
    heat_label = "hot" if heat_score >= 80 else "warm" if heat_score >= 50 else "cold"

    conversion_score = round(
        (intent_score * 0.30)
        + (fit_score * 0.25)
        + (location_score * 0.15)
        + (timing_score * 0.15)
        + (privacy_score * 0.10)
        + (budget_score * 0.05)
    )
    conversion_score = max(0, min(100, conversion_score))

    matched_rules = []
    if booking_intent:
        matched_rules.append("כוונת חיפוש ישירה")
    if strong_recommendation:
        matched_rules.append("בקשת המלצה חזקה עם הקשר הזמנה")
    if matched_couple_family:
        matched_rules.append("איתות זוג / משפחה קטנה")
    if matched_locations:
        matched_rules.append("אזור רלוונטי למרכז / רחובות")
    if matched_privacy:
        matched_rules.append("חיפוש פרטיות / בריכה פרטית / שקט")
    if matched_religious:
        matched_rules.append("איתות דתי / כשרות / שבת")
    if matched_timing:
        matched_rules.append("בקשה עם דחיפות / תאריך")

    weakness_reasons = []
    if not matched_locations:
        weakness_reasons.append("אין אזור ברור שמתאים למתחם")
    if lead_type == "recommendation_request":
        weakness_reasons.append("זו בקשת המלצה ולא בקשת הזמנה ישירה")
    if budget_sensitive:
        weakness_reasons.append("רגישות גבוהה למחיר")
    if not (matched_privacy or pool_intent == "private_pool"):
        weakness_reasons.append("אין דרישת פרטיות / בריכה פרטית חזקה")
    if lead_type == "unclear":
        weakness_reasons.append("כוונת ההזמנה אינה חד משמעית")

    fit_reason_bits = []
    if guest_type == "couple":
        fit_reason_bits.append("זוג שמחפש אירוח")
    elif guest_type == "couple_with_kids":
        fit_reason_bits.append("זוג עם ילדים שמתאים למתחם קטן")
    elif guest_type == "small_family":
        fit_reason_bits.append("משפחה קטנה ושקטה")
    if pool_intent == "private_pool":
        fit_reason_bits.append("בריכה פרטית חשובה להם")
    if privacy_signal:
        fit_reason_bits.append("מחפשים פרטיות ושקט")
    if requested_area in {"rehovot_area", "center", "tel_aviv_area", "jerusalem_area", "mixed_center_jerusalem"}:
        fit_reason_bits.append("האזור רלוונטי לקריית עקרון והמרכז")
    if religious_signal:
        fit_reason_bits.append("יש איתותים שמתאימים לאירוח דתי/מסורתי")
    fit_reason_he = " | ".join(fit_reason_bits) or "אין התאמה חזקה מספיק למתחם."

    reject_reason_he = ""
    if "owner_advertising" in hard_risks:
        reject_reason_he = "נראה שזה פרסום של בעל מקום ולא חיפוש אמיתי של אורח."
    elif "irrelevant_category" in hard_risks:
        reject_reason_he = "הפוסט עוסק בקטגוריה לא רלוונטית לאירוח קצר ב-Royal Water Villa."
    elif "pets_not_allowed" in hard_risks:
        reject_reason_he = "יש בקשה לחיות מחמד, וזה לא אפשרי אצלנו."
    elif "event_or_party" in hard_risks:
        reject_reason_he = "נראה שמחפשים מסיבה או אירוע ולא אירוח שקט."
    elif "too_large" in hard_risks:
        reject_reason_he = "גודל הקבוצה גדול מדי למתחם."
    elif "bad_area_required" in hard_risks:
        reject_reason_he = "נראה שהבקשה מוגבלת לאזור שלא מתאים למיקום שלנו."

    learning_delta = int(learning_adjustment.get("relevance_delta") or 0)
    learning_reasons = list(learning_adjustment.get("learning_reasons") or [])

    relevance_score = round(
        (intent_score * 0.30)
        + (location_score * 0.15)
        + (privacy_score * 0.15)
        + (timing_score * 0.10)
        + (budget_score * 0.05)
        + (conversion_score * 0.10)
        + (fit_score * 0.10)
        + (vibe_score * 0.05)
    )
    if lead_type == "recommendation_request":
        relevance_score -= 8
    if lead_type == "unclear":
        relevance_score -= 20
    if hard_risks:
        relevance_score -= 40
    relevance_score += learning_delta
    relevance_score = max(0, min(100, relevance_score))

    vip_match = bool(
        requested_area in {"rehovot_area", "center", "jerusalem_area", "mixed_center_jerusalem"}
        and guest_type in {"couple", "couple_with_kids", "small_family"}
        and pool_intent == "private_pool"
        and (privacy_signal or religious_signal or romantic_signal)
    )
    if vip_match:
        matched_rules.append("VIP match לדפוס האורח האידיאלי")

    showable_type = lead_type in {"guest_seeker", "recommendation_request"}
    strong_recommendation_ok = lead_type != "recommendation_request" or bool(
        matched_privacy or matched_locations or matched_timing or matched_couple_family
    )
    if hard_risks or not showable_type or not strong_recommendation_ok:
        decision_bucket = "hidden"
    elif relevance_score >= 70:
        decision_bucket = "show"
    elif relevance_score >= 50:
        decision_bucket = "review"
    else:
        decision_bucket = "hidden"

    if decision_bucket == "show":
        recommended_action = "contact_now"
    elif decision_bucket == "review":
        recommended_action = "save_for_later"
    else:
        recommended_action = "reject"

    if decision_bucket == "hidden":
        heat_level = "reject" if hard_risks else "cold"
    elif vip_match and heat_score >= 80:
        heat_level = "ultra_hot"
    elif heat_score >= 80:
        heat_level = "hot"
    elif heat_score >= 50:
        heat_level = "warm"
    else:
        heat_level = "cold"

    decision_label = {"show": "Show", "review": "Review", "hidden": "Hidden"}[decision_bucket]
    decision_explanation_he = (
        f"החלטה: {decision_label}. "
        f"Intent {intent_score}, Location {location_score}, Privacy {privacy_score}, Timing {timing_score}, "
        f"Budget {budget_score}, Conversion {conversion_score}, Relevance {relevance_score}."
    )
    if learning_reasons:
        decision_explanation_he += " השפעת למידה: " + " | ".join(learning_reasons[:3])
    if hard_risks:
        decision_explanation_he += " סיכוני פסילה: " + " | ".join(sorted(set(hard_risks)))

    ai_explanation_he = (
        f"למה עבר: {' | '.join(matched_rules[:6]) or 'אין חיזוקים חזקים'}. "
        f"למה חלש: {' | '.join(weakness_reasons[:4]) or 'אין חולשות מהותיות'}. "
        f"החלטה סופית: {decision_label}."
    )
    short_reason_he = fit_reason_he if decision_bucket != "hidden" else (reject_reason_he or "ליד חלש או לא מתאים.")
    conversion_reason_he = (
        "פוטנציאל סגירה גבוה יחסית בגלל כוונה ברורה והתאמה טובה."
        if decision_bucket == "show"
        else "יש עניין מסוים, אבל צריך בדיקה ידנית לפני השקעת זמן."
        if decision_bucket == "review"
        else "פוטנציאל הסגירה נמוך כרגע."
    )

    recommended_media_type = "pool" if pool_intent == "private_pool" else "privacy" if privacy_signal else "romantic" if romantic_signal else "family" if family_signal else "pastoral"
    recommended_media_reason = "נשמר רק כהכנה עתידית; אין יצירת תגובות אוטומטית."

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
        location_score=location_score,
        privacy_score=privacy_score,
        timing_score=timing_score,
        budget_score=budget_score,
        fit_score=fit_score,
        heat_score=heat_score,
        heat_label=heat_label,
        heat_reasons_json=heat_reasons[:6],
        conversion_score=conversion_score,
        vibe_score=vibe_score,
        relevance_score=relevance_score,
        decision_bucket=decision_bucket,
        decision_explanation_he=decision_explanation_he,
        matched_rules_json=matched_rules[:8],
        weakness_reasons_json=weakness_reasons[:8],
        disqualification_risks_json=sorted(set(hard_risks)),
        heat_level=heat_level,
        vip_match=vip_match,
        owner_advertisement="owner_advertising" in hard_risks,
        budget_sensitive=budget_sensitive,
        ai_explanation_he=ai_explanation_he,
        recommended_media_type=recommended_media_type,
        recommended_media_reason=recommended_media_reason,
        requested_area=requested_area,
        pool_intent=pool_intent,
        privacy_intent=privacy_intent,
        urgency=urgency,
        bad_fit_reasons=sorted(set(hard_risks)),
        recommended_action=recommended_action,
        suggested_first_reply_he="",
        suggested_followup_he="",
        suggested_price_question_he="",
        short_reason_he=short_reason_he,
    )


def _build_ai_prompt(cleaned_text: str) -> list[dict[str, Any]]:
    system_prompt = (
        "נתח פוסט כליד עבור Royal Water Villa. "
        "אם חסרים שדות, עדיף להחזיר אובייקט חלקי ונפילה חזרה לכללים מקומיים."
    )
    user_prompt = f"פוסט:\n{cleaned_text}"
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


def _parse_ai_result(raw_json_text: str, baseline: LeadIntelligenceResult) -> LeadIntelligenceResult:
    payload = json.loads(raw_json_text)
    data = asdict(baseline)
    for key, value in payload.items():
        if key in data:
            data[key] = value
    return LeadIntelligenceResult(**data)


def score_lead_intelligence_with_ai(api_key: str, cleaned_text: str, baseline: LeadIntelligenceResult) -> LeadIntelligenceResult:
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
    return _parse_ai_result(raw_output, baseline)


def analyze_lead_intelligence(
    *,
    cleaned_text: str,
    matched_keywords: list[str] | None,
    enable_ai_scoring: bool,
    openai_api_key: str,
    post_timestamp: str | None = None,
    repeated_author_count: int = 0,
    learning_adjustment: dict[str, Any] | None = None,
) -> LeadIntelligenceResult:
    baseline = build_rule_based_intelligence(
        cleaned_text,
        matched_keywords,
        post_timestamp=post_timestamp,
        repeated_author_count=repeated_author_count,
        learning_adjustment=learning_adjustment,
    )
    if enable_ai_scoring and openai_api_key:
        try:
            return score_lead_intelligence_with_ai(openai_api_key, cleaned_text, baseline)
        except Exception as exc:  # noqa: BLE001
            logger.warning("LEAD_INTELLIGENCE_AI_FAILED | error=%s", exc)
    return baseline


def to_dict(result: LeadIntelligenceResult) -> dict[str, Any]:
    return asdict(result)
