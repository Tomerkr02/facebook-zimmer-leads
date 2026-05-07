# Facebook Group Guest Lead Detector

This project scans one or more Facebook groups using an existing logged-in browser storage state, detects real guest intent, qualifies fit for Royal Water Villa specifically, saves leads into SQLite, sends only review-worthy alerts to Telegram, and provides a local command center for manual lead management.

It is designed for Royal Water Villa lead monitoring, not outreach automation.

## What it does

- Uses Playwright with an existing Facebook storage state.
- Scans recent posts from one or more Facebook group feeds with low-frequency scrolling.
- Can scan deeper into each group feed with configurable scroll depth and per-group post limits.
- Extracts post text, visible author, visible timestamp, and a post URL when Facebook exposes one.
- Tries to extract from likely post-body containers instead of the full Facebook card when possible.
- Normalizes Facebook post URLs by removing tracking query parameters and fragments before using them.
- Cleans extracted text to remove common Facebook UI garbage before matching or alerting.
- Detects guest intent even when people write naturally and do not use exact `צימר` phrasing.
- Scores each lead across multiple dimensions: intent, fit, heat, conversion potential, and emotional vibe.
- Rejects or strongly downgrades pets, parties, events, loud groups, cheap-only requests, and owner/advertiser wording.
- Can optionally run AI scoring after keyword filtering for higher-precision lead review.
- Stores normalized post keys, normalized URLs, and text hashes in SQLite to avoid duplicate alerts across all groups globally.
- Stores relevant leads in a real `leads` table with status, notes, AI reason, and suggested reply fields.
- Adds a lead-intelligence layer with guest type, urgency, religious/romantic/family signals, area flexibility, VIP match, fit score, heat level, conversion score, vibe score, and manual reply suggestions.
- Includes a local Flask dashboard for reviewing and managing leads manually.
- Sends only relevant non-rejected leads to Telegram for manual review.
- Continues scanning remaining groups even if one group fails.

## Project files

- `scraper.py`: main Playwright scraper and orchestration flow
- `matcher.py`: keyword rules, exclusions, and relevance scoring
- `telegram.py`: Telegram message formatting and delivery
- `storage.py`: SQLite dedupe store and real leads database helpers
- `reply_suggestions.py`: Hebrew reply suggestion generation with AI/template fallback
- `lead_intelligence.py`: smart lead qualification with AI/rule-based fallback
- `intent_patterns.py`: natural-language Hebrew intent detection phrases and signal groups
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

   To reprocess groups after improving matching rules without trusting the old `seen_posts` table:

   ```powershell
   python scraper.py --rescan
   ```

   Useful debug variants:

   ```powershell
   python scraper.py --debug-scan --rescan
   python scraper.py --rescan --loose --save-debug-leads
   python scraper.py --rescan --loose --save-debug-leads --send-telegram
   ```

8. Run the local dashboard:

   ```powershell
   python dashboard.py
   ```

   Then open:

   ```text
   http://127.0.0.1:5000
   ```

   Main dashboard routes:

   - `http://127.0.0.1:5000/leads` - main Lead Command Center live feed
   - `http://127.0.0.1:5000/archived` - archived leads
   - `http://127.0.0.1:5000/rejected` - rejected leads
   - `http://127.0.0.1:5000/scans` - local Scan Control Center
   - `http://127.0.0.1:5000/insights` - local analytics and AI insights
   - `http://127.0.0.1:5000/groups` - Facebook group performance view

   Do not expose this dashboard publicly.
   For quick database diagnostics, you can also open:

   ```text
   http://127.0.0.1:5000/debug/db
   ```

10. Local diagnostics:

   ```powershell
   python storage.py --debug
   python telegram.py --test
   ```

9. Optional: schedule the scraper with Windows Task Scheduler at a low frequency.

## Lead management

- Relevant leads are saved into the SQLite `leads` table.
- Leads that look like real accommodation requests but are a bad fit for Royal Water Villa can still be saved locally with `heat_level=reject` and `status=not_relevant` for review, without sending Telegram alerts.
- Archived and rejected leads are hidden from the default inbox and excluded from KPI counts by default.
- Supported lead statuses:
  - `new`
  - `contacted`
  - `waiting_reply`
  - `not_relevant`
  - `closed`
  - `archived`
- The dashboard supports:
  - KPI cards for total, hot, warm, new, contacted, closed, rejected, archived, and today's leads
  - starting local scans directly from the dashboard without exposing the app publicly
  - scan status tracking, group-level scan progress, and recent scan logs
  - a smart priority inbox that focuses on ULTRA HOT, HOT, high-fit, and recent leads
  - a live lead feed with a cleaner action bar for copy reply, open Facebook post, contacted, not relevant, archive, and learning feedback
  - filtering by status, heat level, guest type, urgency, requested area, lead type, AI category, owner ads, romantic/religious/family signals, budget sensitivity, include archived, include rejected, and free-text search
  - sorting by priority inbox, newest, fit score, conversion score, heat level, and urgency
  - a lead detail page with AI fields, intent reasons, urgency reasons, suggested replies, internal notes, activity timeline, and manual learning feedback
  - a local analytics page and a group performance page
  - copying suggested replies, follow-ups, and date/price questions for manual outreach only
  - follow-up recommendation for contacted leads after `FOLLOWUP_AFTER_HOURS`

## Royal Water Villa Lead Brain

The system now thinks like Tomer, not like a simple keyword list.

For each lead it estimates:

- `intent_score`: how likely this is a real accommodation request
- `fit_score`: how well the request matches Royal Water Villa specifically
- `heat_score`: urgency and time sensitivity
- `conversion_score`: realistic chance to close the lead
- `vibe_score`: romantic / quiet / private / pastoral fit

Additional extracted fields include:

- `lead_type`
- `guest_type`
- `group_size_estimate`
- `religious_signal`
- `romantic_signal`
- `family_signal`
- `privacy_signal`
- `urgency_signal`
- `budget_signal`
- `pet_request`
- `preferred_area`
- `required_area`
- `flexibility_level`
- `pool_requirement_strength`
- `emotional_vibe`
- `fit_reason_he`
- `reject_reason_he`
- `conversion_reason_he`
- `vip_match`

Special business rules:

- Pets are not allowed, so dog/cat/pet-friendly requests are rejected as a fit for Royal Water Villa.
- Religious / modesty / full-privacy language gets a strong positive boost.
- Romantic, quiet, private-pool, and “לנקות את הראש” style wording upgrades fit and vibe.
- Large groups, parties, birthdays, events, BBQ-only requests, Eilat-only, and north-only requests are rejected or strongly downgraded.
- Short posts are not punished if they show strong buyer intent.
- Strong VIP matches are highlighted as `👑 PERFECT MATCH`.

## Learning feedback

The dashboard stores structured feedback in a dedicated `lead_feedback` table so the system can learn from Tomer’s actions.

Supported feedback types:

- `good_lead`
- `bad_lead`
- `closed_successfully`
- `irrelevant`
- `too_expensive`
- `too_large`
- `pets`
- `bad_location`
- `spam`
- `owner_ad`

Analytics now include:

- total positive feedback
- total negative feedback
- common rejection reasons
- VIP lead patterns
- owner-ad patterns
- successful keywords/signals

## Heat levels

- `ultra_hot`: exceptional fit, often urgent, often religious/romantic/private-pool heavy
- `hot`: strong fit for Royal Water Villa, usually urgent and highly relevant
- `warm`: good lead with partial fit or less urgency
- `cold`: weak fit or incomplete signal, worth reviewing selectively
- `reject`: bad fit such as parties, events, large groups, or irrelevant requests

## Statuses

- `new`: newly detected and not yet handled
- `contacted`: manual outreach was sent
- `waiting_reply`: outreach was sent and waiting for response
- `closed`: lead was successfully closed or fully resolved
- `not_relevant`: not a real fit after manual review
- `archived`: kept for record but not actively handled

## Scoring rules

- `private pool` match: `+5`
- `couple / couple + kids` match: `+4`
- `availability today / tomorrow / weekend` match: `+3`
- `center / Rehovot / Tel Aviv / Jerusalem area` match: `+3`
- `generic zimmer request` match: `+2`
- `owner / advertiser wording`: rejected immediately

Default keyword threshold is controlled by `MIN_KEYWORD_SCORE` and currently defaults to `4`.

Natural-language intent matching also looks for phrases such as:

- `מחפש מקום`
- `מחפשת מקום`
- `מחפשים מקום`
- `רק לשים את הראש`
- `מקום רומנטי`
- `מקום שקט`
- `לילה אחד`
- `ממחר`
- `חופשה זוגית`
- `דירת נופש`
- `לנקות את הראש`

## AI scoring

- Keyword scoring remains the first filter and is always kept.
- AI scoring is optional and only runs when `ENABLE_AI_SCORING=true`, `OPENAI_API_KEY` is present, and the cleaned post text is a reasonable length.
- If AI scoring fails for any reason, or cannot run because the API key is missing, the scraper falls back to keyword scoring only.
- If AI is enabled, the AI can generate both `reason_he` and `suggested_reply_he`.
- If AI is disabled or fails, the system falls back to template-based Hebrew reply suggestions.
- The lead-intelligence layer also uses AI when enabled, and falls back to local rules if AI is disabled or fails.
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

The local lead-intelligence layer also classifies leads into business-facing types such as:

- `guest_seeker`
- `owner_advertiser`
- `event_seeker`
- `romantic_couple`
- `religious_couple`
- `family_small`
- `budget_sensitive`

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

🔥 ULTRA HOT / HOT / WARM / COLD

Intent Score: X
Fit Score: X
Heat Score: X
Conversion Score: X
Vibe Score: X

למה זוהה כליד:
...

למה מתאים ל-Royal Water Villa:
...

הצעת תגובה:
...
```

## Safety

- Never commit `.env`, `facebook_state.json`, `.db`, `.sqlite`, or `.venv`.
- Do not expose the dashboard publicly.
- The system does not automatically message Facebook users.
- It only detects, qualifies, ranks, and suggests manual replies.

## Multi-group scanning

- Groups are scanned one by one in a single run.
- Each group is isolated so a failure in one group does not stop the rest.
- The scraper adds a random cooldown between groups to stay gentle on Facebook.
- `MAX_SCROLLS` defaults to `8`.
- `POSTS_PER_GROUP_LIMIT` defaults to `80`.
- Logs include per-group progress and a final summary in this shape:

```text
SCAN SUMMARY
Group A -> scanned=X matched=Y alerts=Z
Group B -> scanned=X matched=Y alerts=Z
```

## Debug matching

- Set `DEBUG_MATCHING=true` to log, for every extracted post:
  - cleaned text preview
  - matched positive keywords
  - matched negative keywords
  - keyword score
  - final decision
- Group logs also include counters for:
  - total DOM cards found
  - posts with extracted text
  - posts rejected by owner keywords
  - posts rejected by low keyword score
  - posts passed keyword score
  - posts saved to leads
  - duplicates skipped

## Dashboard safety

- The dashboard is local-only and currently has no authentication.
- Do not expose `http://127.0.0.1:5000` publicly.
- The system only alerts and suggests replies. It does not automatically message users.
- Suggested replies are for manual outreach only.
- `facebook_state.json`, `.env`, `.db`, and `.venv` must stay local and must not be committed.
- `FOLLOWUP_AFTER_HOURS` controls when the dashboard shows `🔔 Follow-up recommended` for contacted leads.

## Operational notes

- Keep scan frequency low.
- Use the built-in random delays to reduce request bursts.
- Do not spam Facebook or send direct messages automatically.
- Review every Telegram alert manually before taking action.
- Facebook DOM structure changes often, so selector tuning may be needed over time.
- `python scraper.py --rescan` will ignore the `seen_posts` table for scanning, but still upsert into the same `leads` rows by `post_url` or `text_hash`.

## Secrets and session safety

- `.env` must never be committed because it can contain Telegram bot credentials, chat IDs, and optional OpenAI API keys.
- `facebook_state.json` must never be committed because it contains a saved logged-in Facebook browser session.
- `.db` and `.sqlite` files must never be committed because they contain collected leads, statuses, notes, and local operational state.
- `.venv` must never be committed because it is generated machine-specific environment data.
- `.env.example` is safe to keep tracked because it documents variable names without storing real secrets.
