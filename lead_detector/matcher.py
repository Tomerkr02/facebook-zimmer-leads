from dataclasses import dataclass, field
import re
from typing import Iterable


def _normalize(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text or "")
    return collapsed.strip().lower()


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
]

AVAILABILITY_KEYWORDS = [
    "פנוי להיום",
    "פנוי למחר",
    "פנוי לסופ\"ש",
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
    "פרטיות",
    "שקט",
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
    "עד 20 איש",
    "קבוצת בנות",
    "קבוצת חברים",
    "מנגל בלבד",
    "על האש בלבד",
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


def _collect_matches(text: str, keywords: Iterable[str]) -> list[str]:
    normalized = _normalize(text)
    return [keyword for keyword in keywords if keyword in normalized]


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

    score = 0
    matched_keywords: list[str] = []
    matched_buckets: list[str] = []

    scoring_buckets = [
        ("private_pool", PRIVATE_POOL_KEYWORDS, 5),
        ("couple_or_family", COUPLE_OR_FAMILY_KEYWORDS, 4),
        ("availability", AVAILABILITY_KEYWORDS, 3),
        ("area", AREA_KEYWORDS, 3),
        ("generic_request", GENERIC_REQUEST_KEYWORDS, 2),
    ]

    for bucket_name, keywords, bucket_score in scoring_buckets:
        matches = _collect_matches(normalized, keywords)
        if matches:
            score += bucket_score
            matched_buckets.append(bucket_name)
            matched_keywords.extend(matches)

    deduped_keywords = sorted(set(matched_keywords))
    is_relevant = score >= min_score and bool(deduped_keywords)
    return MatchResult(
        is_relevant=is_relevant,
        score=score,
        matched_keywords=deduped_keywords,
        matched_buckets=matched_buckets,
        matched_negative_keywords=negative_matches,
        rejection_reason=None if is_relevant else "score_below_threshold",
    )


def score_label(score: int) -> str:
    return "גבוהה" if score >= 8 else "בינונית"
