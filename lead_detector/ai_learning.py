import math
import re
from typing import Any

from storage import LeadStorage


HEBREW_TOKEN_RE = re.compile(r"[\u0590-\u05FF\"'׳״]+")
EMPHASIS_TOKENS = {"חובה", "חייב", "חייבת", "רק", "בלבד", "עדיפות", "רצוי"}
DOMAIN_TOKENS = {
    "בריכה",
    "פרטית",
    "זוג",
    "פרטיות",
    "שקט",
    "רומנטי",
    "דתיים",
    "מוצש",
    "שבת",
    "מנגל",
    "מסיבה",
    "כלבים",
    "כלב",
}


class AILearningEngine:
    def __init__(self, storage: LeadStorage):
        self.storage = storage

    def record_review_feedback(
        self,
        *,
        lead_id: int,
        feedback_type: str,
        reviewer: str = "tomer",
        feedback_reason: str | None = None,
    ) -> None:
        self.storage.add_lead_feedback(
            lead_id,
            feedback_type,
            feedback_reason=feedback_reason,
            reviewer=reviewer,
        )
        lead = self.storage.get_lead(lead_id)
        if not lead:
            return
        seen_at = str(lead.get("feedback_at") or lead.get("updated_at") or lead.get("created_at") or "")
        deltas = self._feedback_deltas(feedback_type)
        for signal_type, signal_value in self.extract_signals(lead):
            previous = self.storage.get_ai_memory_signal(signal_type, signal_value)
            confidence = self._next_confidence(previous, deltas)
            self.storage.upsert_ai_memory_signal(
                signal_type=signal_type,
                signal_value=signal_value,
                positive_delta=deltas["positive"],
                negative_delta=deltas["negative"],
                vip_delta=deltas["vip"],
                last_seen=seen_at,
                confidence_score=confidence,
            )

    def enrich_review_leads(self, leads: list[dict[str, Any]]) -> list[dict[str, Any]]:
        enriched: list[dict[str, Any]] = []
        for lead in leads:
            item = dict(lead)
            confidence_values: list[float] = []
            positive_hits = 0
            negative_hits = 0
            new_signal_hits = 0
            for signal_type, signal_value in self.extract_signals(item):
                memory = self.storage.get_ai_memory_signal(signal_type, signal_value)
                if not memory:
                    new_signal_hits += 1
                    continue
                confidence_values.append(float(memory.get("confidence_score") or 0))
                if int(memory.get("positive_count") or 0) + int(memory.get("vip_count") or 0) > int(memory.get("negative_count") or 0):
                    positive_hits += 1
                if int(memory.get("negative_count") or 0) > int(memory.get("positive_count") or 0) + int(memory.get("vip_count") or 0):
                    negative_hits += 1
            avg_confidence = sum(confidence_values) / len(confidence_values) if confidence_values else 0.0
            item["ai_memory_confidence"] = round(avg_confidence, 3)
            item["review_priority_score"] = self._review_priority_score(
                lead=item,
                ai_confidence=avg_confidence,
                positive_hits=positive_hits,
                negative_hits=negative_hits,
                new_signal_hits=new_signal_hits,
            )
            item["review_priority_reason"] = self._review_priority_reason(
                lead=item,
                ai_confidence=avg_confidence,
                positive_hits=positive_hits,
                negative_hits=negative_hits,
                new_signal_hits=new_signal_hits,
            )
            enriched.append(item)
        enriched.sort(key=lambda lead: (lead.get("review_priority_score") or 0, lead.get("created_at") or ""), reverse=True)
        return enriched

    def learning_analytics(self) -> dict[str, Any]:
        return self.storage.learning_analytics()

    def extract_signals(self, lead: dict[str, Any]) -> list[tuple[str, str]]:
        signals: set[tuple[str, str]] = set()
        for keyword in lead.get("matched_keywords_list") or []:
            signals.add(("matched_keyword", self._normalize_value(keyword)))
        for reason in lead.get("intent_reasons_list") or []:
            signals.add(("intent_reason", self._normalize_value(reason)))
        for reason in lead.get("urgency_reasons_list") or []:
            signals.add(("urgency_reason", self._normalize_value(reason)))
        for reason in lead.get("bad_fit_reasons_list") or []:
            signals.add(("bad_fit_reason", self._normalize_value(reason)))
        for field_name in ("lead_type", "guest_type", "requested_area", "pool_intent", "privacy_intent", "budget_signal"):
            value = lead.get(field_name)
            if value:
                signals.add((field_name, self._normalize_value(str(value))))
        for phrase in self._extract_text_phrases(str(lead.get("cleaned_text") or lead.get("post_text") or "")):
            signals.add(("phrase", phrase))
        return [item for item in signals if item[1]]

    def _extract_text_phrases(self, text: str) -> list[str]:
        tokens = [match.group(0).strip("\"'׳״").lower() for match in HEBREW_TOKEN_RE.finditer(text)]
        phrases: set[str] = set()
        for index, token in enumerate(tokens):
            if token in EMPHASIS_TOKENS or token in DOMAIN_TOKENS:
                for window in (1, 2, 3):
                    start = max(0, index - window)
                    end = min(len(tokens), index + window + 1)
                    phrase = " ".join(tokens[start:end]).strip()
                    if phrase and len(phrase) >= 3:
                        phrases.add(self._normalize_value(phrase))
        return sorted(phrases)

    def _feedback_deltas(self, feedback_type: str) -> dict[str, int]:
        if feedback_type in {"good_lead", "closed_successfully"}:
            return {"positive": 1, "negative": 0, "vip": 0}
        if feedback_type == "perfect_match":
            return {"positive": 1, "negative": 0, "vip": 1}
        return {"positive": 0, "negative": 1, "vip": 0}

    def _next_confidence(self, previous: dict[str, Any] | None, deltas: dict[str, int]) -> float:
        positive = int(previous.get("positive_count") or 0) if previous else 0
        negative = int(previous.get("negative_count") or 0) if previous else 0
        vip = int(previous.get("vip_count") or 0) if previous else 0
        positive += deltas["positive"]
        negative += deltas["negative"]
        vip += deltas["vip"]
        weighted_positive = positive + (vip * 1.5)
        total = weighted_positive + negative
        if total <= 0:
            return 0.0
        raw_score = weighted_positive / total
        sample_factor = min(1.0, math.log(total + 1, 5))
        confidence = max(0.0, min(1.0, raw_score * sample_factor))
        return round(confidence, 3)

    def _review_priority_score(
        self,
        *,
        lead: dict[str, Any],
        ai_confidence: float,
        positive_hits: int,
        negative_hits: int,
        new_signal_hits: int,
    ) -> int:
        score = 0
        if ai_confidence < 0.35:
            score += 5
        elif ai_confidence < 0.55:
            score += 3
        if new_signal_hits:
            score += min(4, new_signal_hits)
        if positive_hits and negative_hits:
            score += 4
        if (lead.get("urgency") or "") in {"today", "tomorrow", "weekend", "shabbat"}:
            score += 3
        if int(lead.get("heat_score") or 0) >= 7:
            score += 2
        if int(lead.get("fit_score") or 0) in {4, 5, 6}:
            score += 2
        if lead.get("vip_match"):
            score += 1
        return score

    def _review_priority_reason(
        self,
        *,
        lead: dict[str, Any],
        ai_confidence: float,
        positive_hits: int,
        negative_hits: int,
        new_signal_hits: int,
    ) -> str:
        reasons: list[str] = []
        if ai_confidence < 0.35:
            reasons.append("AI confidence נמוך")
        if new_signal_hits:
            reasons.append("signals חדשים ללמידה")
        if positive_hits and negative_hits:
            reasons.append("signals סותרים")
        if (lead.get("urgency") or "") in {"today", "tomorrow", "weekend", "shabbat"}:
            reasons.append("דחיפות גבוהה")
        if not reasons:
            reasons.append("ליד רגיל לבדיקה")
        return " | ".join(reasons)

    @staticmethod
    def _normalize_value(value: str) -> str:
        return " ".join(value.strip().lower().split())
