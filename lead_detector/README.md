# Facebook Group Guest Lead Detector MVP

This MVP scans a Facebook group using an existing logged-in browser storage state, scores recent posts with keyword rules, filters out owners/advertisers, and sends only relevant guest leads to Telegram for manual review.

It is designed for Royal Water Villa lead monitoring, not outreach automation.

## What it does

- Uses Playwright with an existing Facebook storage state.
- Scans recent posts from a Facebook group feed with low-frequency scrolling.
- Extracts post text, visible author, visible timestamp, and a post URL when Facebook exposes one.
- Tries to extract from likely post-body containers instead of the full Facebook card when possible.
- Normalizes Facebook post URLs by removing tracking query parameters and fragments before using them.
- Cleans extracted text to remove common Facebook UI garbage before matching or alerting.
- Scores posts with guest-intent rules and rejects owner/advertiser wording.
- Can optionally run AI scoring after keyword filtering for higher-precision lead review.
- Stores normalized post keys, normalized URLs, and text hashes in SQLite to avoid duplicate alerts.
- Sends only relevant leads with score `>= 5` to Telegram.

## Project files

- `scraper.py`: main Playwright scraper and orchestration flow
- `matcher.py`: keyword rules, exclusions, and relevance scoring
- `telegram.py`: Telegram message formatting and delivery
- `storage.py`: SQLite dedupe store
- `config.py`: environment-driven settings and logging
- `create_facebook_state.py`: helper script to save a logged-in Facebook session state

## Setup

1. Install dependencies:

   ```powershell
   cd lead_detector
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```

2. Install Playwright browsers:

   ```powershell
   python -m playwright install chromium
   ```

3. Create your environment file:

   ```powershell
   Copy-Item .env.example .env
   ```

4. Create a Facebook storage state after manual login:

   - Run the helper script:

   ```powershell
   python create_facebook_state.py
   ```

   - A Chromium window will open on the Facebook login page.
   - Log into Facebook manually in that browser window.
   - After the login is complete, return to the terminal and press `Enter`.
   - The script will save the session state to `facebook_state.json`.
   - Set `FACEBOOK_STORAGE_STATE_PATH` in `.env` to point to that file if needed.
   - Do not place credentials in code or `.env`.

5. Add your Telegram bot token and chat ID to `.env`.

6. Optional: enable AI scoring:

   - Set `ENABLE_AI_SCORING=true`
   - Add `OPENAI_API_KEY`
   - Adjust `AI_MIN_SCORE` if needed. Default: `7`
   - AI scoring only runs after a post already passes the keyword threshold, and only for reasonably sized cleaned text.

7. Run the scraper once manually:

   ```powershell
   python scraper.py
   ```

8. Optional: schedule it with Windows Task Scheduler at a low frequency.

## Scoring rules

- `private pool` match: `+5`
- `couple / couple + kids` match: `+4`
- `availability today / tomorrow / weekend` match: `+3`
- `center / Rehovot / Tel Aviv / Jerusalem area` match: `+3`
- `generic zimmer request` match: `+2`
- `owner / advertiser wording`: rejected immediately

Only leads with score `>= 5` are sent.

## AI scoring

- Keyword scoring remains the first filter and is always kept.
- AI scoring is optional and only runs when `ENABLE_AI_SCORING=true`, `OPENAI_API_KEY` is present, and the cleaned post text is a reasonable length.
- If AI scoring fails for any reason, or cannot run because the API key is missing, the scraper falls back to keyword scoring only.
- When AI is enabled and returns a valid result, the lead is sent to Telegram only if:
  - the keyword score passed the normal threshold, and
  - `is_relevant=true`, and
  - `AI score >= AI_MIN_SCORE`
- The AI returns structured JSON with:
  - `is_relevant`
  - `score` from `1` to `10`
  - `category`
  - `reason_he`
  - `suggested_reply_he`

Supported AI categories:

- `couple`
- `couple_with_kids`
- `private_pool`
- `weekend`
- `urgent_today`
- `location_match`
- `not_relevant`

## URL normalization and dedupe

- Facebook links often include unstable tracking parameters such as `__cft__` and `__tn__`.
- The scraper removes all query parameters and fragments and keeps only the clean canonical URL path.
- The normalized URL is used for duplicate detection, logs, Telegram alerts, and storage keys.
- If no usable post URL is extracted, the scraper falls back to a stable text hash for deduplication.
- This keeps logs cleaner and prevents the same post from generating duplicate alerts just because Facebook changed its tracking suffixes.

## Text cleaning behavior

- The scraper prefers likely human-written post-content nodes before falling back to the full card text.
- It removes common Facebook UI phrases such as `Facebook`, `See translation`, `Write a public comment`, `Contributor`, `Like`, `Comment`, `Share`, `Follow`, `Reels`, and `Sponsored`.
- It collapses repeated words, trims short junk lines, and stops reading once footer UI text begins.
- Telegram alerts use only the cleaned post text.
- Debug logs include `RAW_TEXT_PREVIEW` and `CLEAN_TEXT_PREVIEW` to help tune extraction safely.

Example 1

RAW:

```text
Facebook Facebook Facebook
מחפשת צימר לזוג עם בריכה פרטית במרכז להיום
See translation
Contributor
Like
Comment
Share
Write a public comment
```

CLEAN:

```text
מחפשת צימר לזוג עם בריכה פרטית במרכז להיום
```

Example 2

RAW:

```text
מחפשים צימר ליד ירושלים לזוג פלוס ילד
Follow
Reels
Sponsored
Write a public comment
```

CLEAN:

```text
מחפשים צימר ליד ירושלים לזוג פלוס ילד
```

## Telegram alert format

```text
🔥 ליד חדש לצימר

רמת התאמה: גבוהה / בינונית
ניקוד מילים: X
ניקוד AI: X/10
קטגוריה: couple_with_kids

סיבת התאמה:
[reason_he]

הצעת תגובה:
[suggested_reply_he]

התאמות:
[keywords]

תוכן:
[post text]

כותב:
[author if available]

קישור:
[post URL]
```

## Operational notes

- Keep scan frequency low.
- Use the built-in random delays to reduce request bursts.
- Do not spam Facebook or send direct messages automatically.
- Review every Telegram alert manually before taking action.
- Facebook DOM structure changes often, so selector tuning may be needed over time.
