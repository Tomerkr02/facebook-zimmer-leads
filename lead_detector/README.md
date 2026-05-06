# Facebook Group Guest Lead Detector

This project scans one or more Facebook groups using an existing logged-in browser storage state, scores recent posts with keyword rules, filters out owners/advertisers, saves relevant leads into SQLite, sends only relevant guest leads to Telegram for manual review, and provides a small local dashboard for lead management.

It is designed for Royal Water Villa lead monitoring, not outreach automation.

## What it does

- Uses Playwright with an existing Facebook storage state.
- Scans recent posts from one or more Facebook group feeds with low-frequency scrolling.
- Extracts post text, visible author, visible timestamp, and a post URL when Facebook exposes one.
- Tries to extract from likely post-body containers instead of the full Facebook card when possible.
- Normalizes Facebook post URLs by removing tracking query parameters and fragments before using them.
- Cleans extracted text to remove common Facebook UI garbage before matching or alerting.
- Scores posts with guest-intent rules and rejects owner/advertiser wording.
- Can optionally run AI scoring after keyword filtering for higher-precision lead review.
- Stores normalized post keys, normalized URLs, and text hashes in SQLite to avoid duplicate alerts across all groups globally.
- Stores relevant leads in a real `leads` table with status, notes, AI reason, and suggested reply fields.
- Includes a local Flask dashboard for reviewing and managing leads manually.
- Sends only relevant leads with score `>= 5` to Telegram.
- Continues scanning remaining groups even if one group fails.

## Project files

- `scraper.py`: main Playwright scraper and orchestration flow
- `matcher.py`: keyword rules, exclusions, and relevance scoring
- `telegram.py`: Telegram message formatting and delivery
- `storage.py`: SQLite dedupe store and real leads database helpers
- `reply_suggestions.py`: Hebrew reply suggestion generation with AI/template fallback
- `config.py`: environment-driven settings and logging
- `create_facebook_state.py`: helper script to save a logged-in Facebook session state
- `dashboard.py`: local Flask dashboard

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

   - Keep `.env` out of git. It contains secrets such as Telegram credentials and optional OpenAI API keys.

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
   - Do not place credentials in code.
   - Never commit `facebook_state.json`. It contains authenticated browser session data and can expose Facebook account access.

5. Add your Telegram bot token and chat ID to `.env`.

   - Set `FACEBOOK_GROUP_URLS` as a comma-separated list when you want to scan multiple groups in one run.
   - Example:

   ```env
   FACEBOOK_GROUP_URLS=https://www.facebook.com/groups/123,https://www.facebook.com/groups/456
   ```

   - Optional: set `GROUP_SCAN_LIMIT` to scan only the first N configured groups. Use `0` to scan all configured groups.
   - Telegram is only used for alerts. The system does not automatically message Facebook users.

6. Optional: enable AI scoring:

   - Set `ENABLE_AI_SCORING=true`
   - Add `OPENAI_API_KEY`
   - Adjust `AI_MIN_SCORE` if needed. Default: `7`
   - AI scoring only runs after a post already passes the keyword threshold, and only for reasonably sized cleaned text.

7. Run the scraper once manually:

   ```powershell
   python scraper.py
   ```

8. Run the local dashboard:

   ```powershell
   python dashboard.py
   ```

   Then open:

   ```text
   http://127.0.0.1:5000
   ```

   Do not expose this dashboard publicly.

9. Optional: schedule the scraper with Windows Task Scheduler at a low frequency.

## Lead management

- Relevant leads are saved into the SQLite `leads` table.
- Supported lead statuses:
  - `new`
  - `contacted`
  - `not_relevant`
  - `closed`
  - `archived`
- The dashboard supports:
  - newest-first lead list
  - filtering by status
  - full lead detail view
  - editing notes
  - updating lead status
  - copying a suggested manual reply

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
- If AI is enabled, the AI can generate both `reason_he` and `suggested_reply_he`.
- If AI is disabled or fails, the system falls back to template-based Hebrew reply suggestions.
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
- Deduplication is global across all configured groups in the same SQLite database.

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

Lead ID: 123
סטטוס: new

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

קבוצה:
[group name if available]

קישור קבוצה:
[group URL]

קישור:
[post URL]
```

## Multi-group scanning

- Groups are scanned one by one in a single run.
- Each group is isolated so a failure in one group does not stop the rest.
- The scraper adds a random cooldown between groups to stay gentle on Facebook.
- Logs include per-group progress and a final summary in this shape:

```text
SCAN SUMMARY
Group A -> scanned=X matched=Y alerts=Z
Group B -> scanned=X matched=Y alerts=Z
```

## Dashboard safety

- The dashboard is local-only and currently has no authentication.
- Do not expose `http://127.0.0.1:5000` publicly.
- The system only alerts and suggests replies. It does not automatically message users.

## Operational notes

- Keep scan frequency low.
- Use the built-in random delays to reduce request bursts.
- Do not spam Facebook or send direct messages automatically.
- Review every Telegram alert manually before taking action.
- Facebook DOM structure changes often, so selector tuning may be needed over time.

## Secrets and session safety

- `.env` must never be committed because it can contain Telegram bot credentials, chat IDs, and optional OpenAI API keys.
- `facebook_state.json` must never be committed because it contains a saved logged-in Facebook browser session.
- `.db` and `.sqlite` files must never be committed because they contain collected leads, statuses, notes, and local operational state.
- `.venv` must never be committed because it is generated machine-specific environment data.
- `.env.example` is safe to keep tracked because it documents variable names without storing real secrets.
