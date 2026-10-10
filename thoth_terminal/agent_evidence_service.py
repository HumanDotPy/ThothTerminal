"""Operator-supplied source PDFs for agent research.

An upload is evidence, not a research report or an accepted model claim. The
agent can inspect the stored document and link later conclusions back to it.
"""

from __future__ import annotations

import hashlib
import io
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from pypdf import PdfReader
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.document_page_service import index_pdf_pages_from_bytes
from thoth_terminal.models import Attachment, Document, DocumentEvent, ExternalAgentEvidence, ExternalAgentPrincipal, Source
from thoth_terminal.object_store import put_bytes
from thoth_terminal.text import clean_whitespace, detect_language, extract_text_from_pdf, sha256_text


MAX_AGENT_PDF_BYTES = 30 * 1024 * 1024
MAX_AGENT_PDF_PAGES = 500
UPLOAD_SOURCE_NAME = "agent_evidence_upload"


def _publication_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("published_at must be an ISO date or datetime") from exc
    return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed


def _source_url(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("source_url must be a public http(s) URL without embedded credentials")
    return raw[:2048]


def ingest_agent_pdf(
    session: Session,
    *,
    data: bytes,
    filename: str,
    title: str | None = None,
    publisher: str | None = None,
    source_url: str | None = None,
    published_at: str | None = None,
    rights_class: str = "user_provided",
    principal_id: int | None = None,
) -> dict[str, Any]:
    """Store a bounded PDF in the canonical Document/Attachment/Page rails."""
    if not data or len(data) > MAX_AGENT_PDF_BYTES:
        raise ValueError(f"PDF must be between 1 and {MAX_AGENT_PDF_BYTES} bytes")
    if not data.startswith(b"%PDF-"):
        raise ValueError("Uploaded file is not a PDF")
    try:
        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise ValueError("Encrypted PDFs cannot be inspected by the agent")
        page_count = len(reader.pages)
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError("Uploaded PDF could not be parsed") from exc
    if not 1 <= page_count <= MAX_AGENT_PDF_PAGES:
        raise ValueError(f"PDF must have 1-{MAX_AGENT_PDF_PAGES} pages")
    filename = Path(str(filename or "upload.pdf")).name[:255]
    if Path(filename).suffix.lower() != ".pdf":
        raise ValueError("PDF filename must end in .pdf")
    title = str(title or Path(filename).stem.replace("_", " ").replace("-", " ")).strip()[:512]
    if not title:
        raise ValueError("A title or named PDF is required")
    publisher = str(publisher or "").strip()[:255] or None
    source_url = _source_url(source_url)
    publication_date = _publication_date(published_at)
    rights = str(rights_class or "user_provided").strip().lower()
    if rights not in {"user_provided", "public", "paid", "private"}:
        raise ValueError("rights_class must be user_provided, public, paid, or private")

    sha = hashlib.sha256(data).hexdigest()
    now = utcnow()
    if principal_id is not None:
        principal = session.query(ExternalAgentPrincipal).filter_by(id=int(principal_id), status="active").first()
        if principal is None:
            raise ValueError("External agent principal is unavailable")
    source_name = UPLOAD_SOURCE_NAME if principal_id is None else f"{UPLOAD_SOURCE_NAME}:{int(principal_id)}"
    source = session.query(Source).filter_by(name=source_name).first()
    if source is None:
        source = Source(name=source_name, category="research", priority=3,
                        polling_interval_seconds=None,
                        config={"connector": "manual_agent_upload", "enabled": False,
                                "source_quality": {"rights_class": "user_provided", "acquisition_fragility": "low"}},
                        created_at=now)
        session.add(source)
        session.flush()
    external_id = f"operator_pdf:{sha}" if principal_id is None else f"external_agent_pdf:{int(principal_id)}:{sha}"
    existing = session.query(Document).filter_by(source_id=int(source.id), external_id=external_id).first()
    if existing is not None:
        document_url = f"/docs/{int(existing.id)}" if principal_id is None else f"/public/api/agents/evidence/{int(existing.id)}"
        return {"document_id": int(existing.id), "title": existing.title, "source_id": int(source.id),
                "page_count": int((existing.extra or {}).get("pdf_structure", {}).get("page_count") or page_count),
                "text_chars": len(existing.normalized_text or ""), "created": False,
                "document_url": document_url}

    text = clean_whitespace(extract_text_from_pdf(data))
    guess = detect_language(text)
    object_path, object_sha = put_bytes(data, ext=".pdf")
    doc = Document(
        kind="pdf", source_id=int(source.id), external_id=external_id,
        canonical_url=source_url, url=source_url, title=title, author=publisher,
        published_at=publication_date, ingested_at=now,
        raw_text=text, normalized_text=text, language=guess.language,
        language_confidence=guess.confidence, content_hash=sha256_text(text or title),
        triage_state="NEW", triage_updated_at=now, saved=True, saved_at=now,
        importance=3, storage_tier="hot",
        extra={"research_library": True, "content_kind": "pdf", "upload_surface": "native_harness",
               "rights_class": rights, "acquisition_mode": "user_upload", "publisher": publisher,
               "source_url": source_url, "original_filename": filename, "file_sha256": sha,
               "pdf_text_status": "readable" if text else "needs_visual_review",
               "access_scope": "operator" if principal_id is None else "external_agent",
               "owner_principal_id": int(principal_id) if principal_id is not None else None},
    )
    session.add(doc)
    session.flush()
    attachment = Attachment(document_id=int(doc.id), kind="pdf", original_url=source_url,
                            content_type="application/pdf", object_path=object_path,
                            sha256=object_sha, created_at=now)
    session.add(attachment)
    session.flush()
    indexed = index_pdf_pages_from_bytes(session, document_id=int(doc.id), pdf_bytes=data,
                                         source_sha256=sha, source_attachment_id=int(attachment.id))
    session.add(DocumentEvent(document_id=int(doc.id), event_type="INGESTED", event_at=now,
                              meta={"source": "native_harness_pdf_upload", "rights_class": rights,
                                    "attachment_id": int(attachment.id), "sha256": sha}))
    if principal_id is not None:
        session.add(ExternalAgentEvidence(principal_id=int(principal_id), document_id=int(doc.id), created_at=now))
    session.flush()
    document_url = f"/docs/{int(doc.id)}" if principal_id is None else f"/public/api/agents/evidence/{int(doc.id)}"
    return {"document_id": int(doc.id), "title": doc.title, "source_id": int(source.id),
            "page_count": int(indexed.get("page_count") or page_count), "text_chars": len(text),
            "created": True, "document_url": document_url}
