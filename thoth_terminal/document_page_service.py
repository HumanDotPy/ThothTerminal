from __future__ import annotations

import hashlib
import base64
import io
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.models import Attachment, Document, DocumentPage
from thoth_terminal.object_store import abs_path, put_bytes
from thoth_terminal.text import extract_pdf_outline, extract_pdf_pages


PDF_PAGE_TEXT_MAX_BYTES = 100_000_000


def _is_pdf_attachment(row: Attachment) -> bool:
    kind = str(row.kind or "").strip().lower()
    content_type = str(row.content_type or "").split(";", 1)[0].strip().lower()
    return kind == "pdf" or content_type == "application/pdf" or str(row.object_path or "").lower().endswith(".pdf")


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


def _primary_pdf_attachment(document: Document) -> Attachment | None:
    rows = [row for row in document.attachments or [] if _is_pdf_attachment(row)]
    if not rows:
        return None
    preferred_urls = {str(document.url or "").strip(), str(document.canonical_url or "").strip()}
    for row in rows:
        if str(row.original_url or "").strip() in preferred_urls:
            return row
    return sorted(rows, key=lambda row: int(row.id or 0))[0]


def index_pdf_pages_from_bytes(
    session: Session,
    *,
    document_id: int,
    pdf_bytes: bytes,
    source_sha256: str,
    source_attachment_id: int | None = None,
) -> dict[str, Any]:
    if not pdf_bytes or not bytes(pdf_bytes[:5]).startswith(b"%PDF-"):
        return {"document_id": int(document_id), "page_count": 0, "created": False, "reason": "not_pdf"}
    sha = str(source_sha256 or "").strip().lower() or hashlib.sha256(pdf_bytes).hexdigest()
    existing = (
        session.query(DocumentPage)
        .filter_by(document_id=int(document_id), source_sha256=sha)
        .order_by(DocumentPage.page_number.asc())
        .all()
    )
    if existing:
        return {"document_id": int(document_id), "source_sha256": sha, "page_count": len(existing), "created": False}
    pages = extract_pdf_pages(pdf_bytes)
    if not pages:
        return {
            "document_id": int(document_id),
            "source_sha256": sha,
            "page_count": 0,
            "created": False,
            "reason": "page_extraction_empty",
        }
    session.query(DocumentPage).filter(DocumentPage.document_id == int(document_id)).delete()
    now = utcnow()
    for index, text in enumerate(pages, start=1):
        text_v = str(text or "")
        session.add(
            DocumentPage(
                document_id=int(document_id),
                source_attachment_id=int(source_attachment_id) if source_attachment_id is not None else None,
                source_sha256=sha,
                page_number=index,
                text=text_v,
                text_hash=hashlib.sha256(text_v.encode("utf-8", errors="replace")).hexdigest(),
                token_estimate=max(0, (len(text_v) + 3) // 4),
                extra={"index_version": "document-pages-v1", "extractor": "pypdf"},
                created_at=now,
            )
        )
    document = session.query(Document).filter_by(id=int(document_id)).first()
    if document is not None:
        extra = dict(document.extra or {})
        extra["pdf_structure"] = {
            "version": "document-pages-v1",
            "source_sha256": sha,
            "page_count": len(pages),
            "outline": extract_pdf_outline(pdf_bytes),
            "indexed_at": now.isoformat(),
        }
        document.extra = extra
    session.flush()
    return {"document_id": int(document_id), "source_sha256": sha, "page_count": len(pages), "created": True}


def ensure_document_pages(session: Session, *, document_id: int) -> dict[str, Any]:
    document = session.query(Document).filter_by(id=int(document_id)).first()
    if document is None:
        raise ValueError("Document not found.")
    existing = (
        session.query(DocumentPage)
        .filter_by(document_id=int(document_id))
        .order_by(DocumentPage.page_number.asc())
        .all()
    )
    if existing:
        return {
            "document_id": int(document_id),
            "source_sha256": existing[0].source_sha256,
            "page_count": len(existing),
            "created": False,
        }
    attachment = _primary_pdf_attachment(document)
    if attachment is None:
        return {"document_id": int(document_id), "page_count": 0, "created": False, "reason": "no_pdf_attachment"}
    path = _safe_attachment_path(attachment)
    if path is None or not path.exists():
        return {"document_id": int(document_id), "page_count": 0, "created": False, "reason": "attachment_missing"}
    if path.stat().st_size > PDF_PAGE_TEXT_MAX_BYTES:
        return {"document_id": int(document_id), "page_count": 0, "created": False, "reason": "attachment_too_large"}
    return index_pdf_pages_from_bytes(
        session,
        document_id=int(document_id),
        pdf_bytes=path.read_bytes(),
        source_sha256=str(attachment.sha256 or ""),
        source_attachment_id=int(attachment.id),
    )


def _render_page_png(path: Path, *, page_number: int, dpi: int) -> tuple[bytes, int, int, str]:
    errors: list[str] = []
    try:
        import fitz  # type: ignore

        pdf = fitz.open(str(path))
        try:
            if page_number < 1 or page_number > len(pdf):
                raise ValueError(f"page_number must be between 1 and {len(pdf)}.")
            scale = dpi / 72.0
            pix = pdf.load_page(page_number - 1).get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            return bytes(pix.tobytes("png")), int(pix.width), int(pix.height), "pymupdf"
        finally:
            pdf.close()
    except ValueError:
        raise
    except Exception as exc:
        errors.append(f"pymupdf: {exc}")
    try:
        import pypdfium2 as pdfium  # type: ignore

        pdf = pdfium.PdfDocument(str(path))
        if page_number < 1 or page_number > len(pdf):
            raise ValueError(f"page_number must be between 1 and {len(pdf)}.")
        page = pdf[page_number - 1]
        image = page.render(scale=dpi / 72.0).to_pil()
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue(), int(image.width), int(image.height), "pypdfium2"
    except ValueError:
        raise
    except Exception as exc:
        errors.append(f"pypdfium2: {exc}")
    raise RuntimeError("; ".join(errors) or "No PDF page renderer is available.")


def render_document_page(
    session: Session,
    *,
    document_id: int,
    page_number: int,
    dpi: int = 150,
) -> dict[str, Any]:
    document = session.query(Document).filter_by(id=int(document_id)).first()
    if document is None:
        raise ValueError("Document not found.")
    ensure_document_pages(session, document_id=int(document_id))
    page = (
        session.query(DocumentPage)
        .filter_by(document_id=int(document_id), page_number=int(page_number))
        .first()
    )
    if page is None:
        raise ValueError("Page not found in the indexed PDF.")
    attachment = _primary_pdf_attachment(document)
    if attachment is None:
        raise ValueError("Document has no stored PDF attachment.")
    path = _safe_attachment_path(attachment)
    if path is None or not path.exists():
        raise ValueError("Stored PDF attachment is unavailable.")
    dpi_v = max(90, min(int(dpi or 150), 220))
    marker = f"{str(attachment.original_url or attachment.object_path)}#thoth-page={int(page_number)}&dpi={dpi_v}"
    existing = (
        session.query(Attachment)
        .filter_by(document_id=int(document_id), original_url=marker, content_type="image/png")
        .first()
    )
    png, width, height, renderer = _render_page_png(path, page_number=int(page_number), dpi=dpi_v)
    if len(png) > 16_000_000:
        raise ValueError("Rendered page exceeds the 16 MB response safety limit.")
    if existing is None:
        object_path, sha = put_bytes(png, ext=".png")
        existing = Attachment(
            document_id=int(document_id),
            kind="image",
            original_url=marker,
            content_type="image/png",
            object_path=object_path,
            sha256=sha,
            created_at=utcnow(),
        )
        session.add(existing)
        session.flush()
    return {
        "document_id": int(document_id),
        "title": document.title,
        "page_number": int(page_number),
        "page_count": session.query(DocumentPage).filter_by(document_id=int(document_id)).count(),
        "attachment_id": int(existing.id),
        "content_type": "image/png",
        "width": width,
        "height": height,
        "dpi": dpi_v,
        "renderer": renderer,
        "page_text_excerpt": str(page.text or "")[:1200],
        "_image_data_base64": base64.b64encode(png).decode("ascii"),
    }
