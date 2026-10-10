from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import io
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.connectors.document_fetch import build_fetched_document_payload
from thoth_terminal.db import utcnow
from thoth_terminal.net import FetchError, fetch_with_fallback
from thoth_terminal.models import Attachment, Document, DocumentEvent
from thoth_terminal.object_store import abs_path, guess_extension, put_bytes
from thoth_terminal.text import clean_whitespace, detect_language, extract_text_from_pdf, sha256_text


PDF_ATTACHMENT_HYDRATION_MIN_CHARS = 250
PDF_ATTACHMENT_HYDRATION_RETRY_HOURS = 6
PDF_PAGE_IMAGE_RENDER_RETRY_HOURS = 12
PDF_PAGE_IMAGE_RENDER_DEFAULT_MAX_PAGES = 6
PDF_PAGE_IMAGE_RENDER_DEFAULT_DPI = 180
PDF_PAGE_IMAGE_RENDER_MAX_BYTES = 80_000_000
PDF_PAGE_IMAGE_MAX_BYTES = 12_000_000
URL_TEXT_HYDRATION_MIN_CHARS = 500
URL_TEXT_HYDRATION_RETRY_HOURS = 6
URL_HYDRATION_CONNECTORS = {
    "cninfo_announcements",
    "edinet_disclosures",
    "hkexnews_title_search",
    "krx_kind_disclosure",
    "opendart_disclosures",
    "tdnet_company_announcements",
}
URL_HYDRATION_KINDS = {"exchange_disclosure", "sec_filing", "securities_filing"}


def _stored_text(document: Document) -> str:
    return str(document.translation_en or document.normalized_text or document.raw_text or "")


def _is_pdf_attachment(row: Attachment) -> bool:
    kind = str(row.kind or "").strip().lower()
    content_type = str(row.content_type or "").split(";", 1)[0].strip().lower()
    object_path = str(row.object_path or "").strip().lower()
    original_url = str(row.original_url or "").split("?", 1)[0].strip().lower()
    return bool(
        kind == "pdf"
        or content_type == "application/pdf"
        or object_path.endswith(".pdf")
        or original_url.endswith(".pdf")
    )


def _document_has_pdf_hint(document: Document) -> bool:
    extra = document.extra if isinstance(document.extra, dict) else {}
    if bool(extra.get("is_pdf") or extra.get("content_kind") == "pdf"):
        return True
    return any(_is_pdf_attachment(row) for row in (document.attachments or []))


def _stored_text_is_readable(document: Document) -> bool:
    text = clean_whitespace(_stored_text(document))
    if len(text) < PDF_ATTACHMENT_HYDRATION_MIN_CHARS:
        return False
    if text.lstrip().startswith("%PDF-"):
        return False
    extra = document.extra if isinstance(document.extra, dict) else {}
    if _document_has_pdf_hint(document) and extra.get("pdf_text_hydration", {}).get("status") == "ok":
        return True
    if _document_has_pdf_hint(document) and not bool(extra.get("pdf_extracted", True)):
        return False
    return True


def _stored_text_needs_url_hydration(document: Document) -> bool:
    text = clean_whitespace(_stored_text(document))
    if text.lstrip().startswith("%PDF-"):
        return True
    return len(text) < URL_TEXT_HYDRATION_MIN_CHARS


def _recent_failed_attempt(extra: dict[str, Any], *, now: datetime) -> bool:
    payload = extra.get("pdf_text_hydration")
    if not isinstance(payload, dict) or payload.get("status") != "error":
        return False
    raw = str(payload.get("attempted_at") or "").strip()
    if not raw:
        return False
    try:
        attempted = datetime.fromisoformat(raw)
    except Exception:
        return False
    if attempted.tzinfo is not None:
        attempted = attempted.replace(tzinfo=None)
    return attempted >= now - timedelta(hours=PDF_ATTACHMENT_HYDRATION_RETRY_HOURS)


def _recent_failed_url_attempt(extra: dict[str, Any], *, now: datetime) -> bool:
    payload = extra.get("url_text_hydration")
    if not isinstance(payload, dict) or payload.get("status") != "error":
        return False
    raw = str(payload.get("attempted_at") or "").strip()
    if not raw:
        return False
    try:
        attempted = datetime.fromisoformat(raw)
    except Exception:
        return False
    if attempted.tzinfo is not None:
        attempted = attempted.replace(tzinfo=None)
    return attempted >= now - timedelta(hours=URL_TEXT_HYDRATION_RETRY_HOURS)


def _recent_failed_pdf_page_render_attempt(extra: dict[str, Any], *, now: datetime) -> bool:
    payload = extra.get("pdf_page_image_render")
    if not isinstance(payload, dict) or payload.get("status") != "error":
        return False
    raw = str(payload.get("attempted_at") or "").strip()
    if not raw:
        return False
    try:
        attempted = datetime.fromisoformat(raw)
    except Exception:
        return False
    if attempted.tzinfo is not None:
        attempted = attempted.replace(tzinfo=None)
    return attempted >= now - timedelta(hours=PDF_PAGE_IMAGE_RENDER_RETRY_HOURS)


def _safe_attachment_path(attachment: Attachment) -> Path | None:
    raw = str(attachment.object_path or "").strip()
    if not raw:
        return None
    path = abs_path(raw).resolve(strict=False)
    root = abs_path("").resolve(strict=False)
    try:
        path.relative_to(root)
    except ValueError:
        return None
    return path


def _first_pdf_attachment(document: Document) -> Attachment | None:
    for row in document.attachments or []:
        if _is_pdf_attachment(row):
            return row
    return None


def _pdf_page_original_url(attachment: Attachment, page_number: int) -> str:
    base = str(attachment.original_url or attachment.object_path or f"attachment:{int(attachment.id)}").strip()
    sep = "&" if "#" in base else "#"
    return f"{base}{sep}page={int(page_number)}"


def _attachment_page_number_for_pdf(attachment: Attachment, *, pdf_attachment: Attachment) -> int | None:
    original_url = str(attachment.original_url or "").strip()
    base = str(pdf_attachment.original_url or pdf_attachment.object_path or f"attachment:{int(pdf_attachment.id)}").strip()
    if not original_url or not base or not original_url.startswith(base):
        return None
    marker = "page="
    if marker not in original_url:
        return None
    raw = original_url.rsplit(marker, 1)[-1].split("&", 1)[0].split("#", 1)[0]
    try:
        value = int(raw)
    except Exception:
        return None
    return value if value >= 1 else None


def _existing_pdf_page_images(session: Session, document: Document, *, pdf_attachment: Attachment) -> list[Attachment]:
    rows: list[Attachment] = []
    attachments = session.query(Attachment).filter(Attachment.document_id == int(document.id)).all()
    for row in attachments:
        ctype = str(row.content_type or "").split(";", 1)[0].strip().lower()
        if not ctype.startswith("image/"):
            continue
        if _attachment_page_number_for_pdf(row, pdf_attachment=pdf_attachment) is not None:
            rows.append(row)
    return rows


@dataclass(frozen=True)
class RenderedPdfPageImage:
    page_number: int
    png_bytes: bytes
    width: int | None = None
    height: int | None = None
    renderer: str = "unknown"


def _render_pdf_pages_with_fitz(
    data: bytes,
    *,
    max_pages: int,
    dpi: int,
) -> list[RenderedPdfPageImage]:
    try:
        import fitz  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError(f"PyMuPDF unavailable: {exc}") from exc
    doc = fitz.open(stream=data, filetype="pdf")
    try:
        scale = max(72, int(dpi)) / 72.0
        matrix = fitz.Matrix(scale, scale)
        pages: list[RenderedPdfPageImage] = []
        for page_index in range(min(len(doc), max_pages)):
            page = doc.load_page(page_index)
            pix = page.get_pixmap(matrix=matrix, alpha=False)
            pages.append(
                RenderedPdfPageImage(
                    page_number=page_index + 1,
                    png_bytes=bytes(pix.tobytes("png")),
                    width=int(pix.width),
                    height=int(pix.height),
                    renderer="pymupdf",
                )
            )
        return pages
    finally:
        doc.close()


def _render_pdf_pages_with_pypdfium2(
    data: bytes,
    *,
    max_pages: int,
    dpi: int,
) -> list[RenderedPdfPageImage]:
    try:
        import pypdfium2 as pdfium  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on optional runtime package
        raise RuntimeError(f"pypdfium2 unavailable: {exc}") from exc
    pdf = pdfium.PdfDocument(data)
    scale = max(72, int(dpi)) / 72.0
    pages: list[RenderedPdfPageImage] = []
    for page_index in range(min(len(pdf), max_pages)):
        page = pdf[page_index]
        bitmap = page.render(scale=scale)
        pil_image = bitmap.to_pil()
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        pages.append(
            RenderedPdfPageImage(
                page_number=page_index + 1,
                png_bytes=buf.getvalue(),
                width=int(pil_image.width),
                height=int(pil_image.height),
                renderer="pypdfium2",
            )
        )
    return pages


def _render_pdf_attachment_pages(
    data: bytes,
    *,
    max_pages: int,
    dpi: int,
) -> list[RenderedPdfPageImage]:
    errors: list[str] = []
    for renderer in (_render_pdf_pages_with_fitz, _render_pdf_pages_with_pypdfium2):
        try:
            return renderer(data, max_pages=max_pages, dpi=dpi)
        except Exception as exc:
            errors.append(f"{renderer.__name__}: {exc}")
    raise RuntimeError("; ".join(errors) or "no PDF page renderer available")


def ensure_pdf_page_image_attachments(
    session: Session,
    document: Document,
    *,
    force: bool = False,
    render_readable_text: bool = False,
    max_pages: int | None = None,
    dpi: int | None = None,
) -> dict[str, Any]:
    """Render stored PDF pages into service-owned image attachments for OCR/VLM."""
    if not _document_has_pdf_hint(document):
        return {"attempted": False, "reason": "not_pdf"}
    if not force and not render_readable_text and _stored_text_is_readable(document):
        return {"attempted": False, "reason": "already_readable_text"}

    extra = dict(document.extra or {})
    now = utcnow()
    if not force and _recent_failed_pdf_page_render_attempt(extra, now=now):
        return {"attempted": False, "reason": "recent_failed_attempt"}

    attachment = _first_pdf_attachment(document)
    if attachment is None:
        render_status = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "error": "PDF document has no stored PDF attachment to render.",
        }
        extra["pdf_page_image_render"] = render_status
        document.extra = extra
        session.flush()
        return {"attempted": True, **render_status}

    existing = _existing_pdf_page_images(session, document, pdf_attachment=attachment)
    if existing and not force:
        return {
            "attempted": False,
            "reason": "already_rendered",
            "pdf_attachment_id": int(attachment.id),
            "existing_attachments": len(existing),
        }

    path = _safe_attachment_path(attachment)
    if path is None or not path.exists():
        render_status = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "attachment_id": int(attachment.id),
            "object_path": attachment.object_path,
            "error": "Stored PDF attachment is missing from the object store.",
        }
        extra["pdf_page_image_render"] = render_status
        document.extra = extra
        session.flush()
        return {"attempted": True, **render_status}

    try:
        file_size = int(path.stat().st_size)
    except Exception:
        file_size = 0
    if file_size > PDF_PAGE_IMAGE_RENDER_MAX_BYTES:
        render_status = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "attachment_id": int(attachment.id),
            "object_path": attachment.object_path,
            "bytes": file_size,
            "error": f"Stored PDF exceeds page-render safety cap ({PDF_PAGE_IMAGE_RENDER_MAX_BYTES} bytes).",
        }
        extra["pdf_page_image_render"] = render_status
        document.extra = extra
        session.flush()
        return {"attempted": True, **render_status}

    render_max_pages = max(1, min(int(max_pages or PDF_PAGE_IMAGE_RENDER_DEFAULT_MAX_PAGES), 20))
    render_dpi = max(90, min(int(dpi or PDF_PAGE_IMAGE_RENDER_DEFAULT_DPI), 260))
    try:
        rendered = _render_pdf_attachment_pages(path.read_bytes(), max_pages=render_max_pages, dpi=render_dpi)
    except Exception as exc:
        render_status = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "attachment_id": int(attachment.id),
            "object_path": attachment.object_path,
            "error": str(exc)[:500],
        }
        extra["pdf_page_image_render"] = render_status
        document.extra = extra
        session.flush()
        return {"attempted": True, **render_status}

    current_attachments = session.query(Attachment).filter(Attachment.document_id == int(document.id)).all()
    existing_sha = {str(row.sha256 or "") for row in current_attachments if row.sha256}
    existing_original_urls = {str(row.original_url or "").strip() for row in current_attachments if row.original_url}
    added: list[Attachment] = []
    skipped_large = 0
    skipped_existing = 0
    for page in rendered:
        if not page.png_bytes:
            continue
        if len(page.png_bytes) > PDF_PAGE_IMAGE_MAX_BYTES:
            skipped_large += 1
            continue
        original_url = _pdf_page_original_url(attachment, page.page_number)
        object_path, sha = put_bytes(page.png_bytes, ext=".png")
        if sha in existing_sha or original_url in existing_original_urls:
            skipped_existing += 1
            continue
        existing_sha.add(sha)
        existing_original_urls.add(original_url)
        row = Attachment(
            document_id=int(document.id),
            kind="image",
            original_url=original_url,
            content_type="image/png",
            object_path=object_path,
            sha256=sha,
            created_at=now,
        )
        session.add(row)
        added.append(row)
    session.flush()

    page_rows = [
        {
            "attachment_id": int(row.id),
            "source_pdf_attachment_id": int(attachment.id),
            "page_number": _attachment_page_number_for_pdf(row, pdf_attachment=attachment),
            "original_url": row.original_url,
            "object_path": row.object_path,
            "sha256": row.sha256,
        }
        for row in added
    ]
    render_status = {
        "status": "ok" if added or rendered else "error",
        "attempted_at": now.isoformat(),
        "attachment_id": int(attachment.id),
        "object_path": attachment.object_path,
        "rendered_pages": len(rendered),
        "stored_attachments": len(added),
        "skipped_existing": skipped_existing,
        "skipped_large": skipped_large,
        "max_pages": render_max_pages,
        "dpi": render_dpi,
        "renderer": rendered[0].renderer if rendered else None,
    }
    if not added and not rendered:
        render_status["error"] = "PDF renderer returned no pages."
    extra["pdf_page_image_render"] = render_status
    existing_page_rows = [row for row in extra.get("pdf_page_images", []) if isinstance(row, dict)]
    extra["pdf_page_images"] = [*existing_page_rows, *page_rows][-100:]
    document.extra = extra
    if added:
        session.add(
            DocumentEvent(
                document_id=int(document.id),
                event_type="PDF_PAGE_IMAGES_RENDERED",
                event_at=now,
                meta={
                    "source": "pdf_page_render",
                    "pdf_attachment_id": int(attachment.id),
                    "stored_attachments": len(added),
                    "rendered_pages": len(rendered),
                    "max_pages": render_max_pages,
                    "dpi": render_dpi,
                    "renderer": render_status.get("renderer"),
                },
            )
        )
    session.flush()
    return {"attempted": True, **render_status}


def _is_url_hydration_candidate(document: Document) -> bool:
    extra = document.extra if isinstance(document.extra, dict) else {}
    connector = str(extra.get("connector") or "").strip().lower()
    kind = str(document.kind or "").strip().lower()
    if kind not in URL_HYDRATION_KINDS and connector not in URL_HYDRATION_CONNECTORS:
        return False
    url = str(document.url or document.canonical_url or "").strip()
    return url.startswith(("http://", "https://"))


def _store_payload_attachments(
    session: Session,
    document: Document,
    *,
    attachments: list[dict[str, Any]],
    now: datetime,
) -> int:
    if not attachments:
        return 0
    existing_sha = {str(row.sha256 or "") for row in (document.attachments or []) if row.sha256}
    existing_key = {
        (str(row.original_url or "").strip(), str(row.object_path or "").strip())
        for row in (document.attachments or [])
    }
    stored = 0
    for row in attachments:
        if not isinstance(row, dict):
            continue
        data = row.get("data_bytes")
        if not isinstance(data, (bytes, bytearray)):
            continue
        payload = bytes(data)
        if not payload:
            continue
        content_type = str(row.get("content_type") or "").strip() or None
        original_url = str(row.get("original_url") or "").strip() or None
        ext = str(row.get("ext") or "").strip() or guess_extension(content_type=content_type, url=original_url) or ""
        object_path, sha = put_bytes(payload, ext=ext)
        if sha in existing_sha or ((original_url or ""), object_path) in existing_key:
            continue
        existing_sha.add(sha)
        existing_key.add((original_url or "", object_path))
        session.add(
            Attachment(
                document_id=int(document.id),
                kind=str(row.get("kind") or "attachment").strip()[:32] or "attachment",
                original_url=original_url,
                content_type=content_type,
                object_path=object_path,
                sha256=sha,
                created_at=now,
            )
        )
        stored += 1
    return stored


def _apply_hydrated_text(document: Document, *, text: str, old_text: str) -> None:
    document.raw_text = text
    document.normalized_text = text
    previous_translation = str(document.translation_en or "")
    if not previous_translation or previous_translation.strip() == old_text.strip() or len(previous_translation.strip()) < URL_TEXT_HYDRATION_MIN_CHARS:
        document.translation_en = None
        document.translation_backend = None
        document.translation_model = None
        document.translation_glossary_version = None
        document.translated_at = None
    guess = detect_language(text, min_chars=30)
    document.language = guess.language or document.language
    document.language_confidence = guess.confidence if guess.confidence is not None else document.language_confidence
    document.content_hash = sha256_text(text)


def maybe_hydrate_pdf_attachment_text(
    session: Session,
    document: Document,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """Extract readable text from a stored PDF attachment for legacy PDF captures."""
    if not _document_has_pdf_hint(document):
        return {"attempted": False, "reason": "not_pdf"}
    if not force and _stored_text_is_readable(document):
        return {"attempted": False, "reason": "already_readable"}

    extra = dict(document.extra or {})
    now = utcnow()
    if not force and _recent_failed_attempt(extra, now=now):
        return {"attempted": False, "reason": "recent_failed_attempt"}

    attachment = _first_pdf_attachment(document)
    if attachment is None:
        hydration = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "error": "PDF document has no stored PDF attachment to extract.",
        }
        extra["pdf_text_hydration"] = hydration
        document.extra = extra
        session.flush()
        return {"attempted": True, **hydration}

    path = _safe_attachment_path(attachment)
    if path is None or not path.exists():
        hydration = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "attachment_id": int(attachment.id),
            "object_path": attachment.object_path,
            "error": "Stored PDF attachment is missing from the object store.",
        }
        extra["pdf_text_hydration"] = hydration
        document.extra = extra
        session.flush()
        return {"attempted": True, **hydration}

    try:
        text = clean_whitespace(extract_text_from_pdf(path.read_bytes()))
    except Exception as exc:
        text = ""
        extract_error = str(exc)[:500]
    else:
        extract_error = ""

    if len(text) < PDF_ATTACHMENT_HYDRATION_MIN_CHARS:
        page_render = ensure_pdf_page_image_attachments(session, document, force=force)
        extra = dict(document.extra or {})
        hydration = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "attachment_id": int(attachment.id),
            "object_path": attachment.object_path,
            "text_chars": len(text),
            "page_image_render": page_render,
            "error": extract_error or "PDF text extraction returned too little readable text.",
        }
        extra["pdf_text_hydration"] = hydration
        document.extra = extra
        session.flush()
        return {"attempted": True, **hydration}

    old_text = _stored_text(document)
    previous_translation = str(document.translation_en or "")
    document.raw_text = text
    document.normalized_text = text
    if not previous_translation or previous_translation.strip() == old_text.strip() or len(previous_translation.strip()) < PDF_ATTACHMENT_HYDRATION_MIN_CHARS:
        document.translation_en = None
        document.translation_backend = None
        document.translation_model = None
        document.translation_glossary_version = None
        document.translated_at = None
    guess = detect_language(text, min_chars=30)
    document.language = guess.language or document.language
    document.language_confidence = guess.confidence if guess.confidence is not None else document.language_confidence
    document.content_hash = sha256_text(text)

    hydration = {
        "status": "ok",
        "attempted_at": now.isoformat(),
        "attachment_id": int(attachment.id),
        "object_path": attachment.object_path,
        "text_chars": len(text),
    }
    extra["pdf_extracted"] = True
    extra["pdf_text_hydration"] = hydration
    document.extra = extra
    session.add(
        DocumentEvent(
            document_id=int(document.id),
            event_type="TEXT_HYDRATED",
            event_at=now,
            meta={
                "source": "pdf_attachment",
                "attachment_id": int(attachment.id),
                "object_path": attachment.object_path,
                "text_chars": len(text),
            },
        )
    )
    session.flush()
    return {"attempted": True, **hydration}


def maybe_hydrate_url_document_text(
    session: Session,
    document: Document,
    *,
    force: bool = False,
    timeout_s: float = 35.0,
) -> dict[str, Any]:
    """Fetch a filing/disclosure URL and extract text when only list-row metadata was stored."""
    if not _is_url_hydration_candidate(document):
        return {"attempted": False, "reason": "not_official_filing_url"}
    if not force and not _stored_text_needs_url_hydration(document):
        return {"attempted": False, "reason": "already_readable"}

    extra = dict(document.extra or {})
    now = utcnow()
    if not force and _recent_failed_url_attempt(extra, now=now):
        return {"attempted": False, "reason": "recent_failed_attempt"}

    request_url = str(document.url or document.canonical_url or "").strip()
    try:
        result = fetch_with_fallback(request_url, timeout_s=timeout_s)
        payload = build_fetched_document_payload(request_url=request_url, result=result)
    except (FetchError, Exception) as exc:
        hydration = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "url": request_url,
            "error": str(exc)[:500],
        }
        extra["url_text_hydration"] = hydration
        document.extra = extra
        session.flush()
        return {"attempted": True, **hydration}

    text = clean_whitespace(str(payload.get("normalized_text") or ""))
    attachments = [row for row in list(payload.get("attachments") or []) if isinstance(row, dict)]
    stored_attachments = _store_payload_attachments(session, document, attachments=attachments, now=now)
    if len(text) < URL_TEXT_HYDRATION_MIN_CHARS:
        hydration = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "url": request_url,
            "final_url": payload.get("final_url"),
            "text_chars": len(text),
            "stored_attachments": stored_attachments,
            "error": "Fetched filing URL did not yield enough readable text.",
        }
        extra["url_text_hydration"] = hydration
        document.extra = extra
        session.flush()
        return {"attempted": True, **hydration}

    old_text = _stored_text(document)
    _apply_hydrated_text(document, text=text, old_text=old_text)
    payload_extra = payload.get("extra") if isinstance(payload.get("extra"), dict) else {}
    extra.update({key: value for key, value in payload_extra.items() if key not in {"connector"}})
    hydration = {
        "status": "ok",
        "attempted_at": now.isoformat(),
        "url": request_url,
        "final_url": payload.get("final_url"),
        "content_type": payload_extra.get("resolved_content_type"),
        "text_chars": len(text),
        "stored_attachments": stored_attachments,
    }
    extra["url_text_hydration"] = hydration
    document.extra = extra
    session.add(
        DocumentEvent(
            document_id=int(document.id),
            event_type="TEXT_HYDRATED",
            event_at=now,
            meta={
                "source": "official_url",
                "url": request_url,
                "final_url": payload.get("final_url"),
                "text_chars": len(text),
                "stored_attachments": stored_attachments,
            },
        )
    )
    session.flush()
    return {"attempted": True, **hydration}
