# ReddSpectre 🦇

**Zero-browser Reddit scraper (Python 3.12 + CLI).** Subreddits, search, user pages and
full comment trees to JSON / JSONL / CSV / SQLite / Markdown. No Playwright, no Selenium,
no API key: only Reddit's public JSON through `curl_cffi`, so it starts instantly.

## Features

- **Four commands:** `sub`, `search`, `post`, `user` (posts, comments or both).
- **Full threads:** collapsed "load more comments" branches are expanded through
  `/api/morechildren`, so `--max-comments 600` really returns 600, not 200.
- **Honest rate limiting:** the client reads Reddit's `x-ratelimit-*` headers and waits for
  the window to reset instead of collecting 429s and losing what it had.
- **Streaming output:** items are written as they arrive; Ctrl+C or an error leaves a valid
  file with everything collected so far.
- **Filters:** `--since 7d`, `--min-score`, `--keyword`, `--flair`, `--author`, `--no-nsfw`.
- **Incremental mode:** SQLite plus `--only-new` to fetch only what is new, or `--watch 15m`
  to keep polling a subreddit.
- **51 fields per item,** one schema for posts and comments: full-resolution image
  (`previewImageUrl`), galleries with correct extensions, v.redd.it video, polls,
  `stickied / locked / spoiler / removedByCategory` flags; comments carry `isSubmitter`
  (reply by the original poster) and `depth`.
- **Clear errors:** `r/lounge is for Reddit Premium members only (HTTP 403)` with exit
  code 1, not a traceback after five retries.

## Installation

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/) (or plain `pip`).

```bash
git clone https://github.com/volobado/reddit-scraper-zero-browser.git
cd reddit-scraper-zero-browser
uv sync
uv run reddspectre --version

# without uv
pip install .
reddspectre --version
```

## Commands

Without `-o`, items are printed to stdout as JSONL (one JSON object per line), while the
banner, progress bar and summary go to stderr, so `| jq` and `> file` work as expected.
The output format follows the extension of `-o`: `.json`, `.jsonl`, `.csv` (with a BOM so
Excel reads UTF-8), `.db` / `.sqlite`, `.md` (a digest for NotebookLM or an LLM).

### Subreddit

```bash
# 100 newest posts
uv run reddspectre sub webscraping -n 100 -o webscraping.jsonl

# Top of the week with comments (up to 20 per post, depth 3), as CSV for Excel
uv run reddspectre sub technology -n 50 --sort top --time week --comments -o tech.csv

# Several subreddits at once, last 3 days only, score 10 and up
uv run reddspectre sub "python+learnpython" -n 200 --since 3d --min-score 10 -o py.json

# Monitoring: every run appends only new posts to the database
uv run reddspectre sub webscraping -n 100 --only-new -o webscraping.db

# Same, but looping by itself every 15 minutes with a log file (Ctrl+C stops it)
uv run reddspectre sub webscraping -n 100 --watch 15m --log watch.log -o webscraping.db
```

Sort orders: `new`, `hot`, `top`, `rising`, `controversial`; `--time` applies to `top` and
`controversial`. Reddit listings end at roughly 1000 items.

### Search

```bash
uv run reddspectre search "web scraping python" -n 50 -o search.json
uv run reddspectre search "proxies" --sub webscraping --sort new --since 2026-09-01 -o proxies.md
```

### A single post with its thread

```bash
uv run reddspectre post "https://www.reddit.com/r/webscraping/comments/1jbd7ph/..." -o thread.md
uv run reddspectre post https://redd.it/1jbd7ph --max-comments 500 --depth 10 -o thread.jsonl
uv run reddspectre post t3_1jbd7ph --comment-sort top --no-expand-more -o thread.json

# Several threads into one digest; a broken link is logged, the rest are collected
uv run reddspectre post https://redd.it/1jbd7ph https://redd.it/1x3p6bb -o threads.md
```

Accepted forms: full URL, `redd.it/<id>`, share link `/r/x/s/...`, `/gallery/<id>`, a bare
id, `t3_<id>`, and any number of them at once. Comment sorts: `confidence` (as on the
site), `top`, `new`, `controversial`, `old`, `qa`.

### User

```bash
uv run reddspectre user spez -n 50 -o spez_posts.json
uv run reddspectre user spez --type comments -n 100 -o spez_comments.jsonl
uv run reddspectre user spez --type overview --sort top --time year -o spez.csv
```

### Common options

| Option | What it does |
|---|---|
| `-o FILE` | output file; format by extension |
| `--only-new` | with SQLite: skip what is already stored and stop at the first known page |
| `--watch INTERVAL` | repeat forever every `30s` / `15m` / `2h` into SQLite, implies `--only-new`; 30 s minimum |
| `--log FILE` | append log lines (INFO, or DEBUG with `-vv`) to a file, handy with `--watch` |
| `--comments`, `--max-comments N`, `--depth N` | comments for the posts of a listing |
| `--comment-sort`, `--no-expand-more` | comment order; skip collapsed branches (saves requests) |
| `--since`, `--until` | `2026-10-01`, an ISO datetime, or `7d` / `24h` / `30m` / `2w` |
| `--min-score`, `--keyword REGEX`, `--flair`, `--author`, `--no-nsfw` | post filters |
| `--proxy URL` | repeatable; rotates whenever the session is rebuilt (every 30 minutes or after a 403) |
| `-c N` | parallel requests (default 10) |
| `--delay SEC` | minimum pause between requests, polite mode |
| `--timeout`, `--retries` | request timeout and retries for 429 / 5xx / network errors |
| `-v`, `-vv` | log requests and waits; debug |
| `-q`, `--no-banner` | quieter |

## Reddit's quota, and why it matters

An anonymous session gets **100 requests per 10-minute window** (Reddit reports the
remainder in response headers). One listing page is 1 request per 100 posts;
`--comments` adds 1 request per post (plus one per 100 collapsed comments when `more`
branches are expanded). So `sub X -n 200 --comments` is about 200 requests: the client
waits for the window to reset by itself, which shows up in the summary as
`rate-limit waits`, and the `Quota left` line tells you how many requests remain in the
current window. To avoid waiting, lower `-n`, add `--no-expand-more` or `--delay`.
Verified live on 2026-10-12: 115 posts with comments (220 requests) took 195 seconds with
a single 190-second pause and not one 429.

What was checked on 2026-10-12 and is built into the client:
- The cookies from `old.reddit.com/login` are required: without them `www.reddit.com/*.json`
  answers 403.
- TLS impersonation of Chrome (`--impersonate chrome`) gets 403, so it is off. Plain curl
  TLS with the honest User-Agent `reddspectre/<version>` works.
- `raw_json=1` on every request: texts arrive without `&amp;`, `&lt;`, `&gt;`.

## Python API

```python
import asyncio
from reddspectre import Exporter, Filters, ReddSpectre, parse_when

async def main():
    async with ReddSpectre(concurrency=10) as scraper:
        with Exporter("out.jsonl") as out:
            async for item in scraper.stream_subreddit(
                "webscraping", sort="new", limit=50,
                include_comments=True, max_comments_per_post=10,
                filters=Filters(since=parse_when("7d"), min_score=5),
            ):
                out.write(item)
                if item["submissionType"] == "comment":
                    print("  ->", item["author"], item["bodyText"][:60])
                else:
                    print(item["postType"], item["title"], item["score"])

        thread = await scraper.scrape_post("https://redd.it/1jbd7ph", max_comments=300)
        print(scraper.stats, scraper.client.snapshot_stats())

asyncio.run(main())
```

`parse_when` turns `7d` or `2026-10-01` into epoch seconds for `since` / `until`.
Errors: `RedditNotFound`, `RedditForbidden`, `RedditRateLimited`, `RedditServerError`,
`RedditNetworkError`, `RedditBadRequest`, all subclasses of `RedditError`.

## Item schema

The same fields for posts and comments (what does not apply is `None`):
`submissionId`, `submissionIdPrefixed`, `submissionType` (post / comment), `submissionUrl`,
`submittedAt`, `createdUtc`, `parentId`, `parentPostId`, `parentPostUrl`, `depth`,
`postType` (text / link / image / gallery / video / poll), `title`, `bodyText`, `url`,
`externalUrl`, `author`, `authorFullname`, `authorFlairText`, `authorFlairImageUrl`,
`isSubmitter`, `distinguished`, `subreddit`, `score`, `upvoteRatio`, `numComments`,
`numCrossposts`, `controversiality`, `scoreHidden`, `totalAwards`, `postFlair`,
`linkFlairCssClass`, `isNSFW`, `isSpoiler`, `isStickied`, `isLocked`, `isOriginalContent`,
`isEdited`, `editedAt`, `removedByCategory`, `isCrosspost`, `crosspostParentId`,
`crosspostParentUrl`, `thumbnailUrl`, `previewImageUrl`, `imageUrl`, `videoUrl`,
`videoHlsUrl`, `videoDurationSec`, `videoHasAudio`, `galleryImagesUrls`, `pollOptions`.

In SQLite the table is `items` with the same columns plus `scrapedAt`; the key is
`submissionIdPrefixed`.

## Development

```bash
uv run pytest            # offline tests on saved Reddit responses (tests/fixtures)
uv run pytest -m live    # 4 live requests to Reddit
uv run ruff check src tests && uv run ruff format --check src tests
```

History of changes in `CHANGELOG.md`; the original audit that led to the rewrite and the
work plan with its status live in `docs/`.

## Limits and ethics

The tool only visits public pages and respects the limit Reddit itself announces in its
headers. Working around the limit by rotating sessions is deliberately not implemented.
For large volumes or regular monitoring the right path is official OAuth access
(100 requests per minute); the client is designed so that it can be added without a rewrite.
