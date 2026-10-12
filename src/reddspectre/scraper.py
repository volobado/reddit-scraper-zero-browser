"""High-level scraper: subreddit listings, search, user pages and full comment threads.

Everything streams: listings are paged lazily, comments of each page are fetched
concurrently and yielded as the threads complete, so a consumer can write items to disk as
they arrive and keep whatever was collected if the run is interrupted.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncGenerator
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from .client import RedditError, RedditNotFound, ReddSpectreClient
from .parser import parse_comment, parse_item, parse_post

__all__ = ["Filters", "ReddSpectre", "ScrapeStats", "parse_duration", "parse_when"]

log = logging.getLogger("reddspectre")

REDDIT = "https://www.reddit.com"
PAGE_SIZE = 100  # Reddit's maximum per listing request
MORE_BATCH = 100  # Reddit's maximum ids per /api/morechildren call

SubSort = Literal["new", "hot", "top", "rising", "controversial"]
SearchSort = Literal["relevance", "hot", "top", "new", "comments"]
UserSort = Literal["new", "hot", "top", "controversial"]
UserKind = Literal["submitted", "comments", "overview"]
CommentSort = Literal["confidence", "top", "new", "controversial", "old", "qa"]
TimeFrame = Literal["all", "hour", "day", "week", "month", "year"]

_POST_ID = re.compile(r"^(?:t3_)?([a-z0-9]{2,12})$", re.IGNORECASE)
_URL_ID = re.compile(r"/(?:comments|gallery|duplicates)/([a-z0-9]+)", re.IGNORECASE)
_SHORT_LINK = re.compile(r"^https?://(?:www\.)?redd\.it/([a-z0-9]+)", re.IGNORECASE)
_DURATION = re.compile(r"^(\d+(?:\.\d+)?)\s*([smhdw])$", re.IGNORECASE)
_DURATION_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: str | int | float) -> float:
    """``90``, ``30s``, ``15m``, ``2h``, ``7d`` or ``1w`` as seconds."""
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    match = _DURATION.match(text)
    if match:
        return float(match.group(1)) * _DURATION_SECONDS[match.group(2).lower()]
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"cannot parse duration {value!r}: use 30s, 15m, 2h, 7d or 1w") from exc


def parse_when(value: str | int | float | datetime | None) -> float | None:
    """Turn ``2026-10-12``, an ISO datetime, ``7d`` / ``24h`` / ``30m`` / ``2w`` or a datetime into epoch seconds."""
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.timestamp()
    if isinstance(value, int | float):
        return float(value)
    text = str(value).strip()
    if _DURATION.match(text):
        return (datetime.now(UTC) - timedelta(seconds=parse_duration(text))).timestamp()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"cannot parse time {value!r}: use YYYY-MM-DD, an ISO datetime, or 7d / 24h / 30m") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


@dataclass
class Filters:
    """Post-side filters applied to listing items before they are yielded (and before comments are fetched)."""

    min_score: int | None = None
    since: float | None = None  # epoch seconds, see parse_when()
    until: float | None = None
    no_nsfw: bool = False
    flair: str | None = None  # case-insensitive substring of the post flair
    author: str | None = None  # case-insensitive, with or without u/
    keyword: str | None = None  # regex, case-insensitive, against title and body

    def __post_init__(self) -> None:
        self._keyword_re = re.compile(self.keyword, re.IGNORECASE) if self.keyword else None
        self._author = self.author.lower().removeprefix("u/") if self.author else None

    def is_active(self) -> bool:
        return any(
            v not in (None, False)
            for v in (self.min_score, self.since, self.until, self.no_nsfw, self.flair, self.author, self.keyword)
        )

    def matches(self, item: dict[str, Any]) -> bool:
        if self.min_score is not None and (item.get("score") or 0) < self.min_score:
            return False
        if self.no_nsfw and item.get("isNSFW"):
            return False
        created = item.get("createdUtc")
        if created is not None:
            if self.since is not None and created < self.since:
                return False
            if self.until is not None and created > self.until:
                return False
        if self.flair and self.flair.lower() not in (item.get("postFlair") or "").lower():
            return False
        if self._author and (item.get("author") or "").lower() != self._author:
            return False
        if self._keyword_re and not self._keyword_re.search(f"{item.get('title') or ''}\n{item.get('bodyText') or ''}"):
            return False
        return True


@dataclass
class ScrapeStats:
    posts: int = 0
    comments: int = 0
    threads: int = 0
    threads_failed: int = 0
    more_requests: int = 0
    duplicates: int = 0
    filtered: int = 0
    already_known: int = 0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _walk_comments(
    children: list[dict[str, Any]],
    out: list[dict[str, Any]],
    seen: set[str],
    more_ids: list[str],
    *,
    max_comments: int,
    max_depth: int,
    parent_post_url: str,
) -> None:
    """Flatten a nested comment tree; collect ids hidden behind ``more`` stubs."""
    for child in children:
        if len(out) >= max_comments:
            return
        kind = child.get("kind")
        data = child.get("data") or {}
        if kind == "more":
            if data.get("children") and (data.get("depth") or 0) <= max_depth:
                more_ids.extend(data["children"])
            continue
        if kind != "t1":
            continue
        if (data.get("depth") or 0) > max_depth:
            continue  # its replies are deeper still
        comment_id = data.get("id")
        if comment_id:
            if comment_id in seen:
                continue
            seen.add(comment_id)
        out.append(parse_comment(data, parent_post_url))
        replies = data.get("replies")
        if isinstance(replies, dict):
            _walk_comments(
                (replies.get("data") or {}).get("children") or [],
                out,
                seen,
                more_ids,
                max_comments=max_comments,
                max_depth=max_depth,
                parent_post_url=parent_post_url,
            )


class ReddSpectre:
    """Main scraper interface. Use as ``async with ReddSpectre() as scraper``."""

    def __init__(
        self,
        proxies: list[str] | str | None = None,
        concurrency: int = 10,
        impersonate: str | None = None,
        *,
        timeout: float = 15.0,
        min_delay: float = 0.0,
        max_retries: int = 4,
        expand_more: bool = True,
        user_agent: str | None = None,
        client: ReddSpectreClient | None = None,
    ) -> None:
        if client is None:
            kwargs: dict[str, Any] = {
                "proxies": proxies,
                "concurrency": concurrency,
                "impersonate": impersonate,
                "timeout": timeout,
                "min_delay": min_delay,
                "max_retries": max_retries,
            }
            if user_agent:
                kwargs["user_agent"] = user_agent
            client = ReddSpectreClient(**kwargs)
        self.client = client
        self.expand_more = expand_more
        self.stats = ScrapeStats()

    async def __aenter__(self) -> ReddSpectre:
        await self.client.__aenter__()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.client.__aexit__(exc_type, exc, tb)

    async def close(self) -> None:
        await self.client.close()

    def reset_stats(self) -> None:
        """Start counting from zero again (one watch pass = one set of numbers)."""
        self.stats = ScrapeStats()
        reset = getattr(self.client, "reset_stats", None)
        if callable(reset):
            reset()

    # -- single post ------------------------------------------------------------------

    async def resolve_post_id(self, ref: str) -> str:
        """Accepts a bare id, ``t3_id``, any reddit.com post URL, ``redd.it/id`` or a ``/r/x/s/..`` share link."""
        ref = ref.strip()
        match = _POST_ID.match(ref)
        if match:
            return match.group(1).lower()
        url = ref if re.match(r"^https?://", ref, re.IGNORECASE) else f"https://{ref}"
        for _ in range(4):
            match = _SHORT_LINK.match(url) or _URL_ID.search(url)
            if match:
                return match.group(1).lower()
            target = await self.client.resolve_redirect(url)
            if not target or target == url:
                break
            url = target
        raise RedditNotFound(f"no post id found in {ref!r}")

    async def scrape_post(
        self,
        post_url_or_id: str,
        include_comments: bool = True,
        max_comments: int = 100,
        max_depth: int = 5,
        comment_sort: CommentSort = "confidence",
        expand_more: bool | None = None,
    ) -> list[dict[str, Any]]:
        """The post followed by its comments, flattened, each comment carrying ``depth``."""
        post_id = await self.resolve_post_id(post_url_or_id)
        post, comments = await self._fetch_thread(
            post_id,
            include_comments=include_comments,
            max_comments=max_comments,
            max_depth=max_depth,
            comment_sort=comment_sort,
            expand_more=self.expand_more if expand_more is None else expand_more,
        )
        items: list[dict[str, Any]] = []
        if post is not None:
            items.append(post)
            self.stats.posts += 1
        items.extend(comments)
        self.stats.comments += len(comments)
        return items

    async def _fetch_thread(
        self,
        post_id: str,
        *,
        include_comments: bool,
        max_comments: int,
        max_depth: int,
        comment_sort: str,
        expand_more: bool,
        parent_post_url: str | None = None,
    ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        if include_comments and max_comments > 0:
            params: dict[str, Any] = {"limit": max_comments, "depth": max_depth + 1, "sort": comment_sort}
        else:
            params = {"limit": 1, "depth": 1}
        data = await self.client.get_json(f"{REDDIT}/comments/{post_id}.json", params)
        if not isinstance(data, list) or not data:
            raise RedditNotFound(f"post {post_id}: unexpected answer from Reddit", url=f"{REDDIT}/comments/{post_id}")
        post_children = (data[0].get("data") or {}).get("children") or []
        post = None
        if post_children and post_children[0].get("kind") == "t3":
            post = parse_post(post_children[0].get("data") or {})
        parent_post_url = parent_post_url or (post["submissionUrl"] if post else None) or f"{REDDIT}/comments/{post_id}"

        comments: list[dict[str, Any]] = []
        if include_comments and max_comments > 0 and len(data) > 1:
            self.stats.threads += 1
            seen: set[str] = set()
            more_ids: list[str] = []
            _walk_comments(
                (data[1].get("data") or {}).get("children") or [],
                comments,
                seen,
                more_ids,
                max_comments=max_comments,
                max_depth=max_depth,
                parent_post_url=parent_post_url,
            )
            if expand_more and more_ids and len(comments) < max_comments:
                await self._expand_more(
                    post_id,
                    more_ids,
                    comments,
                    seen,
                    max_comments=max_comments,
                    max_depth=max_depth,
                    comment_sort=comment_sort,
                    parent_post_url=parent_post_url,
                )
        return post, comments

    async def _expand_more(
        self,
        post_id: str,
        more_ids: list[str],
        out: list[dict[str, Any]],
        seen: set[str],
        *,
        max_comments: int,
        max_depth: int,
        comment_sort: str,
        parent_post_url: str,
    ) -> None:
        """Fetch comments hidden behind ``more`` stubs via /api/morechildren, 100 ids per request."""
        queue = [i for i in more_ids if i not in seen]
        while queue and len(out) < max_comments:
            batch, queue = queue[:MORE_BATCH], queue[MORE_BATCH:]
            data = await self.client.get_json(
                f"{REDDIT}/api/morechildren.json",
                {
                    "api_type": "json",
                    "link_id": f"t3_{post_id}",
                    "children": ",".join(batch),
                    "sort": comment_sort,
                    "limit_children": "false",
                },
            )
            self.stats.more_requests += 1
            things = (((data or {}).get("json") or {}).get("data") or {}).get("things") or []
            new_more: list[str] = []
            for thing in things:
                if len(out) >= max_comments:
                    break
                kind = thing.get("kind")
                item = thing.get("data") or {}
                if kind == "more":
                    if item.get("children") and (item.get("depth") or 0) <= max_depth:
                        new_more.extend(item["children"])
                    continue
                if kind != "t1" or (item.get("depth") or 0) > max_depth:
                    continue
                comment_id = item.get("id")
                if comment_id in seen:
                    continue
                seen.add(comment_id)
                out.append(parse_comment(item, parent_post_url))
            queue.extend(i for i in new_more if i not in seen)

    # -- listings ---------------------------------------------------------------------

    async def _stream_listing(
        self,
        url: str,
        base_params: dict[str, Any],
        *,
        limit: int,
        chronological: bool,
        include_comments: bool = False,
        max_comments_per_post: int = 20,
        max_comments_depth: int = 3,
        comment_sort: str = "confidence",
        expand_more: bool | None = None,
        filters: Filters | None = None,
        known_ids: set[str] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Page through a Reddit listing, dedupe, filter, and optionally attach comments.

        ``chronological`` (the listing is sorted newest first) lets ``filters.since`` and
        ``known_ids`` stop pagination early instead of just skipping items.
        """
        if limit <= 0:
            return
        expand = self.expand_more if expand_more is None else expand_more
        filtering = filters is not None and filters.is_active()
        after: str | None = None
        seen_after: set[str] = set()
        yielded = 0
        seen: set[str] = set()

        while yielded < limit:
            params = dict(base_params)
            params["limit"] = PAGE_SIZE if filtering else min(PAGE_SIZE, limit - yielded)
            if after:
                params["after"] = after
            data = await self.client.get_json(url, params)
            listing = (data.get("data") or {}) if isinstance(data, dict) else {}
            children = listing.get("children") or []
            if not children:
                break

            page: list[tuple[dict[str, Any], dict[str, Any]]] = []
            stop = False
            unknown_on_page = 0
            for child in children:
                item = parse_item(child)
                if item is None:
                    continue
                key = item["submissionIdPrefixed"]
                if key in seen:
                    self.stats.duplicates += 1
                    continue
                seen.add(key)
                # A stickied post sits on top regardless of age, so it must not end a chronological scan.
                in_order = chronological and not item.get("isStickied")
                if known_ids is not None and key in known_ids:
                    self.stats.already_known += 1
                    if in_order:
                        stop = True
                        break
                    continue
                unknown_on_page += 1
                if filtering:
                    assert filters is not None
                    created = item.get("createdUtc")
                    if in_order and filters.since is not None and created is not None and created < filters.since:
                        stop = True
                        break
                    if not filters.matches(item):
                        self.stats.filtered += 1
                        continue
                if item["submissionType"] == "post":
                    self.stats.posts += 1
                else:
                    self.stats.comments += 1
                page.append((item, child.get("data") or {}))
                yield item
                yielded += 1
                if yielded >= limit:
                    break

            if include_comments and page:
                async for comment in self._comments_for(
                    page,
                    max_comments=max_comments_per_post,
                    max_depth=max_comments_depth,
                    comment_sort=comment_sort,
                    expand_more=expand,
                ):
                    yield comment

            if stop or yielded >= limit:
                break
            if known_ids is not None and unknown_on_page == 0:
                break  # a whole page of things we already have
            after = listing.get("after")
            if not after or after in seen_after:
                break  # end of listing, or Reddit handed us the same cursor again
            seen_after.add(after)

    async def _comments_for(
        self,
        page: list[tuple[dict[str, Any], dict[str, Any]]],
        *,
        max_comments: int,
        max_depth: int,
        comment_sort: str,
        expand_more: bool,
    ) -> AsyncGenerator[dict[str, Any], None]:
        tasks: list[asyncio.Task[Any]] = []
        for item, raw in page:
            if item["submissionType"] != "post" or not (raw.get("num_comments") or 0):
                continue
            tasks.append(
                asyncio.create_task(
                    self._fetch_thread(
                        item["submissionId"],
                        include_comments=True,
                        max_comments=max_comments,
                        max_depth=max_depth,
                        comment_sort=comment_sort,
                        expand_more=expand_more,
                        parent_post_url=item["submissionUrl"],
                    ),
                    name=f"thread-{item['submissionId']}",
                )
            )
        if not tasks:
            return
        try:
            for future in asyncio.as_completed(tasks):
                try:
                    _, comments = await future
                except RedditError as exc:
                    self.stats.threads_failed += 1
                    log.warning("comments skipped: %s", exc)
                    continue
                self.stats.comments += len(comments)
                for comment in comments:
                    yield comment
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()

    async def stream_subreddit(
        self,
        subreddit: str,
        sort: SubSort = "new",
        time_frame: TimeFrame = "all",
        limit: int = 100,
        include_comments: bool = False,
        max_comments_per_post: int = 20,
        max_comments_depth: int = 3,
        *,
        comment_sort: CommentSort = "confidence",
        expand_more: bool | None = None,
        filters: Filters | None = None,
        known_ids: set[str] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream posts (and optionally their comments) of ``r/<subreddit>``; ``a+b`` works for several."""
        sub = subreddit.strip().removeprefix("/").removeprefix("r/").strip("/")
        params: dict[str, Any] = {}
        if sort in ("top", "controversial"):
            params["t"] = time_frame
        async for item in self._stream_listing(
            f"{REDDIT}/r/{sub}/{sort}.json",
            params,
            limit=limit,
            chronological=sort == "new",
            include_comments=include_comments,
            max_comments_per_post=max_comments_per_post,
            max_comments_depth=max_comments_depth,
            comment_sort=comment_sort,
            expand_more=expand_more,
            filters=filters,
            known_ids=known_ids,
        ):
            yield item

    async def stream_search(
        self,
        query: str,
        sort: SearchSort = "relevance",
        time_frame: TimeFrame = "all",
        limit: int = 100,
        subreddit: str | None = None,
        include_comments: bool = False,
        max_comments_per_post: int = 20,
        max_comments_depth: int = 3,
        *,
        comment_sort: CommentSort = "confidence",
        expand_more: bool | None = None,
        filters: Filters | None = None,
        known_ids: set[str] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream search results site-wide or inside one subreddit."""
        params: dict[str, Any] = {"q": query, "sort": sort, "t": time_frame}
        if subreddit:
            sub = subreddit.strip().removeprefix("/").removeprefix("r/").strip("/")
            url = f"{REDDIT}/r/{sub}/search.json"
            params["restrict_sr"] = 1
        else:
            url = f"{REDDIT}/search.json"
        async for item in self._stream_listing(
            url,
            params,
            limit=limit,
            chronological=sort == "new",
            include_comments=include_comments,
            max_comments_per_post=max_comments_per_post,
            max_comments_depth=max_comments_depth,
            comment_sort=comment_sort,
            expand_more=expand_more,
            filters=filters,
            known_ids=known_ids,
        ):
            yield item

    async def stream_user(
        self,
        username: str,
        sort: UserSort = "new",
        time_frame: TimeFrame = "all",
        limit: int = 100,
        *,
        kind: UserKind = "submitted",
        include_comments: bool = False,
        max_comments_per_post: int = 20,
        max_comments_depth: int = 3,
        comment_sort: CommentSort = "confidence",
        expand_more: bool | None = None,
        filters: Filters | None = None,
        known_ids: set[str] | None = None,
    ) -> AsyncGenerator[dict[str, Any], None]:
        """Stream a user's posts (``submitted``), comments (``comments``) or both (``overview``)."""
        user = username.strip().removeprefix("/").removeprefix("u/").removeprefix("user/").strip("/")
        params: dict[str, Any] = {"sort": sort}
        if sort in ("top", "controversial"):
            params["t"] = time_frame
        async for item in self._stream_listing(
            f"{REDDIT}/user/{user}/{kind}.json",
            params,
            limit=limit,
            chronological=sort == "new",
            include_comments=include_comments,
            max_comments_per_post=max_comments_per_post,
            max_comments_depth=max_comments_depth,
            comment_sort=comment_sort,
            expand_more=expand_more,
            filters=filters,
            known_ids=known_ids,
        ):
            yield item
