import csv
import json
import sqlite3

import pytest

from reddspectre.exporter import Exporter, export_data, load_known_ids
from reddspectre.parser import FIELDS, blank_item


def sample_post(i=1):
    item = blank_item()
    item.update(
        submissionId=f"p{i}",
        submissionIdPrefixed=f"t3_p{i}",
        submissionType="post",
        submissionUrl=f"https://www.reddit.com/r/x/comments/p{i}/t/",
        submittedAt="2026-10-12T00:00:00+00:00",
        createdUtc=1.0,
        title=f"Pøst {i} & <b> ✓",
        bodyText="bødy\nsecond line",
        author="u1",
        subreddit="r/x",
        score=5,
        numComments=1,
        galleryImagesUrls=["a", "b"],
        pollOptions=[{"text": "A", "votes": 1}],
        isNSFW=False,
    )
    return item


def sample_comment():
    item = blank_item()
    item.update(
        submissionId="c1",
        submissionIdPrefixed="t1_c1",
        submissionType="comment",
        parentPostId="t3_p1",
        parentPostUrl="https://www.reddit.com/r/x/comments/p1",
        depth=1,
        bodyText="cømment\nwith a line break",
        author="u2",
        score=2,
        isSubmitter=True,
    )
    return item


def test_csv_has_bom_fixed_header_and_flattened_lists(tmp_path):
    path = tmp_path / "out.csv"
    with Exporter(path) as ex:
        ex.write(sample_post())
        ex.write(sample_comment())
    raw = path.read_bytes()
    assert raw[:3] == b"\xef\xbb\xbf", "Excel needs the BOM to read UTF-8"
    with open(path, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert list(rows[0]) == list(FIELDS)
    assert rows[0]["galleryImagesUrls"] == "a|b"
    assert json.loads(rows[0]["pollOptions"]) == [{"text": "A", "votes": 1}]
    assert rows[0]["title"] == "Pøst 1 & <b> ✓"
    assert rows[1]["submissionType"] == "comment" and rows[1]["postType"] == ""


def test_jsonl_round_trip(tmp_path):
    path = tmp_path / "out.jsonl"
    export_data([sample_post(), sample_comment()], path)
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0]) == sample_post()
    assert "Pøst" in lines[0] and "✓" in lines[0], "ensure_ascii must be off"


def test_json_array_is_valid_after_streaming(tmp_path):
    path = tmp_path / "out.json"
    ex = Exporter(path).open()
    ex.write(sample_post(1))
    ex.write(sample_post(2))
    ex.close()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert [d["submissionId"] for d in data] == ["p1", "p2"]
    empty = tmp_path / "empty.json"
    Exporter(empty).open().close()
    assert json.loads(empty.read_text(encoding="utf-8")) == []


def test_sqlite_upsert_and_known_ids(tmp_path):
    path = tmp_path / "out.db"
    assert load_known_ids(path) == set()
    with Exporter(path) as ex:
        ex.write(sample_post())
        ex.write(sample_comment())
    with Exporter(path) as ex:
        ex.write(sample_post())  # same id again: replaced, not duplicated
    assert load_known_ids(path) == {"t3_p1", "t1_c1"}
    conn = sqlite3.connect(path)
    rows = conn.execute(
        'SELECT "submissionId", "score", "galleryImagesUrls", "scrapedAt" FROM items ORDER BY 1'
    ).fetchall()
    conn.close()
    assert len(rows) == 2
    post_row = next(r for r in rows if r[0] == "p1")
    assert post_row[1] == 5 and post_row[2] == "a|b" and post_row[3]


def test_markdown_digest_groups_comments_under_posts(tmp_path):
    path = tmp_path / "digest.md"
    export_data([sample_post(), sample_comment()], path)
    text = path.read_text(encoding="utf-8")
    assert "## [5↑ 1c] Pøst 1 & <b> ✓" in text
    assert "r/x | 2026-10-12 | u/u1 | https://www.reddit.com/r/x/comments/p1/t/" in text
    assert "bødy\nsecond line" in text
    assert "  - (2) [OP] cømment with a line break" in text
    assert text.index("Pøst 1") < text.index("[OP] cømment")


def test_markdown_orphan_comments_listed_separately(tmp_path):
    path = tmp_path / "digest.md"
    export_data([sample_comment()], path)
    text = path.read_text(encoding="utf-8")
    assert "Comments whose post is not in this export" in text
    assert "cømment with a line break" in text


def test_unknown_extension_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="unsupported output format"):
        Exporter(tmp_path / "out.txt")


def test_export_data_returns_count_and_creates_parent_dirs(tmp_path):
    path = tmp_path / "deep" / "er" / "out.jsonl"
    assert export_data([sample_post(), sample_post(2)], path) == 2
    assert path.exists()
