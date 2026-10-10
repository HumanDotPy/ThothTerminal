from __future__ import annotations

from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit


_NOTE_IMAGE_CDN_HOSTS = {"assets.st-note.com", "assets.note.com"}
_NOTE_IMAGE_EXTENSIONS = (".avif", ".gif", ".jpeg", ".jpg", ".png", ".webp")
_NOTE_RESIZE_QUERY_KEYS = {"width", "height", "dpr", "frame", "fit", "crop"}
_IMAGE_EXTENSIONS = (".avif", ".bmp", ".gif", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp")
_KNOWN_IMAGE_HOSTS = {
    "pbs.twimg.com",
    "abs.twimg.com",
    "ton.twimg.com",
    "assets.st-note.com",
    "assets.note.com",
}
_KNOWN_NON_IMAGE_HOSTS = {
    "x.com",
    "twitter.com",
    "mobile.twitter.com",
    "medium.com",
}
_NON_IMAGE_EXTENSIONS = (
    ".aspx",
    ".htm",
    ".html",
    ".md",
    ".pdf",
    ".php",
    ".txt",
    ".xml",
)


def canonicalize_x_media_url(url: object) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw
    host = parsed.netloc.lower().split("@")[-1].split(":", 1)[0]
    if host != "pbs.twimg.com" or not parsed.path.startswith("/media/"):
        return raw
    parsed_query = parse_qsl(parsed.query, keep_blank_values=True)
    if not any(key == "name" for key, _ in parsed_query):
        return raw
    query_pairs = [
        (key, value)
        for key, value in parsed_query
        if key != "name"
    ]
    query_pairs.append(("name", "orig"))
    return urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query_pairs, doseq=True), parsed.fragment)
    )


def canonicalize_note_image_url(url: object) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except Exception:
        return raw
    if parsed.scheme not in {"http", "https"}:
        return raw
    if parsed.netloc.lower() not in _NOTE_IMAGE_CDN_HOSTS:
        return raw
    if not parsed.path.lower().endswith(_NOTE_IMAGE_EXTENSIONS):
        return raw
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if not query_pairs:
        return raw
    kept_pairs = [(key, value) for key, value in query_pairs if key.lower() not in _NOTE_RESIZE_QUERY_KEYS]
    if len(kept_pairs) == len(query_pairs):
        return raw
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(kept_pairs, doseq=True), parsed.fragment))


def canonical_media_identity(url: object) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    canonical_x = canonicalize_x_media_url(raw)
    if "pbs.twimg.com/media/" in canonical_x.lower():
        return f"x:{canonical_x.lower()}"
    decoded = unquote(raw)
    marker = "/public/images/"
    marker_index = decoded.lower().find(marker)
    if marker_index >= 0 and "substack-post-media" in decoded[:marker_index].lower():
        asset_path = decoded[marker_index + len(marker) :].split("?", 1)[0].split("#", 1)[0].strip("/")
        if asset_path:
            return f"substack:{asset_path.lower()}"
    return ""


def note_image_fetch_candidates(url: object) -> list[str]:
    raw = str(url or "").strip()
    if not raw:
        return []
    upgraded = canonicalize_note_image_url(raw)
    out: list[str] = []
    seen: set[str] = set()
    for candidate in (upgraded, raw):
        if not candidate or candidate in seen:
            continue
        seen.add(candidate)
        out.append(candidate)
    return out


def looks_like_image_url(url: object) -> bool:
    raw = str(url or "").strip()
    if not raw:
        return False
    try:
        parsed = urlsplit(raw)
    except Exception:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    host = parsed.netloc.lower().split("@")[-1].split(":", 1)[0]
    path = parsed.path.lower()
    if host in _KNOWN_NON_IMAGE_HOSTS or host.endswith((".twitter.com", ".x.com", ".medium.com")):
        return False
    if path.endswith(_NON_IMAGE_EXTENSIONS):
        return False
    if path.endswith(_IMAGE_EXTENSIONS):
        return True
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    image_format = str(query.get("format") or query.get("fm") or "").strip().lower()
    if image_format in {"avif", "gif", "jpeg", "jpg", "png", "webp"}:
        return True
    if host in _KNOWN_IMAGE_HOSTS and ("/media/" in path or path.endswith(_NOTE_IMAGE_EXTENSIONS)):
        return True
    return False


def _normalize_urls(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        url = canonicalize_x_media_url(item)
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(url)
    return out


def _normalize_thread_rows(raw: object) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        out.append(dict(item))
    return out


def build_tweet_media_manifest(payload: dict[str, Any]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    manifest: list[dict[str, Any]] = []

    def _add(
        urls: object,
        *,
        section: str,
        label_prefix: str,
        tweet_url: str | None = None,
        tweet_id: str | None = None,
        relation: str | None = None,
        article_url: str | None = None,
    ) -> None:
        index = 0
        raw_items = urls if isinstance(urls, list) else ([urls] if urls else [])
        for item in raw_items:
            source_row = dict(item) if isinstance(item, dict) else {}
            url = canonicalize_x_media_url(source_row.get("url") if source_row else item)
            if not looks_like_image_url(url):
                continue
            if url in seen:
                continue
            seen.add(url)
            index += 1
            row: dict[str, Any] = {
                "kind": "image",
                "url": url,
                "section": str(source_row.get("section") or section),
                "position": source_row.get("position") or index,
                "label": str(source_row.get("label") or f"{label_prefix} {index}"),
            }
            for key in ("heading", "caption", "alt", "preceding_text", "following_text", "index", "dom_index"):
                value = source_row.get(key)
                if value not in (None, "", [], {}):
                    row[key] = value
            if tweet_url:
                row["tweet_url"] = str(tweet_url)
            if tweet_id:
                row["tweet_id"] = str(tweet_id)
            if relation:
                row["relation"] = relation
            if article_url:
                row["article_url"] = str(article_url)
            manifest.append(row)

    root_url = str(payload.get("url") or payload.get("tweet_url") or "").strip() or None
    root_tweet_id = str(payload.get("tweet_id") or payload.get("id") or "").strip() or None
    _add(
        payload.get("root_media") or payload.get("media"),
        section="post",
        label_prefix="Post media",
        tweet_url=root_url,
        tweet_id=root_tweet_id,
    )

    article = payload.get("article")
    if isinstance(article, dict):
        article_url = str(article.get("url") or payload.get("article_url") or "").strip() or None
        _add(
            article.get("media"),
            section="article",
            label_prefix="Article image",
            tweet_url=root_url,
            tweet_id=root_tweet_id,
            article_url=article_url,
        )
    else:
        _add(
            payload.get("article_media"),
            section="article",
            label_prefix="Article image",
            tweet_url=root_url,
            tweet_id=root_tweet_id,
            article_url=str(payload.get("article_url") or "").strip() or None,
        )

    quoted = payload.get("quoted")
    if isinstance(quoted, dict):
        _add(
            quoted.get("media"),
            section="quoted",
            label_prefix="Quoted media",
            tweet_url=str(quoted.get("url") or "").strip() or None,
            tweet_id=str(quoted.get("tweet_id") or "").strip() or None,
        )

    thread_context = payload.get("thread_context")
    if isinstance(thread_context, dict):
        for key, label in (("ancestors", "Thread context"), ("replies", "Reply media")):
            relation = "ancestor" if key == "ancestors" else "reply"
            for row in _normalize_thread_rows(thread_context.get(key)):
                _add(
                    row.get("media"),
                    section="thread",
                    label_prefix=label,
                    tweet_url=str(row.get("url") or "").strip() or None,
                    tweet_id=str(row.get("tweet_id") or "").strip() or None,
                    relation=relation,
                )
    else:
        for row in _normalize_thread_rows(payload.get("thread")):
            _add(
                row.get("media"),
                section="thread",
                label_prefix="Thread media",
                tweet_url=str(row.get("url") or "").strip() or None,
                tweet_id=str(row.get("tweet_id") or "").strip() or None,
                relation=str(row.get("relation") or "").strip() or None,
            )

    return manifest


def extract_candidate_image_urls(extra: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()

    def _add(raw: object) -> None:
        for url in _normalize_urls(raw):
            if not looks_like_image_url(url):
                continue
            identity = canonical_media_identity(url) or url.lower()
            if identity in seen:
                continue
            seen.add(identity)
            urls.append(url)

    manifest = extra.get("media_manifest")
    if isinstance(manifest, list):
        for row in manifest:
            if not isinstance(row, dict):
                continue
            if str(row.get("kind") or "image").strip().lower() != "image":
                continue
            _add(row.get("url"))

    _add(extra.get("root_media"))
    _add(extra.get("media"))
    _add(extra.get("image_urls"))

    quoted = extra.get("quoted")
    if isinstance(quoted, dict):
        _add(quoted.get("media"))

    article = extra.get("article")
    if isinstance(article, dict):
        _add(article.get("media"))

    thread_context = extra.get("thread_context")
    if isinstance(thread_context, dict):
        for key in ("ancestors", "replies"):
            for row in _normalize_thread_rows(thread_context.get(key)):
                _add(row.get("media"))
    else:
        for row in _normalize_thread_rows(extra.get("thread")):
            _add(row.get("media"))

    return urls


def extract_external_links(extra: dict[str, Any]) -> list[str]:
    links: list[str] = []
    seen: set[str] = set()

    def _add(raw: object) -> None:
        for url in _normalize_urls(raw):
            if not url.startswith("http") or url in seen:
                continue
            seen.add(url)
            links.append(url)

    _add(extra.get("links") or extra.get("urls"))

    article = extra.get("article")
    if isinstance(article, dict):
        _add(article.get("url"))
        _add(article.get("links"))
    else:
        _add(extra.get("article_url"))

    quoted = extra.get("quoted")
    if isinstance(quoted, dict):
        _add(quoted.get("links"))

    thread_context = extra.get("thread_context")
    if isinstance(thread_context, dict):
        for key in ("ancestors", "replies"):
            for row in _normalize_thread_rows(thread_context.get(key)):
                _add(row.get("links"))
                _add(row.get("url"))
    else:
        for row in _normalize_thread_rows(extra.get("thread")):
            _add(row.get("links"))
            _add(row.get("url"))

    return links


def extract_thread_sections(extra: dict[str, Any]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []

    def _normalize_items(rows: object) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for row in _normalize_thread_rows(rows):
            text = str(row.get("text") or "").strip()
            url = str(row.get("url") or "").strip() or None
            if not text and not url:
                continue
            items.append(
                {
                    "tweet_id": str(row.get("tweet_id") or "").strip() or None,
                    "url": url,
                    "text": text or "(no text)",
                    "created_at": str(row.get("created_at") or "").strip() or None,
                    "relation": str(row.get("relation") or "").strip() or None,
                }
            )
        return items

    thread_context = extra.get("thread_context")
    if isinstance(thread_context, dict):
        ancestors = _normalize_items(thread_context.get("ancestors"))
        replies = _normalize_items(thread_context.get("replies"))
        if ancestors:
            sections.append({"key": "ancestors", "label": "Earlier in thread", "items": ancestors})
        if replies:
            sections.append({"key": "replies", "label": "Later in thread", "items": replies})
        return sections

    thread_rows = _normalize_items(extra.get("thread"))
    if thread_rows:
        sections.append({"key": "thread", "label": "Same-author thread", "items": thread_rows})
    return sections


def extract_image_ocr_notes(extra: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    rows = extra.get("image_ocr")
    if not isinstance(rows, list):
        return out
    for row in rows:
        if not isinstance(row, dict):
            continue
        keys = [
            str(row.get("original_url") or "").strip(),
            str(row.get("object_path") or "").strip(),
        ]
        for key in keys:
            if key:
                out[key] = dict(row)
    return out
