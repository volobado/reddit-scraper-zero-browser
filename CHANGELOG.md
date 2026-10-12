# Changelog

## 0.2.1 (2026-10-12)

Second review pass. The rate limiter was tested against a real quota exhaustion
(115 posts with comments, 220 requests, one 190-second pause, not a single 429).

- `--watch INTERVAL`: monitor mode, the same command repeated into SQLite, new items only;
  a summary per pass, and a failed pass does not stop the loop.
- `post` accepts several links; a broken one is logged, the rest are collected.
- `--log FILE` appends log lines to a file; the summary shows `Quota left`.
- A rate-limit pause is counted and logged once per window, not once per concurrent task.
- A stickied post at the top of a listing no longer ends `--since` and `--only-new` scans.
- Guard against looping forever if Reddit returns the same `after` cursor.
- A network error during session bootstrap is retried like any other.
- On Windows the selector event loop is used (curl_cffi's recommendation, no extra thread).
- The version lives in one place (`_version.py`); the User-Agent reads it from there.

## 0.2.0 (2026-10-12)

Rewritten after the audit in `docs/AUDIT-2026-10-12.md`; the work plan is `docs/PLAN.md`.

### Data
- `raw_json=1` on every request: texts and links without HTML entities.
- 19 new fields: `url`, `previewImageUrl` (full resolution), `createdUtc`, `editedAt`,
  `authorFullname`, `isSubmitter`, `distinguished`, `controversiality`, `scoreHidden`,
  `totalAwards`, `linkFlairCssClass`, `isSpoiler`, `isStickied`, `isLocked`,
  `isOriginalContent`, `removedByCategory`, `videoHlsUrl`, `videoDurationSec`,
  `videoHasAudio`, `pollOptions`.
- Galleries: extensions from `media_metadata` (mime type), not from the thumbnail; galleries
  and videos of crossposts are taken from the original post.
- The flair emoji is searched across the whole `author_flair_richtext`.
- `externalUrl` falls back to `url`, which some posts used to lack.

### Reliability
- Rate limiter driven by the `x-ratelimit-*` headers: waits for the window reset instead of
  blind backoff; 429 honours `Retry-After` / `x-ratelimit-reset`.
- Specific exceptions: `RedditNotFound`, `RedditForbidden` (with the reason: private /
  banned / quarantined / gold_only), `RedditRateLimited`, `RedditServerError`,
  `RedditNetworkError`, `RedditBadRequest`. 4xx responses are no longer retried.
- Session rebuild by generation: on a 403 one task refreshes the session, not all of them,
  and the old session is not closed under its neighbours.
- An honest User-Agent instead of a random number; TLS impersonation stays optional
  (`--impersonate`) because Reddit answers it with 403.
- Failed comment threads are logged and counted instead of being swallowed.

### Completeness
- Collapsed branches expanded through `/api/morechildren`; the 200-comment ceiling is gone.
- `limit`, `depth` and `sort` are passed to the thread request: `--max-comments 5` no
  longer downloads 200.
- `post` accepts `redd.it/<id>`, share links, `/gallery/<id>`, `t3_<id>`.
- `user --type submitted|comments|overview`; `controversial` sort.
- Deduplication by id across pages.

### Export
- Streaming writes for every format; partial results survive Ctrl+C and errors.
- CSV in `utf-8-sig` (Excel reads Cyrillic), a fixed column set.
- New formats: SQLite (`.db`, `INSERT OR REPLACE`, `--only-new`) and a Markdown digest (`.md`).
- An unknown extension is a clear error, not JSON under a foreign name.

### CLI
- Without `-o` the output is JSONL on stdout; banner, progress and summary go to stderr.
- Filters `--since/--until/--min-score/--keyword/--flair/--author/--no-nsfw`.
- `--delay`, `--timeout`, `--retries`, repeatable `--proxy`, `-v/-vv`, `-q`, `--version`.
- The progress bar counts posts and no longer overflows with `--comments`; the summary shows
  request statistics, rate-limit waits, failed threads, duplicates and filtered items.
- Exit code 1 on a Reddit error, 130 on interruption, 2 on bad arguments.

### Project
- Tests (pytest, fixtures from real Reddit responses), ruff, MIT license, `.gitignore` for
  scrape outputs.

## 0.1.0 (2026-10-12)

First version, written with Gemini: four commands, 32 fields, JSON/JSONL/CSV.
