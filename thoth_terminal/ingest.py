from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dt_time, timezone, timedelta
import logging
import re
from typing import Any
from urllib.parse import urlparse, urlunparse

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, selectinload
import requests

from thoth_terminal.config import settings
from thoth_terminal.browser_ingestion_policy import browser_circuit_backoff_seconds
from thoth_terminal.connectors import fetch_items_result
from thoth_terminal.db import utcnow
from thoth_terminal.document_media import build_tweet_media_manifest, extract_candidate_image_urls
from thoth_terminal.models import (
    Attachment,
    Company,
    CompanyDocument,
    Document,
    DocumentEntity,
    DocumentEvent,
    DocumentMention,
    Domain,
    Entity,
    EntityAlias,
    EventType,
    IngestionRun,
    NotificationRule,
    Source,
    SourceState,
    Tag,
)
from thoth_terminal.net import DEFAULT_HEADERS, FetchError, fetch
from thoth_terminal.object_store import guess_extension, put_bytes
from thoth_terminal.text import (
    LanguageGuess,
    clean_whitespace,
    detect_language,
    parse_datetime_iso,
    sha256_text,
)
from thoth_terminal.translate import translate_document
from thoth_terminal.fx import ensure_fx_rates, convert_to_base, get_latest_rates
from thoth_terminal.institutional_service import sync_document_to_institutional
from thoth_terminal.investor_data_service import sync_investor_document
from thoth_terminal.ipo_calendar_service import sync_ipo_document
from thoth_terminal.issuer_identity import enrich_metadata_with_identity
from thoth_terminal.money import extract_money_mentions
from thoth_terminal.structured_data_service import sync_earnings_document_facts
from thoth_terminal.trade import upsert_trade_series_from_stats
from thoth_terminal.monthly_revenue_service import upsert_monthly_revenue_from_stats
from thoth_terminal.future_electronics_data_service import upsert_future_electronics_products_from_stats
from thoth_terminal.url_utils import clean_url

logger = logging.getLogger("thoth.ingest")

try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None


@dataclass(frozen=True)
class IngestResult:
    seen: int
    new: int
    updated: int
    status: str = "success"
    error: str | None = None
    run_id: int | None = None


def _rule_matches_doc(session: Session, rule: NotificationRule, doc: Document) -> bool:
    if not rule.enabled:
        return False
    if rule.source_ids and doc.source_id not in set(rule.source_ids or []):
        return False
    if rule.domain_slugs:
        doc_domains = {d.slug for d in doc.domains or []}
        if not doc_domains.intersection(set(rule.domain_slugs or [])):
            return False
    if rule.tag_slugs:
        doc_tags = {t.slug for t in doc.tags or []}
        if not doc_tags.intersection(set(rule.tag_slugs or [])):
            return False
    if rule.entity_ids:
        entity_ids = set(rule.entity_ids or [])
        if not entity_ids:
            return False
        has_entity = (
            session.query(DocumentEntity)
            .filter(DocumentEntity.document_id == doc.id)
            .filter(DocumentEntity.entity_id.in_(list(entity_ids)))
            .first()
            is not None
        )
        if not has_entity:
            return False
    if rule.keyword_regex:
        hay = f"{doc.title or ''}\n{doc.normalized_text or ''}"
        try:
            if not re.search(rule.keyword_regex, hay, flags=re.IGNORECASE):
                return False
        except Exception:
            return False
    return True


def _parse_hhmm(value: str | None) -> dt_time | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return dt_time(hour=hour, minute=minute)


def _parse_tz(tz_raw: str | None):
    if not tz_raw:
        return timezone.utc
    name = tz_raw.strip()
    if not name:
        return timezone.utc
    if name.upper() == "UTC":
        return timezone.utc
    if ZoneInfo:
        try:
            return ZoneInfo(name)
        except Exception:
            pass
    try:
        if name[0] in {"+", "-"} and ":" in name:
            sign = 1 if name[0] == "+" else -1
            hours, minutes = name[1:].split(":", 1)
            offset = sign * (int(hours) * 60 + int(minutes))
            return timezone.utc if offset == 0 else timezone(timedelta(minutes=offset))
    except Exception:
        return timezone.utc
    return timezone.utc


def _rule_quiet_now(rule: NotificationRule, now_utc: datetime | None = None) -> bool:
    start = _parse_hhmm(rule.quiet_start)
    end = _parse_hhmm(rule.quiet_end)
    if not start or not end:
        return False
    now = now_utc or utcnow()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    tz = _parse_tz(rule.quiet_tz)
    local_now = now.astimezone(tz)
    cur = local_now.time()
    if start == end:
        return True
    if start < end:
        return start <= cur < end
    return cur >= start or cur < end


def _evaluate_notification_rules(
    session: Session, *, docs: list[Document]
) -> dict[str, object]:
    rules = session.query(NotificationRule).filter(NotificationRule.enabled.is_(True)).all()
    if not rules:
        return {"toast": True, "sound": False, "rules": []}

    any_toast = False
    any_sound = False
    rule_hits: dict[int, dict[str, object]] = {}
    quiet_rules: set[int] = set()

    for doc in docs:
        for rule in rules:
            if not _rule_matches_doc(session, rule, doc):
                continue
            quiet_now = _rule_quiet_now(rule)
            if quiet_now:
                quiet_rules.add(rule.id)
            hit = rule_hits.get(rule.id)
            if not hit:
                hit = {"id": rule.id, "count": 0, "quiet": quiet_now}
                rule_hits[rule.id] = hit
            hit["count"] = int(hit.get("count", 0)) + 1
            if quiet_now:
                continue
            action = (rule.action or "toast").strip().lower()
            if action in {"toast", "toast+sound"}:
                any_toast = True
            if action in {"sound", "toast+sound"}:
                any_sound = True

    suppressed = bool(rule_hits) and not any_toast and not any_sound
    return {
        "toast": any_toast,
        "sound": any_sound,
        "rules": list(rule_hits.values()),
        "quiet_rules": sorted(list(quiet_rules)),
        "suppressed": suppressed,
    }


def _is_source_enabled(source: Source) -> tuple[bool, str | None]:
    cfg = source.config or {}
    enabled = cfg.get("enabled")
    if enabled is True:
        return (True, None)
    if enabled is False:
        return (False, cfg.get("disabled_reason") or "disabled")
    if cfg.get("disabled_reason"):
        return (False, str(cfg.get("disabled_reason")))
    return (True, None)


def _apply_defaults(session: Session, doc: Document, *, cfg: dict[str, Any]) -> None:
    for slug in cfg.get("default_domains") or []:
        domain = session.query(Domain).filter_by(slug=slug).first()
        if domain and domain not in doc.domains:
            doc.domains.append(domain)
    for slug in cfg.get("default_tags") or []:
        tag = session.query(Tag).filter_by(slug=slug).first()
        if tag and tag not in doc.tags:
            doc.tags.append(tag)
    for slug in cfg.get("default_event_types") or []:
        et = session.query(EventType).filter_by(slug=slug).first()
        if et and et not in doc.event_types:
            doc.event_types.append(et)


def _apply_company_link(
    session: Session,
    doc: Document,
    *,
    company_slug: str | None,
    doc_type: str | None,
    label: str | None = None,
    defer_entity_link: bool = False,
) -> None:
    if not company_slug:
        return
    company = session.query(Company).filter_by(slug=company_slug).first()
    if not company:
        return
    company_doc_exists = False
    for pending in session.new:
        if isinstance(pending, CompanyDocument):
            if (
                pending.company_id == company.id
                and pending.document_id == doc.id
                and pending.doc_type == doc_type
            ):
                company_doc_exists = True
                break
    exists = (
        session.query(CompanyDocument)
        .filter_by(company_id=company.id, document_id=doc.id, doc_type=doc_type)
        .first()
    )
    if not company_doc_exists and not exists:
        session.add(
            CompanyDocument(
                company_id=company.id,
                document_id=doc.id,
                doc_type=doc_type,
                label=label,
                created_at=utcnow(),
            )
        )
    if not defer_entity_link:
        _ensure_company_entity_link(session, doc, company)


def _ensure_company_entity_link(session: Session, doc: Document, company: Company) -> None:
    canonical = str(company.ticker or "").strip().upper() or str(company.name or "").strip()
    if not canonical:
        return
    entity = session.query(Entity).filter(func.lower(Entity.canonical_name) == canonical.lower()).first()
    if entity is None:
        entity = Entity(canonical_name=canonical, kind="company", description=None)
        session.add(entity)
        session.flush()

    aliases = [company.name, company.ticker]
    existing_aliases = {
        str(alias or "").strip().casefold()
        for (alias,) in session.query(EntityAlias.alias).filter_by(entity_id=int(entity.id)).all()
        if str(alias or "").strip()
    }
    existing_aliases.update(
        str(pending.alias or "").strip().casefold()
        for pending in session.new
        if isinstance(pending, EntityAlias) and int(pending.entity_id or 0) == int(entity.id)
    )
    for alias in aliases:
        alias_text = str(alias or "").strip()
        if not alias_text or alias_text.casefold() == str(entity.canonical_name or "").casefold():
            continue
        if alias_text.casefold() in existing_aliases:
            continue
        session.add(EntityAlias(entity_id=int(entity.id), alias=alias_text, language=None))
        existing_aliases.add(alias_text.casefold())

    existing_entity_ids = {
        int(entity_id)
        for (entity_id,) in session.query(DocumentEntity.entity_id).filter_by(document_id=int(doc.id)).all()
        if entity_id is not None
    }
    existing_entity_ids.update(
        int(link.entity_id)
        for link in session.new
        if isinstance(link, DocumentEntity)
        and int(getattr(link, "document_id", 0) or 0) == int(doc.id)
        and getattr(link, "entity_id", None) is not None
    )
    if int(entity.id) in existing_entity_ids:
        return
    session.add(
        DocumentEntity(
            document_id=int(doc.id),
            entity_id=int(entity.id),
            method="company_link",
            confidence=0.95,
        )
    )


def _sync_company_structured_outputs(
    session: Session,
    doc: Document,
    *,
    source: Source,
    company_slug: str | None,
    doc_type: str | None,
    ingestion_run_id: int | None = None,
) -> None:
    slug = str(company_slug or "").strip()
    if not slug:
        return
    company = session.query(Company).filter_by(slug=slug).first()
    if not company:
        return
    try:
        sync_earnings_document_facts(
            session,
            doc=doc,
            company=company,
            doc_type=doc_type,
            source=source,
            ingestion_run_id=ingestion_run_id,
        )
    except Exception as exc:
        if _is_retriable_ingest_transaction_error(exc) or not getattr(session, "is_active", True):
            raise
        return


def _sync_investor_outputs(
    session: Session,
    doc: Document,
    *,
    source: Source,
    ingestion_run_id: int | None = None,
) -> None:
    try:
        sync_investor_document(
            session,
            doc=doc,
            source=source,
            ingestion_run_id=ingestion_run_id,
        )
    except Exception as exc:
        if _is_retriable_ingest_transaction_error(exc) or not getattr(session, "is_active", True):
            raise
        return


def _sync_ipo_outputs(
    session: Session,
    doc: Document,
    *,
    source: Source,
    ingestion_run_id: int | None = None,
) -> None:
    try:
        sync_ipo_document(
            session,
            doc=doc,
            source=source,
            ingestion_run_id=ingestion_run_id,
        )
    except Exception as exc:
        if _is_retriable_ingest_transaction_error(exc) or not getattr(session, "is_active", True):
            raise
        return


def _apply_entity_dictionary(session: Session, doc: Document) -> None:
    haystack = f"{doc.title or ''}\n{doc.normalized_text}".casefold()
    existing_entity_ids = {
        int(link.entity_id)
        for link in (doc.entity_links or [])
        if getattr(link, "entity_id", None) is not None
    }
    existing_entity_ids.update(
        int(link.entity_id)
        for link in session.new
        if isinstance(link, DocumentEntity)
        and getattr(link, "document_id", None) == doc.id
        and getattr(link, "entity_id", None) is not None
    )
    existing_entity_ids.update(
        int(entity_id)
        for (entity_id,) in session.query(DocumentEntity.entity_id).filter_by(document_id=doc.id).all()
        if entity_id is not None
    )
    entities = session.query(Entity).options(selectinload(Entity.aliases)).all()
    for ent in entities:
        patterns = [ent.canonical_name] + [a.alias for a in ent.aliases]
        if any(p and p.casefold() in haystack for p in patterns):
            if int(ent.id) in existing_entity_ids:
                continue
            session.add(
                DocumentEntity(
                    document_id=doc.id,
                    entity_id=ent.id,
                    method="dictionary",
                    confidence=0.8,
                )
            )
            existing_entity_ids.add(int(ent.id))


def _get_or_create_source(
    session: Session,
    *,
    name: str,
    category: str,
    priority: int,
    config: dict[str, Any] | None = None,
) -> Source:
    src = session.query(Source).filter_by(name=name).first()
    if src:
        return src
    now = utcnow()
    src = Source(
        name=name,
        category=category,
        priority=int(priority),
        polling_interval_seconds=None,
        config=config or {},
        created_at=now,
    )
    session.add(src)
    session.flush()
    return src



def _ensure_state(session: Session, source: Source, now: datetime, *, mark_run_started: bool = True) -> SourceState:
    state = session.query(SourceState).filter_by(source_id=source.id).first()
    if not state:
        state = SourceState(
            source_id=source.id,
            last_run_at=None,
            last_success_at=None,
            next_run_at=None,
            consecutive_failures=0,
            backoff_until=None,
            cursor={},
            updated_at=now,
        )
        session.add(state)
        session.flush()
    if mark_run_started:
        state.last_run_at = now
        state.updated_at = now
    return state


def ingest_note(
    session: Session,
    *,
    title: str,
    text: str,
    url: str | None = None,
    author: str | None = None,
    language_hint: str | None = None,
    extra: dict[str, Any] | None = None,
) -> Document:
    src = session.query(Source).filter_by(name="internal").first()
    if not src:
        raise RuntimeError("Missing source: internal (run thoth init-db)")

    normalized = clean_whitespace(text)
    lang: LanguageGuess = detect_language(normalized)
    now = utcnow()
    canonical_url = str(url or "").strip() or None
    author_v = str(author or "").strip() or None
    language_v = str(language_hint or "").strip() or lang.language
    doc = Document(
        kind="note",
        source_id=src.id,
        external_id=None,
        canonical_url=canonical_url,
        url=canonical_url,
        title=title.strip() or None,
        author=author_v,
        published_at=None,
        ingested_at=now,
        raw_text=text,
        normalized_text=normalized,
        language=language_v,
        language_confidence=lang.confidence,
        translation_en=None,
        translation_backend=None,
        translation_model=None,
        translation_glossary_version=None,
        translated_at=None,
        triage_state="ACTIONED",
        saved=False,
        seen_at=None,
        opened_at=None,
        saved_at=None,
        actioned_at=now,
        ignored_at=None,
        triage_updated_at=now,
        content_hash=sha256_text(normalized),
        extra=dict(extra or {}),
        importance=src.priority,
        storage_tier="hot",
        cold_archive_id=None,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentEvent(
            document_id=doc.id,
            event_type="INGESTED",
            event_at=now,
            meta={"source": "internal_note"},
        )
    )
    _apply_entity_dictionary(session, doc)
    return doc


def upsert_internal_document(
    session: Session,
    *,
    external_id: str,
    title: str,
    text: str,
    kind: str = "note",
    url: str | None = None,
    canonical_url: str | None = None,
    published_at: datetime | None = None,
    extra: dict[str, Any] | None = None,
) -> Document:
    src = session.query(Source).filter_by(name="internal").first()
    if not src:
        raise RuntimeError("Missing source: internal (run thoth init-db)")

    external_id_v = str(external_id or "").strip()
    if not external_id_v:
        raise ValueError("Missing external_id.")

    text_v = text or ""
    normalized = clean_whitespace(text_v)
    if not normalized:
        normalized = clean_whitespace(title or "")
    lang: LanguageGuess = detect_language(normalized)
    now = utcnow()
    published_v = published_at or now
    extra_v = dict(extra or {})
    content_hash = sha256_text(normalized)

    doc = (
        session.query(Document)
        .filter_by(source_id=src.id, external_id=external_id_v)
        .first()
    )
    if not doc and canonical_url:
        doc = (
            session.query(Document)
            .filter_by(source_id=src.id, canonical_url=str(canonical_url))
            .first()
        )

    if doc:
        prev_hash = doc.content_hash
        doc.kind = (kind or doc.kind).strip() or doc.kind
        doc.canonical_url = str(canonical_url) if canonical_url else doc.canonical_url
        doc.url = str(url) if url else doc.url
        doc.title = (title or "").strip() or doc.title
        doc.published_at = published_at or doc.published_at or published_v
        doc.raw_text = text_v or normalized
        doc.normalized_text = normalized
        doc.language = lang.language
        doc.language_confidence = lang.confidence
        doc.content_hash = content_hash
        if isinstance(doc.extra, dict):
            merged_extra = dict(doc.extra)
            merged_extra.update(extra_v)
            doc.extra = merged_extra
        else:
            doc.extra = extra_v
        if prev_hash != content_hash:
            doc.translation_en = None
            doc.translation_backend = None
            doc.translation_model = None
            doc.translation_glossary_version = None
            doc.translated_at = None
        session.flush()
        session.add(
            DocumentEvent(
                document_id=doc.id,
                event_type="UPDATED",
                event_at=now,
                meta={
                    "source": "internal_publish",
                    "external_id": external_id_v,
                    "kind": doc.kind,
                },
            )
        )
        _apply_entity_dictionary(session, doc)
        return doc

    doc = Document(
        kind=(kind or "note").strip() or "note",
        source_id=src.id,
        external_id=external_id_v,
        canonical_url=str(canonical_url) if canonical_url else None,
        url=str(url) if url else None,
        title=(title or "").strip() or None,
        author=None,
        published_at=published_v,
        ingested_at=now,
        raw_text=text_v or normalized,
        normalized_text=normalized,
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
        extra=extra_v,
        importance=src.priority,
        storage_tier="hot",
        cold_archive_id=None,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentEvent(
            document_id=doc.id,
            event_type="INGESTED",
            event_at=now,
            meta={
                "source": "internal_publish",
                "external_id": external_id_v,
                "kind": doc.kind,
            },
        )
    )
    _apply_entity_dictionary(session, doc)
    return doc


def _tweet_id_from_url(url: str | None) -> str | None:
    if not url:
        return None
    match = re.search(r"/status/(\d+)", url)
    if not match:
        return None
    return match.group(1)


def _tweet_title(handle: str | None, text: str | None) -> str | None:
    snippet = clean_whitespace(text or "")
    if snippet:
        snippet = snippet[:160]
    if handle and snippet:
        return f"@{handle}: {snippet}"
    if handle:
        return f"@{handle} tweet"
    return snippet or None


def _auto_translate_capture(
    session: Session, doc: Document, *, text: str, title: str | None = None
) -> None:
    lang_guess = detect_language(text or "")
    if not _should_auto_translate(lang_guess):
        return
    backend = settings.default_translation_backend or "googletrans"
    try:
        translate_document(session, document_id=doc.id, backend=backend, commit=False)
    except Exception:
        return


def _normalize_url(url: str) -> str:
    try:
        parsed = urlparse(url.strip())
    except Exception:
        return url.strip()
    if not parsed.scheme or not parsed.netloc:
        return url.strip()
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")
    return urlunparse((scheme, netloc, path, "", parsed.query, ""))


def _expand_url(url: str, *, timeout_s: float = 8.0) -> str:
    if "t.co/" not in url:
        return url
    try:
        resp = requests.head(
            url,
            allow_redirects=True,
            timeout=timeout_s,
            headers=DEFAULT_HEADERS,
        )
        if resp.url:
            return str(resp.url)
    except Exception:
        return url
    return url


def _url_variants(url: str) -> list[str]:
    out: list[str] = []
    raw = url.strip()
    if not raw:
        return out
    out.append(raw)
    norm = _normalize_url(raw)
    if norm and norm not in out:
        out.append(norm)
    if norm.startswith("http://"):
        alt = "https://" + norm[len("http://") :]
        if alt not in out:
            out.append(alt)
    # Do NOT add the http fallback unless explicitly opted-in via settings
    # (kept for rare legacy sites).
    if norm.startswith("https://") and getattr(settings, "allow_http_fallback", False):
        alt = "http://" + norm[len("https://") :]
        if alt not in out:
            out.append(alt)
    return out


def _record_mentions(session: Session, doc: Document, links: list[str]) -> None:
    if not links:
        return
    expand_limit = int(getattr(settings, "x_link_expand_limit", 4) or 0)
    expanded = 0
    seen: set[str] = set()

    for link in links:
        if not link:
            continue
        raw = link.strip()
        if not raw or raw in seen:
            continue
        seen.add(raw)

        candidates = [raw]
        if expand_limit and "t.co/" in raw and expanded < expand_limit:
            expanded += 1
            expanded_url = _expand_url(raw)
            if expanded_url and expanded_url != raw:
                candidates.append(expanded_url)

        for cand in candidates:
            variants = _url_variants(cand)
            if not variants:
                continue
            target = (
                session.query(Document)
                .filter(
                    or_(Document.canonical_url.in_(variants), Document.url.in_(variants))
                )
                .first()
            )
            if not target or target.id == doc.id:
                continue
            existing = (
                session.query(DocumentMention)
                .filter_by(
                    source_document_id=doc.id,
                    target_document_id=target.id,
                    url=_normalize_url(cand),
                )
                .first()
            )
            if existing:
                continue
            session.add(
                DocumentMention(
                    source_document_id=doc.id,
                    target_document_id=target.id,
                    url=_normalize_url(cand),
                    created_at=utcnow(),
                )
            )


def _ensure_images_for_doc(
    session: Session,
    doc: Document,
    *,
    image_urls: list[str],
    cfg: dict[str, Any],
) -> int:
    if not (cfg.get("download_article_images") or cfg.get("download_post_media") or cfg.get("ocr_enabled")):
        return 0
    if not image_urls:
        return 0

    max_images = int(cfg.get("max_images_per_doc", 2) or 2)
    max_bytes = int(cfg.get("max_image_bytes", 5_000_000) or 5_000_000)
    try:
        session.flush()
        existing_rows = session.query(Attachment).filter_by(document_id=doc.id, kind="image").all()
    except Exception:
        existing_rows = [a for a in (doc.attachments or []) if a.kind == "image"]
    existing_urls = {a.original_url for a in existing_rows if a.original_url}
    added = 0
    for img_url in image_urls:
        if not img_url or img_url in existing_urls:
            continue
        try:
            res = fetch(img_url, timeout_s=10)
            if not res.content:
                continue
            if max_bytes and len(res.content) > max_bytes:
                continue
            ext = guess_extension(content_type=res.content_type, url=img_url) or ".jpg"
            object_path, sha = put_bytes(res.content, ext=ext)
            session.add(
                Attachment(
                    document_id=doc.id,
                    kind="image",
                    original_url=img_url,
                    content_type=res.content_type,
                    object_path=object_path,
                    sha256=sha,
                    created_at=utcnow(),
                )
            )
            added += 1
            if added >= max_images:
                break
        except FetchError:
            continue
        except Exception:
            continue
    return added


def _store_item_attachments(
    session: Session,
    doc: Document,
    *,
    item_attachments: list[dict[str, Any]],
    cfg: dict[str, Any],
) -> int:
    if not item_attachments:
        return 0
    max_bytes = int(cfg.get("max_attachment_bytes", 25_000_000) or 25_000_000)
    existing_urls = {
        (a.kind, a.original_url or "", a.content_type or "")
        for a in (doc.attachments or [])
    }
    existing_sha = {a.sha256 for a in (doc.attachments or []) if a.sha256}
    added = 0
    for row in item_attachments:
        if not isinstance(row, dict):
            continue
        data_bytes = row.get("data_bytes")
        if not isinstance(data_bytes, (bytes, bytearray)):
            continue
        data = bytes(data_bytes)
        if not data:
            continue
        if max_bytes > 0 and len(data) > max_bytes:
            continue
        kind = str(row.get("kind") or "file").strip() or "file"
        content_type = str(row.get("content_type") or "").strip() or None
        original_url = str(row.get("original_url") or "").strip() or None
        dedupe_key = (kind, original_url or "", content_type or "")
        if original_url and dedupe_key in existing_urls:
            continue
        ext = str(row.get("ext") or "").strip()
        if not ext:
            ext = guess_extension(content_type=content_type, url=str(row.get("filename") or original_url or ""))
        object_path, sha = put_bytes(data, ext=ext)
        if sha in existing_sha:
            continue
        attachment = Attachment(
            document_id=doc.id,
            kind=kind,
            original_url=original_url,
            content_type=content_type,
            object_path=object_path,
            sha256=sha,
            created_at=utcnow(),
        )
        session.add(attachment)
        is_pdf = (
            kind.lower() == "pdf"
            or str(content_type or "").split(";", 1)[0].strip().lower() == "application/pdf"
            or data.startswith(b"%PDF-")
        )
        if is_pdf:
            session.flush()
            from thoth_terminal.document_page_service import index_pdf_pages_from_bytes
            from thoth_terminal.models import DocumentPage

            has_pages = (
                session.query(DocumentPage.id)
                .filter(DocumentPage.document_id == int(doc.id))
                .first()
                is not None
            )
            preferred_urls = {str(doc.url or "").strip(), str(doc.canonical_url or "").strip()}
            if not has_pages or str(original_url or "").strip() in preferred_urls:
                index_pdf_pages_from_bytes(
                    session,
                    document_id=int(doc.id),
                    pdf_bytes=data,
                    source_sha256=sha,
                    source_attachment_id=int(attachment.id),
                )
        added += 1
        existing_sha.add(sha)
        existing_urls.add(dedupe_key)
    return added


def _backfill_mentions_for_doc(session: Session, doc: Document) -> None:
    if doc.kind == "tweet":
        return
    urls: list[str] = []
    if doc.canonical_url:
        urls.append(str(doc.canonical_url))
    if doc.url and doc.url not in urls:
        urls.append(str(doc.url))
    if not urls:
        return
    variants: set[str] = set()
    for u in urls:
        for v in _url_variants(u):
            variants.add(v)
    if not variants:
        return

    limit = int(getattr(settings, "mention_backfill_limit", 1000) or 0)
    if limit <= 0:
        return
    tweets = (
        session.query(Document)
        .filter(Document.kind == "tweet")
        .order_by(Document.ingested_at.desc())
        .limit(limit)
        .all()
    )
    for tweet in tweets:
        if tweet.id == doc.id:
            continue
        extra = tweet.extra if isinstance(tweet.extra, dict) else {}
        links = extra.get("links") or []
        if isinstance(links, str):
            links = [links]
        if not isinstance(links, list):
            continue
        for link in links:
            if not link:
                continue
            norm = _normalize_url(str(link))
            if norm not in variants:
                continue
            existing = (
                session.query(DocumentMention)
                .filter_by(
                    source_document_id=tweet.id,
                    target_document_id=doc.id,
                    url=norm,
                )
                .first()
            )
            if existing:
                continue
            session.add(
                DocumentMention(
                    source_document_id=tweet.id,
                    target_document_id=doc.id,
                    url=norm,
                    created_at=utcnow(),
                )
            )


def ingest_tweet(
    session: Session,
    *,
    payload: dict[str, Any],
    defer_heavy: bool = False,
    enqueue_postprocess=None,
) -> Document | None:
    src = _get_or_create_source(
        session,
        name="x_capture",
        category="capture",
        priority=4,
        config={"connector": "x_capture"},
    )

    url = str(payload.get("url") or payload.get("tweet_url") or "").strip() or None
    tweet_id = str(payload.get("tweet_id") or payload.get("id") or "").strip() or None
    if not tweet_id:
        tweet_id = _tweet_id_from_url(url)

    text = str(payload.get("text") or payload.get("full_text") or "").strip()
    quoted_text = payload.get("quoted_text")
    quoted_payload = payload.get("quoted") if isinstance(payload.get("quoted"), dict) else None
    if quoted_text is None and quoted_payload:
        quoted_text = quoted_payload.get("text")
    quoted_text = str(quoted_text or "").strip() or None

    thread_texts: list[str] = []
    thread = payload.get("thread")
    if isinstance(thread, list):
        for item in thread:
            if isinstance(item, dict):
                t = str(item.get("text") or "").strip()
                if t:
                    thread_texts.append(t)
            elif isinstance(item, str):
                t = item.strip()
                if t:
                    thread_texts.append(t)
    elif isinstance(thread, str):
        t = thread.strip()
        if t:
            thread_texts.append(t)

    links = payload.get("links") or payload.get("urls") or []
    if isinstance(links, str):
        links = [links]
    if not isinstance(links, list):
        links = []
    if quoted_payload:
        q_links = quoted_payload.get("links") or []
        if isinstance(q_links, str):
            q_links = [q_links]
        if isinstance(q_links, list):
            for l in q_links:
                if l:
                    links.append(str(l))
    thread_links: list[str] = []
    if isinstance(thread, list):
        for item in thread:
            if not isinstance(item, dict):
                continue
            lks = item.get("links") or []
            if isinstance(lks, str):
                lks = [lks]
            if isinstance(lks, list):
                for l in lks:
                    if l:
                        thread_links.append(str(l))
    if thread_links:
        links.extend(thread_links)
    # de-dupe links
    if links:
        seen_links: set[str] = set()
        uniq_links: list[str] = []
        for l in links:
            s = str(l).strip()
            if not s or s in seen_links:
                continue
            seen_links.add(s)
            uniq_links.append(s)
        links = uniq_links

    author = str(payload.get("author") or payload.get("author_name") or "").strip() or None
    handle = str(payload.get("handle") or payload.get("screen_name") or "").strip().lstrip("@") or None
    created_at = payload.get("created_at") or payload.get("published_at")

    payload_out = dict(payload or {})
    media_manifest = build_tweet_media_manifest(payload_out)
    if media_manifest and not payload_out.get("media_manifest"):
        payload_out["media_manifest"] = media_manifest
    image_urls = extract_candidate_image_urls(payload_out)
    if image_urls and not payload_out.get("image_urls"):
        payload_out["image_urls"] = image_urls

    article_payload = payload_out.get("article") if isinstance(payload_out.get("article"), dict) else None
    article_text = str((article_payload or {}).get("text") or "").strip() or None
    article_title = str((article_payload or {}).get("title") or "").strip() or None
    article_url = (
        str((article_payload or {}).get("url") or payload_out.get("article_url") or "").strip() or None
    )
    thread_context = payload_out.get("thread_context") if isinstance(payload_out.get("thread_context"), dict) else None

    combined = text
    if article_text:
        article_block = article_text
        if article_title and article_title not in article_block:
            article_block = f"{article_title}\n\n{article_block}".strip()
        combined = f"{combined}\n\nX article:\n{article_block}".strip()
    elif article_url:
        combined = f"{combined}\n\nX article link:\n{article_url}".strip()
    if quoted_text:
        combined = f"{combined}\n\nQuoted tweet:\n{quoted_text}".strip()
    if thread_context:
        ancestors = [
            str(item.get("text") or "").strip()
            for item in (thread_context.get("ancestors") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        replies = [
            str(item.get("text") or "").strip()
            for item in (thread_context.get("replies") or [])
            if isinstance(item, dict) and str(item.get("text") or "").strip()
        ]
        if ancestors:
            combined = (
                f"{combined}\n\nEarlier in same-author thread:\n" + "\n\n".join(ancestors)
            ).strip()
        if replies:
            combined = (
                f"{combined}\n\nLater replies in same-author thread:\n" + "\n\n".join(replies)
            ).strip()
    elif thread_texts:
        thread_block = "\n\n".join(thread_texts)
        combined = f"{combined}\n\nThread (same author):\n{thread_block}".strip()
    if links:
        link_block = "\n".join(links)
        combined = f"{combined}\n\nLinks:\n{link_block}".strip()
    if image_urls:
        combined = f"{combined}\n\nMedia:\n" + "\n".join(image_urls)
    normalized = clean_whitespace(combined or "")
    if not normalized:
        normalized = clean_whitespace(url or "(tweet)")

    title = str(payload.get("title") or "").strip() or None
    if not title:
        if article_title:
            title = article_title[:240]
    if not title:
        title = _tweet_title(handle, text)

    content_hash = sha256_text(normalized)

    existing = None
    if tweet_id:
        existing = (
            session.query(Document)
            .filter_by(source_id=src.id, external_id=str(tweet_id))
            .first()
        )
    if not existing and url:
        existing = session.query(Document).filter_by(source_id=src.id, canonical_url=url).first()

    now = utcnow()
    if existing:
        existing.title = title or existing.title
        existing.author = author or existing.author
        existing.raw_text = normalized
        existing.normalized_text = normalized
        existing.content_hash = content_hash
        existing.extra = payload_out or existing.extra
        if created_at:
            dt = parse_datetime_iso(str(created_at))
            if dt:
                existing.published_at = dt
        if not defer_heavy and image_urls:
            _ensure_images_for_doc(session, existing, image_urls=image_urls, cfg={"ocr_enabled": True})
        if defer_heavy and enqueue_postprocess:
            try:
                enqueue_postprocess(session, doc_id=existing.id, source_id=src.id)
            except Exception:
                pass
        return existing

    lang = detect_language(normalized)
    published_at = None
    if created_at:
        published_at = parse_datetime_iso(str(created_at))
    doc = Document(
        kind="tweet",
        source_id=src.id,
        external_id=str(tweet_id) if tweet_id else None,
        canonical_url=url,
        url=url,
        title=title,
        author=author,
        published_at=published_at,
        ingested_at=now,
        raw_text=normalized,
        normalized_text=normalized,
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
        extra=payload_out or {},
        importance=src.priority,
        storage_tier="hot",
        cold_archive_id=None,
    )
    session.add(doc)
    session.flush()
    session.add(
        DocumentEvent(
            document_id=doc.id,
            event_type="INGESTED",
            event_at=now,
            meta={"source": "x_capture"},
        )
    )

    if not defer_heavy:
        if normalized:
            _auto_translate_capture(session, doc, text=normalized, title=title)
        if isinstance(links, list) and links:
            _record_mentions(session, doc, [str(l) for l in links if l])
        if image_urls:
            _ensure_images_for_doc(session, doc, image_urls=image_urls, cfg={"ocr_enabled": True})
    elif enqueue_postprocess:
        try:
            enqueue_postprocess(session, doc_id=doc.id, source_id=src.id)
        except Exception:
            pass

    return doc


def _headline_language(title: str | None) -> LanguageGuess:
    if not title:
        return LanguageGuess(language=None, confidence=None)
    min_chars = int(getattr(settings, "auto_translate_title_min_chars", 12) or 12)
    return detect_language(title, min_chars=min_chars)


def _should_auto_translate(title_lang: LanguageGuess) -> bool:
    if not getattr(settings, "auto_translate_non_english", True):
        return False
    lang = (title_lang.language or "").strip().lower()
    if not lang or lang.startswith("en"):
        return False
    min_conf = float(getattr(settings, "auto_translate_min_confidence", 0.55) or 0.0)
    conf = title_lang.confidence if title_lang.confidence is not None else 0.0
    return conf >= min_conf


def _maybe_auto_translate(
    session: Session, doc: Document, *, title_lang: LanguageGuess, cfg: dict[str, Any]
) -> None:
    if not _should_auto_translate(title_lang):
        body_lang = detect_language(doc.normalized_text or "")
        if not _should_auto_translate(body_lang):
            return
    if cfg.get("auto_translate") is False:
        return
    backend = str(cfg.get("translation_backend") or settings.default_translation_backend or "googletrans")
    try:
        translate_document(session, document_id=doc.id, backend=backend, commit=False)
    except Exception:
        return


def _apply_money_mentions(
    session: Session,
    doc: Document,
    *,
    cfg: dict[str, Any],
) -> None:
    if not getattr(settings, "fx_enabled", True):
        return
    if cfg.get("enable_currency_normalization") is False:
        return

    text_parts = []
    if doc.title:
        text_parts.append(doc.title)
    if doc.translation_en:
        text_parts.append(doc.translation_en)
    else:
        text_parts.append(doc.normalized_text or "")
    text = "\n".join([t for t in text_parts if t])
    if not text:
        return

    mentions = extract_money_mentions(text)
    if not mentions:
        if isinstance(doc.extra, dict) and doc.extra.get("money_mentions"):
            extra = dict(doc.extra)
            extra.pop("money_mentions", None)
            extra.pop("money_base_currency", None)
            extra.pop("money_rate_date", None)
            extra.pop("money_provider", None)
            doc.extra = extra
        return

    rates = get_latest_rates(session, base=str(settings.base_currency or "USD"))
    if not rates:
        rates = ensure_fx_rates(session, base=str(settings.base_currency or "USD"))

    money_rows: list[dict[str, object]] = []
    for m in mentions:
        usd_amount = None
        rate = None
        if rates:
            usd_amount = convert_to_base(m.amount, m.currency, rates)
            if rates.rates.get(m.currency):
                rate = float(rates.rates.get(m.currency))
        money_rows.append(
            {
                "raw": m.raw,
                "currency": m.currency,
                "amount": float(m.amount),
                "usd_amount": float(usd_amount) if usd_amount is not None else None,
                "rate": float(rate) if rate is not None else None,
                "start": int(m.start),
                "end": int(m.end),
            }
        )

    extra = dict(doc.extra or {})
    extra["money_mentions"] = money_rows
    extra["money_base_currency"] = str(settings.base_currency or "USD")
    if rates:
        extra["money_rate_date"] = rates.as_of.isoformat()
        extra["money_provider"] = rates.provider
    doc.extra = extra


def _is_retriable_ingest_transaction_error(exc: Exception) -> bool:
    text = f"{type(exc).__name__}: {exc}".lower()
    return (
        "locknotavailable" in text
        or "lock timeout" in text
        or "deadlock detected" in text
        or "deadlockdetected" in text
        or "could not serialize access" in text
        or "serialization failure" in text
        or "infailedsqltransaction" in text
        or "current transaction is aborted" in text
        or "pendingrollbackerror" in text
    )


def ingest_source(
    session: Session,
    source: Source,
    *,
    source_config_override: dict[str, Any] | None = None,
    prefetched_items: list[dict] | None = None,
    prefetched_error: Exception | None = None,
    prefetched_cursor: dict[str, Any] | None = None,
    prefetched_stats: dict[str, Any] | None = None,
    defer_heavy: bool = False,
    defer_derived_sync: bool = False,
    defer_institutional_sync: bool = False,
    enqueue_postprocess=None,
) -> IngestResult:
    enabled, reason = _is_source_enabled(source)
    now = utcnow()

    cfg = dict(source_config_override) if isinstance(source_config_override, dict) else (source.config or {})
    source_cursor_override = cfg.pop("_source_cursor_override", None)
    connector = cfg.get("connector")
    if not connector:
        raise ValueError(f"Source {source.name} missing config.connector")

    if getattr(settings, "fx_enabled", True):
        try:
            ensure_fx_rates(session, base=str(settings.base_currency or "USD"))
        except Exception:
            pass

    state = _ensure_state(session, source, now, mark_run_started=False)

    items: list[dict] = []
    fetch_error: Exception | None = None
    next_cursor: dict[str, Any] | None = None
    fetch_stats: dict[str, Any] | None = None
    if enabled:
        if prefetched_error is not None:
            fetch_error = prefetched_error
        elif prefetched_items is not None:
            items = prefetched_items
            next_cursor = dict(prefetched_cursor) if isinstance(prefetched_cursor, dict) else None
            fetch_stats = dict(prefetched_stats) if isinstance(prefetched_stats, dict) else None
        else:
            try:
                fetch_cfg = dict(cfg)
                first_run = state.last_success_at is None
                if connector == "web_list" and first_run:
                    bootstrap_days = fetch_cfg.get("bootstrap_days")
                    if bootstrap_days and fetch_cfg.get("recent_days") is None:
                        fetch_cfg["recent_days"] = bootstrap_days
                fetched = fetch_items_result(
                    connector,
                    source_config=fetch_cfg,
                    source_cursor=(
                        dict(source_cursor_override)
                        if isinstance(source_cursor_override, dict)
                        else (state.cursor if isinstance(state.cursor, dict) else {})
                    ),
                )
                items = list(fetched.items or [])
                next_cursor = dict(fetched.cursor) if isinstance(fetched.cursor, dict) else None
                fetch_stats = dict(fetched.stats) if isinstance(fetched.stats, dict) else None
            except Exception as exc:
                fetch_error = exc

    run = IngestionRun(
        source_id=source.id,
        started_at=now,
        finished_at=None,
        status="success",
        error=None,
        items_seen=len(items) if not fetch_error else 0,
        items_new=0,
        items_updated=0,
        extra={},
    )
    session.add(run)
    session.flush()

    if not enabled:
        run.status = "success"
        run.finished_at = utcnow()
        run.extra = {"skipped": True, "reason": reason}
        state.last_run_at = now
        state.updated_at = utcnow()
        session.flush()
        logger.info("ingest run %s source=%s(%s) skipped: %s", run.id, source.name, source.id, reason)
        return IngestResult(seen=0, new=0, updated=0, run_id=int(run.id))

    if fetch_error is not None:
        run.status = "error"
        run.error = f"{type(fetch_error).__name__}: {fetch_error}"
        run.finished_at = utcnow()
        state.last_run_at = now
        state.updated_at = utcnow()
        state.consecutive_failures = int(state.consecutive_failures or 0) + 1
        browser_backoff_s = browser_circuit_backoff_seconds(cfg, state.consecutive_failures)
        if browser_backoff_s is not None:
            state.backoff_until = now + timedelta(seconds=browser_backoff_s)
            cursor = dict(state.cursor or {})
            cursor["_browser_circuit"] = {
                "state": "open",
                "failures": int(state.consecutive_failures),
                "retry_at": state.backoff_until.isoformat(),
                "last_error": run.error[:1000] if run.error else None,
            }
            state.cursor = cursor
        session.flush()
        logger.error(
            "ingest run %s source=%s(%s) fetch failed: %s",
            run.id,
            source.name,
            source.id,
            run.error,
        )
        return IngestResult(
            seen=0,
            new=0,
            updated=0,
            status="error",
            error=run.error,
            run_id=int(run.id),
        )

    items_new = 0
    items_updated = 0
    new_docs: list[Document] = []
    postprocess_ids: list[int] = []
    flush_run_state = True
    try:
        run.items_seen = len(items)
        if fetch_stats:
            run_extra = dict(run.extra or {})
            run_extra["connector_stats"] = dict(fetch_stats)
            run.extra = run_extra
            trade_stats = upsert_trade_series_from_stats(
                session,
                source=source,
                fetch_stats=fetch_stats,
                ingestion_run_id=int(run.id),
            )
            if any(int(v or 0) > 0 for v in trade_stats.values()):
                run_extra = dict(run.extra or {})
                run_extra["trade_series_stats"] = trade_stats
                run.extra = run_extra
            monthly_revenue_stats = upsert_monthly_revenue_from_stats(
                session,
                source=source,
                fetch_stats=fetch_stats,
                ingestion_run_id=int(run.id),
            )
            if any(int(v or 0) > 0 for v in monthly_revenue_stats.values()):
                run_extra = dict(run.extra or {})
                run_extra["monthly_revenue_stats"] = monthly_revenue_stats
                run.extra = run_extra
            future_electronics_stats = upsert_future_electronics_products_from_stats(
                session,
                source=source,
                fetch_stats=fetch_stats,
                ingestion_run_id=int(run.id),
            )
            if any(int(v or 0) > 0 for v in future_electronics_stats.values()):
                run_extra = dict(run.extra or {})
                run_extra["future_electronics_dataset_stats"] = future_electronics_stats
                run.extra = run_extra

        for item in items:
            image_urls = list(item.get("image_urls") or [])
            item_attachments = item.get("attachments") or []
            if not isinstance(item_attachments, list):
                item_attachments = []
            canonical_url = clean_url(item.get("canonical_url") or item.get("url"))
            external_id = item.get("external_id")
            title = (item.get("title") or "").strip() or None
            url = clean_url(item.get("url"))
            links = item.get("links") or []
            if isinstance(links, str):
                links = [links]
            if not isinstance(links, list):
                links = []
            extra = item.get("extra") or {}
            if not isinstance(extra, dict):
                extra = {}
            if image_urls and not extra.get("image_urls"):
                extra["image_urls"] = image_urls
            if links and not extra.get("links"):
                extra["links"] = links
            identity_enriched = False
            try:
                identity_enriched = enrich_metadata_with_identity(
                    session,
                    extra,
                    source=str(connector or ""),
                    author=item.get("author"),
                    title=title,
                ) is not None
            except Exception:
                identity_enriched = False
            company_slug = extra.get("company_slug") or cfg.get("company_slug")
            company_doc_type = extra.get("company_doc_type") or cfg.get("company_doc_type")
            company_doc_label = extra.get("company_doc_label") or cfg.get("company_doc_label")
            defer_company_entity_link = bool(cfg.get("defer_company_entity_link") or cfg.get("skip_company_entity_link"))

            existing = None
            if external_id:
                existing = (
                    session.query(Document)
                    .filter_by(source_id=source.id, external_id=str(external_id))
                    .first()
                )
            if not existing and canonical_url:
                existing = (
                    session.query(Document)
                    .filter_by(source_id=source.id, canonical_url=str(canonical_url))
                    .first()
                )
            if not existing and extra.get("research_library") is True and extra.get("source_path"):
                # Roll forward rows created when publisher URLs or content
                # digests were used as local-library identity. The connector's
                # absolute source path is the only stable bridge shared by both
                # old and new representations.
                incoming_source_path = str(extra.get("source_path") or "").casefold()
                for candidate_id, candidate_extra in (
                    session.query(Document.id, Document.extra)
                    .filter(Document.source_id == source.id)
                    .all()
                ):
                    if not isinstance(candidate_extra, dict):
                        continue
                    if str(candidate_extra.get("source_path") or "").casefold() == incoming_source_path:
                        existing = session.get(Document, int(candidate_id))
                        break

            normalized = clean_whitespace(item.get("normalized_text") or item.get("raw_text") or "")
            if not normalized:
                normalized = title or url or ""

            content_hash = sha256_text(normalized)
            if not existing and cfg.get("dedupe_global"):
                dup_global = session.query(Document).filter_by(content_hash=content_hash).first()
                if dup_global:
                    continue
            if not existing and canonical_url:
                # Per-source hash dedup for list pages that surface duplicates.
                dup = (
                    session.query(Document)
                    .filter_by(source_id=source.id, content_hash=content_hash)
                    .first()
                )
                if dup:
                    continue

            title_lang = _headline_language(title)
            body_lang = detect_language(normalized)
            lang = title_lang if title_lang.language else body_lang

            if existing:
                if (
                    extra.get("research_library") is True
                    and str(external_id or "").startswith("local-research-path:")
                    and existing.external_id != str(external_id)
                ):
                    # Migrate legacy content-digest local-library identities to
                    # path-stable identities when the canonical local URI finds
                    # the existing row during rollout.
                    existing.external_id = str(external_id)
                _apply_company_link(
                    session,
                    existing,
                    company_slug=company_slug,
                    doc_type=company_doc_type,
                    label=company_doc_label,
                    defer_entity_link=defer_company_entity_link,
                )
                # Even if we're not refreshing content, we can still backfill image attachments.
                if not cfg.get("refresh_existing"):
                    updated_here = False
                    if identity_enriched:
                        merged_extra = dict(existing.extra or {}) if isinstance(existing.extra, dict) else {}
                        merged_extra.update(extra or {})
                        existing.extra = merged_extra
                        updated_here = True
                    if not defer_heavy and _ensure_images_for_doc(session, existing, image_urls=image_urls, cfg=cfg) > 0:
                        updated_here = True
                    if not defer_heavy and _store_item_attachments(session, existing, item_attachments=item_attachments, cfg=cfg) > 0:
                        updated_here = True
                    if defer_heavy:
                        existing_extra = existing.extra if isinstance(existing.extra, dict) else {}
                        needs_postprocess = (
                            updated_here
                            or bool(cfg.get("postprocess_existing_unchanged"))
                            or not isinstance(existing_extra.get("document_refinery"), dict)
                        )
                        if needs_postprocess:
                            postprocess_ids.append(existing.id)
                    if updated_here:
                        items_updated += 1
                    sync_unchanged = bool(cfg.get("sync_existing_unchanged") or cfg.get("sync_unchanged_existing"))
                    should_sync_existing = updated_here or sync_unchanged
                    if should_sync_existing and not defer_derived_sync:
                        _sync_company_structured_outputs(
                            session,
                            existing,
                            source=source,
                            company_slug=company_slug,
                            doc_type=company_doc_type,
                            ingestion_run_id=int(run.id),
                        )
                        _sync_investor_outputs(session, existing, source=source, ingestion_run_id=int(run.id))
                        _sync_ipo_outputs(session, existing, source=source, ingestion_run_id=int(run.id))
                    if should_sync_existing and not defer_institutional_sync:
                        sync_document_to_institutional(session, existing)
                    continue

                # Refresh existing rows (useful when extraction/cleaning improves).
                prev_hash = existing.content_hash
                existing.canonical_url = str(canonical_url) if canonical_url else existing.canonical_url
                existing.url = str(url) if url else existing.url
                existing.title = title or existing.title
                if not title_lang.language and existing.title:
                    title_lang = _headline_language(existing.title)
                    if title_lang.language:
                        lang = title_lang
                existing.author = item.get("author") or existing.author
                prior_extra = existing.extra if isinstance(existing.extra, dict) else {}
                incoming_manifest_trust = extra.get("manifest_provenance_trusted")
                prior_manifest_trust = prior_extra.get("manifest_provenance_trusted")
                manifest_history = bool(
                    extra.get("manifest_provenance_ever_seen")
                    or prior_extra.get("manifest_provenance_ever_seen")
                    or incoming_manifest_trust is True
                    or prior_manifest_trust is True
                    or prior_extra.get("source_manifest_validation") == "sha256_match_http_url"
                    or prior_extra.get("source_manifest")
                )
                if manifest_history:
                    extra["manifest_provenance_ever_seen"] = True
                if incoming_manifest_trust is True and extra.get("publication_date_known") is False:
                    # Manifest-backed local acquisitions distinguish access time
                    # from publication time. Clear any legacy file-mtime value.
                    existing.published_at = None
                elif incoming_manifest_trust is False and prior_manifest_trust is True:
                    # A previously verified manifest no longer validates. Do
                    # not preserve its publication date for the refreshed row.
                    existing.published_at = None
                elif incoming_manifest_trust is False and manifest_history:
                    # Once this path has been identified as a manifest-backed
                    # acquisition, later absence must not turn file mtime into
                    # a publication date on a subsequent refresh.
                    existing.published_at = None
                elif item.get("published_at") is not None:
                    existing.published_at = item.get("published_at")
                existing.raw_text = item.get("raw_text") or normalized
                existing.normalized_text = normalized
                existing.language = lang.language
                existing.language_confidence = lang.confidence
                if isinstance(existing.extra, dict):
                    merged_extra = dict(existing.extra)
                    merged_extra.update(extra or {})
                    existing.extra = merged_extra
                else:
                    existing.extra = extra or existing.extra
                existing.content_hash = content_hash

                # If the content changed, drop stale derived fields.
                if prev_hash != content_hash:
                    existing.translation_en = None
                    existing.translation_backend = None
                    existing.translation_model = None
                    existing.translation_glossary_version = None
                    existing.translated_at = None

                if not defer_heavy:
                    _apply_entity_dictionary(session, existing)
                    _ensure_images_for_doc(session, existing, image_urls=image_urls, cfg=cfg)
                    _store_item_attachments(session, existing, item_attachments=item_attachments, cfg=cfg)
                    if existing.translation_en is None:
                        _maybe_auto_translate(session, existing, title_lang=title_lang, cfg=cfg)
                    _apply_money_mentions(session, existing, cfg=cfg)
                    links = links or (existing.extra.get("links") if isinstance(existing.extra, dict) else [])
                    if isinstance(links, str):
                        links = [links]
                    if isinstance(links, list):
                        _record_mentions(session, existing, [str(l) for l in links if l])
                    _backfill_mentions_for_doc(session, existing)
                else:
                    postprocess_ids.append(existing.id)
                if not defer_derived_sync:
                    _sync_company_structured_outputs(
                        session,
                        existing,
                        source=source,
                        company_slug=company_slug,
                        doc_type=company_doc_type,
                        ingestion_run_id=int(run.id),
                    )
                    _sync_investor_outputs(session, existing, source=source, ingestion_run_id=int(run.id))
                    _sync_ipo_outputs(session, existing, source=source, ingestion_run_id=int(run.id))
                if not defer_institutional_sync:
                    sync_document_to_institutional(session, existing)
                items_updated += 1
                continue

            doc = Document(
                kind=str(item.get("kind") or "document"),
                source_id=source.id,
                external_id=str(external_id) if external_id else None,
                canonical_url=str(canonical_url) if canonical_url else None,
                url=str(url) if url else None,
                title=title,
                author=item.get("author"),
                published_at=item.get("published_at"),
                ingested_at=utcnow(),
                raw_text=item.get("raw_text") or normalized,
                normalized_text=normalized,
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
                triage_updated_at=utcnow(),
                content_hash=content_hash,
                extra=extra or {},
                importance=source.priority,
                storage_tier="hot",
                cold_archive_id=None,
            )
            session.add(doc)
            session.flush()

            _apply_defaults(session, doc, cfg=cfg)
            _apply_company_link(
                session,
                doc,
                company_slug=company_slug,
                doc_type=company_doc_type,
                label=company_doc_label,
                defer_entity_link=defer_company_entity_link,
            )
            if not defer_heavy:
                _apply_entity_dictionary(session, doc)
                _maybe_auto_translate(session, doc, title_lang=title_lang, cfg=cfg)
                _apply_money_mentions(session, doc, cfg=cfg)
                links = links or (doc.extra.get("links") if isinstance(doc.extra, dict) else [])
                if isinstance(links, str):
                    links = [links]
                if isinstance(links, list):
                    _record_mentions(session, doc, [str(l) for l in links if l])
                _backfill_mentions_for_doc(session, doc)
            else:
                postprocess_ids.append(doc.id)
            session.add(
                DocumentEvent(
                    document_id=doc.id,
                    event_type="INGESTED",
                    event_at=utcnow(),
                    meta={
                        "source": source.name,
                        "connector": cfg.get("connector"),
                    },
                )
            )
            if not defer_derived_sync:
                _sync_company_structured_outputs(
                    session,
                    doc,
                    source=source,
                    company_slug=company_slug,
                    doc_type=company_doc_type,
                    ingestion_run_id=int(run.id),
                )
                _sync_investor_outputs(session, doc, source=source, ingestion_run_id=int(run.id))
                _sync_ipo_outputs(session, doc, source=source, ingestion_run_id=int(run.id))
            if not defer_institutional_sync:
                sync_document_to_institutional(session, doc)

            raw_html = item.get("raw_html")
            has_html_attachment = any(
                isinstance(a, dict) and str(a.get("kind") or "").strip().lower() == "html"
                for a in item_attachments
            )
            if raw_html and cfg.get("store_raw_html_on_failure") and (not has_html_attachment):
                # Store raw HTML only when parsing likely failed (very short extracted text).
                if len(doc.normalized_text) < 200:
                    data = raw_html.encode("utf-8", errors="ignore")
                    object_path, sha = put_bytes(data, ext=".html")
                    session.add(
                        Attachment(
                            document_id=doc.id,
                            kind="html",
                            original_url=doc.url,
                            content_type="text/html",
                            object_path=object_path,
                            sha256=sha,
                            created_at=utcnow(),
                        )
                    )

            _store_item_attachments(session, doc, item_attachments=item_attachments, cfg=cfg)
            if not defer_heavy:
                _ensure_images_for_doc(session, doc, image_urls=image_urls, cfg=cfg)
            items_new += 1
            new_docs.append(doc)

        run.items_new = items_new
        run.items_updated = items_updated
        run.finished_at = utcnow()
        run.status = "success"
        run.error = None

        if items_new > 0:
            notify = _evaluate_notification_rules(session, docs=new_docs)
            extra = dict(run.extra or {})
            extra["new_doc_ids"] = [d.id for d in new_docs]
            extra["notify"] = {
                "new": items_new,
                "updated": items_updated,
                "toast": bool(notify.get("toast")),
                "sound": bool(notify.get("sound")),
                "rules": notify.get("rules") or [],
                "message": f"Ingest complete: {items_new} new, {items_updated} updated",
            }
            run.extra = extra

        rate_limited = bool(fetch_stats and fetch_stats.get("rate_limited"))
        state.last_run_at = now
        state.updated_at = utcnow()
        if rate_limited:
            retry_after_seconds = max(
                60,
                int(
                    (fetch_stats or {}).get("retry_after_seconds")
                    or cfg.get("rate_limit_backoff_seconds")
                    or 3600
                ),
            )
            if items:
                state.last_success_at = utcnow()
            state.consecutive_failures = max(1, int(state.consecutive_failures or 0))
            state.backoff_until = utcnow() + timedelta(seconds=retry_after_seconds)
        else:
            state.last_success_at = utcnow()
            state.consecutive_failures = 0
            state.backoff_until = None
            if isinstance(state.cursor, dict) and "_browser_circuit" in state.cursor:
                cursor = dict(state.cursor)
                cursor.pop("_browser_circuit", None)
                state.cursor = cursor
        if next_cursor is not None:
            state.cursor = dict(next_cursor)
    except Exception as exc:
        if _is_retriable_ingest_transaction_error(exc) or not getattr(session, "is_active", True):
            flush_run_state = False
            session.rollback()
            session.close()
            raise
        run.status = "error"
        run.error = f"{type(exc).__name__}: {exc}"
        run.finished_at = utcnow()

        state.last_run_at = now
        state.updated_at = utcnow()
        state.consecutive_failures = int(state.consecutive_failures or 0) + 1
    finally:
        if flush_run_state:
            session.flush()

    if defer_heavy and enqueue_postprocess and postprocess_ids:
        for doc_id in postprocess_ids:
            try:
                enqueue_postprocess(session, doc_id=doc_id, source_id=source.id)
            except Exception:
                continue

    if run.status == "success":
        new_ids = [d.id for d in new_docs]
        ids_preview = new_ids[:10]
        more = "..." if len(new_ids) > 10 else ""
        logger.info(
            "ingest run %s source=%s(%s) success: seen=%s new=%s updated=%s new_ids=%s%s",
            run.id,
            source.name,
            source.id,
            run.items_seen,
            run.items_new,
            run.items_updated,
            ids_preview,
            more,
        )
    else:
        logger.error(
            "ingest run %s source=%s(%s) failed: %s",
            run.id,
            source.name,
            source.id,
            run.error,
        )

    return IngestResult(
        seen=run.items_seen,
        new=items_new,
        updated=items_updated,
        status=str(run.status or "error"),
        error=run.error,
        run_id=int(run.id),
    )
