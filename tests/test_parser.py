from reddspectre.parser import FIELDS, parse_comment, parse_item, parse_post


def posts_of(listing):
    return [c["data"] for c in listing["data"]["children"] if c["kind"] == "t3"]


def test_schema_is_complete_and_ordered(pics_listing):
    for raw in posts_of(pics_listing):
        assert list(parse_post(raw)) == list(FIELDS)


def test_pics_listing_media(pics_listing):
    items = [parse_post(r) for r in posts_of(pics_listing)]
    assert {i["postType"] for i in items} <= {"image", "gallery", "video", "link", "text"}
    galleries = [i for i in items if i["postType"] == "gallery"]
    assert galleries, "fixture should contain at least one gallery"
    for gallery in galleries:
        assert gallery["galleryImagesUrls"]
        for url in gallery["galleryImagesUrls"]:
            assert url.startswith("https://i.redd.it/")
            assert url.rsplit(".", 1)[1] in {"jpg", "png", "gif", "webp"}
    images = [i for i in items if i["postType"] == "image"]
    assert images and all(i["imageUrl"] for i in images)
    assert any(i["previewImageUrl"] for i in items), "preview.images source should be exported"
    assert all(i["url"] for i in items)
    assert all(i["submissionUrl"].startswith("https://www.reddit.com/r/") for i in items)


def test_gallery_extension_comes_from_media_metadata():
    raw = {
        "id": "g1",
        "permalink": "/r/x/comments/g1/t/",
        "is_gallery": True,
        "thumbnail": "https://b.thumbs.redditmedia.com/abc.jpg",
        "gallery_data": {"items": [{"media_id": "aaa"}, {"media_id": "bbb"}, {"media_id": "ccc"}, {"media_id": "ddd"}]},
        "media_metadata": {
            "aaa": {"m": "image/png"},
            "bbb": {"m": "image/gif"},
            "ccc": {"s": {"u": "https://preview.redd.it/ccc.webp?width=1&amp;s=x"}},
        },
    }
    item = parse_post(raw)
    assert item["postType"] == "gallery"
    assert item["galleryImagesUrls"] == [
        "https://i.redd.it/aaa.png",
        "https://i.redd.it/bbb.gif",
        "https://preview.redd.it/ccc.webp?width=1&s=x",
        "https://i.redd.it/ddd.jpg",  # no metadata: falls back to the thumbnail's extension
    ]


def test_external_video_links(videos_listing):
    items = [parse_post(r) for r in posts_of(videos_listing)]
    videos = [i for i in items if i["postType"] == "video"]
    assert videos and all(i["videoUrl"] for i in videos)


def test_reddit_hosted_video():
    raw = {
        "id": "v1",
        "permalink": "/r/x/comments/v1/t/",
        "is_video": True,
        "domain": "v.redd.it",
        "url": "https://v.redd.it/abc",
        "media": {
            "reddit_video": {
                "fallback_url": "https://v.redd.it/abc/DASH_720.mp4?source=fallback",
                "hls_url": "https://v.redd.it/abc/HLSPlaylist.m3u8",
                "duration": 12,
                "has_audio": True,
            }
        },
    }
    item = parse_post(raw)
    assert item["postType"] == "video"
    assert item["videoUrl"] == "https://v.redd.it/abc/DASH_720.mp4"
    assert item["videoHlsUrl"].endswith(".m3u8")
    assert item["videoDurationSec"] == 12
    assert item["videoHasAudio"] is True


def test_poll_text_and_link_types():
    poll = parse_post(
        {
            "id": "p",
            "is_self": True,
            "poll_data": {"options": [{"text": "A", "vote_count": 3}, {"text": "B", "vote_count": 1}]},
        }
    )
    assert poll["postType"] == "poll"
    assert poll["pollOptions"] == [{"text": "A", "votes": 3}, {"text": "B", "votes": 1}]

    text = parse_post(
        {
            "id": "t",
            "is_self": True,
            "domain": "self.x",
            "selftext": "hi <b>&",
            "url": "https://www.reddit.com/r/x/comments/t/",
        }
    )
    assert text["postType"] == "text"
    assert text["externalUrl"] is None
    assert text["bodyText"] == "hi <b>&"

    link = parse_post(
        {"id": "l", "is_self": False, "domain": "example.com", "url": "https://example.com/a", "post_hint": "link"}
    )
    assert link["postType"] == "link"
    assert link["externalUrl"] == "https://example.com/a"


def test_url_falls_back_when_url_overridden_by_dest_is_missing():
    item = parse_post({"id": "u", "url": "https://example.com/x", "domain": "example.com"})
    assert item["url"] == "https://example.com/x"
    assert item["externalUrl"] == "https://example.com/x"


def test_crosspost_uses_parent_media():
    raw = {
        "id": "c",
        "crosspost_parent": "t3_orig",
        "url": "/r/orig/comments/orig/t/",
        "crosspost_parent_list": [
            {
                "id": "orig",
                "permalink": "/r/orig/comments/orig/t/",
                "is_gallery": True,
                "gallery_data": {"items": [{"media_id": "m1"}]},
                "media_metadata": {"m1": {"m": "image/jpeg"}},
            }
        ],
    }
    item = parse_post(raw)
    assert item["isCrosspost"] is True
    assert item["crosspostParentUrl"] == "https://www.reddit.com/r/orig/comments/orig/t/"
    assert item["postType"] == "gallery"
    assert item["galleryImagesUrls"] == ["https://i.redd.it/m1.jpg"]


def test_flags_and_edit_timestamp():
    item = parse_post(
        {
            "id": "f",
            "edited": 1700000000,
            "stickied": True,
            "locked": True,
            "spoiler": True,
            "over_18": True,
            "distinguished": "moderator",
            "removed_by_category": "moderator",
            "total_awards_received": 2,
            "author_flair_richtext": [{"e": "text", "t": "hi"}, {"e": "emoji", "u": "https://emoji/x.png"}],
        }
    )
    assert item["isEdited"] is True
    assert item["editedAt"].startswith("2023-11-14")
    assert item["isStickied"] and item["isLocked"] and item["isSpoiler"] and item["isNSFW"]
    assert item["distinguished"] == "moderator"
    assert item["removedByCategory"] == "moderator"
    assert item["totalAwards"] == 2
    assert item["authorFlairImageUrl"] == "https://emoji/x.png"
    untouched = parse_post({"id": "n", "edited": False})
    assert untouched["isEdited"] is False and untouched["editedAt"] is None


def test_thumbnail_rewrite():
    item = parse_post({"id": "th", "thumbnail": "https://external-preview.redd.it/HASH.jpeg?width=140&s=abc"})
    assert item["thumbnailUrl"] == "https://i.redd.it/HASH.jpeg"
    assert parse_post({"id": "th", "thumbnail": "self"})["thumbnailUrl"] is None
    plain = "https://b.thumbs.redditmedia.com/x.jpg"
    assert parse_post({"id": "th", "thumbnail": plain})["thumbnailUrl"] == plain


def test_comments_from_thread_fixture(thread):
    post_id = thread[0]["data"]["children"][0]["data"]["id"]
    first = next(c["data"] for c in thread[1]["data"]["children"] if c["kind"] == "t1")
    item = parse_comment(first)
    assert item["submissionType"] == "comment"
    assert list(item) == list(FIELDS)
    assert item["parentPostUrl"] == f"https://www.reddit.com/r/AskReddit/comments/{post_id}"
    assert item["parentPostId"] == f"t3_{post_id}"
    assert item["depth"] == 0
    assert isinstance(item["isSubmitter"], bool)
    assert item["submissionIdPrefixed"].startswith("t1_")


def test_comment_parent_url_from_link_id_when_no_permalink():
    item = parse_comment({"id": "c", "link_id": "t3_abc", "parent_id": "t3_abc", "body": "x"})
    assert item["parentPostUrl"] == "https://www.reddit.com/comments/abc"
    assert item["submissionIdPrefixed"] == "t1_c"


def test_parse_item_kinds():
    assert parse_item({"kind": "more", "data": {}}) is None
    assert parse_item({"kind": "t3", "data": {"id": "a"}})["submissionType"] == "post"
    assert parse_item({"kind": "t1", "data": {"id": "a"}})["submissionType"] == "comment"
