from __future__ import annotations

import html
import re

from sqlalchemy.orm import Session

from thoth_terminal.models import Document, EarningsEvent, ExchangeDisclosure, IRArtifact


_SUBSIDIARY_ONLY_RESULT_RE = re.compile(
    r"\b("
    r"results?\s+of\s+a\s+(?:listed\s+)?subsidiary|"
    r"by\s+a\s+(?:listed\s+)?subsidiary|"
    r"of\s+a\s+listed\s+subsidiary"
    r")\b",
    re.IGNORECASE,
)


def _text_parts(*values: object) -> list[str]:
    out: list[str] = []
    for value in values:
        raw = str(value or "").strip()
        if raw:
            out.append(html.unescape(raw))
    return out


def _event_policy_blobs(session: Session, event: EarningsEvent) -> list[str]:
    blobs = _text_parts(
        event.source_label_raw,
        event.vendor_fiscal_label,
        event.issuer_fiscal_year_label,
    )
    raw_meta = event.raw_meta if isinstance(event.raw_meta, dict) else {}
    period_meta = raw_meta.get("period_normalization") if isinstance(raw_meta.get("period_normalization"), dict) else {}
    blobs.extend(_text_parts(raw_meta.get("source_label_raw"), period_meta.get("source_label_raw")))

    for artifact_id in (event.release_artifact_id, event.presentation_artifact_id, event.webcast_artifact_id, event.transcript_artifact_id):
        if artifact_id is None:
            continue
        artifact = session.get(IRArtifact, int(artifact_id))
        if artifact is None:
            continue
        blobs.extend(
            _text_parts(
                artifact.title,
                artifact.description,
                artifact.canonical_url,
                artifact.download_url,
                artifact.webcast_url,
                artifact.audio_url,
            )
        )
        artifact_meta = artifact.raw_meta if isinstance(artifact.raw_meta, dict) else {}
        artifact_period = artifact_meta.get("period_normalization") if isinstance(artifact_meta.get("period_normalization"), dict) else {}
        blobs.extend(_text_parts(artifact_meta.get("source_label_raw"), artifact_period.get("source_label_raw")))
        if artifact.document_id is not None:
            doc = session.get(Document, int(artifact.document_id))
            if doc is not None:
                blobs.extend(_text_parts(doc.title, doc.canonical_url, doc.url))

    if event.release_disclosure_id is not None:
        disclosure = session.get(ExchangeDisclosure, int(event.release_disclosure_id))
        if disclosure is not None:
            blobs.extend(_text_parts(disclosure.headline, disclosure.title, disclosure.disclosure_url))
            if disclosure.document_id is not None:
                doc = session.get(Document, int(disclosure.document_id))
                if doc is not None:
                    blobs.extend(_text_parts(doc.title, doc.canonical_url, doc.url))

    return blobs


def earnings_event_is_subsidiary_only_result(session: Session, event: EarningsEvent) -> bool:
    """True when a parent-linked event is only a listed subsidiary result."""

    return any(_SUBSIDIARY_ONLY_RESULT_RE.search(blob) for blob in _event_policy_blobs(session, event))
