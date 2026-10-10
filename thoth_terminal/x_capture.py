from __future__ import annotations

import json
from urllib.parse import quote_plus, urlparse

from bs4 import BeautifulSoup

from thoth_terminal.net import FetchError, fetch
from thoth_terminal.text import clean_whitespace


def _handle_from_author_url(url: str | None) -> str | None:
    if not url:
        return None
    try:
        path = urlparse(url).path.strip("/")
    except Exception:
        return None
    if not path:
        return None
    return path.split("/", 1)[0]


def fetch_oembed_tweet(url: str) -> dict[str, object] | None:
    oembed_url = f"https://publish.twitter.com/oembed?omit_script=1&url={quote_plus(url)}"
    try:
        res = fetch(oembed_url, timeout_s=15.0)
    except FetchError:
        return None
    if not res.text:
        return None
    try:
        data = json.loads(res.text)
    except Exception:
        return None

    html = data.get("html") if isinstance(data, dict) else None
    text = ""
    if isinstance(html, str) and html.strip():
        soup = BeautifulSoup(html, "html.parser")
        block = soup.find("blockquote")
        if block:
            ps = block.find_all("p")
            if ps:
                parts = []
                for p in ps:
                    t = clean_whitespace(p.get_text(" ", strip=True) or "")
                    if t:
                        parts.append(t)
                text = "\n\n".join(parts).strip()
            else:
                text = clean_whitespace(block.get_text(" ", strip=True) or "")

    author_name = None
    author_url = None
    if isinstance(data, dict):
        author_name = data.get("author_name")
        author_url = data.get("author_url")

    handle = _handle_from_author_url(str(author_url)) if author_url else None

    payload: dict[str, object] = {"url": url}
    if text:
        payload["text"] = text
    if author_name:
        payload["author"] = str(author_name)
    if handle:
        payload["handle"] = handle
    return payload or None
