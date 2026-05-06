import logging
import random
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Browser, BrowserContext, ElementHandle, sync_playwright

from ai_scorer import AIScoreResult, is_text_reasonable_for_ai, score_post_with_ai
from config import load_settings
from matcher import MatchResult, classify_post
from reply_suggestions import generate_reply_suggestion
from storage import LeadStorage
from telegram import build_alert_message, send_message


logger = logging.getLogger(__name__)


POST_EXTRACTION_SCRIPT = """
(node) => {
  const clean = (value) => (value || "").replace(/\\s+/g, " ").trim();
  const bodySelectors = [
    "[data-ad-comet-preview='message']",
    "[data-ad-preview='message']",
    "div[dir='auto']",
    "span[dir='auto']",
  ];
  const bodyNodes = [];
  for (const selector of bodySelectors) {
    for (const element of node.querySelectorAll(selector)) {
      if (!bodyNodes.includes(element)) {
        bodyNodes.push(element);
      }
    }
  }

  const bodyTexts = bodyNodes
    .map((element) => clean(element.innerText || element.textContent || ""))
    .filter((value) => value.length >= 8);

  const text = bodyTexts.length
    ? bodyTexts.sort((left, right) => right.length - left.length)[0]
    : clean(node.innerText || "");
  const anchors = Array.from(node.querySelectorAll("a[href]"));
  const hrefs = anchors.map((anchor) => anchor.href).filter(Boolean);
  const permalink = hrefs.find((href) =>
    /facebook\\.com\\/.+\\/(posts|permalink)\\//.test(href) ||
    /facebook\\.com\\/groups\\/[^/]+\\/user\\//.test(href) ||
    /facebook\\.com\\/groups\\/[^/]+\\/posts\\//.test(href)
  ) || null;

  const timeNode = node.querySelector("a[aria-label] span, a[aria-label], span[aria-label], time");
  const timestamp = clean(
    (timeNode && (timeNode.getAttribute("aria-label") || timeNode.getAttribute("datetime") || timeNode.textContent)) || ""
  );

  const headingCandidates = Array.from(node.querySelectorAll("h2, h3, strong, span"));
  let author = "";
  for (const candidate of headingCandidates) {
    const value = clean(candidate.textContent || "");
    if (!value) continue;
    if (text.startsWith(value) || value.split(" ").length <= 5) {
      author = value;
      break;
    }
  }

  return {
    text,
    author,
    timestamp,
    post_url: permalink,
  };
}
"""


@dataclass
class PostCandidate:
    post_key: str
    post_url: str | None
    author_name: str | None
    timestamp: str | None
    raw_text: str
    text: str
    match: MatchResult
    ai_result: AIScoreResult | None = None
    lead_id: int | None = None


@dataclass(frozen=True)
class GroupTarget:
    url: str
    display_name: str


@dataclass
class GroupScanStats:
    group_name: str
    group_url: str
    scanned: int = 0
    matched: int = 0
    alerts_sent: int = 0


def human_delay(min_seconds: float, max_seconds: float) -> None:
    time.sleep(random.uniform(min_seconds, max_seconds))


def normalize_facebook_url(url: str) -> str:
    cleaned = (url or "").strip()
    if not cleaned:
        return ""

    parsed = urlsplit(cleaned)
    path = parsed.path.rstrip("/") or parsed.path or "/"

    if parsed.netloc.endswith("facebook.com"):
        return urlunsplit(("https", "www.facebook.com", path, "", ""))

    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def build_text_preview(text: str, limit: int = 80) -> str:
    collapsed = re.sub(r"\s+", " ", text or "").strip()
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit].rstrip()}..."


def collapse_repeated_words(text: str) -> str:
    words = re.split(r"(\s+)", text)
    collapsed: list[str] = []
    previous_word_key = None

    for token in words:
        if not token:
            continue
        if token.isspace():
            if collapsed and not collapsed[-1].isspace():
                collapsed.append(" ")
            continue

        normalized = re.sub(r"[^\w\u0590-\u05FF]+", "", token, flags=re.UNICODE).lower()
        if normalized and normalized == previous_word_key:
            continue

        collapsed.append(token)
        previous_word_key = normalized or None

    return "".join(collapsed).strip()


def clean_post_text(text: str) -> str:
    footer_patterns = (
        "write a public comment",
        "like",
        "comment",
        "share",
    )
    junk_phrases = {
        "facebook",
        "see translation",
        "write a public comment",
        "contributor",
        "like",
        "comment",
        "share",
        "follow",
        "reels",
        "sponsored",
    }

    normalized_text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    normalized_text = re.sub(r"\n{3,}", "\n\n", normalized_text)

    cleaned_lines: list[str] = []
    for raw_line in normalized_text.split("\n"):
        line = re.sub(r"\s+", " ", raw_line).strip()
        if not line:
            continue

        lower_line = line.lower()
        if any(pattern in lower_line for pattern in footer_patterns):
            break

        if lower_line in junk_phrases:
            continue

        line = collapse_repeated_words(line)
        line = re.sub(r"\s+", " ", line).strip()
        if not line:
            continue

        if len(line) < 3 and not line.isnumeric():
            continue

        if line.lower() in junk_phrases:
            continue

        cleaned_lines.append(line)

    cleaned_text = "\n".join(cleaned_lines)
    cleaned_text = collapse_repeated_words(cleaned_text)
    cleaned_text = re.sub(r"[ \t]+", " ", cleaned_text)
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text)
    return cleaned_text.strip()


def build_post_key(post_url: str | None, text: str) -> str:
    if post_url:
        patterns = [
            r"/groups/(\d+)/posts/(\d+)",
            r"/permalink/(\d+)",
            r"/groups/(\d+)/user/(\d+)",
            r"story_fbid=(\d+)",
        ]
        for pattern in patterns:
            match = re.search(pattern, post_url)
            if match:
                return ":".join(match.groups())
        return post_url
    return LeadStorage.build_text_hash(text)


def ensure_logged_in_context(browser: Browser, storage_state_path: str) -> BrowserContext:
    logger.info("Opening Facebook context using storage state: %s", storage_state_path)
    return browser.new_context(
        storage_state=storage_state_path,
        locale="he-IL",
        viewport={"width": 1440, "height": 1200},
    )


def detect_group_name(page, group_url: str) -> str:
    selectors = [
        "h1",
        "[role='main'] h2",
        "[role='main'] h1",
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector).first
            if locator.count():
                text = locator.inner_text(timeout=3000).strip()
                if text:
                    return build_text_preview(text, limit=120)
        except Exception:  # noqa: BLE001
            continue

    match = re.search(r"/groups/([^/?#]+)", group_url)
    if match:
        return f"Group {match.group(1)}"
    return group_url


def collect_post_handles(page, max_scrolls: int, max_posts: int, min_delay: float, max_delay: float) -> list[ElementHandle]:
    seen_handles: list[ElementHandle] = []
    seen_ids: set[int] = set()
    selectors = [
        "[role='article']",
        "div[data-pagelet*='FeedUnit']",
        "div[aria-posinset]",
    ]

    for scroll_index in range(max_scrolls):
        logger.info("Scanning visible posts (scroll %s/%s)...", scroll_index + 1, max_scrolls)
        for selector in selectors:
            handles = page.locator(selector).element_handles()
            for handle in handles:
                handle_id = id(handle)
                if handle_id not in seen_ids:
                    seen_ids.add(handle_id)
                    seen_handles.append(handle)
                    if len(seen_handles) >= max_posts:
                        return seen_handles[:max_posts]

        page.mouse.wheel(0, random.randint(1200, 2200))
        human_delay(min_delay, max_delay)

    return seen_handles[:max_posts]


def extract_post_candidate(handle: ElementHandle, min_score: int) -> PostCandidate | None:
    raw_post = handle.evaluate(POST_EXTRACTION_SCRIPT)
    raw_text = (raw_post.get("text") or "").strip()
    if len(raw_text) < 20:
        return None

    logger.debug("RAW_TEXT_PREVIEW | %s", build_text_preview(raw_text))

    text = clean_post_text(raw_text)
    logger.debug("CLEAN_TEXT_PREVIEW | %s", build_text_preview(text))
    if len(text) < 20:
        return None

    match = classify_post(text, min_score=min_score)
    raw_url = (raw_post.get("post_url") or "").strip()
    post_url = normalize_facebook_url(raw_url) or None
    post_key = build_post_key(post_url, text)

    return PostCandidate(
        post_key=post_key,
        post_url=post_url,
        author_name=(raw_post.get("author") or "").strip() or None,
        timestamp=(raw_post.get("timestamp") or "").strip() or None,
        raw_text=raw_text,
        text=text,
        match=match,
    )


def maybe_apply_ai_scoring(settings, candidate: PostCandidate, preview: str, group_name: str) -> None:
    if not settings.enable_ai_scoring:
        return

    if not settings.openai_api_key:
        logger.warning(
            "AI_SCORING_SKIPPED | group=%s | key=%s | reason=missing_openai_api_key | preview=%s",
            group_name,
            candidate.post_key,
            preview,
        )
        return

    if not is_text_reasonable_for_ai(candidate.text):
        logger.info(
            "AI_SCORING_SKIPPED | group=%s | key=%s | reason=text_length_not_reasonable | preview=%s",
            group_name,
            candidate.post_key,
            preview,
        )
        return

    try:
        candidate.ai_result = score_post_with_ai(
            api_key=settings.openai_api_key,
            post_text=candidate.text,
        )
        logger.info(
            "AI_SCORE_RESULT | group=%s | key=%s | ai_score=%s | category=%s | relevant=%s | preview=%s",
            group_name,
            candidate.post_key,
            candidate.ai_result.score,
            candidate.ai_result.category,
            candidate.ai_result.is_relevant,
            preview,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "AI_SCORING_FAILED | group=%s | key=%s | error=%s | preview=%s",
            group_name,
            candidate.post_key,
            exc,
            preview,
        )


def infer_category(candidate: PostCandidate) -> str:
    matched = set(candidate.match.matched_buckets)
    if candidate.ai_result and candidate.ai_result.category:
        return candidate.ai_result.category
    if "availability" in matched:
        return "urgent_today"
    if "private_pool" in matched:
        return "private_pool"
    if "couple_or_family" in matched and any("ילד" in keyword for keyword in candidate.match.matched_keywords):
        return "couple_with_kids"
    if "couple_or_family" in matched:
        return "couple"
    if "area" in matched:
        return "location_match"
    return "not_relevant"


def infer_reason_he(candidate: PostCandidate, group_name: str) -> str:
    if candidate.ai_result and candidate.ai_result.reason_he:
        return candidate.ai_result.reason_he
    keywords = ", ".join(candidate.match.matched_keywords[:4]) or "מילות מפתח רלוונטיות"
    return f"זוהה כליד מתאים מתוך הקבוצה {group_name} לפי: {keywords}."


def is_ai_rejected(settings, candidate: PostCandidate, preview: str, group_name: str) -> bool:
    if not settings.enable_ai_scoring or not candidate.ai_result:
        return False

    if candidate.ai_result.is_relevant and candidate.ai_result.score >= settings.ai_min_score:
        return False

    logger.info(
        "REJECTED_POST | group=%s | key=%s | score=%s | reason=ai_rejected | ai_score=%s | category=%s | url=%s | preview=%s",
        group_name,
        candidate.post_key,
        candidate.match.score,
        candidate.ai_result.score,
        candidate.ai_result.category,
        candidate.post_url or "-",
        preview,
    )
    return True


def scan_single_group(page, settings, storage: LeadStorage, group_url: str) -> GroupScanStats:
    logger.info("GROUP_SCAN_START | url=%s", group_url)
    page.goto(group_url, wait_until="domcontentloaded", timeout=90000)
    human_delay(settings.min_delay_seconds, settings.max_delay_seconds)

    group_name = detect_group_name(page, group_url)
    stats = GroupScanStats(group_name=group_name, group_url=group_url)
    logger.info("GROUP_CONTEXT | name=%s | url=%s", group_name, group_url)

    post_handles = collect_post_handles(
        page=page,
        max_scrolls=settings.max_scrolls,
        max_posts=settings.max_posts,
        min_delay=settings.min_delay_seconds,
        max_delay=settings.max_delay_seconds,
    )

    logger.info(
        "GROUP_POST_CONTAINERS | name=%s | url=%s | count=%s",
        group_name,
        group_url,
        len(post_handles),
    )

    for handle in post_handles:
        try:
            candidate = extract_post_candidate(handle, min_score=settings.min_score)
        except Exception as exc:  # noqa: BLE001
            logger.warning("POST_EXTRACTION_FAILED | group=%s | error=%s", group_name, exc)
            continue

        if not candidate:
            continue

        stats.scanned += 1
        preview = build_text_preview(candidate.text)

        if storage.has_seen(candidate.post_key, candidate.post_url, candidate.text):
            logger.info(
                "DUPLICATE_SKIPPED | group=%s | key=%s | url=%s | preview=%s",
                group_name,
                candidate.post_key,
                candidate.post_url or "-",
                preview,
            )
            continue

        storage.mark_seen(
            post_key=candidate.post_key,
            post_url=candidate.post_url,
            text=candidate.text,
            author_name=candidate.author_name,
        )

        if not candidate.match.is_relevant:
            logger.info(
                "REJECTED_POST | group=%s | key=%s | score=%s | reason=%s | url=%s | preview=%s",
                group_name,
                candidate.post_key,
                candidate.match.score,
                candidate.match.rejection_reason or "-",
                candidate.post_url or "-",
                preview,
            )
            continue

        maybe_apply_ai_scoring(settings, candidate, preview, group_name)
        if is_ai_rejected(settings, candidate, preview, group_name):
            continue

        ai_category = infer_category(candidate)
        ai_reason_he = infer_reason_he(candidate, group_name)
        suggested_reply_he = (
            candidate.ai_result.suggested_reply_he
            if candidate.ai_result and candidate.ai_result.suggested_reply_he
            else generate_reply_suggestion(
                cleaned_text=candidate.text,
                ai_category=ai_category,
                matched_keywords=candidate.match.matched_keywords,
            )
        )

        candidate.lead_id = storage.save_lead(
            source="facebook",
            group_name=group_name,
            group_url=group_url,
            author=candidate.author_name,
            post_url=candidate.post_url,
            post_text=candidate.raw_text,
            cleaned_text=candidate.text,
            matched_keywords=candidate.match.matched_keywords,
            keyword_score=candidate.match.score,
            ai_score=candidate.ai_result.score if candidate.ai_result else None,
            ai_category=ai_category,
            ai_reason_he=ai_reason_he,
            suggested_reply_he=suggested_reply_he,
            status="new",
            sent_to_telegram=0,
        )

        stats.matched += 1
        logger.info(
            "MATCHED_LEAD | group=%s | lead_id=%s | key=%s | score=%s | ai_score=%s | keywords=%s | url=%s | preview=%s",
            group_name,
            candidate.lead_id,
            candidate.post_key,
            candidate.match.score,
            candidate.ai_result.score if candidate.ai_result else "-",
            ",".join(candidate.match.matched_keywords) or "-",
            candidate.post_url or "-",
            preview,
        )
        message = build_alert_message(
            lead_id=candidate.lead_id,
            status="new",
            match_result=candidate.match,
            post_text=candidate.text,
            author_name=candidate.author_name,
            post_url=candidate.post_url,
            group_name=group_name,
            group_url=group_url,
            ai_reason_he=ai_reason_he,
            suggested_reply_he=suggested_reply_he,
            ai_category=ai_category,
            ai_score=candidate.ai_result.score if candidate.ai_result else None,
            ai_result=candidate.ai_result,
        )
        if send_message(settings.telegram_bot_token, settings.telegram_chat_id, message):
            stats.alerts_sent += 1
            storage.mark_lead_telegram_sent(candidate.lead_id)
            logger.info(
                "ALERT_SENT | group=%s | lead_id=%s | key=%s | score=%s | url=%s | preview=%s",
                group_name,
                candidate.lead_id,
                candidate.post_key,
                candidate.match.score,
                candidate.post_url or "-",
                preview,
            )

        human_delay(settings.min_delay_seconds, settings.max_delay_seconds)

    logger.info(
        "GROUP_SCAN_DONE | name=%s | scanned=%s | matched=%s | alerts=%s",
        stats.group_name,
        stats.scanned,
        stats.matched,
        stats.alerts_sent,
    )
    return stats


def resolve_group_targets(settings) -> list[GroupTarget]:
    urls = settings.facebook_group_urls
    if settings.group_scan_limit > 0:
        urls = urls[: settings.group_scan_limit]
    return [GroupTarget(url=url, display_name=url) for url in urls]


def scrape_group_posts() -> None:
    settings = load_settings()
    if not settings.facebook_group_urls:
        raise ValueError("FACEBOOK_GROUP_URLS is required.")
    if not settings.facebook_storage_state_path.exists():
        raise FileNotFoundError(
            f"Facebook storage state not found: {settings.facebook_storage_state_path}"
        )

    storage = LeadStorage(settings.database_path)
    group_targets = resolve_group_targets(settings)
    group_stats: list[GroupScanStats] = []

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        context = ensure_logged_in_context(browser, str(settings.facebook_storage_state_path))
        page = context.new_page()

        for index, group_target in enumerate(group_targets, start=1):
            logger.info(
                "GROUP_SCAN_QUEUE | index=%s/%s | url=%s",
                index,
                len(group_targets),
                group_target.url,
            )
            try:
                stats = scan_single_group(
                    page=page,
                    settings=settings,
                    storage=storage,
                    group_url=group_target.url,
                )
                group_stats.append(stats)
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "GROUP_SCAN_FAILED | url=%s | error=%s",
                    group_target.url,
                    exc,
                )
                group_stats.append(
                    GroupScanStats(
                        group_name=group_target.display_name,
                        group_url=group_target.url,
                    )
                )

            if index < len(group_targets):
                logger.info("GROUP_COOLDOWN | next_in_progress_after_delay=true")
                human_delay(
                    settings.min_delay_seconds + 2.0,
                    settings.max_delay_seconds + 4.0,
                )

        context.close()
        browser.close()

    logger.info("SCAN SUMMARY")
    for stats in group_stats:
        logger.info(
            "%s -> scanned=%s matched=%s alerts=%s",
            stats.group_name,
            stats.scanned,
            stats.matched,
            stats.alerts_sent,
        )


if __name__ == "__main__":
    scrape_group_posts()
