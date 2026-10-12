import json

import pytest

from reddspectre import cli
from reddspectre.client import ClientStats, RedditNotFound
from reddspectre.parser import blank_item
from reddspectre.scraper import ScrapeStats


def test_parser_accepts_every_command():
    parser = cli.build_parser()
    args = parser.parse_args(["sub", "webscraping", "-n", "5", "--comments", "--since", "7d", "-o", "x.jsonl"])
    assert (args.command, args.limit, args.comments, args.since, args.out) == ("sub", 5, True, "7d", "x.jsonl")
    args = parser.parse_args(["search", "proxy", "-s", "webscraping", "--sort", "new", "--keyword", "a|b"])
    assert args.sub == "webscraping" and args.keyword == "a|b"
    args = parser.parse_args(["post", "t3_abc", "--no-comments", "--comment-sort", "top"])
    assert args.no_comments and args.comment_sort == "top" and args.max_comments == 100
    args = parser.parse_args(["user", "spez", "--type", "comments", "--proxy", "http://a", "--proxy", "http://b"])
    assert args.type == "comments" and args.proxy == ["http://a", "http://b"]


def test_build_filters_only_when_something_is_set():
    parser = cli.build_parser()
    assert cli.build_filters(parser.parse_args(["sub", "x"])) is None
    filters = cli.build_filters(parser.parse_args(["sub", "x", "--min-score", "3", "--no-nsfw"]))
    assert filters is not None and filters.min_score == 3 and filters.no_nsfw
    assert cli.build_filters(parser.parse_args(["post", "abc"])) is None


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--version"])
    assert exc.value.code == 0
    assert "reddspectre 0.2." in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["sub", "x", "--only-new", "-o", "x.json"], ["sub", "x", "--only-new"]])
def test_only_new_needs_sqlite(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv + ["-q"])
    assert exc.value.code == 2


def test_bad_since_is_a_usage_error():
    with pytest.raises(SystemExit) as exc:
        cli.main(["sub", "x", "--since", "yesterday", "-q"])
    assert exc.value.code == 2


def fake_post(i):
    item = blank_item()
    item.update(submissionId=f"p{i}", submissionIdPrefixed=f"t3_p{i}", submissionType="post", title="Pøst ✓")
    return item


class FakeScraper:
    instances: list["FakeScraper"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.stats = ScrapeStats()
        self.client = self
        self.passes = 0
        self.known_seen: list[set | None] = []
        FakeScraper.instances.append(self)

    def snapshot_stats(self):
        return ClientStats()

    def reset_stats(self):
        self.passes += 1
        self.stats = ScrapeStats()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def stream_subreddit(self, subreddit, **kwargs):
        self.subreddit, self.stream_kwargs = subreddit, kwargs
        self.known_seen.append(kwargs.get("known_ids"))
        for i in range(2):
            yield fake_post(i)

    async def stream_search(self, *a, **k):
        raise RedditNotFound("r/nope does not exist (HTTP 404)", status=404)
        yield  # pragma: no cover

    async def scrape_post(self, ref, **kwargs):
        if ref == "bad":
            raise RedditNotFound("post bad does not exist (HTTP 404)", status=404)
        return [fake_post(ref)]


@pytest.fixture
def fake_scraper(monkeypatch):
    FakeScraper.instances.clear()
    monkeypatch.setattr(cli, "ReddSpectre", FakeScraper)
    return FakeScraper


def test_stdout_jsonl_when_no_output_file(fake_scraper, capsys):
    code = cli.main(["sub", "r/x", "-n", "2", "--comments", "--depth", "1", "-q", "--delay", "0.5"])
    assert code == 0
    out = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["submissionId"] for line in out] == ["p0", "p1"]
    assert "Pøst ✓" in out[0]
    scraper = fake_scraper.instances[0]
    assert scraper.kwargs["min_delay"] == 0.5 and scraper.kwargs["expand_more"] is True
    assert scraper.stream_kwargs["include_comments"] is True and scraper.stream_kwargs["max_comments_depth"] == 1


def test_output_file_and_summary(fake_scraper, tmp_path, capsys):
    out = tmp_path / "x.jsonl"
    code = cli.main(["sub", "x", "-n", "2", "-o", str(out), "--no-banner"])
    assert code == 0
    assert len(out.read_text(encoding="utf-8").splitlines()) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "Scrape summary (ok)" in captured.err and "posts 2" in captured.err


def test_reddit_error_gives_exit_code_1_and_message(fake_scraper, tmp_path, capsys):
    out = tmp_path / "x.json"
    code = cli.main(["search", "q", "-o", str(out), "-q"])
    assert code == 1
    assert "r/nope does not exist" in capsys.readouterr().err
    assert json.loads(out.read_text(encoding="utf-8")) == [], "file is closed properly even on error"


def test_cancellation_keeps_partial_output(fake_scraper, tmp_path, capsys, monkeypatch):
    """Ctrl+C arrives as CancelledError inside asyncio.run(); what was collected must stay on disk."""
    import asyncio

    async def stream_then_cancel(self, subreddit, **kwargs):
        item = blank_item()
        item.update(submissionId="p0", submissionIdPrefixed="t3_p0", submissionType="post", title="saved")
        yield item
        raise asyncio.CancelledError

    monkeypatch.setattr(FakeScraper, "stream_subreddit", stream_then_cancel)
    out = tmp_path / "partial.json"
    with pytest.raises(asyncio.CancelledError):
        cli.main(["sub", "x", "-o", str(out), "--no-banner"])
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [d["submissionId"] for d in data] == ["p0"]
    assert "Scrape summary (aborted)" in capsys.readouterr().err


def test_post_accepts_several_urls_and_skips_failed_ones(fake_scraper, capsys):
    code = cli.main(["post", "abc", "bad", "def", "-q"])
    assert code == 0
    captured = capsys.readouterr()
    assert [json.loads(line)["submissionId"] for line in captured.out.splitlines()] == ["pabc", "pdef"]
    assert "post bad does not exist" in captured.err


def test_post_single_failed_url_is_an_error(fake_scraper, capsys):
    assert cli.main(["post", "bad", "-q"]) == 1
    code = cli.main(["post", "bad", "bad", "-q"])
    assert code == 1, "every url failed: that is an error even with several urls"


@pytest.mark.parametrize("argv", [["sub", "x", "--watch", "1m", "-o", "x.jsonl"], ["sub", "x", "--watch", "1m"]])
def test_watch_needs_sqlite(argv):
    with pytest.raises(SystemExit) as exc:
        cli.main(argv + ["-q"])
    assert exc.value.code == 2


def test_watch_rejects_tiny_intervals(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["sub", "x", "--watch", "5s", "-o", str(tmp_path / "x.db"), "-q"])
    assert exc.value.code == 2


def test_watch_repeats_passes_with_fresh_known_ids(fake_scraper, tmp_path, capsys, monkeypatch):
    import asyncio

    monkeypatch.setattr(cli, "MIN_WATCH_SECONDS", 0.0)
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(cli.asyncio, "sleep", fake_sleep)
    out = tmp_path / "watch.db"
    log_path = tmp_path / "watch.log"
    with pytest.raises(asyncio.CancelledError):
        cli.main(["sub", "x", "--watch", "90s", "-o", str(out), "--no-banner", "--log", str(log_path)])
    scraper = fake_scraper.instances[0]
    assert scraper.passes == 2 and sleeps == [90.0, 90.0]
    assert scraper.known_seen[0] == set(), "first pass: empty database"
    assert scraper.known_seen[1] == {"t3_p0", "t3_p1"}, "second pass sees what the first stored"
    err = capsys.readouterr().err
    assert "Pass 1 summary (ok)" in err and "Pass 2 summary (ok)" in err and "next pass in 90s" in err
    log_text = log_path.read_text(encoding="utf-8")
    assert "watch mode:" in log_text and "pass 1: 2 posts" in log_text and "pass 2: 2 posts" in log_text


def test_log_file_gets_info_lines(fake_scraper, tmp_path):
    log_path = tmp_path / "run.log"
    cli.main(["post", "abc", "bad", "-q", "--log", str(log_path)])
    text = log_path.read_text(encoding="utf-8")
    assert "ERROR" in text and "post bad does not exist" in text
