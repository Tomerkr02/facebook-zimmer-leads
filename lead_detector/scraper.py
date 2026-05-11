import logging
import random
import re
import time
import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import Browser, BrowserContext, ElementHandle, sync_playwright

from ai_scorer import AIScoreResult, is_text_reasonable_for_ai, score_post_with_ai
from config import load_settings
from lead_intelligence import analyze_lead_intelligence
from matcher import EXCLUDE_KEYWORDS, NEGATIVE_REQUEST_KEYWORDS, MatchResult, classify_post
from reply_suggestions import generate_reply_suggestion
from storage import LeadStorage
from telegram import build_alert_message, send_message


logger = logging.getLogger(__name__)
BASE_DIR = Path(__file__).resolve().parent
DEBUG_REPORT_PATH = BASE_DIR / "debug_scan_report.txt"
DEBUG_JSON_PATH = BASE_DIR / "debug_extracted_posts.json"
DEBUG_SCREENSHOT_DIR = BASE_DIR / "debug_screenshots"
LOOSE_MATCH_TERMS = [
    "צימר",
    "וילה",
    "בריכה",
    "זוג",
    "מקום",
    "פנוי",
    "סופש",
    "שבת",
    "לילה",
]


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
    scan_match_id: int | None = None


@dataclass(frozen=True)
class GroupTarget:
    url: str
    display_name: str


@dataclass
class GroupScanStats:
    group_name: str
    group_url: str
    requested_scan_depth: int = 0
    actual_scan_depth_used: int = 0
    group_quality_score: int = 50
    page_title: str | None = None
    current_url: str | None = None
    login_valid: bool = False
    access_problem: bool = False
    access_problem_reason: str | None = None
    total_dom_cards_found: int = 0
    posts_with_extracted_text: int = 0
    total_text_blocks_extracted: int = 0
    total_cleaned_posts: int = 0
    extraction_failed: int = 0
    empty_after_cleaning: int = 0
    posts_rejected_by_owner_keywords: int = 0
    posts_rejected_by_negative_keywords: int = 0
    posts_rejected_by_low_keyword_score: int = 0
    posts_rejected_by_ai: int = 0
    posts_passed_keyword_score: int = 0
    posts_saved_to_leads: int = 0
    duplicate_seen: int = 0
    duplicate_lead: int = 0
    scanned: int = 0
    matched: int = 0
    alerts_sent: int = 0
    loose_matches: int = 0
    hot_leads_found: int = 0
    raw_text_previews: list[str] | None = None
    cleaned_text_previews: list[str] | None = None
    post_url_previews: list[str] | None = None
    failure_reason: str | None = None

    def __post_init__(self) -> None:
        self.raw_text_previews = self.raw_text_previews or []
        self.cleaned_text_previews = self.cleaned_text_previews or []
        self.post_url_previews = self.post_url_previews or []


@dataclass
class DebugPostRecord:
    group_url: str
    raw_text: str
    cleaned_text: str
    post_url: str | None
    author: str | None
    keyword_score: int
    matched_positive_keywords: list[str]
    matched_negative_keywords: list[str]
    reject_reason: str | None


@dataclass(frozen=True)
class ScanOptions:
    rescan: bool = False
    debug_scan: bool = False
    loose: bool = False
    save_debug_leads: bool = False
    send_telegram: bool | None = None
    scan_run_id: int | None = None
    posts_per_group_override: int | None = None


@dataclass(frozen=True)
class ScanRuntime:
    rescan: bool
    debug_scan: bool
    loose: bool
    save_debug_leads: bool
    send_telegram: bool
    scan_run_id: int | None = None
    posts_per_group_override: int | None = None

    @property
    def safe_debug_mode(self) -> bool:
        return self.debug_scan and not self.save_debug_leads

    @property
    def persist_leads(self) -> bool:
        return (not self.debug_scan) or self.save_debug_leads


class ScanStopped(Exception):
    pass


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


def determine_scan_depth(runtime: ScanRuntime, settings, storage: LeadStorage, group_name: str, group_url: str) -> tuple[int, int]:
    quality_score = storage.get_group_quality_score(group_name, group_url)
    if runtime.posts_per_group_override and runtime.posts_per_group_override > 0:
        return runtime.posts_per_group_override, quality_score
    if quality_score >= 70:
        return 200, quality_score
    if quality_score <= 35:
        return 40, quality_score
    return settings.posts_per_group_limit, quality_score


def count_author_repetition(storage: LeadStorage, author_name: str | None) -> int:
    if not author_name:
        return 0
    with storage._connect() as connection:  # noqa: SLF001
        row = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM leads
            WHERE COALESCE(author, '') = ?
            """,
            (author_name,),
        ).fetchone()
    return int(row["count"]) if row else 0


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


def detect_login_valid(page) -> bool:
    current_url = (page.url or "").lower()
    if "facebook.com/login" in current_url or "checkpoint" in current_url:
        return False

    page_text = ""
    try:
        page_text = (page.locator("body").inner_text(timeout=3000) or "").lower()
    except Exception:  # noqa: BLE001
        return True

    invalid_markers = [
        "log into facebook",
        "login",
        "התחבר",
        "התחברי",
        "התחברות",
    ]
    return not any(marker in page_text for marker in invalid_markers)


def detect_access_problem(page) -> tuple[bool, str | None]:
    current_url = page.url or ""
    body_text = ""
    try:
        body_text = page.locator("body").inner_text(timeout=3000) or ""
    except Exception:  # noqa: BLE001
        body_text = ""

    lowered = body_text.lower()
    patterns = {
        "join_group_prompt": [
            "join group",
            "request to join",
            "הצטרף לקבוצה",
            "בקשת הצטרפות",
        ],
        "content_unavailable": [
            "this content isn't available",
            "content isn't available",
            "הדף הזה אינו זמין",
            "התוכן הזה אינו זמין",
        ],
        "private_or_blocked": [
            "private group",
            "only members can see",
            "רק חברים יכולים לראות",
            "קבוצה פרטית",
        ],
    }
    for reason, markers in patterns.items():
        if any(marker in lowered for marker in markers):
            return True, reason
    if "login" in current_url.lower() and "facebook.com" in current_url.lower():
        return True, "redirected_to_login"
    return False, None


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


def append_preview(bucket: list[str], value: str | None, limit: int = 5) -> None:
    if value and len(bucket) < limit:
        bucket.append(build_text_preview(value, limit=160))


def write_debug_artifacts(debug_lines: list[str], records: list[DebugPostRecord]) -> None:
    DEBUG_REPORT_PATH.write_text("\n".join(debug_lines).strip() + "\n", encoding="utf-8")
    payload = [
        {
            "group_url": record.group_url,
            "raw_text": record.raw_text,
            "cleaned_text": record.cleaned_text,
            "post_url": record.post_url,
            "author": record.author,
            "keyword_score": record.keyword_score,
            "matched_positive_keywords": record.matched_positive_keywords,
            "matched_negative_keywords": record.matched_negative_keywords,
            "reject_reason": record.reject_reason,
        }
        for record in records
    ]
    DEBUG_JSON_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def emit_progress(
    progress_callback: Callable[[dict[str, Any]], None] | None,
    *,
    event: str,
    log_line: str | None = None,
    **payload: Any,
) -> None:
    if progress_callback is None:
        return
    message = {"event": event, **payload}
    if log_line is not None:
        message["log_line"] = log_line
    progress_callback(message)


def loose_match(text: str, min_score: int = 1) -> MatchResult:
    normalized = re.sub(r"\s+", " ", (text or "")).strip().lower()
    matches = sorted({term for term in LOOSE_MATCH_TERMS if term in normalized})
    negative_matches = sorted({term for term in NEGATIVE_REQUEST_KEYWORDS if term in normalized})
    owner_matches = sorted({term for term in EXCLUDE_KEYWORDS if term in normalized})
    score = max(min_score, len(matches)) if matches else 0
    return MatchResult(
        is_relevant=bool(matches),
        score=score,
        matched_keywords=matches,
        matched_buckets=["loose_match"] if matches else [],
        matched_negative_keywords=negative_matches,
        matched_owner_keywords=owner_matches,
        rejection_reason=None if matches else "no_loose_terms",
    )


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
    stagnant_scrolls = 0

    for scroll_index in range(max_scrolls):
        before_count = len(seen_handles)
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

        if len(seen_handles) == before_count:
            stagnant_scrolls += 1
        else:
            stagnant_scrolls = 0
        if stagnant_scrolls >= 2:
            logger.info("No new posts found for two consecutive scrolls. Stopping group scan early.")
            break

        page.mouse.wheel(0, random.randint(1200, 2200))
        human_delay(min_delay, max_delay)

    return seen_handles[:max_posts]


def extract_post_candidate(handle: ElementHandle, min_score: int, loose: bool = False) -> PostCandidate | None:
    raw_post = handle.evaluate(POST_EXTRACTION_SCRIPT)
    raw_text = (raw_post.get("text") or "").strip()
    if len(raw_text) < 20:
        return None

    logger.debug("RAW_TEXT_PREVIEW | %s", build_text_preview(raw_text))

    text = clean_post_text(raw_text)
    logger.debug("CLEAN_TEXT_PREVIEW | %s", build_text_preview(text))
    if len(text) < 20:
        return None

    match = loose_match(text, min_score=min_score) if loose else classify_post(text, min_score=min_score)
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


def log_matching_debug(settings, candidate: PostCandidate, final_decision: str) -> None:
    if not settings.debug_matching:
        return

    logger.info(
        "DEBUG_MATCHING | preview=%s | positive=%s | negative=%s | owner=%s | keyword_score=%s | intent_score=%s | intent=%s | timing=%s | couple=%s | decision=%s",
        build_text_preview(candidate.text),
        ",".join(candidate.match.matched_keywords) or "-",
        ",".join(candidate.match.matched_negative_keywords) or "-",
        ",".join(candidate.match.matched_owner_keywords) or "-",
        candidate.match.score,
        candidate.match.intent_score,
        ",".join(candidate.match.intent_reasons) or "-",
        ",".join(candidate.match.urgency_reasons) or "-",
        ",".join(candidate.match.couple_family_reasons) or "-",
        final_decision,
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


def is_ai_rejected(settings, candidate: PostCandidate, preview: str, group_name: str, loose: bool = False) -> bool:
    if loose:
        return False
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


def capture_group_screenshot(page, screenshot_index: int) -> Path:
    DEBUG_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    screenshot_path = DEBUG_SCREENSHOT_DIR / f"group_{screenshot_index}.png"
    page.screenshot(path=str(screenshot_path), full_page=True)
    return screenshot_path


def scan_single_group(
    page,
    settings,
    storage: LeadStorage,
    group_url: str,
    runtime: ScanRuntime,
    debug_records: list[DebugPostRecord],
    debug_lines: list[str],
    screenshot_index: int,
    group_index: int,
    total_groups: int,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> GroupScanStats:
    if stop_requested and stop_requested():
        raise ScanStopped("Scan stop requested before group load.")
    logger.info("GROUP_SCAN_START | url=%s", group_url)
    emit_progress(
        progress_callback,
        event="group_started",
        current_group=group_url,
        group_index=group_index,
        total_groups=total_groups,
        log_line=f"Started group {group_index}/{total_groups}: {group_url}",
    )
    page.goto(group_url, wait_until="domcontentloaded", timeout=90000)
    human_delay(settings.min_delay_seconds, settings.max_delay_seconds)

    group_name = detect_group_name(page, group_url)
    stats = GroupScanStats(group_name=group_name, group_url=group_url)
    requested_scan_depth, group_quality_score = determine_scan_depth(runtime, settings, storage, group_name, group_url)
    stats.requested_scan_depth = requested_scan_depth
    stats.group_quality_score = group_quality_score
    try:
        stats.page_title = page.title()
    except Exception:  # noqa: BLE001
        stats.page_title = None
    stats.current_url = page.url
    stats.login_valid = detect_login_valid(page)
    stats.access_problem, stats.access_problem_reason = detect_access_problem(page)
    logger.info("GROUP_CONTEXT | name=%s | url=%s", group_name, group_url)
    logger.info(
        "GROUP_DEBUG_CONTEXT | url=%s | title=%s | current_url=%s | login_valid=%s | access_problem=%s | access_reason=%s",
        group_url,
        stats.page_title or "-",
        stats.current_url or "-",
        stats.login_valid,
        stats.access_problem,
        stats.access_problem_reason or "-",
    )
    if runtime.debug_scan:
        debug_lines.extend(
            [
                f"GROUP: {group_name}",
                f"group_url: {group_url}",
                f"page_title: {stats.page_title or '-'}",
                f"current_url: {stats.current_url or '-'}",
                f"login_valid: {stats.login_valid}",
                f"access_problem: {stats.access_problem}",
                f"access_problem_reason: {stats.access_problem_reason or '-'}",
                f"requested_scan_depth: {stats.requested_scan_depth}",
                f"group_quality_score: {stats.group_quality_score}",
            ]
        )

    post_handles = collect_post_handles(
        page=page,
        max_scrolls=settings.max_scrolls,
        max_posts=requested_scan_depth,
        min_delay=settings.min_delay_seconds,
        max_delay=settings.max_delay_seconds,
    )
    stats.total_dom_cards_found = len(post_handles)
    stats.actual_scan_depth_used = len(post_handles)

    logger.info(
        "GROUP_SCAN_DEPTH | name=%s | url=%s | requested_depth=%s | actual_depth=%s | group_quality_score=%s",
        group_name,
        group_url,
        requested_scan_depth,
        stats.actual_scan_depth_used,
        group_quality_score,
    )
    logger.info(
        "GROUP_POST_CONTAINERS | name=%s | url=%s | count=%s",
        group_name,
        group_url,
        len(post_handles),
    )

    for handle in post_handles:
        if stop_requested and stop_requested():
            stats.failure_reason = "stopped"
            raise ScanStopped("Scan stop requested during group processing.")
        try:
            raw_post = handle.evaluate(POST_EXTRACTION_SCRIPT)
        except Exception as exc:  # noqa: BLE001
            stats.extraction_failed += 1
            logger.warning("POST_EXTRACTION_FAILED | group=%s | error=%s", group_name, exc)
            continue

        raw_text = (raw_post.get("text") or "").strip()
        if raw_text:
            stats.total_text_blocks_extracted += 1
            append_preview(stats.raw_text_previews, raw_text)
        if len(raw_text) < 20:
            stats.extraction_failed += 1
            continue

        cleaned_text = clean_post_text(raw_text)
        if cleaned_text:
            stats.total_cleaned_posts += 1
            append_preview(stats.cleaned_text_previews, cleaned_text)
        else:
            stats.empty_after_cleaning += 1
        raw_url = normalize_facebook_url((raw_post.get("post_url") or "").strip()) or None
        append_preview(stats.post_url_previews, raw_url)
        if len(cleaned_text) < 20:
            continue

        match = loose_match(cleaned_text, min_score=1) if runtime.loose else classify_post(cleaned_text, min_score=settings.min_keyword_score)
        candidate = PostCandidate(
            post_key=build_post_key(raw_url, cleaned_text),
            post_url=raw_url,
            author_name=(raw_post.get("author") or "").strip() or None,
            timestamp=(raw_post.get("timestamp") or "").strip() or None,
            raw_text=raw_text,
            text=cleaned_text,
            match=match,
        )

        stats.scanned += 1
        stats.posts_with_extracted_text += 1
        preview = build_text_preview(candidate.text)
        duplicate_seen = False if runtime.rescan or runtime.debug_scan else storage.has_seen(candidate.post_key, candidate.post_url, candidate.text)
        if duplicate_seen:
            stats.duplicate_seen += 1
        if not runtime.debug_scan:
            storage.mark_seen(
                post_key=candidate.post_key,
                post_url=candidate.post_url,
                text=candidate.text,
                author_name=candidate.author_name,
            )

        reject_reason = candidate.match.rejection_reason
        debug_record = DebugPostRecord(
            group_url=group_url,
            raw_text=candidate.raw_text,
            cleaned_text=candidate.text,
            post_url=candidate.post_url,
            author=candidate.author_name,
            keyword_score=candidate.match.score,
            matched_positive_keywords=candidate.match.matched_keywords,
            matched_negative_keywords=candidate.match.matched_negative_keywords,
            reject_reason=reject_reason,
        )
        debug_records.append(debug_record)

        if not candidate.match.is_relevant:
            if candidate.match.rejection_reason == "owner_or_advertiser_wording":
                stats.posts_rejected_by_owner_keywords += 1
            elif candidate.match.rejection_reason == "unsuitable_event_request":
                stats.posts_rejected_by_negative_keywords += 1
            else:
                stats.posts_rejected_by_low_keyword_score += 1
            log_matching_debug(settings, candidate, f"rejected:{candidate.match.rejection_reason}")
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

        stats.posts_passed_keyword_score += 1
        if runtime.loose:
            stats.loose_matches += 1
        log_matching_debug(settings, candidate, "passed_keyword_score")
        maybe_apply_ai_scoring(settings, candidate, preview, group_name)
        if is_ai_rejected(settings, candidate, preview, group_name, loose=runtime.loose):
            stats.posts_rejected_by_ai += 1
            debug_record.reject_reason = "ai_rejected"
            if runtime.scan_run_id is not None:
                candidate.scan_match_id = storage.save_scan_match(
                    runtime.scan_run_id,
                    group_url=group_url,
                    group_name=group_name,
                    raw_text=candidate.raw_text,
                    cleaned_text=candidate.text,
                    post_url=candidate.post_url,
                    author=candidate.author_name,
                    matched_keywords=candidate.match.matched_keywords,
                    intent_score=candidate.match.intent_score,
                    fit_score=0,
                    heat_score=0,
                    conversion_score=0,
                    classification=infer_category(candidate),
                    saved_as_lead_id=None,
                    reject_reason="ai_rejected",
                )
            log_matching_debug(settings, candidate, "rejected:ai")
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
        intelligence = analyze_lead_intelligence(
            cleaned_text=candidate.text,
            matched_keywords=candidate.match.matched_keywords,
            enable_ai_scoring=settings.enable_ai_scoring and not runtime.loose,
            openai_api_key=settings.openai_api_key,
            post_timestamp=candidate.timestamp,
            repeated_author_count=count_author_repetition(storage, candidate.author_name),
        )
        suggested_reply_he = intelligence.suggested_first_reply_he or suggested_reply_he
        fit_rejected = intelligence.heat_level == "reject"
        lead_status = "not_relevant" if fit_rejected else "new"

        lead_action = "debug_only"
        if runtime.persist_leads:
            candidate.lead_id, lead_action = storage.save_lead(
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
                guest_type=intelligence.guest_type,
                urgency=intelligence.urgency,
                requested_area=intelligence.requested_area,
                pool_intent=intelligence.pool_intent,
                privacy_intent=intelligence.privacy_intent,
                bad_fit_reasons=intelligence.bad_fit_reasons,
                fit_score=intelligence.fit_score,
                heat_level=intelligence.heat_level,
                short_reason_he=intelligence.short_reason_he,
                recommended_action=intelligence.recommended_action,
                suggested_first_reply_he=intelligence.suggested_first_reply_he,
                suggested_followup_he=intelligence.suggested_followup_he,
                suggested_price_question_he=intelligence.suggested_price_question_he,
                intent_score=candidate.match.intent_score,
                intent_reasons=candidate.match.intent_reasons,
                urgency_reasons=candidate.match.urgency_reasons,
                pet_friendly_requested=candidate.match.pet_friendly_requested,
                lead_type=intelligence.lead_type,
                group_size_estimate=intelligence.group_size_estimate,
                religious_signal=intelligence.religious_signal,
                romantic_signal=intelligence.romantic_signal,
                family_signal=intelligence.family_signal,
                privacy_signal=intelligence.privacy_signal,
                urgency_signal=intelligence.urgency_signal,
                budget_signal=intelligence.budget_signal,
                pet_request=intelligence.pet_request,
                preferred_area=intelligence.preferred_area,
                required_area=intelligence.required_area,
                flexibility_level=intelligence.flexibility_level,
                pool_requirement_strength=intelligence.pool_requirement_strength,
                emotional_vibe=intelligence.emotional_vibe,
                fit_reason_he=intelligence.fit_reason_he,
                reject_reason_he=intelligence.reject_reason_he,
                conversion_reason_he=intelligence.conversion_reason_he,
                heat_score=intelligence.heat_score,
                heat_label=intelligence.heat_label,
                heat_reasons_json=json.dumps(intelligence.heat_reasons_json, ensure_ascii=False),
                conversion_score=intelligence.conversion_score,
                vibe_score=intelligence.vibe_score,
                vip_match=intelligence.vip_match,
                owner_advertisement=intelligence.owner_advertisement,
                budget_sensitive=intelligence.budget_sensitive,
                ai_explanation_he=intelligence.ai_explanation_he,
                recommended_media_type=intelligence.recommended_media_type,
                recommended_media_reason=intelligence.recommended_media_reason,
                scan_run_id=runtime.scan_run_id,
                scan_depth_used=stats.actual_scan_depth_used,
                group_quality_score=stats.group_quality_score,
                status=lead_status,
                sent_to_telegram=0,
            )
            stats.posts_saved_to_leads += 1
            if lead_action == "created":
                logger.info(
                    "LEAD_SAVED | group=%s | lead_id=%s | url=%s | preview=%s",
                    group_name,
                    candidate.lead_id,
                    candidate.post_url or "-",
                    preview,
                )
            else:
                logger.info(
                    "LEAD_UPDATED | group=%s | lead_id=%s | url=%s | preview=%s",
                    group_name,
                    candidate.lead_id,
                    candidate.post_url or "-",
                    preview,
                )

        if lead_action == "updated":
            stats.duplicate_lead += 1

        if duplicate_seen or (runtime.rescan and lead_action == "updated"):
            debug_record.reject_reason = "duplicate"
            if runtime.scan_run_id is not None:
                candidate.scan_match_id = storage.save_scan_match(
                    runtime.scan_run_id,
                    group_url=group_url,
                    group_name=group_name,
                    raw_text=candidate.raw_text,
                    cleaned_text=candidate.text,
                    post_url=candidate.post_url,
                    author=candidate.author_name,
                    matched_keywords=candidate.match.matched_keywords,
                    intent_score=intelligence.intent_score,
                    fit_score=intelligence.fit_score,
                    heat_score=intelligence.heat_score,
                    conversion_score=intelligence.conversion_score,
                    classification=intelligence.lead_type,
                    saved_as_lead_id=candidate.lead_id,
                    reject_reason="duplicate",
                )
            log_matching_debug(settings, candidate, "duplicate_skipped")
            if runtime.persist_leads:
                logger.info(
                    "LEAD_SKIPPED_DUPLICATE | group=%s | lead_id=%s | key=%s | url=%s | preview=%s",
                    group_name,
                    candidate.lead_id,
                    candidate.post_key,
                    candidate.post_url or "-",
                    preview,
                )
            continue

        if fit_rejected:
            debug_record.reject_reason = "fit_reject"
            if runtime.scan_run_id is not None:
                candidate.scan_match_id = storage.save_scan_match(
                    runtime.scan_run_id,
                    group_url=group_url,
                    group_name=group_name,
                    raw_text=candidate.raw_text,
                    cleaned_text=candidate.text,
                    post_url=candidate.post_url,
                    author=candidate.author_name,
                    matched_keywords=candidate.match.matched_keywords,
                    intent_score=intelligence.intent_score,
                    fit_score=intelligence.fit_score,
                    heat_score=intelligence.heat_score,
                    conversion_score=intelligence.conversion_score,
                    classification=intelligence.lead_type,
                    saved_as_lead_id=candidate.lead_id,
                    reject_reason=intelligence.reject_reason_he or "fit_reject",
                )
            logger.info(
                "REJECTED_POST | group=%s | lead_id=%s | key=%s | reason=fit_reject | reject_reason=%s | url=%s | preview=%s",
                group_name,
                candidate.lead_id,
                candidate.post_key,
                intelligence.reject_reason_he or intelligence.short_reason_he or "-",
                candidate.post_url or "-",
                preview,
            )
            continue

        stats.matched += 1
        if intelligence.heat_score >= 80:
            stats.hot_leads_found += 1
        debug_record.reject_reason = None
        if runtime.scan_run_id is not None:
            candidate.scan_match_id = storage.save_scan_match(
                runtime.scan_run_id,
                group_url=group_url,
                group_name=group_name,
                raw_text=candidate.raw_text,
                cleaned_text=candidate.text,
                post_url=candidate.post_url,
                author=candidate.author_name,
                matched_keywords=candidate.match.matched_keywords,
                intent_score=intelligence.intent_score,
                fit_score=intelligence.fit_score,
                heat_score=intelligence.heat_score,
                conversion_score=intelligence.conversion_score,
                classification=intelligence.lead_type,
                saved_as_lead_id=candidate.lead_id,
                reject_reason=None,
            )
        logger.info(
            "MATCHED_LEAD | group=%s | lead_id=%s | key=%s | score=%s | intent_score=%s | ai_score=%s | keywords=%s | why=%s | intent=%s | timing=%s | couple=%s | url=%s | preview=%s",
            group_name,
            candidate.lead_id,
            candidate.post_key,
            candidate.match.score,
            candidate.match.intent_score,
            candidate.ai_result.score if candidate.ai_result else "-",
            ",".join(candidate.match.matched_keywords) or "-",
            candidate.match.why_detected_he or "-",
            ",".join(candidate.match.intent_reasons) or "-",
            ",".join(candidate.match.urgency_reasons) or "-",
            ",".join(candidate.match.couple_family_reasons) or "-",
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
            intent_score=intelligence.intent_score,
            heat_score=intelligence.heat_score,
            heat_label=intelligence.heat_label,
            heat_reasons=intelligence.heat_reasons_json,
            conversion_score=intelligence.conversion_score,
            vibe_score=intelligence.vibe_score,
            heat_level=intelligence.heat_level,
            fit_score=intelligence.fit_score,
            fit_reason_he=intelligence.fit_reason_he,
            guest_type=intelligence.guest_type,
            urgency=intelligence.urgency,
            requested_area=intelligence.requested_area,
            pool_intent=intelligence.pool_intent,
            ai_result=candidate.ai_result,
        )
        if not runtime.send_telegram:
            logger.info(
                "ALERT_SKIPPED_DEBUG | group=%s | key=%s | preview=%s",
                group_name,
                candidate.post_key,
                preview,
            )
        elif candidate.lead_id and send_message(settings.telegram_bot_token, settings.telegram_chat_id, message):
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
        elif runtime.send_telegram:
            logger.warning(
                "TELEGRAM_SEND_FAILED | reason=missing_lead_id | group=%s | key=%s",
                group_name,
                candidate.post_key,
            )

        human_delay(settings.min_delay_seconds, settings.max_delay_seconds)

    logger.info(
        "GROUP_SCAN_DONE | name=%s | requested_depth=%s | actual_depth=%s | group_quality_score=%s | scanned=%s | matched=%s | hot_leads=%s | alerts=%s | dom_cards=%s | extracted=%s | cleaned=%s | extraction_failed=%s | empty_after_cleaning=%s | owner_rejected=%s | negative_rejected=%s | low_score_rejected=%s | ai_rejected=%s | passed_keyword=%s | leads_saved=%s | duplicate_seen=%s | duplicate_lead=%s",
        stats.group_name,
        stats.requested_scan_depth,
        stats.actual_scan_depth_used,
        stats.group_quality_score,
        stats.scanned,
        stats.matched,
        stats.hot_leads_found,
        stats.alerts_sent,
        stats.total_dom_cards_found,
        stats.total_text_blocks_extracted,
        stats.total_cleaned_posts,
        stats.extraction_failed,
        stats.empty_after_cleaning,
        stats.posts_rejected_by_owner_keywords,
        stats.posts_rejected_by_negative_keywords,
        stats.posts_rejected_by_low_keyword_score,
        stats.posts_rejected_by_ai,
        stats.posts_passed_keyword_score,
        stats.posts_saved_to_leads,
        stats.duplicate_seen,
        stats.duplicate_lead,
    )
    if runtime.debug_scan:
        if stats.access_problem or not stats.login_valid:
            screenshot_path = capture_group_screenshot(page, screenshot_index)
            debug_lines.append(f"screenshot: {screenshot_path}")
        debug_lines.extend(
            [
                f"cards_found: {stats.total_dom_cards_found}",
                f"actual_scan_depth_used: {stats.actual_scan_depth_used}",
                f"text_blocks_extracted: {stats.total_text_blocks_extracted}",
                f"cleaned_posts: {stats.total_cleaned_posts}",
                f"extraction_failed: {stats.extraction_failed}",
                f"empty_after_cleaning: {stats.empty_after_cleaning}",
                f"rejected_owner_ad: {stats.posts_rejected_by_owner_keywords}",
                f"rejected_negative_keywords: {stats.posts_rejected_by_negative_keywords}",
                f"rejected_low_score: {stats.posts_rejected_by_low_keyword_score}",
                f"rejected_ai: {stats.posts_rejected_by_ai}",
                f"duplicate_seen: {stats.duplicate_seen}",
                f"duplicate_lead: {stats.duplicate_lead}",
                f"saved: {stats.posts_saved_to_leads}",
                f"hot_leads_found: {stats.hot_leads_found}",
                f"telegram_sent: {stats.alerts_sent}",
                "first_5_raw_text_previews:",
                *[f"  - {item}" for item in stats.raw_text_previews],
                "first_5_cleaned_text_previews:",
                *[f"  - {item}" for item in stats.cleaned_text_previews],
                "first_5_post_urls:",
                *[f"  - {item}" for item in stats.post_url_previews],
                "",
            ]
        )
    emit_progress(
        progress_callback,
        event="group_completed",
        current_group=group_url,
        group_index=group_index,
        total_groups=total_groups,
        counters={
            "cards_found": stats.total_dom_cards_found,
            "posts_extracted": stats.total_text_blocks_extracted,
            "posts_matched": stats.matched,
            "leads_saved": stats.posts_saved_to_leads,
            "telegram_sent": stats.alerts_sent,
            "hot_leads": stats.hot_leads_found,
        },
        group_stats=stats.__dict__.copy(),
        log_line=(
            f"Completed group {group_index}/{total_groups}: "
            f"matched={stats.matched} hot={stats.hot_leads_found} saved={stats.posts_saved_to_leads} alerts={stats.alerts_sent}"
        ),
    )
    return stats


def resolve_group_targets(settings) -> list[GroupTarget]:
    urls = settings.facebook_group_urls
    if settings.group_scan_limit > 0:
        urls = urls[: settings.group_scan_limit]
    return [GroupTarget(url=url, display_name=url) for url in urls]


def run_scan(
    options: ScanOptions,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    stop_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    settings = load_settings()
    if not settings.facebook_group_urls:
        raise ValueError("FACEBOOK_GROUP_URLS is required.")
    if not settings.facebook_storage_state_path.exists():
        raise FileNotFoundError(
            f"Facebook storage state not found: {settings.facebook_storage_state_path}"
        )

    should_send_telegram = options.send_telegram if options.send_telegram is not None else not options.debug_scan
    runtime = ScanRuntime(
        rescan=options.rescan,
        debug_scan=options.debug_scan,
        loose=options.loose,
        save_debug_leads=options.save_debug_leads,
        send_telegram=should_send_telegram,
        scan_run_id=options.scan_run_id,
        posts_per_group_override=options.posts_per_group_override,
    )
    storage = LeadStorage(settings.resolved_database_path)
    logger.info("DB_PATH | %s", settings.resolved_database_path)
    logger.info("Current leads count: %s", storage.count_leads())
    if runtime.safe_debug_mode:
        logger.info("SAFE_DEBUG_MODE=true | Telegram alerts, seen-table writes, and permanent lead saves are disabled for this run.")
    elif runtime.debug_scan and runtime.persist_leads:
        logger.info("SAFE_DEBUG_MODE=false | Debug scan will persist leads because --save-debug-leads was provided.")
    if runtime.loose:
        logger.info("LOOSE_MODE=true | min keyword score forced to 1, AI rejection disabled, negative keywords recorded only.")
    logger.info("TELEGRAM_ENABLED | %s", runtime.send_telegram)
    group_targets = resolve_group_targets(settings)
    group_stats: list[GroupScanStats] = []
    debug_lines: list[str] = []
    debug_records: list[DebugPostRecord] = []
    scan_mode = "debug" if runtime.debug_scan else "loose" if runtime.loose else "normal"
    emit_progress(
        progress_callback,
        event="scan_started",
        total_groups=len(group_targets),
        mode=scan_mode,
        counters={
            "groups_done": 0,
            "posts_extracted": 0,
            "posts_matched": 0,
            "leads_saved": 0,
            "telegram_sent": 0,
        },
        log_line=f"Scan started in {scan_mode} mode for {len(group_targets)} groups.",
    )

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=settings.headless)
        context = ensure_logged_in_context(browser, str(settings.facebook_storage_state_path))
        page = context.new_page()

        for index, group_target in enumerate(group_targets, start=1):
            if stop_requested and stop_requested():
                emit_progress(
                    progress_callback,
                    event="scan_stopped",
                    total_groups=len(group_targets),
                    groups_done=len(group_stats),
                    log_line="Scan stop requested before next group.",
                )
                raise ScanStopped("Scan stop requested.")
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
                    runtime=runtime,
                    debug_records=debug_records,
                    debug_lines=debug_lines,
                    screenshot_index=index,
                    group_index=index,
                    total_groups=len(group_targets),
                    progress_callback=progress_callback,
                    stop_requested=stop_requested,
                )
                group_stats.append(stats)
            except ScanStopped:
                group_stats.append(
                    GroupScanStats(
                        group_name=group_target.display_name,
                        group_url=group_target.url,
                        failure_reason="stopped",
                    )
                )
                emit_progress(
                    progress_callback,
                    event="scan_stopped",
                    total_groups=len(group_targets),
                    groups_done=len(group_stats),
                    log_line=f"Scan stopped while processing {group_target.url}",
                )
                break
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "GROUP_SCAN_FAILED | url=%s | error=%s",
                    group_target.url,
                    exc,
                )
                if runtime.debug_scan:
                    try:
                        screenshot_path = capture_group_screenshot(page, index)
                    except Exception:  # noqa: BLE001
                        screenshot_path = None
                    debug_lines.extend(
                        [
                            f"GROUP: {group_target.display_name}",
                            f"group_url: {group_target.url}",
                            f"failure_reason: {exc}",
                            f"screenshot: {screenshot_path or '-'}",
                            "",
                        ]
                    )
                group_stats.append(
                    GroupScanStats(
                        group_name=group_target.display_name,
                        group_url=group_target.url,
                        failure_reason=str(exc),
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
            "%s -> requested_depth=%s actual_depth=%s quality=%s scanned=%s matched=%s hot=%s saved=%s alerts=%s cards_found=%s extracted=%s cleaned=%s extraction_failed=%s empty_after_cleaning=%s owner_rejected=%s negative_rejected=%s low_score_rejected=%s ai_rejected=%s duplicate_seen=%s duplicate_lead=%s failure_reason=%s",
            stats.group_name,
            stats.requested_scan_depth,
            stats.actual_scan_depth_used,
            stats.group_quality_score,
            stats.scanned,
            stats.matched,
            stats.hot_leads_found,
            stats.posts_saved_to_leads,
            stats.alerts_sent,
            stats.total_dom_cards_found,
            stats.total_text_blocks_extracted,
            stats.total_cleaned_posts,
            stats.extraction_failed,
            stats.empty_after_cleaning,
            stats.posts_rejected_by_owner_keywords,
            stats.posts_rejected_by_negative_keywords,
            stats.posts_rejected_by_low_keyword_score,
            stats.posts_rejected_by_ai,
            stats.duplicate_seen,
            stats.duplicate_lead,
            stats.failure_reason or "-",
        )
    accessible_groups = sum(1 for stats in group_stats if not stats.access_problem and stats.login_valid and not stats.failure_reason)
    blocked_groups = len(group_stats) - accessible_groups
    total_extracted = sum(stats.total_text_blocks_extracted for stats in group_stats)
    total_posts_with_text = sum(stats.posts_with_extracted_text for stats in group_stats)
    total_loose_matches = sum(stats.loose_matches for stats in group_stats)
    total_rejected_keywords = sum(
        stats.posts_rejected_by_owner_keywords
        + stats.posts_rejected_by_negative_keywords
        + stats.posts_rejected_by_low_keyword_score
        for stats in group_stats
    )
    total_rejected_ai = sum(stats.posts_rejected_by_ai for stats in group_stats)
    total_saved = sum(stats.posts_saved_to_leads for stats in group_stats)
    total_hot = sum(stats.hot_leads_found for stats in group_stats)
    logger.info("TOTAL GROUPS: %s", len(group_stats))
    logger.info("GROUPS ACCESSIBLE: %s", accessible_groups)
    logger.info("GROUPS BLOCKED/NOT_JOINED: %s", blocked_groups)
    logger.info("POSTS EXTRACTED: %s", total_extracted)
    logger.info("POSTS_WITH_TEXT: %s", total_posts_with_text)
    logger.info("POSTS_MATCHING_LOOSE: %s", total_loose_matches)
    logger.info("POSTS_REJECTED_BY_KEYWORDS: %s", total_rejected_keywords)
    logger.info("POSTS_REJECTED_BY_AI: %s", total_rejected_ai)
    logger.info("LEADS_SAVED: %s", total_saved)
    logger.info("HOT_LEADS_FOUND: %s", total_hot)
    if runtime.debug_scan:
        debug_lines.extend(
            [
                "FINAL SUMMARY",
                f"TOTAL GROUPS: {len(group_stats)}",
                f"GROUPS ACCESSIBLE: {accessible_groups}",
                f"GROUPS BLOCKED/NOT_JOINED: {blocked_groups}",
                f"POSTS EXTRACTED: {total_extracted}",
                f"POSTS_WITH_TEXT: {total_posts_with_text}",
                f"POSTS_MATCHING_LOOSE: {total_loose_matches}",
                f"POSTS_REJECTED_BY_KEYWORDS: {total_rejected_keywords}",
                f"POSTS_REJECTED_BY_AI: {total_rejected_ai}",
                f"LEADS_SAVED: {total_saved}",
                f"HOT_LEADS_FOUND: {total_hot}",
            ]
        )
        write_debug_artifacts(debug_lines, debug_records)
        logger.info("DEBUG_REPORT_WRITTEN | path=%s", DEBUG_REPORT_PATH)
        logger.info("DEBUG_JSON_WRITTEN | path=%s", DEBUG_JSON_PATH)
    final_status = "stopped" if any(stats.failure_reason == "stopped" for stats in group_stats) else "completed"
    summary = {
        "status": final_status,
        "mode": scan_mode,
        "total_groups": len(group_stats),
        "groups_accessible": accessible_groups,
        "groups_blocked": blocked_groups,
        "posts_extracted": total_extracted,
        "posts_with_text": total_posts_with_text,
        "posts_matching_loose": total_loose_matches,
        "posts_rejected_by_keywords": total_rejected_keywords,
        "posts_rejected_by_ai": total_rejected_ai,
        "leads_saved": total_saved,
        "hot_leads_found": total_hot,
        "telegram_sent": sum(stats.alerts_sent for stats in group_stats),
        "group_stats": [stats.__dict__.copy() for stats in group_stats],
    }
    emit_progress(
        progress_callback,
        event="scan_completed" if final_status == "completed" else "scan_stopped",
        total_groups=len(group_targets),
        groups_done=len(group_stats),
        counters={
            "posts_extracted": total_extracted,
            "posts_matched": sum(stats.matched for stats in group_stats),
            "leads_saved": total_saved,
            "telegram_sent": sum(stats.alerts_sent for stats in group_stats),
        },
        summary=summary,
        log_line=f"Scan finished with status={final_status}. leads_saved={total_saved}",
    )
    return summary


def scrape_group_posts(
    rescan: bool = False,
    debug_scan: bool = False,
    loose: bool = False,
    save_debug_leads: bool = False,
    send_telegram_alerts: bool | None = None,
    posts_per_group: int | None = None,
) -> dict[str, Any]:
    return run_scan(
        ScanOptions(
            rescan=rescan,
            debug_scan=debug_scan,
            loose=loose,
            save_debug_leads=save_debug_leads,
            send_telegram=send_telegram_alerts,
            posts_per_group_override=posts_per_group,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Facebook guest lead detector scraper")
    parser.add_argument(
        "--rescan",
        action="store_true",
        help="Ignore the seen/dedupe table for scanning while still avoiding duplicate lead rows.",
    )
    parser.add_argument(
        "--debug-scan",
        action="store_true",
        help="Enable deep diagnostics mode with reports, JSON output, and screenshots for failed groups.",
    )
    parser.add_argument(
        "--loose",
        action="store_true",
        help="Use very loose guest-intent matching for diagnostics and recall testing.",
    )
    parser.add_argument(
        "--save-debug-leads",
        action="store_true",
        help="When used with debug/loose rescans, save matched leads into the real leads table.",
    )
    parser.add_argument(
        "--send-telegram",
        action="store_true",
        help="Send Telegram alerts for newly saved leads in this run.",
    )
    parser.add_argument(
        "--posts-per-group",
        type=int,
        help="Override dynamic scan depth for every group in this run.",
    )
    args = parser.parse_args()
    scrape_group_posts(
        rescan=args.rescan,
        debug_scan=args.debug_scan,
        loose=args.loose,
        save_debug_leads=args.save_debug_leads,
        send_telegram_alerts=args.send_telegram if (args.debug_scan or args.loose) else None,
        posts_per_group=args.posts_per_group,
    )
