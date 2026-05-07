from dataclasses import dataclass, field
import re
from typing import Iterable

from intent_patterns import IntentSignals, detect_intent_signals


def _normalize(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text or "")
    normalized_quotes = (
        collapsed.replace("״", '"')
        .replace("“", '"')
        .replace("”", '"')
        .replace("׳", "'")
        .replace("’", "'")
    )
    return normalized_quotes.strip().lower()


GENERIC_REQUEST_KEYWORDS = [
    "מחפש צימר",
    "מחפשת צימר",
    "מחפשים צימר",
    "צריכים צימר",
    "מחפש מקום",
    "מחפשת מקום",
    "מחפשים מקום",
    "מחפשת מקום ללילה",
    "מקום לזוג",
    "מקום עם בריכה",
    "צימר פנוי",
    "יש פנוי",
    "פנוי היום",
    "פנוי מחר",
    "פנוי לסופש",
    "לסופש הקרוב",
    "לזוג",
    "צימר לזוג",
    "צימר זוגי",
    "צימר רומנטי",
]

PRIVATE_POOL_KEYWORDS = [
    "צימר עם בריכה",
    "בריכה פרטית",
    "צימר עם בריכה פרטית",
    "צימר לזוג עם בריכה",
    "וילה עם בריכה",
    "בריכה רק לנו",
    "מקום עם בריכה",
]

COUPLE_OR_FAMILY_KEYWORDS = [
    "צימר לזוג",
    "צימר זוגי",
    "זוג פלוס ילד",
    "זוג פלוס ילדים",
    "זוג פלוס שתיים",
    "צימר לזוג פלוס",
    "זוג + 1",
    "זוג + 2",
    "זוג עם ילד",
    "זוג עם שני ילדים",
    "זוג + ילד",
    "משפחה קטנה",
    "לזוג",
    "זוגי",
    "זוג",
]

AVAILABILITY_KEYWORDS = [
    "פנוי להיום",
    "פנוי למחר",
    'פנוי לסופ"ש',
    "פנוי לסופש",
    "פנוי לשבת",
    "להיום",
    "למחר",
    "היום בערב",
    "השבת הקרובה",
    "לסופש הקרוב",
    "פנוי היום",
    "פנוי מחר",
    "פנוי לסופש",
    "יש פנוי",
    "צימר פנוי",
    "לילה אחד",
    "ממחר",
]

AREA_KEYWORDS = [
    "צימר במרכז",
    "במרכז",
    "ליד רחובות",
    "רחובות",
    "ליד תל אביב",
    "תל אביב",
    "ליד ירושלים",
    "ירושלים",
    "מרכז הארץ",
    "שפלה",
    "אזור המרכז",
    "קריית עקרון",
    "נס ציונה",
    "גדרה",
    "ראשון לציון",
]

PRIVACY_VACATION_KEYWORDS = [
    "פרטיות",
    "שקט",
    "רגוע",
    "חמוד",
    "מקום חמוד",
    "מקום שקט",
    "חופשה",
    "לברוח קצת",
]

EXCLUDE_KEYWORDS = [
    "מפרסם צימר",
    "יש לי צימר",
    "הצימר שלי",
    "בעל צימר",
    "בעלי צימרים",
    "דרושים צימרים",
    "פרסום צימרים",
    "מוזמנים אלינו",
    "נשמח לארח",
    "אצלנו בצימר",
    "נותרו תאריכים",
    "נשארו תאריכים",
    "מבצע",
    "הנחה",
]

NEGATIVE_REQUEST_KEYWORDS = [
    "לאירוע",
    "למסיבה",
    "יום הולדת",
    "וילה למסיבה",
    "אירוע",
    "על האש",
    "מנגל",
    "מנגל בלבד",
    "על האש בלבד",
    "10 אנשים",
    "15 אנשים",
    "20 אנשים",
    "קבוצת בנות",
    "קבוצת חברים",
    "קבוצה גדולה",
]


@dataclass
class MatchResult:
    is_relevant: bool
    score: int
    matched_keywords: list[str] = field(default_factory=list)
    matched_buckets: list[str] = field(default_factory=list)
    matched_negative_keywords: list[str] = field(default_factory=list)
    matched_owner_keywords: list[str] = field(default_factory=list)
    rejection_reason: str | None = None
    intent_score: int = 0
    intent_reasons: list[str] = field(default_factory=list)
    urgency_reasons: list[str] = field(default_factory=list)
    couple_family_reasons: list[str] = field(default_factory=list)
    pet_friendly_requested: bool = False
    why_detected_he: str | None = None


def _collect_matches(text: str, keywords: Iterable[str]) -> list[str]:
    normalized = _normalize(text)
    return [keyword for keyword in keywords if keyword in normalized]


def _build_why_detected(
    intent_signals: IntentSignals,
    accommodation_reasons: list[str] | None = None,
) -> str:
    reasons: list[str] = []
    if accommodation_reasons:
        reasons.append(f"כוונת אירוח: {', '.join(accommodation_reasons[:3])}")
    if intent_signals.couple_family_signals:
        reasons.append(f"זוג/משפחה: {', '.join(intent_signals.couple_family_signals[:3])}")
    if intent_signals.timing_signals:
        reasons.append(f"תזמון: {', '.join(intent_signals.timing_signals[:3])}")
    if intent_signals.privacy_pool_signals:
        reasons.append(f"פרטיות/בריכה: {', '.join(intent_signals.privacy_pool_signals[:3])}")
    if intent_signals.location_signals:
        reasons.append(f"אזור: {', '.join(intent_signals.location_signals[:3])}")
    if intent_signals.pet_signals:
        reasons.append(f"חיות מחמד: {', '.join(intent_signals.pet_signals[:2])}")
    return " | ".join(reasons) if reasons else "זוהתה כוונת חיפוש למקום אירוח."


def classify_post(text: str, min_score: int = 5) -> MatchResult:
    normalized = _normalize(text)
    if not normalized:
        return MatchResult(False, 0, rejection_reason="empty_text")

    exclude_matches = _collect_matches(normalized, EXCLUDE_KEYWORDS)
    if exclude_matches:
        return MatchResult(
            is_relevant=False,
            score=0,
            matched_keywords=exclude_matches,
            matched_owner_keywords=exclude_matches,
            rejection_reason="owner_or_advertiser_wording",
        )

    negative_matches = _collect_matches(normalized, NEGATIVE_REQUEST_KEYWORDS)
    if negative_matches:
        return MatchResult(
            is_relevant=False,
            score=0,
            matched_negative_keywords=negative_matches,
            rejection_reason="unsuitable_event_request",
        )

    intent_signals = detect_intent_signals(normalized)
    intent_score = 0
    matched_keywords: list[str] = []
    matched_buckets: list[str] = []

    scoring_buckets = [
        ("private_pool", PRIVATE_POOL_KEYWORDS, 3),
        ("couple_or_family", COUPLE_OR_FAMILY_KEYWORDS, 3),
        ("availability", AVAILABILITY_KEYWORDS, 3),
        ("area", AREA_KEYWORDS, 3),
        ("generic_request", GENERIC_REQUEST_KEYWORDS, 2),
        ("privacy_vacation", PRIVACY_VACATION_KEYWORDS, 2),
    ]

    for bucket_name, keywords, bucket_score in scoring_buckets:
        matches = _collect_matches(normalized, keywords)
        if matches:
            intent_score += bucket_score
            matched_buckets.append(bucket_name)
            matched_keywords.extend(matches)

    accommodation_reasons = list(intent_signals.accommodation_intent)
    soft_accommodation_markers = ["מקום", "וילה", "דירת נופש", "לילה", "לישון", "להתארח", "חופשה"]
    has_support_signal = any(
        (
            intent_signals.couple_family_signals,
            intent_signals.timing_signals,
            intent_signals.location_signals,
            intent_signals.privacy_pool_signals,
            intent_signals.vacation_signals,
        )
    )
    if not accommodation_reasons and has_support_signal and any(marker in normalized for marker in soft_accommodation_markers):
        accommodation_reasons.append("כוונת אירוח משתמעת")

    if accommodation_reasons:
        intent_score += 4
        matched_buckets.append("accommodation_intent")
    if intent_signals.couple_family_signals:
        intent_score += 3
    if intent_signals.timing_signals:
        intent_score += 3
    if intent_signals.location_signals:
        intent_score += 3
    if intent_signals.privacy_pool_signals:
        intent_score += 2
    if intent_signals.vacation_signals:
        intent_score += 2
    if any("בריכה" in phrase for phrase in intent_signals.privacy_pool_signals):
        intent_score += 1

    matched_keywords.extend(intent_signals.all_positive_phrases)
    deduped_keywords = sorted(set(matched_keywords))
    is_relevant = bool(accommodation_reasons) and has_support_signal and intent_score >= min_score
    why_detected_he = _build_why_detected(intent_signals, accommodation_reasons)
    return MatchResult(
        is_relevant=is_relevant,
        score=intent_score,
        matched_keywords=deduped_keywords,
        matched_buckets=sorted(set(matched_buckets)),
        matched_negative_keywords=negative_matches,
        rejection_reason=None if is_relevant else "intent_score_below_threshold",
        intent_score=intent_score,
        intent_reasons=accommodation_reasons + intent_signals.privacy_pool_signals + intent_signals.vacation_signals + intent_signals.location_signals,
        urgency_reasons=intent_signals.timing_signals,
        couple_family_reasons=intent_signals.couple_family_signals,
        pet_friendly_requested=intent_signals.pet_friendly_requested,
        why_detected_he=why_detected_he,
    )


def score_label(score: int) -> str:
    return "גבוהה" if score >= 8 else "בינונית"
