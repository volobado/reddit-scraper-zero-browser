"""ReddSpectre: zero-browser Reddit scraper (subreddits, search, users, full comment threads)."""

from ._version import __version__
from .client import (
    RedditBadRequest,
    RedditError,
    RedditForbidden,
    RedditNetworkError,
    RedditNotFound,
    RedditRateLimited,
    RedditServerError,
    RedditSessionError,
    ReddSpectreClient,
)
from .exporter import Exporter, export_data, load_known_ids
from .parser import FIELDS, parse_comment, parse_item, parse_post
from .scraper import Filters, ReddSpectre, ScrapeStats, parse_duration, parse_when

__all__ = [
    "__version__",
    "FIELDS",
    "Exporter",
    "Filters",
    "ReddSpectre",
    "ReddSpectreClient",
    "RedditBadRequest",
    "RedditError",
    "RedditForbidden",
    "RedditNetworkError",
    "RedditNotFound",
    "RedditRateLimited",
    "RedditServerError",
    "RedditSessionError",
    "ScrapeStats",
    "export_data",
    "load_known_ids",
    "parse_comment",
    "parse_duration",
    "parse_item",
    "parse_post",
    "parse_when",
]
