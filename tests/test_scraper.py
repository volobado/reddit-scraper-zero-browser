from datetime import UTC, datetime

import pytest

from reddspectre.client import ClientStats, RedditForbidden, RedditNotFound
from reddspectre.scraper import REDDIT, Filters, ReddSpectre, _walk_comments, parse_duration, parse_when


class FakeClient:
    """Routes URL -> payload (or callable(params) -> payload); records every call."""

    def __init__(self, routes=None, redirects=None):
        self.routes = routes or {}
        self.redirects = redirects or {}
        self.calls: list[tuple[str, dict]] = []
        self.stats = ClientStats()

    async def get_json(self, url, params=None, **_):
        params = dict(params or {})
        self.calls.append((url, params))
        handler = self.routes.get(url)
        if handler is None:
            raise RedditNotFound(f"{url} does not exist", status=404, url=url)
        if isinstance(handler, Exception):
            raise handler
        return handler(params) if callable(handler) else handler

    async def resolve_redirect(self, url):
        self.calls.append((url, {"redirect": True}))
        return self.redirects.get(url)

    async def close(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    def snapshot_stats(self):
        return self.stats


def post(i, **extra):
    data = {
        "id": f"p{i}",
        "name": f"t3_p{i}",
        "permalink": f"/r/x/comments/p{i}/t/",
        "title": f"post {i}",
        "selftext": "",
        "created_utc": 1000 + i,
        "score": i,
        "num_comments": 0,
        "subreddit": "x",
    }
    data.update(extra)
    return data


def listing(posts, after=None):
    return {"kind": "Listing", "data": {"after": after, "children": [{"kind": "t3", "data": p} for p in posts]}}


def comment(cid, depth=0, replies=None, **extra):
    data = {"id": cid, "name": f"t1_{cid}", "body": f"c {cid}", "depth": depth, "link_id": "t3_p1", "score": 1}
    data.update(extra)
    if replies is not None:
        data["replies"] = {"kind": "Listing", "data": {"children": replies}}
    return {"kind": "t1", "data": data}


def more(ids, depth=0):
    return {"kind": "more", "data": {"children": list(ids), "depth": depth, "count": len(ids)}}


def thread_payload(post_data, children):
    return [
        {"kind": "Listing", "data": {"children": [{"kind": "t3", "data": post_data}]}},
        {"kind": "Listing", "data": {"children": children}},
    ]


async def collect(gen):
    return [item async for item in gen]


# ----------------------------------------------------------------------------- ids and time


@pytest.mark.parametrize(
    "ref",
    [
        "1jbd7ph",
        "t3_1jbd7ph",
        "T3_1JBD7PH",
        "https://www.reddit.com/r/webscraping/comments/1jbd7ph/ive_collected/",
        "reddit.com/r/webscraping/comments/1jbd7ph",
        "https://redd.it/1jbd7ph",
        "https://www.reddit.com/gallery/1jbd7ph",
        "https://old.reddit.com/r/x/duplicates/1jbd7ph/t/",
    ],
)
async def test_resolve_post_id_offline(ref):
    scraper = ReddSpectre(client=FakeClient())
    assert await scraper.resolve_post_id(ref) == "1jbd7ph"
    assert not scraper.client.calls, "these forms must not touch the network"


async def test_resolve_post_id_follows_share_link():
    share = "https://www.reddit.com/r/x/s/AbCdEf"
    client = FakeClient(redirects={share: "https://www.reddit.com/r/x/comments/abc123/t/?share_id=1"})
    assert await ReddSpectre(client=client).resolve_post_id(share) == "abc123"


async def test_resolve_post_id_gives_up_cleanly():
    with pytest.raises(RedditNotFound):
        await ReddSpectre(client=FakeClient()).resolve_post_id("https://example.com/nothing-here")


def test_parse_when():
    assert parse_when(None) is None
    assert parse_when("2026-10-12") == datetime(2026, 10, 12, tzinfo=UTC).timestamp()
    assert parse_when("2026-10-12T10:30:00+02:00") == datetime(2026, 10, 12, 8, 30, tzinfo=UTC).timestamp()
    now = datetime.now(UTC).timestamp()
    assert abs(parse_when("7d") - (now - 7 * 86400)) < 5
    assert abs(parse_when("90m") - (now - 5400)) < 5
    assert parse_when(12345) == 12345.0
    with pytest.raises(ValueError):
        parse_when("yesterday")


# ----------------------------------------------------------------------------- comment trees


def test_walk_comments_on_fixture(thread):
    children = thread[1]["data"]["children"]
    out, seen, more_ids = [], set(), []
    _walk_comments(children, out, seen, more_ids, max_comments=1000, max_depth=0, parent_post_url="u")
    assert out and all(c["depth"] == 0 for c in out)
    deep, seen2, more2 = [], set(), []
    _walk_comments(children, deep, seen2, more2, max_comments=1000, max_depth=10, parent_post_url="u")
    assert len(deep) > len(out)
    assert max(c["depth"] for c in deep) >= 1
    assert more2, "fixture has 'more' stubs"
    capped, seen3, _ = [], set(), []
    _walk_comments(children, capped, seen3, [], max_comments=5, max_depth=10, parent_post_url="u")
    assert len(capped) == 5


def test_walk_comments_ignores_deep_more_stubs_and_duplicates():
    children = [
        comment("a", 0, replies=[comment("b", 1, replies=[more(["x"], depth=2)]), more(["y"], depth=1)]),
        comment("a", 0),
        more(["z"], depth=0),
    ]
    out, seen, more_ids = [], set(), []
    _walk_comments(children, out, seen, more_ids, max_comments=100, max_depth=1, parent_post_url="u")
    assert [c["submissionId"] for c in out] == ["a", "b"]
    assert sorted(more_ids) == ["y", "z"]


async def test_scrape_post_expands_more_and_respects_cap():
    thread = thread_payload(post(1, num_comments=5), [comment("a", 0), more(["b", "c", "d"], depth=0)])
    things = [comment("b", 0), comment("c", 1), more(["e"], depth=0)]
    things2 = [comment("e", 0), comment("f", 1)]

    def morechildren(params):
        assert params["link_id"] == "t3_p1" and params["api_type"] == "json"
        batch = params["children"].split(",")
        return {"json": {"data": {"things": things if "b" in batch else things2}}}

    client = FakeClient({f"{REDDIT}/comments/p1.json": thread, f"{REDDIT}/api/morechildren.json": morechildren})
    scraper = ReddSpectre(client=client)
    items = await scraper.scrape_post("p1", max_comments=100, max_depth=5, comment_sort="top")
    assert items[0]["submissionType"] == "post"
    assert [c["submissionId"] for c in items[1:]] == ["a", "b", "c", "e", "f"]
    assert scraper.stats.more_requests == 2
    first_call = client.calls[0]
    assert first_call[1]["limit"] == 100 and first_call[1]["depth"] == 6 and first_call[1]["sort"] == "top"

    client.calls.clear()
    scraper = ReddSpectre(client=client, expand_more=False)
    items = await scraper.scrape_post("p1", max_comments=100)
    assert [c["submissionId"] for c in items[1:]] == ["a"]
    assert len(client.calls) == 1

    scraper = ReddSpectre(client=client)
    items = await scraper.scrape_post("p1", max_comments=2)
    assert len(items) == 3  # post + 2 comments, no second morechildren round


async def test_scrape_post_without_comments():
    client = FakeClient({f"{REDDIT}/comments/p1.json": thread_payload(post(1), [comment("a")])})
    items = await ReddSpectre(client=client).scrape_post("p1", include_comments=False)
    assert len(items) == 1 and client.calls[0][1]["limit"] == 1


# ----------------------------------------------------------------------------- listings


async def test_listing_paginates_and_dedupes():
    pages = {None: listing([post(1), post(2), post(3)], after="t3_p3"), "t3_p3": listing([post(3), post(4), post(5)])}
    client = FakeClient({f"{REDDIT}/r/x/new.json": lambda p: pages[p.get("after")]})
    scraper = ReddSpectre(client=client)
    items = await collect(scraper.stream_subreddit("r/x", limit=10))
    assert [i["submissionId"] for i in items] == ["p1", "p2", "p3", "p4", "p5"]
    assert scraper.stats.duplicates == 1 and scraper.stats.posts == 5
    assert client.calls[0][1]["limit"] == 10 and "t" not in client.calls[0][1]


async def test_listing_limit_and_time_frame():
    client = FakeClient({f"{REDDIT}/r/x/top.json": listing([post(i) for i in range(1, 8)], after="more")})
    items = await collect(ReddSpectre(client=client).stream_subreddit("x", sort="top", time_frame="week", limit=3))
    assert len(items) == 3 and len(client.calls) == 1
    assert client.calls[0][1]["t"] == "week"


async def test_since_stops_chronological_pagination():
    page1 = listing([post(5), post(4), post(3)], after="next")
    client = FakeClient({f"{REDDIT}/r/x/new.json": lambda p: page1 if not p.get("after") else listing([post(2)])})
    scraper = ReddSpectre(client=client)
    items = await collect(scraper.stream_subreddit("x", sort="new", limit=100, filters=Filters(since=1004)))
    assert [i["submissionId"] for i in items] == ["p5", "p4"]
    assert len(client.calls) == 1, "must not request the next page once posts are older than --since"


async def test_filters_keyword_score_nsfw_author():
    posts = [
        post(1, title="Proxy pricing", score=10),
        post(2, title="other", selftext="about proxies", score=1),
        post(3, title="proxy", over_18=True, score=10),
        post(4, title="proxy", author="bob", score=10),
    ]
    client = FakeClient({f"{REDDIT}/r/x/hot.json": listing(posts)})
    scraper = ReddSpectre(client=client)
    filters = Filters(keyword="prox(y|ies)", min_score=5, no_nsfw=True, author="u/BOB")
    items = await collect(scraper.stream_subreddit("x", sort="hot", filters=filters))
    assert [i["submissionId"] for i in items] == ["p4"]
    assert scraper.stats.filtered == 3
    assert client.calls[0][1]["limit"] == 100, "with filters on, pages are fetched at full size"


async def test_only_new_stops_at_first_known_in_chronological_mode():
    client = FakeClient({f"{REDDIT}/r/x/new.json": listing([post(3), post(2), post(1)], after="next")})
    scraper = ReddSpectre(client=client)
    items = await collect(scraper.stream_subreddit("x", known_ids={"t3_p2"}))
    assert [i["submissionId"] for i in items] == ["p3"]
    assert scraper.stats.already_known == 1 and len(client.calls) == 1


async def test_only_new_skips_known_in_hot_and_stops_on_fully_known_page():
    pages = {
        None: listing([post(1), post(2)], after="n"),
        "n": listing([post(3), post(4)], after="m"),
        "m": listing([post(5)]),
    }
    client = FakeClient({f"{REDDIT}/r/x/hot.json": lambda p: pages[p.get("after")]})
    scraper = ReddSpectre(client=client)
    items = await collect(scraper.stream_subreddit("x", sort="hot", known_ids={"t3_p1", "t3_p3", "t3_p4"}))
    assert [i["submissionId"] for i in items] == ["p2"]
    assert len(client.calls) == 2, "page 'n' was entirely known, so pagination stopped there"


async def test_listing_with_comments_streams_and_counts_failures():
    posts = [post(1, num_comments=2), post(2, num_comments=1), post(3, num_comments=0)]
    routes = {
        f"{REDDIT}/r/x/new.json": listing(posts),
        f"{REDDIT}/comments/p1.json": thread_payload(post(1), [comment("a", 0), comment("b", 1)]),
        f"{REDDIT}/comments/p2.json": RedditForbidden("p2 is private", status=403),
    }
    scraper = ReddSpectre(client=FakeClient(routes))
    items = await collect(scraper.stream_subreddit("x", include_comments=True, max_comments_per_post=5))
    kinds = [(i["submissionType"], i["submissionId"]) for i in items]
    assert kinds[:3] == [("post", "p1"), ("post", "p2"), ("post", "p3")]
    assert sorted(k for t, k in kinds[3:]) == ["a", "b"]
    assert items[3]["parentPostUrl"] == "https://www.reddit.com/r/x/comments/p1/t/"
    assert scraper.stats.threads_failed == 1 and scraper.stats.comments == 2
    thread_calls = [c for c in scraper.client.calls if "/comments/" in c[0]]
    assert len(thread_calls) == 2, "posts with zero comments are not fetched"


async def test_search_and_user_urls():
    client = FakeClient(
        {
            f"{REDDIT}/r/x/search.json": listing([post(1)]),
            f"{REDDIT}/search.json": listing([post(2)]),
            f"{REDDIT}/user/spez/comments.json": {"data": {"children": [comment("c1")]}},
        }
    )
    scraper = ReddSpectre(client=client)
    await collect(scraper.stream_search("proxy", subreddit="r/x", sort="new"))
    assert client.calls[-1][1]["restrict_sr"] == 1 and client.calls[-1][1]["q"] == "proxy"
    await collect(scraper.stream_search("proxy"))
    assert "restrict_sr" not in client.calls[-1][1]
    items = await collect(scraper.stream_user("u/spez", kind="comments", sort="top", time_frame="month"))
    assert items[0]["submissionType"] == "comment"
    assert client.calls[-1][1]["t"] == "month"


async def test_unknown_subreddit_raises():
    with pytest.raises(RedditNotFound):
        await collect(ReddSpectre(client=FakeClient()).stream_subreddit("nope"))


def test_parse_duration():
    assert parse_duration("30s") == 30.0
    assert parse_duration("15m") == 900.0
    assert parse_duration("2h") == 7200.0
    assert parse_duration("1.5d") == 129600.0
    assert parse_duration("1w") == 604800.0
    assert parse_duration("45") == 45.0
    assert parse_duration(7) == 7.0
    with pytest.raises(ValueError):
        parse_duration("soon")


async def test_repeated_after_cursor_does_not_loop_forever():
    page = listing([post(1), post(2)], after="same")
    client = FakeClient({f"{REDDIT}/r/x/new.json": page})
    items = await collect(ReddSpectre(client=client).stream_subreddit("x", limit=50))
    assert [i["submissionId"] for i in items] == ["p1", "p2"]
    assert len(client.calls) == 2, "the cursor repeated once, then we stopped"


async def test_stickied_post_does_not_end_chronological_scan():
    old_sticky = post(9, created_utc=1, stickied=True)
    page = listing([old_sticky, post(5), post(4), post(3)], after="n")
    client = FakeClient({f"{REDDIT}/r/x/new.json": lambda p: page if not p.get("after") else listing([])})
    scraper = ReddSpectre(client=client)
    items = await collect(scraper.stream_subreddit("x", filters=Filters(since=1004)))
    assert [i["submissionId"] for i in items] == ["p5", "p4"], "sticky is filtered out, scan still reaches p5/p4"
    known_client = FakeClient({f"{REDDIT}/r/x/new.json": lambda p: page if not p.get("after") else listing([])})
    items = await collect(ReddSpectre(client=known_client).stream_subreddit("x", known_ids={"t3_p9", "t3_p4"}))
    assert [i["submissionId"] for i in items] == ["p5"], "known sticky is skipped, known p4 stops the scan"
