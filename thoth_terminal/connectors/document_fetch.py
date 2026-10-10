from __future__ import annotations

from typing import Any

from thoth_terminal.net import FetchResult
from thoth_terminal.text import (
    clean_whitespace,
    extract_document_from_html,
    extract_text_from_pdf,
    is_pdf_response,
)


def _pdf_title_from_text(text: str, *, fallback_url: str) -> str | None:
    for line in text.splitlines():
        cleaned = clean_whitespace(line)
        if len(cleaned) >= 12:
            return cleaned[:240]
    tail = fallback_url.rsplit("/", 1)[-1].strip()
    if tail.lower().endswith(".pdf"):
        tail = tail[:-4]
    tail = tail.replace("-", " ").replace("_", " ").strip()
    return tail or None


def build_fetched_document_payload(
    *,
    request_url: str,
    result: FetchResult,
    download_article_images: bool = False,
    max_images: int = 3,
) -> dict[str, Any]:
    final_url = str(result.url or request_url or "").strip() or request_url
    resolved_content_type = (result.content_type or "").strip() or None

    if is_pdf_response(content_type=resolved_content_type, url=final_url, content=result.content):
        normalized_text = extract_text_from_pdf(result.content)
        attachment_content_type = resolved_content_type or "application/pdf"
        return {
            "final_url": final_url,
            "title": _pdf_title_from_text(normalized_text, fallback_url=final_url),
            "normalized_text": normalized_text,
            "raw_html": None,
            "image_urls": [],
            "attachments": [
                {
                    "kind": "pdf",
                    "original_url": final_url,
                    "content_type": attachment_content_type,
                    "filename": final_url.rsplit("/", 1)[-1],
                    "ext": ".pdf",
                    "data_bytes": result.content,
                }
            ],
            "extra": {
                "content_kind": "pdf",
                "is_pdf": True,
                "resolved_url": final_url,
                "resolved_content_type": attachment_content_type,
                "pdf_extracted": bool(normalized_text),
            },
        }

    raw_html = result.text or ""
    normalized_text = ""
    title = None
    image_urls = []
    if raw_html:
        normalized_text, title, extracted_images = extract_document_from_html(
            raw_html,
            url=final_url,
            image_limit=max_images if download_article_images else 0,
        )
        if download_article_images:
            image_urls = extracted_images
    extra: dict[str, Any] = {}
    if final_url and final_url != request_url:
        extra["resolved_url"] = final_url
    if resolved_content_type:
        extra["resolved_content_type"] = resolved_content_type
    return {
        "final_url": final_url,
        "title": title,
        "normalized_text": normalized_text,
        "raw_html": raw_html,
        "image_urls": image_urls,
        "attachments": [],
        "extra": extra,
    }
