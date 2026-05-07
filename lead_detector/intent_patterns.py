from dataclasses import dataclass, field
import re


def normalize_hebrew_text(text: str) -> str:
    collapsed = re.sub(r"\s+", " ", text or "")
    normalized_quotes = (
        collapsed.replace("״", '"')
        .replace("“", '"')
        .replace("”", '"')
        .replace("׳", "'")
        .replace("’", "'")
    )
    return normalized_quotes.strip().lower()


ACCOMMODATION_INTENT_PHRASES = [
    "מחפש מקום",
    "מחפשת מקום",
    "מחפשים מקום",
    "מחפש צימר",
    "מחפשת צימר",
    "מחפשים צימר",
    "מחפשת מקום ללילה",
    "רק לשים את הראש",
    "מחפש איפה לישון",
    "מחפשת איפה לישון",
    "מחפשים איפה לישון",
    "התארחות",
    "דירת נופש",
    "חופשה זוגית",
    "מקום ללילה",
    "מקום ללילה אחד",
]

COUPLE_FAMILY_PHRASES = [
    "מקום לזוג",
    "זוגי",
    "זוג",
    "זוג עם ילד",
    "זוג עם שני ילדים",
    "זוג + 1",
    "זוג + 2",
    "זוג + 3",
    "משפחה קטנה",
    "אני ובעלי",
    "מאורסים",
]

TIMING_PHRASES = [
    "לילה אחד",
    "ללילה",
    "לסופש",
    'לסופ"ש',
    "לסופש הקרוב",
    "ממחר",
    "להיום",
    "למחר",
    "לשבת",
    "לסוף שבוע",
    "שישי שבת",
    "מוצש",
    "מוצ\"ש",
]

LOCATION_PHRASES = [
    "מרכז",
    "אזור המרכז",
    "רחובות",
    "קריית עקרון",
    "נס ציונה",
    "גדרה",
    "ראשון לציון",
    "תל אביב",
    "ירושלים",
    "עד שעה מירושלים",
    "מרכז/ירושלים",
    "מרכז או ירושלים",
    "ליד רחובות",
    "ליד תל אביב",
    "ליד ירושלים",
]

POOL_PRIVACY_PHRASES = [
    "בריכה",
    "בריכה פרטית",
    "פרטיות",
    "מקום שקט",
    "שקט",
    "רגוע",
    "מקום פסטורלי",
    "מקום חמוד",
    "חמוד",
    "רומנטי",
    "פרטיות מלאה",
    "לנקות את הראש",
]

VACATION_PHRASES = [
    "חופשה",
    "לברוח קצת",
    "להתאוורר",
    "שקט",
    "רגוע",
    "פסטורלי",
    "אווירה",
    "רומנטי",
]

PET_FRIENDLY_PHRASES = [
    "עם כלב",
    "עם כלבים",
    "כלב",
    "כלבים",
    "חתול",
    "חיות",
    "pet friendly",
]


@dataclass
class IntentSignals:
    accommodation_intent: list[str] = field(default_factory=list)
    couple_family_signals: list[str] = field(default_factory=list)
    timing_signals: list[str] = field(default_factory=list)
    location_signals: list[str] = field(default_factory=list)
    privacy_pool_signals: list[str] = field(default_factory=list)
    vacation_signals: list[str] = field(default_factory=list)
    pet_signals: list[str] = field(default_factory=list)

    @property
    def pet_friendly_requested(self) -> bool:
        return bool(self.pet_signals)

    @property
    def all_positive_phrases(self) -> list[str]:
        values = (
            self.accommodation_intent
            + self.couple_family_signals
            + self.timing_signals
            + self.location_signals
            + self.privacy_pool_signals
            + self.vacation_signals
        )
        deduped: list[str] = []
        seen: set[str] = set()
        for item in values:
            if item not in seen:
                seen.add(item)
                deduped.append(item)
        return deduped


def _collect_matches(normalized_text: str, phrases: list[str]) -> list[str]:
    return [phrase for phrase in phrases if phrase in normalized_text]


def detect_intent_signals(text: str) -> IntentSignals:
    normalized = normalize_hebrew_text(text)
    return IntentSignals(
        accommodation_intent=_collect_matches(normalized, ACCOMMODATION_INTENT_PHRASES),
        couple_family_signals=_collect_matches(normalized, COUPLE_FAMILY_PHRASES),
        timing_signals=_collect_matches(normalized, TIMING_PHRASES),
        location_signals=_collect_matches(normalized, LOCATION_PHRASES),
        privacy_pool_signals=_collect_matches(normalized, POOL_PRIVACY_PHRASES),
        vacation_signals=_collect_matches(normalized, VACATION_PHRASES),
        pet_signals=_collect_matches(normalized, PET_FRIENDLY_PHRASES),
    )
