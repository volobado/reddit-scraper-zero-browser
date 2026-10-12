"""Normalise raw Reddit JSON (``t3`` posts and ``t1`` comments) into one flat schema.

Every item, post or comment, is a dict with exactly the keys in :data:`FIELDS`, in that
order. Keys that do not apply (a comment has no ``postType``) are ``None``; list fields are
empty lists. One schema means CSV and SQLite have a fixed column set and consumers never
have to probe for keys.
"""

from __future__ import annotations

import html
from datetime import UTC, datetime
from typing import Any

REDDIT = "https://www.reddit.com"

FIELDS: tuple[str, ...] = (
    # identity
    "submissionId",
    "submissionIdPrefixed",
    "submissionType",
    "submissionUrl",
    "submittedAt",
    "createdUtc",
    # tree position (comments)
    "parentId",
    "parentPostId",
    "parentPostUrl",
    "depth",
    # content
    "postType",
    "title",
    "bodyText",
    "url",
    "externalUrl",
    # author
    "author",
    "authorFullname",
    "authorFlairText",
    "authorFlairImageUrl",
    "isSubmitter",
    "distinguished",
    # where and how popular
    "subreddit",
    "score",
    "upvoteRatio",
    "numComments",
    "numCrossposts",
    "controversiality",
    "scoreHidden",
    "totalAwards",
    # flags
    "postFlair",
    "linkFlairCssClass",
    "isNSFW",
    "isSpoiler",
    "isStickied",
    "isLocked",
    "isOriginalContent",
    "isEdited",
    "editedAt",
    "removedByCategory",
    # crossposts
    "isCrosspost",
    "crosspostParentId",
    "crosspostParentUrl",
    # media
    "thumbnailUrl",
    "previewImageUrl",
    "imageUrl",
    "videoUrl",
    "videoHlsUrl",
    "videoDurationSec",
    "videoHasAudio",
    "galleryImagesUrls",
    "pollOptions",
)

LIST_FIELDS: frozenset[str] = frozenset({"galleryImagesUrls", "pollOptions"})

IMAGE_EXT_BY_MIME = {
    "image/jpg": "jpg",
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp")


def blank_item() -> dict[str, Any]:
    """A dict with every schema key set to its empty value."""
    item: dict[str, Any] = dict.fromkeys(FIELDS)
    for key in LIST_FIELDS:
        item[key] = []
    return item


# ----------------------------------------------------------------------------- helpers


def _absolute(url: str | None) -> str | None:
    if not url:
        return None
    return f"{REDDIT}{url}" if url.startswith("/") else url


def _iso(ts: Any) -> str | None:
    if ts is None or ts is False:
        return None
    try:
        return datetime.fromtimestamp(float(ts), tz=UTC).isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _edited(data: dict[str, Any]) -> tuple[bool, str | None]:
    """Reddit sends ``edited`` as ``false`` or as the edit timestamp."""
    edited = data.get("edited")
    if edited is None or isinstance(edited, bool):
        return bool(edited), None
    return True, _iso(edited)


def _flair_emoji(richtext: Any) -> str | None:
    if not isinstance(richtext, list):
        return None
    for element in richtext:
        if isinstance(element, dict) and element.get("e") == "emoji" and element.get("u"):
            return element["u"]
    return None


def _subreddit(data: dict[str, Any]) -> str | None:
    if data.get("subreddit_name_prefixed"):
        return data["subreddit_name_prefixed"]
    return f"r/{data['subreddit']}" if data.get("subreddit") else None


def _oembed(src: dict[str, Any]) -> dict[str, Any]:
    for key in ("media", "secure_media"):
        oembed = (src.get(key) or {}).get("oembed")
        if isinstance(oembed, dict):
            return oembed
    return {}


def _reddit_video(src: dict[str, Any]) -> dict[str, Any]:
    for key in ("media", "secure_media"):
        video = (src.get(key) or {}).get("reddit_video")
        if isinstance(video, dict):
            return video
    return {}


def detect_post_type(src: dict[str, Any]) -> str | None:
    """poll / gallery / video / image / text / link, judged on the original post for crossposts."""
    domain = (src.get("domain") or "").lower()
    hint = src.get("post_hint") or ""
    url = (src.get("url") or "").lower().split("?")[0]
    if src.get("poll_data"):
        return "poll"
    if src.get("is_gallery"):
        return "gallery"
    if src.get("is_video") or domain == "v.redd.it" or hint == "hosted:video" or _reddit_video(src):
        return "video"
    if hint == "rich:video" or _oembed(src).get("type") == "video":
        return "video"
    if hint == "image" or domain == "i.redd.it" or url.endswith(IMAGE_SUFFIXES):
        return "image"
    if src.get("is_self"):
        return "text"
    if hint == "link" or (domain and not domain.startswith("self.")):
        return "link"
    return "link" if url else None


def _thumbnail(data: dict[str, Any]) -> str | None:
    thumb = data.get("thumbnail") or ""
    if not thumb.startswith("http"):
        return None  # "self", "default", "nsfw", "spoiler", "image"
    if "preview.redd.it" in thumb:
        # Covers external-preview.redd.it too: the same hash is served full-size from
        # i.redd.it (verified with HEAD requests on 2026-10-12).
        oembed_thumb = _oembed(data).get("thumbnail_url")
        if oembed_thumb:
            return html.unescape(oembed_thumb)
        part = thumb.split(".it/")[-1].split("?")[0]
        return f"https://i.redd.it/{part}"
    return thumb


def _preview_image(src: dict[str, Any]) -> str | None:
    images = (src.get("preview") or {}).get("images") or []
    if not images or not isinstance(images[0], dict):
        return None
    url = (images[0].get("source") or {}).get("url")
    return html.unescape(url) if url else None


def _gallery_urls(src: dict[str, Any], thumbnail: str | None) -> list[str]:
    items = (src.get("gallery_data") or {}).get("items") or []
    meta = src.get("media_metadata") or {}
    fallback_ext = "jpg"
    if thumbnail:
        ext = thumbnail.split("?")[0].rsplit(".", 1)[-1].lower()
        if ext in IMAGE_EXT_BY_MIME.values():
            fallback_ext = ext
    urls: list[str] = []
    for entry in items:
        media_id = entry.get("media_id") if isinstance(entry, dict) else None
        if not media_id:
            continue
        info = meta.get(media_id) or {}
        ext = IMAGE_EXT_BY_MIME.get((info.get("m") or "").lower())
        if ext:
            urls.append(f"https://i.redd.it/{media_id}.{ext}")
            continue
        source = info.get("s") or {}
        direct = source.get("u") or source.get("gif") or source.get("mp4")
        urls.append(html.unescape(direct) if direct else f"https://i.redd.it/{media_id}.{fallback_ext}")
    return urls


def _poll_options(src: dict[str, Any]) -> list[dict[str, Any]]:
    options = (src.get("poll_data") or {}).get("options") or []
    return [
        {"text": option.get("text"), "votes": option.get("vote_count")}
        for option in options
        if isinstance(option, dict)
    ]


# ----------------------------------------------------------------------------- public API


def parse_post(data: dict[str, Any]) -> dict[str, Any]:
    """Parse the ``data`` of a ``t3`` listing child into the flat schema."""
    item = blank_item()
    post_id = data.get("id") or ""
    parents = data.get("crosspost_parent_list") or []
    src = parents[0] if parents and isinstance(parents[0], dict) else data
    is_crosspost = bool(data.get("crosspost_parent"))
    post_type = detect_post_type(src)
    url = _absolute(data.get("url") or data.get("url_overridden_by_dest"))
    src_url = _absolute(src.get("url")) or url
    thumbnail = _thumbnail(data) or _thumbnail(src)
    is_edited, edited_at = _edited(data)
    video = _reddit_video(src)

    crosspost_parent_url = None
    if is_crosspost:
        crosspost_parent_url = _absolute(src.get("permalink")) if src is not data else None
        crosspost_parent_url = crosspost_parent_url or _absolute(data.get("url_overridden_by_dest"))

    video_url = None
    if video.get("fallback_url"):
        video_url = video["fallback_url"].split("?")[0]
    elif post_type == "video":
        video_url = src_url

    item.update(
        {
            "submissionId": post_id,
            "submissionIdPrefixed": data.get("name") or f"t3_{post_id}",
            "submissionType": "post",
            "submissionUrl": _absolute(data.get("permalink")),
            "submittedAt": _iso(data.get("created_utc")),
            "createdUtc": data.get("created_utc"),
            "postType": post_type,
            "title": data.get("title") or "",
            "bodyText": data.get("selftext") or "",
            "url": url,
            "externalUrl": None if data.get("is_self") else url,
            "author": data.get("author") or "[deleted]",
            "authorFullname": data.get("author_fullname"),
            "authorFlairText": data.get("author_flair_text"),
            "authorFlairImageUrl": _flair_emoji(data.get("author_flair_richtext")),
            "distinguished": data.get("distinguished"),
            "subreddit": _subreddit(data),
            "score": data.get("score", 0),
            "upvoteRatio": data.get("upvote_ratio"),
            "numComments": data.get("num_comments", 0),
            "numCrossposts": data.get("num_crossposts") or 0,
            "totalAwards": data.get("total_awards_received") or 0,
            "postFlair": data.get("link_flair_text"),
            "linkFlairCssClass": data.get("link_flair_css_class"),
            "isNSFW": bool(data.get("over_18")),
            "isSpoiler": bool(data.get("spoiler")),
            "isStickied": bool(data.get("stickied")),
            "isLocked": bool(data.get("locked")),
            "isOriginalContent": bool(data.get("is_original_content")),
            "isEdited": is_edited,
            "editedAt": edited_at,
            "removedByCategory": data.get("removed_by_category"),
            "isCrosspost": is_crosspost,
            "crosspostParentId": data.get("crosspost_parent"),
            "crosspostParentUrl": crosspost_parent_url,
            "thumbnailUrl": thumbnail,
            "previewImageUrl": _preview_image(src) or _preview_image(data),
            "imageUrl": src_url if post_type == "image" else None,
            "videoUrl": video_url,
            "videoHlsUrl": video.get("hls_url"),
            "videoDurationSec": video.get("duration"),
            "videoHasAudio": video.get("has_audio") if video else None,
            "galleryImagesUrls": _gallery_urls(src, thumbnail) if post_type == "gallery" else [],
            "pollOptions": _poll_options(src) if post_type == "poll" else [],
        }
    )
    return item


def parse_comment(data: dict[str, Any], parent_post_url: str | None = None) -> dict[str, Any]:
    """Parse the ``data`` of a ``t1`` child (thread, morechildren or user listing)."""
    item = blank_item()
    comment_id = data.get("id") or ""
    submission_url = _absolute(data.get("permalink"))
    link_id = data.get("link_id")
    if not parent_post_url:
        if submission_url and "/comments/" in submission_url:
            head, _, rest = submission_url.partition("/comments/")
            parent_post_url = f"{head}/comments/{rest.split('/')[0]}"
        elif link_id:
            parent_post_url = f"{REDDIT}/comments/{link_id.removeprefix('t3_')}"
    is_edited, edited_at = _edited(data)

    item.update(
        {
            "submissionId": comment_id,
            "submissionIdPrefixed": data.get("name") or f"t1_{comment_id}",
            "submissionType": "comment",
            "submissionUrl": submission_url,
            "submittedAt": _iso(data.get("created_utc")),
            "createdUtc": data.get("created_utc"),
            "parentId": data.get("parent_id"),
            "parentPostId": link_id,
            "parentPostUrl": parent_post_url,
            "depth": data.get("depth") or 0,
            "bodyText": data.get("body") or "",
            "author": data.get("author") or "[deleted]",
            "authorFullname": data.get("author_fullname"),
            "authorFlairText": data.get("author_flair_text"),
            "authorFlairImageUrl": _flair_emoji(data.get("author_flair_richtext")),
            "isSubmitter": bool(data.get("is_submitter")),
            "distinguished": data.get("distinguished"),
            "subreddit": _subreddit(data),
            "score": data.get("score", 0),
            "controversiality": data.get("controversiality"),
            "scoreHidden": bool(data.get("score_hidden")),
            "totalAwards": data.get("total_awards_received") or 0,
            "isNSFW": False,
            "isStickied": bool(data.get("stickied")),
            "isLocked": bool(data.get("locked")),
            "isEdited": is_edited,
            "editedAt": edited_at,
            "removedByCategory": data.get("removed_by_category"),
            "isCrosspost": False,
        }
    )
    return item


def parse_item(raw: dict[str, Any], parent_post_url: str | None = None) -> dict[str, Any] | None:
    """Parse a listing child of kind ``t3`` or ``t1``; anything else gives ``None``."""
    kind = raw.get("kind")
    data = raw.get("data") or {}
    if kind == "t3":
        return parse_post(data)
    if kind == "t1":
        return parse_comment(data, parent_post_url=parent_post_url)
    return None
