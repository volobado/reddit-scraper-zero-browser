# ReddSpectre repair plan (version 0.2.x)

Basis: `AUDIT-2026-10-12.md`. Executed by Claude.

Status on 2026-10-12: phases 0–8 done, then a second review pass (phase 9); 72 offline
tests and 1 live test green, version 0.2.1.

Rules for the whole job:
- Every phase is verified live against Reddit, not "by reading the code".
- Reddit's quota: 100 requests per 10-minute window per session. Tests stay frugal.
- TLS impersonation of Chrome is NOT enabled by default: checked on 2026-10-12, Reddit
  answers 403 on both the bootstrap and the JSON. Plain curl TLS with an honest UA works.
  The option stays for experiments, the default is `impersonate=None`.
- The cookie bootstrap via `old.reddit.com/login` stays: without it www answers 403.
- No working around the limit by rotating sessions. Only an honest limiter and optional OAuth.

---

## Phase 0. Infrastructure

- [x] Rebuild the environment (`uv sync`), put the project under version control.
- [x] `.gitignore`: scrape outputs (`*.json`, `*.jsonl`, `*.csv`, `*.db`, `*.sqlite`, `out/`),
      except `tests/fixtures/*.json`.
- [x] `pyproject.toml`: description, version 0.2.0, MIT license, `dev` group (pytest,
      pytest-asyncio, ruff), ruff settings (line-length 120) and pytest (`asyncio_mode = auto`,
      live tests behind the `live` marker).
- [x] First commit on `main`.

## Phase 1. Client (`client.py`): data and reliability

- [x] `raw_json=1` on every request.
- [x] `RateLimiter` driven by `x-ratelimit-remaining` / `x-ratelimit-reset`; on 429 wait for
      `Retry-After` or `reset`. `min_delay` option (`--delay`).
- [x] Exceptions: `RedditError`, `RedditNotFound`, `RedditForbidden` (with `reason`),
      `RedditRateLimited`, `RedditBadRequest`, `RedditServerError`, `RedditNetworkError`.
- [x] Session rebuild by generation; the old session is closed only in `close()`.
- [x] Retries only for 429 / 5xx / network / non-JSON 200. 4xx is not retried.
- [x] Honest User-Agent `reddspectre/<version> (zero-browser reddit scraper)`; `impersonate`
      as an option; `--timeout`, `--retries`.
- [x] Proxies as a list, round-robin on session rebuild; `--proxy` repeatable.
- [x] Logging through `logging.getLogger("reddspectre")`.
- [x] `ClientStats`: requests, retries, 429s, sessions, rate-limit waits.

Verified live: private r/lounge → `is for Reddit Premium members only (HTTP 403,
reason=gold_only)`; nonexistent → `does not exist (HTTP 404)`; zero entities in exports;
25 posts + 124 comments in 2.7 s without loss.

## Phase 2. Parser (`parser.py`): schema and fields

- [x] `blank_item()` and one `FIELDS` tuple (51 fields); `parse_post` / `parse_comment`
      only fill it in.
- [x] New post and comment fields (see CHANGELOG).
- [x] Galleries from `media_metadata` (mime → extension, `s.u/gif/mp4`), crossposts from
      `crosspost_parent_list[0]`.
- [x] Flair emoji by `e == "emoji"`.
- [x] Video: `fallback_url` without query, `hls_url`, `duration`, `has_audio`.
- [x] Dead `image_url` branch removed; type order poll → gallery → video → image → text → link.

## Phase 3. Scraper (`scraper.py`): completeness and control

- [x] Shared `_stream_listing()` for `sub`, `search`, `user`.
- [x] Deduplication by `submissionIdPrefixed` across pages.
- [x] `resolve_post_id()`: id, `t3_`, `/comments/`, `redd.it`, `/gallery/`, `/duplicates/`,
      share links via redirect.
- [x] `limit`, `depth`, `sort` in the thread request; `comment_sort`.
- [x] `more` expansion through `/api/morechildren` (batches of 100) honouring `max_depth`
      and `max_comments`; `--no-expand-more`.
- [x] Listing comments through `as_completed`; thread errors are logged and counted.
- [x] `user --type submitted|comments|overview`, `controversial` sort.
- [x] `Filters` (`min_score`, `since`, `until`, `no_nsfw`, `flair`, `author`, `keyword`);
      `since` stops pagination on chronological sorts.
- [x] `known_ids` (`--only-new`): stop at the first known item (new) or at a fully known
      page (hot/top).
- [x] `_walk_comments` as a plain function; it does not descend past `max_depth`.

Verified live: a post with 4839 comments at `--max-comments 600` → 600 in 3 requests
(before: 200 whatever the limit); `redd.it/…` and `t3_…` work.

## Phase 4. Export (`exporter.py`)

- [x] `Exporter` with `open / write / close`: JSONL line by line, JSON array incrementally,
      CSV `utf-8-sig`, SQLite (`INSERT OR REPLACE`, key `submissionIdPrefixed`, `scrapedAt`),
      Markdown digest with comments under their posts.
- [x] Unknown extension → `ValueError` listing the formats.
- [x] `close()` in `finally`: partial results survive errors and Ctrl+C (test
      `test_cancellation_keeps_partial_output`).
- [x] Lists in CSV joined with `|`, dicts as JSON.

## Phase 5. CLI (`cli.py`)

- [x] Parent parsers: common options, filters, comment options.
- [x] `--version`.
- [x] Without `-o` JSONL to stdout; everything else to stderr; no `force_terminal`.
- [x] Progress by posts, comment counter in the description.
- [x] `RedditError` → one line and exit code 1; Ctrl+C → saved, code 130; summary always.
- [x] `post --comment-sort/--no-expand-more/--no-comments`, `user --type`, `sub controversial`.
- [x] Banner without the stray `\`, shown only in a terminal.
- [x] `sys.stdout.reconfigure(encoding="utf-8")`.

## Phase 6. Tests (`tests/`)

- [x] Fixtures: `pics_listing.json`, `videos_listing.json`, `thread.json` (real Reddit
      responses from 2026-10-12, `raw_json=1`).
- [x] `test_parser.py`, `test_scraper.py` (fake client, no network), `test_exporter.py`,
      `test_ratelimit.py`, `test_cli.py`.
- [x] `test_live.py` (`live` marker, 4 requests).

## Phase 7. Documentation

- [x] README rewritten for 0.2.0.
- [x] `CHANGELOG.md`.
- [x] Boxes ticked in this plan.

## Phase 8. Hand-over

- [x] `uv run pytest`, `uv run ruff check src tests`, `ruff format --check`.
- [x] Live run of every command exporting to every format.
- [x] Commit and push.

## Phase 9. Second pass: re-verification and improvements (0.2.1)

- [x] The limiter tested live: 115 posts + comments, 220 requests, a 190 s pause at the
      window reset, zero 429s, exit code 0.
- [x] Found and fixed: the pause was logged and counted once per each of the 10 concurrent
      tasks.
- [x] Stickied posts no longer end a chronological scan (`--since`, `--only-new`).
- [x] Guard against a repeated `after` cursor.
- [x] Retry on a network error during session bootstrap.
- [x] `--watch INTERVAL` (SQLite monitor, 30 s minimum), `--log FILE`, `post` with several
      links, a `Quota left` line in the summary.
- [x] Selector event loop on Windows as curl_cffi recommends.
- [x] Version in `_version.py`; `uv build` produces a wheel and an sdist.
- [x] Tests: +12 (monitor, several links, log file, cursor, sticky, durations, quota).

---

## Deferred (not in this version, but designed for)

- OAuth mode (`client_id`/`secret`, `oauth.reddit.com`, the official 100 requests per
  minute): the client gets a `base_url` and an `Authorization` header, nothing else changes.
- Multi-account / session pool: not doing it, that is working around the limit.
- Parquet/XLSX export: via pandas as an optional dependency if ever needed.
- Comment search (`search.json?type=comment`) and `sr` (subreddit search).
