"""Talks to Reddit. Deselected by default; run with ``uv run pytest -m live``. Costs ~4 requests."""

import pytest

from reddspectre import RedditForbidden, RedditNotFound, ReddSpectre

pytestmark = pytest.mark.live


async def test_subreddit_post_and_errors():
    async with ReddSpectre() as scraper:
        posts = [p async for p in scraper.stream_subreddit("webscraping", limit=2)]
        assert len(posts) == 2 and all(p["submissionType"] == "post" for p in posts)
        assert "&amp;" not in (posts[0]["bodyText"] + posts[0]["title"])

        items = await scraper.scrape_post("https://redd.it/1jbd7ph", max_comments=5, max_depth=1)
        assert items[0]["submissionType"] == "post" and items[0]["submissionId"] == "1jbd7ph"
        assert 1 <= len(items) - 1 <= 5

        with pytest.raises(RedditNotFound):
            [p async for p in scraper.stream_subreddit("thissubdoesnotexist12345", limit=1)]
        with pytest.raises(RedditForbidden):
            [p async for p in scraper.stream_subreddit("lounge", limit=1)]

        stats = scraper.client.snapshot_stats()
        assert stats.requests == 4 and stats.sessions == 1
