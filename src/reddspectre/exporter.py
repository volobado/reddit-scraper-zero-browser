"""Streaming export: JSON, JSONL, CSV (Excel-friendly), SQLite and a Markdown digest.

Items are written as they arrive, so an interrupted run keeps everything collected so far.
The format is chosen by the file extension.
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .parser import FIELDS

__all__ = ["SQLITE_SUFFIXES", "SUPPORTED", "Exporter", "export_data", "load_known_ids"]

SQLITE_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SUPPORTED = (".json", ".jsonl", ".csv", ".md", *SQLITE_SUFFIXES)
INTEGER_COLUMNS = {
    "score",
    "numComments",
    "numCrossposts",
    "depth",
    "totalAwards",
    "controversiality",
    "videoDurationSec",
    "isSubmitter",
    "scoreHidden",
    "isNSFW",
    "isSpoiler",
    "isStickied",
    "isLocked",
    "isOriginalContent",
    "isEdited",
    "isCrosspost",
    "videoHasAudio",
}
REAL_COLUMNS = {"upvoteRatio", "createdUtc"}
SQLITE_COMMIT_EVERY = 500
MD_BODY_CHARS = 2000
MD_COMMENT_CHARS = 600


def _column_type(name: str) -> str:
    if name in INTEGER_COLUMNS:
        return "INTEGER"
    if name in REAL_COLUMNS:
        return "REAL"
    return "TEXT"


def _cell(value: Any) -> Any:
    """Flatten a value for CSV/SQLite: scalar lists joined with ``|``, anything else as JSON."""
    if value is None or isinstance(value, str | int | float):
        return value
    if isinstance(value, list) and all(v is None or isinstance(v, str | int | float) for v in value):
        return "|".join("" if v is None else str(v) for v in value)
    return json.dumps(value, ensure_ascii=False)


def _trim(text: str, limit: int, *, one_line: bool = False) -> str:
    text = (text or "").strip()
    if one_line:
        text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


class Exporter:
    """Open once, ``write()`` each item, ``close()`` in a ``finally``."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.fmt = self.path.suffix.lower()
        if self.fmt not in SUPPORTED:
            raise ValueError(f"unsupported output format {self.fmt or '(none)'!r}; use one of: {', '.join(SUPPORTED)}")
        self.count = 0
        self.posts = 0
        self.comments = 0
        self._fh: Any = None
        self._csv: csv.DictWriter | None = None
        self._conn: sqlite3.Connection | None = None
        self._sql = ""
        self._pending = 0
        self._md_posts: dict[str, tuple[dict[str, Any] | None, list[dict[str, Any]]]] = {}
        self._opened = False

    # -- lifecycle --------------------------------------------------------------------

    def open(self) -> Exporter:
        if self._opened:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.fmt == ".jsonl":
            self._fh = open(self.path, "w", encoding="utf-8", newline="\n")
        elif self.fmt == ".json":
            self._fh = open(self.path, "w", encoding="utf-8", newline="\n")
            self._fh.write("[\n")
        elif self.fmt == ".csv":
            # utf-8-sig: the BOM makes Excel on Windows read Cyrillic and emoji correctly.
            self._fh = open(self.path, "w", encoding="utf-8-sig", newline="")
            self._csv = csv.DictWriter(self._fh, fieldnames=list(FIELDS), extrasaction="ignore")
            self._csv.writeheader()
        elif self.fmt in SQLITE_SUFFIXES:
            self._open_sqlite()
        # .md is rendered on close(): comments must be grouped under their posts.
        self._opened = True
        return self

    def close(self) -> None:
        if not self._opened:
            return
        try:
            if self.fmt == ".json":
                self._fh.write("\n]\n")
            elif self.fmt == ".md":
                self.path.write_text(self._render_markdown(), encoding="utf-8")
            elif self._conn is not None:
                self._conn.commit()
        finally:
            if self._fh is not None:
                self._fh.close()
                self._fh = None
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            self._opened = False

    def __enter__(self) -> Exporter:
        return self.open()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # -- writing ----------------------------------------------------------------------

    def write(self, item: dict[str, Any]) -> None:
        if not self._opened:
            self.open()
        self.count += 1
        if item.get("submissionType") == "comment":
            self.comments += 1
        else:
            self.posts += 1

        if self.fmt == ".jsonl":
            self._fh.write(json.dumps(item, ensure_ascii=False) + "\n")
        elif self.fmt == ".json":
            text = json.dumps(item, ensure_ascii=False, indent=2)
            text = "\n".join("  " + line for line in text.splitlines())
            self._fh.write(("" if self.count == 1 else ",\n") + text)
        elif self.fmt == ".csv":
            assert self._csv is not None
            self._csv.writerow({k: _cell(item.get(k)) for k in FIELDS})
        elif self.fmt == ".md":
            self._collect_markdown(item)
        else:
            self._write_sqlite(item)

    def write_all(self, items: Iterable[dict[str, Any]]) -> None:
        for item in items:
            self.write(item)

    # -- sqlite -----------------------------------------------------------------------

    def _open_sqlite(self) -> None:
        self._conn = sqlite3.connect(self.path)
        columns = ", ".join(f'"{f}" {_column_type(f)}' for f in FIELDS)
        self._conn.execute(
            f'CREATE TABLE IF NOT EXISTS items ("scrapedAt" TEXT, {columns}, PRIMARY KEY ("submissionIdPrefixed"))'
        )
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_items_sub_time ON items ("subreddit", "createdUtc")')
        self._conn.execute('CREATE INDEX IF NOT EXISTS idx_items_parent ON items ("parentPostId")')
        names = ", ".join(f'"{f}"' for f in FIELDS)
        marks = ", ".join("?" for _ in range(len(FIELDS) + 1))
        self._sql = f'INSERT OR REPLACE INTO items ("scrapedAt", {names}) VALUES ({marks})'

    def _write_sqlite(self, item: dict[str, Any]) -> None:
        assert self._conn is not None
        now = datetime.now(UTC).isoformat(timespec="seconds")
        self._conn.execute(self._sql, (now, *[_cell(item.get(k)) for k in FIELDS]))
        self._pending += 1
        if self._pending >= SQLITE_COMMIT_EVERY:
            self._conn.commit()
            self._pending = 0

    # -- markdown ---------------------------------------------------------------------

    def _collect_markdown(self, item: dict[str, Any]) -> None:
        if item.get("submissionType") == "comment":
            key = (item.get("parentPostId") or "").removeprefix("t3_") or "?"
            self._md_posts.setdefault(key, (None, []))[1].append(item)
        else:
            key = item.get("submissionId") or "?"
            existing = self._md_posts.get(key)
            self._md_posts[key] = (item, existing[1] if existing else [])

    def _render_markdown(self) -> str:
        now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
        lines = [
            f"# Reddit digest: {self.posts} posts, {self.comments} comments",
            "",
            f"Generated by reddspectre on {now}. Post header: `[score↑ comments] title`, "
            "then subreddit | date | author | link. Comments are indented by depth as `- (score) text`.",
            "",
        ]
        orphans: list[dict[str, Any]] = []
        for post, comments in self._md_posts.values():
            if post is None:
                orphans.extend(comments)
                continue
            lines.extend(self._md_post(post, comments))
        if orphans:
            lines.append("## Comments whose post is not in this export")
            lines.append("")
            for comment in orphans:
                lines.append(self._md_comment(comment, indent=0) + f"  [{comment.get('parentPostUrl') or ''}]")
            lines.append("")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _md_post(post: dict[str, Any], comments: list[dict[str, Any]]) -> list[str]:
        date = (post.get("submittedAt") or "")[:10]
        flair = f" · flair: {post['postFlair']}" if post.get("postFlair") else ""
        nsfw = " · NSFW" if post.get("isNSFW") else ""
        title = _trim(post.get("title") or "(no title)", 200, one_line=True)
        out = [
            f"## [{post.get('score', 0)}↑ {post.get('numComments', 0)}c] {title}",
            f"{post.get('subreddit') or ''} | {date} | u/{post.get('author')} | "
            f"{post.get('submissionUrl') or ''}{flair}{nsfw}",
        ]
        external = post.get("externalUrl")
        if external and external != post.get("submissionUrl"):
            out.append(f"Link: {external}")
        body = _trim(post.get("bodyText") or "", MD_BODY_CHARS)
        if body:
            out.extend(["", body])
        if comments:
            out.append("")
            for comment in comments:
                out.append(Exporter._md_comment(comment, indent=int(comment.get("depth") or 0)))
        out.extend(["", "---", ""])
        return out

    @staticmethod
    def _md_comment(comment: dict[str, Any], *, indent: int) -> str:
        op = " [OP]" if comment.get("isSubmitter") else ""
        text = _trim(comment.get("bodyText") or "", MD_COMMENT_CHARS, one_line=True)
        return f"{'  ' * indent}- ({comment.get('score', 0)}){op} {text}"


def load_known_ids(path: str | Path) -> set[str]:
    """Prefixed ids (``t3_…``/``t1_…``) already stored in a SQLite export; empty if the file is new."""
    path = Path(path)
    if not path.exists():
        return set()
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute('SELECT "submissionIdPrefixed" FROM items').fetchall()
    except sqlite3.OperationalError:
        return set()
    finally:
        conn.close()
    return {row[0] for row in rows if row[0]}


def export_data(items: Iterable[dict[str, Any]], filepath: str | Path) -> int:
    """Write all ``items`` to ``filepath`` (format by extension); returns the item count."""
    with Exporter(filepath) as exporter:
        exporter.write_all(items)
        return exporter.count
