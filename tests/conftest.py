import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def pics_listing():
    """r/pics hot, fetched 2026-10-12 with raw_json=1: images and galleries."""
    return load("pics_listing.json")


@pytest.fixture(scope="session")
def videos_listing():
    """r/videos new, fetched 2026-10-12 with raw_json=1: external video links."""
    return load("videos_listing.json")


@pytest.fixture(scope="session")
def thread():
    """/comments/1x355ez.json?limit=20&depth=3&sort=top: post + nested comments + 'more' stubs."""
    return load("thread.json")
