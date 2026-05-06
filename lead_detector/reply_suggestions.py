from matcher import AREA_KEYWORDS, AVAILABILITY_KEYWORDS, COUPLE_OR_FAMILY_KEYWORDS, PRIVATE_POOL_KEYWORDS


def _contains_any(text: str, keywords: list[str]) -> bool:
    normalized = (text or "").strip().lower()
    return any(keyword in normalized for keyword in keywords)


def generate_reply_suggestion(
    *,
    cleaned_text: str,
    ai_category: str | None,
    matched_keywords: list[str] | None,
) -> str:
    normalized = (cleaned_text or "").strip().lower()
    keyword_text = " ".join(matched_keywords or []).lower()
    full_text = f"{normalized} {keyword_text}".strip()

    if ai_category == "urgent_today" or _contains_any(full_text, AVAILABILITY_KEYWORDS):
        return (
            "היי 🙂 אם עדיין רלוונטי להיום או למחר, אפשר לבדוק לך זמינות אצלנו ב-Royal Water Villa "
            "בקריית עקרון. יש מתחם פרטי ושקט עם בריכה פרטית שמתאים לזוג או למשפחה קטנה."
        )

    if ai_category == "couple_with_kids" or "ילד" in full_text or "זוג + 1" in full_text or "זוג + 2" in full_text:
        return (
            "היי 🙂 אם עדיין רלוונטי, יש לנו סוויטה פרטית בקריית עקרון עם בריכה פרטית, "
            "מתאימה לזוג עם ילד או שניים. מקום שקט, פרטי ונעים במרכז. לשלוח לך פרטים?"
        )

    if ai_category == "private_pool" or _contains_any(full_text, PRIVATE_POOL_KEYWORDS):
        return (
            "היי 🙂 אם עדיין רלוונטי, יש לנו סוויטה פרטית בקריית עקרון עם בריכה פרטית, "
            "מתאימה מאוד לזוג שמחפש שקט ופרטיות. אפשר לשלוח לך פרטים ותמונות?"
        )

    if ai_category == "location_match" or _contains_any(full_text, AREA_KEYWORDS):
        return (
            "היי 🙂 אם עדיין רלוונטי, יש לנו מתחם פרטי בקריית עקרון ליד רחובות, "
            "נוח מאוד למי שמחפש אירוח שקט במרכז עם בריכה פרטית. לשלוח לך פרטים?"
        )

    if ai_category == "weekend":
        return (
            "היי 🙂 אם עדיין רלוונטי לסופ\"ש, יש לנו סוויטה פרטית בקריית עקרון עם בריכה פרטית, "
            "מתאימה לזוג או למשפחה קטנה שמחפשים שקט ופרטיות. רוצה שאשלח פרטים?"
        )

    if ai_category == "couple" or _contains_any(full_text, COUPLE_OR_FAMILY_KEYWORDS):
        return (
            "היי 🙂 אם עדיין רלוונטי, יש לנו סוויטה פרטית בקריית עקרון עם בריכה פרטית, "
            "מתאימה מאוד לזוג שמחפש שקט ופרטיות. אפשר לשלוח לך פרטים ותמונות?"
        )

    return (
        "היי 🙂 אם עדיין רלוונטי, יש לנו מתחם פרטי בקריית עקרון עם בריכה פרטית, "
        "מתאים לזוג או למשפחה קטנה שמחפשים שקט ופרטיות במרכז. לשלוח לך פרטים?"
    )


def run_example_checks() -> list[tuple[str, str]]:
    examples = [
        (
            "מחפשת צימר לזוג עם בריכה פרטית במרכז להיום",
            generate_reply_suggestion(
                cleaned_text="מחפשת צימר לזוג עם בריכה פרטית במרכז להיום",
                ai_category="urgent_today",
                matched_keywords=["מחפשת צימר", "בריכה פרטית"],
            ),
        ),
        (
            "מחפשים צימר לזוג פלוס ילד ליד רחובות",
            generate_reply_suggestion(
                cleaned_text="מחפשים צימר לזוג פלוס ילד ליד רחובות",
                ai_category="couple_with_kids",
                matched_keywords=["זוג פלוס ילד", "ליד רחובות"],
            ),
        ),
    ]
    return examples


if __name__ == "__main__":
    for raw_text, suggestion in run_example_checks():
        print("POST:", raw_text)
        print("SUGGESTION:", suggestion)
        print("---")
