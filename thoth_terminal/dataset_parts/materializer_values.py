from __future__ import annotations

import csv
import hashlib
import json
import re
from datetime import date, datetime
from io import BytesIO, StringIO
from threading import RLock
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from thoth_terminal.db import utcnow
from thoth_terminal.dataset_catalog import (
    DATASET_FAMILY_COLUMN_TEMPLATES,
    DATASET_FAMILY_MATERIALIZER_FAMILIES,
    DATASET_FAMILY_META,
    DATASET_FAMILY_REQUIRED_ROLES,
    DATASET_OBJECT_TYPES,
    DATASET_VIEW_OPERATOR_VALUES,
    DATASET_VIEW_OPERATORS,
)
from thoth_terminal.models import (
    ChartDatasetView,
    Dataset,
    DatasetColumn,
    Document,
    DatasetLink,
    DatasetRow,
    DatasetVersion,
    DatasetView,
    InstrumentRequest,
    ProductStageEntry,
    StructuredDataFact,
)
from thoth_terminal.product_stage_service import serialize_product_stage_entry


def _family_materializer_slug(family: str, slug: str | None, title: str) -> str:
    raw = str(slug or "").strip()
    if raw:
        return _slugify(raw, default=family.replace("_", "-"))
    return _slugify(family.replace("_", "-") or title, default="family-dataset")


def _family_materializer_extra(
    *,
    family: str,
    extra: dict[str, Any] | None,
    provenance: dict[str, Any] | None,
) -> dict[str, Any]:
    meta = dataset_family_meta(family)
    provenance_payload = dict(provenance or {})
    merged = {
        "family_materializer": True,
        "family": family,
        "family_label": meta["label"],
        "family_tone": meta["tone"],
        "family_template_keys": [row["key"] for row in dataset_family_column_template(family)],
        "provenance_summary": provenance_payload.get("provenance_summary")
        or (extra or {}).get("provenance_summary")
        or f"{meta['label']} rows materialized through the canonical family dataset template.",
    }
    evidence_tier = provenance_payload.get("evidence_tier_summary") or (extra or {}).get("evidence_tier_summary")
    if evidence_tier not in (None, ""):
        merged["evidence_tier_summary"] = evidence_tier
    if provenance_payload:
        merged["provenance"] = provenance_payload
    merged.update(dict(extra or {}))
    return merged


def _materializer_text_hash(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _materializer_excerpt(value: Any, *, limit: int = 500) -> str | None:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if not text:
        return None
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)].rstrip() + "..."


def _first_materializer_value(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _materializer_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _family_materializer_document_values(row: Document) -> dict[str, Any]:
    extra = dict(row.extra or {})
    source_access = _materializer_dict(extra.get("source_access") or extra.get("access_provenance"))
    source_attribution = _materializer_dict(extra.get("source_attribution"))
    translation_provenance = _materializer_dict(extra.get("translation_provenance"))
    agent_translation = _materializer_dict(extra.get("local_agent_translation_status"))
    hydration = _materializer_dict(extra.get("hydration_provenance") or extra.get("hydration"))
    url_hydration = _materializer_dict(extra.get("url_text_hydration"))
    pdf_hydration = _materializer_dict(extra.get("pdf_text_hydration"))
    source_name = str(getattr(getattr(row, "source", None), "name", "") or "").strip()
    source_text = row.normalized_text or row.raw_text or ""
    translated_text = row.translation_en or ""
    language = str(row.language or "").strip()
    translation_needed = bool(language and language.lower() not in {"en", "eng"})
    translated = bool(str(translated_text or "").strip())
    translation_attempted = bool(agent_translation or translation_provenance.get("translation_attempted"))
    if translated:
        translation_status = "translated"
    elif translation_needed:
        translation_status = "missing"
    else:
        translation_status = "not_required"
    inline_translation_fallback = bool(
        translation_needed
        and not translated
        and translation_attempted
        and agent_translation.get("available") is not True
    )
    agent_translation_mode = (
        str(translation_provenance.get("agent_translation_mode") or "").strip()
        or (
            "stored_translation"
            if translated
            else "agent_inline_translation_required"
            if inline_translation_fallback
            else "pending_translation"
            if translation_needed
            else "not_required"
        )
    )
    observed_at = row.ingested_at or row.published_at or row.translated_at
    payload: dict[str, Any] = {
        "row_key": f"document:{int(row.id)}",
        "observed_at": observed_at.date().isoformat() if isinstance(observed_at, datetime) else observed_at,
        "canonical_url": row.canonical_url or row.url,
        "source_url": row.url or row.canonical_url,
        "source_owner": source_name or extra.get("source_owner"),
        "source_document_id": int(row.id),
        "source_language": language,
        "output_language": translation_provenance.get("output_language") or ("en" if translation_needed or translated else None),
        "language": language,
        "language_confidence": row.language_confidence,
        "translation_needed": translation_needed,
        "translation_status": translation_provenance.get("translation_status") or extra.get("translation_status") or translation_status,
        "translation_attempted": translation_attempted,
        "inline_signal_extraction_fallback": _first_materializer_value(
            translation_provenance.get("inline_signal_extraction_fallback"),
            inline_translation_fallback,
        ),
        "agent_translation_mode": agent_translation_mode,
        "translation_backend": row.translation_backend or agent_translation.get("backend"),
        "translation_model": row.translation_model,
        "translation_glossary_version": row.translation_glossary_version,
        "translation_failure_reason": translation_provenance.get("translation_failure_reason") or agent_translation.get("error"),
        "translated_at": row.translated_at.date().isoformat() if isinstance(row.translated_at, datetime) else row.translated_at,
        "translated_title_en": extra.get("translation_title_en"),
        "source_text_hash": row.content_hash or _materializer_text_hash(source_text),
        "translated_text_hash": _materializer_text_hash(translated_text),
        "translated_excerpt": _materializer_excerpt(translated_text),
        "extraction_method": translation_provenance.get("extraction_method") or extra.get("extraction_method"),
        "extraction_provenance": translation_provenance.get("extraction_provenance") or extra.get("extraction_provenance"),
        "fetch_method": _first_materializer_value(
            hydration.get("fetch_method"),
            extra.get("fetch_method"),
            "official_url" if url_hydration else None,
            "pdf_attachment" if pdf_hydration else None,
        ),
        "fetch_status": _first_materializer_value(hydration.get("fetch_status"), url_hydration.get("status"), pdf_hydration.get("status"), extra.get("fetch_status")),
        "content_type": _first_materializer_value(hydration.get("content_type"), url_hydration.get("content_type"), extra.get("content_type")),
        "content_hash": row.content_hash or hydration.get("content_hash") or extra.get("content_hash"),
        "hydration_status": _first_materializer_value(hydration.get("hydration_status"), url_hydration.get("status"), pdf_hydration.get("status"), extra.get("hydration_status")),
        "pdf_hydration_status": _first_materializer_value(hydration.get("pdf_hydration_status"), pdf_hydration.get("status"), hydration.get("pdf_status"), extra.get("pdf_hydration_status")),
        "readability_status": _first_materializer_value(hydration.get("readability_status"), extra.get("readability_status")),
        "ocr_status": _first_materializer_value(hydration.get("ocr_status"), extra.get("ocr_status")),
        "media_hydration_status": _first_materializer_value(
            hydration.get("media_hydration_status"),
            "attachments_stored" if url_hydration.get("stored_attachments") else None,
            hydration.get("media_status"),
            extra.get("media_hydration_status"),
        ),
        "vlm_status": _first_materializer_value(hydration.get("vlm_status"), extra.get("vlm_status")),
        "robots_state": _first_materializer_value(hydration.get("robots_state"), extra.get("robots_state")),
        "retry_count": _first_materializer_value(hydration.get("retry_count"), url_hydration.get("retry_count"), pdf_hydration.get("retry_count"), extra.get("retry_count")),
        "failure_reason": _first_materializer_value(hydration.get("failure_reason"), hydration.get("error"), url_hydration.get("error"), pdf_hydration.get("error"), extra.get("failure_reason")),
        "access_path": source_access.get("access_path") or source_attribution.get("access_path") or extra.get("access_path"),
        "observed_source": source_access.get("observed_source") or source_attribution.get("observed_source"),
        "underlying_source": source_access.get("underlying_source") or source_attribution.get("underlying_source"),
        "rights_class": source_access.get("rights_class") or extra.get("rights_class"),
        "rights_state": source_access.get("rights_state") or hydration.get("rights_state") or extra.get("rights_state"),
        "paywall_state": source_access.get("paywall_state") or hydration.get("paywall_state") or extra.get("paywall_state"),
        "license_state": source_access.get("license_state") or extra.get("license_state"),
        "extraction_license": source_access.get("extraction_license") or extra.get("extraction_license"),
        "quote_policy": source_access.get("quote_policy") or extra.get("quote_policy"),
        "summarization_policy": source_access.get("summarization_policy") or extra.get("summarization_policy"),
        "reprocess_policy": source_access.get("reprocess_policy") or extra.get("reprocess_policy"),
        "durable_write_permission": _first_materializer_value(
            source_access.get("durable_write_permission"),
            extra.get("durable_write_permission"),
        ),
        "review_required": _first_materializer_value(source_access.get("review_required"), extra.get("review_required")),
    }
    return {key: value for key, value in payload.items() if value not in (None, "")}


def _family_materializer_source_values(row: Any) -> dict[str, Any]:
    if isinstance(row, StructuredDataFact):
        payload = dict(row.payload or {})
        if row.period_date is not None:
            payload.setdefault("period_date", row.period_date.isoformat())
            payload.setdefault("event_date", row.period_date.isoformat())
            payload.setdefault("observed_at", row.period_date.isoformat())
        if row.as_of_date is not None:
            payload.setdefault("as_of_date", row.as_of_date.isoformat())
        if row.document_id is not None:
            payload.setdefault("source_document_id", int(row.document_id))
        if row.provenance_label:
            payload.setdefault("source_owner", row.provenance_label)
        if row.provenance_url:
            payload.setdefault("source_url", row.provenance_url)
        payload.setdefault("metric_key", row.metric_key)
        payload.setdefault("metric_label", row.metric_label)
        payload.setdefault("value_number", row.value_number)
        payload.setdefault("value_text", row.value_text)
        payload.setdefault("unit", row.unit)
        payload.setdefault("currency", row.currency)
        payload.setdefault("evidence_tier", row.evidence_tier)
        payload.setdefault("statement_type", row.statement_type)
        payload.setdefault("fact_key", row.fact_key)
        return payload
    if isinstance(row, Document):
        return _family_materializer_document_values(row)
    if isinstance(row, dict):
        values = row.get("values")
        if isinstance(values, dict):
            merged = dict(values)
            for key, value in row.items():
                if key != "values":
                    merged.setdefault(str(key), value)
            return merged
        return dict(row)
    return {}
