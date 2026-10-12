"""Command line interface for ReddSpectre.

Data goes to the output file, or as JSONL to stdout when no ``-o`` is given. Everything
cosmetic (banner, progress, summary, log lines) goes to stderr, so piping works.
``--watch`` turns a run into a monitor: the same scrape repeats every interval into a
SQLite file, storing only what is new.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections.abc import AsyncIterator
from typing import Any

from rich.console import Console
from rich.logging import RichHandler
from rich.progress import BarColumn, Progress, SpinnerColumn, TaskProgressColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from . import __version__
from .client import RedditError
from .exporter import SQLITE_SUFFIXES, SUPPORTED, Exporter, load_known_ids
from .scraper import Filters, ReddSpectre, parse_duration, parse_when

log = logging.getLogger("reddspectre")

BANNER = r"""[bold red]
  ____           _     _ ____                  _
 |  _ \ ___   __| | __| / ___| _ __   ___  ___| |_ _ __ ___
 | |_) / _ \ / _` |/ _` \___ \| '_ \ / _ \/ __| __| '__/ _ \
 |  _ <  __/| (_| | (_| |___) | |_) |  __/ (__| |_| | |  __/
 |_| \_\___| \__,_|\__,_|____/| .__/ \___|\___|\__|_|  \___|
                              |_|[/bold red]
[dim italic]Zero-browser Reddit scraper v{version} · LordVecna Edition[/dim italic]
"""

TIME_FRAMES = ["all", "hour", "day", "week", "month", "year"]
COMMENT_SORTS = ["confidence", "top", "new", "controversial", "old", "qa"]
MIN_WATCH_SECONDS = 30.0


def _utf8_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass


# ----------------------------------------------------------------------------- arguments


def _add_comment_options(
    parser: argparse.ArgumentParser, *, default_max: int, default_depth: int, toggle: bool
) -> None:
    group = parser.add_argument_group("comments")
    if toggle:
        group.add_argument(
            "--comments",
            action="store_true",
            help="also fetch comments of every post (one request per post, more when --max-comments exceeds ~200)",
        )
    else:
        group.add_argument("--no-comments", action="store_true", help="fetch the post only")
    group.add_argument("--max-comments", type=int, default=default_max, help="max comments per post")
    group.add_argument("--depth", type=int, default=default_depth, help="max comment depth (0 = top level only)")
    group.add_argument("--comment-sort", choices=COMMENT_SORTS, default="confidence", help="comment order")
    group.add_argument(
        "--no-expand-more",
        action="store_true",
        help="do not fetch collapsed 'load more comments' branches (saves requests)",
    )


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    out = common.add_argument_group("output")
    out.add_argument(
        "-o",
        "--out",
        metavar="FILE",
        help="output file; format by extension: " + ", ".join(SUPPORTED) + ". Without -o items go to stdout as JSONL",
    )
    out.add_argument(
        "--only-new",
        action="store_true",
        help="with a SQLite output: skip items already stored and stop at the first known page",
    )
    out.add_argument(
        "--watch",
        metavar="INTERVAL",
        help="repeat forever every INTERVAL (30s, 15m, 2h); needs a SQLite output and implies --only-new",
    )
    net = common.add_argument_group("network")
    net.add_argument("--proxy", action="append", metavar="URL", help="proxy URL, repeatable; rotates per session")
    net.add_argument("-c", "--concurrency", type=int, default=10, help="parallel requests")
    net.add_argument("--delay", type=float, default=0.0, help="minimum seconds between requests (polite mode)")
    net.add_argument("--timeout", type=float, default=15.0, help="request timeout, seconds")
    net.add_argument("--retries", type=int, default=4, help="retries for 429 / 5xx / network errors")
    net.add_argument(
        "--impersonate",
        metavar="TARGET",
        help="curl_cffi TLS impersonation, e.g. chrome. Off by default: Reddit answers 403 to it (checked 2026-10)",
    )
    display = common.add_argument_group("display")
    display.add_argument("-v", "--verbose", action="count", default=0, help="-v requests and waits, -vv debug")
    display.add_argument("-q", "--quiet", action="store_true", help="no banner, progress or summary")
    display.add_argument("--no-banner", action="store_true", help="skip the banner")
    display.add_argument("--log", metavar="FILE", help="also append log lines (INFO, or DEBUG with -vv) to FILE")

    filters = argparse.ArgumentParser(add_help=False)
    group = filters.add_argument_group("filters (posts; comments follow their post)")
    group.add_argument("--min-score", type=int, metavar="N")
    group.add_argument("--since", metavar="WHEN", help="newer than: YYYY-MM-DD, ISO datetime, or 7d / 24h / 30m / 2w")
    group.add_argument("--until", metavar="WHEN", help="older than (same formats)")
    group.add_argument("--no-nsfw", action="store_true", help="skip posts marked over 18")
    group.add_argument("--flair", metavar="TEXT", help="post flair contains TEXT (case-insensitive)")
    group.add_argument("--author", metavar="USER", help="only this author")
    group.add_argument("--keyword", metavar="REGEX", help="regex that must match title or body (case-insensitive)")

    parser = argparse.ArgumentParser(
        prog="reddspectre",
        description="Zero-browser Reddit scraper: subreddits, search, users and full comment threads.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"reddspectre {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    fmt = argparse.ArgumentDefaultsHelpFormatter

    sub = subparsers.add_parser(
        "sub", parents=[common, filters], formatter_class=fmt, help="posts of a subreddit (r/a+b for several)"
    )
    sub.add_argument("subreddit", help="name, with or without r/")
    sub.add_argument("-n", "--limit", type=int, default=100, help="max posts (Reddit listings end around 1000)")
    sub.add_argument("--sort", choices=["new", "hot", "top", "rising", "controversial"], default="new")
    sub.add_argument("--time", choices=TIME_FRAMES, default="all", help="time frame for top/controversial")
    _add_comment_options(sub, default_max=20, default_depth=3, toggle=True)

    search = subparsers.add_parser(
        "search", parents=[common, filters], formatter_class=fmt, help="search posts site-wide or in one subreddit"
    )
    search.add_argument("query", help="search words or phrase")
    search.add_argument("-s", "--sub", metavar="SUBREDDIT", help="restrict to this subreddit")
    search.add_argument("-n", "--limit", type=int, default=100, help="max posts")
    search.add_argument("--sort", choices=["relevance", "hot", "top", "new", "comments"], default="relevance")
    search.add_argument("--time", choices=TIME_FRAMES, default="all")
    _add_comment_options(search, default_max=20, default_depth=3, toggle=True)

    post = subparsers.add_parser(
        "post", parents=[common], formatter_class=fmt, help="one or more posts with their comment threads"
    )
    post.add_argument("url", nargs="+", metavar="URL", help="post URL, redd.it link, share link, id or t3_id")
    _add_comment_options(post, default_max=100, default_depth=5, toggle=False)

    user = subparsers.add_parser(
        "user", parents=[common, filters], formatter_class=fmt, help="posts and/or comments of a user"
    )
    user.add_argument("username", help="name, with or without u/")
    user.add_argument("-n", "--limit", type=int, default=100, help="max items")
    user.add_argument("--type", choices=["submitted", "comments", "overview"], default="submitted", help="what to list")
    user.add_argument("--sort", choices=["new", "hot", "top", "controversial"], default="new")
    user.add_argument("--time", choices=TIME_FRAMES, default="all", help="time frame for top/controversial")
    _add_comment_options(user, default_max=20, default_depth=3, toggle=True)
    return parser


def build_filters(args: argparse.Namespace) -> Filters | None:
    if not hasattr(args, "min_score"):
        return None
    filters = Filters(
        min_score=args.min_score,
        since=parse_when(args.since),
        until=parse_when(args.until),
        no_nsfw=args.no_nsfw,
        flair=args.flair,
        author=args.author,
        keyword=args.keyword,
    )
    return filters if filters.is_active() else None


# ----------------------------------------------------------------------------- running


def make_stream(
    args: argparse.Namespace, scraper: ReddSpectre, filters: Filters | None, known: set[str] | None
) -> AsyncIterator[dict[str, Any]]:
    if args.command == "post":

        async def posts() -> AsyncIterator[dict[str, Any]]:
            failed = 0
            last_error: RedditError | None = None
            for ref in args.url:
                try:
                    items = await scraper.scrape_post(
                        ref,
                        include_comments=not args.no_comments,
                        max_comments=args.max_comments,
                        max_depth=args.depth,
                        comment_sort=args.comment_sort,
                        expand_more=not args.no_expand_more,
                    )
                except RedditError as exc:
                    if len(args.url) == 1:
                        raise
                    failed += 1
                    last_error = exc
                    log.error("%s: %s", ref, exc)
                    continue
                for item in items:
                    yield item
            if last_error is not None and failed == len(args.url):
                raise last_error

        return posts()

    comment_kwargs = {
        "include_comments": args.comments,
        "max_comments_per_post": args.max_comments,
        "max_comments_depth": args.depth,
        "comment_sort": args.comment_sort,
        "expand_more": not args.no_expand_more,
        "filters": filters,
        "known_ids": known,
    }
    if args.command == "sub":
        return scraper.stream_subreddit(
            args.subreddit, sort=args.sort, time_frame=args.time, limit=args.limit, **comment_kwargs
        )
    if args.command == "search":
        return scraper.stream_search(
            args.query, sort=args.sort, time_frame=args.time, limit=args.limit, subreddit=args.sub, **comment_kwargs
        )
    if args.command == "user":
        return scraper.stream_user(
            args.username, sort=args.sort, time_frame=args.time, limit=args.limit, kind=args.type, **comment_kwargs
        )
    raise SystemExit(f"unknown command {args.command}")


def _configure_logging(verbosity: int, console: Console, log_file: str | None) -> None:
    console_level = logging.WARNING if verbosity == 0 else logging.INFO if verbosity == 1 else logging.DEBUG
    handlers: list[logging.Handler] = [
        RichHandler(
            level=console_level,
            console=console,
            show_path=False,
            show_time=verbosity >= 2,
            markup=False,
            rich_tracebacks=False,
        )
    ]
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG if verbosity >= 2 else logging.INFO)
        file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        handlers.append(file_handler)
    level = min(h.level for h in handlers)
    logging.basicConfig(level=level, format="%(message)s", handlers=handlers, force=True)
    logging.getLogger("reddspectre").setLevel(level)


def _print_summary(
    ui: Console,
    *,
    posts: int,
    comments: int,
    elapsed: float,
    scraper: ReddSpectre,
    out: str | None,
    outcome: str,
    pass_no: int | None = None,
) -> None:
    client_stats = scraper.client.snapshot_stats()
    stats = scraper.stats
    total = posts + comments
    colour = {"ok": "green", "error": "red", "aborted": "yellow"}[outcome]
    title = f"Scrape summary ({outcome})" if pass_no is None else f"Pass {pass_no} summary ({outcome})"
    table = Table(title=f"[bold {colour}]{title}[/bold {colour}]", box=None, show_header=False)
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="bold", overflow="fold")
    table.add_row("Items", f"{total}  (posts {posts}, comments {comments})")
    table.add_row("Elapsed", f"{elapsed:.1f} s  ({total / max(elapsed, 0.001):.1f} items/s)")
    waits = f"{client_stats.rate_waits} ({client_stats.rate_wait_seconds:.0f} s)" if client_stats.rate_waits else "0"
    table.add_row(
        "Requests",
        f"{client_stats.requests}  (retries {client_stats.retries}, 429s {client_stats.rate_limited}, "
        f"rate-limit waits {waits})",
    )
    limiter = getattr(scraper.client, "limiter", None)
    quota = limiter.quota() if limiter is not None else None
    if quota is not None:
        left, seconds = quota
        table.add_row("Quota left", f"{left:.0f} requests in this window, resets in {seconds:.0f} s")
    if stats.threads:
        table.add_row(
            "Threads", f"{stats.threads}  (failed {stats.threads_failed}, 'more' requests {stats.more_requests})"
        )
    skipped = []
    if stats.duplicates:
        skipped.append(f"duplicates {stats.duplicates}")
    if stats.filtered:
        skipped.append(f"filtered out {stats.filtered}")
    if stats.already_known:
        skipped.append(f"already stored {stats.already_known}")
    if skipped:
        table.add_row("Skipped", ", ".join(skipped))
    if out:
        table.add_row("Saved to", f"[underline]{out}[/underline]")
    ui.print(table)


async def run_pass(
    args: argparse.Namespace,
    ui: Console,
    scraper: ReddSpectre,
    filters: Filters | None,
    known: set[str] | None,
    *,
    pass_no: int | None,
) -> tuple[RedditError | None, int, int]:
    """One scrape: stream items into the exporter (or stdout), always close, always summarise.

    Returns ``(error, posts, comments)``.
    """
    scraper.reset_stats()
    exporter = Exporter(args.out) if args.out else None
    progress = Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        TimeElapsedColumn(),
        console=ui,
        transient=True,
        disable=args.quiet or not ui.is_terminal,
    )
    task = progress.add_task("starting", total=getattr(args, "limit", None))
    posts = comments = 0
    outcome = "ok"
    error: RedditError | None = None
    started = time.monotonic()

    def emit(item: dict[str, Any]) -> None:
        nonlocal posts, comments
        if exporter is not None:
            exporter.write(item)
        else:
            sys.stdout.write(json.dumps(item, ensure_ascii=False) + "\n")
        if item.get("submissionType") == "post":
            posts += 1
            progress.advance(task)
        else:
            comments += 1
        progress.update(task, description=f"posts {posts} · comments {comments}")

    try:
        if exporter is not None:
            exporter.open()
        with progress:
            async for item in make_stream(args, scraper, filters, known):
                emit(item)
    except RedditError as exc:
        error = exc
        outcome = "error"
    except asyncio.CancelledError:
        outcome = "aborted"
        raise
    finally:
        if exporter is not None:
            exporter.close()
        try:
            sys.stdout.flush()
        except (BrokenPipeError, OSError):
            pass
        if error is not None:
            ui.print(f"[bold red]✖ {error}[/bold red]")
        if not args.quiet:
            _print_summary(
                ui,
                posts=posts,
                comments=comments,
                elapsed=time.monotonic() - started,
                scraper=scraper,
                out=args.out,
                outcome=outcome,
                pass_no=pass_no,
            )
    return error, posts, comments


async def run(args: argparse.Namespace, ui: Console, parser: argparse.ArgumentParser) -> int:
    _configure_logging(args.verbose, ui, args.log)
    if ui.is_terminal and not args.quiet and not args.no_banner:
        ui.print(BANNER.replace("{version}", __version__))

    try:
        filters = build_filters(args)
        interval = parse_duration(args.watch) if args.watch else None
    except Exception as exc:  # bad --since / --until / --keyword / --watch
        parser.error(str(exc))
    if interval is not None and interval < MIN_WATCH_SECONDS:
        parser.error(
            f"--watch interval must be at least {MIN_WATCH_SECONDS:.0f}s (Reddit allows ~100 requests per 10 min)"
        )

    incremental = args.only_new or interval is not None
    known: set[str] | None = None
    if args.out:
        try:
            fmt = Exporter(args.out).fmt
        except ValueError as exc:
            parser.error(str(exc))
        if incremental and fmt not in SQLITE_SUFFIXES:
            parser.error("--only-new and --watch need a SQLite output, e.g. -o posts.db")
    elif incremental:
        parser.error("--only-new and --watch need a SQLite output, e.g. -o posts.db")
    if incremental:
        known = load_known_ids(args.out)

    scraper = ReddSpectre(
        proxies=args.proxy,
        concurrency=args.concurrency,
        impersonate=args.impersonate,
        timeout=args.timeout,
        min_delay=args.delay,
        max_retries=args.retries,
        expand_more=not args.no_expand_more,
    )
    code = 0
    if interval is not None:
        log.info("watch mode: '%s' every %s into %s", " ".join(sys.argv[1:]) or args.command, args.watch, args.out)
    async with scraper:
        pass_no = 0
        while True:
            pass_no += 1
            error, posts, comments = await run_pass(
                args, ui, scraper, filters, known, pass_no=pass_no if interval else None
            )
            if interval is None:
                code = 1 if error is not None else 0
                break
            stats = scraper.client.snapshot_stats()
            log.info(
                "pass %d: %d posts, %d comments, %d requests, %d already stored%s",
                pass_no,
                posts,
                comments,
                stats.requests,
                scraper.stats.already_known,
                f", error: {error}" if error is not None else "",
            )
            if error is not None:
                log.warning("pass %d failed (%s); trying again in %s", pass_no, error, args.watch)
            known = load_known_ids(args.out)
            if not args.quiet:
                ui.print(f"[dim]next pass in {args.watch} (Ctrl+C to stop)[/dim]")
            await asyncio.sleep(interval)
    return code


def main(argv: list[str] | None = None) -> int:
    _utf8_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    ui = Console(stderr=True)
    if sys.platform == "win32":
        # curl_cffi needs add_reader(); the proactor loop would spawn an extra selector thread.
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        return asyncio.run(run(args, ui, parser))
    except KeyboardInterrupt:
        ui.print("[bold yellow]Aborted by user; whatever was collected is saved.[/bold yellow]")
        return 130
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    sys.exit(main())
