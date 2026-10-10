from __future__ import annotations

import io
import hashlib
import re
from urllib.parse import urlparse
from dataclasses import dataclass
from datetime import datetime, timezone

from bs4 import BeautifulSoup
from bs4 import XMLParsedAsHTMLWarning
from langdetect import DetectorFactory, detect_langs
import warnings


DetectorFactory.seed = 0
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)

_WS_RE = re.compile(r"[ \t]+")
_NL_RE = re.compile(r"\n{3,}")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()


def clean_whitespace(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "\n".join(_WS_RE.sub(" ", line).strip() for line in text.split("\n"))
    text = _NL_RE.sub("\n\n", text)
    return text.strip()


def looks_like_pdf_bytes(content: bytes | None) -> bool:
    if not content:
        return False
    return bytes(content[:5]).startswith(b"%PDF-")


def is_pdf_response(
    *,
    content_type: str | None = None,
    url: str | None = None,
    content: bytes | None = None,
) -> bool:
    ctype = (content_type or "").split(";", 1)[0].strip().lower()
    if ctype == "application/pdf":
        return True
    if url:
        path = (urlparse(url).path or "").strip().lower()
        if path.endswith(".pdf"):
            return True
    return looks_like_pdf_bytes(content)


def extract_pdf_pages(data: bytes) -> list[str]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return []
    try:
        reader = PdfReader(io.BytesIO(data))
        parts: list[str] = []
        for page in reader.pages:
            txt = page.extract_text() or ""
            parts.append(clean_whitespace(txt))
        return parts
    except Exception:
        return []


def extract_pdf_outline(data: bytes) -> list[dict[str, object]]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception:
        return []
    try:
        reader = PdfReader(io.BytesIO(data))
        outline = reader.outline
    except Exception:
        return []
    rows: list[dict[str, object]] = []

    def visit(items: object, depth: int = 0) -> None:
        if not isinstance(items, list):
            return
        for item in items:
            if isinstance(item, list):
                visit(item, depth + 1)
                continue
            title = str(getattr(item, "title", "") or "").strip()
            if not title:
                continue
            try:
                page_number = int(reader.get_destination_page_number(item)) + 1
            except Exception:
                page_number = None
            rows.append({"title": title[:512], "page_number": page_number, "depth": max(0, depth)})

    visit(outline)
    return rows[:1000]


def extract_text_from_pdf(data: bytes) -> str:
    return clean_whitespace("\n\n".join(page for page in extract_pdf_pages(data) if page))


@dataclass(frozen=True)
class LanguageGuess:
    language: str | None
    confidence: float | None


def detect_language(text: str, *, min_chars: int = 30) -> LanguageGuess:
    sample = clean_whitespace(text)
    if len(sample) < int(min_chars or 0):
        return LanguageGuess(language=None, confidence=None)
    try:
        langs = detect_langs(sample[:5000])
        if not langs:
            return LanguageGuess(language=None, confidence=None)
        best = langs[0]
        return LanguageGuess(language=str(best.lang), confidence=float(best.prob))
    except Exception:
        return LanguageGuess(language=None, confidence=None)


def strip_boilerplate(soup: BeautifulSoup) -> None:
    """Remove page chrome while preserving article lists and filter forms."""
    for tag in soup(["script", "style", "noscript", "svg", "iframe"]):
        tag.decompose()

    # Footer/nav/aside are almost always boilerplate. Headers can contain the article title,
    # so only remove headers that are not inside a main/article container.
    for tag in soup(["footer", "nav", "aside"]):
        tag.decompose()

    for tag in soup.find_all("header"):
        if tag.find_parent(["article", "main"]) is not None:
            continue
        tag.decompose()

    # ARIA roles that are rarely part of the article body.
    for tag in soup.find_all(attrs={"role": True}):
        # Some BeautifulSoup nodes may have attrs=None on malformed pages.
        if not getattr(tag, "attrs", None):
            continue
        role = tag.get("role")
        if not role:
            continue
        r = str(role).strip().lower()
        if r in {"navigation", "banner", "contentinfo", "search"}:
            tag.decompose()

    # Remove hidden sections.
    for tag in soup.find_all(attrs={"aria-hidden": True}):
        if not getattr(tag, "attrs", None):
            continue
        if str(tag.get("aria-hidden")).strip().lower() in {"true", "1"}:
            tag.decompose()


def extract_text_from_html(html: str, *, url: str | None = None) -> str:
    """Extract article text with the required native HTML extension."""
    from thoth_terminal.html_native import extract_native

    return extract_native(html, url=url)


def extract_document_from_html(
    html: str, *, url: str | None = None, image_limit: int = 3
) -> tuple[str, str | None, list[str]]:
    """Extract text, title, and images with one native DOM parse."""
    from thoth_terminal.html_native import extract_document_native

    return extract_document_native(html, url=url, image_limit=image_limit)


def extract_title_from_html(html: str, *, url: str | None = None) -> str | None:
    from thoth_terminal.html_native import extract_title_native

    return extract_title_native(html, url=url)


def extract_image_urls_from_html(html: str, *, url: str | None = None, limit: int = 3) -> list[str]:
    from thoth_terminal.html_native import extract_images_native

    return extract_images_native(html, url=url, limit=limit)


def parse_datetime_iso(value: str) -> datetime | None:
    value = value.strip()
    if not value:
        return None
    # Accept RFC3339-ish and common HTML time formats.
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(value, fmt)
            # Normalize to naive UTC where possible.
            if dt.tzinfo is not None:
                dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt
        except Exception:
            continue
    return None
