from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from thoth_terminal.connectors.sec_edgar import (
    fetch_filing_primary_document_text,
    sec_filing_identity_from_url,
)
from thoth_terminal.db import utcnow
from thoth_terminal.document_refinery import REFINERY_EXTRA_KEY
from thoth_terminal.models import Document, DocumentEvent
from thoth_terminal.text import clean_whitespace, detect_language, sha256_text


SEC_FILING_HYDRATION_MIN_CHARS = 1000
SEC_FILING_HYDRATION_MAX_CHARS = 350_000
SEC_FILING_HYDRATION_RETRY_HOURS = 6
_PERIODIC_FILING_RE = re.compile(r"\b10-[QK](?:/A)?\b", flags=re.IGNORECASE)
_INLINE_XBRL_TOKEN_RE = re.compile(
    r"\b(?:[a-z][a-z0-9_-]{1,24}):(?!//)[A-Za-z][A-Za-z0-9_.-]+",
    flags=re.IGNORECASE,
)
_READABLE_FILING_START_PATTERNS = (
    re.compile(r"(?:^|\n)\s*Table of Contents\s*\n\s*UNITED STATES SECURITIES AND EXCHANGE COMMISSION\b", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*UNITED STATES SECURITIES AND EXCHANGE COMMISSION\b", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*PART I\.?\s+FINANCIAL INFORMATION\b", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*Commission File Number\s*:", re.IGNORECASE),
)
_SECTION_SPECS: tuple[tuple[str, str, str, bool], ...] = (
    (
        "Cover",
        "cover",
        r"\bUNITED STATES SECURITIES AND EXCHANGE COMMISSION\b",
        False,
    ),
    (
        "Financial Statements",
        "financial-statements",
        r"\bITEM\s*1\.?\s+CONDENSED CONSOLIDATED FINANCIAL STATEMENTS\b|\bPART I\.?\s+FINANCIAL INFORMATION\b",
        True,
    ),
    (
        "MD&A",
        "mda",
        r"\bITEM\s*2\.?\s+MANAGEMENT.{0,140}?DISCUSSION.{0,140}?ANALYSIS\b",
        True,
    ),
    (
        "Market Risk",
        "market-risk",
        r"\bITEM\s*3\.?\s+QUANTITATIVE AND QUALITATIVE DISCLOSURES ABOUT MARKET RISK\b",
        True,
    ),
    (
        "Controls",
        "controls",
        r"\bITEM\s*4\.?\s+CONTROLS AND PROCEDURES\b",
        True,
    ),
    (
        "Risk Factors",
        "risk-factors",
        r"\bITEM\s*1A\.?\s+RISK FACTORS\b",
        True,
    ),
    (
        "Legal Proceedings",
        "legal-proceedings",
        r"\bITEM\s*1\.?\s+LEGAL PROCEEDINGS\b",
        True,
    ),
    (
        "Exhibits",
        "exhibits",
        r"\bITEM\s*6\.?\s+EXHIBITS\b",
        True,
    ),
    (
        "Signature",
        "signature",
        r"\bSIGNATURES?\b",
        True,
    ),
)
_REVIEW_EXCERPT_PRIORITY = (
    "mda",
    "financial-statements",
    "risk-factors",
    "controls",
    "market-risk",
)


@dataclass(frozen=True)
class SecFilingTextCleanup:
    text: str
    changed: bool
    reason: str | None = None
    removed_chars: int = 0


def _stored_text(document: Document) -> str:
    return str(document.translation_en or document.normalized_text or document.raw_text or "")


def _taxonomy_token_count(text: str) -> int:
    return len(_INLINE_XBRL_TOKEN_RE.findall(str(text or "")))


def _looks_like_inline_xbrl_preamble(text: str) -> bool:
    sample = str(text or "")[:20_000]
    if len(sample) < 500:
        return False
    token_count = _taxonomy_token_count(sample)
    if token_count >= 24:
        return True
    lines = [line.strip() for line in sample.splitlines() if line.strip()][:260]
    noisy_lines = 0
    for line in lines:
        lower = line.lower()
        if _INLINE_XBRL_TOKEN_RE.search(line) or lower in {"true", "false"} or re.fullmatch(r"\d{7,10}", lower):
            noisy_lines += 1
    return token_count >= 10 and noisy_lines >= 12


def _readable_filing_start_index(text: str) -> int | None:
    sample = str(text or "")[:90_000]
    candidates: list[int] = []
    for pattern in _READABLE_FILING_START_PATTERNS:
        match = pattern.search(sample)
        if match:
            candidates.append(match.start())
    if not candidates:
        return None
    return max(0, min(candidates))


def clean_sec_filing_text(text: str) -> SecFilingTextCleanup:
    cleaned = clean_whitespace(str(text or ""))
    start = _readable_filing_start_index(cleaned)
    if start is None or start < 750:
        return SecFilingTextCleanup(text=cleaned, changed=cleaned != str(text or ""))
    preamble = cleaned[:start]
    if not _looks_like_inline_xbrl_preamble(preamble):
        return SecFilingTextCleanup(text=cleaned, changed=cleaned != str(text or ""))
    trimmed = cleaned[start:].lstrip()
    return SecFilingTextCleanup(
        text=trimmed,
        changed=True,
        reason="inline_xbrl_preamble",
        removed_chars=start,
    )


def _refinery_payload_looks_taxonomy_noisy(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    parts: list[str] = []
    for key in ("signal", "summary", "why_it_matters"):
        parts.append(str(payload.get(key) or ""))
    for key in ("key_points", "topics"):
        value = payload.get(key)
        if isinstance(value, list):
            parts.extend(str(item or "") for item in value)
    text = " ".join(parts)
    return bool(_INLINE_XBRL_TOKEN_RE.search(text))


def _invalidate_noisy_refinery(extra: dict[str, Any], *, now: datetime, reason: str) -> None:
    payload = extra.get(REFINERY_EXTRA_KEY)
    if not _refinery_payload_looks_taxonomy_noisy(payload):
        return
    stale = extra.get("stale_document_refinery")
    stale_rows = list(stale) if isinstance(stale, list) else []
    assert isinstance(payload, dict)
    stale_rows.append(
        {
            "invalidated_at": now.isoformat(),
            "reason": reason,
            "source_hash": payload.get("source_hash"),
            "signal": payload.get("signal"),
            "summary": payload.get("summary"),
        }
    )
    extra["stale_document_refinery"] = stale_rows[-5:]
    extra.pop(REFINERY_EXTRA_KEY, None)


def _is_readable_sec_text(text: str) -> bool:
    cleaned = clean_whitespace(str(text or ""))
    if len(cleaned) < SEC_FILING_HYDRATION_MIN_CHARS:
        return False
    cleanup = clean_sec_filing_text(cleaned)
    return not cleanup.reason


def _apply_clean_sec_text(
    session: Session,
    document: Document,
    *,
    cleanup: SecFilingTextCleanup,
    old_text: str,
    now: datetime,
) -> dict[str, Any]:
    previous_translation = str(document.translation_en or "")
    document.raw_text = cleanup.text
    document.normalized_text = cleanup.text
    if previous_translation and (previous_translation.strip() == old_text.strip() or previous_translation.strip() != cleanup.text.strip()):
        document.translation_en = None
        document.translation_backend = None
        document.translation_model = None
        document.translation_glossary_version = None
        document.translated_at = None
    guess = detect_language(cleanup.text, min_chars=30)
    document.language = guess.language or document.language
    document.language_confidence = guess.confidence if guess.confidence is not None else document.language_confidence
    document.content_hash = sha256_text(cleanup.text)
    extra = dict(document.extra or {})
    _invalidate_noisy_refinery(extra, now=now, reason="sec_filing_text_cleaned")
    readability = {
        "status": "cleaned",
        "attempted_at": now.isoformat(),
        "reason": cleanup.reason,
        "removed_chars": cleanup.removed_chars,
        "text_chars": len(cleanup.text),
    }
    extra["sec_filing_readability"] = readability
    document.extra = extra
    session.add(
        DocumentEvent(
            document_id=int(document.id),
            event_type="TEXT_CLEANED",
            event_at=now,
            meta={
                "source": "sec_filing_text",
                "reason": cleanup.reason,
                "removed_chars": cleanup.removed_chars,
                "text_chars": len(cleanup.text),
            },
        )
    )
    session.flush()
    return {"attempted": True, **readability}


def _is_sec_filing_candidate(document: Document) -> bool:
    extra = document.extra if isinstance(document.extra, dict) else {}
    connector = str(extra.get("connector") or "").strip().lower()
    raw_url = str(getattr(document, "url", "") or getattr(document, "canonical_url", "") or "")
    url = raw_url.lower()
    kind = str(getattr(document, "kind", "") or "").strip().lower()
    if kind == "sec_filing":
        return connector in {"sec_edgar", "sec_edgar_rss"} or "sec.gov/archives/edgar/data" in url
    if "sec.gov/archives/edgar/data" in url:
        return True
    cik, accession = sec_filing_identity_from_url(raw_url, extra=extra)
    if not accession:
        return False
    title = str(getattr(document, "title", "") or "")
    filing_type = str(extra.get("filing_type") or "")
    return bool(cik and _PERIODIC_FILING_RE.search(f"{title} {filing_type}"))


def _recent_failed_attempt(extra: dict[str, Any], *, now: datetime) -> bool:
    payload = extra.get("sec_filing_hydration")
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
    return attempted >= now - timedelta(hours=SEC_FILING_HYDRATION_RETRY_HOURS)


def maybe_hydrate_sec_filing_document(
    session: Session,
    document: Document,
    *,
    force: bool = False,
) -> dict[str, Any]:
    """
    Fill metadata-only SEC filing documents with readable EDGAR filing text.

    Current-filings Atom rows intentionally ingest cheap metadata. Hydration is
    on-demand so the feed can stay light while document detail and local-agent
    reads get the body when they explicitly open the filing.
    """
    if not _is_sec_filing_candidate(document):
        return {"attempted": False, "reason": "not_sec_filing"}

    old_text = _stored_text(document)
    old_cleanup = clean_sec_filing_text(old_text)
    extra = dict(document.extra or {})
    now = utcnow()
    if (
        not force
        and old_cleanup.reason
        and len(old_cleanup.text) >= SEC_FILING_HYDRATION_MIN_CHARS
    ):
        return _apply_clean_sec_text(session, document, cleanup=old_cleanup, old_text=old_text, now=now)
    if not force and _is_readable_sec_text(old_text):
        _invalidate_noisy_refinery(extra, now=now, reason="sec_filing_already_readable")
        document.extra = extra
        session.flush()
        return {"attempted": False, "reason": "already_readable"}

    if not force and _recent_failed_attempt(extra, now=now):
        return {"attempted": False, "reason": "recent_failed_attempt"}

    cik, accession = sec_filing_identity_from_url(
        document.url or document.canonical_url,
        extra=extra,
    )
    if not cik or not accession:
        hydration = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "error": "Unable to resolve SEC CIK/accession from document metadata.",
        }
        extra["sec_filing_hydration"] = hydration
        document.extra = extra
        session.flush()
        return {"attempted": True, **hydration}

    try:
        result = fetch_filing_primary_document_text(cik=cik, accession=accession)
    except Exception as exc:
        hydration = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "cik": cik,
            "accession_number": accession,
            "error": str(exc)[:500],
        }
        extra["sec_filing_hydration"] = hydration
        document.extra = extra
        session.flush()
        return {"attempted": True, **hydration}

    if not result.get("ok"):
        hydration = {
            "status": "error",
            "attempted_at": now.isoformat(),
            "cik": cik,
            "accession_number": accession,
            "error": str(result.get("error") or "SEC filing hydration failed.")[:500],
        }
        extra["sec_filing_hydration"] = hydration
        document.extra = extra
        session.flush()
        return {"attempted": True, **hydration}

    fetched_text = clean_whitespace(str(result.get("text") or ""))
    cleanup = clean_sec_filing_text(fetched_text)
    text = cleanup.text
    truncated = False
    if len(text) > SEC_FILING_HYDRATION_MAX_CHARS:
        text = text[:SEC_FILING_HYDRATION_MAX_CHARS].rstrip()
        text += "\n\n[SEC filing text truncated by Thoth Terminal. Open the source filing for the complete document.]"
        truncated = True

    previous_translation = str(document.translation_en or "")
    document.raw_text = text
    document.normalized_text = text
    if not previous_translation or previous_translation.strip() == old_text.strip() or len(previous_translation.strip()) < SEC_FILING_HYDRATION_MIN_CHARS:
        document.translation_en = None
        document.translation_backend = None
        document.translation_model = None
        document.translation_glossary_version = None
        document.translated_at = None
    guess = detect_language(text, min_chars=30)
    document.language = guess.language or document.language
    document.language_confidence = guess.confidence if guess.confidence is not None else document.language_confidence
    document.content_hash = sha256_text(text)

    primary_url = str(result.get("url") or "").strip()
    if primary_url:
        extra.setdefault("sec_index_url", document.url or document.canonical_url)
        document.url = primary_url
    hydration = {
        "status": "ok",
        "attempted_at": now.isoformat(),
        "cik": cik,
        "accession_number": accession,
        "primary_document_url": primary_url or None,
        "primary_document": result.get("filename"),
        "content_type": result.get("content_type"),
        "text_chars": len(text),
        "truncated": truncated,
    }
    if cleanup.reason:
        hydration["readability_cleanup"] = {
            "reason": cleanup.reason,
            "removed_chars": cleanup.removed_chars,
        }
    _invalidate_noisy_refinery(extra, now=now, reason="sec_filing_hydrated")
    extra["cik"] = cik
    extra["accession_number"] = accession
    extra["primary_document_url"] = primary_url or extra.get("primary_document_url")
    extra["sec_filing_hydration"] = hydration
    document.extra = extra
    session.add(
        DocumentEvent(
            document_id=int(document.id),
            event_type="TEXT_HYDRATED",
            event_at=now,
            meta={
                "source": "sec_edgar",
                "cik": cik,
                "accession_number": accession,
                "primary_document_url": primary_url or None,
                "text_chars": len(text),
                "truncated": truncated,
            },
        )
    )
    session.flush()
    return {"attempted": True, **hydration}


def _looks_like_toc_match(text: str, start: int) -> bool:
    chunk = str(text or "")[start : start + 520]
    if start >= 45_000:
        return False
    return len(re.findall(r"\bItem\s+\d[A-Z]?\b", chunk, flags=re.IGNORECASE)) >= 2


def _find_section_start(text: str, pattern: str, *, skip_toc: bool) -> int | None:
    matches = list(re.finditer(pattern, text, flags=re.IGNORECASE | re.DOTALL))
    if not matches:
        return None
    if not skip_toc:
        return matches[0].start()
    for match in matches:
        if not _looks_like_toc_match(text, match.start()):
            return match.start()
    return matches[-1].start()


def _clip_section(text: str, *, max_chars: int) -> tuple[str, bool]:
    section = clean_whitespace(text)
    limit = max(800, int(max_chars or 0))
    if len(section) <= limit:
        return section, False
    clipped = section[:limit].rstrip(" ,;:-")
    return (
        clipped
        + "\n\n[Section clipped for reading speed. Use the full reading surface below for the complete text.]",
        True,
    )


def build_sec_filing_review(
    document: Document,
    *,
    max_section_chars: int = 4_800,
) -> dict[str, Any] | None:
    """Build a sectioned analyst reading surface for SEC periodic filings."""
    if not _is_sec_filing_candidate(document):
        return None
    extra = document.extra if isinstance(document.extra, dict) else {}
    cleanup = clean_sec_filing_text(_stored_text(document))
    text = cleanup.text
    if len(text) < SEC_FILING_HYDRATION_MIN_CHARS:
        return {
            "status": "unreadable",
            "form": extra.get("filing_type"),
            "accession_number": extra.get("accession_number"),
            "sections": [],
            "quality_flags": ["Stored SEC filing text is still too short for review."],
        }

    starts: list[dict[str, Any]] = []
    seen_offsets: set[int] = set()
    for label, anchor, pattern, skip_toc in _SECTION_SPECS:
        idx = _find_section_start(text, pattern, skip_toc=skip_toc)
        if idx is None or idx in seen_offsets:
            continue
        seen_offsets.add(idx)
        starts.append({"label": label, "anchor": anchor, "start": idx})
    starts.sort(key=lambda row: int(row["start"]))

    sections: list[dict[str, Any]] = []
    for pos, row in enumerate(starts):
        start = int(row["start"])
        end = int(starts[pos + 1]["start"]) if pos + 1 < len(starts) else len(text)
        excerpt, clipped = _clip_section(text[start:end], max_chars=max_section_chars)
        if not excerpt:
            continue
        sections.append(
            {
                "label": row["label"],
                "anchor": row["anchor"],
                "excerpt": excerpt,
                "start": start,
                "char_count": max(0, end - start),
                "clipped": clipped,
            }
        )

    quality_flags: list[str] = []
    if cleanup.reason:
        quality_flags.append("Inline XBRL preamble hidden from review.")
    if not any(section["anchor"] == "mda" for section in sections):
        quality_flags.append("MD&A heading was not detected.")
    return {
        "status": "readable" if sections else "unreadable",
        "form": extra.get("filing_type"),
        "accession_number": extra.get("accession_number"),
        "primary_document_url": extra.get("primary_document_url") or getattr(document, "url", None),
        "sections": sections,
        "quality_flags": quality_flags,
    }


def filing_review_excerpt(document: Document, *, max_chars: int = 1_200) -> str | None:
    """Return a compact analyst-first excerpt for feed cards and local-agent reads."""
    limit = max(240, int(max_chars or 0))
    review = build_sec_filing_review(document, max_section_chars=min(2_000, max(600, limit)))
    if not review or review.get("status") != "readable":
        return None
    sections = [row for row in (review.get("sections") or []) if isinstance(row, dict)]
    by_anchor = {str(row.get("anchor") or ""): row for row in sections}
    ordered: list[dict[str, Any]] = []
    for anchor in _REVIEW_EXCERPT_PRIORITY:
        row = by_anchor.get(anchor)
        if row:
            ordered.append(row)
    if not ordered:
        ordered = sections[:2]

    parts: list[str] = []
    for row in ordered[:3]:
        label = str(row.get("label") or "Filing section").strip()
        excerpt = clean_whitespace(str(row.get("excerpt") or ""))
        if not excerpt:
            continue
        excerpt = re.sub(r"\n?\[Section clipped for reading speed\..*?\]\s*$", "", excerpt, flags=re.DOTALL).strip()
        parts.append(f"{label}: {excerpt}")
    if not parts:
        return None
    text = clean_whitespace(" ".join(parts))
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip(" ,;:-") + "..."
