from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.config import settings
from thoth_terminal.db import session_factory, session_scope, utcnow
from thoth_terminal.discord import services
from thoth_terminal.discord.models import (
    DiscordEvidenceItem,
    DiscordLinkInboxItem,
    DiscordLinkScanState,
    DiscordQueueItem,
)
from thoth_terminal.models import Attachment, Company, Document, DocumentEvent, Issuer, IssuerIdentifier, Source
from thoth_terminal.object_store import put_bytes
from thoth_terminal.text import clean_whitespace, detect_language, extract_text_from_pdf, sha256_text
from thoth_terminal.instrument_service import REQUEST_STATUS_REVIEW, create_instrument_request
from thoth_terminal.local_model_runtime import local_model_endpoint, local_model_name


INBOX_STATUS_COLLECTED = "collected"
INBOX_STATUS_DETERMINISTIC_INGESTED = "deterministic_ingested"
INBOX_STATUS_AGENTIC_INGESTED = "agentic_ingested"
INBOX_STATUS_RESOLUTION_REQUESTED = "resolution_requested"
INBOX_STATUS_QUEUED = "queued"
INBOX_STATUS_IGNORED = "ignored"
INBOX_STATUS_ERROR = "error"

INBOX_STATUSES = {
    INBOX_STATUS_COLLECTED,
    INBOX_STATUS_DETERMINISTIC_INGESTED,
    INBOX_STATUS_AGENTIC_INGESTED,
    INBOX_STATUS_RESOLUTION_REQUESTED,
    INBOX_STATUS_QUEUED,
    INBOX_STATUS_IGNORED,
    INBOX_STATUS_ERROR,
}

_DOLLAR_TICKER_RE = re.compile(r"(?<![A-Za-z0-9])\$(?P<sym>[A-Za-z][A-Za-z0-9.\-]{0,15})\b")
_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_UNSET = object()

_TRACKING_QUERY_KEYS = {
    "fbclid",
    "gclid",
    "dclid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "igshid",
    "si",
    "spm",
}

_ARXIV_HOSTS = {"arxiv.org", "export.arxiv.org"}
_ARXIV_REDIRECT_SOURCE_HOSTS = {"t.co"}
_ARXIV_MODERN_ID_RE = re.compile(r"^(?P<id>\d{4}\.\d{4,5})(?:v(?P<version>\d+))?$", re.IGNORECASE)
_ARXIV_OLD_ID_RE = re.compile(r"^(?P<archive>[a-z\-]+(?:\.[A-Z]{2})?)/(?P<num>\d{7})(?:v(?P<version>\d+))?$", re.IGNORECASE)
_ARXIV_MAX_BYTES = 25_000_000
_ARXIV_FETCH_TIMEOUT_S = 20.0
_X_STATUS_HOSTS = {"x.com", "twitter.com", "www.x.com", "www.twitter.com", "mobile.x.com", "mobile.twitter.com"}
_X_STATUS_PATH_RE = re.compile(r"^/(?P<handle>[A-Za-z0-9_]{1,15}|i)/status/(?P<id>[0-9]{1,25})/?$")


def canonical_x_status_url(url: str | None) -> str | None:
    """Accept a single public X status URL, without tracking parameters."""
    raw = str(url or "").strip()
    try:
        parsed = urlparse(raw)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or parsed.netloc.lower() not in _X_STATUS_HOSTS:
        return None
    match = _X_STATUS_PATH_RE.fullmatch(parsed.path)
    if not match:
        return None
    return f"https://x.com/{match.group('handle')}/status/{match.group('id')}"


def enqueue_x_thread(*, top_url: str, bottom_url: str | None, requested_by_user_id: int) -> int:
    from thoth_terminal.jobs import enqueue_x_thread_ingest_job

    with session_scope() as session:
        job = enqueue_x_thread_ingest_job(
            session,
            top_url=top_url,
            bottom_url=bottom_url,
            requested_by_user_id=requested_by_user_id,
        )
        return int(job.id)


@dataclass(frozen=True)
class DeterministicTickerMatch:
    ticker: str | None
    candidates: list[str]
    reason: str
    targets: list[dict[str, Any]] = field(default_factory=list)
    candidate_targets: list[dict[str, Any]] = field(default_factory=list)
    resolution_state: str = "pending"


@dataclass(frozen=True)
class IntakeResult:
    status: str
    inbox_id: int | None
    source_id: int | None
    ticker: str | None
    created: bool
    url_canonical: str | None


@dataclass
class TickerNameMatcher:
    by_first_token: dict[str, list[tuple[str, dict[str, Any]]]]
    symbol_targets: dict[str, dict[str, Any]] = field(default_factory=dict)
    all_name_targets: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    topic_rules: list[dict[str, Any]] = field(default_factory=list)
    macro_rules: list[dict[str, Any]] = field(default_factory=list)

    def resolve(self, text: str | None) -> DeterministicTickerMatch:
        raw = str(text or "")
        explicit_targets = _unique_targets(
            [
                _copy_target(self.symbol_targets.get(sym) or _generic_ticker_target(sym))
                for sym in sorted(_extract_dollar_tickers(raw))
            ]
        )
        if len(explicit_targets) == 1:
            return _resolved_match(explicit_targets, reason="ticker")
        if explicit_targets:
            return _ambiguous_match(explicit_targets, reason="ambiguous")

        name_hits = self._name_targets(raw)
        if len(name_hits) == 1:
            return _resolved_match(name_hits, reason="company_name")
        if name_hits:
            return _ambiguous_match(name_hits, reason="ambiguous")

        topic_hits = self._rule_targets(raw, self.topic_rules)
        if topic_hits:
            return _resolved_match(topic_hits, reason="topic_rule")

        macro_hits = self._rule_targets(raw, self.macro_rules)
        if macro_hits:
            return _resolved_match(macro_hits, reason="macro_rule")

        candidate_targets = self._rank_candidates(raw)
        return DeterministicTickerMatch(
            ticker=None,
            candidates=_target_symbols(candidate_targets),
            reason="none",
            targets=[],
            candidate_targets=candidate_targets,
            resolution_state="pending",
        )

    def _name_targets(self, text: str) -> list[dict[str, Any]]:
        text_norm = _normalize_phrase(text)
        if not text_norm:
            return []
        padded = f" {text_norm} "
        tokens = set(text_norm.split())
        hits: list[dict[str, Any]] = []
        for token in tokens:
            for phrase_norm, target in self.by_first_token.get(token, []):
                if f" {phrase_norm} " not in padded:
                    continue
                candidate = _copy_target(target)
                extra_json = dict(candidate.get("extra_json") or {})
                extra_json.setdefault("match_phrase", phrase_norm)
                candidate["extra_json"] = extra_json
                hits.append(candidate)
        return _unique_targets(hits)

    def _rule_targets(self, text: str, rules: list[dict[str, Any]]) -> list[dict[str, Any]]:
        text_norm = _normalize_phrase(text)
        if not text_norm:
            return []
        raw_lower = str(text or "").lower()
        padded = f" {text_norm} "
        hits: list[dict[str, Any]] = []
        for rule in rules:
            terms = [str(term or "").strip() for term in (rule.get("terms") or []) if str(term or "").strip()]
            matched_term = None
            for term in terms:
                term_norm = _normalize_phrase(term)
                if not term_norm:
                    continue
                if f" {term_norm} " in padded or str(term).lower() in raw_lower:
                    matched_term = term
                    break
            if not matched_term:
                continue
            for target in rule.get("targets") or []:
                candidate = _copy_target(target)
                extra_json = dict(candidate.get("extra_json") or {})
                extra_json.setdefault("match_term", matched_term)
                extra_json.setdefault("rule_name", rule.get("name"))
                candidate["extra_json"] = extra_json
                hits.append(candidate)
        return _unique_targets(hits)

    def _rank_candidates(self, text: str, *, limit: int = 6) -> list[dict[str, Any]]:
        text_norm = _normalize_phrase(text)
        if not text_norm:
            return []
        text_tokens = set(text_norm.split())
        scored: list[tuple[float, str, dict[str, Any]]] = []
        seen: set[str] = set()
        for phrase_norm, target in self.all_name_targets:
            phrase_tokens = set(phrase_norm.split())
            if not phrase_tokens:
                continue
            overlap = len(text_tokens & phrase_tokens)
            if overlap <= 0:
                continue
            overlap_ratio = overlap / max(1, len(phrase_tokens))
            if overlap < min(2, len(phrase_tokens)) and overlap_ratio < 0.75:
                continue
            target_key = str(target.get("target_key") or "").strip()
            if not target_key or target_key in seen:
                continue
            seen.add(target_key)
            score = overlap_ratio + min(0.15, len(phrase_norm) / 120.0)
            candidate = _copy_target(target)
            extra_json = dict(candidate.get("extra_json") or {})
            extra_json["match_phrase"] = phrase_norm
            extra_json["score"] = round(score, 4)
            candidate["extra_json"] = extra_json
            scored.append((score, phrase_norm, candidate))
        scored.sort(key=lambda item: (item[0], len(item[1])), reverse=True)
        return [candidate for _, _, candidate in scored[: max(1, int(limit))]]


def _now() -> datetime:
    return utcnow()


def _normalize_symbol(value: str | None) -> str:
    return (value or "").strip().lstrip("$").upper()


def _normalize_phrase(value: str | None) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    return " ".join([part for part in _SPLIT_RE.split(raw) if part])


def _extract_dollar_tickers(text: str | None) -> set[str]:
    raw = str(text or "")
    out: set[str] = set()
    for match in _DOLLAR_TICKER_RE.finditer(raw):
        sym = _normalize_symbol(match.group("sym"))
        if sym:
            out.add(sym)
    return out


def should_skip_message_author(*, author_is_bot: bool, webhook_id: int | None) -> bool:
    return bool(author_is_bot) or (webhook_id is not None)


def _is_url_char(ch: str) -> bool:
    if ch.isalnum():
        return True
    return ch in "-._~:/?#[]@!$&'()*+,;=%"


def extract_urls_from_text(text: str | None) -> list[str]:
    if not text:
        return []
    cleaned = (
        str(text)
        .replace("\u00ad", "")
        .replace("\u200b", "")
        .replace("\u200c", "")
        .replace("\u200d", "")
        .replace("\ufeff", "")
    )
    urls: list[str] = []
    n = len(cleaned)
    i = 0
    while i < n:
        if cleaned.startswith("http://", i) or cleaned.startswith("https://", i):
            scheme = "https://" if cleaned.startswith("https://", i) else "http://"
            j = i + len(scheme)
            buf = scheme
            while j < n:
                ch = cleaned[j]
                if _is_url_char(ch):
                    buf += ch
                    j += 1
                    continue
                if ch.isspace():
                    k = j
                    while k < n and cleaned[k].isspace():
                        k += 1
                    if k < n and _is_url_char(cleaned[k]):
                        j = k
                        continue
                    break
                break
            buf = buf.rstrip(").,;\"'…")
            if buf and buf not in urls:
                urls.append(buf)
            i = j
            continue
        if cleaned.startswith("www.", i) and (i == 0 or not cleaned[i - 1].isalnum()):
            j = i + 4
            buf = "https://www."
            while j < n:
                ch = cleaned[j]
                if _is_url_char(ch):
                    buf += ch
                    j += 1
                    continue
                if ch.isspace():
                    k = j
                    while k < n and cleaned[k].isspace():
                        k += 1
                    if k < n and _is_url_char(cleaned[k]):
                        j = k
                        continue
                    break
                break
            buf = buf.rstrip(").,;\"'…")
            if buf and buf not in urls:
                urls.append(buf)
            i = j
            continue
        i += 1
    return urls


def canonicalize_url(url: str | None) -> str | None:
    raw = (url or "").strip()
    if not raw:
        return None
    if raw.startswith("www."):
        raw = "https://" + raw
    parsed = urlparse(raw)
    scheme = (parsed.scheme or "").lower()
    if scheme not in {"http", "https"}:
        return None
    netloc = (parsed.netloc or "").strip().lower()
    if not netloc:
        return None
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = parsed.path or "/"
    path = re.sub(r"/{2,}", "/", path)
    kept: list[tuple[str, str]] = []
    for key, val in parse_qsl(parsed.query or "", keep_blank_values=True):
        lk = key.strip().lower()
        if lk.startswith("utm_"):
            continue
        if lk in _TRACKING_QUERY_KEYS:
            continue
        kept.append((key, val))
    query = urlencode(sorted(kept), doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def _arxiv_host(url: str | None) -> str:
    try:
        parsed = urlparse(str(url or "").strip())
    except Exception:
        return ""
    netloc = (parsed.netloc or "").strip().lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def _normalize_arxiv_id(raw_id: str | None) -> str | None:
    raw = str(raw_id or "").strip().strip("/")
    if raw.lower().endswith(".pdf"):
        raw = raw[:-4]
    raw = raw.replace("//", "/")
    if ".." in raw or not raw:
        return None
    modern = _ARXIV_MODERN_ID_RE.fullmatch(raw)
    if modern:
        base = modern.group("id")
        version = modern.group("version")
        return f"{base}v{version}" if version else base
    old = _ARXIV_OLD_ID_RE.fullmatch(raw)
    if old:
        version = old.group("version")
        base = f"{old.group('archive')}/{old.group('num')}"
        return f"{base}v{version}" if version else base
    return None


def canonical_arxiv_urls(url: str | None) -> dict[str, str] | None:
    canonical = canonicalize_url(url)
    if not canonical:
        return None
    parsed = urlparse(canonical)
    if _arxiv_host(canonical) not in _ARXIV_HOSTS:
        return None
    parts = [part for part in (parsed.path or "").split("/") if part]
    if len(parts) < 2 or parts[0].lower() not in {"abs", "pdf"}:
        return None
    arxiv_id = _normalize_arxiv_id("/".join(parts[1:]))
    if not arxiv_id:
        return None
    return {
        "arxiv_id": arxiv_id,
        "abs_url": f"https://arxiv.org/abs/{arxiv_id}",
        "pdf_url": f"https://arxiv.org/pdf/{arxiv_id}.pdf",
    }


def resolve_arxiv_link(url: str | None, *, timeout_s: float = 8.0) -> dict[str, str] | None:
    direct = canonical_arxiv_urls(url)
    if direct:
        return direct
    canonical = canonicalize_url(url)
    host = _arxiv_host(canonical)
    if host not in _ARXIV_REDIRECT_SOURCE_HOSTS:
        return None
    session = requests.Session()
    session.max_redirects = 5
    try:
        resp = session.head(
            str(canonical),
            headers={"User-Agent": "ThothTerminal/1.0 arxiv-link-intake"},
            timeout=timeout_s,
            allow_redirects=True,
        )
        final_url = str(resp.url or "")
    except requests.RequestException:
        return None
    return canonical_arxiv_urls(final_url)


def _fetch_arxiv_pdf(pdf_url: str, *, timeout_s: float = _ARXIV_FETCH_TIMEOUT_S, max_bytes: int = _ARXIV_MAX_BYTES) -> tuple[bytes, str | None, str]:
    if not canonical_arxiv_urls(pdf_url):
        raise ValueError("Refusing to fetch non-arXiv PDF URL.")
    session = requests.Session()
    session.max_redirects = 3
    try:
        with session.get(
            pdf_url,
            headers={
                "User-Agent": "ThothTerminal/1.0 arxiv-link-intake",
                "Accept": "application/pdf,*/*;q=0.1",
            },
            timeout=timeout_s,
            allow_redirects=True,
            stream=True,
        ) as resp:
            final_url = str(resp.url or pdf_url)
            if not canonical_arxiv_urls(final_url):
                raise ValueError("arXiv PDF fetch redirected off allowlist.")
            if resp.status_code >= 400:
                raise ValueError(f"HTTP {resp.status_code} fetching arXiv PDF.")
            content_type = resp.headers.get("content-type")
            chunks: list[bytes] = []
            total = 0
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > int(max_bytes):
                    raise ValueError("arXiv PDF exceeds byte cap.")
                chunks.append(chunk)
    except requests.RequestException as exc:
        raise ValueError(f"arXiv PDF request failed: {exc}") from exc
    payload = b"".join(chunks)
    if not payload.startswith(b"%PDF-"):
        raise ValueError("arXiv response was not a PDF.")
    return payload, content_type, final_url


def _ensure_arxiv_source(session: Session, *, now: datetime) -> Source:
    source = session.query(Source).filter_by(name="arxiv").first()
    if source:
        return source
    source = Source(
        name="arxiv",
        category="research",
        priority=3,
        polling_interval_seconds=None,
        config={"connector": "discord_link_intake", "rights_class": "public", "allowlisted_host": "arxiv.org"},
        created_at=now,
    )
    session.add(source)
    session.flush()
    return source


def _upsert_arxiv_document(
    session: Session,
    *,
    arxiv: dict[str, str],
    pdf_bytes: bytes,
    content_type: str | None,
    final_pdf_url: str,
    title_hint: str | None,
    guild_id: int,
    channel_id: int,
    message_id: int,
    author_user_id: int | None,
    now: datetime,
) -> Document:
    source = _ensure_arxiv_source(session, now=now)
    arxiv_id = str(arxiv["arxiv_id"])
    abs_url = str(arxiv["abs_url"])
    pdf_url = str(arxiv["pdf_url"])
    text = clean_whitespace(extract_text_from_pdf(pdf_bytes))
    if not text:
        text = f"arXiv paper {arxiv_id}. PDF was stored but text extraction returned no readable text."
    lang = detect_language(text)
    title = str(title_hint or "").strip() or f"arXiv {arxiv_id}"
    extra = {
        "source": "arxiv",
        "arxiv_id": arxiv_id,
        "abs_url": abs_url,
        "pdf_url": pdf_url,
        "final_pdf_url": final_pdf_url,
        "via": "discord_link_intake",
        "discord": {
            "guild_id": int(guild_id),
            "channel_id": int(channel_id),
            "message_id": int(message_id),
            "author_user_id": int(author_user_id) if author_user_id is not None else None,
        },
        "security": {
            "allowlisted_hosts": sorted(_ARXIV_HOSTS),
            "agent_instruction_policy": "untrusted_source_material",
            "max_pdf_bytes": _ARXIV_MAX_BYTES,
        },
    }
    doc = (
        session.query(Document)
        .filter_by(source_id=int(source.id), external_id=f"arxiv:{arxiv_id}")
        .first()
    )
    content_hash = sha256_text(text)
    if doc is None:
        doc = Document(
            kind="paper",
            source_id=int(source.id),
            external_id=f"arxiv:{arxiv_id}",
            canonical_url=abs_url,
            url=abs_url,
            title=title[:512],
            author=None,
            published_at=None,
            ingested_at=now,
            raw_text=text,
            normalized_text=text,
            language=lang.language,
            language_confidence=lang.confidence,
            translation_en=None,
            translation_backend=None,
            translation_model=None,
            translation_glossary_version=None,
            translated_at=None,
            triage_state="NEW",
            saved=False,
            seen_at=None,
            opened_at=None,
            saved_at=None,
            actioned_at=None,
            ignored_at=None,
            triage_updated_at=now,
            content_hash=content_hash,
            extra=extra,
            importance=int(source.priority or 1),
            storage_tier="hot",
            cold_archive_id=None,
        )
        session.add(doc)
        session.flush()
        session.add(DocumentEvent(document_id=int(doc.id), event_type="INGESTED", event_at=now, meta={"source": "arxiv_discord_link_intake", "arxiv_id": arxiv_id}))
    else:
        merged_extra = dict(doc.extra or {})
        merged_extra.update(extra)
        doc.canonical_url = abs_url
        doc.url = abs_url
        doc.title = title[:512] or doc.title
        doc.raw_text = text
        doc.normalized_text = text
        doc.language = lang.language or doc.language
        doc.language_confidence = lang.confidence if lang.confidence is not None else doc.language_confidence
        doc.content_hash = content_hash
        doc.extra = merged_extra
        session.add(DocumentEvent(document_id=int(doc.id), event_type="UPDATED", event_at=now, meta={"source": "arxiv_discord_link_intake", "arxiv_id": arxiv_id}))
        session.flush()

    object_path, sha = put_bytes(pdf_bytes, ext=".pdf")
    existing = (
        session.query(Attachment)
        .filter_by(document_id=int(doc.id), sha256=sha)
        .first()
    )
    if existing is None:
        session.add(
            Attachment(
                document_id=int(doc.id),
                kind="pdf",
                original_url=pdf_url,
                content_type=(content_type or "application/pdf")[:128],
                object_path=object_path,
                sha256=sha,
                created_at=now,
            )
        )
    session.flush()
    return doc


def maybe_ingest_arxiv_document_for_inbox(
    session: Session,
    *,
    row: DiscordLinkInboxItem,
    title_hint: str | None,
    now: datetime,
) -> Document | None:
    arxiv = resolve_arxiv_link(row.url_raw) or resolve_arxiv_link(row.url_canonical)
    if not arxiv:
        return None
    pdf_bytes, content_type, final_pdf_url = _fetch_arxiv_pdf(str(arxiv["pdf_url"]))
    return _upsert_arxiv_document(
        session,
        arxiv=arxiv,
        pdf_bytes=pdf_bytes,
        content_type=content_type,
        final_pdf_url=final_pdf_url,
        title_hint=title_hint,
        guild_id=int(row.guild_id),
        channel_id=int(row.channel_id),
        message_id=int(row.message_id),
        author_user_id=(int(row.author_user_id) if row.author_user_id is not None else None),
        now=now,
    )


def _record_arxiv_ingest_result(
    row: DiscordLinkInboxItem,
    *,
    document: Document | None = None,
    error: Exception | str | None = None,
) -> None:
    payload = dict(row.agentic_decision_json or {})
    arxiv_payload = dict(payload.get("arxiv") or {})
    if document is not None:
        arxiv_payload.update(
            {
                "status": "document_ingested",
                "document_id": int(document.id),
                "arxiv_id": (dict(document.extra or {}).get("arxiv_id")),
                "canonical_url": document.canonical_url,
            }
        )
    elif error is not None:
        arxiv_payload.update({"status": "error", "error": str(error)[:500]})
    if arxiv_payload:
        payload["arxiv"] = arxiv_payload
        row.agentic_decision_json = payload


def hash_url(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def build_message_excerpt(text: str | None, *, max_len: int = 280) -> str | None:
    if not text:
        return None
    cleaned = " ".join(str(text).split())
    if not cleaned:
        return None
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 1] + "…"


def extract_candidate_urls(
    *,
    content: str | None,
    embed_urls: list[str] | None,
    attachment_urls: list[str] | None,
) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in extract_urls_from_text(content):
        key = raw.strip()
        if key and key not in seen:
            out.append(key)
            seen.add(key)
    for seq in (embed_urls or [], attachment_urls or []):
        key = str(seq or "").strip()
        if key and key not in seen:
            out.append(key)
            seen.add(key)
    return out


def _add_name_mapping(index: dict[str, set[str]], *, name: str | None, ticker: str | None) -> None:
    name_norm = _normalize_phrase(name)
    ticker_norm = _normalize_symbol(ticker)
    if not name_norm or not ticker_norm:
        return
    if len(name_norm) < 4:
        return
    index.setdefault(name_norm, set()).add(ticker_norm)


def _copy_target(target: dict[str, Any] | None) -> dict[str, Any]:
    payload = dict(target or {})
    extra_json = payload.get("extra_json")
    if isinstance(extra_json, dict):
        payload["extra_json"] = dict(extra_json)
    return payload


def _generic_ticker_target(symbol: str) -> dict[str, Any]:
    sym = _normalize_symbol(symbol)
    return {
        "target_key": f"ticker:{sym}",
        "target_kind": "ticker",
        "target_label": sym,
        "issuer_id": None,
        "company_id": None,
        "symbol": sym,
    }


def _company_target(company: Company) -> dict[str, Any]:
    symbol = _normalize_symbol(getattr(company, "ticker", None))
    slug = str(getattr(company, "slug", "") or "").strip() or f"company-{int(company.id)}"
    return {
        "target_key": f"company:{slug}",
        "target_kind": "company",
        "target_label": str(getattr(company, "name", None) or slug),
        "issuer_id": (int(company.issuer_id) if getattr(company, "issuer_id", None) is not None else None),
        "company_id": int(company.id),
        "symbol": symbol or None,
    }


def _issuer_target(issuer: Issuer, *, company_id: int | None = None, symbol: str | None = None) -> dict[str, Any]:
    slug = str(getattr(issuer, "slug", "") or "").strip() or f"issuer-{int(issuer.id)}"
    sym = _normalize_symbol(symbol)
    return {
        "target_key": f"issuer:{slug}",
        "target_kind": "issuer",
        "target_label": str(getattr(issuer, "display_name", None) or getattr(issuer, "legal_name", None) or slug),
        "issuer_id": int(issuer.id),
        "company_id": (int(company_id) if company_id is not None else None),
        "symbol": sym or None,
    }


def _ticker_target(
    *,
    symbol: str,
    issuer_id: int | None = None,
    company_id: int | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    sym = _normalize_symbol(symbol)
    return {
        "target_key": f"ticker:{sym}",
        "target_kind": "ticker",
        "target_label": str(label or sym).strip() or sym,
        "issuer_id": issuer_id,
        "company_id": company_id,
        "symbol": sym,
    }


def _unique_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for target in targets:
        target_key = str(target.get("target_key") or "").strip()
        if not target_key or target_key in seen:
            continue
        seen.add(target_key)
        out.append(_copy_target(target))
    return out


def _target_symbols(targets: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for target in targets:
        sym = _normalize_symbol(target.get("symbol"))
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(sym)
    return out


def _candidate_targets_payload(*, reason: str, targets: list[dict[str, Any]]) -> dict[str, Any] | None:
    payload_targets: list[dict[str, Any]] = []
    for target in targets:
        row = {
            "target_key": target.get("target_key"),
            "target_kind": target.get("target_kind"),
            "target_label": target.get("target_label"),
            "issuer_id": target.get("issuer_id"),
            "company_id": target.get("company_id"),
            "symbol": _normalize_symbol(target.get("symbol")) or None,
        }
        if isinstance(target.get("extra_json"), dict):
            row["extra_json"] = dict(target.get("extra_json") or {})
        payload_targets.append(row)
    if not payload_targets:
        return None
    return {"reason": str(reason or "").strip() or "candidate", "targets": payload_targets}


def _resolved_match(targets: list[dict[str, Any]], *, reason: str) -> DeterministicTickerMatch:
    unique_targets = _unique_targets(targets)
    symbols = _target_symbols(unique_targets)
    ticker = symbols[0] if len(symbols) == 1 else None
    return DeterministicTickerMatch(
        ticker=ticker,
        candidates=symbols,
        reason=reason,
        targets=unique_targets,
        candidate_targets=[],
        resolution_state="resolved",
    )


def _ambiguous_match(targets: list[dict[str, Any]], *, reason: str) -> DeterministicTickerMatch:
    unique_targets = _unique_targets(targets)
    return DeterministicTickerMatch(
        ticker=None,
        candidates=_target_symbols(unique_targets),
        reason=reason,
        targets=[],
        candidate_targets=unique_targets,
        resolution_state="pending",
    )


def _parse_rule_specs(raw_json: str | None) -> list[dict[str, Any]]:
    raw = str(raw_json or "").strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except Exception:
        return []
    if isinstance(parsed, dict):
        rows = []
        for key, value in parsed.items():
            if isinstance(value, dict):
                row = dict(value)
                row.setdefault("name", str(key))
                rows.append(row)
        return rows
    if isinstance(parsed, list):
        return [dict(row) for row in parsed if isinstance(row, dict)]
    return []


def _build_rule_targets(
    session: Session,
    *,
    raw_rules: str | None,
    rule_kind: str,
    symbol_targets: dict[str, dict[str, Any]],
    name_targets: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    rules: list[dict[str, Any]] = []
    for row in _parse_rule_specs(raw_rules):
        name = str(row.get("name") or row.get("topic") or row.get("label") or rule_kind).strip() or rule_kind
        terms = [
            str(term or "").strip()
            for key in ("terms", "keywords", "match_terms", "aliases", "patterns")
            for term in (row.get(key) or [])
            if str(term or "").strip()
        ]
        targets: list[dict[str, Any]] = []
        for sym in [str(v or "").strip() for key in ("tickers", "symbols") for v in (row.get(key) or [])]:
            symbol = _normalize_symbol(sym)
            if not symbol:
                continue
            targets.append(_copy_target(symbol_targets.get(symbol) or _generic_ticker_target(symbol)))
        for topic in [str(v or "").strip() for v in (row.get("topics") or [])]:
            if topic:
                targets.append(services.build_topic_target(topic=topic, label=topic))
        for target_row in row.get("targets") or []:
            if not isinstance(target_row, dict):
                continue
            target_kind = str(target_row.get("kind") or "").strip().lower()
            if target_kind in {"ticker", "symbol"}:
                symbol = _normalize_symbol(target_row.get("ticker") or target_row.get("symbol"))
                if symbol:
                    targets.append(_copy_target(symbol_targets.get(symbol) or _generic_ticker_target(symbol)))
                continue
            if target_kind == "topic":
                topic = str(target_row.get("topic") or target_row.get("label") or target_row.get("name") or "").strip()
                if topic:
                    targets.append(services.build_topic_target(topic=topic, label=str(target_row.get("label") or topic).strip() or topic))
                continue
            if target_kind in {"issuer", "company", "private_company"}:
                company_name = str(
                    target_row.get("company_name")
                    or target_row.get("issuer_name")
                    or target_row.get("name")
                    or target_row.get("label")
                    or ""
                ).strip()
                if not company_name:
                    continue
                name_norm = _normalize_phrase(company_name)
                mapped = [_copy_target(item) for item in name_targets.get(name_norm, [])]
                if mapped:
                    targets.extend(mapped)
                    continue
                targets.append(services.build_private_company_target(session, company_name=company_name))
        if not terms or not targets:
            continue
        rules.append({"name": name, "terms": sorted(set(terms)), "targets": _unique_targets(targets)})
    return rules


def build_ticker_name_matcher(session: Session) -> TickerNameMatcher:
    issuers = session.query(Issuer).all()
    companies = session.query(Company).all()
    identifiers = (
        session.query(IssuerIdentifier)
        .filter(IssuerIdentifier.id_type == "TICKER")
        .order_by(IssuerIdentifier.is_primary.desc(), IssuerIdentifier.id.asc())
        .all()
    )

    issuer_by_id = {int(row.id): row for row in issuers}
    company_by_issuer: dict[int, Company] = {}
    for company in companies:
        if getattr(company, "issuer_id", None) is None:
            continue
        company_by_issuer.setdefault(int(company.issuer_id), company)

    primary_ticker_by_issuer: dict[int, str] = {}
    symbol_targets: dict[str, dict[str, Any]] = {}
    for ident in identifiers:
        issuer_id = int(ident.issuer_id)
        symbol = _normalize_symbol(ident.id_value)
        if not symbol:
            continue
        primary_ticker_by_issuer.setdefault(issuer_id, symbol)
        issuer = issuer_by_id.get(issuer_id)
        company = company_by_issuer.get(issuer_id)
        symbol_targets[symbol] = _ticker_target(
            symbol=symbol,
            issuer_id=issuer_id,
            company_id=(int(company.id) if company is not None else None),
            label=(
                str(getattr(company, "name", None) or "")
                or str(getattr(issuer, "display_name", None) or "")
                or symbol
            ),
        )

    by_name: dict[str, list[dict[str, Any]]] = {}
    all_name_targets: list[tuple[str, dict[str, Any]]] = []

    def _register_name(name: str | None, target: dict[str, Any]) -> None:
        name_norm = _normalize_phrase(name)
        if not name_norm or len(name_norm) < 4:
            return
        target_list = by_name.setdefault(name_norm, [])
        target_key = str(target.get("target_key") or "").strip()
        if any(str(item.get("target_key") or "").strip() == target_key for item in target_list):
            return
        copied = _copy_target(target)
        target_list.append(copied)
        all_name_targets.append((name_norm, copied))

    for company in companies:
        issuer = issuer_by_id.get(int(company.issuer_id)) if getattr(company, "issuer_id", None) is not None else None
        company_ticker = _normalize_symbol(getattr(company, "ticker", None))
        primary_ticker = company_ticker or primary_ticker_by_issuer.get(int(company.issuer_id)) if getattr(company, "issuer_id", None) is not None else company_ticker
        if primary_ticker:
            target = _copy_target(symbol_targets.get(primary_ticker) or _ticker_target(
                symbol=primary_ticker,
                issuer_id=(int(company.issuer_id) if getattr(company, "issuer_id", None) is not None else None),
                company_id=int(company.id),
                label=str(getattr(company, "name", None) or primary_ticker),
            ))
        elif issuer is not None:
            target = _issuer_target(issuer, company_id=int(company.id))
        else:
            target = _company_target(company)
        _register_name(getattr(company, "name", None), target)

    for issuer in issuers:
        issuer_id = int(issuer.id)
        company = company_by_issuer.get(issuer_id)
        primary_ticker = primary_ticker_by_issuer.get(issuer_id)
        if primary_ticker:
            target = _copy_target(symbol_targets.get(primary_ticker) or _ticker_target(
                symbol=primary_ticker,
                issuer_id=issuer_id,
                company_id=(int(company.id) if company is not None else None),
                label=str(getattr(issuer, "display_name", None) or getattr(issuer, "legal_name", None) or primary_ticker),
            ))
        else:
            target = _issuer_target(issuer, company_id=(int(company.id) if company is not None else None))
        _register_name(getattr(issuer, "display_name", None), target)
        _register_name(getattr(issuer, "legal_name", None), target)

    by_first_token: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name_norm, targets in by_name.items():
        tokens = name_norm.split()
        if not tokens:
            continue
        first = tokens[0]
        for target in targets:
            by_first_token.setdefault(first, []).append((name_norm, _copy_target(target)))

    topic_rules = _build_rule_targets(
        session,
        raw_rules=getattr(settings, "discord_link_topic_rules_json", None),
        rule_kind="topic",
        symbol_targets=symbol_targets,
        name_targets=by_name,
    )
    macro_rules = _build_rule_targets(
        session,
        raw_rules=getattr(settings, "discord_link_macro_proxy_rules_json", None),
        rule_kind="macro",
        symbol_targets=symbol_targets,
        name_targets=by_name,
    )
    return TickerNameMatcher(
        by_first_token=by_first_token,
        symbol_targets={key: _copy_target(value) for key, value in symbol_targets.items()},
        all_name_targets=all_name_targets,
        topic_rules=topic_rules,
        macro_rules=macro_rules,
    )


def load_ticker_name_matcher() -> TickerNameMatcher:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        return build_ticker_name_matcher(session)


def get_scan_state(*, guild_id: int, channel_id: int) -> dict[str, Any] | None:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        row = (
            session.query(DiscordLinkScanState)
            .filter_by(guild_id=int(guild_id), channel_id=int(channel_id))
            .first()
        )
        if not row:
            return None
        return {
            "guild_id": int(row.guild_id),
            "channel_id": int(row.channel_id),
            "last_message_id": int(row.last_message_id) if row.last_message_id else None,
            "last_scanned_at": row.last_scanned_at,
            "last_error": row.last_error,
            "updated_at": row.updated_at,
        }


def upsert_scan_state(
    *,
    guild_id: int,
    channel_id: int,
    last_message_id: int | None | object = _UNSET,
    last_scanned_at: datetime | None | object = _UNSET,
    last_error: str | None | object = _UNSET,
) -> None:
    now = _now()
    with session_scope() as session:
        row = (
            session.query(DiscordLinkScanState)
            .filter_by(guild_id=int(guild_id), channel_id=int(channel_id))
            .first()
        )
        if not row:
            row = DiscordLinkScanState(
                guild_id=int(guild_id),
                channel_id=int(channel_id),
                last_message_id=None,
                last_scanned_at=None,
                last_error=None,
                updated_at=now,
            )
            session.add(row)
        if last_message_id is not _UNSET:
            row.last_message_id = int(last_message_id) if last_message_id is not None else None
        if last_scanned_at is not _UNSET:
            row.last_scanned_at = last_scanned_at
        if last_error is not _UNSET:
            row.last_error = (str(last_error).strip() if isinstance(last_error, str) and str(last_error).strip() else None)
        row.updated_at = now


def bootstrap_after_datetime(mode: str, *, now: datetime | None = None) -> datetime | None:
    mode_v = (mode or "now").strip().lower()
    ref = now or _now()
    if mode_v == "24h":
        return ref - timedelta(hours=24)
    if mode_v == "7d":
        return ref - timedelta(days=7)
    return None


def _auto_source_title(*, url: str, embed_title: str | None, excerpt: str | None) -> str:
    title = (embed_title or "").strip()
    if title:
        return title[:220]
    excerpt_v = (excerpt or "").strip()
    if excerpt_v:
        return excerpt_v[:220]
    parsed = urlparse(url)
    host = (parsed.netloc or "").strip() or "link"
    path = (parsed.path or "").strip()
    return (host + path)[:220]


def _discord_message_url(*, guild_id: int, channel_id: int | None, message_id: int | None) -> str | None:
    if not channel_id or not message_id:
        return None
    return f"https://discord.com/channels/{int(guild_id)}/{int(channel_id)}/{int(message_id)}"


def _auto_source_note(*, guild_id: int, channel_id: int | None, message_id: int | None, message_excerpt: str | None) -> str | None:
    parts: list[str] = []
    message_url = _discord_message_url(guild_id=guild_id, channel_id=channel_id, message_id=message_id)
    if message_url:
        parts.append(f"Discord origin: {message_url}")
    excerpt = str(message_excerpt or "").strip()
    if excerpt:
        parts.append(excerpt)
    return "\n\n".join(parts) if parts else None


def _target_resolution_method(reason: str) -> str:
    clean = str(reason or "").strip().lower() or "deterministic"
    return f"deterministic_{clean}"


def _request_key(*, guild_id: int, url_hash: str) -> str:
    return f"company-resolution:{int(guild_id)}:{str(url_hash or '').strip()}"


def _create_company_resolution_request(
    session: Session,
    *,
    guild_id: int,
    inbox_id: int,
    evidence: DiscordEvidenceItem,
    title: str,
    message_excerpt: str | None,
    reason: str,
    candidate_targets: dict[str, Any] | None,
) -> int:
    candidate_rows = candidate_targets.get("targets") if isinstance(candidate_targets, dict) else []
    labels = [
        str(row.get("target_label") or row.get("symbol") or row.get("target_key") or "").strip()
        for row in (candidate_rows or [])
        if isinstance(row, dict)
    ]
    summary_bits = [f"Resolve canonical company routing for Discord evidence item #{int(inbox_id)}."]
    if message_excerpt:
        summary_bits.append(str(message_excerpt).strip()[:400])
    if candidate_targets and candidate_targets.get("reason"):
        summary_bits.append(f"Deterministic reason: {candidate_targets.get('reason')}.")
    request = create_instrument_request(
        session,
        title=(str(title or "").strip() or f"Resolve company for evidence #{int(evidence.id)}")[:255],
        intent="company_resolution",
        summary=" ".join([bit for bit in summary_bits if bit])[:1200],
        target_entities=[label for label in labels if label][:8],
        desired_outputs=["company_match"],
        evidence_bundle=[
            {
                "title": str(evidence.title or title or evidence.url_canonical or evidence.url_raw or "Discord Evidence"),
                "url": str(evidence.url_canonical or evidence.url_raw or ""),
                "source": "discord_link",
                "evidence_id": int(evidence.id),
                "inbox_id": int(inbox_id),
            }
        ],
        provenance_summary=(
            f"Discord link intake unresolved for {evidence.url_canonical or evidence.url_raw}. "
            f"Reason={str(reason or 'none').strip() or 'none'}."
        )[:1200],
        evidence_tier="trusted_analyst",
        status=REQUEST_STATUS_REVIEW,
        priority=4,
        automation_source="discord_link_intake",
        source_kind="discord_link",
        draft_payload={
            "guild_id": int(guild_id),
            "inbox_id": int(inbox_id),
            "evidence_id": int(evidence.id),
            "url_hash": str(evidence.url_hash or ""),
            "url": str(evidence.url_canonical or evidence.url_raw or ""),
            "candidate_targets": dict(candidate_targets or {}),
        },
        linked_objects={
            "discord_inbox_ids": [int(inbox_id)],
            "discord_evidence_ids": [int(evidence.id)],
        },
        request_key=_request_key(guild_id=guild_id, url_hash=str(evidence.url_hash or "")),
    )
    session.flush()
    return int(request.id)


def _apply_match_to_inbox(
    session: Session,
    *,
    row: DiscordLinkInboxItem,
    guild_id: int,
    title: str,
    note: str | None,
    source_type: str,
    user_id: int,
    match: DeterministicTickerMatch,
    now: datetime,
) -> tuple[DiscordEvidenceItem, int | None]:
    candidate_payload = _candidate_targets_payload(reason=match.reason, targets=(match.candidate_targets or match.targets))
    desired_resolution_state = "resolved" if match.targets else "pending"
    evidence = services.ensure_evidence_item(
        session,
        guild_id=int(guild_id),
        url_raw=str(row.url_raw),
        title=title,
        note=note,
        sentiment=None,
        tags=None,
        source_type=source_type,
        user_id=int(user_id),
        discord_message_id=(int(row.message_id) if row.message_id else None),
        discord_channel_id=(int(row.channel_id) if row.channel_id else None),
        resolution_state=desired_resolution_state,
        candidate_targets_json=candidate_payload,
        now=now,
    )
    request_id: int | None = None
    if match.targets:
        for index, target in enumerate(match.targets):
            services.attach_evidence_target(
                session,
                evidence=evidence,
                target=target,
                resolution_method=_target_resolution_method(match.reason),
                is_primary=(index == 0),
                confidence=(1.0 if match.reason in {"ticker", "company_name"} else 0.9),
                now=now,
            )
        row.status = INBOX_STATUS_DETERMINISTIC_INGESTED
        row.deterministic_ticker = match.ticker
        row.queue_id = None
        row.resolution_state = "resolved"
        row.resolution_candidates_json = None
        row.resolution_request_id = None
    else:
        request_id = _create_company_resolution_request(
            session,
            guild_id=int(guild_id),
            inbox_id=int(row.id),
            evidence=evidence,
            title=title,
            message_excerpt=row.message_excerpt,
            reason=match.reason,
            candidate_targets=candidate_payload,
        )
        evidence.resolution_state = "request_created"
        evidence.candidate_targets_json = candidate_payload
        evidence.updated_at = now
        row.status = INBOX_STATUS_RESOLUTION_REQUESTED
        row.deterministic_ticker = None
        row.queue_id = None
        row.resolution_state = "request_created"
        row.resolution_candidates_json = candidate_payload
        row.resolution_request_id = int(request_id)
    row.evidence_id = int(evidence.id)
    row.source_id = int(evidence.id)
    row.updated_at = now
    row.agentic_decision_json = {
        "deterministic_candidates": list(match.candidates or []),
        "candidate_targets": dict(candidate_payload or {}),
        "reason": match.reason,
        "resolution_state": str(row.resolution_state or ""),
    }
    session.flush()
    return evidence, request_id


def intake_link_candidate(
    *,
    guild_id: int,
    channel_id: int,
    message_id: int,
    author_user_id: int | None,
    message_created_at: datetime | None,
    url_raw: str,
    message_excerpt: str | None,
    embed_title: str | None,
    match_text: str | None,
    matcher: TickerNameMatcher,
    user_id: int = 0,
) -> IntakeResult:
    canonical = canonicalize_url(url_raw)
    if not canonical:
        return IntakeResult(
            status=INBOX_STATUS_IGNORED,
            inbox_id=None,
            source_id=None,
            ticker=None,
            created=False,
            url_canonical=None,
        )
    now = _now()
    digest = hash_url(canonical)
    match = matcher.resolve(match_text)
    with session_scope() as session:
        existing = (
            session.query(DiscordLinkInboxItem)
            .filter_by(guild_id=int(guild_id), url_hash=str(digest))
            .first()
        )
        if existing:
            existing.seen_count = int(existing.seen_count or 0) + 1
            existing.updated_at = now
            if not existing.message_excerpt and message_excerpt:
                existing.message_excerpt = str(message_excerpt)[:1000]
            if not existing.embed_title and embed_title:
                existing.embed_title = str(embed_title)[:512]
            if str(existing.resolution_state or "").strip().lower() != "resolved":
                _apply_match_to_inbox(
                    session,
                    row=existing,
                    guild_id=int(guild_id),
                    title=_auto_source_title(
                        url=str(canonical),
                        embed_title=(embed_title or existing.embed_title),
                        excerpt=(message_excerpt or existing.message_excerpt),
                    ),
                    note=_auto_source_note(
                        guild_id=int(guild_id),
                        channel_id=(int(existing.channel_id) if existing.channel_id else int(channel_id)),
                        message_id=(int(existing.message_id) if existing.message_id else int(message_id)),
                        message_excerpt=(message_excerpt or existing.message_excerpt),
                    ),
                    source_type="research",
                    user_id=int(user_id),
                    match=match,
                    now=now,
                )
            try:
                doc = maybe_ingest_arxiv_document_for_inbox(
                    session,
                    row=existing,
                    title_hint=(embed_title or existing.embed_title),
                    now=now,
                )
                if doc is not None:
                    _record_arxiv_ingest_result(existing, document=doc)
            except Exception as exc:
                _record_arxiv_ingest_result(existing, error=exc)
            return IntakeResult(
                status=str(existing.status),
                inbox_id=int(existing.id),
                source_id=(int(existing.evidence_id or existing.source_id) if (existing.evidence_id or existing.source_id) else None),
                ticker=(str(existing.deterministic_ticker) if existing.deterministic_ticker else None),
                created=False,
                url_canonical=str(existing.url_canonical),
            )

        row = DiscordLinkInboxItem(
            guild_id=int(guild_id),
            channel_id=int(channel_id),
            message_id=int(message_id),
            author_user_id=(int(author_user_id) if author_user_id is not None else None),
            message_created_at=message_created_at,
            url_raw=str(url_raw).strip()[:2048],
            url_canonical=str(canonical),
            url_hash=str(digest),
            message_excerpt=(str(message_excerpt)[:1000] if message_excerpt else None),
            embed_title=(str(embed_title)[:512] if embed_title else None),
            deterministic_ticker=None,
            status=INBOX_STATUS_COLLECTED,
            agentic_decision_json=None,
            evidence_id=None,
            resolution_state="pending",
            resolution_candidates_json=None,
            resolution_request_id=None,
            source_id=None,
            queue_id=None,
            seen_count=1,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.flush()
        evidence, _request_id = _apply_match_to_inbox(
            session,
            row=row,
            guild_id=int(guild_id),
            title=_auto_source_title(url=str(canonical), embed_title=embed_title, excerpt=message_excerpt),
            note=_auto_source_note(
                guild_id=int(guild_id),
                channel_id=(int(channel_id) if channel_id else None),
                message_id=(int(message_id) if message_id else None),
                message_excerpt=message_excerpt,
            ),
            source_type="research",
            user_id=int(user_id),
            match=match,
            now=now,
        )
        try:
            doc = maybe_ingest_arxiv_document_for_inbox(
                session,
                row=row,
                title_hint=embed_title,
                now=now,
            )
            if doc is not None:
                _record_arxiv_ingest_result(row, document=doc)
        except Exception as exc:
            _record_arxiv_ingest_result(row, error=exc)
        return IntakeResult(
            status=str(row.status),
            inbox_id=int(row.id),
            source_id=int(evidence.id),
            ticker=(str(match.ticker) if match.ticker else None),
            created=True,
            url_canonical=str(canonical),
        )


def reconcile_collected_links(
    *,
    guild_id: int,
    limit: int = 200,
) -> dict[str, int]:
    limit_v = max(1, min(int(limit or 200), 500))
    now = _now()
    with session_scope() as session:
        matcher = build_ticker_name_matcher(session)
        rows = (
            session.query(DiscordLinkInboxItem)
            .filter_by(guild_id=int(guild_id), status=INBOX_STATUS_COLLECTED)
            .order_by(DiscordLinkInboxItem.created_at.asc(), DiscordLinkInboxItem.id.asc())
            .limit(limit_v)
            .all()
        )
        reviewed = 0
        ingested = 0
        requested = 0
        for row in rows:
            reviewed += 1
            match_text = " ".join(
                [
                    str(row.embed_title or ""),
                    str(row.message_excerpt or ""),
                    str(row.url_canonical or ""),
                ]
            ).strip()
            match = matcher.resolve(match_text)
            _apply_match_to_inbox(
                session,
                row=row,
                guild_id=int(guild_id),
                title=_auto_source_title(url=str(row.url_canonical), embed_title=row.embed_title, excerpt=row.message_excerpt),
                note=_auto_source_note(
                    guild_id=int(guild_id),
                    channel_id=(int(row.channel_id) if row.channel_id else None),
                    message_id=(int(row.message_id) if row.message_id else None),
                    message_excerpt=row.message_excerpt,
                ),
                source_type="research",
                user_id=0,
                match=match,
                now=now,
            )
            if row.status == INBOX_STATUS_DETERMINISTIC_INGESTED:
                ingested += 1
            elif row.status == INBOX_STATUS_RESOLUTION_REQUESTED:
                requested += 1
        return {
            "reviewed": int(reviewed),
            "deterministic_ingested": int(ingested),
            "resolution_requested": int(requested),
        }


def list_inbox_items(
    *,
    guild_id: int,
    status: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    limit_v = max(1, min(int(limit or 50), 500))
    status_v = (status or "").strip().lower() or None
    SessionLocal = session_factory()
    with SessionLocal() as session:
        q = session.query(DiscordLinkInboxItem).filter_by(guild_id=int(guild_id))
        if status_v:
            q = q.filter(DiscordLinkInboxItem.status == status_v)
        rows = (
            q.order_by(DiscordLinkInboxItem.updated_at.desc(), DiscordLinkInboxItem.id.desc())
            .limit(limit_v)
            .all()
        )
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": int(row.id),
                    "guild_id": int(row.guild_id),
                    "channel_id": int(row.channel_id),
                    "message_id": int(row.message_id),
                    "author_user_id": int(row.author_user_id) if row.author_user_id is not None else None,
                    "message_created_at": row.message_created_at.isoformat() if row.message_created_at else None,
                    "url_raw": row.url_raw,
                    "url_canonical": row.url_canonical,
                    "url_hash": row.url_hash,
                    "message_excerpt": row.message_excerpt,
                    "embed_title": row.embed_title,
                    "deterministic_ticker": row.deterministic_ticker,
                    "status": row.status,
                    "evidence_id": int(row.evidence_id) if row.evidence_id else None,
                    "resolution_state": row.resolution_state,
                    "candidate_targets": dict(row.resolution_candidates_json or {}),
                    "resolution_request_id": int(row.resolution_request_id) if row.resolution_request_id else None,
                    "source_id": int(row.source_id) if row.source_id else None,
                    "queue_id": int(row.queue_id) if row.queue_id else None,
                    "seen_count": int(row.seen_count or 0),
                    "created_at": row.created_at.isoformat() if row.created_at else None,
                    "updated_at": row.updated_at.isoformat() if row.updated_at else None,
                }
            )
        return out


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _decision_action_for_confidence(
    *,
    action: str | None,
    confidence: float | None,
    confidence_min: float,
    queue_threshold: float,
) -> str:
    action_v = (action or "auto").strip().lower()
    if action_v in {"ingest", "queue", "ignore"}:
        return action_v
    conf = float(confidence or 0.0)
    if conf >= float(confidence_min):
        return "ingest"
    if conf >= float(queue_threshold):
        return "queue"
    return "ignore"


def _decision_targets(
    session: Session,
    *,
    row: DiscordLinkInboxItem,
    decision: dict[str, Any],
) -> list[dict[str, Any]]:
    targets: list[dict[str, Any]] = []
    explicit_targets = decision.get("targets")
    if isinstance(explicit_targets, list):
        for item in explicit_targets:
            if not isinstance(item, dict):
                continue
            kind = str(item.get("kind") or item.get("target_kind") or "").strip().lower()
            if kind in {"ticker", "symbol"}:
                ticker = _normalize_symbol(item.get("ticker") or item.get("symbol"))
                if ticker:
                    targets.append(services.build_ticker_target(session, ticker=ticker))
                continue
            if kind == "topic":
                topic = str(item.get("topic") or item.get("label") or item.get("name") or "").strip()
                if topic:
                    targets.append(services.build_topic_target(topic=topic, label=str(item.get("label") or topic).strip() or topic))
                continue
            if kind in {"issuer", "company", "private_company"}:
                company_name = str(
                    item.get("company_name")
                    or item.get("issuer_name")
                    or item.get("name")
                    or item.get("label")
                    or ""
                ).strip()
                if company_name:
                    targets.append(services.build_private_company_target(session, company_name=company_name))
    ticker = _normalize_symbol(decision.get("ticker")) or _normalize_symbol(row.deterministic_ticker)
    if ticker and not targets:
        targets.append(services.build_ticker_target(session, ticker=ticker))
    company_name = str(decision.get("company_name") or decision.get("issuer_name") or "").strip()
    if company_name and not targets:
        targets.append(services.build_private_company_target(session, company_name=company_name))
    return _unique_targets(targets)


def apply_agentic_decisions(
    *,
    guild_id: int,
    decisions: list[dict[str, Any]],
    created_by_user_id: int = 0,
    confidence_min: float | None = None,
    queue_threshold: float | None = None,
) -> dict[str, Any]:
    conf_min = (
        float(confidence_min)
        if confidence_min is not None
        else float(getattr(settings, "discord_link_agentic_confidence_min", 0.75) or 0.75)
    )
    queue_th = (
        float(queue_threshold)
        if queue_threshold is not None
        else float(getattr(settings, "discord_link_agentic_queue_threshold", 0.50) or 0.50)
    )
    queue_th = min(queue_th, conf_min)

    results: list[dict[str, Any]] = []
    now = _now()
    with session_scope() as session:
        for idx, decision in enumerate(decisions or []):
            if not isinstance(decision, dict):
                results.append({"index": idx, "ok": False, "error": "decision must be an object"})
                continue

            inbox_id = decision.get("inbox_id")
            url_hash = str(decision.get("url_hash") or "").strip()
            row = None
            if inbox_id is not None and str(inbox_id).isdigit():
                row = (
                    session.query(DiscordLinkInboxItem)
                    .filter_by(guild_id=int(guild_id), id=int(inbox_id))
                    .first()
                )
            elif url_hash:
                row = (
                    session.query(DiscordLinkInboxItem)
                    .filter_by(guild_id=int(guild_id), url_hash=url_hash)
                    .first()
                )
            if not row:
                results.append({"index": idx, "ok": False, "error": "inbox item not found"})
                continue

            confidence = _to_float(decision.get("confidence"))
            action = _decision_action_for_confidence(
                action=str(decision.get("action") or "auto"),
                confidence=confidence,
                confidence_min=conf_min,
                queue_threshold=queue_th,
            )
            source_type = str(decision.get("source_type") or "research").strip().lower() or "research"
            title = (
                str(decision.get("title") or "").strip()
                or _auto_source_title(url=str(row.url_canonical), embed_title=row.embed_title, excerpt=row.message_excerpt)
            )
            note = str(decision.get("note") or "").strip() or _auto_source_note(
                guild_id=int(guild_id),
                channel_id=(int(row.channel_id) if row.channel_id else None),
                message_id=(int(row.message_id) if row.message_id else None),
                message_excerpt=row.message_excerpt,
            )
            targets = _decision_targets(session, row=row, decision=decision)
            candidate_payload = _candidate_targets_payload(
                reason=str(decision.get("reason") or "").strip() or "agentic_decision",
                targets=targets,
            )
            ticker = _target_symbols(targets)[0] if len(_target_symbols(targets)) == 1 else None

            if action == "ingest":
                synthetic_match = DeterministicTickerMatch(
                    ticker=ticker,
                    candidates=_target_symbols(targets),
                    reason="agentic_decision",
                    targets=targets,
                    candidate_targets=[],
                    resolution_state=("resolved" if targets else "pending"),
                )
                evidence, request_id = _apply_match_to_inbox(
                    session,
                    row=row,
                    guild_id=int(guild_id),
                    title=title,
                    note=note,
                    source_type=source_type,
                    user_id=int(created_by_user_id),
                    match=synthetic_match,
                    now=now,
                )
                if targets:
                    row.status = INBOX_STATUS_AGENTIC_INGESTED
                    row.resolution_state = "resolved"
                row.updated_at = now
                row.agentic_decision_json = dict(decision)
                results.append(
                    {
                        "index": idx,
                        "ok": True,
                        "id": int(row.id),
                        "action": "ingest",
                        "status": row.status,
                        "source_id": int(evidence.id),
                        "evidence_id": int(evidence.id),
                        "resolution_request_id": int(request_id) if request_id is not None else None,
                    }
                )
                continue

            if action == "queue":
                evidence = services.ensure_evidence_item(
                    session,
                    guild_id=int(guild_id),
                    url_raw=str(row.url_raw),
                    title=title,
                    note=note,
                    sentiment=None,
                    tags=None,
                    source_type=source_type,
                    user_id=int(created_by_user_id),
                    discord_message_id=(int(row.message_id) if row.message_id else None),
                    discord_channel_id=(int(row.channel_id) if row.channel_id else None),
                    resolution_state=("pending" if targets else "request_created"),
                    candidate_targets_json=candidate_payload,
                    now=now,
                )
                row.evidence_id = int(evidence.id)
                row.source_id = int(evidence.id)
                if not targets:
                    request_id = _create_company_resolution_request(
                        session,
                        guild_id=int(guild_id),
                        inbox_id=int(row.id),
                        evidence=evidence,
                        title=title,
                        message_excerpt=row.message_excerpt,
                        reason=str(decision.get("reason") or "").strip() or "agentic_queue",
                        candidate_targets=candidate_payload,
                    )
                    evidence.resolution_state = "request_created"
                    evidence.updated_at = now
                    row.status = INBOX_STATUS_RESOLUTION_REQUESTED
                    row.queue_id = None
                    row.deterministic_ticker = None
                    row.updated_at = now
                    row.resolution_state = "request_created"
                    row.resolution_candidates_json = candidate_payload
                    row.resolution_request_id = int(request_id)
                    row.agentic_decision_json = dict(decision)
                    results.append(
                        {
                            "index": idx,
                            "ok": True,
                            "id": int(row.id),
                            "action": "request",
                            "status": row.status,
                            "evidence_id": int(evidence.id),
                            "resolution_request_id": int(request_id),
                        }
                    )
                    continue
                payload = {
                    "ticker": ticker,
                    "url": str(row.url_canonical),
                    "title": title,
                    "note": note,
                    "type": source_type,
                    "reason": str(decision.get("reason") or "").strip() or None,
                    "confidence": confidence,
                    "evidence_id": int(evidence.id),
                    "candidate_targets": dict(candidate_payload or {}),
                }
                queue_row = DiscordQueueItem(
                    guild_id=int(guild_id),
                    kind="source_add",
                    status="pending",
                    payload=payload,
                    created_at=now,
                    created_by_user_id=int(created_by_user_id),
                    decided_at=None,
                    decided_by_user_id=None,
                    decision_note=None,
                )
                session.add(queue_row)
                session.flush()
                row.status = INBOX_STATUS_QUEUED
                row.queue_id = int(queue_row.id)
                row.deterministic_ticker = ticker
                row.updated_at = now
                row.resolution_state = "pending"
                row.resolution_candidates_json = candidate_payload
                row.agentic_decision_json = dict(decision)
                results.append(
                    {
                        "index": idx,
                        "ok": True,
                        "id": int(row.id),
                        "action": "queue",
                        "status": row.status,
                        "evidence_id": int(evidence.id),
                        "queue_id": int(queue_row.id),
                    }
                )
                continue

            row.status = INBOX_STATUS_IGNORED
            row.updated_at = now
            row.resolution_state = str(row.resolution_state or "ignored")
            row.agentic_decision_json = dict(decision)
            results.append(
                {
                    "index": idx,
                    "ok": True,
                    "id": int(row.id),
                    "action": "ignore",
                    "status": row.status,
                }
            )

    return {
        "count": len(results),
        "ok_count": sum(1 for row in results if bool(row.get("ok"))),
        "error_count": sum(1 for row in results if not bool(row.get("ok"))),
        "results": results,
    }


def run_agentic_ingest(
    *,
    guild_id: int,
    limit: int = 20,
    dry_run: bool = False,
) -> dict[str, Any]:
    from thoth_terminal.agent.local_llm import ChatMessage, chat_json

    endpoint = local_model_endpoint() or ""
    model = local_model_name() or ""
    if not endpoint or not model:
        raise ValueError("Agentic ingest requires THOTH_AGENT_LLM_ENDPOINT/THOTH_LOCAL_LLM_ENDPOINT and THOTH_AGENT_LLM_MODEL.")

    candidates = list_inbox_items(guild_id=int(guild_id), status=INBOX_STATUS_COLLECTED, limit=int(limit))
    decisions: list[dict[str, Any]] = []
    for item in candidates:
        prompt = (
            "You classify Discord links into source-ingest decisions.\n"
            "Return strict JSON with keys: action, ticker, source_type, confidence, reason, title, note.\n"
            "action must be one of: ingest, queue, ignore.\n"
            "confidence must be 0.0-1.0.\n"
            f"URL: {item.get('url_canonical')}\n"
            f"EmbedTitle: {item.get('embed_title')}\n"
            f"Excerpt: {item.get('message_excerpt')}\n"
            f"DeterministicTicker: {item.get('deterministic_ticker')}\n"
            f"ResolutionState: {item.get('resolution_state')}\n"
            f"CandidateTargets: {json.dumps(item.get('candidate_targets') or {}, ensure_ascii=True)}\n"
        )
        messages = [
            ChatMessage(role="system", content="You are a careful financial research triage assistant."),
            ChatMessage(role="user", content=prompt),
        ]
        try:
            payload = chat_json(
                endpoint=endpoint,
                model=model,
                messages=messages,
                timeout_s=float(getattr(settings, "agent_llm_timeout_s", 60) or 60),
                max_input_chars=int(getattr(settings, "agent_llm_max_input_chars", 24000) or 24000),
            )
        except Exception as exc:
            payload = {
                "action": "ignore",
                "confidence": 0.0,
                "reason": f"llm_error:{type(exc).__name__}",
            }
        decision = dict(payload) if isinstance(payload, dict) else {}
        decision["inbox_id"] = int(item["id"])
        if not decision.get("ticker") and item.get("deterministic_ticker"):
            decision["ticker"] = item.get("deterministic_ticker")
        decisions.append(decision)

    if dry_run:
        return {"count": len(decisions), "decisions": decisions}
    return apply_agentic_decisions(
        guild_id=int(guild_id),
        decisions=decisions,
        created_by_user_id=0,
    )


def inbox_stats(*, guild_id: int | None = None) -> dict[str, Any]:
    now = _now()
    SessionLocal = session_factory()
    with SessionLocal() as session:
        q = session.query(DiscordLinkInboxItem)
        if guild_id is not None:
            q = q.filter(DiscordLinkInboxItem.guild_id == int(guild_id))
        rows = (
            q.with_entities(DiscordLinkInboxItem.status, func.count(DiscordLinkInboxItem.id))
            .group_by(DiscordLinkInboxItem.status)
            .all()
        )
        counts = {str(status): int(count or 0) for status, count in rows}
        for status in INBOX_STATUSES:
            counts.setdefault(status, 0)

        scan_q = session.query(DiscordLinkScanState)
        if guild_id is not None:
            scan_q = scan_q.filter(DiscordLinkScanState.guild_id == int(guild_id))
        latest_scan = (
            scan_q.order_by(DiscordLinkScanState.last_scanned_at.desc().nullslast(), DiscordLinkScanState.updated_at.desc())
            .first()
        )
        latest_error = (
            scan_q.filter(DiscordLinkScanState.last_error.isnot(None))
            .order_by(DiscordLinkScanState.updated_at.desc())
            .first()
        )

        q24 = session.query(DiscordLinkInboxItem)
        if guild_id is not None:
            q24 = q24.filter(DiscordLinkInboxItem.guild_id == int(guild_id))
        deterministic_ingested_24h = (
            q24.filter(DiscordLinkInboxItem.status == INBOX_STATUS_DETERMINISTIC_INGESTED)
            .filter(DiscordLinkInboxItem.updated_at >= (now - timedelta(hours=24)))
            .count()
        )
        queued_24h = (
            q24.filter(DiscordLinkInboxItem.status == INBOX_STATUS_QUEUED)
            .filter(DiscordLinkInboxItem.updated_at >= (now - timedelta(hours=24)))
            .count()
        )

        return {
            "collector_enabled": bool(getattr(settings, "discord_link_collect_enabled", True)),
            "collect_interval_minutes": int(getattr(settings, "discord_link_collect_interval_minutes", 10) or 10),
            "scan_channel_id": int(getattr(settings, "discord_link_scan_channel_id", 0) or getattr(settings, "discord_channel_id", 0) or 0),
            "inbox_counts": counts,
            "last_scan_at": latest_scan.last_scanned_at.isoformat() if latest_scan and latest_scan.last_scanned_at else None,
            "last_scan_error": latest_error.last_error if latest_error else None,
            "deterministic_ingested_24h": int(deterministic_ingested_24h),
            "queued_24h": int(queued_24h),
        }


def inbox_total_count(*, guild_id: int) -> int:
    SessionLocal = session_factory()
    with SessionLocal() as session:
        return int(
            session.query(func.count(DiscordLinkInboxItem.id))
            .filter(DiscordLinkInboxItem.guild_id == int(guild_id))
            .scalar()
            or 0
        )
